"""
shared/intensity_model.py — the display's light model, shared by the controller (previews,
Correct), the Leader (stimulus generation) and the follower's renderer (per-pixel drive).
numpy only: the renderer runs on the Pi's system python3.

Calibration (display_calibration/<rig>/intensity/intensity_cal_<stamp>.yaml, one altitude):
    level_sweeps: [{az_deg, measurements: [{level, reading}, ...]}, ...]
one column per azimuth: the meter reading at each drive level 1.0 .. 0.0 (0.0 = black floor).
Two columns minimum. Mock files (theoretical / none) have two levels per column (0 -> 0, 1 -> gain).

Model: a (|az| x level) table L(az, v). Each column is made monotone and put on the common level
grid; for each level the light is interpolated linearly across the columns' |az| (±az averaged)
and held constant beyond the end columns. Forward: interpolate L(az, ·) in v. Inverse (the
renderer, per pixel): the drive at which L(az, ·) reaches the target light. Uniform range
Lo = max_az L(az, 0), Hi = min_az L(az, 1) over the calibrated azimuths; brightness b in [0, 1]
means L = Lo + b (Hi - Lo); b > 1 is allowed where the screen can deliver it (pixels clip at 1).
With a theoretical mock, drive = b * min(gain)/gain(az): the old multiplicative correction.
"""
from __future__ import annotations

import numpy as np

AZ_GRID = np.linspace(0.0, 105.0, 211)       # |az| grid the table is stored on (0.5°, + the column azimuths)
PREFIX = "int_"                              # array names inside warp_map.npz
MIN_COLUMNS = 2


class CalibrationError(ValueError):
    """The calibration file cannot be turned into a model (and the message says why)."""


# ── fitting ──────────────────────────────────────────────────────────────────────────────

def _column(meas, az):
    """One level column -> (levels ascending, readings monotone non-decreasing)."""
    rows = sorted((float(m["level"]), float(m["reading"])) for m in meas
                  if m.get("reading") not in (None, ""))
    if len(rows) < 2:
        raise CalibrationError(f"column at az {az:g}°: needs at least the 1.0 and 0.0 readings")
    lv = np.array([r[0] for r in rows]); rd = np.array([r[1] for r in rows])
    if np.any(np.diff(lv) <= 0):
        raise CalibrationError(f"column at az {az:g}°: a drive level appears twice")
    if lv[0] > 0.0 or lv[-1] < 1.0:
        raise CalibrationError(f"column at az {az:g}°: readings at level 0.0 and 1.0 are required")
    if np.any(lv < 0) or np.any(lv > 1):
        raise CalibrationError(f"column at az {az:g}°: drive levels must be within 0..1")
    rd = np.maximum.accumulate(rd)                    # light never drops as drive rises
    if rd[-1] <= rd[0]:
        raise CalibrationError(f"column at az {az:g}°: drive 1.0 is not brighter than drive 0.0")
    return lv, rd


def _columns(cal):
    """{signed az: (levels, readings)} with duplicate azimuths averaged on a common level grid.
    Returns (signed dict, common levels)."""
    sweeps = cal.get("level_sweeps") or []
    cols = []
    for s in sweeps:
        az = float(s["az_deg"])
        lv, rd = _column(s.get("measurements") or [], az)
        cols.append((az, lv, rd))
    if not cols:
        raise CalibrationError("no level columns: add columns at two or more azimuths "
                               "(each with readings at drive 1.0 .. 0.0)")
    levels = np.unique(np.concatenate([c[1] for c in cols]))
    signed = {}
    for az, lv, rd in cols:
        signed.setdefault(az, []).append(np.interp(levels, lv, rd))
    return {az: np.mean(v, axis=0) for az, v in signed.items()}, levels


def fit(cal: dict) -> dict:
    """Calibration dict (the YAML) -> model parameters (plain numpy arrays + scalars), the same
    structure stored in warp_map.npz under PREFIX. Raises CalibrationError with a readable reason.
    An `azimuth_sweep` key from the earlier format is ignored."""
    signed, levels = _columns(cal)
    folded = {}
    for az, rd in signed.items():
        folded.setdefault(round(abs(az), 6), []).append(rd)
    cols_az = np.array(sorted(folded))
    table = np.array([np.mean(folded[a], axis=0) for a in cols_az])      # K x N
    if len(cols_az) < MIN_COLUMNS:
        raise CalibrationError(f"only {len(cols_az)} column azimuth(s); need at least {MIN_COLUMNS} "
                               f"different |az| (three or more recommended)")
    # per level: light across |az| on the 0.5° grid + the column azimuths (columns reproduced exactly)
    az_grid = np.union1d(AZ_GRID, cols_az)
    L = np.column_stack([np.interp(az_grid, cols_az, table[:, j]) for j in range(len(levels))])
    az_cal = float(cols_az.max())
    in_cal = az_grid <= az_cal + 1e-9
    lo, hi = float(L[in_cal, 0].max()), float(L[in_cal, -1].min())
    if hi <= lo:
        raise CalibrationError(f"no uniform range: darkest-everywhere {lo:g} >= brightest-everywhere {hi:g}")
    return {"az_grid": az_grid, "levels": levels, "L": L, "cols_az": cols_az,
            "lo": lo, "hi": hi, "az_cal": az_cal,
            "unit": str(cal.get("unit") or "lux"), "source": str(cal.get("source") or "measured"),
            "name": str(cal.get("name") or "")}


def azimuth_asymmetry(cal: dict):
    """[(|az|, left_top, right_top, (right-left)/mean)] for column azimuths measured on both
    sides of centre, at full drive."""
    try:
        signed, levels = _columns(cal)
    except CalibrationError:
        return []
    out = []
    for a in sorted({abs(k) for k in signed} - {0.0}):
        if a in signed and -a in signed:
            left, right = float(signed[-a][-1]), float(signed[a][-1])
            m = (left + right) / 2.0
            if m > 0:
                out.append((a, left, right, (right - left) / m))
    return out


def fit_report(cal: dict, params: dict) -> dict:
    """Numbers worth showing after a fit: the uniform range, how much the columns' normalized
    response SHAPES differ (0 = the same curve everywhere; the model does not need them to agree,
    this just says how non-separable the screen is), and the left/right asymmetry."""
    tab = np.array([np.interp(params["cols_az"], params["az_grid"], params["L"][:, j])
                    for j in range(len(params["levels"]))]).T          # K x N at the columns
    norm = (tab - tab[:, :1]) / (tab[:, -1:] - tab[:, :1])
    spread = float(np.max(np.abs(norm - norm[:1]))) if len(tab) > 1 else 0.0
    asym = azimuth_asymmetry(cal)
    return {"lo": params["lo"], "hi": params["hi"], "unit": params["unit"],
            "az_cal": params["az_cal"], "columns": [float(a) for a in params["cols_az"]],
            "levels": int(len(params["levels"])), "shape_spread": spread,
            "asymmetry_max": max((abs(p[3]) for p in asym), default=None),
            "ignored_azimuth_sweep": bool(cal.get("azimuth_sweep"))}


# ── mock calibrations ──────────────────────────────────────────────────────────────────

def mock_cal(source: str, az=None, gain=None, alt_deg: float = 0.0, **extra) -> dict:
    """A calibration dict with two levels per column — drive 0 -> 0, drive 1 -> gain(az) — and
    no floor. source="none": flat light (drive = brightness). source="theoretical": the geometry
    model's per-azimuth gain (1.0 at az 0), which reproduces the old theoretical correction."""
    if source == "none" or az is None:
        az, gain = [0.0, 105.0], [1.0, 1.0]
    return {
        "kind": "intensity", "version": 1, "source": source, "unit": "lux (mock)",
        "alt_deg": float(alt_deg),
        "level_sweeps": [{"az_deg": float(a), "measurements": [
            {"level": 1.0, "reading": round(float(g), 6)}, {"level": 0.0, "reading": 0.0}]}
            for a, g in zip(az, gain)],
        **extra,
    }


# ── the model ────────────────────────────────────────────────────────────────────────────

class IntensityModel:
    """Forward/inverse light model over the (|az|, drive) table. Azimuths may be scalars or
    arrays (degrees, sign ignored)."""

    def __init__(self, params: dict):
        self.p = params
        self.az_grid = np.asarray(params["az_grid"], dtype=float)
        self.levels = np.asarray(params["levels"], dtype=float)
        self.L = np.asarray(params["L"], dtype=float)                 # (grid, levels)
        self.lo, self.hi = float(params["lo"]), float(params["hi"])
        self.az_cal = float(params.get("az_cal", self.az_grid[-1]))
        self.unit = str(params.get("unit", "lux"))
        self.source = str(params.get("source", ""))
        self.name = str(params.get("name", ""))
        # strictly increasing rows for the inverse (flat stretches -> the lowest drive that reaches L)
        self._Lstrict = self.L + np.arange(self.L.shape[1]) * (1e-9 * max(float(np.abs(self.L).max()), 1e-12))

    # construction
    @classmethod
    def from_cal(cls, cal: dict) -> "IntensityModel":
        return cls(fit(cal))

    @classmethod
    def identity(cls) -> "IntensityModel":
        """No calibration at all: drive == brightness == light."""
        return cls.from_cal(mock_cal("none"))

    @classmethod
    def from_arrays(cls, arrays) -> "IntensityModel":
        """From warp_map.npz (or any mapping) holding PREFIX-ed arrays. A warp built before the
        light model existed is read through its old luminance arrays instead — a mock with the
        same gain curve (empirical if that mode was baked in, else theoretical; 'none' -> flat) —
        so an un-rebuilt warp on a Pi renders exactly as it did before. Identity if nothing."""
        keys = arrays.files if hasattr(arrays, "files") else list(arrays.keys())
        if PREFIX + "L" not in keys:
            mode = str(np.asarray(arrays["lum_correction_mode"]).reshape(-1)[0]) \
                if "lum_correction_mode" in keys else None
            if mode == "none":
                return cls.identity()
            if mode == "empirical" and "lum_gain_empirical" in keys:
                m = cls.from_cal(mock_cal("theoretical", arrays["lum_az_empirical"], arrays["lum_gain_empirical"]))
                m.source, m.name = "legacy-empirical", "legacy warp (empirical gain)"
                return m
            if "lum_gain_theoretical" in keys:
                m = cls.from_cal(mock_cal("theoretical", arrays["lum_az"], arrays["lum_gain_theoretical"]))
                m.source, m.name = "legacy-theoretical", "legacy warp (theoretical gain)"
                return m
            return cls.identity()
        g = lambda k: arrays[PREFIX + k]                                       # noqa: E731
        sc = lambda k: float(np.asarray(g(k)).reshape(-1)[0])                 # noqa: E731
        st = lambda k, d="": str(np.asarray(g(k)).reshape(-1)[0]) if PREFIX + k in keys else d  # noqa: E731
        return cls({"az_grid": g("az_grid"), "levels": g("levels"), "L": g("L"), "cols_az": g("cols_az"),
                    "lo": sc("lo"), "hi": sc("hi"), "az_cal": sc("az_cal") if PREFIX + "az_cal" in keys else 105.0,
                    "unit": st("unit", "lux"), "source": st("source"), "name": st("name")})

    def to_arrays(self) -> dict:
        """PREFIX-ed arrays for np.savez(warp_map.npz, ...)."""
        return {PREFIX + "az_grid": self.az_grid, PREFIX + "levels": self.levels, PREFIX + "L": self.L,
                PREFIX + "cols_az": np.asarray(self.p["cols_az"], dtype=float),
                PREFIX + "lo": np.array([self.lo]), PREFIX + "hi": np.array([self.hi]),
                PREFIX + "az_cal": np.array([self.az_cal]),
                PREFIX + "unit": np.array([self.unit]), PREFIX + "source": np.array([self.source]),
                PREFIX + "name": np.array([self.name])}

    # per-azimuth curves
    def floor(self, az):
        return np.interp(np.abs(az), self.az_grid, self.L[:, 0])

    def max_light(self, az):
        return np.interp(np.abs(az), self.az_grid, self.L[:, -1])

    # the two directions
    def _drive_grid(self, L_target: float):
        """Drive at every grid azimuth that delivers L_target (clipped to the reachable range)."""
        return np.array([np.interp(L_target, row, self.levels) for row in self._Lstrict])

    def drive(self, az, L):
        """Drive (0..1) that delivers light L at azimuth az; values outside the reachable range clip.
        L a scalar (the common case: one background or one stimulus light over many pixels) or an
        array broadcastable against az."""
        a = np.abs(np.asarray(az, dtype=float))
        L = np.asarray(L, dtype=float)
        if L.ndim == 0:
            v = np.interp(a, self.az_grid, self._drive_grid(float(L)))
            return float(v) if np.ndim(v) == 0 else v
        a, L = np.broadcast_arrays(a, L)
        out = np.empty(a.shape, dtype=float)
        for val in np.unique(L):
            sel = (L == val)
            out[sel] = np.interp(a[sel], self.az_grid, self._drive_grid(float(val)))
        return float(out) if out.ndim == 0 else out

    def light(self, az, v):
        """Light delivered at azimuth az for drive v (0..1)."""
        a = np.abs(np.asarray(az, dtype=float)); v = np.clip(np.asarray(v, dtype=float), 0, 1)
        a, v = np.broadcast_arrays(a, v)
        out = np.empty(a.shape, dtype=float)
        for val in np.unique(v):
            sel = (v == val)
            L_at_v = np.array([np.interp(val, self.levels, row) for row in self.L])   # per grid az
            out[sel] = np.interp(a[sel], self.az_grid, L_at_v)
        return float(out) if out.ndim == 0 else out

    # brightness <-> light
    def L_from_b(self, b):
        return self.lo + np.asarray(b, dtype=float) * (self.hi - self.lo)

    def b_from_L(self, L):
        return (np.asarray(L, dtype=float) - self.lo) / (self.hi - self.lo)

    # contrast in light units
    def degenerate(self, L_b, metric) -> bool:
        """Weber/Michelson are undefined on a background with (almost) no light."""
        return str(metric).lower() in ("weber", "michelson") and float(L_b) <= 1e-6 * max(self.hi, 1e-12)

    def stim_light(self, c, L_b, metric="weber"):
        """Stimulus light for contrast c (a fraction: 0.5 = 50 %) on background light L_b.
        normalized: c is the fraction of the uniform headroom above the background (L_b .. Hi).
        A degenerate Weber/Michelson (no background light) falls back to normalized."""
        c, L_b, metric = float(c), float(L_b), str(metric).lower()
        if metric == "normalized" or self.degenerate(L_b, metric):
            return L_b + c * (self.hi - L_b)
        if metric == "michelson":
            c = min(c, 0.999)
            return L_b * (1 + c) / (1 - c)
        return L_b * (1 + c)

    def contrast(self, L_s, L_b, metric="weber"):
        L_s, L_b, metric = float(L_s), float(L_b), str(metric).lower()
        if metric == "normalized" or self.degenerate(L_b, metric):
            return (L_s - L_b) / (self.hi - L_b) if self.hi > L_b else 0.0
        if metric == "michelson":
            return (L_s - L_b) / (L_s + L_b) if (L_s + L_b) > 0 else 0.0
        return (L_s - L_b) / L_b

    def ceiling(self, az_list, L_b, metric="weber"):
        """Highest contrast every listed azimuth can show on this background (drive 1 at the
        dimmest of them)."""
        top = min(float(self.max_light(a)) for a in (az_list or [0.0]))
        return self.contrast(top, L_b, metric)
