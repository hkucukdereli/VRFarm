"""
devices/lsm6dsv16x.py

6-axis IMU: ST LSM6DSV16X on the SparkFun "6DoF IMU Breakout - LSM6DSV16X (Qwiic)", read by
the Pi directly over I2C (smbus2) — no microcontroller in the loop.

Wiring: Qwiic cable to the leader's i2c-1. On the Seeed Grove Base Hat: a Grove-to-Qwiic cable
into one of its three I2C ports (match colours: yellow SCL, red 3V3, black GND, Grove white =
Qwiic blue = SDA); plug with the Pi OFF. 3.3 V only. Address 0x6B (SparkFun default; their ADR
jumper selects 0x6A). Shares the bus with the IR LED board (0x60) and the hat's MCU (0x04/0x08).
init() checks WHO_AM_I (0x70), so wrong wiring or a wrong address fails at Initialize.

Acquisition: accelerometer + gyroscope in high-performance mode at odr_hz (15, 30, 60, 120 or
240 Hz, both sensors; default 120), block data update on. A thread polls STATUS_REG at twice
the ODR and reads the 12 output bytes whenever a new accelerometer sample is ready, so each
sample is read once; imu_t is the Pi's time.time() at that read (at most half a sample period
after the chip took it). Samples the loop misses (CPU/GIL stalls) show as gaps in imu_t and in
the `missed` estimate. Above 120 Hz the default 100 kHz I2C bus gets busy — 240 Hz works but
leaves less room for other devices on the bus.

Full scale: accel_fs_g 2/4/8/16 (default 16), gyro_fs_dps 125/250/500/1000/2000/4000
(default 2000). The matching sensitivities (datasheet DS13510, typical) are recorded next to
the raw counts. Axes are the ones printed on the SparkFun board.
"""

from __future__ import annotations

import struct
import threading
import time

from devices.base import DeviceInfo, IOType, register_device
from devices.imu_base import ImuBase

I2C_ADDR = 0x6B

# Register map (LSM6DSV16X datasheet DS13510)
WHO_AM_I = 0x0F
WHO_AM_I_VALUE = 0x70
CTRL1 = 0x10        # accel: OP_MODE_XL[6:4] (000 = high-performance), ODR_XL[3:0]
CTRL2 = 0x11        # gyro:  OP_MODE_G[6:4]  (000 = high-performance), ODR_G[3:0]
CTRL3 = 0x12        # BOOT[7] BDU[6] IF_INC[2] SW_RESET[0]; reset value 0x44
CTRL3_BDU_IF_INC = 0x44
CTRL3_SW_RESET = 0x01
CTRL6 = 0x15        # gyro:  LPF1_G_BW[6:4], FS_G[3:0]
CTRL8 = 0x17        # accel: HP_LPF2_XL_BW[7:5], XL_DualC_EN[3], FS_XL[1:0]
STATUS_REG = 0x1E   # TDA[2] GDA[1] XLDA[0]
XLDA = 0x01
OUT_TEMP_L = 0x20   # int16, 256 LSB/degC, 0 = 25 degC
OUTX_L_G = 0x22     # gyro X/Y/Z (0x22-0x27) then accel X/Y/Z (0x28-0x2D), little-endian int16

ODR_CODE = {15: 0x3, 30: 0x4, 60: 0x5, 120: 0x6, 240: 0x7}
# full scale -> (register code, sensitivity): accel mg/LSB, gyro mdps/LSB
FS_XL = {2: (0x0, 0.061), 4: (0x1, 0.122), 8: (0x2, 0.244), 16: (0x3, 0.488)}
FS_G = {125: (0x0, 4.375), 250: (0x1, 8.75), 500: (0x2, 17.5), 1000: (0x3, 35.0),
        2000: (0x4, 70.0), 4000: (0xC, 140.0)}


def _parse_addr(raw) -> int:
    return int(raw, 0) if isinstance(raw, str) else int(raw)


def _choice(cfg: dict, key: str, default: int, table: dict) -> int:
    try:
        v = int(float(cfg.get(key, default)))
    except (TypeError, ValueError):
        v = None
    if v not in table:
        raise ValueError(f"lsm6dsv16x: {key} must be one of {sorted(table)} "
                         f"(got {cfg.get(key)!r})")
    return v


@register_device
class Lsm6dsv16x(ImuBase):
    info = DeviceInfo(
        name="lsm6dsv16x",
        label="IMU (LSM6DSV16X)",
        io_type=IOType.I2C,
        required_packages=["smbus2"],
    )

    def __init__(self):
        # No hardware here (Device contract): the controller constructs this to read the schema.
        self.bus = None
        self._bus_lock = threading.Lock()
        self._stream_active = False      # so close()/stop_stream() are safe before init()
        self._thread = None
        self._cb = None
        self.missed = 0

    @classmethod
    def task_params_schema(cls) -> dict:
        return {
            "odr_hz": {"type": "int", "default": 120,
                       "label": "Output data rate (Hz: 15, 30, 60, 120 or 240)"},
        }

    def init(self, rig_config: dict, task_params: dict):
        cfg = dict(rig_config or {})
        cfg.update(task_params or {})
        self.addr = _parse_addr(cfg.get("i2c_address", I2C_ADDR))
        self.i2c_bus = int(cfg.get("i2c_bus", 1))
        odr = _choice(cfg, "odr_hz", 120, ODR_CODE)
        self.accel_fs_g = _choice(cfg, "accel_fs_g", 16, FS_XL)
        self.gyro_fs_dps = _choice(cfg, "gyro_fs_dps", 2000, FS_G)
        self._init_buffers()
        self.odr_hz = float(odr)
        xl_code, mg_per_lsb = FS_XL[self.accel_fs_g]
        g_code, mdps_per_lsb = FS_G[self.gyro_fs_dps]
        self.accel_lsb_per_g = 1000.0 / mg_per_lsb
        self.gyro_lsb_per_dps = 1000.0 / mdps_per_lsb
        self._fail_count = 0

        import smbus2   # lazy: keeps the module importable on the controller
        self.bus = smbus2.SMBus(self.i2c_bus)
        try:
            who = self.bus.read_byte_data(self.addr, WHO_AM_I)
            if who != WHO_AM_I_VALUE:
                raise RuntimeError(
                    f"WHO_AM_I=0x{who:02X} at 0x{self.addr:02X} (expected 0x{WHO_AM_I_VALUE:02X}) "
                    "— is this the LSM6DSV16X? (SparkFun default 0x6B, ADR jumper -> 0x6A)")
            self._configure(ODR_CODE[odr], xl_code, g_code)
            self._last_vals = self._wait_first_sample()
        except OSError as e:
            self._close_bus()
            raise RuntimeError(f"LSM6DSV16X at 0x{self.addr:02X} on I2C-{self.i2c_bus}: {e}")
        except Exception:
            self._close_bus()
            raise

    # ── chip access ──

    def _configure(self, odr_code: int, xl_code: int, g_code: int):
        b, a = self.bus, self.addr
        b.write_byte_data(a, CTRL3, CTRL3_SW_RESET)   # control registers back to defaults
        deadline = time.time() + 0.1
        while True:
            time.sleep(0.002)
            try:
                if not b.read_byte_data(a, CTRL3) & CTRL3_SW_RESET:
                    break
            except OSError:
                pass                                  # may NACK while resetting
            if time.time() > deadline:
                raise RuntimeError("software reset did not complete")
        b.write_byte_data(a, CTRL3, CTRL3_BDU_IF_INC)   # block data update + auto-increment
        b.write_byte_data(a, CTRL8, (b.read_byte_data(a, CTRL8) & 0xFC) | xl_code)
        b.write_byte_data(a, CTRL6, (b.read_byte_data(a, CTRL6) & 0xF0) | g_code)
        b.write_byte_data(a, CTRL1, odr_code)          # high-performance mode, ODR
        b.write_byte_data(a, CTRL2, odr_code)

    def _read_sample(self):
        """(ax, ay, az, gx, gy, gz) raw counts; the chip stores gyro first."""
        d = self.bus.read_i2c_block_data(self.addr, OUTX_L_G, 12)
        gx, gy, gz, ax, ay, az = struct.unpack("<6h", bytes(d))
        return ax, ay, az, gx, gy, gz

    def _wait_first_sample(self):
        deadline = time.time() + 0.5                   # gyro start-up is ~70 ms
        while time.time() < deadline:
            if self.bus.read_byte_data(self.addr, STATUS_REG) & XLDA:
                return self._read_sample()
            time.sleep(0.005)
        raise RuntimeError("configured, but no data-ready within 0.5 s")

    def _read_temp_c(self) -> float:
        lo, hi = self.bus.read_i2c_block_data(self.addr, OUT_TEMP_L, 2)
        return 25.0 + struct.unpack("<h", bytes((lo, hi)))[0] / 256.0

    def _close_bus(self):
        b, self.bus = self.bus, None
        if b is not None:
            try:
                b.close()
            except Exception:
                pass

    # ── Device contract ──

    def check(self) -> dict:
        if self.bus is None:
            return {"ok": False, "message": "bus not open"}
        try:
            with self._bus_lock:
                ax, ay, az, gx, gy, gz = self._read_sample()
                temp = self._read_temp_c()
        except Exception as e:
            return {"ok": False, "message": f"read failed: {e}"}
        mag = (ax * ax + ay * ay + az * az) ** 0.5 / self.accel_lsb_per_g
        msg = (f"LSM6DSV16X @ 0x{self.addr:02X} on I2C-{self.i2c_bus}: {self.odr_hz:g} Hz, "
               f"±{self.accel_fs_g} g / ±{self.gyro_fs_dps} dps, |a| {mag:.2f} g, {temp:.1f} °C")
        if self.missed:
            msg += f", ~{self.missed} samples missed"
        return {"ok": True, "message": msg}

    def _stream_loop(self):
        poll = 0.5 / self.odr_hz                      # twice per sample: none read twice or skipped
        next_t = time.time()
        while self._stream_active:
            now = time.time()
            vals = None
            try:
                with self._bus_lock:
                    if self.bus.read_byte_data(self.addr, STATUS_REG) & XLDA:
                        vals = self._read_sample()
                self._fail_count = 0
            except Exception:
                self._fail_count += 1
                if self._fail_count == 30:
                    print(f"[lsm6dsv16x] I2C reads failing at 0x{self.addr:02X}", flush=True)
            if vals is not None:
                self._last_vals = vals
                self._append_sample(now, *vals)
            next_t += poll
            slack = next_t - time.time()
            if slack > 0:
                time.sleep(slack)
            else:
                next_t = time.time()                  # fell behind: resync instead of bursting

    def close(self):
        self.stop_stream()
        with self._bus_lock:
            if self.bus is not None:
                try:
                    self.bus.write_byte_data(self.addr, CTRL1, 0x00)   # power down
                    self.bus.write_byte_data(self.addr, CTRL2, 0x00)
                except Exception:
                    pass
            self._close_bus()
