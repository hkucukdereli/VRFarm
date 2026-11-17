"""
devices/ir_led.py

IR illuminator for the behaviour camera: an 850 nm LED star (ILS ILH-IS01-85SN, 1 A) driven by
the IRDriver board rev 3.2 — Mean Well LDD-1000L constant-current module + on-board MCP4725
12-bit I2C DAC (0x60). The board piggybacks on the Raspberry Pi HQ Camera.
Board handoff: docs/assets/IRDriver_HANDOFF.md (source: Dropbox/Hakan/lab/code/LED_Driver_RPi).

Wiring: ONE 4-wire Qwiic / STEMMA QT cable (JST SH, pin 1 GND black, 2 3V3 red, 3 SDA blue,
4 SCL yellow) to the leader's I2C-1: on a Seeed Grove Base Hat, a Grove-to-Qwiic cable into one
of its three I2C ports (Grove order 1 SCL yellow, 2 SDA white, 3 VCC red, 4 GND black — the cable
crosses them, so match colours, white = blue = SDA); without the hat, a Qwiic-to-female-jumper
cable on header pins 9 (GND), 1 (3V3), 3 (SDA), 5 (SCL). Plug cables with the Pi OFF (Seeed: the
hat must not be hot-plugged). The hat's own MCU sits on the same bus at 0x04 (v1.0: 0x08).
The LED supply (9-12 V recommended, 6-36 V) goes to the
board's screw terminal J1. There is NO enable GPIO any more (rev >= 2.3): the Pi's 3V3 on the
cable is the hardware gate.

Safety model (handoff §2.2, decision 17):
  * Pi off / unpowered        -> no 3V3 -> the board's Q1/Q2 stage shorts DIM -> LED OFF (hardware).
  * Pi booting                -> DAC comes up at its EEPROM value; init() programs that to 0 V,
                                 so the LED stays off. A FACTORY-FRESH MCP4725 powers up at
                                 mid-scale (1.65 V ≈ 60 % LED) — program it once on the bench
                                 before wiring the LED (see "Bench bring-up" below).
  * Pi up                     -> DAC 0 V = OFF; DAC 0.5..2.5 V = min..100 % LED current.
  * process crash / warm reboot with the LED on -> the DAC keeps its last value. This is the
                                 accepted gap of the I2C-only board; this module narrows it in
                                 software (off() on close/stop, an atexit hook, off() verified by
                                 read-back), but a SIGKILL or kernel panic leaves the LED lit
                                 until the next DAC write of 0 (any Connect does that) or a
                                 power cycle.
  The EEPROM is only ever written with 0 — never write a non-zero power-up value.

Analog dimming (no PWM: PWM at a few hundred Hz beats against the camera frame rate). Intensity
is a percentage of the driver's analog window (0.5..2.5 V ≈ min..1 A), NOT calibrated to
irradiance; 0 % is the dimmest lit level, "off" is DAC 0 V. Keep camera exposure/gain fixed and
pick an intensity per rig (rig YAML `intensity_pct`); a task YAML may override it under
`devices: <name>: intensity_pct:`. If the bench sweep shows the LDD's DIM pull-up bends the
curve, put a measured table in the rig YAML as `curve: [[pct, volts], ...]` (monotonic).

Bench bring-up (once per board, LED unplugged, LED supply off):
  i2cdetect -y 1                      # expect 60
  python3 -c "import smbus2; smbus2.SMBus(1).write_i2c_block_data(0x60, 0x60, [0, 0])"
  (or just Connect the rig: init() does the same and reports 'EEPROM 0' in the device check)

Lifecycle in VRFarm: Connect -> pi_api init (DAC 0, LED off). Setup "Live monitor" =
start_stream -> LED on at the rig intensity (aiming/focusing); stopping the monitor turns it off.
GO -> pi_api releases its instance (LED off) -> the engine inits its own -> session start
switches it on (task `on_during_session`, default true) -> session end / STOP switches it off.
"""

from __future__ import annotations

import atexit
import threading
import time
import weakref

from .base import Device, DeviceInfo, IOType, register_device

# MCP4725 (datasheet DS22039D §6): C2 C1 C0 in bits 7..5, PD1 PD0 in bits 2..1 (00 = normal).
_CMD_WRITE_DAC = 0x40          # write DAC register only (volatile)
_CMD_WRITE_DAC_EEPROM = 0x60   # write DAC register + EEPROM (power-up value)
_DAC_FULL = 4095

# Every live instance, so an interpreter exit (normal, uncaught exception, sys.exit from a
# signal handler) still drives the DAC to 0 even if nobody called close().
_LIVE: "weakref.WeakSet[IRLed]" = weakref.WeakSet()


def _all_off_at_exit():
    for dev in list(_LIVE):
        try:
            dev.off()
        except Exception:
            pass


atexit.register(_all_off_at_exit)


def _parse_addr(raw) -> int:
    return int(raw, 0) if isinstance(raw, str) else int(raw)


@register_device
class IRLed(Device):
    info = DeviceInfo("ir_led", "IR Illuminator", IOType.I2C, ["smbus2"])

    def __init__(self):
        # No hardware here (Device contract): the controller constructs this to read the schema.
        self.bus = None
        self._lock = threading.RLock()
        self._on = False
        self._code = 0
        self._on_t = None
        self._off_t = None
        self._eeprom = None

    @classmethod
    def task_params_schema(cls):
        return {
            "intensity_pct": {
                "type": "float", "default": 50.0,
                "label": "IR intensity (% of the driver's dimming range)", "min": 0.0, "max": 100.0,
            },
            "on_during_session": {
                "type": "bool", "default": True,
                "label": "IR on for the whole session",
            },
        }

    def init(self, rig_config: dict, task_params: dict):
        import smbus2
        self._smbus2 = smbus2
        self.addr = _parse_addr(rig_config.get("i2c_address", "0x60"))
        self.i2c_bus = int(rig_config.get("i2c_bus", 1))
        # Board constants (IRDriver rev 3.2): DAC reference = the Pi's 3V3; LDD-1000L analog window.
        self.vdd = float(rig_config.get("dac_vdd", 3.3))
        self.v_min = float(rig_config.get("v_min", 0.5))     # LDD analog dimming floor
        self.v_max = float(rig_config.get("v_max", 2.5))     # LDD analog dimming = 100 %
        if not (0.0 <= self.v_min < self.v_max <= self.vdd):
            raise ValueError(f"ir_led: need 0 <= v_min < v_max <= dac_vdd "
                             f"(got {self.v_min}, {self.v_max}, {self.vdd})")
        self.curve = self._parse_curve(rig_config.get("curve"))
        # intensity: task override > rig default > schema default
        self.intensity_pct = self._clamp_pct(task_params.get(
            "intensity_pct", rig_config.get("intensity_pct", 50.0)))
        self.on_during_session = bool(task_params.get(
            "on_during_session", rig_config.get("on_during_session", True)))

        self.bus = smbus2.SMBus(self.i2c_bus)
        try:
            with self._lock:
                self._ensure_eeprom_zero()
                self._write_dac(0, verify=True)    # LED off, whatever a previous run left behind
        except Exception as e:
            try:
                self.bus.close()               # the chip didn't answer: nothing to switch off
            except Exception:
                pass
            self.bus = None
            raise RuntimeError(f"MCP4725 at 0x{self.addr:02x} on I2C-{self.i2c_bus}: {e}")
        _LIVE.add(self)

    # ── MCP4725 access (callers hold self._lock) ──

    def _read_status(self) -> dict:
        """5-byte read: [status][DAC hi][DAC lo][EEPROM hi][EEPROM lo]."""
        msg = self._smbus2.i2c_msg.read(self.addr, 5)
        self.bus.i2c_rdwr(msg)
        b = list(msg)
        dac = ((b[1] << 4) | (b[2] >> 4)) & 0xFFF
        eeprom = (((b[3] & 0x0F) << 8) | b[4]) & 0xFFF
        return {"ready": bool(b[0] & 0x80), "por": bool(b[0] & 0x40),
                "pd": (b[0] >> 1) & 0x3, "dac": dac,
                "eeprom": eeprom, "eeprom_pd": (b[3] >> 5) & 0x3}

    def _write_dac(self, code: int, verify: bool = False):
        """Volatile write (power-down bits 00 = normal); takes effect on the last byte's ACK.
        verify=True reads it back and retries once — used for every write that turns the LED off."""
        code = max(0, min(_DAC_FULL, int(code)))
        hi, lo = (code >> 4) & 0xFF, (code << 4) & 0xF0
        for attempt in (0, 1):
            self.bus.write_i2c_block_data(self.addr, _CMD_WRITE_DAC, [hi, lo])
            self._code = code
            if not verify:
                return
            st = self._read_status()
            if st["dac"] == code and st["pd"] == 0:
                return
        raise RuntimeError(f"DAC read-back {st['dac']} (pd {st['pd']}) != written {code}")

    def _ensure_eeprom_zero(self):
        """Make the power-up value 0 V (idempotent). Factory default is mid-scale (D11 = 1), which
        lights the LED at ~60 % the instant the Pi's 3V3 appears — on this board 3V3 IS the gate."""
        st = self._read_status()
        if st["eeprom"] == 0 and st["eeprom_pd"] == 0:
            self._eeprom = 0
            return
        was = st["eeprom"]
        self.bus.write_i2c_block_data(self.addr, _CMD_WRITE_DAC_EEPROM, [0, 0])
        deadline = time.time() + 0.5                 # EEPROM write takes ~25-50 ms
        while True:
            time.sleep(0.01)
            try:
                st = self._read_status()
            except OSError:                          # tolerate a NACK while the cell is written
                if time.time() > deadline:
                    raise
                continue
            if st["ready"] or time.time() > deadline:
                break
        self._eeprom = st["eeprom"]
        if not st["ready"] or st["eeprom"] != 0 or st["eeprom_pd"] != 0:
            raise RuntimeError(f"EEPROM write to 0 failed (reads {st['eeprom']}, "
                               f"ready={st['ready']}) — the LED would light at boot")
        print(f"[ir_led] MCP4725 EEPROM power-up value set to 0 (was {was})", flush=True)

    # ── intensity mapping ──

    @staticmethod
    def _clamp_pct(pct) -> float:
        return max(0.0, min(100.0, float(pct)))

    def _parse_curve(self, raw):
        """Optional measured [[pct, volts], ...] (bench sweep), sorted by pct, volts monotonic
        and inside the LDD window. None = the linear 0.5..2.5 V map."""
        if not raw:
            return None
        pts = sorted((float(p), float(v)) for p, v in raw)
        if len(pts) < 2 or any(b[1] < a[1] for a, b in zip(pts, pts[1:])):
            raise ValueError("ir_led: curve needs >= 2 [pct, volts] points with volts non-decreasing")
        if pts[0][1] < self.v_min - 1e-6 or pts[-1][1] > self.vdd:
            raise ValueError(f"ir_led: curve volts must lie in [{self.v_min}, {self.vdd}] "
                             f"(0 V would be 'off', not a lit level)")
        return pts

    def volts_for_pct(self, pct: float) -> float:
        pct = self._clamp_pct(pct)
        if self.curve:
            pts = self.curve
            if pct <= pts[0][0]:
                return pts[0][1]
            for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
                if pct <= p1:
                    return v0 if p1 == p0 else v0 + (v1 - v0) * (pct - p0) / (p1 - p0)
            return pts[-1][1]
        return self.v_min + (self.v_max - self.v_min) * pct / 100.0

    def code_for_pct(self, pct: float) -> int:
        return max(0, min(_DAC_FULL, int(round(self.volts_for_pct(pct) / self.vdd * _DAC_FULL))))

    def volts(self) -> float:
        return self._code / _DAC_FULL * self.vdd

    # ── control ──

    def set_intensity(self, pct: float):
        """Set the intensity (0..100 % of the dimming range). Applied immediately if on."""
        with self._lock:
            self.intensity_pct = self._clamp_pct(pct)
            if self._on:
                self._write_dac(self.code_for_pct(self.intensity_pct))

    def on(self):
        with self._lock:
            if self.bus is None:
                raise RuntimeError("ir_led not initialised")
            self._write_dac(self.code_for_pct(self.intensity_pct), verify=True)
            self._on = True
            self._on_t = time.time()

    def off(self):
        """DAC to 0 V (DIM < 0.4 V = LED off), verified by read-back."""
        with self._lock:
            if self.bus is None:
                return
            was_on = self._on
            self._on = False
            self._write_dac(0, verify=True)
            if was_on:
                self._off_t = time.time()

    @property
    def is_on(self) -> bool:
        return self._on

    def status(self) -> dict:
        return {"on": self._on, "intensity_pct": self.intensity_pct, "dac_code": self._code,
                "dac_volts": round(self.volts(), 3), "i2c_address": f"0x{self.addr:02x}"}

    def check(self) -> dict:
        try:
            with self._lock:
                st = self._read_status()
            eeprom_ok = st["eeprom"] == 0 and st["eeprom_pd"] == 0
            msg = (f"MCP4725 0x{self.addr:02x} on I2C-{self.i2c_bus}: "
                   f"DAC {st['dac']}/4095 ({st['dac'] / _DAC_FULL * self.vdd:.2f} V), "
                   f"EEPROM {st['eeprom']}{'' if eeprom_ok else ' (NOT 0 — LED lights at boot!)'}, "
                   f"{'ON' if self._on else 'off'}, set {self.intensity_pct:.0f} % "
                   f"= {self.volts_for_pct(self.intensity_pct):.2f} V")
            return {"ok": eeprom_ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": f"MCP4725 read failed: {e}"}

    # ── session lifecycle (engine/leader.py; pi_api's Setup monitor uses the same calls) ──

    def start_stream(self, callback):
        """Session start (or Setup live monitor): switch on if wanted and announce it once."""
        if self.on_during_session:
            self.on()
        callback({"event": "ir_led", "t": time.time(), "on": self._on,
                  "intensity_pct": self.intensity_pct, "dac_code": self._code,
                  "dac_volts": round(self.volts(), 3)})

    def stop_stream(self):
        self.off()

    def hdf5_session_data(self) -> dict:
        import numpy as np
        nan = float("nan")
        return {
            "ir_led_intensity_pct": np.asarray(self.intensity_pct, dtype=np.float32),
            "ir_led_dac_volts": np.asarray(self.code_for_pct(self.intensity_pct) / _DAC_FULL * self.vdd,
                                           dtype=np.float32),
            "ir_led_on": np.asarray(1 if self._on_t is not None else 0, dtype=np.int8),
            "ir_led_on_t": np.asarray(self._on_t if self._on_t is not None else nan, dtype=np.float64),
            "ir_led_off_t": np.asarray(self._off_t if self._off_t is not None else nan, dtype=np.float64),
        }

    def close(self):
        try:
            self.off()
        except Exception as e:
            print(f"[ir_led] WARNING: could not switch the LED off on close: {e}", flush=True)
        _LIVE.discard(self)
        with self._lock:
            try:
                if self.bus is not None:
                    self.bus.close()
            except Exception:
                pass
            self.bus = None
