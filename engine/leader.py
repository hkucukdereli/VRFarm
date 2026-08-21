"""
engine/leader.py

Leader Pi main process (barebones template). Runs a simple 4-phase trial loop —
ITI -> pre-stim -> stim -> post-stim — with no stimulus hardware and no response
contingency: the stim phase is a timed placeholder window that a real task can
grow behavior into. Devices are fully generic: every enabled device in the rig
yaml is instantiated from DEVICE_REGISTRY (module name == device type), streamed
through one generic callback, and saved through the Device HDF5 contract.
Events stream to the Controller over UDP; HDF5 is written locally per trial and
transferred after the session.
"""

from __future__ import annotations
import argparse
import importlib
import json
import random
import signal
import socket
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from devices.base import DEVICE_REGISTRY


class Leader:
    def __init__(self, rig_config: dict, task_config: dict, session: dict,
                 skip_save=None):
        self.rig = rig_config
        self.task = task_config
        self.session = session  # {subject_id, date, session_num, notes}
        # Device names whose HDF5 datasets to SKIP (user unchecked them at GO). Core
        # trial timing columns are written directly and unaffected.
        self._skip_save = set(skip_save or ())
        self.session_id = (f"{session['subject_id']}_{session['date']}"
                           f"_{session['session_num']:03d}")
        self.devices = {}
        self.trial_num = 0
        self.running = False
        self.hdf5_file = None
        self._trial_ctx = {}

        # UDP sockets
        net = rig_config["network"]
        self._event_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock.bind(("0.0.0.0", net["command_port"]))
        self._cmd_sock.setblocking(False)

        # Controller address for events — set when the first command arrives
        self._mac_addr = None

    # ── Device initialization ──

    def init_devices(self):
        leader_pi = next(p for p in self.rig["pis"]
                         if p["role"] == "leader")
        for dev_name, dev_config in self.rig["devices"].items():
            if dev_name not in leader_pi.get("devices", []):
                continue
            if not dev_config.get("enabled", True):
                continue
            dev_type = dev_config["type"]
            if dev_type not in DEVICE_REGISTRY:
                # Devices self-register on import; module name == device type.
                try:
                    importlib.import_module(f"devices.{dev_type}")
                except ImportError as e:
                    print(f"  Device module devices/{dev_type}.py not importable: {e}")
            if dev_type not in DEVICE_REGISTRY:
                print(f"  Unknown device type: {dev_type}")
                continue
            cls = DEVICE_REGISTRY[dev_type]
            dev = cls()
            # Per-device tunables come from the task yaml's `devices:` map
            # (Device.task_params_schema contract); rig yaml carries the hardware config.
            task_params = self.task.get("devices", {}).get(dev_name, {}) or {}
            dev.init(rig_config=dev_config, task_params=task_params)
            if dev.needs_calibration and "calibration" in dev_config:
                dev.load_calibration(dev_config["calibration"])
            self.devices[dev_name] = dev
            print(f"  {dev.info.label}: OK")

    # ── Event forwarding ──

    def _on_device_event(self, name: str, evt: dict):
        # Devices emit dicts carrying `event`; the browser dispatches on evt.type.
        self._publish({**evt, "type": evt.get("event", name)})

    # ── Wait helpers ──

    def _wait(self, duration: float):
        """Wait `duration` seconds while pumping UDP so STOP keeps working."""
        t0 = time.time()
        while self.running and (time.time() - t0) < duration:
            self._check_udp()
            time.sleep(0.005)

    def _run_pre_session_delay(self, delay: float):
        print(f"Pre-session delay: {delay}s")
        t0 = time.time()
        last_pub = 0
        while self.running and (time.time() - t0) < delay:
            self._check_udp()
            remaining = delay - (time.time() - t0)
            # Publish countdown once per second
            now = time.time()
            if now - last_pub >= 1.0:
                self._publish({"type": "countdown",
                               "remaining": round(remaining, 1),
                               "t": now})
                last_pub = now
            time.sleep(0.1)
        if self.running:
            print("Pre-session delay complete. Starting trials.")

    # ── Session loop ──

    def run_session(self):
        print(f"\n=== Session {self.session_id} ===")

        # Start every device's stream through the generic callback
        for name, dev in self.devices.items():
            dev.start_stream(lambda evt, n=name: self._on_device_event(n, evt))

        # Open HDF5 (if h5py available)
        # Nested: subject / subject_date / session_id
        subj = self.session["subject_id"]
        subj_date = f"{subj}_{self.session['date']}"
        leader_dir = (self.rig.get("data", {}) or {}).get("leader_dir")
        if not leader_dir:
            # An empty/missing leader_dir would make Path("") == "." and silently write the
            # session into pi_api's cwd — fall back to a known place with a loud warning instead.
            leader_dir = str(Path.home() / "data")
            print(f"WARNING: rig data.leader_dir is empty — writing session to {leader_dir}", flush=True)
        data_dir = Path(leader_dir).expanduser() / subj / subj_date / self.session_id
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            import h5py
            h5_path = data_dir / f"{self.session_id}.h5"
            self.hdf5_file = h5py.File(str(h5_path), "w")
            self._create_hdf5_datasets()
            print(f"HDF5: {h5_path}")
        except ImportError:
            print("h5py not available — skipping HDF5")
            self.hdf5_file = None

        sess_cfg = self.task.get("session", {})
        n_trials = int(sess_cfg.get("n_trials", 60))
        iti_cfg = sess_cfg.get("iti_s", 5.0)          # scalar or [min, max]
        prestim_s = float(sess_cfg.get("prestim_s", 2.0))
        stim_s = float(sess_cfg.get("stim_s", 2.0))
        poststim_s = float(sess_cfg.get("poststim_s", 2.0))

        self.running = True
        print(f"Running {n_trials} trials "
              f"(iti {iti_cfg}s, prestim {prestim_s}s, stim {stim_s}s, poststim {poststim_s}s)...")

        # Pre-session grace period (task 'session' section; rig config as legacy fallback)
        delay = sess_cfg.get("grace_period_s", self.rig.get("grace_period_s", 0))
        if delay > 0:
            self._publish({"type": "grace_period", "duration": delay,
                           "t": time.time()})
            self._run_pre_session_delay(delay)
            if not self.running:
                self._finalize_session(data_dir, 0, n_trials)
                return

        self._publish({"type": "experiment_start", "t": time.time()})

        trial_num = -1
        for trial_num in range(n_trials):
            if not self.running:
                trial_num -= 1  # this trial never ran
                break
            self.trial_num = trial_num

            if isinstance(iti_cfg, (list, tuple)) and len(iti_cfg) == 2:
                iti_dur = random.uniform(float(iti_cfg[0]), float(iti_cfg[1]))
            else:
                iti_dur = float(iti_cfg)

            trial_start_t = time.time()
            self._trial_ctx = {
                "trial_num": trial_num,
                "trial_start_t": trial_start_t,
            }
            for dev in self.devices.values():
                if hasattr(dev, "reset_trial"):
                    dev.reset_trial()
            self._publish({"type": "trial_start", "trial": trial_num,
                           "t": trial_start_t, "iti": round(iti_dur, 3)})

            # ── ITI ──
            self._wait(iti_dur)
            if not self.running:
                break

            # ── Pre-stim ──
            self._wait(prestim_s)
            if not self.running:
                break

            # ── Stim window (placeholder — no display; a real task hooks in here) ──
            stim_on_t = time.time()
            self._trial_ctx["stim_on_t"] = stim_on_t
            self._publish({"type": "stim", "on": True,
                           "trial": trial_num, "t": stim_on_t})
            self._wait(stim_s)
            stim_off_t = time.time()
            self._trial_ctx["stim_off_t"] = stim_off_t
            self._publish({"type": "stim", "on": False,
                           "trial": trial_num, "t": stim_off_t})
            if not self.running:
                break

            # ── Post-stim ──
            self._wait(poststim_s)

            # ── Record trial ──
            trial_end_t = time.time()
            self._trial_ctx["trial_end_t"] = trial_end_t
            self._write_trial()
            self._publish({"type": "trial", "trial_num": trial_num,
                           "t": trial_end_t, "stim_on_t": stim_on_t,
                           "duration_s": round(trial_end_t - trial_start_t, 3)})

        self._finalize_session(data_dir, trial_num + 1, n_trials)

    def _finalize_session(self, data_dir: Path, n_completed: int, n_planned: int):
        """Clean up after session: write session-level device data, close HDF5, save metadata."""
        self.running = False
        n_completed = max(0, int(n_completed))

        # Stop device streams so their poll threads aren't still appending to their buffers while we
        # snapshot the session-level data below (matters for continuous devices).
        for dev in self.devices.values():
            if hasattr(dev, "stop_stream"):
                try:
                    dev.stop_stream()
                except Exception:
                    pass

        # Write session-level device data (honor the per-session save gate, like the per-trial paths)
        if self.hdf5_file:
            for name, dev in self.devices.items():
                if name in self._skip_save:
                    continue
                for ds_name, arr in dev.hdf5_session_data().items():
                    self.hdf5_file.create_dataset(ds_name, data=arr)
            self.hdf5_file.close()

        self._publish({"type": "session_end", "n_completed": n_completed,
                       "n_planned": n_planned, "t": time.time()})
        print(f"Session complete ({n_completed}/{n_planned} trials). Data: {data_dir}")

        # Save metadata
        meta = {
            "session_id": self.session_id,
            "subject_id": self.session["subject_id"],
            "date": self.session.get("date", ""),
            "session_num": self.session.get("session_num", 1),
            "notes": self.session.get("notes", ""),
            "n_trials_completed": int(n_completed),
            "n_trials_planned": int(n_planned),
            "task_config": self.task,
            "rig_name": self.rig.get("name", ""),
            "timestamp": float(time.time()),
            # Data provenance: which devices' detailed data was recorded this session.
            "saved_devices": sorted(n for n in self.devices if n not in self._skip_save),
            "skipped_devices": sorted(self._skip_save),
        }
        meta_path = data_dir / "metadata.yaml"
        with open(meta_path, "w") as f:
            yaml.dump(meta, f, default_flow_style=False)

    # ── UDP helpers ──

    def _check_udp(self):
        """Check for UDP commands from the Controller (non-blocking)."""
        try:
            data, addr = self._cmd_sock.recvfrom(4096)
            msg = json.loads(data)
            if self._mac_addr is None:
                self._mac_addr = (addr[0], self.rig["network"]["event_port"])
            cmd = msg.get("cmd")
            if cmd == "STOP":
                self.running = False
        except BlockingIOError:
            pass
        except json.JSONDecodeError:
            pass

    def _publish(self, event: dict):
        """Send event to the Controller via UDP."""
        if self._mac_addr:
            self._event_sock.sendto(
                json.dumps(event).encode(), self._mac_addr)

    # ── HDF5 ──

    def _create_hdf5_datasets(self):
        f = self.hdf5_file
        sess = self.task.get("session", {})
        n = int(sess.get("n_trials", 60))

        # Core trial timing
        f.create_dataset("trial_num", shape=(0,), maxshape=(n,), dtype="i4")
        for name in ["trial_start_t", "stim_on_t", "stim_off_t", "trial_end_t"]:
            f.create_dataset(name, shape=(0,), maxshape=(n,), dtype="f8")

        # Device datasets (generalized) — skip devices the user chose not to save.
        for name, dev in self.devices.items():
            if name in self._skip_save:
                continue
            for ds_name, ds_spec in dev.hdf5_datasets().items():
                if isinstance(ds_spec, dict):
                    f.create_dataset(ds_name, shape=(0,), maxshape=(n,), **ds_spec)
                else:
                    f.create_dataset(ds_name, shape=(0,), maxshape=(n,), dtype=ds_spec)

    def _write_trial(self):
        if self.hdf5_file is None:
            return
        f = self.hdf5_file
        i = self.trial_num

        f["trial_num"].resize(i + 1, axis=0)
        f["trial_num"][i] = i
        for name in ["trial_start_t", "stim_on_t", "stim_off_t", "trial_end_t"]:
            f[name].resize(i + 1, axis=0)
            f[name][i] = self._trial_ctx.get(name, float("nan"))

        # Device datasets — skip devices the user chose not to save (datasets were never created).
        for name, dev in self.devices.items():
            if name in self._skip_save:
                continue
            for ds_name, value in dev.hdf5_trial_data(self._trial_ctx).items():
                if ds_name in f:
                    f[ds_name].resize(i + 1, axis=0)
                    f[ds_name][i] = value

        f.flush()

    # ── Shutdown ──

    def shutdown(self):
        self.running = False
        for dev in self.devices.values():
            dev.close()
        self._event_sock.close()
        self._cmd_sock.close()
        # Consolidate into one self-contained .h5 now that every sidecar is finalized —
        # instead of deferring it to transfer.
        self._consolidate_at_exit()

    def _consolidate_at_exit(self):
        """Fold this session's sidecars into the single <session_id>.h5 as the final exit step, so the
        archive is self-contained the moment the session ends (stop OR finish). Runs after dev.close(),
        so metadata.yaml / video sidecars all exist. Idempotent + best-effort: it never raises out of
        shutdown, and transfer still calls consolidate as a no-op safety net."""
        try:
            d = self.rig.get("data", {})
            leader_dir = d.get("leader_dir")
            if not leader_dir:
                return
            subj = self.session["subject_id"]
            subj_date = f"{subj}_{self.session['date']}"
            session_dir = Path(leader_dir) / subj / subj_date / self.session_id
            vd = d.get("video_dir") or leader_dir
            video_dir = Path(vd) / subj / subj_date / self.session_id
            from shared.consolidate import consolidate_session
            res = consolidate_session(session_dir, video_dir if video_dir.exists() else None)
            print(f"[consolidate] session end: {res}", flush=True)
        except FileNotFoundError:
            pass   # no .h5 written (save skipped / no data) — nothing to consolidate
        except Exception as e:
            print(f"[consolidate] session-end consolidation failed ({e}); transfer will retry",
                  flush=True)


# ── CLI entry point ──

def main():
    parser = argparse.ArgumentParser(description="VRFarm Leader")
    parser.add_argument("--rig", required=True, help="Rig config YAML path")
    parser.add_argument("--task", required=True, help="Task config YAML path")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--session-num", type=int, required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--no-save", default="",
                        help="comma-separated device names whose HDF5 datasets to skip")
    args = parser.parse_args()

    # load_rig (not a bare safe_load): it makes the FILENAME the rig's identity, so the
    # `rig_name` stamped into the HDF5 below matches the rig the user actually selected even if
    # this yaml was copied from another rig and kept its old `name:` field.
    from shared.config import load_rig
    rig_config = load_rig(args.rig)
    with open(args.task) as f:
        task_config = yaml.safe_load(f)

    session = {
        "subject_id": args.subject,
        "date": args.date,
        "session_num": args.session_num,
        "notes": args.notes,
    }

    skip_save = {s.strip() for s in args.no_save.split(",") if s.strip()}
    leader = Leader(rig_config, task_config, session, skip_save=skip_save)

    # Handle SIGTERM gracefully (from pi_api stop)
    def _sigterm(sig, frame):
        print("SIGTERM received — stopping gracefully")
        leader.running = False
    signal.signal(signal.SIGTERM, _sigterm)

    print("Initializing devices...")
    leader.init_devices()

    print("Waiting for START command...")
    # Block until we receive START from the Controller
    while True:
        try:
            data, addr = leader._cmd_sock.recvfrom(4096)
            leader._mac_addr = (addr[0], rig_config["network"]["event_port"])
            msg = json.loads(data)
            if msg.get("cmd") == "START":
                break
        except BlockingIOError:
            time.sleep(0.01)
        except json.JSONDecodeError:
            pass  # malformed packet — ignore

    try:
        leader.run_session()
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        leader.shutdown()
        print("Leader shutdown complete")


if __name__ == "__main__":
    main()
