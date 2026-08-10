"""
devices/photodiode.py

Photodiode TTL sync input for stimulus frame timing.

Reads a CLEAN, pre-filtered 3.3V sync pulse from a Teensy (teensy/photodiode_sync.ino):
the Teensy watches the raw photodiode and applies the steady/hold-off filtering, then
emits one square pulse per sync frame. So this device just timestamps the rising edges —
no on-Pi glitch/hold-off filtering. (The old self-filtering version is stashed at
devices/photodiode_filtered.py in case the Teensy path is abandoned.)

GPIO input via pigpio hardware callbacks (microsecond resolution).
"""

from __future__ import annotations
import time

from .base import Device, DeviceInfo, IOType, register_device


@register_device
class Photodiode(Device):
    info = DeviceInfo("photodiode", "Photodiode (Teensy sync)", IOType.GPIO_IN, ["pigpio"])

    @classmethod
    def task_params_schema(cls):
        return {
            "pulse_every_n_frames": {
                "type": "int", "default": 5,
                "label": "Sync pulse every N frames", "min": 1, "max": 60,
            },
        }

    def init(self, rig_config: dict, task_params: dict):
        import pigpio
        self.gpio = rig_config.get("gpio", 24)
        self.pulse_every_n = task_params.get("pulse_every_n_frames",
                             rig_config.get("pulse_every_n_frames", 5))
        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpiod not running")
        self._pi.set_mode(self.gpio, pigpio.INPUT)
        self._pi.set_pull_up_down(self.gpio, pigpio.PUD_DOWN)
        # Filtering lives on the Teensy now — clear any glitch filter a previous (filtered) init may
        # have left on this GPIO so the clean Teensy edges pass through untouched.
        self._pi.set_glitch_filter(self.gpio, 0)
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
        import pigpio
        if self._cb:                     # cancel a prior callback so a re-start can't double-deliver
            self._cb.cancel()
            self._cb = None
        self._callback = callback
        self._active = True
        self._pulse_idx = 0
        self._ref_tick = None
        self._cb = self._pi.callback(self.gpio, pigpio.RISING_EDGE, self._on_edge)

    def stop_stream(self):
        self._active = False
        if self._cb:
            self._cb.cancel()
            self._cb = None

    def start_raw_capture(self, callback):
        """Diagnostic (setup-UI scope): capture EVERY transition (both edges) at µs resolution, so
        the browser can display the true digital waveform coming from the Teensy.
        callback({'t': unix_seconds, 'level': 0|1})."""
        import pigpio
        if self._raw_cb:                 # cancel a prior capture so a re-start can't double-deliver
            self._raw_cb.cancel()
            self._raw_cb = None
        self._raw_active = True
        self._raw_callback = callback
        self._raw_ref_tick = None
        self._raw_ref_time = None
        self._raw_cb = self._pi.callback(self.gpio, pigpio.EITHER_EDGE, self._on_raw_edge)

    def _on_raw_edge(self, gpio, level, tick):
        if not self._raw_active:
            return
        if level == 2:      # pigpio watchdog timeout, not a real transition
            return
        wall_t = time.time()
        if self._raw_ref_tick is None:
            self._raw_ref_tick = tick
            self._raw_ref_time = wall_t
        t = self._raw_ref_time + ((tick - self._raw_ref_tick) & 0xFFFFFFFF) / 1e6
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
        a paired (hardware µs tick, wall clock) read — so the first pulse's timestamp comes from the
        jitter-free hardware tick, not a time.time() sampled inside the GIL-scheduled pigpio callback.
        Re-anchoring each trial keeps the tick delta sub-second, so the uint32-µs tick can't wrap."""
        self._pulse_idx = 0
        self._trial_pulses = []
        try:
            self._ref_tick = self._pi.get_current_tick()
            self._ref_time = time.time()
        except Exception:
            self._ref_tick = None            # fall back to first-pulse anchoring in _on_edge
            self._ref_time = None

    def _on_edge(self, gpio, level, tick):
        if not self._active:
            return
        # No hold-off/glitch filtering here — the Teensy already delivered a clean, single pulse.
        # Timebase is anchored in reset_trial (main thread); fall back to the first pulse if that read
        # failed, so timing still works if reset_trial wasn't called or get_current_tick raised.
        if self._ref_tick is None:
            self._ref_tick = tick
            self._ref_time = time.time()
        # tick is uint32 microseconds, wraps at 2^32 (~71 min); the per-trial re-anchor keeps the
        # delta small and the mask stays wrap-safe for a rare within-trial wrap.
        tick_diff_us = (tick - self._ref_tick) & 0xFFFFFFFF
        t_precise = self._ref_time + tick_diff_us / 1e6
        self._pulse_idx += 1
        self._trial_pulses.append(t_precise)
        if self._callback:
            self._callback({
                "event": "sync_pulse",
                "t": t_precise,
                "pulse_idx": self._pulse_idx,
            })

    def check(self) -> dict:
        if not self._pi.connected:
            return {"ok": False, "message": "pigpiod not connected"}
        try:
            level = self._pi.read(self.gpio)
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
        self._pi.stop()
