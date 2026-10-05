"""
shared/intensity_model.py — the display's light model, one implementation for the controller
(previews, Correct), the Leader (stimulus generation) and the follower's renderer (per-pixel
drive). numpy only: the renderer runs on the Pi's system python3.

THE CALIBRATION (display_calibration/<rig>/intensity/intensity_cal_<stamp>.yaml, one altitude)

  azimuth_sweep:  [{az_deg, reading}]       column 1: light at FULL drive along azimuth
  level_sweeps:   [{az_deg, measurements: [{level, reading}, ...]}, ...]
                                            further columns: light vs drive 1.0..0.0 at an
                                            azimuth the user chose (one or more columns)

Readings are meter values (lux, W, cd/m2 — the `unit` field only labels them). Mock files
(source: theoretical / none) use the same format with a straight 0 -> 0, 1 -> 1 response, so
"no measurement yet" runs through the exact same code.

THE MODEL (symmetric about az 0: every azimuth is folded to |az|)

  L(az, v) = F(az) + (M(az) - F(az)) * h(v; az)

  M(az)   light at drive 1: the azimuth sweep's shape R(az), scaled at each level-sweep azimuth
          by that sweep's own drive-1 reading (the two tables are measured minutes apart, so the
          scale is re-anchored rather than assumed), the scale interpolated in |az|
  F(az)   black floor: the level sweeps' drive-0 readings, interpolated in |az|
  h       normalized response, 0 at v=0 and 1 at v=1, from each level sweep; between sweep
          azimuths the INVERSE responses are interpolated linearly in |az|; beyond the first and
          last sweep everything is held constant (np.interp)

  Uniform range over the calibrated azimuths (0 .. the azimuth sweep's largest |az|):
      Lo = max F      (nothing can be darker everywhere)      Hi = min M (nor brighter)
  Brightness b in [0, 1] means L = Lo + b * (Hi - Lo). A stimulus may ask for b > 1 (above the
  uniform ceiling, where the screen can deliver it); each pixel then clips at drive 1.

  With a theoretical mock (F = 0, linear h, M = the geometry gain) drive = b * min(gain)/gain(az),
  exactly the old multiplicative luminance correction.
"""
from __future__ import annotations

import numpy as np

AZ_GRID = np.linspace(0.0, 105.0, 211)       # |az| grid the folded curves are stored on
X_GRID = np.linspace(0.0, 1.0, 513)          # normalized-response grid for the inverse LUTs
PREFIX = "int_"                              # array names inside warp_map.npz


class CalibrationError(ValueError):
    """The calibration file cannot be turned into a model (and the message says why)."""


# ── fitting ──────────────────────────────────────────────────────────────────────────────

def _fold(rows, key="az_deg", val="reading"):
    """[(|az|, mean reading)] sorted by |az| — readings at +az and -az are averaged."""
    acc = {}
    for r in rows:
        a = round(abs(float(r[key])), 6)
        acc.setdefault(a, []).append(float(r[val]))
    return sorted((a, float(np.mean(v))) for a, v in acc.items())


def azimuth_asymmetry(rows):
    """[(|az|, left, right, (right-left)/mean)] for every |az| measured on both sides."""
    by = {}
    for r in rows:
        by.setdefault(float(r["az_deg"]), []).append(float(r["reading"]))
    out = []
    for a in sorted({abs(k) for k in by} - {0.0}):
        if a in by and -a in by:
            left, right = float(np.mean(by[-a])), float(np.mean(by[a]))
            m = (left + right) / 2.0
            if m > 0:
                out.append((a, left, right, (right - left) / m))
    return out


def _response(meas, az):
    """One level sweep -> (floor, top, levels, normalized response h) with h monotone."""
    rows = sorted((float(m["level"]), float(m["reading"])) for m in meas)
    if len(rows) < 2:
        raise CalibrationError(f"level column at az {az}: needs at least the 1.0 and 0.0 readings")
    lv = np.array([r[0] for r in rows])
    rd = np.maximum.accumulate(np.array([r[1] for r in rows]))   # light never drops as drive rises
    if lv[0] > 0.0 or lv[-1] < 1.0:
        raise CalibrationError(f"level column at az {az}: readings at level 0.0 and 1.0 are required")
    floor, top = float(rd[0]), float(rd[-1])
    if top <= floor:
        raise CalibrationError(f"level column at az {az}: drive 1.0 is not brighter than drive 0.0")
    return floor, top, lv, (rd - floor) / (top - floor)


def fit(cal: dict) -> dict:
    """Calibration dict (the YAML) -> model parameters (plain numpy arrays + scalars), the same
    structure stored in warp_map.npz under PREFIX. Raises CalibrationError with a readable reason."""
    az_rows = cal.get("azimuth_sweep") or []
    if not az_rows:
        raise CalibrationError("column 1 (light along azimuth) has no readings")
    sweeps = cal.get("level_sweeps") or []
    if not sweeps:
        raise CalibrationError("no level column: add at least one azimuth with readings 1.0 .. 0.0")

    folded = _fold(az_rows)
    az_u = np.array([a for a, _ in folded]); r_u = np.array([r for _, r in folded])
    if np.any(r_u <= 0):
        raise CalibrationError("column 1 has a reading <= 0 — every azimuth must show light at drive 1")
    R = lambda a: np.interp(a, az_u, r_u)                    # noqa: E731  shape of max light
    az_cal = float(az_u.max())                               # calibrated range is 0 .. az_cal

    per = {}                                                 # |az| -> list of (floor, top, hinv)
    for s in sweeps:
        az = abs(float(s["az_deg"]))
        floor, top, lv, h = _response(s.get("measurements") or [], s["az_deg"])
        h_strict = h + np.arange(len(h)) * 1e-9              # strictly increasing for the inverse
        hinv = np.interp(X_GRID, h_strict, lv)
        per.setdefault(round(az, 6), []).append((floor, top, hinv))
    sweep_az = np.array(sorted(per))
    floors = np.array([np.mean([p[0] for p in per[a]]) for a in sweep_az])
    tops = np.array([np.mean([p[1] for p in per[a]]) for a in sweep_az])
    hinv = np.array([np.mean([p[2] for p in per[a]], axis=0) for a in sweep_az])

    scale = tops / R(sweep_az)                               # re-anchor column 1 at each sweep
    M = np.interp(AZ_GRID, sweep_az, scale) * R(AZ_GRID)
    F = np.interp(AZ_GRID, sweep_az, floors)
    if np.any(M <= F):
        bad = AZ_GRID[M <= F][0]
        raise CalibrationError(f"at |az| {bad:g}° the floor is not below the full-drive light")
    in_cal = AZ_GRID <= az_cal + 1e-9
    lo, hi = float(F[in_cal].max()), float(M[in_cal].min())
    if hi <= lo:
        raise CalibrationError(f"no uniform range: darkest-everywhere {lo:g} >= brightest-everywhere {hi:g}")
    return {"az_grid": AZ_GRID.copy(), "F": F, "M": M, "sweep_az": sweep_az, "hinv": hinv,
            "lo": lo, "hi": hi, "az_cal": az_cal,
            "unit": str(cal.get("unit") or "lux"), "source": str(cal.get("source") or "measured"),
            "name": str(cal.get("name") or "")}


def fit_report(cal: dict, params: dict) -> dict:
    """Numbers worth showing after a fit: the uniform range, how much the level columns' shapes
    differ (0 = one response everywhere; large = the az-interpolation is doing real work), and the
    left/right asymmetry of column 1."""
    spread = 0.0
    if len(params["hinv"]) > 1:
        v = np.linspace(0, 1, 101)
        hs = [np.interp(v, h + np.arange(len(h)) * 1e-12, X_GRID) for h in params["hinv"]]
        spread = float(max(np.max(np.abs(h - hs[0])) for h in hs))
    asym = azimuth_asymmetry(cal.get("azimuth_sweep") or [])
    return {"lo": params["lo"], "hi": params["hi"], "unit": params["unit"],
            "az_cal": params["az_cal"], "columns": [float(a) for a in params["sweep_az"]],
            "shape_spread": spread,
            "asymmetry_max": max((abs(p[3]) for p in asym), default=None)}


# ── mock calibrations ──────────────────────────────────────────────────────────────────

def mock_cal(source: str, az=None, gain=None, alt_deg: float = 0.0, **extra) -> dict:
    """A calibration dict with a straight 0 -> 0, 1 -> 1 response and no floor. source="none":
    flat light (drive = brightness). source="theoretical": column 1 = the geometry model's
    per-azimuth gain (1.0 at az 0), which reproduces the old theoretical luminance correction."""
    if source == "none" or az is None:
        az, gain = [0.0, 105.0], [1.0, 1.0]
    az = [float(a) for a in az]; gain = [float(g) for g in gain]
    g0 = float(np.interp(0.0, az, gain))
    return {
        "kind": "intensity", "version": 1, "source": source, "unit": "lux (mock)",
        "alt_deg": float(alt_deg),
        "azimuth_sweep": [{"az_deg": a, "reading": round(g, 6)} for a, g in zip(az, gain)],
        "level_sweeps": [{"az_deg": 0.0, "measurements": [
            {"level": 1.0, "reading": round(g0, 6)}, {"level": 0.0, "reading": 0.0}]}],
        **extra,
    }


# ── the model ────────────────────────────────────────────────────────────────────────────

class IntensityModel:
    """Forward/inverse light model. Every method takes azimuths as scalars or arrays (degrees,
    sign ignored)."""

    def __init__(self, params: dict):
        self.p = params
        self.az_grid = np.asarray(params["az_grid"], dtype=float)
        self.F_grid = np.asarray(params["F"], dtype=float)
        self.M_grid = np.asarray(params["M"], dtype=float)
        self.sweep_az = np.atleast_1d(np.asarray(params["sweep_az"], dtype=float))
        self.hinv = np.atleast_2d(np.asarray(params["hinv"], dtype=float))
        self.lo, self.hi = float(params["lo"]), float(params["hi"])
        self.unit = str(params.get("unit", "lux"))
        self.source = str(params.get("source", ""))
        self.name = str(params.get("name", ""))

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
        if PREFIX + "F" not in keys:
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
        s = lambda k, d="": str(np.asarray(g(k)).reshape(-1)[0]) if PREFIX + k in keys else d  # noqa: E731
        return cls({"az_grid": g("az_grid"), "F": g("F"), "M": g("M"), "sweep_az": g("sweep_az"),
                    "hinv": g("hinv"), "lo": float(np.asarray(g("lo")).reshape(-1)[0]),
                    "hi": float(np.asarray(g("hi")).reshape(-1)[0]),
                    "az_cal": float(np.asarray(g("az_cal")).reshape(-1)[0]) if PREFIX + "az_cal" in keys else 105.0,
                    "unit": s("unit", "lux"), "source": s("source"), "name": s("name")})

    def to_arrays(self) -> dict:
        """PREFIX-ed arrays for np.savez(warp_map.npz, ...)."""
        p = self.p
        return {PREFIX + "az_grid": self.az_grid, PREFIX + "F": self.F_grid, PREFIX + "M": self.M_grid,
                PREFIX + "sweep_az": self.sweep_az, PREFIX + "hinv": self.hinv,
                PREFIX + "lo": np.array([self.lo]), PREFIX + "hi": np.array([self.hi]),
                PREFIX + "az_cal": np.array([float(p.get("az_cal", 105.0))]),
                PREFIX + "unit": np.array([self.unit]), PREFIX + "source": np.array([self.source]),
                PREFIX + "name": np.array([self.name])}

    # per-azimuth curves
    def floor(self, az):
        return np.interp(np.abs(az), self.az_grid, self.F_grid)

    def max_light(self, az):
        return np.interp(np.abs(az), self.az_grid, self.M_grid)

    def _inverse_at(self, a, x):
        """drive for normalized response x at |az| a (arrays of equal shape or broadcastable)."""
        if len(self.sweep_az) == 1:
            return np.interp(x, X_GRID, self.hinv[0])
        a = np.clip(a, self.sweep_az[0], self.sweep_az[-1])
        k = np.clip(np.searchsorted(self.sweep_az, a, side="right") - 1, 0, len(self.sweep_az) - 2)
        w = (a - self.sweep_az[k]) / (self.sweep_az[k + 1] - self.sweep_az[k])
        out = np.zeros(np.shape(x), dtype=float)
        for j in np.unique(k):
            sel = (k == j)
            lo_v = np.interp(np.asarray(x)[sel], X_GRID, self.hinv[j])
            hi_v = np.interp(np.asarray(x)[sel], X_GRID, self.hinv[j + 1])
            out[sel] = (1 - w[sel]) * lo_v + w[sel] * hi_v
        return out

    # the two directions
    def drive(self, az, L):
        """Drive (0..1) that delivers light L at azimuth az; values outside the reachable range clip."""
        a = np.abs(np.asarray(az, dtype=float))
        L = np.asarray(L, dtype=float)
        a, L = np.broadcast_arrays(a, L)
        Fa, Ma = self.floor(a), self.max_light(a)
        x = np.clip((L - Fa) / (Ma - Fa), 0.0, 1.0)
        v = self._inverse_at(a, x)
        return float(v) if np.ndim(v) == 0 else v

    def light(self, az, v):
        """Light delivered at azimuth az for drive v (0..1)."""
        a_arr = np.abs(np.asarray(az, dtype=float)); v_arr = np.clip(np.asarray(v, dtype=float), 0, 1)
        a_arr, v_arr = np.broadcast_arrays(a_arr, v_arr)
        out = np.empty(a_arr.shape, dtype=float)
        for a in np.unique(a_arr):
            sel = (a_arr == a)
            curve = self._inverse_at(np.full(X_GRID.shape, a), X_GRID)     # drive at each x
            curve = curve + np.arange(len(curve)) * 1e-12
            x = np.interp(v_arr[sel], curve, X_GRID)
            out[sel] = self.floor(a) + (self.max_light(a) - self.floor(a)) * x
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
