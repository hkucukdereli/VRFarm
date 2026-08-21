"""
devices/gyroscope_i2c.py

Gyroscope (I2C) — the ICM-42670-P 6-axis IMU wired DIRECTLY to the Pi's i2c-1
(no Arduino in the loop), polled from Python via smbus2 at sample_hz.

Only ONE of gyroscope_nano / gyroscope_i2c is physically wired at a time —
enable the one that matches the wiring in the rig yaml. init() verifies
WHO_AM_I so wrong wiring fails loudly at Initialize.

Wiring: SDA->GPIO2, SCL->GPIO3, VCC->3V3 (the ICM-42670 is NOT 5 V tolerant), GND.
"""

from __future__ import annotations
import time

from devices.base import DeviceInfo, IOType, register_device
from devices.icm42670 import (
    ACCEL_CONFIG0, ACCEL_DATA_X1, CONFIG0_100HZ, GYRO_CONFIG0, I2C_ADDR,
    ImuBase, PWR_MGMT0, PWR_MGMT0_LN, WHO_AM_I, WHO_AM_I_VALUE, be16,
)


@register_device
class GyroscopeI2c(ImuBase):
    info = DeviceInfo(
        name="gyroscope_i2c",
        label="Gyroscope (I2C)",
        io_type=IOType.I2C,
        required_packages=["smbus2"],
    )

    def init(self, rig_config: dict, task_params: dict):
        cfg = dict(rig_config or {})
        cfg.update(task_params or {})
        self.addr = int(str(cfg.get("i2c_address", hex(I2C_ADDR))), 16)
        self._init_buffers(cfg.get("sample_hz", 100))
        self._fail_count = 0

        import smbus2   # lazy: keeps the module importable on the controller
        self.bus = smbus2.SMBus(int(cfg.get("i2c_bus", 1)))
        who = self.bus.read_byte_data(self.addr, WHO_AM_I)
        if who != WHO_AM_I_VALUE:
            self.bus.close()
            self.bus = None
            raise RuntimeError(
                f"WHO_AM_I=0x{who:02X} at 0x{self.addr:02X} (expected "
                f"0x{WHO_AM_I_VALUE:02X}) — is the ICM-42670 wired to i2c-1?")
        # Low-noise mode, ±16 g / ±2000 dps @ 100 Hz ODR, then let it spin up.
        self.bus.write_byte_data(self.addr, PWR_MGMT0, PWR_MGMT0_LN)
        self.bus.write_byte_data(self.addr, GYRO_CONFIG0, CONFIG0_100HZ)
        self.bus.write_byte_data(self.addr, ACCEL_CONFIG0, CONFIG0_100HZ)
        time.sleep(0.05)
        self._last_vals = self._read_sample()

    def _read_sample(self):
        data = self.bus.read_i2c_block_data(self.addr, ACCEL_DATA_X1, 12)
        return tuple(be16(data[i], data[i + 1]) for i in range(0, 12, 2))

    def check(self) -> dict:
        if getattr(self, "bus", None) is None:
            return {"ok": False, "message": "bus not open"}
        try:
            v = self._read_sample()
        except Exception as e:
            return {"ok": False, "message": f"read failed: {e}"}
        return {"ok": True,
                "message": f"ICM-42670 @ 0x{self.addr:02X} (ax={v[0]} ay={v[1]} az={v[2]})"}

    def _stream_loop(self):
        period = 1.0 / max(1.0, self.sample_hz)
        while self._stream_active:
            t0 = time.time()
            try:
                vals = self._read_sample()
            except Exception:
                self._fail_count += 1
                if self._fail_count == 30:
                    print(f"[gyroscope_i2c] i2c read failing at 0x{self.addr:02X}", flush=True)
                time.sleep(period)
                continue
            self._fail_count = 0
            self._last_vals = vals
            self._append_sample(t0, *vals)
            slack = period - (time.time() - t0)
            if slack > 0:
                time.sleep(slack)

    def close(self):
        self.stop_stream()
        b = getattr(self, "bus", None)
        if b is not None:
            try:
                b.close()
            except Exception:
                pass
            self.bus = None

    @classmethod
    def task_params_schema(cls) -> dict:
        return {
            "sample_hz": {"type": "int", "default": 100, "label": "Sample rate (Hz)"},
        }
