"""
engine/follower.py

Follower Pi main process. Listens for UDP commands from Leader,
drives the display device. Stim params pre-loaded from NPZ.

Leader sends {"cmd": "SHOW", "trial": N} — Follower looks up
trial N from NPZ, renders stim, waits duration, blanks.
"""

from __future__ import annotations
import argparse
import json
import socket
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from devices.base import DEVICE_REGISTRY
import devices.display  # noqa: F401 — register device


class Follower:
    def __init__(self, rig_config: dict):
        self.rig = rig_config
        self.devices = {}
        self.stims = None  # loaded from NPZ
        self.running = False
        self._frame_count = 0
        self._sync_every_n = 0

        net = rig_config["network"]
        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock.bind(("0.0.0.0", net["display_port"]))
        self._cmd_sock.settimeout(1.0)

        # Optional ack socket back to leader
        self._ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._leader_addr = None
        for pi in rig_config["pis"]:
            if pi["role"] == "leader":
                self._leader_addr = (pi["ip"], net.get("event_port", 5571))
                break

    # ── Device initialization ──

    def init_devices(self):
        follower_pi = next(p for p in self.rig["pis"]
                           if p["role"] == "follower")
        for dev_name in follower_pi.get("devices", []):
            dev_config = self.rig["devices"].get(dev_name)
            if not dev_config or not dev_config.get("enabled", True):
                continue
            dev_type = dev_config["type"]
            if dev_type not in DEVICE_REGISTRY:
                print(f"  Unknown device type: {dev_type}")
                continue
            cls = DEVICE_REGISTRY[dev_type]
            dev = cls()
            dev.init(rig_config=dev_config, task_params={})
            self.devices[dev_name] = dev
            print(f"  {dev.info.label}: OK")

    # ── Stim loading ──

    def load_stims(self, npz_path: str):
        """Load pre-generated stim params from NPZ."""
        self.stims = dict(np.load(npz_path, allow_pickle=True))
        n = int(self.stims.get("n_trials", [0])[0])
        self._sync_every_n = int(self.stims.get("sync_square_every_n", [0])[0])
        # Update display background gray from stim config
        if "display" in self.devices and "background_gray" in self.stims:
            self.devices["display"].bg_gray = float(self.stims["background_gray"][0])
        print(f"  Loaded {n} trials from {npz_path}")
        if self._sync_every_n > 0:
            print(f"  Sync square every {self._sync_every_n} frames")

    # ── Main loop ──

    def run(self):
        self.running = True
        timeout_count = 0
        MAX_TIMEOUTS = 60  # 60 consecutive 1s timeouts = 60s of silence
        print("Follower listening for commands...")

        # Start display if present
        if "display" in self.devices:
            display = self.devices["display"]
            display.start_display()
            # Load the warp so stimuli render through the TRUE SPHERICAL path (fills the
            # visible screen, correct visual angle). Without this, display._warp stays None
            # and _handle_show silently falls back to the flat show_rect.
            warp_path = Path.home() / "rig" / "calibration" / "warp_map.npz"
            if display.load_warp(str(warp_path)):
                print(f"  Warp map loaded from {warp_path}")
            else:
                print(f"  No warp map at {warp_path} — stimuli use flat fallback")

        while self.running:
            try:
                data, addr = self._cmd_sock.recvfrom(4096)
                timeout_count = 0  # reset on successful recv
            except socket.timeout:
                timeout_count += 1
                if timeout_count >= MAX_TIMEOUTS:
                    print(f"WARNING: No command from Leader for {MAX_TIMEOUTS}s. Shutting down.")
                    break
                continue
            except OSError:
                break

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            cmd = msg.get("cmd")

            if cmd == "SHOW":
                self._handle_show(msg)
            elif cmd == "BG":
                self._handle_blank()  # show background gray
            elif cmd == "BLANK":
                self._handle_blank()
            elif cmd == "LOAD_STIMS":
                self.load_stims(msg["path"])
            elif cmd == "QUIT":
                print("Received QUIT")
                break

        self.running = False

    def _handle_show(self, msg: dict):
        """Show stimulus for trial N, wait duration, blank."""
        display = self.devices.get("display")
        if display is None or self.stims is None:
            return

        trial = msg["trial"]
        n = int(self.stims.get("n_trials", [0])[0])
        if trial < 0 or trial >= n:
            print(f"  Trial {trial} out of range (0..{n-1})")
            return

        corr_contrast = float(self.stims["corr_contrast"][trial])
        bg_gray = float(self.stims["background_gray"][0])

        # Duration from stim params (added by updated stim_generator)
        if "duration_s" in self.stims:
            duration = float(self.stims["duration_s"][trial])
        else:
            duration = 2.0  # fallback

        # Prefer true spherical rendering (warp loaded + visual-angle size); else flat rect.
        spherical = (getattr(display, "_warp", None) is not None
                     and "stim_size_deg" in self.stims
                     and "stim_az_deg" in self.stims)
        if spherical:
            az = float(self.stims["stim_az_deg"][trial])
            alt = float(self.stims["stim_alt_deg"][trial])
            size_deg = float(self.stims["stim_size_deg"][trial])

            def draw(sync):
                display.show_patch_spherical(az, alt, size_deg, corr_contrast,
                                             bg_gray, sync_square=sync)
        else:
            px_x = float(self.stims["px_x"][trial])
            px_y = float(self.stims["px_y"][trial])
            px_size = float(self.stims["px_size"][trial])

            def draw(sync):
                display.show_rect(px_x, px_y, px_size, corr_contrast,
                                  bg_gray, sync_square=sync)

        if self._sync_every_n > 0:
            # Photodiode sync: per-frame loop, patch ON every Nth frame
            self._show_synced(display, draw, duration, trial)
        else:
            # Single-flip path: one render, hold for duration, blank
            draw(False)
            self._send_onset_ack(trial, time.time())
            time.sleep(duration)
            display.blank()

    def _send_onset_ack(self, trial: int, onset_t: float):
        """Tell the Leader when the first stimulus frame flipped."""
        if self._leader_addr:
            ack = {"type": "stim_onset", "trial": trial, "t": onset_t}
            try:
                self._ack_sock.sendto(
                    json.dumps(ack).encode(), self._leader_addr)
            except OSError:
                pass

    def _show_synced(self, display, draw, duration, trial):
        """Per-frame render loop for photodiode sync. `draw(sync)` renders one frame (rect
        or spherical patch) with the sync square ON every Nth frame starting at frame 0.
        Relies on vsync-locked flip to pace frames; the wall-clock bound guarantees the
        stimulus stays up for `duration` regardless of vsync."""
        onset_t = None
        fi = 0
        while True:
            patch_on = (fi % self._sync_every_n == 0)
            draw(patch_on)
            if fi == 0:
                onset_t = time.time()
                self._send_onset_ack(trial, onset_t)
            fi += 1
            if time.time() - onset_t >= duration:
                break
        display.blank()

    def _handle_blank(self):
        display = self.devices.get("display")
        if display:
            display.blank()

    # ── Shutdown ──

    def shutdown(self):
        self.running = False
        for dev in self.devices.values():
            dev.close()
        self._cmd_sock.close()
        self._ack_sock.close()


# ── CLI entry point ──

def main():
    parser = argparse.ArgumentParser(description="VRFarm Follower")
    parser.add_argument("--rig", required=True, help="Rig config JSON path")
    parser.add_argument("--stims", default=None,
                        help="Pre-generated stim NPZ path")
    args = parser.parse_args()

    import yaml
    with open(args.rig) as f:
        rig_config = yaml.safe_load(f)

    follower = Follower(rig_config)
    print("Initializing devices...")
    follower.init_devices()

    if args.stims:
        follower.load_stims(args.stims)

    try:
        follower.run()
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        follower.shutdown()
        print("Follower shutdown complete")


if __name__ == "__main__":
    main()
