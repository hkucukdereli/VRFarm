#!/usr/bin/env python3
"""tools/test_intensity_model.py — checks for shared/intensity_model.py. No hardware.

    conda activate vrfarm && python tools/test_intensity_model.py
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared.intensity_model import (IntensityModel, CalibrationError, mock_cal, fit,  # noqa: E402
                                    fit_report)

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


def close(a, b, tol=1e-6):
    return np.allclose(np.asarray(a, dtype=float), np.asarray(b, dtype=float), atol=tol)


def column(az, floor, top, gamma, levels=np.linspace(0, 1, 11)):
    return {"az_deg": az, "measurements": [{"level": float(v), "reading": floor + (top - floor) * v ** gamma}
                                           for v in levels]}


print("identity (no calibration)")
m = IntensityModel.identity()
check(close([m.lo, m.hi], [0, 1]), "uniform range 0..1")
check(close(m.drive([0, 50, 100], 0.37), 0.37), "drive == light everywhere")
check(close(m.light(30, 0.8), 0.8), "light == drive")

print("theoretical mock == the old multiplicative correction")
az = np.linspace(0, 105, 53)
gain = np.clip(1 - (az / 140.0) ** 2, 0.2, 1)                # any monotone falloff
m = IntensityModel.from_cal(mock_cal("theoretical", az, gain))
C = gain.min() / gain                                        # old correction min(g)/g
for b in (0.0, 0.1, 0.5, 1.0):
    want = b * np.interp([0, 40, 80, 105], az, C)
    got = m.drive([0, 40, 80, 105], m.L_from_b(b))
    check(close(got, want, 1e-4), f"b={b}: drive = b*min(g)/g(az)  {np.round(got, 4)}")

print("three columns, separable response (same gamma everywhere)")
cal = {"level_sweeps": [column(0, 0.8, 225, 2.2), column(40, 0.45, 103, 2.2), column(80, 0.2, 51, 2.2),
                        column(-40, 0.45, 103, 2.2)]}          # one column on both sides
m = IntensityModel.from_cal(cal); rep = fit_report(cal, fit(cal))
check(close([m.lo, m.hi], [0.8, 51]), f"uniform range {m.lo}..{m.hi}")
check(close(m.floor([0, 40, 80]), [0.8, 0.45, 0.2]) and close(m.max_light([0, 40, 80]), [225, 103, 51]),
      "floors and tops at the columns")
check(close(m.max_light(20), 164.0) and close(m.floor(60), 0.325), "linear in azimuth between columns")
check(close(m.max_light(100), 51), "held constant beyond the last column")
for a in (0, 25, 40, 61, 80):
    for v in (0.05, 0.3, 0.7, 1.0):
        check(close(m.drive(a, m.light(a, v)), v, 2e-3), f"round trip az {a} drive {v}")
check(close(m.light(0, 0.5), 0.8 + 224.2 * 0.5 ** 2.2, 1e-6), "a column reproduces its own readings")
L = m.L_from_b(0.5); d = m.drive([0, 30, 60, 80], L)
check(close(m.light([0, 30, 60, 80], d), L, 1e-2 * L), "uniform background: same light at every az")
check(np.all(np.diff(d) > 0), "…which needs MORE drive toward the dim edge")
check(rep["shape_spread"] < 1e-6, f"report: identical shapes -> spread {rep['shape_spread']:.1e}")
check(rep["asymmetry_max"] == 0.0 and rep["columns"] == [0.0, 40.0, 80.0], "report: ±40 folded, symmetric")
check(rep["levels"] == 11 and rep["az_cal"] == 80.0, "report: levels + calibrated range")
Lb = m.L_from_b(0.2); Ls = m.stim_light(0.5, Lb, "weber")
check(close(m.contrast(Ls, Lb, "weber"), 0.5), "Weber 50 % round trip")
check(m.degenerate(0.0, "weber") and not m.degenerate(Lb, "weber"), "Weber degenerate only without light")

print("non-separable response (edge column much flatter): per-level curves honour it")
cal2 = {"level_sweeps": [column(0, 0.8, 225, 2.2), column(45, 0.5, 100, 1.6), column(90, 0.3, 40, 1.0)]}
m2 = IntensityModel.from_cal(cal2); rep2 = fit_report(cal2, fit(cal2))
check(rep2["shape_spread"] > 0.15, f"report flags the spread ({rep2['shape_spread']:.2f})")
for a, fl, top, gm in ((0, 0.8, 225, 2.2), (45, 0.5, 100, 1.6), (90, 0.3, 40, 1.0)):
    check(close(m2.light(a, 0.5), fl + (top - fl) * 0.5 ** gm, 1e-6), f"column at {a}° reproduced exactly")
mid = m2.light(22.5, 0.5)
check(close(mid, (m2.light(0, 0.5) + m2.light(45, 0.5)) / 2, 1e-6), "between columns: the mean of both at that level")
check(close(m2.drive(67.5, m2.light(67.5, 0.42)), 0.42, 2e-3), "inverse consistent between columns")

print("warp round trip")
m3 = IntensityModel.from_arrays(m2.to_arrays())
check(close(m3.drive([0, 33, 70], L), m2.drive([0, 33, 70], L)), "same drives after to/from arrays")
check(IntensityModel.from_arrays({}).hi == 1.0, "no int_ arrays -> identity")

print("warp from before the light model (old lum_* arrays only)")
legacy = ROOT / "display_calibration" / "cheddar" / "warp_map.npz"
if legacy.exists():
    old = np.load(str(legacy))
    if "int_L" not in old.files and "lum_gain_theoretical" in old.files:
        m_old = IntensityModel.from_arrays(old)
        g = np.maximum(old["lum_gain_theoretical"], 0.05); Cc = g.min() / g
        for b in (0.1, 0.5, 1.0):
            # 1e-3 drive = a quarter of an 8-bit code: the old code interpolated min(g)/g between the
            # 2° gain points, the model interpolates the gain then divides — same curve, ~3e-4 apart.
            want = b * np.interp([0, 45, 90, 105], old["lum_az"], Cc)
            check(close(m_old.drive([0, 45, 90, 105], m_old.L_from_b(b)), want, 1e-3),
                  f"legacy warp renders as the old correction (b={b})")
        check(m_old.source.startswith("legacy"), f"flagged as {m_old.source}")
    else:
        print("  (cheddar warp already carries the model — legacy path not exercised)")
check(IntensityModel.from_arrays({"lum_correction_mode": np.array(["none"])}).hi == 1.0, "legacy mode none -> flat")

print("errors are readable")
one = {"level_sweeps": [column(0, 0.8, 225, 2.2)]}
for bad, why in [({"level_sweeps": []}, "no level columns"),
                 (one, "only 1 column"),
                 ({"level_sweeps": [column(0, 0.8, 225, 2.2), {"az_deg": 40, "measurements": [
                     {"level": 1, "reading": 5}, {"level": 0.5, "reading": 2}]}]}, "level 0.0 and 1.0"),
                 ({"level_sweeps": [column(0, 0.8, 225, 2.2), column(40, 50, 50, 1.0)]}, "not brighter")]:
    try:
        fit(bad); check(False, f"rejects: {why}")
    except CalibrationError as e:
        check(why in str(e), f"rejects: {e}")
old_format = {"azimuth_sweep": [{"az_deg": 0, "reading": 1}], "level_sweeps": cal["level_sweeps"]}
check(fit_report(old_format, fit(old_format))["ignored_azimuth_sweep"], "an azimuth_sweep is ignored, and said so")

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
