"""
devices/photodiode.py

Photodiode TTL sync input for stimulus frame timing.
GPIO input via lgpio hardware alerts (nanosecond ticks) — works on Pi 5 (RP1) and Pi 4 (BCM), no daemon.
"""

from __future__ import annotations
import time

from .base import Device, DeviceInfo, IOType, register_device


@register_device
class Photodiode(Device):
    info = DeviceInfo("photodiode", "Photodiode", IOType.GPIO_IN, ["lgpio"])

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
        # Two independent edge filters, both authored in the Setup UI (ms). Each has an enable flag
        # so it can be toggled off while keeping its value; effective 0 => that filter is off.
        #  - STEADY (lgpio debounce filter): reports an edge only after the level held N µs, dropping
        #    anything narrower — a driver-level filter that also DELAYS every reported edge by N.
        #  - HOLD-OFF (debounce): once a rising edge is accepted, ignore edges for N ms (in _on_edge);
        #    no delay on the accepted edge.
        glitch_on = bool(rig_config.get("glitch_enabled", True))
        glitch_ms = float(rig_config.get("glitch_ms", 0.5))
        self._glitch_us = max(int(glitch_ms * 1000), 0) if glitch_on else 0
        debounce_on = bool(rig_config.get("debounce_enabled", True))
        debounce_ms = float(rig_config.get("debounce_ms", 5.0))
        self._debounce_us = max(int(debounce_ms * 1000), 0) if debounce_on else 0
        # lgpio handle to the header gpiochip. Pi 5 (current OS) & Pi 4 expose the 40-pin header as
        # gpiochip0; very early Pi 5 images used gpiochip4 — set rig 'gpiochip' to override.
        self._chipnum = int(rig_config.get("gpiochip", 0))
        try:
            self._chip = lgpio.gpiochip_open(self._chipnum)
        except Exception as e:
            raise RuntimeError(f"lgpio: cannot open gpiochip{self._chipnum} ({e})")
        # Claim the pin for alerts on BOTH edges with a pull-down; the per-mode callbacks below filter
        # to the edge they want. The STEADY filter maps to lgpio's debounce; set it now (0 clears it).
        lgpio.gpio_claim_alert(self._chip, self.gpio, lgpio.BOTH_EDGES, lgpio.SET_PULL_DOWN)
        lgpio.gpio_set_debounce_micros(self._chip, self.gpio, self._glitch_us)
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
        lgpio = self._lgpio
        if self._cb:                     # cancel a prior callback so a re-start can't double-deliver
            self._cb.cancel()
            self._cb = None
        self._callback = callback
        self._active = True
        self._pulse_idx = 0
        self._ref_tick = None
        self._last_accept_tick = None
        # detected stream uses the configured STEADY (glitch) filter
        lgpio.gpio_set_debounce_micros(self._chip, self.gpio, self._glitch_us)
        self._cb = lgpio.callback(self._chip, self.gpio, lgpio.RISING_EDGE, self._on_edge)

    def stop_stream(self):
        self._active = False
        if self._cb:
            self._cb.cancel()
            self._cb = None

    def start_raw_capture(self, callback):
        """Diagnostic (setup-UI scope): capture EVERY transition (both edges) at hardware-tick
        resolution with the glitch filter DISABLED, so the browser sees the true digital waveform and
        applies steady/hold-off in software. callback({'t': unix_seconds, 'level': 0|1})."""
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
        """Stop diagnostic capture and restore the configured glitch (debounce) filter."""
        self._raw_active = False
        if self._raw_cb:
            self._raw_cb.cancel()
            self._raw_cb = None
        try:
            self._lgpio.gpio_set_debounce_micros(self._chip, self.gpio, getattr(self, "_glitch_us", 0))
        except Exception:
            pass

    def reset_trial(self):
        """Reset pulse counter at trial start."""
        self._pulse_idx = 0
        self._ref_tick = None
        self._ref_time = None
        self._last_accept_tick = None
        self._trial_pulses = []

    def _on_edge(self, chip, gpio, level, tick):
        if not self._active:
            return
        if level == 2:      # lgpio watchdog/timeout, not a real edge
            return
        # HOLD-OFF debounce: after an accepted rising edge, ignore edges for _debounce_us. Uses the
        # lgpio hardware tick (nanoseconds, uint64 — no wrap), NOT a fresh time.time() call. First edge
        # (_last_accept_tick None) always passes; 0 disables. NB lgpio ticks are CLOCK_REALTIME-based,
        # so a rare NTP step could momentarily perturb the hold-off (pigpio's monotonic µs tick did not).
        if self._debounce_us > 0 and self._last_accept_tick is not None:
            if (tick - self._last_accept_tick) < self._debounce_us * 1000:
                return
        self._last_accept_tick = tick
        wall_t = time.time()
        if self._ref_tick is None:
            self._ref_tick = tick
            self._ref_time = wall_t
        # anchor the precise edge time to the first edge's wall clock + hardware-tick delta (ns -> s)
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
