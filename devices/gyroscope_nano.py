"""
devices/gyroscope_nano.py

Gyroscope (Nano) — the ICM-42670-P 6-axis IMU read by an Arduino Nano over the
Nano's own I2C, streamed to the Pi as CSV over USB serial (FT232, /dev/ttyUSB0):

    ax,ay,az,gx,gy,gz\\n      raw int16, ~sample_hz lines/s @ 115200 baud

Flash arduino/gyroscope_nano/gyroscope_nano.ino to the Nano. Only ONE of
gyroscope_nano / gyroscope_i2c is physically wired at a time — enable the one
that matches the wiring in the rig yaml.

Serial IS the device here, so init() raises loudly when the port can't be opened
or no parseable CSV arrives (dead sketch / wrong port) — a bad wire fails at
Initialize, not silently at GO. Opening the port toggles DTR and auto-resets the
Nano, so init discards output until the first valid line (up to ~4 s).
"""

from __future__ import annotations
import time

from devices.base import DeviceInfo, IOType, register_device
from devices.icm42670 import ImuBase


def _parse_csv(line):
    """b'ax,ay,az,gx,gy,gz' -> 6 ints, or None for banners/garbage."""
    try:
        parts = line.decode(errors="replace").strip().split(",")
        if len(parts) != 6:
            return None
        return tuple(int(float(p)) for p in parts)
    except (ValueError, UnicodeDecodeError):
        return None


@register_device
class GyroscopeNano(ImuBase):
    info = DeviceInfo(
        name="gyroscope_nano",
        label="Gyroscope (Nano)",
        io_type=IOType.USB,
        required_packages=["pyserial"],
    )

    def init(self, rig_config: dict, task_params: dict):
        cfg = dict(rig_config or {})
        cfg.update(task_params or {})
        self.port = cfg.get("serial_port", "/dev/ttyUSB0")
        self.baud = int(cfg.get("baud", 115200))
        self._init_buffers(cfg.get("sample_hz", 100))
        self._fail_count = 0

        import serial   # pyserial
        self._serial = serial.Serial(self.port, self.baud, timeout=1.0)
        # DTR toggled on open -> the Nano resets and reboots its sketch. Discard
        # everything until the first parseable CSV line; no line = dead sketch.
        deadline = time.time() + 4.0
        self._last_vals = None
        while time.time() < deadline:
            vals = _parse_csv(self._serial.readline())
            if vals is not None:
                self._last_vals = vals
                break
        if self._last_vals is None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
            raise RuntimeError(
                f"no CSV from the Nano on {self.port} @ {self.baud} — "
                f"is gyroscope_nano.ino flashed and the IMU wired?")

    def check(self) -> dict:
        if getattr(self, "_serial", None) is None:
            return {"ok": False, "message": "serial not open"}
        v = self._last_vals or (0,) * 6
        return {"ok": True,
                "message": f"CSV on {self.port} (ax={v[0]} ay={v[1]} az={v[2]})"}

    def _stream_loop(self):
        try:
            self._serial.reset_input_buffer()   # drop the backlog since init
        except Exception:
            pass
        while self._stream_active:
            try:
                line = self._serial.readline()
            except Exception:
                self._fail_count += 1
                if self._fail_count == 30:
                    print(f"[gyroscope_nano] serial read failing on {self.port}", flush=True)
                time.sleep(0.1)
                continue
            vals = _parse_csv(line)
            if vals is None:
                continue
            self._fail_count = 0
            self._last_vals = vals
            self._append_sample(time.time(), *vals)

    def close(self):
        self.stop_stream()
        s = getattr(self, "_serial", None)
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
            self._serial = None

    @classmethod
    def task_params_schema(cls) -> dict:
        return {
            "sample_hz": {"type": "int", "default": 100, "label": "Sample rate (Hz)"},
        }
