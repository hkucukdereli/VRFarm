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
import select
import signal
import socket
import struct
import sys
import time
from collections import deque
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
import devices.opto           # noqa: F401 — optogenetic stimulator (laser TTL + masking LED)
import devices.ir_led         # noqa: F401 — IR illuminator for the behaviour camera (MCP4725 DAC)
from shared.stim_generator import is_go_trial


# ── Per-trial photodiode onset window ──
# The sync square pulses every `photodiode_sync_every_n` FRAMES, so after the stimulus flips
# the first pulse can be anything up to every_n frames away. A fixed 100 ms budget silently
# lost most trials at every_n=5 (87 ms at 57.46 Hz, before the flip itself): they recorded
# sync_ok=0 and fell back to the displayd ack, which keeps the timeline roughly right and so
# never looked broken. Derive the window from the cadence instead of hardcoding it — waiting
# longer is free, because the reward-delay phase below is an ABSOLUTE deadline anchored to
# true_onset_t and absorbs whatever the wait consumed.
SYNC_FLIP_BUDGET_S = 0.05     # SHOW send -> photons: UDP hop + up to one frame + slop
SYNC_TIMEOUT_MARGIN_S = 0.03  # jitter headroom
SYNC_TIMEOUT_MIN_S = 0.10     # never tighter than the historical value
SYNC_TIMEOUT_MAX_S = 0.30     # and never long enough to crowd reward_delay_s
# An edge earlier than this after the SHOW send cannot be this stimulus: genuine SHOW->photons
# latencies start at ~57 ms (40 sessions, 2026-11), while a heartbeat flash commanded just
# before SHOW lands its sub-pulses 3-40 ms after it — 1.2 % of confirmed trials had anchored
# their onset (and the response window, and now the laser) on one of those.
SYNC_MIN_LATENCY_S = 0.045


# ── L3 display heartbeat (displayd/PROTOCOL.md "Leader-clocked L3 heartbeats") ──
# Between stim windows the leader flashes the display's sync square on its OWN clock and
# expects the photodiode to see it: one closed loop that proves projector + optics + wiring
# are still alive during the long stretches where nothing else is drawn.
HB_PERIOD_DEFAULT_S = 5.0     # task devices.photodiode.heartbeat_period_s overrides
HB_CLUSTER_GAP_S = 0.030      # a gap this large starts a new flash cluster (one flash is
                              # 2-3 sub-pulses at ~8.7 ms)
HB_WIN_PRE_S = 0.020          # confirm window around the ARRIVAL of displayd's flip report on
HB_WIN_POST_S = 0.070         # this Pi: [t_rx - 20 ms, t_rx + 70 ms]; the light follows 32-59 ms
                              # after it, median 43 (cheddar + morbier, 1280 flashes, 2026-11-20)
HB_EVAL_AGE_S = 0.2           # judge a heartbeat once it is this old (and its window has closed)
HB_FLIP_FALLBACK_S = 0.05     # no flip report from displayd -> assume one frame + slop
HB_CLUSTER_KEEP_S = 2.0       # cluster onsets older than this can never match a pending hb
HB_LOST_N = 3                 # consecutive unconfirmed heartbeats -> alarm
HB_OK_N = 2                   # consecutive confirmed heartbeats -> recovery
HB_REALERT_S = 60.0           # re-alert cadence while the alarm persists
HB_SETTLE_S = 0.15            # hold SHOW until a flash commanded this recently has landed
                              # (flash latency up to ~0.12 s + its 2-3 sub-pulses)
# The window hangs on the moment the flip report REACHES this Pi (kernel receive stamp, the
# leader's clock, like the pulse times) and never on the flip time written inside the report,
# which is the follower's clock: a follower 34 ms behind its leader read as a dead display for
# whole sessions while every stimulus was confirmed (morbier, 2026-11-16/18). The time inside
# the report is only set against the arrival, to MEASURE the offset between the two clocks.
HB_CLOCK_WARN_S = 0.010       # |follower clock - leader clock| above this is said once a session
HB_CLOCK_WARN_N = 5           # ... judged on the median of this many flip reports
# A lost heartbeat means "the loop is broken", not "the projector is dark" — the display and the
# whole sensor chain (diode -> amp -> Teensy -> TTL -> GPIO16) fail with the SAME signature, and
# L1/L2 cannot separate them (an EVM barrel pull back-feeds DLPC logic through the Pi header, so
# the status bits still claim healthy LEDs while the room is dark). The Teensy's 1 Hz "B
# <floor> <ceil>" idle line is the discriminator: it reports the analog front end independently
# of whether any pulse was detected. Absent = the sensor chain died; present-and-dark = the front
# end is alive and genuinely sees no light; present-and-bright = light is arriving but the digital
# leg (Teensy OUT pin 16 -> GPIO16) is broken.
HB_TELEMETRY_STALE_S = 3.0    # B line older than this = Teensy serial is silent

# ── Arrival time of the follower's flip reports ──
# SO_TIMESTAMPNS: the kernel stamps a datagram when it enters the network stack (CLOCK_REALTIME,
# the clock behind time.time()) and hands the stamp over as ancillary data, so the arrival time
# does not depend on when this process gets round to reading the socket. Python's socket module
# has no name for the option; 35 is its number on Linux (asm-generic, so arm64 and x86-64), and
# the ancillary message carries the same number as its type.
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
_TIMESPEC = struct.Struct("@ll")   # struct timespec {tv_sec, tv_nsec}, native longs
RX_STAMP_MAX_AGE_S = 5.0           # a stamp older than this, or in the future, is not believed


def _enable_rx_stamp(sock) -> bool:
    """Ask the kernel for a receive stamp on every datagram of `sock`. False where the option
    does not exist (not Linux) or is refused; _recv_stamped then falls back to the read time."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        return True
    except OSError:
        return False


def _recv_stamped(sock, bufsize: int = 4096):
    """One datagram and the time it reached this machine: (data, t_rx), t_rx in time.time()'s
    clock. t_rx is the kernel's receive stamp when the socket delivers a believable one, and
    the time of this read otherwise."""
    data, ancdata, _flags, _addr = sock.recvmsg(bufsize, 64)
    now = time.time()
    for level, ctype, cdata in ancdata:
        if (level == socket.SOL_SOCKET and ctype == SO_TIMESTAMPNS
                and len(cdata) >= _TIMESPEC.size):
            sec, nsec = _TIMESPEC.unpack_from(cdata)
            t_rx = sec + nsec * 1e-9
            if now - RX_STAMP_MAX_AGE_S <= t_rx <= now + 0.001:
                return data, t_rx
    return data, now


class Leader:
    def __init__(self, rig_config: dict, task_config: dict, session: dict,
                 skip_save=None):
        self.rig = rig_config
        self.task = task_config
        self.session = session  # {subject_id, date, session_num, notes}
        # Device names whose per-trial HDF5 datasets to SKIP (user unchecked them at GO). Core
        # trial outcomes (first_lick_t, trial_outcome, timings) are written directly and unaffected.
        self._skip_save = set(skip_save or ())
        # The controller's START message. Newer controllers put camera_requested / camera_saved in
        # it (was video being recorded?); written to metadata.yaml when present. Empty until START.
        self.start_info = {}
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
        self._sync_timeout_s = SYNC_TIMEOUT_MIN_S  # resized per session from the sync cadence
        # Display-fault abort: consecutive stimuli whose onset the photodiode never confirmed,
        # and the reason the session stopped (None = ran to the end / operator STOP).
        self._bad_sync_streak = 0
        self._abort_reason = None
        # displayd/follower stim_onset ack per trial: when the report of the stimulus's first
        # flip reached THIS Pi (leader clock) — the onset anchor used when the photodiode didn't
        # win. Capped at 50 entries.
        self._onset_acks = {}

        # ── L3 heartbeat state (scheduler + correlator, both ride the trial-loop tick) ──
        pd_task = (self.task.get("devices", {}) or {}).get("photodiode", {}) or {}
        pd_rig = (self.rig.get("devices", {}) or {}).get("photodiode", {}) or {}
        self._hb_period_s = float(pd_task.get(
            "heartbeat_period_s",
            pd_rig.get("heartbeat_period_s", HB_PERIOD_DEFAULT_S)) or HB_PERIOD_DEFAULT_S)
        self._hb_enabled = False        # set in run_session (needs photodiode + sync on)
        self._hb_seq = 0
        self._hb_next = 0.0             # next scheduled flash (wall clock)
        self._stim_active = False       # SHOW..stim off: the scheduler stays out of the way
        # awaiting judgement: {seq, t_cmd, t_rx (flip report's arrival here; None = no report
        # yet), t_flip (the follower's own flip time from that report — its clock, never judged)}
        self._heartbeats = deque(maxlen=64)
        self._hb_pulses = deque(maxlen=4096)  # raw photodiode t from the GPIO callback thread
        self._hb_clusters = deque(maxlen=256)  # cluster onset times (first pulse of a flash)
        self._hb_last_pulse_t = None    # clusterer's previous pulse (gap detection)
        self._hb_lost_streak = 0
        self._hb_ok_streak = 0
        self._hb_alarm_on = False
        self._hb_last_alarm_t = 0.0
        # The session's record of the loop (_hb_summary -> metadata.yaml `display_heartbeat`)
        self._hb_n = 0                  # heartbeats judged
        self._hb_n_confirmed = 0
        self._hb_n_no_report = 0        # ... judged from the command time (no flip report came)
        self._hb_light_lat = deque(maxlen=20000)   # flip report's arrival -> first light (s)
        # follower clock - leader clock, one sample per flip report (heartbeats and stimuli)
        self._clock_offsets = deque(maxlen=20000)
        self._clock_warned = False

        # UDP sockets
        net = rig_config["network"]
        self._event_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock.bind(("0.0.0.0", net["command_port"]))
        self._cmd_sock.setblocking(False)
        # Ack socket: the follower's stim_onset acks land here (they used to be sent to
        # event_port, which only the CONTROLLER binds — dead-lettered since day one).
        self._ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ack_sock.bind(("0.0.0.0", net.get("ack_port", 5573)))
        self._ack_sock.setblocking(False)
        # Each ack is timed by the kernel when it arrives (see _recv_stamped): the display
        # heartbeat and the ack onset anchor use that time, on this Pi's clock, and not the
        # follower-clock time written inside the ack.
        self._ack_rx_stamped = _enable_rx_stamp(self._ack_sock)

        # Follower addresses
        self._follower_addrs = {}
        # displayd's pulse-ingest port. The daemon owns the display but has no way to learn
        # whether its own light ever lands on the diode — the L3 correlator runs here, on the
        # leader's clock. Without this the ingest socket sits bound and empty and displayd
        # reports optics "unverified" forever, however many heartbeats it has served.
        self._displayd_pd_addrs = {}
        for pi in rig_config["pis"]:
            if pi["role"] == "follower":
                self._follower_addrs[pi["name"]] = (
                    pi["ip"], net["display_port"])
                self._displayd_pd_addrs[pi["name"]] = (
                    pi["ip"], net.get("displayd_pd_port", 5582))

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
            elif dev_name == "opto":
                task_params = self.task.get("opto") or {}      # the whole opto: section
            elif dev_name == "ir_led":
                # optional task override `devices: ir_led:` (intensity_pct, on_during_session);
                # without it the rig's intensity_pct applies and the LED is on for the session
                task_params = (self.task.get("devices") or {}).get("ir_led") or {}
            else:
                task_params = {}
            dev.init(rig_config=dev_config, task_params=task_params)
            if dev.needs_calibration and "calibration" in dev_config:
                dev.load_calibration(dev_config["calibration"])
            self.devices[dev_name] = dev
            print(f"  {dev.info.label}: OK")
            # Photodiode sync verify — ONLY when the "Photodiode stim sync" checkbox is on for this
            # experiment (stimulus.photodiode_sync_enabled). Default OFF, matching self._sync_enabled
            # below and shared/stim_generator, so a task without the flag doesn't demand flashes the
            # session never draws. If it's on but the check fails, ABORT with an actionable message so
            # the operator can untick it and run without sync. every_n is the SAME cadence the session
            # flashes at (the checkbox's "every N frames"). The burst request/reply waits for the
            # follower display (a reply == display ready).
            stim = self.task.get("stimulus", {})
            if (dev_name == "photodiode" and stim.get("photodiode_sync_enabled", False)
                    and getattr(dev, "verify_enabled", True) and self._follower_addrs):
                every_n = stim.get("photodiode_sync_every_n", dev_config.get("pulse_every_n_frames", 5))
                res = dev.verify_sync(lambda d: self._sync_burst_follower(d, every_n, dev_config))
                if not res.get("ok"):
                    raise RuntimeError(
                        f"Photodiode cannot init: {res.get('reason')}. "
                        f"Untick 'Photodiode stim sync' to run without it.")
                print(f"  photodiode sync verify: OK "
                      f"(detected {res['detected']} vs emitted {res['emitted']})")

    # ── Go / No-Go classification ──

    def _classify_go(self, az_deg: float) -> bool:
        # ONE rule, shared with the controller and the opto assignment (shared/stim_generator).
        return is_go_trial(az_deg, self.task.get("stimulus", {}).get("go_rule", "left"))

    # ── Actions ──

    def _action_show_stim(self):
        self._stim_active = True   # heartbeat scheduler pauses from SHOW until stim off
        opto = self.devices.get("opto")
        if opto is not None:
            # Arm the stimulus edge-lock BEFORE the command goes out: the first photodiode edge
            # at/after since_t (+ SYNC_MIN_LATENCY_S) fires the stim_onset windows on lgpio's
            # notify thread, sub-ms after the first stimulus frame; if no edge comes they fire
            # from the predicted onset instead.
            since_t = time.time()
            opto.arm_edge(since_t, since_t + self._opto_fallback_s(opto))
        self._send_follower({"cmd": "SHOW", "trial": self.trial_num})
        t = time.time()
        self._trial_ctx["stim_onset_t"] = t
        self._publish({"type": "stim", "on": True,
                       "trial": self.trial_num, "t": t,
                       "az": self._trial_ctx.get("stim_az", 0)})

    def _opto_fallback_s(self, opto) -> float:
        """When the stim_onset windows fire if no photodiode edge confirms the onset: with sync
        on, a little past the predicted onset (the edge normally wins, and a miss then costs
        tens of ms, not the whole onset window); with sync off, the predicted onset itself."""
        lat = opto.latency_estimate()
        if self._sync_enabled:
            return min(self._sync_timeout_s, max(lat + 0.02, 0.12))
        return lat

    def _action_deliver_reward(self, manual: bool = False):
        """manual = the controller's REWARD command (outside the trial logic): it never anchors
        an opto reward window — that would light the NEXT trial's window during the ITI."""
        if "reward" in self.devices:
            ms = self.devices["reward"].deliver()
            t = time.time()
            self._trial_ctx["reward_t"] = t
            self._trial_ctx["reward_amount_ul"] = self.devices["reward"].effective_amount_ul
            self._trial_ctx["rewarded"] = True
            self._publish({"type": "reward", "on": True,
                           "t": t, "pulse_ms": ms})
            if not manual and "opto" in self.devices:
                self.devices["opto"].fire_at("reward", t)

    # ── Lick handling ──

    def _on_lick(self, event: dict):
        self._lick_queue.put(event["t"])
        self._publish({"type": "lick", "t": event["t"]})

    def _on_sync_pulse(self, evt: dict):
        """Photodiode rising edge: queue for online onset detection, publish to Mac."""
        if evt.get("event") == "sync_pulse":
            opto = self.devices.get("opto")
            if opto is not None:
                opto.on_sync_edge(evt["t"])   # FIRST: the laser's ON edge rides this thread
            try:
                self._sync_queue.put_nowait(evt["t"])
            except queue.Full:
                pass
            # Second, independent tap for the heartbeat correlator: _sync_queue is drained
            # (and refilled) per trial by the stim-onset path, so it can never also serve as
            # the between-trials pulse record. deque.append is atomic — this runs on the
            # lgpio callback thread, the correlator drains it on the trial-loop thread.
            if self._hb_enabled:
                self._hb_pulses.append(evt["t"])
        # Publish with a `type` key: the device dict carries `event`, but the browser dispatches on
        # switch(evt.type), so without this the live SYNC raster (case 'sync_pulse') never populates.
        self._publish({**evt, "type": evt.get("event", "sync_pulse")})

    def _wait_stim_pulse(self, since_t: float, timeout_s: float):
        """First photodiode pulse of THIS stimulus: the first queued edge at/after the SHOW
        send time, waited for up to timeout_s. An earlier edge cannot be this stim's onset —
        the display can't flash before it is told to — so it is discarded rather than
        mistaken for one. That matters now that heartbeat flashes also pulse the photodiode
        between trials: the trial-setup drain can't catch a flash whose pulse is still in
        flight, and one stale edge would anchor the whole trial timeline seconds early.
        Returns None on timeout (wire off / no pulse), exactly as the queue get did."""
        deadline = time.time() + timeout_s
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                t = self._sync_queue.get(timeout=remaining)
            except queue.Empty:
                return None
            # ... and an edge too soon after SHOW is a heartbeat sub-pulse, not the stimulus.
            if t >= since_t + SYNC_MIN_LATENCY_S:
                return t

    def _compute_sync_timeout(self) -> float:
        """Per-trial onset window sized to the sync-square cadence (see SYNC_* above). every_n
        comes from the task (falling back to the device's pulse_every_n_frames — the same
        precedence the INIT verify burst uses), refresh from the follower's display config.
        Clamped, so a wrong refresh_hz in the rig yaml degrades the window rather than
        breaking the trial clock."""
        stim = self.task.get("stimulus", {})
        devs = (self.rig or {}).get("devices", {})
        pd_cfg = devs.get("photodiode", {}) or {}
        every_n = int(stim.get("photodiode_sync_every_n",
                               pd_cfg.get("pulse_every_n_frames", 5)) or 1)
        refresh = float((devs.get("display", {}) or {}).get("refresh_hz", 60) or 60)
        if refresh <= 0:
            refresh = 60.0
        want = SYNC_FLIP_BUDGET_S + (every_n / refresh) + SYNC_TIMEOUT_MARGIN_S
        timeout = min(SYNC_TIMEOUT_MAX_S, max(SYNC_TIMEOUT_MIN_S, want))
        if want > SYNC_TIMEOUT_MAX_S:
            # every_n is too coarse to anchor onsets at this refresh: the clamp will drop
            # trials to the ack fallback. Say so instead of quietly under-waiting.
            print(f"[sync] WARNING: every_n={every_n} at {refresh:g} Hz needs "
                  f"{want * 1000:.0f} ms but the window is capped at "
                  f"{SYNC_TIMEOUT_MAX_S * 1000:.0f} ms — expect sync_ok=0 on many trials; "
                  f"lower photodiode_sync_every_n", flush=True)
        return timeout

    def _on_opto_event(self, evt: dict):
        """Opto window on/off from the device's threads -> the controller (raster + table)."""
        self._publish({**evt, "type": "opto"})

    def _on_ir_led_event(self, evt: dict):
        """IR illuminator switched on / off -> the controller (a line in the session log)."""
        self._publish({**evt, "type": "ir_led"})

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

        # Stimuli: Deploy's plan (generated here only if nothing was deployed)
        self._stims = self._generate_stims()
        refuse = None
        if self._stims is None:
            refuse = "no stimulus plan (Deploy's stimuli.npz missing and generation failed — see above)"
        else:
            sess_cfg = self.task.get("session", {})
            bs = sess_cfg.get("block_size", 25)
            want = sess_cfg.get("n_blocks", sess_cfg.get("n_trials", 150) // max(bs, 1)) * bs
            have = int(self._stims["n_trials"][0])
            if have != want:
                refuse = (f"stimulus plan has {have} trials but the task asks for {want} — "
                          f"re-Deploy after changing the task")
            elif (self.task.get("opto") or {}).get("enabled") and "opto_on" not in self._stims:
                refuse = "the stimulus plan predates the task's opto section — re-Deploy"
        end_reason = "no_stimuli"
        if not refuse and (self.task.get("opto") or {}).get("enabled") and "opto" not in self.devices:
            # Deploy refuses this too; here for a hand-edited task or a rig changed since.
            refuse = ("the task enables opto but this rig has no opto device on its leader "
                      "(Setup tab) — add it, or untick Opto in the Experiment tab")
            end_reason = "no_opto_device"
        if refuse:
            # Never run on the hard-coded fallbacks (8 s ITI, az 0, contrast 0 in the data): end
            # the session at once, visibly, before any trial or data file is started.
            print(f"Session REFUSED — {refuse}", flush=True)
            self.running = False
            self._publish({"type": "session_end", "n_completed": 0, "n_planned": 0,
                           "t": time.time(), "end_reason": end_reason, "error": refuse})
            return

        # Start lick streaming
        if "lick_sensor" in self.devices:
            self.devices["lick_sensor"].start_stream(self._on_lick)

        # Start photodiode streaming
        if "photodiode" in self.devices:
            self.devices["photodiode"].start_stream(self._on_sync_pulse)

        # Start running-wheel encoder streaming
        if "encoder" in self.devices:
            self.devices["encoder"].start_stream(self._on_encoder)

        # Opto events (laser / mask on and off) to the controller's live view
        if "opto" in self.devices:
            self.devices["opto"].start_stream(self._on_opto_event)

        # IR illuminator: on from here to the session's end (task devices.ir_led.on_during_session,
        # default true). It only lights the camera, so a board that fails at THIS point must not
        # cost the session: say so loudly (console + controller + Slack) and run on.
        if "ir_led" in self.devices:
            try:
                self.devices["ir_led"].start_stream(self._on_ir_led_event)
            except Exception as e:
                print(f"WARNING: IR LED did not switch on ({e}) — the session runs on, "
                      f"its video will be dark", flush=True)
                self._publish({"type": "ir_led", "on": False, "t": time.time(), "error": str(e)})
        self._sync_enabled = (
            "photodiode" in self.devices
            and self.task.get("stimulus", {}).get("photodiode_sync_enabled", False))
        if self._sync_enabled:
            self._sync_timeout_s = self._compute_sync_timeout()
            print("Photodiode stim-sync: ON (online onset correction, "
                  f"onset window {self._sync_timeout_s * 1000:.0f} ms)")

        # ── L3 display heartbeat ── The same photodiode + sync-square loop that anchors stim
        # onsets, run on the leader's clock between stim windows so a dark projector is caught
        # in seconds instead of at the next trial (or at post-hoc analysis).
        self._hb_enabled = self._sync_enabled and bool(self._follower_addrs)
        if self._hb_enabled:
            self._hb_next = time.time() + self._hb_period_s
            print(f"Display heartbeat: ON (every {self._hb_period_s:g}s between stims; flip "
                  f"reports timed on this Pi's clock, "
                  f"{'kernel receive stamp' if self._ack_rx_stamped else 'at the read'})")
        elif not self._sync_enabled:
            # The named degraded mode, never silence: with sync off nothing watches the
            # optics this session, and sync_ok is recorded as -1 (unavailable, not failed).
            self._publish_display_health(
                "optics_watch_off",
                "photodiode stim sync is off — display optics are unmonitored this session "
                "and per-trial sync_ok is unavailable (-1)")
            print("Display heartbeat: OFF (photodiode sync disabled)")
        else:
            # Sync is on (per-trial onsets still verified), but nothing to flash between
            # trials — not a degraded optics watch, so no alarm.
            print("Display heartbeat: OFF (no follower display in this rig)")

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
        # Display-fault abort: a stimulus nobody can prove was shown is not data, and the whole
        # point of catching a dark projector in 15 s is wasted if the session then runs for an
        # hour anyway. Requires photodiode sync (nothing watches the optics without it).
        abort_on_display = bool(sess_cfg.get("abort_on_display_fault", True))
        abort_bad_n = max(1, int(sess_cfg.get("abort_after_bad_trials", 3) or 3))
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

            # ── Opto: arm BEFORE the ITI (iti_start is the first anchor). The plan says which
            # trials are lit (stimuli.npz opto_on, assigned at Deploy); the device fires the
            # mask on every trial and the laser on those.
            opto = self.devices.get("opto")
            if opto is not None:
                opto_on = (bool(int(self._stims["opto_on"][trial_num]))
                           if (self._stims is not None and "opto_on" in self._stims) else False)
                opto.arm_trial(trial_num, opto_on)

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
            if opto is not None:
                opto.fire_at("iti_start", iti_start_t)

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
            if opto is not None:
                opto.fire_at("trial_start", trial_start_t)

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

            # ── Pre-SHOW guards ── both wait through _wait_for_event, so UDP / heartbeat ticks
            # continue and their licks still count as pre-stim below.
            # 1. Heartbeat settle: a flash commanded in the last HB_SETTLE_S lands its sub-pulses
            #    AFTER the SHOW send and used to be taken for the stimulus onset (1.2 % of
            #    confirmed trials, 2026-11). Hold the stimulus until it has landed.
            self._stim_active = True   # from here to stim off the scheduler stays out
            if self._hb_enabled and self._heartbeats:
                settle = self._heartbeats[-1]["t_cmd"] + HB_SETTLE_S - time.time()
                if settle > 0:
                    self._wait_for_event(settle)
            # 2. Opto lead: a stim_onset window with a negative onset starts BEFORE the stimulus,
            #    timed from the predicted onset (SHOW + the latency estimate); when the lead
            #    exceeds the latency, SHOW itself is held by the difference.
            if opto is not None:
                lead = opto.lead_s()
                if lead > 0:
                    lat = opto.latency_estimate()
                    t_show_planned = time.time() + max(0.0, lead - lat)
                    opto.fire_stim_lead(t_show_planned + lat)
                    self._wait_for_event(max(0.0, t_show_planned - time.time()))

            # Licks so far this trial all belong to the pre-stim phase (list was reset
            # at trial setup; pre-stim is the only phase before the stimulus).
            n_licks_prestim = len(self._trial_licks)

            # ── Stimulus ON ──
            self._action_show_stim()
            stim_onset_t = self._trial_ctx["stim_onset_t"]

            # ── Online photodiode sync: anchor timeline to true display onset ──
            # Best available anchor, in order: photodiode pulse > displayd/follower
            # stim_onset ack (the first flip, timed by the ack's arrival here) > the SHOW send
            # time; all three are this Pi's clock. sync_ok is tri-state:
            # 1 = pulse seen, 0 = sync on but no pulse (a real failure), -1 = sync disabled
            # (unavailable — which the old flat 0 made indistinguishable from failure).
            onset_source = "command"
            if self._sync_enabled:
                pulse_t = self._wait_stim_pulse(stim_onset_t, self._sync_timeout_s)
                if pulse_t is not None:
                    true_onset_t = pulse_t
                    self._trial_ctx["sync_ok"] = 1
                    onset_source = "photodiode"
                else:
                    true_onset_t = stim_onset_t  # wire off / no pulse: fall back
                    self._trial_ctx["sync_ok"] = 0
            else:
                true_onset_t = stim_onset_t
                self._trial_ctx["sync_ok"] = -1
            if onset_source == "command":
                ack_t = self._await_onset_ack(
                    trial_num, self._sync_timeout_s - (time.time() - stim_onset_t))
                if ack_t is not None:
                    true_onset_t = ack_t
                    onset_source = "ack"
            self._trial_ctx["onset_source"] = onset_source
            self._trial_ctx["true_onset_t"] = true_onset_t
            self._trial_ctx["display_latency_s"] = true_onset_t - stim_onset_t
            if opto is not None and onset_source == "photodiode":
                opto.note_true_onset(stim_onset_t, true_onset_t)   # feeds the lead estimate

            # Response-delay phase (default 0.6s) — anchored to TRUE onset so the response window
            # opens resp_delay_s after the mouse actually sees the stim. No reward here: every
            # reward is delivered relative to the response-window start (below).
            self._wait_for_event(max(0.0, reward_delay - (time.time() - true_onset_t)))

            # ── Response window ──
            rw_start_t = time.time()
            self._trial_ctx["response_window_t"] = rw_start_t
            if opto is not None:
                opto.fire_at("response_window", rw_start_t)
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
            # The engine is done with the stim window, but the follower keeps drawing until
            # stim_off_t — hold the next heartbeat past that, so a flash never repaints the
            # sync square over a stimulus still on the glass.
            self._stim_active = False
            self._hb_next = max(self._hb_next, stim_off_t + 0.05)
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

            # ── Command-path latency ── displayd acks the renderer's first flip of this
            # stimulus on the ack port, and the ack's arrival is timed on this Pi's clock
            # (_note_onset_ack): SHOW -> flip (+ the ack's ~1 ms way back), independent of the
            # follower's clock. Read it HERE, at trial end: the ack lands asynchronously and may
            # still be in flight at the onset decision.
            # Decomposes display_latency_s: SHOW -> flip is this, flip -> photons is the rest.
            _ack_t = self._onset_acks.get(trial_num)
            self._trial_ctx["onset_ack_latency_s"] = (
                (_ack_t - stim_onset_t) if _ack_t is not None else float("nan"))

            # ── Record trial ──
            if opto is not None:
                self._trial_ctx.update(opto.finish_trial())   # what fired, and when
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

            # ── Display-fault abort ── Decided HERE, at the end of the trial, and never mid-cue:
            # the stimulus and its post-stim window are already over, so stopping costs nothing
            # and the animal is never cut off mid-presentation. Two independent triggers, both
            # requiring evidence AT THE CUE that the loop is broken:
            #   * abort_bad_n consecutive stimuli whose onset the photodiode never confirmed, or
            #   * the L3 heartbeat alarm standing (>=3 missed heartbeats, i.e. ~15 s dark)
            #     together with at least one unconfirmed stimulus.
            # The heartbeat alarm alone is deliberately NOT enough: if this trial's stimulus WAS
            # optically confirmed then light is reaching the diode right now, whatever the
            # between-trials flashes did, and killing a working session on a stale alarm is the
            # worse error.
            if abort_on_display and self._sync_enabled:
                ab = self._check_display_fault(trial_num, abort_bad_n)
                if ab:
                    self._abort_reason = ab
                    self._publish({"type": "display_abort", "trial": ab["trial"],
                                   "cause": ab["cause"], "reason": ab["reason"],
                                   "n_bad": ab["n_bad"], "t": time.time()})
                    print(f"Display fault: {ab['reason']} — aborting session at "
                          f"trial {trial_num}.", flush=True)
                    self.running = False
                    break

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

        # Why the session ended, recorded rather than inferred: a run cut short by a dark
        # projector must not read as a clean early finish in the metadata months later.
        end_reason = "display_fault" if self._abort_reason else "completed"
        end_ev = {"type": "session_end", "n_completed": n_completed,
                  "n_planned": n_planned, "t": time.time(), "end_reason": end_reason}
        if self._abort_reason:
            end_ev["abort"] = self._abort_reason
        self._publish(end_ev)
        if self._abort_reason:
            print(f"Session ABORTED — display fault ({self._abort_reason['cause']}) at trial "
                  f"{self._abort_reason['trial']} after {n_completed}/{n_planned} trials. "
                  f"Data: {data_dir}")
        else:
            print(f"Session complete ({n_completed}/{n_planned} trials). Data: {data_dir}")
        hb_summary = self._hb_summary()
        if hb_summary:
            print(f"[heartbeat] session: {self._hb_summary_text(hb_summary)}", flush=True)

        # Save metadata. The camera records through pi_api, not the engine, so only the
        # controller's START message says whether this session saved video: camera_requested
        # (Camera checked at GO) and camera_saved (the camera Pi confirmed recording started).
        # Both are written only when present — absent means unknown (an older controller).
        camera_not_saved = "camera" in self.devices and self.start_info.get("camera_saved") is False
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
            "saved_devices": sorted(n for n in self.devices
                                    if n not in self._skip_save and not (n == "camera" and camera_not_saved)),
            "skipped_devices": sorted(set(self._skip_save) | ({"camera"} if camera_not_saved else set())),
            "end_reason": end_reason,
        }
        for k in ("camera_requested", "camera_saved"):
            if isinstance(self.start_info.get(k), bool):
                meta[k] = self.start_info[k]
        if self._abort_reason:
            meta["abort"] = self._abort_reason
        if hb_summary:
            meta["display_heartbeat"] = hb_summary
        meta_path = data_dir / "metadata.yaml"
        with open(meta_path, "w") as f:
            yaml.dump(meta, f, default_flow_style=False)

    # ── L3 display heartbeat (leader-clocked scheduler + correlator) ──

    def _hb_tick(self):
        """One trial-loop tick of the display heartbeat: flash if one is due, then judge the
        flashes whose confirm window has closed. Cheap by design — the trial loop calls this
        at up to 1 kHz through _check_udp, so the heartbeat needs no thread and no clock of
        its own (one clock, the leader's, times the command, the flip report's arrival and
        the pulse)."""
        if not self._hb_enabled:
            return
        now = time.time()
        # Never flash into a stim window: the stimulus draws its own sync square, and the
        # per-trial sync path already doubles as the in-stim heartbeat.
        if now >= self._hb_next and not self._stim_active:
            self._hb_seq += 1
            self._send_follower({"cmd": "HB_FLASH", "seq": self._hb_seq})
            self._heartbeats.append({"seq": self._hb_seq, "t_cmd": now,
                                     "t_rx": None, "t_flip": None})
            self._hb_next = now + self._hb_period_s
        self._hb_correlate(now)

    def _hb_correlate(self, now: float):
        """Fold new photodiode pulses into flash clusters (one flash = 2-3 sub-pulses ~8.7 ms
        apart; a gap > HB_CLUSTER_GAP_S starts a new cluster, whose onset is its first pulse)
        and confirm every heartbeat old enough to judge."""
        while self._hb_pulses:
            try:
                t = self._hb_pulses.popleft()
            except IndexError:
                break
            if (self._hb_last_pulse_t is None
                    or (t - self._hb_last_pulse_t) > HB_CLUSTER_GAP_S):
                self._hb_clusters.append(t)
            self._hb_last_pulse_t = t
        # Cluster onsets this old can no longer match a pending heartbeat (heartbeats are
        # judged HB_EVAL_AGE_S after t_cmd) — drop them so the scan below stays short.
        while self._hb_clusters and self._hb_clusters[0] < now - HB_CLUSTER_KEEP_S:
            self._hb_clusters.popleft()

        while self._heartbeats:
            hb = self._heartbeats[0]
            # Judged once its window has closed: HB_EVAL_AGE_S after the command and, when the
            # flip itself came late (a busy renderer), not before the window around its report
            # is over — the light of a flip 170 ms after the command lands after HB_EVAL_AGE_S.
            if (now - hb["t_cmd"]) <= HB_EVAL_AGE_S or (
                    hb["t_rx"] is not None and now <= hb["t_rx"] + HB_WIN_POST_S):
                break
            self._heartbeats.popleft()
            # The reference is when the flip report reached this Pi: the leader's clock, like
            # the pulse times. The follower's own flip time (hb["t_flip"]) is not used here.
            t_ref = hb["t_rx"]
            if t_ref is None:
                t_ref = hb["t_cmd"] + HB_FLIP_FALLBACK_S   # no report from displayd
                self._hb_n_no_report += 1
            hb["t_light"] = next((c for c in self._hb_clusters
                                  if (t_ref - HB_WIN_PRE_S) <= c <= (t_ref + HB_WIN_POST_S)),
                                 None)
            confirmed = hb["t_light"] is not None
            self._hb_n += 1
            if confirmed:
                self._hb_n_confirmed += 1
                if hb["t_rx"] is not None:
                    self._hb_light_lat.append(hb["t_light"] - hb["t_rx"])
            self._hb_judge(hb, confirmed, now)

    def _hb_judge(self, hb: dict, confirmed: bool, now: float):
        """Streak bookkeeping: HB_LOST_N consecutive misses raise the alarm (edge-triggered,
        re-alerted every HB_REALERT_S while it persists), HB_OK_N consecutive hits clear it."""
        self._report_hb_verdict(hb.get("seq"), confirmed)
        if confirmed:
            self._hb_lost_streak = 0
            self._hb_ok_streak += 1
            if self._hb_alarm_on and self._hb_ok_streak >= HB_OK_N:
                self._hb_alarm_on = False
                self._hb_ok_streak = 0
                self._publish_display_health(
                    "heartbeat_ok",
                    "display heartbeat confirmed again (%d in a row)" % HB_OK_N,
                    level="event", seq=hb["seq"])
                print(f"[heartbeat] recovered at seq {hb['seq']}", flush=True)
            return

        self._hb_ok_streak = 0
        self._hb_lost_streak += 1
        if self._hb_lost_streak < HB_LOST_N:
            return
        if self._hb_alarm_on and (now - self._hb_last_alarm_t) < HB_REALERT_S:
            return
        self._hb_alarm_on = True
        self._hb_last_alarm_t = now
        cause, detail = self._hb_diagnose()
        msg = ("no photodiode pulse for %d consecutive display heartbeats (%gs apart) — %s"
               % (self._hb_lost_streak, self._hb_period_s, detail))
        self._publish_display_health("heartbeat_lost", msg, seq=hb["seq"], cause=cause,
                                     n_missed=self._hb_lost_streak)
        print(f"[heartbeat] ALARM: {msg}", flush=True)

    def _check_display_fault(self, trial_num, abort_bad_n):
        """Fold this trial's onset result into the unconfirmed-stimulus streak and decide
        whether the display loop is broken enough to end the session. Returns the abort record
        (also stored as _abort_reason by the caller) or None to keep running. Call ONCE per
        trial, after the cue is over — it mutates the streak."""
        if int(self._trial_ctx.get("sync_ok", -1)) == 0:
            self._bad_sync_streak += 1
        else:
            self._bad_sync_streak = 0
        if not (self._bad_sync_streak >= abort_bad_n
                or (self._hb_alarm_on and self._bad_sync_streak > 0)):
            return None
        cause, detail = self._hb_diagnose()
        trigger = ("display heartbeat lost and stimulus unconfirmed" if self._hb_alarm_on
                   else "%d consecutive stimuli unconfirmed" % self._bad_sync_streak)
        return {"cause": cause, "trigger": trigger, "detail": detail,
                "reason": "%s — %s" % (trigger, detail),
                "trial": int(trial_num), "n_bad": int(self._bad_sync_streak)}

    def _hb_diagnose(self):
        """Localize a lost heartbeat using the photodiode's own analog telemetry, which is
        independent of pulse detection (see HB_TELEMETRY_STALE_S). Returns (cause, detail):
        a machine-readable cause for the notifier and one human sentence naming the suspect
        instead of the whole chain. `no_light` deliberately does NOT claim the projector is
        dark — a covered or knocked-out-of-alignment diode is equally dark from here, and only
        L4 (camera arbiter) can tell those apart; say what is known, not what is guessed."""
        dev = self.devices.get("photodiode")
        if dev is None or not hasattr(dev, "get_v_status"):
            return "unknown", "no photodiode telemetry — check projector, optics and wiring"
        try:
            v = dev.get_v_status()
        except Exception:  # noqa: BLE001 — an alarm must never be lost to a telemetry error
            return "unknown", "photodiode telemetry unreadable — check projector and wiring"
        if not v.get("available"):
            return ("no_telemetry",
                    "Teensy serial not open — cannot tell a dark display from a dead sensor; "
                    "check projector, optics and wiring")
        base = v.get("last_baseline")
        if not base or (time.time() - base.get("t", 0)) > HB_TELEMETRY_STALE_S:
            return ("sensor_dead",
                    "Teensy serial has gone silent — SENSOR CHAIN fault (Teensy or USB), "
                    "the display itself may be fine")
        ceil_v = base.get("ceil_V")
        v_low = v.get("v_low") or 0.3
        if ceil_v is not None and ceil_v >= v_low:
            return ("ttl_dead",
                    "photodiode sees light (ceil %.3f V) but no TTL pulses arrive — DIGITAL leg "
                    "fault (Teensy OUT pin 16 -> GPIO16), the display is showing" % ceil_v)
        return ("no_light",
                "photodiode is alive and sees darkness (ceil %.3f V) — NO LIGHT reaching the "
                "diode: projector dark, or the diode is covered/misaimed"
                % (ceil_v if ceil_v is not None else float("nan")))

    def _publish_display_health(self, kind: str, msg: str, level: str = "alarm", **extra):
        """Publish in the SAME envelope displayd sends on the ack port, so the controller's
        display_health path treats leader-side and daemon-side alarms identically."""
        alarm = {"t": time.time(), "level": level, "kind": kind, "msg": msg}
        alarm.update(extra)
        self._publish({"type": "display_health", "source": "leader",
                       "alarm": alarm, "t": alarm["t"]})

    def _note_hb_flip(self, msg: dict, t_rx: float):
        """displayd's {"type":"hb_flash","seq":k,"t_flip":T}: one scheduled heartbeat has hit
        the glass. t_rx — when that report reached this Pi, on this Pi's clock — is what the
        correlator judges against. T is the follower's clock: kept for the clock-offset
        estimate only. Newest-first scan; a duplicate report keeps the first arrival."""
        seq = msg.get("seq")
        if seq is None:
            return
        for hb in reversed(self._heartbeats):
            if hb["seq"] == seq:
                if hb["t_rx"] is None:
                    hb["t_rx"] = t_rx
                    hb["t_flip"] = self._note_clock_offset(msg.get("t_flip"), t_rx)
                return

    def _note_onset_ack(self, msg: dict, t_rx: float):
        """displayd's {"type":"stim_onset","trial":N,"t":T}: the first flip of a stimulus.
        Remember when the ack reached this Pi (t_rx, this Pi's clock) as the trial's flip time
        — the onset anchor used when the photodiode didn't win, and the SHOW -> flip latency.
        T is the follower's clock: clock-offset estimate only. A duplicate ack keeps the first
        arrival. Bounded at 50: dicts keep insertion order, so the pop drops the oldest trial."""
        try:
            trial = int(msg.get("trial"))
        except (TypeError, ValueError):
            return
        if trial in self._onset_acks:
            return
        self._onset_acks[trial] = t_rx
        self._note_clock_offset(msg.get("t"), t_rx)
        while len(self._onset_acks) > 50:
            self._onset_acks.pop(next(iter(self._onset_acks)))

    def _note_clock_offset(self, t_follower, t_rx: float):
        """One sample of (follower clock - leader clock): the flip time displayd wrote into a
        report minus the time the report arrived here. It reads low by the report's way back
        (renderer -> displayd -> LAN, about a millisecond). Nothing in the engine depends on
        the offset; it is measured so that two Pis whose clocks have drifted apart are named
        as that, once a session, instead of surfacing as a display alarm (as before
        2026-11-20) or in follower-side times months later. Returns the follower time as a
        float, None if the report carried none."""
        try:
            t_follower = float(t_follower)
        except (TypeError, ValueError):
            return None
        self._clock_offsets.append(t_follower - t_rx)
        if not self._clock_warned and len(self._clock_offsets) >= HB_CLOCK_WARN_N:
            last = [self._clock_offsets[i] for i in range(-HB_CLOCK_WARN_N, 0)]
            med = float(np.median(last))
            if abs(med) > HB_CLOCK_WARN_S:
                self._clock_warned = True
                msg = (f"the follower's clock is {abs(med) * 1000:.0f} ms "
                       f"{'behind' if med < 0 else 'ahead of'} the leader's. The display "
                       f"heartbeat and the stimulus onsets are timed on the leader's clock and "
                       f"are not affected; follower-side times (displayd's log) are off by "
                       f"that much. Check chrony on both Pis (`chronyc tracking`)")
                print(f"[clock] {msg}", flush=True)
                self._publish_display_health("clock_offset", msg, level="warn",
                                             offset_s=round(med, 6))
        return t_follower

    def _hb_summary(self):
        """What this session learned about the display loop, for metadata.yaml (the .h5 root
        attribute `display_heartbeat`): heartbeats judged / confirmed / judged without a flip
        report, the delay from a flip report's arrival to the first light, and the follower's
        clock against this Pi's. None when there is nothing to say (no heartbeat, no ack)."""
        if not self._hb_n and not self._clock_offsets:
            return None

        def _ms(vals):
            a = np.asarray(vals, dtype=float) * 1000.0
            return {"median": round(float(np.median(a)), 2), "min": round(float(a.min()), 2),
                    "max": round(float(a.max()), 2), "n": int(a.size)}

        out = {"n": int(self._hb_n), "n_confirmed": int(self._hb_n_confirmed),
               "n_no_report": int(self._hb_n_no_report),
               "rx_stamp": "kernel" if self._ack_rx_stamped else "read"}
        if self._hb_light_lat:
            out["report_to_light_ms"] = _ms(self._hb_light_lat)
        if self._clock_offsets:
            out["follower_clock_offset_ms"] = _ms(self._clock_offsets)
        return out

    @staticmethod
    def _hb_summary_text(s: dict) -> str:
        parts = [f"{s['n_confirmed']}/{s['n']} heartbeats confirmed"]
        if s.get("n_no_report"):
            parts.append(f"{s['n_no_report']} judged without a flip report")
        lat = s.get("report_to_light_ms")
        if lat:
            parts.append(f"flip report -> light {lat['median']:.1f} ms "
                         f"({lat['min']:.1f} to {lat['max']:.1f})")
        off = s.get("follower_clock_offset_ms")
        if off:
            parts.append(f"follower clock {off['median']:+.1f} ms against this Pi "
                         f"({off['min']:+.1f} to {off['max']:+.1f}, {off['n']} reports)")
        return "; ".join(parts)

    def _await_onset_ack(self, trial: int, budget_s: float):
        """Best-effort: this trial's stim_onset ack, within whatever is LEFT of the per-trial
        onset budget (_sync_timeout_s from SHOW). Costs nothing when the photodiode wait
        already spent it, so the pre-onset wait never grows beyond today's worst case.
        Returns when the ack arrived (this Pi's clock, see _note_onset_ack), or None."""
        if not self._follower_addrs:
            return None
        deadline = time.time() + max(0.0, budget_s)
        while True:
            self._check_udp()
            t = self._onset_acks.get(trial)
            if t is not None:
                return t
            if time.time() >= deadline or not self.running:
                return None
            time.sleep(0.001)

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
                self._action_deliver_reward(manual=True)
        except BlockingIOError:
            pass
        except json.JSONDecodeError:
            pass
        # Drain follower acks. _check_udp runs at up to 1 kHz inside the trial loop, so the
        # empty case must be one cheap select() — not a raised-and-caught BlockingIOError.
        while select.select([self._ack_sock], [], [], 0)[0]:
            try:
                data, t_rx = _recv_stamped(self._ack_sock)
                msg = json.loads(data)
            except (OSError, ValueError):   # ValueError covers JSON + non-UTF-8 datagrams
                continue
            mtype = msg.get("type")
            if mtype == "stim_onset":
                # Republish to the controller (its stim_onset handler was dormant until the
                # ack channel became real), and keep its arrival as this trial's onset anchor.
                self._note_onset_ack(msg, t_rx)
                self._publish({"type": "stim_onset", "trial": msg.get("trial"),
                               "t": msg.get("t")})
            elif mtype == "hb_flash":
                # displayd reporting that a scheduled heartbeat has hit the glass.
                self._note_hb_flip(msg, t_rx)
            elif mtype == "display_health":
                # displayd's own alarms (DLPC, renderer, optics) — forward verbatim; the
                # controller's event path is what surfaces and notifies them.
                self._publish(msg)

        # Every trial-loop wait ticks through here at ms rate, so the display heartbeat
        # (scheduler + correlator) rides this call instead of running a thread of its own.
        self._hb_tick()

    # ── Communication ──

    def _send_follower(self, msg: dict):
        """UDP to all followers."""
        data = json.dumps(msg).encode()
        for addr in self._follower_addrs.values():
            self._event_sock.sendto(data, addr)

    def _report_hb_verdict(self, seq, confirmed: bool):
        """Tell displayd how one of its heartbeat flashes actually landed, so the daemon can
        report optics honestly instead of a permanent 'unverified'. Fire-and-forget: this is
        telemetry, and the leader's own alarm is the authority either way."""
        if not self._displayd_pd_addrs:
            return
        data = json.dumps({"type": "hb_verdict", "seq": seq,
                           "confirmed": bool(confirmed), "t": time.time()}).encode()
        for addr in self._displayd_pd_addrs.values():
            try:
                self._event_sock.sendto(data, addr)
            except OSError:
                pass

    def _sync_burst_follower(self, duration_s, every_n, dev_config):
        """Photodiode-verify flash trigger (experiment path): ask the follower to flash the red sync
        square for `duration_s` and return the emitted flash count. Request/reply on a short-lived
        socket; retries until the follower answers (a reply means displayd's renderer is up),
        so the verify waits for display init. Raises if the display never comes up within the wait."""
        body = {"cmd": "SYNC_TEST", "every_n": int(every_n), "duration_s": float(duration_s)}
        for k in ("sync_corner", "sync_size_px", "sync_brightness"):
            if k in dev_config:
                body[k] = dev_config[k]
        addr = next(iter(self._follower_addrs.values()))
        data = json.dumps(body).encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(float(duration_s) + 3.0)      # long enough for one burst to run + reply
            deadline = time.monotonic() + 20.0         # wait up to ~20 s for the follower display
            while time.monotonic() < deadline:
                s.sendto(data, addr)
                try:
                    reply, _ = s.recvfrom(4096)
                except socket.timeout:
                    continue                           # follower not listening yet -> retry
                r = json.loads(reply.decode())
                if r.get("cmd") == "SYNC_TEST_DONE":
                    if r.get("error"):
                        raise RuntimeError(f"follower display flash failed: {r['error']}")
                    return int(r.get("flashes", 0))
            raise RuntimeError("display not ready (no SYNC_TEST reply from follower)")
        finally:
            s.close()

    def _publish(self, event: dict):
        """Send event to the controller via UDP. Every event carries this rig's name: the
        controller sorts datagrams by sender IP first and by this field when the IP is
        ambiguous (mock Pis on one machine, or a Pi whose IP was edited while loaded)."""
        event.setdefault("rig", (self.rig or {}).get("name", ""))
        if self._mac_addr:
            self._event_sock.sendto(
                json.dumps(event).encode(), self._mac_addr)

    def _publish_trial_event(self, stim: dict):
        ctx = self._trial_ctx
        _ack_lat = float(ctx.get("onset_ack_latency_s", float("nan")))
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
        # Optogenetics: laser onset relative to the true stimulus onset (ms); null when no laser
        # window fired or no onset is known (never NaN — see onset_ack_latency_s below).
        _oo = float(ctx.get("opto_onset_t", float("nan")))
        _to = float(ctx.get("true_onset_t", float("nan")))
        opto_rel_ms = round((_oo - _to) * 1000, 1) if (_oo == _oo and _to == _to) else None

        self._publish({
            "type": "trial",
            "trial_num": self.trial_num,
            "stim_az": float(stim.get("stim_az_deg", 0)),
            "contrast": float(stim.get("contrast", 0)),
            "outcome": "hit" if ctx.get("responded") else "miss",
            "reward_type": "operant" if is_operant else "pavlovian",
            "rt_ms": rt_ms,
            "resp_licks": ctx.get("n_window_licks", 0),   # licks within the response window
            # Onset provenance for the live table: tri-state sync_ok (1/0/-1) + which anchor
            # true_onset_t actually came from.
            "sync_ok": int(ctx.get("sync_ok", -1)),
            # JSON null, never NaN: json.dumps emits a bare NaN, which the browser's
            # JSON.parse REJECTS — one missing ack would kill the live event stream.
            # (The HDF5 column keeps NaN; that is the right sentinel for a float dataset.)
            "onset_ack_latency_s": (None if _ack_lat != _ack_lat else round(_ack_lat, 6)),
            "onset_source": ctx.get("onset_source", "command"),
            "level": level,
            "adaptive_state": adaptive_state,
            "opto": bool(ctx.get("opto_on", 0)),
            "opto_rel_ms": opto_rel_ms,
            "t": time.time(),
        })

    # ── Stim generation ──

    def _generate_stims(self):
        """Load the stimuli Deploy generated for this session, or generate them if there are none.

        Deploy writes <data>/<subj>/<subj_date>/<session_id>/stimuli.npz on this Pi, and the SAME
        file goes to the follower, which draws trial N from it. Generation is randomized (ITIs,
        within-block order), so regenerating here would make this engine run a different plan from
        the one the follower draws and the UI shows. Until 2026-10-03 it always regenerated —
        the ITIs it ran never matched Deploy's — so loading the deployed file is the rule and
        regeneration only a fallback for a session nobody deployed."""
        try:
            from shared.stim_generator import generate_stimuli
            subj = self.session["subject_id"]
            subj_date = f"{subj}_{self.session['date']}"
            stim_dir = Path(self.rig["data"]["leader_dir"]).expanduser() / subj / subj_date / self.session_id
            # pi_api's /api/generate_stims writes under ~/data; the rig's leader_dir is normally
            # the same folder, but look in both.
            for cand in (stim_dir / "stimuli.npz",
                         Path.home() / "data" / subj / subj_date / self.session_id / "stimuli.npz"):
                if cand.exists():
                    with np.load(str(cand)) as z:
                        stims = {k: z[k] for k in z.files}
                    cal = str(stims.get("intensity_calibration", ["(pre-light-model file)"])[0])
                    print(f"  Stimuli: loaded Deploy's {cand} "
                          f"({int(stims['n_trials'][0])} trials, intensity calibration {cal})")
                    return stims
            print(f"  Stimuli: no deployed stimuli.npz for {self.session_id} — generating here")
            stim_dir.mkdir(parents=True, exist_ok=True)
            # Load warp map if available
            warp_map = None
            warp_path = Path.home() / "rig" / "calibration" / "warp_map.npz"
            if warp_path.exists():
                warp_map = np.load(str(warp_path))
            metric = self.rig["devices"].get("display", {}).get("contrast_metric", "weber")
            # The light model travels in warp_map.npz, which Generate Warp copies to this Pi too,
            # so a fallback generation here uses the same intensity calibration as Deploy.
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
        # onset_ack_latency_s = SHOW -> renderer flip (the command path), timed by the arrival
        # of displayd's ack on this Pi (so on this Pi's clock, + the ack's ~1 ms way back).
        # display_latency_s - onset_ack_latency_s = flip -> photons (the projector pipeline).
        for name in ["iti_start_t", "stim_onset_t", "response_window_t",
                      "outcome_t", "first_lick_t",
                      "true_onset_t", "display_latency_s", "onset_ack_latency_s"]:
            f.create_dataset(name, shape=(0,), maxshape=(n,), dtype="f8")
        # Tri-state: 1 confirmed by a photodiode pulse, 0 sync on but no pulse, -1 sync off.
        f.create_dataset("sync_ok", shape=(0,), maxshape=(n,), dtype="i1")
        # Which anchor true_onset_t came from: "photodiode" | "ack" | "command".
        f.create_dataset("onset_source", shape=(0,), maxshape=(n,),
                         dtype=h5py.vlen_dtype(str))

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
        # onset_ack_latency_s = SHOW -> renderer flip (the command path), timed by the arrival
        # of displayd's ack on this Pi. display_latency_s - onset_ack_latency_s = flip -> photons.
        for name in ["iti_start_t", "stim_onset_t", "response_window_t",
                      "outcome_t", "first_lick_t",
                      "true_onset_t", "display_latency_s", "onset_ack_latency_s"]:
            f[name].resize(i + 1, axis=0)
            f[name][i] = self._trial_ctx.get(name, float("nan"))
        f["sync_ok"].resize(i + 1, axis=0)
        # Tri-state (see _create_hdf5_datasets); -1 when the key is missing = no sync ran.
        f["sync_ok"][i] = int(self._trial_ctx.get("sync_ok", -1))
        f["onset_source"].resize(i + 1, axis=0)
        f["onset_source"][i] = self._trial_ctx.get("onset_source", "command")

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
        self._ack_sock.close()
        # Consolidate into one self-contained .h5 now that every sidecar is finalized (the camera's
        # frame_timestamps.npy is flushed above in dev.close()) — instead of deferring it to transfer.
        self._consolidate_at_exit()

    def _consolidate_at_exit(self):
        """Fold this session's sidecars into the single <session_id>.h5 as the final exit step, so the
        archive is self-contained the moment the session ends (stop OR finish). Runs after dev.close(),
        so metadata.yaml / stimuli.npz / frame_timestamps.npy all exist. Idempotent + best-effort: it
        never raises out of shutdown, and the Data tab's sync runs consolidate again as a safety net.

        The video remux (ffmpeg over the whole recording) is NOT done here: the controller's teardown
        has pi_api SIGKILL this process 10 s after its SIGTERM, which cut every remux longer than that
        short (video.h264 + an orphaned video.mp4.part, cheddar 2026-09..11). The Data tab's consolidate
        step remuxes on the Pi right before the copy instead (shared/leader_data.py)."""
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
            res = consolidate_session(session_dir, video_dir if video_dir.exists() else None,
                                      remux_video=False)
            print(f"[consolidate] session end: {res} (video remux deferred to the Data-tab sync)",
                  flush=True)
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
                leader.start_info = msg
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
