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
from datetime import date
from collections import defaultdict
from scipy.interpolate import UnivariateSpline

CAL_DIR  = Path(__file__).parent
WARP_MAP = CAL_DIR / "warp_map.npz"


def _reading(m):
    """One measurement's light value. Accepts the neutral `reading` key (PM100D power / any
    linear-in-luminance meter) or the legacy `luminance_cdm2` (photometer)."""
    if "reading" in m:
        return float(m["reading"])
    return float(m["luminance_cdm2"])


def fit_luminance(measurements):
    """Fit a per-azimuth luminance correction from raw measurements.

    measurements: list of {az_deg, alt_deg, reading | luminance_cdm2}. Readings are averaged over
    altitude per |azimuth| (the correction is 1D along azimuth).

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


def save_luminance_cal(az_full, gain_fit, correction, source_file=None, cal_dir=CAL_DIR):
    """Write luminance_cal_<date>.yaml and update the luminance_cal_latest.yaml symlink.
    compute_warp_map.py `--lum-mode empirical` re-injects this into warp_map.npz on every build,
    so the measured correction survives warp regeneration. Returns the written file path."""
    cal_dir = Path(cal_dir)
    out = cal_dir / f"luminance_cal_{date.today().isoformat()}.yaml"
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
