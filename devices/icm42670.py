"""
devices/icm42670.py

Register map + scale constants for the TDK InvenSense ICM-42670-P 6-axis IMU,
shared by devices/gyroscope_nano.py (Nano reads it over its own I2C, streams CSV)
and devices/gyroscope_i2c.py (the Pi polls it directly on i2c-1).

Values cross-checked against the ICM-42670-P datasheet; the arduino sketch
(arduino/gyroscope_nano/gyroscope_nano.ino) uses the same ones.
"""

I2C_ADDR = 0x68

WHO_AM_I = 0x75          # reads 0x67 on an ICM-42670-P
WHO_AM_I_VALUE = 0x67

PWR_MGMT0 = 0x1F
PWR_MGMT0_LN = 0x0F      # gyro + accel in low-noise mode

# Full-scale + output-data-rate config. FS bits [6:5] = 00 -> widest range
# (gyro ±2000 dps, accel ±16 g); ODR bits [3:0] = 0b1001 -> 100 Hz.
GYRO_CONFIG0 = 0x20
ACCEL_CONFIG0 = 0x21
CONFIG0_100HZ = 0x09     # FS=00, ODR=100 Hz

# Data registers: 12-byte big-endian burst AX..GZ (int16 x 6)
ACCEL_DATA_X1 = 0x0B     # burst base; accel 0x0B-0x10, gyro 0x11-0x16

# Raw-count scales at the FS settings above (recorded into the HDF5 alongside the
# raw int16 samples, so analysis can convert without guessing the config)
ACCEL_LSB_PER_G = 2048.0     # ±16 g
GYRO_LSB_PER_DPS = 16.4      # ±2000 dps


def be16(hi, lo):
    """Big-endian byte pair -> signed int16."""
    v = ((hi & 0xFF) << 8) | (lo & 0xFF)
    return v - 65536 if v >= 32768 else v


# ── Shared Device base for the two gyro variants ──

import threading
import time
from array import array

from devices.base import Device


class ImuBase(Device):
    """Buffers, publish throttle, and HDF5 output shared by gyroscope_nano and
    gyroscope_i2c. Subclasses implement _stream_loop() (the acquisition thread
    body: loop while self._stream_active, calling _append_sample per reading)."""

    def _init_buffers(self, sample_hz):
        self.sample_hz = float(sample_hz)
        self._buf_lock = threading.Lock()
        self._t = array("d")
        self._raw = [array("h") for _ in range(6)]   # ax ay az gx gy gz
        self._stream_active = False
        self._thread = None
        self._cb = None
        # Full rate into the buffers; callbacks throttled to ~15 Hz so a 100+ Hz
        # IMU can't flood UDP/SSE.
        self._pub_period = 1.0 / 15
        self._last_pub = 0.0

    def _append_sample(self, t, ax, ay, az, gx, gy, gz):
        with self._buf_lock:
            self._t.append(t)
            for arr, v in zip(self._raw, (ax, ay, az, gx, gy, gz)):
                arr.append(max(-32768, min(32767, int(v))))
        cb = self._cb
        if cb is not None and (t - self._last_pub) >= self._pub_period:
            self._last_pub = t
            mag = (ax * ax + ay * ay + az * az) ** 0.5 / ACCEL_LSB_PER_G
            try:
                cb({"event": "imu", "t": t,
                    "ax": int(ax), "ay": int(ay), "az": int(az),
                    "gx": int(gx), "gy": int(gy), "gz": int(gz),
                    "mag": round(mag, 3)})
            except Exception:
                pass

    def start_stream(self, callback):
        if self._stream_active:
            self._cb = callback
            return
        self._cb = callback
        self._stream_active = True
        self._thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._thread.start()

    def stop_stream(self):
        self._stream_active = False
        if self._thread is not None:
            self._thread.join(timeout=1.5)   # quiesce before buffers are snapshotted
            self._thread = None
        self._cb = None

    def _stream_loop(self):
        raise NotImplementedError

    def hdf5_session_data(self) -> dict:
        import numpy as np
        with self._buf_lock:
            n = len(self._t)
            t = np.asarray(self._t[:n], dtype=np.float64)
            cols = [np.asarray(a[:n], dtype=np.int16) for a in self._raw]
        return {
            "imu_t": t,
            "imu_accel_raw": np.stack(cols[0:3], axis=1) if n else np.zeros((0, 3), np.int16),
            "imu_gyro_raw": np.stack(cols[3:6], axis=1) if n else np.zeros((0, 3), np.int16),
            "imu_accel_lsb_per_g": np.array([ACCEL_LSB_PER_G]),
            "imu_gyro_lsb_per_dps": np.array([GYRO_LSB_PER_DPS]),
        }

    def reset_trial(self):
        pass   # continuous-only device; nothing per trial
