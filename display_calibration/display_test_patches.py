"""
display_test_patches.py

Displays gray patches at known azimuth/altitude positions so you can
measure their luminance with a photometer.

Run on mozzarella with the projector on:
  DISPLAY=:0 python display_test_patches.py

Controls:
  LEFT/RIGHT arrow: step through azimuths
  UP/DOWN arrow:    step through altitudes
  SPACE:            record measurement (prompts for photometer reading)
  S:                save measurements
  Q or ESC:         quit

Output: luminance_measurements_YYYY-MM-DD.yaml
"""

import sys
import yaml
import numpy as np
from pathlib import Path
from datetime import date

# Test azimuths to measure (degrees)
TEST_AZIMUTHS  = [0, 20, 40, 60, 80, 100]
TEST_ALTITUDES = [25, 49, 74]   # bottom, center, top of screen

CAL_DIR  = Path(__file__).parent
WARP_MAP = CAL_DIR / "warp_map.npz"
GEO_FILE = CAL_DIR / "rig_geometry.yaml"


def load_forward_map():
    data = np.load(WARP_MAP)
    return {
        'px_map':      data['px_from_az'],
        'py_map':      data['py_from_az'],
        'az_samples':  data['az_samples'],
        'alt_samples': data['alt_samples'],
        'valid':       data['valid_map'],
    }


def az_alt_to_pixel(fwd, az_deg, alt_deg):
    """Interpolate pixel coordinates for a given azimuth/altitude."""
    az_idx  = np.argmin(np.abs(fwd['az_samples']  - az_deg))
    alt_idx = np.argmin(np.abs(fwd['alt_samples'] - alt_deg))
    if not fwd['valid'][alt_idx, az_idx]:
        return None, None
    return int(fwd['px_map'][alt_idx, az_idx]), int(fwd['py_map'][alt_idx, az_idx])


def main():
    try:
        from psychopy import visual, event, core
    except ImportError:
        print("PsychoPy not found. Install with: pip install psychopy")
        sys.exit(1)

    with open(GEO_FILE) as f:
        geo = yaml.safe_load(f)

    if not WARP_MAP.exists():
        print("warp_map.npz not found. Run compute_warp_map.py first.")
        sys.exit(1)

    fwd = load_forward_map()
    res_w, res_h = geo['projector']['resolution']
    bg_gray = 0.0   # neutral gray in PsychoPy units (-1 to 1)

    # Patch size in pixels (small, ~0.5° visual angle at screen center)
    PATCH_PX = 30

    win = visual.Window(
        size=[res_w, res_h],
        fullscr=True,
        color=[bg_gray] * 3,
        colorSpace='rgb',
        units='pix',
        allowGUI=False,
    )

    patch = visual.Rect(
        win,
        width=PATCH_PX, height=PATCH_PX,
        fillColor=[1, 1, 1],
        lineColor=None,
        units='pix',
    )

    label = visual.TextStim(
        win, text='', pos=(0, -res_h//2 + 40),
        color='white', height=20, units='pix'
    )

    measurements = []
    az_i   = 0
    alt_i  = 1   # start at center altitude

    def update_display():
        az  = TEST_AZIMUTHS[az_i]
        alt = TEST_ALTITUDES[alt_i]
        px, py = az_alt_to_pixel(fwd, az, alt)
        win.color = [bg_gray] * 3

        if px is not None:
            # PsychoPy pixel origin is center, y flipped
            patch.pos = (px - res_w//2, res_h//2 - py)
            patch.draw()
            label.text = (f"Az={az}°  Alt={alt}°  Pixel=({px},{py})\n"
                          f"SPACE=record  S=save  Q=quit")
        else:
            label.text = f"Az={az}°  Alt={alt}° — OUT OF RANGE"
        label.draw()
        win.flip()

    update_display()

    print("Luminance measurement tool")
    print("  LEFT/RIGHT: change azimuth")
    print("  UP/DOWN:    change altitude")
    print("  SPACE:      record photometer reading")
    print("  S:          save measurements")
    print("  Q/ESC:      quit\n")

    while True:
        keys = event.getKeys()
        for key in keys:
            if key in ('q', 'escape'):
                win.close()
                if measurements:
                    _save(measurements)
                return

            elif key == 'right':
                az_i = min(az_i + 1, len(TEST_AZIMUTHS) - 1)
                update_display()

            elif key == 'left':
                az_i = max(az_i - 1, 0)
                update_display()

            elif key == 'up':
                alt_i = min(alt_i + 1, len(TEST_ALTITUDES) - 1)
                update_display()

            elif key == 'down':
                alt_i = max(alt_i - 1, 0)
                update_display()

            elif key == 'space':
                win.color = [-1, -1, -1]   # black screen while entering
                win.flip()
                az  = TEST_AZIMUTHS[az_i]
                alt = TEST_ALTITUDES[alt_i]
                reading = input(f"  Photometer reading at Az={az}°, Alt={alt}° (cd/m²): ")
                try:
                    val = float(reading)
                    measurements.append({
                        'az_deg': az, 'alt_deg': alt,
                        'luminance_cdm2': val
                    })
                    print(f"  Recorded: {val} cd/m²")
                except ValueError:
                    print("  Invalid input, skipped.")
                update_display()

            elif key == 's':
                _save(measurements)
                print("Saved. Continue measuring or press Q to quit.")

        core.wait(0.05)


def _save(measurements):
    out = CAL_DIR / f"luminance_measurements_{date.today().isoformat()}.yaml"
    with open(out, 'w') as f:
        yaml.dump({'measurements': measurements}, f)
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
