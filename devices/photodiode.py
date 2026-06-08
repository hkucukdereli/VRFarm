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
        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpiod not running")
        self._pi.set_mode(self.gpio, pigpio.INPUT)
        self._pi.set_pull_up_down(self.gpio, pigpio.PUD_DOWN)
        self._cb = None
        self._callback = None
        self._active = False
        self._pulse_idx = 0
        self._ref_tick = None
        self._ref_time = None
        self._trial_pulses = []

    def start_stream(self, callback):
        import pigpio
        self._callback = callback
        self._active = True
        self._pulse_idx = 0
        self._ref_tick = None
        self._cb = self._pi.callback(self.gpio, pigpio.RISING_EDGE,
                                     self._on_edge)

    def stop_stream(self):
        self._active = False
        if self._cb:
            self._cb.cancel()
            self._cb = None

    def reset_trial(self):
        """Reset pulse counter at trial start."""
        self._pulse_idx = 0
        self._ref_tick = None
        self._ref_time = None
        self._trial_pulses = []

    def _on_edge(self, gpio, level, tick):
        if not self._active:
            return
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
        self._pi.stop()
