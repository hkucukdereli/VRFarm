"""
devices/imu_base.py

Chip-neutral plumbing for 6-axis IMU devices (devices/lsm6dsv16x.py): sample buffers, the
~15 Hz publish throttle, and the HDF5 output — raw int16 counts plus the scale factors the
chip was configured with, so analysis converts without guessing the configuration.

A subclass calls _init_buffers() in init(), sets accel_lsb_per_g / gyro_lsb_per_dps /
odr_hz, and implements _stream_loop(): loop while self._stream_active, calling
_append_sample(t, ax, ay, az, gx, gy, gz) once per new reading.

Events: {"event": "imu", t, ax..gz (raw counts), mag (|a| in g), missed}. The Experiment
tab's IMU strip and the Setup card's |a| trace (Device.monitor_view = "imu") draw them.
HDF5 (session level; shared/consolidate.py groups it under /imu): imu_t (Pi time of each
read), imu_accel_raw / imu_gyro_raw (N x 3 int16), imu_accel_lsb_per_g,
imu_gyro_lsb_per_dps, imu_odr_hz, imu_missed.
"""

from __future__ import annotations

import threading
from array import array

from devices.base import Device


class ImuBase(Device):
    monitor_view = "imu"

    def _init_buffers(self):
        self._buf_lock = threading.Lock()
        self._t = array("d")
        self._raw = [array("h") for _ in range(6)]   # ax ay az gx gy gz
        self._stream_active = False
        self._thread = None
        self._cb = None
        # Full rate into the buffers; callbacks throttled to ~15 Hz so a 100+ Hz IMU can't
        # flood UDP/SSE.
        self._pub_period = 1.0 / 15
        self._last_pub = 0.0
        self._last_t = None
        self.missed = 0
        # the subclass sets the real values for its configuration
        self.accel_lsb_per_g = 1.0
        self.gyro_lsb_per_dps = 1.0
        self.odr_hz = 0.0

    def _append_sample(self, t, ax, ay, az, gx, gy, gz):
        # Estimate samples the poll loop missed: a gap longer than 1.75 sample periods
        # (polling jitter alone stays under 1.5).
        if self._last_t is not None and self.odr_hz > 0:
            n = int((t - self._last_t) * self.odr_hz + 0.25) - 1
            if n > 0:
                self.missed += n
        self._last_t = t
        with self._buf_lock:
            self._t.append(t)
            for arr, v in zip(self._raw, (ax, ay, az, gx, gy, gz)):
                arr.append(max(-32768, min(32767, int(v))))
        cb = self._cb
        if cb is not None and (t - self._last_pub) >= self._pub_period:
            self._last_pub = t
            mag = (ax * ax + ay * ay + az * az) ** 0.5 / self.accel_lsb_per_g
            try:
                cb({"event": "imu", "t": t,
                    "ax": int(ax), "ay": int(ay), "az": int(az),
                    "gx": int(gx), "gy": int(gy), "gz": int(gz),
                    "mag": round(mag, 3), "missed": self.missed})
            except Exception:
                pass

    def start_stream(self, callback):
        self._cb = callback
        if self._stream_active:
            return
        self._stream_active = True
        self._thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._thread.start()

    def stop_stream(self):
        self._stream_active = False
        if self._thread is not None:
            self._thread.join(timeout=1.5)   # quiesce before the buffers are snapshotted
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
            "imu_accel_lsb_per_g": np.array([self.accel_lsb_per_g]),
            "imu_gyro_lsb_per_dps": np.array([self.gyro_lsb_per_dps]),
            "imu_odr_hz": np.array([self.odr_hz]),
            "imu_missed": np.array([self.missed], dtype=np.int64),
        }

    def reset_trial(self):
        pass   # continuous-only device; nothing per trial
