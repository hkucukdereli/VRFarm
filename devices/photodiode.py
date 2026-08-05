"""
devices/photodiode.py

Photodiode TTL sync input for stimulus frame timing.
GPIO input via pigpio hardware callbacks (microsecond resolution).
"""

from __future__ import annotations
import time

from .base import Device, DeviceInfo, IOType, register_device


@register_device
class Photodiode(Device):
    info = DeviceInfo("photodiode", "Photodiode", IOType.GPIO_IN, ["pigpio"])

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
        # Two independent edge filters, both authored in the Setup UI (ms). Each has an enable flag
        # so it can be toggled off while keeping its value; effective 0 => that filter is off.
        #  - STEADY (pigpio glitch filter): reports an edge only after the level held N µs, dropping
        #    anything narrower — a daemon-level filter that also DELAYS every reported edge by N.
        #  - HOLD-OFF (debounce): once a rising edge is accepted, ignore edges for N ms (in _on_edge);
        #    no delay on the accepted edge.
        glitch_on = bool(rig_config.get("glitch_enabled", True))
        glitch_ms = float(rig_config.get("glitch_ms", 0.5))
        # pigpio requires 0..300000 µs; clamp BOTH ends so a bad value can't raise after connect.
        self._glitch_us = min(max(int(glitch_ms * 1000), 0), 300000) if glitch_on else 0
        debounce_on = bool(rig_config.get("debounce_enabled", True))
        debounce_ms = float(rig_config.get("debounce_ms", 5.0))
        self._debounce_us = max(int(debounce_ms * 1000), 0) if debounce_on else 0
        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpiod not running")
        self._pi.set_mode(self.gpio, pigpio.INPUT)
        self._pi.set_pull_up_down(self.gpio, pigpio.PUD_DOWN)
        # Always set the glitch filter (even to 0) so toggling it off clears any filter a previous
        # init left on this GPIO on the same pigpiod.
        self._pi.set_glitch_filter(self.gpio, self._glitch_us)
        self._cb = None
        self._callback = None
        self._active = False
        self._pulse_idx = 0
        self._ref_tick = None
        self._ref_time = None
        self._last_accept_tick = None   # tick of the last ACCEPTED edge (hold-off anchor)
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
        self._last_accept_tick = None
        self._cb = self._pi.callback(self.gpio, pigpio.RISING_EDGE,
                                     self._on_edge)

    def stop_stream(self):
        self._active = False
        if self._cb:
            self._cb.cancel()
            self._cb = None

    def start_raw_capture(self, callback):
        """Diagnostic (setup-UI scope): capture EVERY transition (both edges) at µs resolution with
        the glitch filter DISABLED, so the browser sees the true digital waveform and applies
        steady/hold-off in software. callback({'t': unix_seconds, 'level': 0|1})."""
        import pigpio
        if self._raw_cb:                 # cancel a prior capture so a re-start can't double-deliver
            self._raw_cb.cancel()
            self._raw_cb = None
        self._raw_active = True
        self._raw_callback = callback
        self._raw_ref_tick = None
        self._raw_ref_time = None
        self._pi.set_glitch_filter(self.gpio, 0)   # raw view: no daemon-level filtering
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
        """Stop diagnostic capture and restore the configured glitch filter."""
        self._raw_active = False
        if self._raw_cb:
            self._raw_cb.cancel()
            self._raw_cb = None
        try:
            self._pi.set_glitch_filter(self.gpio, getattr(self, "_glitch_us", 0))
        except Exception:
            pass

    def reset_trial(self):
        """Reset pulse counter at trial start."""
        self._pulse_idx = 0
        self._ref_tick = None
        self._ref_time = None
        self._last_accept_tick = None
        self._trial_pulses = []

    def _on_edge(self, gpio, level, tick):
        if not self._active:
            return
        # HOLD-OFF debounce: after an accepted rising edge, ignore edges for _debounce_us. Uses the
        # pigpio uint32 µs tick (wrap-safe via the same & 0xFFFFFFFF mask used below), NOT time.time()
        # (NTP-disciplined, can step). Placed before the _ref_tick anchor so a dropped bounce never
        # becomes the trial timebase. First edge (_last_accept_tick None) always passes; 0 disables.
        if self._debounce_us > 0 and self._last_accept_tick is not None:
            if ((tick - self._last_accept_tick) & 0xFFFFFFFF) < self._debounce_us:
                return
        self._last_accept_tick = tick
        wall_t = time.time()
        if self._ref_tick is None:
            self._ref_tick = tick
            self._ref_time = wall_t
        # tick is uint32 microseconds, wraps at 2^32 (~71 min)
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
