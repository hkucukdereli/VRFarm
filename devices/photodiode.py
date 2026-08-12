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
import time

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
        if level == 2:      # lgpio watchdog/timeout, not a real edge
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
        try:
            self._lgpio.gpio_free(self._chip, self.gpio)
        except Exception:
            pass
        try:
            self._lgpio.gpiochip_close(self._chip)
        except Exception:
            pass
