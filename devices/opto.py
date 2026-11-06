"""
devices/opto.py

Optogenetic stimulator: a laser / LED driver's TTL input on a leader GPIO, plus an optional
masking LED on a second GPIO that flashes on EVERY trial with the same timing, so the mouse
cannot tell opto trials by the light.

Division of labour:
  WHICH trials  — task opto: fraction / eligible / min_gap_trials, decided at Deploy in
                  shared/stim_generator.py (stimuli.npz `opto_on`, trials.yaml `opto`).
  WHEN          — task opto.windows: one window per anchor (iti_start, trial_start, stim_onset,
                  response_window, reward), each {enabled, onset_s (signed), duration_s}. On an
                  opto trial EVERY enabled window fires (the laser); the mask fires them on every
                  trial. Only stim_onset may lead (onset_s < 0): every other anchor is unpredictable.
  WHAT          — task opto.pattern: continuous, or pulsed (pulse_hz x pulse_width_ms).
  WHERE         — rig devices.opto: pins {laser, mask}, gpiochip, max_on_s (hard per-window cap),
                  show_latency_s (seed of the SHOW->photons estimate the leads use).

Timing lives here. The engine calls fire_at() the moment it stamps an anchor time, the photodiode
edge callback (lgpio's notify thread) calls on_sync_edge() for the stimulus anchor — so a window
with onset_s = 0 is written sub-ms after the first stimulus frame — and a scheduler thread writes
every later edge at an ABSOLUTE deadline (no relative-sleep drift).

Why gpio_write and not lgpio.tx_pulse: tx_pulse is a phase-locked PWM engine (lgPthTx.c,
lgGpioCreateTxRec: the first edge waits for the next boundary of the cycle grid — a 2 s
"continuous" pulse would start up to 2 s late — and (0, 0) stops a train without driving the pin
LOW). gpio_write is one ioctl (~50 us); Python-timed edges land ~0.1-1 ms late, <= 5 ms in the
worst case (GIL switch interval), hence the floors: pulse_width_ms >= 2, pulse_hz <= 100.

Safety: pins are claimed output-LOW and driven LOW at cancel()/close(); every window is capped by
max_on_s; the OFF edge of an active window survives finish_trial(). The Pi is NOT an interlock —
a SIGKILLed engine leaves the line at its last level (pi_api /api/opto_off clears it at teardown);
the driver's own max-on-time or a one-shot on the TTL line is the real safeguard.
"""

from __future__ import annotations
import heapq
import math
import statistics
import threading
import time
from collections import deque

from .base import Device, DeviceInfo, IOType, register_device

WINDOW_ANCHORS = ("iti_start", "trial_start", "stim_onset", "response_window", "reward")
DEFAULT_MAX_ON_S = 5.0
DEFAULT_SHOW_LATENCY_S = 0.09   # SHOW -> photons, measured median across the rigs (2026-11)
SYNC_MIN_LATENCY_S = 0.045      # an edge earlier than this after SHOW cannot be this stimulus
                                # (genuine latencies start at ~57 ms; heartbeat sub-pulses sent
                                # just before SHOW land earlier) — same constant as the engine
MIN_PULSE_WIDTH_MS = 2.0
MAX_PULSE_HZ = 100.0
MAX_LEAD_S = 1.0                # a stim_onset lead delays SHOW by (lead - latency): keep it short
LATENCY_WINDOW = 20             # running median over this many confirmed onsets
_DUE_SLOP_S = 0.0005            # an edge this close to now is written on the calling thread


def pattern_edges(pattern: dict, t_on: float, duration_s: float) -> list:
    """The absolute (t, level) edges of one window starting at t_on. continuous -> ON at t_on,
    OFF at t_on + duration. pulsed -> round(duration x hz) pulses of pulse_width_ms on a fixed
    grid from t_on (the last OFF may run past t_on + duration by less than one width)."""
    mode = str((pattern or {}).get("mode", "continuous") or "continuous").strip().lower()
    duration_s = float(duration_s)
    if mode == "pulsed":
        hz = float(pattern.get("pulse_hz", 20.0))
        width = float(pattern.get("pulse_width_ms", 5.0)) / 1000.0
        period = 1.0 / hz
        n = max(1, int(round(duration_s * hz)))
        edges = []
        for k in range(n):
            t0 = t_on + k * period
            edges.append((t0, 1))
            edges.append((t0 + width, 0))
        return edges
    return [(t_on, 1), (t_on + duration_s, 0)]


def parse_windows(opto: dict) -> list:
    """The ENABLED windows of a task opto: section, in anchor order:
    [{"anchor", "onset_s", "duration_s"}]."""
    wins = (opto or {}).get("windows") or {}
    out = []
    for anchor in WINDOW_ANCHORS:
        w = wins.get(anchor)
        if isinstance(w, dict) and w.get("enabled"):
            out.append({"anchor": anchor,
                        "onset_s": float(w.get("onset_s", 0.0) or 0.0),
                        "duration_s": float(w.get("duration_s", 0.0) or 0.0)})
    return out


def opto_spec_errors(opto: dict, pins, max_on_s: float) -> list:
    """Everything wrong with a task opto: section's windows / pattern / mask, as messages. `pins`
    is the rig device's pins dict (None = no rig to check against). Shared by the Deploy
    validation (shared/config.validate_opto) and the device's init, so the controller refuses
    exactly what the device would."""
    errs = []
    opto = opto or {}
    pat = opto.get("pattern") or {}
    mode = str(pat.get("mode", "continuous") or "continuous").strip().lower()
    if mode not in ("continuous", "pulsed"):
        errs.append("opto.pattern.mode must be continuous | pulsed")
    elif mode == "pulsed":
        try:
            hz = float(pat.get("pulse_hz", 0))
            width = float(pat.get("pulse_width_ms", 0))
        except (TypeError, ValueError):
            hz, width = 0.0, 0.0
        if not 0 < hz <= MAX_PULSE_HZ:
            errs.append(f"opto.pattern.pulse_hz must be > 0 and <= {MAX_PULSE_HZ:g} Hz "
                        "(Python-timed edges)")
        elif width < MIN_PULSE_WIDTH_MS:
            errs.append(f"opto.pattern.pulse_width_ms must be >= {MIN_PULSE_WIDTH_MS:g} ms")
        elif width >= 1000.0 / hz:
            errs.append(f"opto.pattern.pulse_width_ms ({width:g}) must be shorter than the "
                        f"{1000.0 / hz:g} ms period at {hz:g} Hz")
    wins = opto.get("windows") or {}
    for k in wins:
        if k not in WINDOW_ANCHORS:
            errs.append(f"unknown opto window anchor '{k}' (one of {', '.join(WINDOW_ANCHORS)})")
    enabled = parse_windows(opto)
    if not enabled:
        errs.append("opto is enabled but no window is enabled")
    for w in enabled:
        d, o = w["duration_s"], w["onset_s"]
        if not (d > 0):
            errs.append(f"opto window {w['anchor']}: duration_s must be > 0")
        elif max_on_s and d > float(max_on_s):
            errs.append(f"opto window {w['anchor']}: duration_s {d:g} exceeds the rig's "
                        f"max_on_s {float(max_on_s):g}")
        if not math.isfinite(o):
            errs.append(f"opto window {w['anchor']}: onset_s must be a number")
        elif o < 0 and w["anchor"] != "stim_onset":
            errs.append(f"opto window {w['anchor']}: a negative onset is only possible for "
                        "stim_onset (the other anchors cannot be predicted)")
        elif o < -MAX_LEAD_S:
            errs.append(f"opto window stim_onset: the lead is capped at {MAX_LEAD_S:g} s "
                        "(it delays the stimulus)")
    if opto.get("mask") and pins is not None and "mask" not in pins:
        errs.append("opto.mask is on but the rig's opto device has no mask pin")
    if pins is not None and "laser" not in pins:
        errs.append("the rig's opto device has no laser pin")
    return errs


@register_device
class OptoStim(Device):
    info = DeviceInfo("opto", "Optogenetics", IOType.GPIO_OUT, ["lgpio"])

    def __init__(self):
        self._lgpio = None
        self._chip = None
        self._pins = {}                 # name -> gpio
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._heap = []                 # (t, seq, pin|None, level, rec|None, last)
        self._seq = 0
        self._thread = None
        self._stop = False
        self._cb = None
        self.enabled = False
        self.windows = []
        self.pattern = {"mode": "continuous", "pulse_hz": 20.0, "pulse_width_ms": 5.0}
        self.mask = False
        self.max_on_s = DEFAULT_MAX_ON_S
        self.show_latency_s = DEFAULT_SHOW_LATENCY_S
        self._lat = deque(maxlen=LATENCY_WINDOW)
        self._trial = None              # per-trial state (see arm_trial)
        self._edge = None               # {"since", "seq"} while the stim edge-lock is armed
        self._on_until = {}             # pin -> wall-clock time until which it is ON

    # ── lifecycle ──

    def init(self, rig_config: dict, task_params: dict):
        import lgpio
        self._lgpio = lgpio
        rig_config = rig_config or {}
        task_params = task_params or {}
        pins_cfg = rig_config.get("pins") or {}
        if "laser" not in pins_cfg:
            raise RuntimeError("Opto cannot init: the rig's opto device has no laser pin")
        self.max_on_s = float(rig_config.get("max_on_s", DEFAULT_MAX_ON_S) or DEFAULT_MAX_ON_S)
        self.show_latency_s = float(rig_config.get("show_latency_s", DEFAULT_SHOW_LATENCY_S)
                                    or DEFAULT_SHOW_LATENCY_S)
        self.enabled = bool(task_params.get("enabled", False))
        self.windows = parse_windows(task_params) if self.enabled else []
        pat = task_params.get("pattern") or {}
        self.pattern = {"mode": str(pat.get("mode", "continuous") or "continuous").lower(),
                        "pulse_hz": float(pat.get("pulse_hz", 20.0) or 20.0),
                        "pulse_width_ms": float(pat.get("pulse_width_ms", 5.0) or 5.0)}
        self.mask = self.enabled and bool(task_params.get("mask", False))
        if self.enabled:
            errs = opto_spec_errors(task_params, pins_cfg, self.max_on_s)
            if errs:
                raise RuntimeError("Opto cannot init: " + "; ".join(errs))
        chipnum = int(rig_config.get("gpiochip", 0) or 0)
        try:
            self._chip = lgpio.gpiochip_open(chipnum)
        except Exception as e:
            raise RuntimeError(f"Opto cannot init: lgpio cannot open gpiochip{chipnum} ({e})")
        for name in ("laser", "mask"):
            cfg = pins_cfg.get(name)
            if not cfg:
                continue
            gpio = int(cfg["gpio"] if isinstance(cfg, dict) else cfg)
            lgpio.gpio_claim_output(self._chip, gpio, 0)      # claim as output, LOW
            if lgpio.gpio_read(self._chip, gpio) != 0:
                raise RuntimeError(f"Opto cannot init: GPIO{gpio} ({name}) reads HIGH after a "
                                   "LOW claim — wiring or pin conflict")
            self._pins[name] = gpio
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="opto-sched", daemon=True)
        self._thread.start()

    def check(self) -> dict:
        try:
            levels = {n: self._lgpio.gpio_read(self._chip, g) for n, g in self._pins.items()}
        except Exception as e:
            return {"ok": False, "message": f"GPIO read failed: {e}"}
        pins = ", ".join(f"{n} GPIO{g}" + (" HIGH!" if levels[n] else "") for n, g in self._pins.items())
        pat = self.pattern["mode"]
        if pat == "pulsed":
            pat += f" {self.pattern['pulse_hz']:g} Hz x {self.pattern['pulse_width_ms']:g} ms"
        wins = ", ".join(f"{w['anchor']} {w['onset_s']:+.2f}/{w['duration_s']:g}s" for w in self.windows)
        task = (f"{pat}; windows: {wins or 'none'}" + ("; mask" if self.mask else "")
                if self.enabled else "task opto off")
        return {"ok": not any(levels.values()),
                "message": f"pins: {pins} · cap {self.max_on_s:g} s · {task}"}

    def start_stream(self, callback):
        self._cb = callback

    def stop_stream(self):
        self.cancel()

    def close(self):
        self._stop = True
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._chip is not None:
            for gpio in self._pins.values():
                try:
                    self._lgpio.gpio_write(self._chip, gpio, 0)
                except Exception:
                    pass
                try:
                    self._lgpio.gpio_free(self._chip, gpio)
                except Exception:
                    pass
            try:
                self._lgpio.gpiochip_close(self._chip)
            except Exception:
                pass
            self._chip = None

    # ── trial API (engine) ──

    @property
    def active(self) -> bool:
        """The task asked for opto AND the device has something to fire."""
        return self.enabled and bool(self.windows)

    def arm_trial(self, trial_num: int, opto_on: bool):
        """Called BEFORE the trial's ITI (the iti_start anchor comes first). Laser windows fire
        only when opto_on; mask windows whenever the task's mask is on."""
        with self._lock:
            self._edge = None
            pending = {}
            if self.active:
                for w in self.windows:
                    pending.setdefault(w["anchor"], []).append(w)
            self._trial = {"num": int(trial_num), "opto_on": bool(opto_on) and self.active,
                           "pending": pending, "windows": [], "dropped": 0,
                           "reward_done": False, "in_trial": False}

    def lead_s(self) -> float:
        """How far the stim_onset window leads the stimulus on THIS trial (0 when nothing fires)."""
        with self._lock:
            t = self._trial
            if t is None or not (t["opto_on"] or self.mask):
                return 0.0
            return max([-w["onset_s"] for w in t["pending"].get("stim_onset", [])
                        if w["onset_s"] < 0] or [0.0])

    def latency_estimate(self) -> float:
        """SHOW -> photons estimate: the running median of confirmed onsets, else the rig seed."""
        with self._lock:
            if len(self._lat) >= 3:
                return float(statistics.median(self._lat))
            return float(self.show_latency_s)

    def note_true_onset(self, t_show: float, t_true: float):
        lat = float(t_true) - float(t_show)
        if 0.04 <= lat < 0.5:
            with self._lock:
                self._lat.append(lat)

    def fire_at(self, anchor: str, t_anchor: float):
        """The engine has just stamped `anchor` at t_anchor: start every pending window of that
        anchor at t_anchor + onset_s (written now if due, else scheduled). trial_start opens the
        trial for the reward anchor; a reward fires it once per trial."""
        evts = []
        now = time.time()
        with self._lock:
            t = self._trial
            if t is None:
                return
            if anchor == "trial_start":
                t["in_trial"] = True
            if anchor == "reward":
                if not t["in_trial"] or t["reward_done"]:
                    return
                t["reward_done"] = True
            for w in t["pending"].pop(anchor, []):
                if anchor == "stim_onset" and w["onset_s"] >= 0:
                    t["pending"].setdefault(anchor, []).append(w)   # edge-locked, not here
                    continue
                self._start_window(w, float(t_anchor) + w["onset_s"], "anchor", now, evts)
        self._emit(evts)

    def fire_stim_lead(self, t_pred_onset: float):
        """stim_onset windows with a negative onset, relative to the PREDICTED onset (SHOW + the
        latency estimate); the engine holds SHOW so the lead comes out right on average."""
        evts = []
        now = time.time()
        with self._lock:
            t = self._trial
            if t is None:
                return
            keep = []
            for w in t["pending"].pop("stim_onset", []):
                if w["onset_s"] < 0:
                    self._start_window(w, float(t_pred_onset) + w["onset_s"], "pred", now, evts)
                else:
                    keep.append(w)
            if keep:
                t["pending"]["stim_onset"] = keep
        self._emit(evts)

    def arm_edge(self, since_t: float, t_fallback_onset: float):
        """Arm the stimulus edge-lock just before SHOW: the first photodiode edge at or after
        since_t + SYNC_MIN_LATENCY_S fires the stim_onset windows with onset_s >= 0; if none
        arrives, they fire from t_fallback_onset (the predicted onset) instead."""
        with self._cond:
            t = self._trial
            if t is None or not any(w["onset_s"] >= 0 for w in t["pending"].get("stim_onset", [])):
                return
            self._seq += 1
            self._edge = {"since": float(since_t), "seq": self._seq}
            heapq.heappush(self._heap, (float(t_fallback_onset), self._seq, None, 0, None, False))
            self._cond.notify_all()

    def on_sync_edge(self, t_edge: float):
        """Photodiode rising edge (lgpio's notify thread). Sub-ms path: the ON edge of an
        onset_s = 0 window is written right here."""
        evts = []
        now = time.time()
        with self._lock:
            e = self._edge
            if e is None or float(t_edge) < e["since"] + SYNC_MIN_LATENCY_S:
                return
            self._edge = None
            self._fire_stim_locked(float(t_edge), "edge", now, evts)
        self._emit(evts)

    def finish_trial(self) -> dict:
        """The trial's record for HDF5 (merged into the trial context by the engine). Windows
        still ON keep their scheduled OFF edge; a laser window that has not started yet is
        recorded with its planned on time."""
        nan = float("nan")
        with self._lock:
            t = self._trial
            self._edge = None
            if t is None:
                return {"opto_on": 0, "opto_mask_on": 0, "opto_onset_t": nan, "opto_n_windows": 0,
                        "opto_n_dropped": 0, "opto_window_t": [], "opto_window_anchor": ""}
            t["pending"] = {}
            t["in_trial"] = False
            laser = [r for r in t["windows"] if r["pin"] == "laser"]
            mask = [r for r in t["windows"] if r["pin"] == "mask"]
            t_ons = [r["t_on"] for r in laser if r["t_on"] is not None]
            flat = []
            for r in laser:
                flat += [r["t_on"] if r["t_on"] is not None else r["t_on_planned"], r["t_off"]]
            rec = {"opto_on": int(t["opto_on"]), "opto_mask_on": int(bool(mask)),
                   "opto_onset_t": min(t_ons) if t_ons else nan,
                   "opto_n_windows": len(laser), "opto_n_dropped": int(t["dropped"]),
                   "opto_window_t": flat,
                   "opto_window_anchor": ",".join(f"{r['anchor']}:{r['source']}" for r in laser)}
        return rec

    def cancel(self):
        """Light off NOW on every pin; every scheduled edge and the edge-lock dropped."""
        evts = []
        with self._cond:
            self._heap.clear()
            self._edge = None
            now = time.time()
            for name, gpio in self._pins.items():
                if self._chip is not None:
                    try:
                        self._lgpio.gpio_write(self._chip, gpio, 0)
                    except Exception:
                        pass
                if self._on_until.get(name, 0.0) > now:
                    evts.append({"event": "opto", "on": False, "t": now, "pin": name,
                                 "trial": self._trial["num"] if self._trial else None,
                                 "anchor": "cancel", "source": "cancel"})
            self._on_until.clear()
            if self._trial is not None:
                self._trial["pending"] = {}
            self._cond.notify_all()
        self._emit(evts)

    # ── HDF5 ──

    def hdf5_datasets(self) -> dict:
        import numpy as np
        import h5py
        return {
            "opto_on": "i1",                   # 1 = laser windows fired this trial (an opto trial)
            "opto_mask_on": "i1",              # 1 = masking LED fired (every trial when mask is on)
            "opto_onset_t": "f8",              # first laser ON edge (actual write time), NaN if none
            "opto_n_windows": "i2",            # laser windows started
            "opto_n_dropped": "i2",            # windows dropped because the pin was still ON
            "opto_window_t": {"dtype": h5py.vlen_dtype(np.float64)},   # [on, off, on, off, ...]
            "opto_window_anchor": {"dtype": h5py.vlen_dtype(str)},     # "stim_onset:edge,reward:anchor"
        }

    def hdf5_trial_data(self, ctx: dict) -> dict:
        nan = float("nan")
        return {
            "opto_on": int(ctx.get("opto_on", 0) or 0),
            "opto_mask_on": int(ctx.get("opto_mask_on", 0) or 0),
            "opto_onset_t": float(ctx.get("opto_onset_t", nan)),
            "opto_n_windows": int(ctx.get("opto_n_windows", 0) or 0),
            "opto_n_dropped": int(ctx.get("opto_n_dropped", 0) or 0),
            "opto_window_t": list(ctx.get("opto_window_t", []) or []),
            "opto_window_anchor": str(ctx.get("opto_window_anchor", "") or ""),
        }

    # ── internals (lock held unless stated) ──

    def _fire_stim_locked(self, t_onset: float, source: str, now: float, evts: list):
        t = self._trial
        if t is None:
            return
        keep = []
        for w in t["pending"].pop("stim_onset", []):
            if w["onset_s"] >= 0:
                self._start_window(w, t_onset + w["onset_s"], source, now, evts)
            else:
                keep.append(w)
        if keep:
            t["pending"]["stim_onset"] = keep

    def _start_window(self, w: dict, t_on: float, source: str, now: float, evts: list):
        t = self._trial
        pins = []
        if t["opto_on"]:
            pins.append("laser")
        if self.mask and "mask" in self._pins:
            pins.append("mask")
        for pin in pins:
            if self._on_until.get(pin, 0.0) > t_on:        # still ON from an earlier window
                t["dropped"] += 1
                continue
            edges = pattern_edges(self.pattern, t_on, w["duration_s"])
            rec = {"anchor": w["anchor"], "pin": pin, "source": source, "trial": t["num"],
                   "t_on": None, "t_on_planned": t_on, "t_off": edges[-1][0]}
            t["windows"].append(rec)
            self._on_until[pin] = rec["t_off"]
            if edges[0][0] <= now + _DUE_SLOP_S:            # due: write it on THIS thread
                self._write(pin, 1, rec, False, evts)
                edges = edges[1:]
            for i, (te, level) in enumerate(edges):
                self._seq += 1
                heapq.heappush(self._heap, (te, self._seq, pin, level, rec, i == len(edges) - 1))
        self._cond.notify_all()

    def _write(self, pin: str, level: int, rec: dict, last: bool, evts: list):
        if self._chip is not None:
            self._lgpio.gpio_write(self._chip, self._pins[pin], 1 if level else 0)
        t = time.time()
        if level and rec["t_on"] is None:
            rec["t_on"] = t
            evts.append({"event": "opto", "on": True, "t": t, "trial": rec["trial"], "pin": pin,
                         "anchor": rec["anchor"], "source": rec["source"], "t_off": rec["t_off"]})
        elif not level and last:
            evts.append({"event": "opto", "on": False, "t": t, "trial": rec["trial"], "pin": pin,
                         "anchor": rec["anchor"], "source": rec["source"]})

    def _run(self):
        while not self._stop:
            evts = []
            with self._cond:
                while not self._stop:
                    if not self._heap:
                        self._cond.wait()
                        continue
                    dt = self._heap[0][0] - time.time()
                    if dt > 0:
                        self._cond.wait(timeout=min(dt, 0.25))
                        continue
                    te, seq, pin, level, rec, last = heapq.heappop(self._heap)
                    if pin is None:                                   # the stim edge fallback
                        if self._edge is not None and self._edge["seq"] == seq:
                            self._edge = None
                            self._fire_stim_locked(te, "fallback", time.time(), evts)
                    else:
                        self._write(pin, level, rec, last, evts)
                    break                                             # emit outside the lock
            self._emit(evts)

    def _emit(self, evts: list):
        cb = self._cb
        if cb is None:
            return
        for e in evts:
            try:
                cb(e)
            except Exception:
                pass
