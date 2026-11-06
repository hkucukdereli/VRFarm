"""
devices/lick_sensor.py

MPR121 capacitive touch sensor for lick detection.
I/O: I2C (smbus2). 200 Hz polling of the touch-status register, rising-edge detection.

Refractory period / debounce — rig YAML `devices.lick_sensor.debounce_ms` (default 50, 0 = off).
It is the MPR121's RELEASE debounce (register 0x5B, field DR): after a touch the electrode must
read released for DR consecutive filter periods before the status bit clears, so a new lick can
only be counted once the spout has been untouched for about debounce_ms. The TOUCH debounce (DT)
stays 0, so lick onsets are not delayed. This absorbs the 6-12 ms double counts seen on morbier
(threshold chatter at the edge of a lick: 16 % of inter-lick intervals < 20 ms, 2026-10-27).

The unit of the debounce is the chip's second-level filter period, ESI x SFI (datasheet 5.7:
"the status bit change will have a delay of {ESI x SFI x DR}"), and a 1 ms ESI is overridden by
the time one scan of all enabled channels takes (datasheet 5.8: FFI x (CDT + 32 us) x channels
= 6 x 32.5 us x 12 = 2.34 ms with ECR 0x8F). debounce_registers() picks the (ESI, SFI, DR) whose
DR x period lands nearest the request, preferring the shortest filter period (least onset
latency) among those within 10 %. init() then MEASURES the period from the electrode's filtered
data and re-picks the registers if the chip disagrees with the model by more than 25 %, so the
ms value is honoured either way; the outcome is printed (session log, Setup init log) and
reported by check().
"""

from __future__ import annotations
import statistics
import threading
import time

from .base import Device, DeviceInfo, IOType, register_device

DEFAULT_DEBOUNCE_MS = 50
MAX_DEBOUNCE_MS = 1000

# Register 0x5D (CDT | SFI | ESI) encodings and the fixed parts of this driver's configuration.
_ESI_MS = [1, 2, 4, 8, 16, 32, 64, 128]   # ESI encoding n -> 2^n ms between second-filter samples
_SFI_SAMPLES = [4, 6, 10, 18]             # SFI encoding -> samples per second-filter output
_CDT_CODE, _CDT_US = 1, 0.5               # charge/discharge time 0.5 us
_FFI_SAMPLES = 6                          # 0x5C = 0x10: FFI 6 samples, CDC 16 uA
_N_CHANNELS = 12                          # 0x5E = 0x8F: all twelve electrodes scanned
_CONVERSION_US = 32.0                     # per-sample conversion time (datasheet 5.8 example)


def scan_ms() -> float:
    """One scan of every enabled channel (datasheet 5.8); it overrides a shorter ESI."""
    return _FFI_SAMPLES * (_CDT_US + _CONVERSION_US) * _N_CHANNELS / 1000.0


def debounce_registers(debounce_ms, scan: float | None = None) -> dict:
    """Register values for a requested release debounce.

    Returns {"ms", "dr", "esi_ms", "esi_eff_ms", "sfi", "period_ms", "effective_ms",
    "reg_5b", "reg_5d"}. `scan` replaces the modelled scan time (e.g. a measured one).
    0, negative or unparsable = off: DR 0 and the 1 ms / 4-sample filter the driver always used.
    """
    try:
        ms = float(debounce_ms)
    except (TypeError, ValueError):
        ms = 0.0
    ms = min(max(ms, 0.0), float(MAX_DEBOUNCE_MS))
    scan = scan_ms() if scan is None else float(scan)
    if ms <= 0:
        return _pack(0, 0, 0, 0.0, scan)
    tol = max(2.0, 0.1 * ms)
    best = None
    for ei, esi in enumerate(_ESI_MS):
        for si, sfi in enumerate(_SFI_SAMPLES):
            period = max(esi, scan) * sfi
            dr = min(7, max(1, round(ms / period)))
            err = abs(dr * period - ms)
            # Within tolerance: the shortest filter period wins (least onset latency, fastest
            # baseline tracking). Otherwise the smallest error, then the shortest period.
            key = (0, period, err) if err <= tol else (1, err, period)
            if best is None or key < best[0]:
                best = (key, ei, si, dr)
    _, ei, si, dr = best
    return _pack(ei, si, dr, ms, scan)


def _pack(ei: int, si: int, dr: int, ms: float, scan: float) -> dict:
    esi, sfi = _ESI_MS[ei], _SFI_SAMPLES[si]
    eff = max(esi, scan)
    period = eff * sfi
    return {"ms": ms, "dr": dr, "esi_ms": esi, "esi_eff_ms": eff, "sfi": sfi,
            "period_ms": period, "effective_ms": dr * period,
            "reg_5b": dr << 4, "reg_5d": (_CDT_CODE << 5) | (si << 3) | ei}


def describe_debounce(d: dict, measured_ms: float | None = None) -> str:
    """One line for logs and the Setup card: what was asked, what the chip does."""
    if not d.get("dr"):
        s = "debounce off"
    else:
        esi = f"ESI {d['esi_ms']} ms"
        if d["esi_eff_ms"] > d["esi_ms"] + 1e-9:
            esi += f" (scan-bound {d['esi_eff_ms']:.2f} ms)"
        s = (f"debounce {d['ms']:g} ms -> {d['effective_ms']:.0f} ms = release DR {d['dr']} x "
             f"{d['period_ms']:.1f} ms filter period ({esi} x SFI {d['sfi']})")
    if measured_ms:
        s += f"; period measured {measured_ms:.1f} ms"
    return s


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
        self.electrode = int(rig_config.get("electrode", 4))
        self.max_lick_rate = task_params.get("max_lick_rate", 0.3)
        deb = rig_config.get("debounce_ms")
        self.debounce_ms = DEFAULT_DEBOUNCE_MS if deb is None else deb
        self.debounce = debounce_registers(self.debounce_ms)
        self.measured_period_ms = None
        self.bus = smbus2.SMBus(rig_config.get("i2c_bus", 1))
        self._init_registers(self.debounce)
        self._verify_debounce()
        print(f"  MPR121 {self.describe()}", flush=True)
        self._stream_active = False
        self._stream_thread = None
        self._was_touched = False

    def _init_registers(self, d: dict):
        """Soft reset, thresholds, filter + debounce, then run mode (config writes are only
        accepted in stop mode, which the reset leaves the chip in)."""
        a = self.addr
        self.bus.write_byte_data(a, 0x80, 0x63)         # soft reset -> stop mode, defaults
        time.sleep(0.02)
        for i in range(12):
            self.bus.write_byte_data(a, 0x41 + i * 2, 12)   # touch threshold
            self.bus.write_byte_data(a, 0x42 + i * 2,  6)   # release threshold
        self.bus.write_byte_data(a, 0x5B, d["reg_5b"])  # debounce: DR (release) << 4 | DT 0
        self.bus.write_byte_data(a, 0x5C, 0x10)         # FFI 6 samples, CDC 16 uA
        self.bus.write_byte_data(a, 0x5D, d["reg_5d"])  # CDT 0.5 us | SFI | ESI
        self.bus.write_byte_data(a, 0x5E, 0x8F)         # run: baseline tracking, 12 electrodes

    def _measure_period_ms(self, duration_s: float = 0.3) -> float | None:
        """The second-filter period, from how often this electrode's filtered data changes
        (datasheet 5.3: updated every ESI x SFI). The smallest cluster of intervals between
        changes is the period; unchanged readings only make longer intervals. None when the
        reading hardly ever changed (a perfectly quiet electrode) or on an I2C error."""
        reg = 0x04 + 2 * self.electrode
        try:
            time.sleep(0.05)                      # let run mode produce its first outputs
            t_end = time.monotonic() + duration_s
            last_v, t_change, gaps = None, None, []
            while time.monotonic() < t_end:
                lo, hi = self.bus.read_i2c_block_data(self.addr, reg, 2)   # coherent LSB/MSB
                v, now = ((hi & 0x03) << 8) | lo, time.monotonic()
                if last_v is not None and v != last_v:
                    if t_change is not None:
                        gaps.append(now - t_change)
                    t_change = now
                last_v = v
        except Exception:
            return None
        if len(gaps) < 3:
            return None
        lo = min(gaps)
        return 1000.0 * statistics.median([g for g in gaps if g <= 1.5 * lo])

    def _verify_debounce(self):
        """Measure the filter period; if the chip disagrees with the model by more than 25 %,
        re-pick the registers from the measured scan time so debounce_ms is honoured anyway."""
        d = self.debounce
        meas = self._measure_period_ms()
        if meas and d["dr"] and abs(meas - d["period_ms"]) > 0.25 * d["period_ms"]:
            d2 = debounce_registers(self.debounce_ms, scan=meas / d["sfi"])
            if (d2["reg_5b"], d2["reg_5d"]) != (d["reg_5b"], d["reg_5d"]):
                a = self.addr
                self.bus.write_byte_data(a, 0x5E, 0x00)   # stop mode: config writes allowed
                self.bus.write_byte_data(a, 0x5B, d2["reg_5b"])
                self.bus.write_byte_data(a, 0x5D, d2["reg_5d"])
                self.bus.write_byte_data(a, 0x5E, 0x8F)
                meas = self._measure_period_ms() or meas
            d2["period_source"] = "measured"
            self.debounce = d = d2
        self.measured_period_ms = meas

    def describe(self) -> str:
        return describe_debounce(self.debounce, self.measured_period_ms)

    def is_touched(self) -> bool:
        lo = self.bus.read_byte_data(self.addr, 0x00)
        hi = self.bus.read_byte_data(self.addr, 0x01)
        return bool(((hi << 8) | lo) & (1 << self.electrode))

    def check(self) -> dict:
        try:
            touched = self.is_touched()
            return {"ok": True,
                    "message": f"electrode {self.electrode} touched={touched}; {self.describe()}"}
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
