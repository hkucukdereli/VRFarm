"""
fit_luminance_correction.py

Fits a per-azimuth luminance correction curve from intensity-calibration measurements and
writes `luminance_cal_latest.yaml` (which compute_warp_map.py re-injects into warp_map.npz when
built with `--lum-mode empirical`). Can also inject a local warp_map.npz directly.

Measurements come from the setup UI's intensity-cal panel (or the legacy manual tool). Each
reading is a relative light value — a Thorlabs PM100D power reading (W) or a photometer cd/m² —
the units cancel because the gain is normalized to 1.0 at screen center.

G(az) is the relative delivered luminance (1.0 at center, lower toward the edges). To make the
DELIVERED luminance uniform we ATTENUATE the drive (never boost, so nothing clips):
  correction(az)     = min(G) / G(az)        # 1.0 at the dimmest/outermost az, <1 at bright center
  stimulus_drive(az) = requested × correction(az)
so delivered = drive × G = requested × min(G) is identical at every azimuth. The bright center is
darkened down to match the dim edges.

Standalone use:
  python fit_luminance_correction.py [luminance_measurements_YYYY-MM-DD.yaml]
"""

import sys
import yaml
import numpy as np
from pathlib import Path
from datetime import date, datetime
from collections import defaultdict
from scipy.interpolate import UnivariateSpline

CAL_DIR  = Path(__file__).parent
WARP_MAP = CAL_DIR / "warp_map.npz"


def set_cal_dir(path):
    """Point every reader/writer here at a rig's intensity folder. The controller calls this
    with display_calibration/<rig>/intensity/; the rig's warp map is one level up
    (display_calibration/<rig>/warp_map.npz). The functions below resolve their `cal_dir`
    argument against CAL_DIR at CALL time."""
    global CAL_DIR, WARP_MAP
    CAL_DIR = Path(path)
    base = CAL_DIR.parent if CAL_DIR.name == "intensity" else CAL_DIR
    WARP_MAP = base / "warp_map.npz"


def _reading(m):
    """One measurement's light value. Accepts the neutral `reading` key (PM100D power / any
    linear-in-luminance meter) or the legacy `luminance_cdm2` (photometer)."""
    if "reading" in m:
        return float(m["reading"])
    return float(m["luminance_cdm2"])


def azimuth_asymmetry(measurements):
    """Compare +az against -az for every |az| measured on both sides.

    fit_luminance folds to |az| because the correction is 1D and symmetric, an assumption the
    geometry cannot check: lateral_offset_cm=0 makes the MODEL symmetric, but projector yaw (not
    a parameter at all), DLP illumination asymmetry, and screen mounting are outside it. Measuring
    both sides is the only thing that tests it.

    Returns None if no |az| was measured on both sides, else
      {"pairs": [(az, left, right, rel)], "max_rel": float, "mean_rel": float}
    where rel = (right - left) / mean(left, right), signed so a positive value means the +az side
    is brighter. A few percent is measurement noise; a large spread means the symmetric 1D
    correction is the wrong shape and folding is averaging away a real gradient."""
    by_az = defaultdict(list)
    for m in measurements:
        by_az[float(m["az_deg"])].append(_reading(m))
    mags = sorted({abs(a) for a in by_az} - {0.0})
    pairs = []
    for mag in mags:
        if mag in by_az and -mag in by_az:
            left, right = np.mean(by_az[-mag]), np.mean(by_az[mag])
            mean = (left + right) / 2.0
            if mean > 0:
                pairs.append((mag, float(left), float(right), float((right - left) / mean)))
    if not pairs:
        return None
    rels = [abs(p[3]) for p in pairs]
    return {"pairs": pairs, "max_rel": max(rels), "mean_rel": float(np.mean(rels))}


def fit_luminance(measurements):
    """Fit a per-azimuth luminance correction from raw measurements.

    measurements: list of {az_deg, alt_deg, reading | luminance_cdm2}. Readings are averaged over
    altitude AND over sign per |azimuth| (the correction is 1D along azimuth and symmetric about
    centre). Measuring both sides therefore buys noise averaging, not resolution — its real value
    is that azimuth_asymmetry() can then check whether the symmetry it assumes actually holds.

    Returns (az_full, gain_fit, correction) as numpy arrays over az 0..105°:
      - gain_fit:   relative luminance, 1.0 at center (az=0), lower toward the edges
      - correction: min(gain)/gain — 1.0 (full drive) at the dimmest/outermost az and < 1.0
        (darker) at center. Multiply requested contrast by this: it attenuates the bright center
        down to the dim edge so delivered luminance is uniform and never clips.
    """
    az_vals = defaultdict(list)
    for m in measurements:
        az_vals[abs(float(m["az_deg"]))].append(_reading(m))

    az_arr  = np.array(sorted(az_vals.keys()))
    lum_arr = np.array([np.mean(az_vals[az]) for az in az_arr])

    # Normalize: gain = 1.0 at center (az=0)
    center_lum = lum_arr[az_arr == 0][0] if 0 in az_arr else lum_arr[0]
    gain = lum_arr / center_lum

    # Fit smooth spline (needs ≥4 points), else linear-interpolate
    az_full = np.linspace(0, 105, 211)
    if len(az_arr) >= 4:
        spl = UnivariateSpline(az_arr, gain, s=0.01, k=3, ext='extrapolate')
        gain_fit = np.clip(spl(az_full), 0.05, 1.0)
    else:
        gain_fit = np.interp(az_full, az_arr, gain)

    # Equalize by attenuating the bright center DOWN to the dim outermost azimuth: correction is
    # 1.0 (full drive) at the dimmest/outermost az and < 1 (darker) toward center, so delivered
    # luminance is uniform and never exceeds the panel range (no clipping).
    gf = np.maximum(gain_fit, 0.05)
    correction = np.min(gf) / gf
    return az_full, gain_fit, correction


def save_luminance_cal(az_full, gain_fit, correction, source_file=None, cal_dir=None):
    """Write luminance_cal_<date>.yaml and update the luminance_cal_latest.yaml symlink.
    compute_warp_map.py `--lum-mode empirical` re-injects this into warp_map.npz on every build,
    so the measured correction survives warp regeneration. Returns the written file path."""
    cal_dir = Path(cal_dir or CAL_DIR)
    # Timestamped to the MINUTE, matching the rig_geometry_YYYYmmdd_HHMM convention. Keying on the
    # date alone meant every run on a given day silently overwrote the previous one, so a session
    # of successive fits left exactly one file and nothing to compare or fall back to.
    out = cal_dir / f"luminance_cal_{datetime.now().strftime('%Y%m%d_%H%M')}.yaml"
    cal_data = {
        'date':        date.today().isoformat(),
        'source_file': str(source_file) if source_file else None,
        'az_degrees':  [float(x) for x in az_full],
        'gain':        [float(x) for x in gain_fit],
        'correction':  [float(x) for x in correction],
        'note': ('gain: relative luminance (1.0 at center). '
                 'correction: multiply stimulus contrast by this to equalize.'),
    }
    out.write_text(yaml.dump(cal_data, default_flow_style=False))

    latest = cal_dir / "luminance_cal_latest.yaml"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(out.name)
    return out


def save_luminance_measurements(measurements, patch=None, method=None, source=None,
                                cal_dir=None):
    """Write the RAW readings to luminance_measurements_<ts>.yaml before anything is fitted.

    The cal file holds a 211-point interpolated curve; these are the handful of numbers actually
    taken off the meter. Keeping them means a fit can be redone later with a different spline,
    a bad point can be dropped without re-measuring, and the record says under what conditions
    the light was measured — which matters because the gain is measured at FULL drive and then
    applied at partial drive, an assumption only the raw record lets you revisit.

    The format is the one main() already reads, so `python fit_luminance_correction.py <file>`
    re-fits a saved measurement set offline. Returns the written path."""
    cal_dir = Path(cal_dir or CAL_DIR)
    out = cal_dir / f"luminance_measurements_{datetime.now().strftime('%Y%m%d_%H%M')}.yaml"
    rows = []
    for m in measurements:
        row = {"az_deg": float(m["az_deg"]), "reading": _reading(m)}
        if m.get("alt_deg") is not None:
            row["alt_deg"] = float(m["alt_deg"])
        rows.append(row)
    rows.sort(key=lambda r: r["az_deg"])
    doc = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": source or "setup-ui",
        "method": method,
        # The render conditions the readings were taken under. contrast/bg/apply_lum are fixed by
        # the measurement itself (max drive on black, correction OFF) — recorded so the file is
        # self-describing rather than relying on the reader knowing the convention.
        "patch": {"shape": "square", "contrast": 1, "bg_gray": 0, "apply_lum": False,
                  **(patch or {})},
        "measurements": rows,
    }
    out.write_text(yaml.dump(doc, default_flow_style=False, sort_keys=False))
    return out


def list_luminance_cals(cal_dir=None):
    """Saved luminance cal files, newest first, excluding the `latest` symlink. Returns a list of
    {name, mtime, is_latest} — is_latest marks which file luminance_cal_latest.yaml resolves to,
    i.e. the one a warp rebuild with --lum-mode empirical would actually pick up."""
    cal_dir = Path(cal_dir or CAL_DIR)
    latest = cal_dir / "luminance_cal_latest.yaml"
    target = latest.resolve().name if latest.exists() else None
    out = []
    for f in cal_dir.glob("luminance_cal_*.yaml"):
        if f.name == "luminance_cal_latest.yaml":
            continue
        out.append({"name": f.name, "mtime": f.stat().st_mtime, "is_latest": f.name == target,
                    "source": "theoretical" if f.stem.endswith("_theoretical") else "measured"})
    return sorted(out, key=lambda d: d["mtime"], reverse=True)


def select_luminance_cal(name, cal_dir=None):
    """Point luminance_cal_latest.yaml at a saved cal file so the next warp rebuild uses it.

    This is the whole mechanism for re-applying an earlier measurement: compute_warp_map's
    `--lum-mode empirical` reads _load_empirical_cal(), which only ever opens the `latest` symlink.
    Returns the resolved Path. Raises FileNotFoundError / ValueError on a bad name."""
    cal_dir = Path(cal_dir or CAL_DIR)
    safe = Path(name).name                       # no traversal: basename only, same as the geo picker
    if not safe.startswith("luminance_cal_") or not safe.endswith(".yaml"):
        raise ValueError(f"Not a luminance cal file: {name}")
    if safe == "luminance_cal_latest.yaml":
        raise ValueError("Pick a dated cal file, not the 'latest' pointer")
    target = cal_dir / safe
    if not target.exists():
        raise FileNotFoundError(f"No such cal file: {safe}")
    latest = cal_dir / "luminance_cal_latest.yaml"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(target.name)
    return target


def save_theoretical_cal(az, gain, geometry_name, cal_dir=None):
    """Write the geometry model's per-azimuth gain as an intensity cal file, same format as a
    measured one, so every rig has an intensity calibration before anything is measured.

    Named after the geometry it came from (rig_geometry_<stamp>.yaml ->
    luminance_cal_<stamp>_theoretical.yaml), so rebuilding the same geometry rewrites the same
    file instead of piling up copies. Returns the written path."""
    import re
    cal_dir = Path(cal_dir or CAL_DIR)
    cal_dir.mkdir(parents=True, exist_ok=True)
    m = re.search(r"(\d{8}_\d{4})", Path(geometry_name).stem)
    stamp = m.group(1) if m else datetime.now().strftime('%Y%m%d_%H%M')
    gain = np.asarray(gain, dtype=float)
    gf = np.maximum(gain, 0.05)
    correction = np.min(gf) / gf
    out = cal_dir / f"luminance_cal_{stamp}_theoretical.yaml"
    doc = {
        'date': date.today().isoformat(),
        'source': 'theoretical',
        'geometry_file': Path(geometry_name).name,
        'az_degrees': [float(x) for x in az],
        'gain': [float(x) for x in gain],
        'correction': [float(x) for x in correction],
        'note': ('THEORETICAL: cos(incidence) of the projector beam on the parabolic screen, '
                 'from the geometry file above. Not measured. gain: relative luminance '
                 '(1.0 at center). correction: min(gain)/gain.'),
    }
    out.write_text(yaml.dump(doc, default_flow_style=False, sort_keys=False))
    return out


# ── Contrast calibration: luminance vs drive level at ONE azimuth/altitude ──
#
# The along-azimuth cal above equalizes brightness ACROSS the screen. This one measures how the
# light at a single spot depends on the drive level (1.0 .. 0.0), including the black floor that
# a drive of 0 still leaves (DMD leakage + stray light). With it, a contrast value means the
# measured luminance contrast, not the drive ratio: on a black background the floor makes Weber
# contrast finite and large (a 10:1 light ratio is 900 %), instead of undefined.

def save_contrast_cal(measurements, patch=None, method=None, source=None, cal_dir=None):
    """Write contrast_cal_<ts>.yaml. measurements: [{level: 0..1, reading: float}].
    Returns the written path. Raises ValueError on unusable input."""
    cal_dir = Path(cal_dir or CAL_DIR)
    cal_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for m in measurements:
        lv, rd = float(m["level"]), float(m["reading"])
        if not 0.0 <= lv <= 1.0:
            raise ValueError(f"level {lv} outside 0..1")
        rows.append({"level": lv, "reading": rd})
    rows.sort(key=lambda r: r["level"], reverse=True)
    levels = [r["level"] for r in rows]
    if len(rows) < 2 or max(levels) < 1.0 or min(levels) > 0.0:
        raise ValueError("need readings at level 1.0 and level 0.0 (plus any in between)")
    doc = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "kind": "contrast",
        "source": source or "setup-ui",
        "method": method,
        # Conditions: a raw square (luminance correction OFF) at one az/alt on a black field.
        "patch": {"shape": "square", "bg_gray": 0, "apply_lum": False, **(patch or {})},
        "measurements": rows,
        "note": ("reading = meter value at the patch for each drive level; level 0.0 is the "
                 "black floor. Units cancel (only ratios are used)."),
    }
    out = cal_dir / f"contrast_cal_{datetime.now().strftime('%Y%m%d_%H%M')}.yaml"
    out.write_text(yaml.dump(doc, default_flow_style=False, sort_keys=False))
    return out


def list_contrast_cals(cal_dir=None):
    """Saved contrast cal files, newest first: [{name, mtime, az_deg, alt_deg, ratio}], where
    ratio = reading(1.0) / reading(0.0) (inf when the floor read 0)."""
    cal_dir = Path(cal_dir or CAL_DIR)
    out = []
    for f in cal_dir.glob("contrast_cal_*.yaml"):
        entry = {"name": f.name, "mtime": f.stat().st_mtime}
        try:
            d = load_contrast_cal(f.name, cal_dir)
            entry.update(az_deg=d["patch"].get("az_deg"), alt_deg=d["patch"].get("alt_deg"),
                         ratio=d["ratio"])
        except Exception as e:
            entry["error"] = str(e)
        out.append(entry)
    return sorted(out, key=lambda d: d["mtime"], reverse=True)


def load_contrast_cal(name, cal_dir=None):
    """Read and validate one contrast cal. Returns {name, patch, levels, readings, ratio} with
    levels ascending. Raises FileNotFoundError / ValueError."""
    cal_dir = Path(cal_dir or CAL_DIR)
    safe = Path(name).name
    if not (safe.startswith("contrast_cal_") and safe.endswith(".yaml")):
        raise ValueError(f"Not a contrast cal file: {name}")
    path = cal_dir / safe
    if not path.exists():
        raise FileNotFoundError(f"No contrast cal {safe} in {cal_dir}")
    d = yaml.safe_load(path.read_text()) or {}
    rows = sorted(((float(m["level"]), float(m["reading"])) for m in d.get("measurements", [])))
    if len(rows) < 2:
        raise ValueError(f"{safe}: fewer than 2 readings")
    levels = [r[0] for r in rows]
    readings = [r[1] for r in rows]
    if levels[0] > 0.0 or levels[-1] < 1.0:
        raise ValueError(f"{safe}: needs readings at level 0.0 and 1.0")
    ratio = readings[-1] / readings[0] if readings[0] > 0 else float("inf")
    return {"name": safe, "patch": d.get("patch") or {}, "levels": levels,
            "readings": readings, "ratio": ratio}


def inject_into_warp(az_full, gain_fit, correction, warp_map=WARP_MAP):
    """Convenience for standalone/local use: write the empirical arrays straight into a local
    warp_map.npz (the setup-UI flow instead regenerates the warp, which re-injects from the yaml).
    Returns True if a warp_map.npz was present and updated."""
    warp_map = Path(warp_map)
    if not warp_map.exists():
        return False
    existing = dict(np.load(warp_map))
    existing['lum_az_empirical']         = az_full
    existing['lum_gain_empirical']       = gain_fit
    existing['lum_correction_empirical'] = correction
    existing['lum_correction_mode']      = 'empirical'
    np.savez(warp_map, **existing)
    return True


def _plot(az_measured, gain_measured, az_full, gain_fit, correction):
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle("Luminance Correction", fontsize=13)

    ax = axes[0]
    ax.scatter(az_measured, gain_measured, s=80, zorder=5, label='Measured')
    ax.plot(az_full, gain_fit, 'b-', linewidth=2, label='Fitted')
    ax.set_xlabel("Azimuth (degrees)"); ax.set_ylabel("Relative luminance (gain)")
    ax.set_title("Luminance gain"); ax.legend(); ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.plot(az_full, correction, 'r-', linewidth=2)
    ax.plot(-az_full, correction, 'r-', linewidth=2)
    ax.set_xlabel("Azimuth (degrees)"); ax.set_ylabel("Correction factor")
    ax.set_title("Contrast correction (1/gain)"); ax.grid(True, alpha=0.3)
    ax.axhline(1.0, color='gray', linestyle='--', alpha=0.5)

    plt.tight_layout()
    plot_out = CAL_DIR / f"luminance_correction_{date.today().isoformat()}.png"
    plt.savefig(plot_out, dpi=120, bbox_inches='tight')
    print(f"Plot saved: {plot_out}")
    plt.show()


def main(measurement_file=None):
    if measurement_file is None:
        files = sorted(CAL_DIR.glob("luminance_measurements_*.yaml"))
        if not files:
            print("No measurement file found. Run the intensity-cal measurement first.")
            sys.exit(1)
        measurement_file = files[-1]
        print(f"Using: {measurement_file}")

    data = yaml.safe_load(Path(measurement_file).read_text())
    measurements = data.get('measurements') if isinstance(data, dict) else data
    if not measurements:
        print("No measurements in file.")
        sys.exit(1)

    az_full, gain_fit, correction = fit_luminance(measurements)

    if inject_into_warp(az_full, gain_fit, correction):
        print("Updated warp_map.npz with empirical luminance correction")
    else:
        print("warp_map.npz not found — wrote cal file only (regenerate the warp to apply)")

    out = save_luminance_cal(az_full, gain_fit, correction, source_file=measurement_file)
    print(f"Saved: {out}")

    # Measured points for the plot (same averaging as the fit)
    az_vals = defaultdict(list)
    for m in measurements:
        az_vals[abs(float(m['az_deg']))].append(_reading(m))
    az_measured = np.array(sorted(az_vals.keys()))
    center = az_vals[0.0] if 0.0 in az_vals else az_vals[az_measured[0]]
    gain_measured = np.array([np.mean(az_vals[a]) for a in az_measured]) / np.mean(center)
    _plot(az_measured, gain_measured, az_full, gain_fit, correction)


if __name__ == "__main__":
    mfile = sys.argv[1] if len(sys.argv) > 1 else None
    main(mfile)
