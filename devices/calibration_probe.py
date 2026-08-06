"""
devices/calibration_probe.py

Calibration Probe. A single GPIO output that LATCHES high/low (not a pulse).
Used as a TTL marker during geometry calibration: driven HIGH when a calibration
session starts and LOW when it stops, and toggleable by hand from the setup UI.
GPIO output via lgpio (Pi 5 / Pi 4, no daemon); the gpiochip handle keeps the line
latched between HTTP requests.
"""

from __future__ import annotations

from .base import Device, DeviceInfo, IOType, register_device


@register_device
class CalibrationProbe(Device):
    info = DeviceInfo("calibration_probe", "Calibration Probe",
                      IOType.GPIO_OUT, ["lgpio"])

    def init(self, rig_config: dict, task_params: dict):
        import lgpio
        self._lgpio = lgpio
        self.gpio = rig_config.get("gpio", 22)
        # Header gpiochip: gpiochip0 on current Pi 5 / Pi 4; rig 'gpiochip' overrides (early Pi 5 = 4).
        self._chipnum = int(rig_config.get("gpiochip", 0))
        try:
            self._chip = lgpio.gpiochip_open(self._chipnum)
        except Exception as e:
            raise RuntimeError(f"lgpio: cannot open gpiochip{self._chipnum} ({e})")
        lgpio.gpio_claim_output(self._chip, self.gpio, 0)   # start LOW / OFF
        self._high = False

    def set_state(self, high: bool):
        """Latch the pin HIGH or LOW — it holds until changed."""
        self._lgpio.gpio_write(self._chip, self.gpio, 1 if high else 0)
        self._high = bool(high)

    @property
    def is_high(self) -> bool:
        return self._high

    def check(self) -> dict:
        try:
            level = self._lgpio.gpio_read(self._chip, self.gpio)
            return {"ok": True, "message": f"GPIO{self.gpio} level={level}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def close(self):
        try:
            self._lgpio.gpio_write(self._chip, self.gpio, 0)
        finally:
            try:
                self._lgpio.gpiochip_close(self._chip)
            except Exception:
                pass
