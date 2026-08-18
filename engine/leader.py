"""
engine/leader.py

Leader Pi main process. Runs the trial loop for attention-paradigm
experiments, controls all local devices (lick sensor, reward, camera,
photodiode), sends display commands to Follower(s) via UDP, streams
events to Mac. HDF5 data saved locally, transferred to Mac after session.
"""

from __future__ import annotations
import argparse
import json
import queue
import random
import signal
import socket
import sys
import time
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from devices.base import DEVICE_REGISTRY
import devices.lick_sensor   # noqa: F401 — register device
import devices.reward         # noqa: F401
import devices.camera         # noqa: F401
import devices.photodiode     # noqa: F401
import devices.calibration_probe  # noqa: F401
import devices.encoder        # noqa: F401 — running-wheel encoder


class Leader:
    def __init__(self, rig_config: dict, task_config: dict, session: dict,
                 skip_save=None):
        self.rig = rig_config
        self.task = task_config
        self.session = session  # {subject_id, date, session_num, notes}
        # Device names whose per-trial HDF5 datasets to SKIP (user unchecked them at GO). Core
        # trial outcomes (first_lick_t, trial_outcome, timings) are written directly and unaffected.
        self._skip_save = set(skip_save or ())
        self.session_id = (f"{session['subject_id']}_{session['date']}"
                           f"_{session['session_num']:03d}")
        self.devices = {}
        self.trial_num = 0
        self.running = False
        self.hdf5_file = None
        self._stims = None
        self._lick_queue = queue.Queue()
        self._trial_licks = []
        self._iti_licks = []
        self._trial_ctx = {}
        self._running_speed = 0.0   # latest running-wheel speed (cm/s); updated by _on_encoder,
                                    # available to the trial loop for future run-gating (_running_ok)
        # Photodiode stim-sync (online onset correction)
        self._sync_queue = queue.Queue()
        self._sync_enabled = False
        self._sync_timeout_s = 0.1  # max wait for first photodiode edge per trial

        # UDP sockets
        net = rig_config["network"]
        self._event_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock.bind(("0.0.0.0", net["command_port"]))
        self._cmd_sock.setblocking(False)

        # Follower addresses
        self._follower_addrs = {}
        for pi in rig_config["pis"]:
            if pi["role"] == "follower":
                self._follower_addrs[pi["name"]] = (
                    pi["ip"], net["display_port"])

        # Mac address for events
        self._mac_addr = None  # set when Mac sends first command

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
                print(f"  Unknown device type: {dev_type}")
                continue
            cls = DEVICE_REGISTRY[dev_type]
            dev = cls()
            # Map new YAML structure to device task_params
            if dev_name == "reward":
                rew = self.task.get("reward", {})
                task_params = {"amount_ul": rew.get("amount_ul", 4.0),
                               "amount_mode": rew.get("amount_mode", "volume"),
                               "amount_count": rew.get("amount_count", 1),
                               "pulse_gap_ms": rew.get("pulse_gap_ms", 150.0)}
            elif dev_name == "lick_sensor":
                task_params = {"max_lick_rate": self.task.get("reward", {}).get("max_lick_rate", 0.3)}
            elif dev_name == "display":
                task_params = {"background_gray": self.task.get("stimulus", {}).get("background_gray", 0.0)}
            else:
                task_params = {}
            dev.init(rig_config=dev_config, task_params=task_params)
            if dev.needs_calibration and "calibration" in dev_config:
                dev.load_calibration(dev_config["calibration"])
            self.devices[dev_name] = dev
            print(f"  {dev.info.label}: OK")

    # ── Go / No-Go classification ──

    def _classify_go(self, az_deg: float) -> bool:
        rule = self.task.get("stimulus", {}).get("go_rule", "left")
        if rule == "all":
            return True
        if rule == "right":
            return az_deg > 0
        return az_deg < 0  # default: left = go

    # ── Actions ──

    def _action_show_stim(self):
        self._send_follower({"cmd": "SHOW", "trial": self.trial_num})
        t = time.time()
        self._trial_ctx["stim_onset_t"] = t
        self._publish({"type": "stim", "on": True,
                       "trial": self.trial_num, "t": t,
                       "az": self._trial_ctx.get("stim_az", 0)})

    def _action_deliver_reward(self):
        if "reward" in self.devices:
            ms = self.devices["reward"].deliver()
            t = time.time()
            self._trial_ctx["reward_t"] = t
            self._trial_ctx["reward_amount_ul"] = self.devices["reward"].effective_amount_ul
            self._trial_ctx["rewarded"] = True
            self._publish({"type": "reward", "on": True,
                           "t": t, "pulse_ms": ms})

    # ── Lick handling ──

    def _on_lick(self, event: dict):
        self._lick_queue.put(event["t"])
        self._publish({"type": "lick", "t": event["t"]})

    def _on_sync_pulse(self, evt: dict):
        """Photodiode rising edge: queue for online onset detection, publish to Mac."""
        if evt.get("event") == "sync_pulse":
            try:
                self._sync_queue.put_nowait(evt["t"])
            except queue.Full:
                pass
        # Publish with a `type` key: the device dict carries `event`, but the browser dispatches on
        # switch(evt.type), so without this the live SYNC raster (case 'sync_pulse') never populates.
        self._publish({**evt, "type": evt.get("event", "sync_pulse")})

    def _on_encoder(self, evt: dict):
        """Running-wheel sample: cache the live speed (for future gating) and publish to the Mac."""
        self._running_speed = float(evt.get("speed_cms", 0.0) or 0.0)
        self._publish({"type": "running", "t": evt.get("t"),
                       "speed_cms": evt.get("speed_cms"), "distance_cm": evt.get("distance_cm")})

    def _running_ok(self) -> bool:
        """Hook for future trial-gating: is the mouse running fast enough to start a trial?
        Reads session.run_gate_cms (default 0 = disabled → always True). Not yet enforced anywhere."""
        gate = float(self.task.get("session", {}).get("run_gate_cms", 0) or 0)
        return self._running_speed >= gate

    def _drain_lick_queue(self) -> list[float]:
        """Drain all pending lick timestamps from the queue."""
        licks = []
        while not self._lick_queue.empty():
            try:
                licks.append(self._lick_queue.get_nowait())
            except queue.Empty:
                break
        return licks

    # ── ITI helper ──

    def _timeout_phases(self) -> set:
        """Which trial phases the timeout rule enforces (reward.timeout_phases, set from
        the Pre-stim / Post-stim / ITI checkboxes). Default ['iti'] = prior behavior."""
        return set(self.task.get("reward", {}).get("timeout_phases", ["iti"]))

    def _run_enforced_wait(self, planned_duration: float, enforce: bool,
                           lick_sink: list, restart_event: str,
                           update_lick_count: bool = False) -> float:
        """Wait `planned_duration`s, draining licks into `lick_sink`. When `enforce` is
        True, apply the timeout rule and RESTART the period whenever the mouse over-licks
        (so the wait extends until it stays quiet). Delivers no rewards. Returns actual
        elapsed duration.
          timeout_rule 'none' -> no enforcement (plain wait),
                       'rate' -> restart if lick rate in the last 1 s reaches max_lick_rate,
                       'count' -> restart if licks this period exceed max_lick_rate*duration.
        """
        rew = self.task.get("reward", {})
        timeout_rule = rew.get("timeout_rule", "none")
        max_lick_rate = rew.get("max_lick_rate", 0)
        enforce = enforce and timeout_rule in ("rate", "count") and max_lick_rate > 0

        start = time.time()
        period_start = start
        period_licks = 0
        max_lick_count = (int(max_lick_rate * planned_duration)
                          if timeout_rule == "count" else 0)

        while self.running:
            self._check_udp()
            if not self.running:
                break

            new_licks = self._drain_lick_queue()
            lick_sink.extend(new_licks)
            period_licks += len(new_licks)
            if update_lick_count:
                self._trial_ctx["lick_count"] = len(self._trial_licks)

            now = time.time()
            if now - period_start >= planned_duration:
                break

            if enforce and timeout_rule == "rate":
                # Real-time rate: count licks in the last 1 second
                cutoff = now - 1.0
                recent = sum(1 for t in lick_sink if t >= cutoff)
                if recent >= max_lick_rate:
                    period_start = now
                    period_licks = 0
                    self._publish({"type": restart_event, "t": period_start})
            elif enforce and timeout_rule == "count" and max_lick_count > 0:
                # Count mode: restart if total period licks exceed budget
                if period_licks > max_lick_count:
                    period_start = now
                    period_licks = 0
                    self._publish({"type": restart_event, "t": period_start})

            time.sleep(0.005)

        return time.time() - start

    def _run_iti(self, planned_duration: float) -> float:
        """Run an ITI period, enforcing the timeout rule if 'iti' is in timeout_phases."""
        self._iti_licks = []
        return self._run_enforced_wait(
            planned_duration, enforce=("iti" in self._timeout_phases()),
            lick_sink=self._iti_licks, restart_event="iti_restart")

    # ── Wait-for-event helper ──

    def _wait_for_event(self, duration: float, lick_reward: bool = False) -> bool:
        """Wait for `duration` seconds, polling licks and UDP. If `lick_reward` is set, the first
        lick (when the trial is not already rewarded) delivers the reward and returns early — this
        is the operant path (L2/L3 go trials). Returns True if that operant reward fired.
        """
        t0 = time.time()
        while self.running and (time.time() - t0) < duration:
            self._check_udp()
            new_licks = self._drain_lick_queue()
            for lick_t in new_licks:
                self._trial_licks.append(lick_t)
                self._trial_ctx["lick_count"] = len(self._trial_licks)

                if lick_reward and not self._trial_ctx.get("rewarded", False):
                    self._action_deliver_reward()
                    return True

            time.sleep(0.001)
        return False

    # ── Pre-session delay ──

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

        # Generate stimuli
        self._stims = self._generate_stims()

        # Start lick streaming
        if "lick_sensor" in self.devices:
            self.devices["lick_sensor"].start_stream(self._on_lick)

        # Start photodiode streaming
        if "photodiode" in self.devices:
            self.devices["photodiode"].start_stream(self._on_sync_pulse)

        # Start running-wheel encoder streaming
        if "encoder" in self.devices:
            self.devices["encoder"].start_stream(self._on_encoder)
        self._sync_enabled = (
            "photodiode" in self.devices
            and self.task.get("stimulus", {}).get("photodiode_sync_enabled", False))
        if self._sync_enabled:
            print("Photodiode stim-sync: ON (online onset correction)")

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
            print("h5py not available — skipping HDF5, using JSON fallback")
            self.hdf5_file = None

        sess_cfg = self.task["session"]
        block_size = sess_cfg.get("block_size", 25)
        n_blocks = sess_cfg.get("n_blocks", sess_cfg.get("n_trials", 150) // max(block_size, 1))
        n_trials = n_blocks * block_size
        # Global timeout: abort the whole session (as if STOP) when the mouse produces no
        # licks in the selected phases for `gt_n` consecutive trials. 0 trials or no phases
        # selected = disabled.
        gt_n = int(sess_cfg.get("global_timeout_trials", 0) or 0)
        gt_phases = set(sess_cfg.get("global_timeout_phases", []) or [])
        gt_active = gt_n > 0 and bool(gt_phases)
        gt_dry_streak = 0
        reward_cfg = self.task.get("reward", {})
        nominal_level = float(reward_cfg.get("level", 1))
        adaptive_state = self.task.get("adaptive", {}).get("initial_state", 0.0)
        adaptive_cfg = self.task.get("adaptive", {})
        # resp_delay_s: delay from stim onset before the response window opens (was reward_delay_s;
        # the fallback keeps any pre-rename experiment yaml working).
        reward_delay = reward_cfg.get("resp_delay_s", reward_cfg.get("reward_delay_s", 0.6))
        rw_dur = reward_cfg.get("response_window", 1.4)
        pav_delay = reward_cfg.get("pav_delay_s", 0.0)
        visual_dur = self.task.get("stimulus", {}).get("duration_s", 2.0)

        # Per-trial durations from NPZ (pre-generated)
        prestim_durs = self._stims.get("prestim_durations") if self._stims else None
        poststim_durs = self._stims.get("poststim_durations") if self._stims else None
        block_delays = self._stims.get("block_delays") if self._stims else None
        block_delay_skip_first = bool(
            self._stims.get("block_delay_skip_first", [False])[0]
        ) if self._stims else False
        global_delay_dur = float(
            self._stims.get("global_delay", [0])[0]
        ) if self._stims else 0

        self.running = True
        print(f"Running {n_trials} trials (level {reward_cfg.get('level', '?')})...")

        # Pre-session grace period (task 'session' section; rig config as legacy fallback)
        delay = self.task.get("session", {}).get(
            "grace_period_s", self.rig.get("grace_period_s", 0))
        if delay > 0:
            self._publish({"type": "grace_period", "duration": delay,
                           "t": time.time()})
            self._run_pre_session_delay(delay)
            if not self.running:
                self._finalize_session(data_dir, 0, n_trials)
                return

        self._publish({"type": "experiment_start", "t": time.time()})
        # Tell Follower to show background gray
        self._send_follower({"cmd": "BG"})

        # ── Global delay ──
        if global_delay_dur > 0:
            self._publish({"type": "global_delay", "duration": global_delay_dur,
                           "t": time.time()})
            self._wait_for_event(global_delay_dur)
            if not self.running:
                self._finalize_session(data_dir, 0, n_trials)
                return

        current_block = -1  # track block transitions
        trial_num = 0
        for trial_num in range(n_trials):
            if not self.running:
                break
            self.trial_num = trial_num

            # ── Block delay (on block transition) ──
            trial_block = int(self._stims["block_num"][trial_num]) if self._stims else 0
            if trial_block != current_block:
                current_block = trial_block
                if block_delays is not None:
                    skip = block_delay_skip_first and current_block == 0
                    if not skip:
                        bd = float(block_delays[current_block % len(block_delays)])
                        if bd > 0:
                            self._publish({"type": "block_delay",
                                           "block": current_block,
                                           "duration": bd, "t": time.time()})
                            self._wait_for_event(bd)
                            if not self.running:
                                break

            # ── ITI before this trial ──
            iti_dur = float(self._stims["iti_durations"][trial_num]) if self._stims else 8.0
            iti_start_t = time.time()
            self._publish({"type": "iti_start", "trial": trial_num,
                           "t": iti_start_t, "duration": iti_dur})

            iti_actual = self._run_iti(iti_dur)

            if not self.running:
                break

            # ── Resolve level ──
            effective_level = nominal_level
            if nominal_level == 2.5:
                effective_level = 3.0 if random.random() < adaptive_state else 2.0

            # ── Setup trial context ──
            stim = {}
            if self._stims:
                for k in self._stims:
                    arr = self._stims[k]
                    if hasattr(arr, 'shape') and len(arr.shape) == 1 and arr.shape[0] == n_trials:
                        stim[k] = arr[trial_num]

            self._trial_ctx = {
                "trial_num": trial_num,
                "level": effective_level,
                "nominal_level": nominal_level,
                "adaptive_state": adaptive_state,
                "stim_az": float(stim.get("stim_az_deg", 0)),
                "is_go": self._classify_go(float(stim.get("stim_az_deg", 0))),
                "rewarded": False,
                "reward_t": float("nan"),
                "reward_amount_ul": 0.0,
                "iti_start_t": iti_start_t,
                "iti_duration_s": iti_actual,
                "iti_lick_times": self._iti_licks[:],
                "iti_lick_count": len(self._iti_licks),
                "lick_count": 0,
            }
            self._trial_licks = []

            # Drain stale licks from ITI
            self._drain_lick_queue()

            # Reset photodiode
            if "photodiode" in self.devices:
                self.devices["photodiode"].reset_trial()
            # Mark running-distance baseline for this trial
            if "encoder" in self.devices:
                self.devices["encoder"].reset_trial()
            # Drain stale sync pulses from prior trial
            while not self._sync_queue.empty():
                try:
                    self._sync_queue.get_nowait()
                except queue.Empty:
                    break

            # ── Trial start ──
            trial_start_t = time.time()
            self._trial_ctx["trial_start_t"] = trial_start_t
            self._publish({"type": "trial_start", "trial": trial_num,
                           "t": trial_start_t})

            # ── Pre-stimulus period (baseline lick collection) ──
            prestim_dur = float(prestim_durs[trial_num]) if prestim_durs is not None else 0
            if prestim_dur > 0:
                self._run_enforced_wait(
                    prestim_dur, enforce=("prestim" in self._timeout_phases()),
                    lick_sink=self._trial_licks, restart_event="prestim_restart",
                    update_lick_count=True)
                if not self.running:
                    break
                self._trial_ctx["prestim_lick_count"] = len(self._trial_licks)

            # Licks so far this trial all belong to the pre-stim phase (list was reset
            # at trial setup; pre-stim is the only phase before the stimulus).
            n_licks_prestim = len(self._trial_licks)

            # ── Stimulus ON ──
            self._action_show_stim()
            stim_onset_t = self._trial_ctx["stim_onset_t"]

            # ── Online photodiode sync: anchor timeline to true display onset ──
            if self._sync_enabled:
                try:
                    true_onset_t = self._sync_queue.get(timeout=self._sync_timeout_s)
                    self._trial_ctx["sync_ok"] = True
                except queue.Empty:
                    true_onset_t = stim_onset_t  # wire off / no pulse: fall back
                    self._trial_ctx["sync_ok"] = False
            else:
                true_onset_t = stim_onset_t
                self._trial_ctx["sync_ok"] = False
            self._trial_ctx["true_onset_t"] = true_onset_t
            self._trial_ctx["display_latency_s"] = true_onset_t - stim_onset_t

            # Response-delay phase (default 0.6s) — anchored to TRUE onset so the response window
            # opens resp_delay_s after the mouse actually sees the stim. No reward here: every
            # reward is delivered relative to the response-window start (below).
            self._wait_for_event(max(0.0, reward_delay - (time.time() - true_onset_t)))

            # ── Response window ──
            rw_start_t = time.time()
            self._trial_ctx["response_window_t"] = rw_start_t
            is_go = self._trial_ctx["is_go"]

            # Reward rule by level — GO trials only (no-go is never rewarded); all timing is
            # relative to the response-window start:
            #   L1 — free (Pavlovian) reward at window entry.
            #   L2 — operant: first in-window lick rewards (ends the window early). If no lick by
            #        pav_delay_s, a free "Pavlovian rescue" reward fires, then the window runs out.
            #   L3 — pure operant: first lick anywhere in the window rewards; no rescue (pav ignored).
            #   L2.5 draws L2 vs L3 per trial (resolved into effective_level above).
            if effective_level == 1:
                if is_go and not self._trial_ctx["rewarded"]:
                    self._action_deliver_reward()               # free reward at window entry
                self._wait_for_event(rw_dur)                    # collect licks, no further reward
            else:
                operant = (effective_level in (2, 3)) and is_go
                if effective_level == 2 and operant and pav_delay > 0:
                    # L2: operant with a Pavlovian rescue at pav_delay.
                    pav = min(pav_delay, rw_dur)
                    self._wait_for_event(pav, lick_reward=True)
                    if not self._trial_ctx["rewarded"]:
                        self._action_deliver_reward()           # free rescue
                        self._trial_ctx["pavlovian_rescue"] = True
                        self._wait_for_event(max(0.0, rw_dur - pav))
                    # else: the operant lick already ended the window early
                else:
                    # Pure operant (L3, or L2 with pav disabled): a lick anywhere rewards.
                    self._wait_for_event(rw_dur, lick_reward=operant)

            # ── Stim OFF event — no waiting; Follower runs for visual_dur independently ──
            stim_off_t = true_onset_t + visual_dur
            self._publish({"type": "stim", "on": False,
                           "trial": self.trial_num,
                           "t": stim_off_t})

            # ── Post-stimulus period ──
            n_licks_poststim = 0
            poststim_dur = float(poststim_durs[trial_num]) if poststim_durs is not None else 0
            if poststim_dur > 0:
                # Wait for stim to actually end on Follower
                remaining_stim = stim_off_t - time.time()
                if remaining_stim > 0:
                    self._wait_for_event(remaining_stim)
                self._publish({"type": "poststim_start", "trial": self.trial_num,
                               "t": max(stim_off_t, time.time()),
                               "duration": poststim_dur})
                n_before_poststim = len(self._trial_licks)
                self._run_enforced_wait(
                    poststim_dur, enforce=("poststim" in self._timeout_phases()),
                    lick_sink=self._trial_licks, restart_event="poststim_restart",
                    update_lick_count=True)
                if not self.running:
                    break
                n_licks_poststim = len(self._trial_licks) - n_before_poststim
            else:
                self._wait_for_event(0.5)  # brief pause before outcome when no post-stim

            # ── Outcome ── hit/miss and the response latency are measured from licks WITHIN the
            # response window [response_window_t, +rw_dur]. Pre-stim (anticipatory) and post-stim
            # licks do NOT count as a response — for pavlovian trials too: the reward may be free,
            # but the OUTCOME is still whether the mouse licked inside the window.
            outcome_t = time.time()
            self._trial_ctx["outcome_t"] = outcome_t
            rw_open = self._trial_ctx.get("response_window_t", true_onset_t)
            rw_close = rw_open + rw_dur
            window_licks = [t for t in self._trial_licks if rw_open <= t < rw_close]
            self._trial_ctx["responded"] = bool(window_licks)               # in-window lick = hit
            self._trial_ctx["n_window_licks"] = len(window_licks)           # # licks in the window
            self._trial_ctx["first_lick_t"] = window_licks[0] if window_licks else float("nan")
            self._trial_ctx["lick_times"] = self._trial_licks[:]            # full record (all phases)

            # ── Record trial ──
            self._write_trial(stim)
            self._publish_trial_event(stim)

            # ── Update adaptive state ──
            # Level 2.5 IS the adaptive mode (adaptive is implied by 2.5, forced on in
            # the UI), so step whenever nominal_level==2.5 — matching the unconditional
            # 2.5 draw at line 403 (previously the `enabled` flag could desync them).
            if nominal_level == 2.5:
                licked = bool(self._trial_ctx.get("responded"))   # step on the in-window response
                if licked:
                    adaptive_state += adaptive_cfg.get("step_up", 0.2)
                else:
                    adaptive_state -= adaptive_cfg.get("step_down", 0.2)
                adaptive_state = max(0.0, min(1.0, adaptive_state))

            # ── Global timeout: abort if the mouse is dry for gt_n consecutive trials ──
            # A trial is "dry" when it produced no licks in ANY of the selected phases.
            # (iti_lick_count is the ITI that ran immediately before this trial.)
            if gt_active:
                n_licks_stim = len(self._trial_licks) - n_licks_prestim - n_licks_poststim
                phase_licks = {
                    "prestim": n_licks_prestim,
                    "stim": n_licks_stim,
                    "poststim": n_licks_poststim,
                    "iti": self._trial_ctx.get("iti_lick_count", 0),
                }
                dry = sum(phase_licks[p] for p in gt_phases) == 0
                gt_dry_streak = gt_dry_streak + 1 if dry else 0
                if gt_dry_streak >= gt_n:
                    self._publish({"type": "global_timeout", "trial": trial_num,
                                   "n_dry": gt_dry_streak,
                                   "phases": sorted(gt_phases), "t": time.time()})
                    print(f"Global timeout: no licks in {sorted(gt_phases)} for "
                          f"{gt_dry_streak} consecutive trials — aborting session.")
                    self.running = False
                    break

        # ── Trailing ITI ──
        if self.running and self._stims is not None:
            trailing_dur = float(self._stims["iti_durations"][n_trials])
            self._run_iti(trailing_dur)

        self._finalize_session(data_dir, trial_num + 1, n_trials)

    def _finalize_session(self, data_dir: Path, n_completed: int, n_planned: int):
        """Clean up after session: write session-level device data, close HDF5, save metadata."""
        self.running = False

        # Stop device streams so their poll threads aren't still appending to their buffers while we
        # snapshot the session-level data below (matters for continuous devices like the encoder).
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
            "level": self.task.get("reward", {}).get("level", "?"),
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
        """Check for UDP commands from Mac (non-blocking)."""
        try:
            data, addr = self._cmd_sock.recvfrom(4096)
            msg = json.loads(data)
            if self._mac_addr is None:
                self._mac_addr = (addr[0], self.rig["network"]["event_port"])
            cmd = msg.get("cmd")
            if cmd == "STOP":
                self.running = False
            elif cmd == "REWARD":
                self._action_deliver_reward()
        except BlockingIOError:
            pass
        except json.JSONDecodeError:
            pass

    # ── Communication ──

    def _send_follower(self, msg: dict):
        """UDP to all followers."""
        data = json.dumps(msg).encode()
        for addr in self._follower_addrs.values():
            self._event_sock.sendto(data, addr)

    def _publish(self, event: dict):
        """Send event to Mac via UDP."""
        if self._mac_addr:
            self._event_sock.sendto(
                json.dumps(event).encode(), self._mac_addr)

    def _publish_trial_event(self, stim: dict):
        ctx = self._trial_ctx
        level = ctx.get("level", 0)
        # ctx["level"] is the RESOLVED level (2.0/3.0, never 2.5), so operant = L3.
        is_operant = (level == 3)
        rw_t = ctx.get("response_window_t", 0)

        # Adaptive state = P(this trial is L3) — only meaningful when the configured
        # (nominal) level is 2.5 (which IS adaptive). For any deterministic level
        # (L1/L2/L3) the level is certain, so report 1. This gate matches the draw at
        # line 403 and the state step, so publish / table / HDF5 all agree.
        adaptive_on = (ctx.get("nominal_level") == 2.5)
        adaptive_state = ctx.get("adaptive_state", 0.0) if adaptive_on else 1.0

        # Reaction time: the first in-window lick (first_lick_t, set at outcome), relative to
        # the response-window opening. NaN (no in-window response) -> no RT.
        fl = ctx.get("first_lick_t", float("nan"))
        rt_ms = round((fl - rw_t) * 1000, 1) if fl == fl else None   # fl != fl only when NaN

        self._publish({
            "type": "trial",
            "trial_num": self.trial_num,
            "stim_az": float(stim.get("stim_az_deg", 0)),
            "contrast": float(stim.get("contrast", 0)),
            "outcome": "hit" if ctx.get("responded") else "miss",
            "reward_type": "operant" if is_operant else "pavlovian",
            "rt_ms": rt_ms,
            "resp_licks": ctx.get("n_window_licks", 0),   # licks within the response window
            "level": level,
            "adaptive_state": adaptive_state,
            "t": time.time(),
        })

    # ── Stim generation ──

    def _generate_stims(self):
        """Generate or load pre-generated stimuli."""
        try:
            from shared.stim_generator import generate_stimuli
            subj = self.session["subject_id"]
            subj_date = f"{subj}_{self.session['date']}"
            stim_dir = Path(self.rig["data"]["leader_dir"]) / subj / subj_date / self.session_id
            stim_dir.mkdir(parents=True, exist_ok=True)
            # Load warp map if available
            warp_map = None
            warp_path = Path.home() / "rig" / "calibration" / "warp_map.npz"
            if warp_path.exists():
                warp_map = np.load(str(warp_path))
            metric = self.rig["devices"].get("display", {}).get("contrast_metric", "weber")
            return generate_stimuli(self.task, warp_map, str(stim_dir),
                                    contrast_metric=metric)
        except Exception as e:
            print(f"  Stim generation failed: {e}")
            return None

    # ── HDF5 ──

    def _create_hdf5_datasets(self):
        import h5py
        f = self.hdf5_file
        sess = self.task["session"]
        bsize = sess.get("block_size", 25)
        n = sess.get("n_blocks", sess.get("n_trials", 150) // max(bsize, 1)) * bsize

        # Core timing (all f8 for full precision)
        for name in ["iti_start_t", "stim_onset_t", "response_window_t",
                      "outcome_t", "first_lick_t",
                      "true_onset_t", "display_latency_s"]:
            f.create_dataset(name, shape=(0,), maxshape=(n,), dtype="f8")
        f.create_dataset("sync_ok", shape=(0,), maxshape=(n,), dtype="i1")

        # Core trial data
        f.create_dataset("trial_num", shape=(0,), maxshape=(n,), dtype="i4")
        f.create_dataset("block_num", shape=(0,), maxshape=(n,), dtype="i4")
        f.create_dataset("iti_duration_s", shape=(0,), maxshape=(n,), dtype="f4")
        f.create_dataset("stim_az_deg", shape=(0,), maxshape=(n,), dtype="f4")
        # contrast = raw value in the configured metric (what the UI shows / user set);
        # corr_contrast = the normalized headroom fraction actually rendered.
        f.create_dataset("contrast", shape=(0,), maxshape=(n,), dtype="f4")
        f.create_dataset("corr_contrast", shape=(0,), maxshape=(n,), dtype="f4")
        f.create_dataset("trial_outcome", shape=(0,), maxshape=(n,),
                         dtype=h5py.vlen_dtype(str))
        f.create_dataset("level_effective", shape=(0,), maxshape=(n,), dtype="f4")
        f.create_dataset("adaptive_state", shape=(0,), maxshape=(n,), dtype="f4")

        # Pre-generated table (saved once, full arrays)
        if self._stims and "iti_durations" in self._stims:
            f.create_dataset("_iti_durations_planned",
                             data=self._stims["iti_durations"])

        # Device datasets (generalized) — skip devices the user chose not to save.
        for name, dev in self.devices.items():
            if name in self._skip_save:
                continue
            for ds_name, ds_spec in dev.hdf5_datasets().items():
                if isinstance(ds_spec, dict):
                    f.create_dataset(ds_name, shape=(0,), maxshape=(n,), **ds_spec)
                else:
                    f.create_dataset(ds_name, shape=(0,), maxshape=(n,), dtype=ds_spec)

    def _write_trial(self, stim: dict):
        if self.hdf5_file is None:
            return
        f = self.hdf5_file
        i = self.trial_num

        # Core timing
        for name in ["iti_start_t", "stim_onset_t", "response_window_t",
                      "outcome_t", "first_lick_t",
                      "true_onset_t", "display_latency_s"]:
            f[name].resize(i + 1, axis=0)
            f[name][i] = self._trial_ctx.get(name, float("nan"))
        f["sync_ok"].resize(i + 1, axis=0)
        f["sync_ok"][i] = 1 if self._trial_ctx.get("sync_ok") else 0

        # Core trial data
        for name in ["trial_num", "block_num", "iti_duration_s",
                      "stim_az_deg", "contrast", "corr_contrast",
                      "trial_outcome", "level_effective", "adaptive_state"]:
            f[name].resize(i + 1, axis=0)

        f["trial_num"][i] = i
        f["block_num"][i] = int(stim.get("block_num", 0))
        f["iti_duration_s"][i] = self._trial_ctx.get("iti_duration_s", 0)
        f["stim_az_deg"][i] = float(stim.get("stim_az_deg", 0))
        f["contrast"][i] = float(stim.get("contrast", 0))            # raw metric value
        f["corr_contrast"][i] = float(stim.get("corr_contrast", 0))  # rendered fraction
        f["trial_outcome"][i] = "hit" if self._trial_ctx.get("responded") else "miss"
        f["level_effective"][i] = self._trial_ctx.get("level", 0)
        # Same gate as the published event (line 643): P(L3) for adaptive 2.5, else 1 —
        # so HDF5, the trial event, and the live table all report the same number.
        f["adaptive_state"][i] = (self._trial_ctx.get("adaptive_state", 0.0)
                                  if self._trial_ctx.get("nominal_level") == 2.5 else 1.0)

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
        self._send_follower({"cmd": "QUIT"})
        for dev in self.devices.values():
            dev.close()
        self._event_sock.close()
        self._cmd_sock.close()
        # Consolidate into one self-contained .h5 now that every sidecar is finalized (the camera's
        # frame_timestamps.npy is flushed above in dev.close()) — instead of deferring it to transfer.
        self._consolidate_at_exit()

    def _consolidate_at_exit(self):
        """Fold this session's sidecars into the single <session_id>.h5 as the final exit step, so the
        archive is self-contained the moment the session ends (stop OR finish). Runs after dev.close(),
        so metadata.yaml / stimuli.npz / frame_timestamps.npy all exist. Idempotent + best-effort: it
        never raises out of shutdown, and transfer still calls consolidate as a no-op safety net."""
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

    import yaml
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
    # Block until we receive START from Mac
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
