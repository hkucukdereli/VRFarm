#!/usr/bin/env python3
"""tools/test_intensity_model.py — checks for shared/intensity_model.py. No hardware.

    conda activate vrfarm && python tools/test_intensity_model.py
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.intensity_model import (IntensityModel, CalibrationError, mock_cal, fit,  # noqa: E402
                                    fit_report)

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


def close(a, b, tol=1e-6):
    return np.allclose(np.asarray(a, dtype=float), np.asarray(b, dtype=float), atol=tol)


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

print("real-looking calibration: floor, nonlinear response, two columns")
def resp(v, gamma):
    return v ** gamma
cal = {"azimuth_sweep": [{"az_deg": a, "reading": r} for a, r in
                         [(-80, 42), (-40, 116), (0, 261), (40, 120), (80, 44)]],
       "level_sweeps": [
           {"az_deg": 0, "measurements": [{"level": v, "reading": 0.85 + 232 * resp(v, 2.2)}
                                          for v in np.linspace(0, 1, 11)]},
           {"az_deg": 60, "measurements": [{"level": v, "reading": 0.6 + 70 * resp(v, 2.0)}
                                           for v in np.linspace(0, 1, 11)]}]}
m = IntensityModel.from_cal(cal)
check(close(m.floor(0), 0.85) and close(m.floor(60), 0.6), "floor at the two columns")
check(close(m.max_light(0), 232.85, 1e-3) and close(m.max_light(60), 70.6, 1e-3),
      "drive-1 light re-anchored at both columns")
check(m.lo == max(m.floor(a) for a in m.az_grid[m.az_grid <= 80]), "Lo = highest floor in range")
for a in (0, 25, 60, 80):
    for v in (0.05, 0.3, 0.7, 1.0):
        check(close(m.drive(a, m.light(a, v)), v, 2e-3), f"round trip az {a} drive {v}")
check(close(m.light(0, 0.5), 0.85 + 232 * 0.5 ** 2.2, 0.2), "column 0 reproduces its own readings")
L = m.L_from_b(0.5)
d = m.drive([0, 30, 60, 80], L)
check(close(m.light([0, 30, 60, 80], d), L, 1e-2 * L), "uniform background: same light at every az")
check(np.all(np.diff(d) > 0), "…which needs MORE drive toward the dim edge")
Lb = m.L_from_b(0.2)
Ls = m.stim_light(0.5, Lb, "weber")
check(close(m.contrast(Ls, Lb, "weber"), 0.5), "Weber 50 % round trip")
check(m.degenerate(0.0, "weber") and not m.degenerate(Lb, "weber"), "Weber degenerate only without light")
rep = fit_report(cal, fit(cal))
check(rep["shape_spread"] > 0.01, f"report: column shapes differ ({rep['shape_spread']:.3f})")
check(rep["asymmetry_max"] is not None and rep["asymmetry_max"] < 0.1, "report: L/R asymmetry")

print("warp round trip")
m2 = IntensityModel.from_arrays(m.to_arrays())
check(close(m2.drive([0, 33, 70], L), m.drive([0, 33, 70], L)), "same drives after to/from arrays")
check(IntensityModel.from_arrays({}).hi == 1.0, "no int_ arrays -> identity")

print("errors are readable")
for bad, why in [({"azimuth_sweep": [], "level_sweeps": cal["level_sweeps"]}, "column 1"),
                 ({"azimuth_sweep": cal["azimuth_sweep"], "level_sweeps": []}, "no level column"),
                 ({"azimuth_sweep": cal["azimuth_sweep"], "level_sweeps": [
                     {"az_deg": 0, "measurements": [{"level": 1, "reading": 5}, {"level": 0.5, "reading": 2}]}]},
                  "level 0.0 and 1.0")]:
    try:
        fit(bad); check(False, f"rejects: {why}")
    except CalibrationError as e:
        check(why in str(e), f"rejects: {e}")

print("\nPASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
