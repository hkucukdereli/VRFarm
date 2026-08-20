"""
devices/photodiode.py

Photodiode TTL sync input for stimulus frame timing.

Reads a CLEAN, pre-filtered 3.3V sync pulse from a Teensy (teensy/photodiode_sync.ino):
the Teensy watches the raw photodiode and applies the steady/hold-off filtering, then
emits one square pulse per sync frame. So this device just timestamps the rising edges —
no on-Pi glitch/hold-off filtering. (The old self-filtering version is stashed at
devices/photodiode_filtered.py in case the Teensy path is abandoned.)

GPIO input via lgpio hardware alerts (nanosecond ticks) — works on Pi 5 (RP1) and Pi 4 (BCM), no daemon.
"""

from __future__ import annotations
import threading
import time
from collections import deque

from .base import Device, DeviceInfo, IOType, register_device


@register_device
class Photodiode(Device):
    info = DeviceInfo("photodiode", "Photodiode (Teensy sync)", IOType.GPIO_IN, ["lgpio"])

    @classmethod
    def task_params_schema(cls):
        return {
            "pulse_every_n_frames": {
                "type": "int", "default": 5,
                "label": "Sync pulse every N frames", "min": 1, "max": 60,
            },
        }

    def init(self, rig_config: dict, task_params: dict):
        import lgpio
        self._lgpio = lgpio
        self.gpio = rig_config.get("gpio", 24)
        self.pulse_every_n = task_params.get("pulse_every_n_frames",
                             rig_config.get("pulse_every_n_frames", 5))
        # lgpio handle to the header gpiochip. Pi 5 (current OS) & Pi 4 expose the 40-pin header as
        # gpiochip0; very early Pi 5 images used gpiochip4 — set rig 'gpiochip' to override.
        self._chipnum = int(rig_config.get("gpiochip", 0))
        try:
            self._chip = lgpio.gpiochip_open(self._chipnum)
        except Exception as e:
            raise RuntimeError(f"lgpio: cannot open gpiochip{self._chipnum} ({e})")
        # Claim the pin for alerts on BOTH edges with a pull-down; the per-mode callbacks below filter
        # to the edge they want. Filtering lives on the Teensy now — clear the debounce (0) so the
        # clean Teensy edges pass through untouched.
        lgpio.gpio_claim_alert(self._chip, self.gpio, lgpio.BOTH_EDGES, lgpio.SET_PULL_DOWN)
        lgpio.gpio_set_debounce_micros(self._chip, self.gpio, 0)
        self._cb = None
        self._callback = None
        self._active = False
        self._pulse_idx = 0
        self._ref_tick = None
        self._ref_time = None
        self._trial_pulses = []
        # Diagnostic raw-transition capture (setup-UI scope); separate from the detected stream.
        self._raw_active = False
        self._raw_cb = None
        self._raw_callback = None
        self._raw_ref_tick = None
        self._raw_ref_time = None

        # ── v2_1: read the photodiode VOLTAGE from the Teensy over USB serial ──────────────────
        # The Teensy (photodiode_sync_v2_1) streams a bare V float per line at ~100 Hz on its USB
        # serial (plugged into THIS leader Pi). We keep a rolling window to (a) warn when the signal
        # drifts out of range and (b) validate the display->diode sync path at init (verify_sync).
        # No serial_port configured (or open fails) -> V features silently disabled; pulse timing
        # over GPIO is unaffected.
        self.serial_port = rig_config.get("serial_port")            # e.g. /dev/ttyACM0; None = off
        self.v_high = float(rig_config.get("v_high", 3.0))          # saturated: top 1% >= this
        self.v_low = float(rig_config.get("v_low", 0.3))           # too weak:  bottom 1% <= this
        self.v_window_s = float(rig_config.get("v_window_s", 10.0))
        self.v_realert_s = float(rig_config.get("v_realert_s", 30.0))
        self.verify_duration_s = float(rig_config.get("verify_duration_s", 1.0))
        self.verify_enabled = bool(rig_config.get("verify_enabled", True))
        self._v = deque(maxlen=max(100, int(self.v_window_s * 100)))   # (t, V) samples
        self._v_lock = threading.Lock()
        self._v_state = "ok"           # edge-triggered range monitor: "ok" | "warn"
        self._v_since = 0.0
        self._serial = None
        self._serial_thread = None
        self._serial_stop = threading.Event()
        if self.serial_port:
            self._open_serial()

    def _open_serial(self):
        """Open the Teensy serial port and start the reader thread. Never raises — a missing pyserial
        or unopenable port just disables the V features with a log line."""
        try:
            import serial
        except Exception as e:
            print(f"[photodiode] pyserial unavailable ({e}); V monitoring disabled", flush=True)
            return
        try:
            self._serial = serial.Serial(self.serial_port, 115200, timeout=1.0)
        except Exception as e:
            print(f"[photodiode] cannot open {self.serial_port} ({e}); V monitoring disabled", flush=True)
            self._serial = None
            return
        self._serial_stop.clear()
        self._serial_thread = threading.Thread(target=self._serial_reader, daemon=True)
        self._serial_thread.start()

    def _serial_reader(self):
        """Daemon: parse the bare V float per line into the rolling window; periodic range check."""
        n = 0
        while not self._serial_stop.is_set():
            try:
                line = self._serial.readline()
            except Exception:
                break
            if not line:
                continue
            try:
                v = float(line.split()[0])     # first token = V (also works if DEBUG 4-field is on)
            except (ValueError, IndexError):
                continue
            with self._v_lock:
                self._v.append((time.time(), v))
            n += 1
            if n >= 200:                       # ~2 s at 100 Hz -> re-evaluate the V range
                n = 0
                try:
                    self._check_v_range()
                except Exception:
                    pass

    def _v_percentiles(self, t_start=None, t_end=None):
        """(p1, p99) over the V window, optionally within [t_start, t_end]; None if too few samples."""
        import numpy as np
        with self._v_lock:
            vals = [v for (t, v) in self._v
                    if (t_start is None or t >= t_start) and (t_end is None or t <= t_end)]
        if len(vals) < 10:
            return None
        return float(np.percentile(vals, 1)), float(np.percentile(vals, 99))

    def _check_v_range(self):
        """Edge-triggered warning: top 1% >= v_high (saturating) or bottom 1% <= v_low (too weak).
        Recover notice on return; re-alert every v_realert_s while sustained (shepherd pattern)."""
        pr = self._v_percentiles()
        if pr is None:
            return
        p1, p99 = pr
        bad = (p99 >= self.v_high) or (p1 <= self.v_low)
        now = time.time()
        if bad:
            if p99 >= self.v_high:
                msg = f"Photodiode V saturating (top 1% = {p99:.2f} V >= {self.v_high})"
            else:
                msg = f"Photodiode V too weak (bottom 1% = {p1:.2f} V <= {self.v_low})"
            if self._v_state == "ok":
                self._v_state = "warn"
                self._v_since = now
                self._emit_warning(msg, "warning")
            elif (now - self._v_since) >= self.v_realert_s:
                self._v_since = now
                self._emit_warning(msg, "warning")
        elif self._v_state == "warn":
            self._v_state = "ok"
            self._emit_warning(f"Photodiode V back in range (1% = {p1:.2f}, 99% = {p99:.2f})", "ok")

    def _emit_warning(self, message, level):
        """Fire a UI-log event through the stream callback (Leader._publish -> SSE). If not streaming
        (e.g. setup), just print — pi_api captures it."""
        evt = {"event": "photodiode_warning", "level": level, "message": message, "t": time.time()}
        if self._callback:
            try:
                self._callback(evt)
                return
            except Exception:
                pass
        print(f"[photodiode] {level}: {message}", flush=True)

    def verify_sync(self, sync_burst, duration_s=None):
        """Universal init check: flash the display for ~duration_s while counting our own rising edges
        and sampling V, then pass/fail. `sync_burst(duration_s) -> emitted_flash_count` is supplied by
        the caller (the flash runs on the FOLLOWER, so the transport differs: setup POSTs to the
        follower, the experiment UDPs it). Returns {ok, detected, emitted, p1, p99, reason}."""
        duration_s = self.verify_duration_s if duration_s is None else float(duration_s)
        started_here = self._cb is None
        if started_here:
            self.start_stream(lambda e: None)    # register the RISING-edge counter (_on_edge)
        self.reset_trial()                       # zero pulse count + anchor timebase on the main thread
        t0 = time.time()
        try:
            emitted = int(sync_burst(duration_s))
        except Exception as e:
            if started_here:
                self.stop_stream()
                self._callback = None
            return {"ok": False, "detected": 0, "emitted": None, "p1": None, "p99": None,
                    "reason": f"display flash failed: {e}"}
        detected = self._pulse_idx
        if started_here:
            self.stop_stream()
            self._callback = None
        # V gate (only when the serial stream is available): fail if the flash peak never rose
        # (99% <= v_low) or the signal is saturated (1% >= v_high).
        p1 = p99 = None
        v_ok = True
        if self._serial is not None:
            pr = self._v_percentiles(t0, time.time())
            if pr is not None:
                p1, p99 = pr
                v_ok = (p99 > self.v_low) and (p1 < self.v_high)
        count_ok = abs(detected - emitted) <= 1
        ok = bool(count_ok and v_ok)
        if not count_ok:
            reason = f"pulse mismatch: detected {detected} vs emitted {emitted} (±1 allowed)"
        elif not v_ok and p99 is not None and p99 <= self.v_low:
            reason = f"no flash detected: peak V (99% = {p99:.2f}) <= {self.v_low}"
        elif not v_ok:
            reason = f"saturated: baseline V (1% = {p1:.2f}) >= {self.v_high}"
        else:
            reason = "ok"
        return {"ok": ok, "detected": detected, "emitted": emitted, "p1": p1, "p99": p99,
                "reason": reason}

    def start_stream(self, callback):
        lgpio = self._lgpio
        if self._cb:                     # cancel a prior callback so a re-start can't double-deliver
            self._cb.cancel()
            self._cb = None
        self._callback = callback
        self._active = True
        self._pulse_idx = 0
        self._ref_tick = None
        self._cb = lgpio.callback(self._chip, self.gpio, lgpio.RISING_EDGE, self._on_edge)

    def stop_stream(self):
        self._active = False
        if self._cb:
            self._cb.cancel()
            self._cb = None

    def start_raw_capture(self, callback):
        """Diagnostic (setup-UI scope): capture EVERY transition (both edges) at hardware-tick
        resolution, so the browser can display the true digital waveform coming from the Teensy.
        callback({'t': unix_seconds, 'level': 0|1})."""
        lgpio = self._lgpio
        if self._raw_cb:                 # cancel a prior capture so a re-start can't double-deliver
            self._raw_cb.cancel()
            self._raw_cb = None
        self._raw_active = True
        self._raw_callback = callback
        self._raw_ref_tick = None
        self._raw_ref_time = None
        lgpio.gpio_set_debounce_micros(self._chip, self.gpio, 0)   # raw view: no driver-level filtering
        self._raw_cb = lgpio.callback(self._chip, self.gpio, lgpio.BOTH_EDGES, self._on_raw_edge)

    def _on_raw_edge(self, chip, gpio, level, tick):
        if not self._raw_active:
            return
        if level == 2:      # lgpio watchdog/timeout, not a real transition
            return
        wall_t = time.time()
        if self._raw_ref_tick is None:
            self._raw_ref_tick = tick
            self._raw_ref_time = wall_t
        t = self._raw_ref_time + (tick - self._raw_ref_tick) / 1e9   # lgpio tick = nanoseconds
        if self._raw_callback:
            self._raw_callback({"t": t, "level": int(level)})

    def stop_raw_capture(self):
        """Stop diagnostic capture."""
        self._raw_active = False
        if self._raw_cb:
            self._raw_cb.cancel()
            self._raw_cb = None

    def reset_trial(self):
        """Reset the per-trial pulse buffer AND re-anchor the trial timebase on the MAIN thread from
        a paired (hardware ns tick, wall clock) read — so the first pulse's timestamp comes from the
        jitter-free hardware tick, not a time.time() sampled inside the GIL-scheduled lgpio callback.
        lgpio alert ticks are CLOCK_MONOTONIC ns (the kernel GPIO-event timestamp), so
        time.monotonic_ns() is the matching main-thread reference. Using time.time_ns()
        (CLOCK_REALTIME) here mismatched the clocks and put every pulse ~1.7e9 s in the past
        (sync_ok=0 every trial, pulses off the raster)."""
        self._pulse_idx = 0
        self._trial_pulses = []
        try:
            self._ref_tick = time.monotonic_ns()
            self._ref_time = time.time()
        except Exception:
            self._ref_tick = None            # fall back to first-pulse anchoring in _on_edge
            self._ref_time = None

    def _on_edge(self, chip, gpio, level, tick):
        if not self._active:
            return
        # The line is claimed BOTH_EDGES (the raw scope needs falling edges too), so this
        # RISING-only detected stream must drop non-rising deliveries itself: level 0 = falling
        # (the trailing edge of the Teensy's ~2 ms pulse — logging it double-counts every flash),
        # level 2 = lgpio watchdog/timeout. Keep only level 1 = the rising edge = one per sync flash.
        if level != 1:
            return
        # No hold-off/glitch filtering here — the Teensy already delivered a clean, single pulse.
        # Timebase is anchored in reset_trial (main thread); fall back to the first pulse if that
        # reference read failed, so timing still works if reset_trial wasn't called.
        if self._ref_tick is None:
            self._ref_tick = tick
            self._ref_time = time.time()
        # lgpio tick is CLOCK_MONOTONIC ns; the main-thread (monotonic_ns, time) anchor makes the
        # precise edge time = ref wall clock + hardware-tick delta (ns -> s), free of callback jitter.
        t_precise = self._ref_time + (tick - self._ref_tick) / 1e9
        self._pulse_idx += 1
        self._trial_pulses.append(t_precise)
        if self._callback:
            self._callback({
                "event": "sync_pulse",
                "t": t_precise,
                "pulse_idx": self._pulse_idx,
            })

    def check(self) -> dict:
        try:
            level = self._lgpio.gpio_read(self._chip, self.gpio)
            return {"ok": True, "message": f"GPIO{self.gpio} level={level}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def hdf5_datasets(self) -> dict:
        import numpy as np
        import h5py
        return {"sync_pulses": h5py.vlen_dtype(np.float64)}

    def hdf5_trial_data(self, ctx: dict) -> dict:
        return {"sync_pulses": ctx.get("sync_pulses", self._trial_pulses)}

    def close(self):
        self.stop_stream()
        self.stop_raw_capture()
        self._serial_stop.set()
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        try:
            self._lgpio.gpio_free(self._chip, self.gpio)
        except Exception:
            pass
        try:
            self._lgpio.gpiochip_close(self._chip)
        except Exception:
            pass
