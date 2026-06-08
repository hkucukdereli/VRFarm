"""
devices/lick_sensor.py

MPR121 capacitive touch sensor for lick detection.
I/O: I2C (smbus2). 200Hz polling, rising-edge detection.
"""

from __future__ import annotations
import threading
import time

from .base import Device, DeviceInfo, IOType, register_device


@register_device
class LickSensor(Device):
    info = DeviceInfo("lick_sensor", "Lick Sensor", IOType.I2C, ["smbus2"])

    @classmethod
    def task_params_schema(cls):
        return {
            "max_lick_rate": {
                "type": "float", "default": 0.3,
                "label": "Max lick rate (Hz)", "min": 0.1, "max": 5.0,
            },
        }

    def init(self, rig_config: dict, task_params: dict):
        import smbus2
        addr_raw = rig_config.get("i2c_address", "0x5A")
        self.addr = int(addr_raw, 16) if isinstance(addr_raw, str) else addr_raw
        self.electrode = rig_config.get("electrode", 4)
        self.max_lick_rate = task_params.get("max_lick_rate", 0.3)
        self.bus = smbus2.SMBus(rig_config.get("i2c_bus", 1))
        self._init_registers()
        self._stream_active = False
        self._stream_thread = None
        self._was_touched = False

    def _init_registers(self):
        a = self.addr
        self.bus.write_byte_data(a, 0x80, 0x63)
        time.sleep(0.02)
        for i in range(12):
            self.bus.write_byte_data(a, 0x41 + i * 2, 12)
            self.bus.write_byte_data(a, 0x42 + i * 2,  6)
        self.bus.write_byte_data(a, 0x5C, 0x10)
        self.bus.write_byte_data(a, 0x5D, 0x20)
        self.bus.write_byte_data(a, 0x5E, 0x8F)

    def is_touched(self) -> bool:
        lo = self.bus.read_byte_data(self.addr, 0x00)
        hi = self.bus.read_byte_data(self.addr, 0x01)
        return bool(((hi << 8) | lo) & (1 << self.electrode))

    def check(self) -> dict:
        try:
            touched = self.is_touched()
            return {"ok": True, "message": f"electrode {self.electrode} touched={touched}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def start_stream(self, callback):
        self._stream_active = True
        self._was_touched = False

        def poll():
            while self._stream_active:
                try:
                    touched = self.is_touched()
                    if touched and not self._was_touched:
                        callback({"event": "lick", "t": time.time()})
                    self._was_touched = touched
                except Exception:
                    pass
                time.sleep(0.005)  # 200 Hz

        self._stream_thread = threading.Thread(target=poll, daemon=True)
        self._stream_thread.start()

    def stop_stream(self):
        self._stream_active = False

    def hdf5_datasets(self) -> dict:
        import numpy as np
        import h5py
        return {
            "lick_times": {"dtype": h5py.vlen_dtype(np.float64)},
            "iti_lick_times": {"dtype": h5py.vlen_dtype(np.float64)},
            "iti_lick_count": "i4",
        }

    def hdf5_trial_data(self, ctx: dict) -> dict:
        return {
            "lick_times": ctx.get("lick_times", []),
            "iti_lick_times": ctx.get("iti_lick_times", []),
            "iti_lick_count": ctx.get("iti_lick_count", 0),
        }

    def close(self):
        self.stop_stream()
        self.bus.close()
