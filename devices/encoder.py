"""
devices/encoder.py

Running-wheel encoder via an Adafruit AS5600 magnetic angle sensor (I2C, addr 0x36).
A diametric magnet on the wheel axle spins over the chip; we poll the 12-bit absolute
angle, UNWRAP it over time into cumulative rotation, and derive running distance (cm)
and speed (cm/s). The full running trace is saved to the session HDF5.

I/O: I2C (smbus2). Polls RAW ANGLE (reg 0x0C/0x0D); STATUS (0x0B) reports magnet presence.
"""

from __future__ import annotations
import math
import threading
import time
from array import array

from .base import Device, DeviceInfo, IOType, register_device

# AS5600 registers
_REG_RAW_ANGLE = 0x0C   # 2 bytes, 12-bit (0..4095) = 0..360 deg
_REG_STATUS = 0x0B      # bit5 MD=magnet detected, bit4 ML=too strong, bit3 MH=too weak
_COUNTS_PER_REV = 4096


@register_device
class Encoder(Device):
    info = DeviceInfo("encoder", "Running Wheel", IOType.I2C, ["smbus2"])

    def init(self, rig_config: dict, task_params: dict):
        import smbus2
        addr_raw = rig_config.get("i2c_address", "0x36")
        self.addr = int(addr_raw, 16) if isinstance(addr_raw, str) else addr_raw
        self.i2c_bus = rig_config.get("i2c_bus", 1)
        self.wheel_diameter_cm = float(rig_config.get("wheel_diameter_cm", 15.0))
        self.circ_cm = math.pi * self.wheel_diameter_cm     # wheel circumference (1 rev = circ_cm)
        self.sample_hz = int(rig_config.get("sample_hz", 100))   # high enough that <½ turn/sample
        self.bus = smbus2.SMBus(self.i2c_bus)
        self._pub_period = 1.0 / 15.0        # throttle live events to ~15 Hz (full trace still saved)
        self._stream_active = False
        self._stream_thread = None
        # running state
        self._angle_prev = 0
        self._cum_counts = 0
        self._last_t = time.time()
        self._last_dist_cm = 0.0
        self._speed_ema = 0.0
        self._trial_dist0 = 0.0
        # session trace buffers (converted to arrays in hdf5_session_data)
        self._buf_t = array('d')
        self._buf_dist = array('f')
        self._buf_speed = array('f')
        self._buf_counts = array('q')   # cumulative signed counts (raw ground truth; rotations = counts/4096)

    def _read_raw_angle(self) -> int:
        data = self.bus.read_i2c_block_data(self.addr, _REG_RAW_ANGLE, 2)
        return ((data[0] & 0x0F) << 8) | data[1]     # 12-bit, 0..4095

    def check(self) -> dict:
        try:
            status = self.bus.read_byte_data(self.addr, _REG_STATUS)
            md = bool(status & 0x20)     # magnet detected
            too_strong = bool(status & 0x10)
            too_weak = bool(status & 0x08)
            angle = self._read_raw_angle() * 360.0 / _COUNTS_PER_REV
            msg = "magnet OK" if md else "magnet NOT detected"
            if too_strong:
                msg += " (too close)"
            if too_weak:
                msg += " (too far)"
            msg += f", angle={angle:.1f}deg"
            return {"ok": md, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def start_stream(self, callback):
        """Poll the AS5600 at sample_hz, unwrap the angle into cumulative rotation, and derive
        distance (cm) + speed (cm/s). Emits throttled {"event":"running", ...} for the live UIs and
        accumulates the full trace for hdf5_session_data()."""
        self._stream_active = True
        try:
            self._angle_prev = self._read_raw_angle()
        except Exception:
            self._angle_prev = 0
        self._cum_counts = 0
        self._last_t = time.time()
        self._last_dist_cm = 0.0
        self._speed_ema = 0.0
        self._trial_dist0 = 0.0
        self._buf_t = array('d')
        self._buf_dist = array('f')
        self._buf_speed = array('f')
        self._buf_counts = array('q')

        def poll():
            period = 1.0 / max(1, self.sample_hz)
            last_pub = 0.0
            fails = 0
            while self._stream_active:
                t0 = time.time()
                try:
                    raw = self._read_raw_angle()
                    fails = 0
                    delta = raw - self._angle_prev
                    if delta > _COUNTS_PER_REV // 2:          # wrapped 4095->0
                        delta -= _COUNTS_PER_REV
                    elif delta < -_COUNTS_PER_REV // 2:        # wrapped 0->4095
                        delta += _COUNTS_PER_REV
                    self._angle_prev = raw
                    self._cum_counts += delta
                    now = time.time()
                    dt = now - self._last_t
                    dist_cm = self._cum_counts / _COUNTS_PER_REV * self.circ_cm
                    inst = (dist_cm - self._last_dist_cm) / dt if dt > 0 else 0.0
                    self._speed_ema += 0.2 * (inst - self._speed_ema)   # light smoothing for display
                    self._last_t = now
                    self._last_dist_cm = dist_cm
                    self._buf_t.append(now)
                    self._buf_dist.append(dist_cm)
                    self._buf_speed.append(self._speed_ema)
                    self._buf_counts.append(self._cum_counts)
                    if now - last_pub >= self._pub_period:
                        last_pub = now
                        callback({"event": "running", "t": now,
                                  "speed_cms": self._speed_ema, "distance_cm": dist_cm})
                except Exception:
                    fails += 1
                    if fails == 30:   # ~0.3s of dead reads — surface it (magnet lost / cable / wrong addr)
                        print("[encoder] I2C reads failing — running trace paused "
                              "(check magnet/wiring/address)", flush=True)
                slack = period - (time.time() - t0)
                if slack > 0:
                    time.sleep(slack)

        self._stream_thread = threading.Thread(target=poll, daemon=True)
        self._stream_thread.start()

    def stop_stream(self):
        self._stream_active = False
        t = self._stream_thread
        self._stream_thread = None
        if t is not None:
            t.join(timeout=1.0)   # ensure the poll thread stopped before hdf5_session_data reads buffers

    def reset_trial(self):
        """Mark the distance baseline at trial start (for the per-trial distance-run slice)."""
        self._trial_dist0 = self._last_dist_cm

    def hdf5_datasets(self) -> dict:
        return {"trial_run_distance_cm": "f4"}

    def hdf5_trial_data(self, ctx: dict) -> dict:
        return {"trial_run_distance_cm": float(self._last_dist_cm - self._trial_dist0)}

    def hdf5_session_data(self) -> dict:
        """Full running trace (whole session): timestamps, cumulative distance (cm), speed (cm/s),
        and raw cumulative encoder counts. running_counts is the ground truth (4096 counts/rev) —
        rotations = counts/4096, distance = rotations*circ, speed = d(distance)/dt — so speed and
        distance can be recomputed offline (even with a corrected wheel diameter)."""
        import numpy as np
        n = min(len(self._buf_t), len(self._buf_dist),
                len(self._buf_speed), len(self._buf_counts))   # keep all channels aligned
        return {
            "running_t": np.asarray(self._buf_t[:n], dtype=np.float64),
            "running_distance_cm": np.asarray(self._buf_dist[:n], dtype=np.float32),
            "running_speed_cms": np.asarray(self._buf_speed[:n], dtype=np.float32),
            "running_counts": np.asarray(self._buf_counts[:n], dtype=np.int64),   # cumulative; rotations = /4096
        }

    def close(self):
        self.stop_stream()
        try:
            self.bus.close()
        except Exception:
            pass
