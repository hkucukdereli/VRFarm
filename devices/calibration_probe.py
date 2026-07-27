"""
devices/calibration_probe.py

Calibration Probe. A single GPIO output that LATCHES high/low (not a pulse).
Used as a TTL marker during geometry calibration: driven HIGH when a calibration
session starts and LOW when it stops, and toggleable by hand from the setup UI.
A persistent pigpio handle keeps the line latched between HTTP requests.
"""

from __future__ import annotations

from .base import Device, DeviceInfo, IOType, register_device


@register_device
class CalibrationProbe(Device):
    info = DeviceInfo("calibration_probe", "Calibration Probe",
                      IOType.GPIO_OUT, ["pigpio"])

    def init(self, rig_config: dict, task_params: dict):
        import pigpio
        self.gpio = rig_config.get("gpio", 22)
        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpiod not running — sudo pigpiod")
        self._pi.set_mode(self.gpio, pigpio.OUTPUT)
        self._pi.write(self.gpio, 0)   # start LOW / OFF
        self._high = False

    def set_state(self, high: bool):
        """Latch the pin HIGH or LOW — it holds until changed."""
        self._pi.write(self.gpio, 1 if high else 0)
        self._high = bool(high)

    @property
    def is_high(self) -> bool:
        return self._high

    def check(self) -> dict:
        if not self._pi.connected:
            return {"ok": False, "message": "pigpiod not connected"}
        level = self._pi.read(self.gpio)
        return {"ok": True, "message": f"GPIO{self.gpio} level={level}"}

    def close(self):
        try:
            self._pi.write(self.gpio, 0)
        finally:
            self._pi.stop()
