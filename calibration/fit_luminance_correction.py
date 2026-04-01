"""
fit_luminance_correction.py

Fits a luminance correction curve from photometer measurements and
updates the warp_map.npz with the empirical correction.

Run after display_test_patches.py:
  python fit_luminance_correction.py luminance_measurements_2026-03-28.yaml

The correction gain G(az) is defined such that:
  actual_luminance(az) = intended_luminance × G(az)

To display a stimulus at a target contrast C at azimuth az, set:
  stimulus_contrast = C / G(az)   (clamped to [0, 1])
"""

import sys
import yaml
import numpy as np
from pathlib import Path
from datetime import date
import matplotlib.pyplot as plt
from scipy.interpolate import UnivariateSpline

CAL_DIR  = Path(__file__).parent
WARP_MAP = CAL_DIR / "warp_map.npz"


def main(measurement_file=None):
    if measurement_file is None:
        # Find most recent measurement file
        files = sorted(CAL_DIR.glob("luminance_measurements_*.yaml"))
        if not files:
            print("No measurement file found. Run display_test_patches.py first.")
            sys.exit(1)
        measurement_file = files[-1]
        print(f"Using: {measurement_file}")

    with open(measurement_file) as f:
        data = yaml.safe_load(f)

    measurements = data['measurements']
    if not measurements:
        print("No measurements in file.")
        sys.exit(1)

    # Average over altitudes for each azimuth (luminance correction is 1D along azimuth)
    from collections import defaultdict
    az_vals = defaultdict(list)
    for m in measurements:
        az_vals[abs(m['az_deg'])].append(m['luminance_cdm2'])

    az_arr  = np.array(sorted(az_vals.keys()))
    lum_arr = np.array([np.mean(az_vals[az]) for az in az_arr])

    # Normalize: gain = 1.0 at center (az=0)
    center_lum = lum_arr[az_arr == 0][0] if 0 in az_arr else lum_arr[0]
    gain = lum_arr / center_lum

    # Fit smooth spline
    az_full = np.linspace(0, 105, 211)
    if len(az_arr) >= 4:
        spl = UnivariateSpline(az_arr, gain, s=0.01, k=3, ext='extrapolate')
        gain_fit = np.clip(spl(az_full), 0.05, 1.0)
    else:
        # Not enough points for spline, use linear interpolation
        gain_fit = np.interp(az_full, az_arr, gain)

    # Correction factor: to achieve target contrast, multiply by 1/gain
    # Clip to avoid division by zero
    correction = 1.0 / np.maximum(gain_fit, 0.05)
    correction = correction / correction[0]   # normalize to 1 at center

    # Update warp_map.npz
    existing = dict(np.load(WARP_MAP))
    existing['lum_az_empirical']         = az_full
    existing['lum_gain_empirical']       = gain_fit
    existing['lum_correction_empirical'] = correction

    np.savez(WARP_MAP, **existing)
    print(f"Updated warp_map.npz with empirical luminance correction")

    # Also save standalone calibration file
    out = CAL_DIR / f"luminance_cal_{date.today().isoformat()}.yaml"
    cal_data = {
        'date': date.today().isoformat(),
        'source_file': str(measurement_file),
        'az_degrees':    az_full.tolist(),
        'gain':          gain_fit.tolist(),
        'correction':    correction.tolist(),
        'note': ('gain: relative luminance (1.0 at center). '
                 'correction: multiply stimulus contrast by this to equalize.')
    }
    with open(out, 'w') as f:
        yaml.dump(cal_data, f, default_flow_style=False)
    print(f"Saved: {out}")

    # Update symlink to latest
    latest = CAL_DIR / "luminance_cal_latest.yaml"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(out.name)
    print(f"Updated symlink: {latest} → {out.name}")

    # Plot
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle("Luminance Correction", fontsize=13)

    ax = axes[0]
    ax.scatter(az_arr, gain, s=80, zorder=5, label='Measured')
    ax.plot(az_full, gain_fit, 'b-', linewidth=2, label='Fitted')
    ax.set_xlabel("Azimuth (degrees)")
    ax.set_ylabel("Relative luminance (gain)")
    ax.set_title("Luminance gain")
    ax.legend(); ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.plot(az_full, correction, 'r-', linewidth=2)
    ax.plot(-az_full, correction, 'r-', linewidth=2)
    ax.set_xlabel("Azimuth (degrees)")
    ax.set_ylabel("Correction factor")
    ax.set_title("Contrast correction (1/gain)")
    ax.grid(True, alpha=0.3)
    ax.axhline(1.0, color='gray', linestyle='--', alpha=0.5)

    plt.tight_layout()
    plot_out = CAL_DIR / f"luminance_correction_{date.today().isoformat()}.png"
    plt.savefig(plot_out, dpi=120, bbox_inches='tight')
    print(f"Plot saved: {plot_out}")
    plt.show()


if __name__ == "__main__":
    mfile = sys.argv[1] if len(sys.argv) > 1 else None
    main(mfile)
