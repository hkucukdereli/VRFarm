"""
validate_calibration.py

Displays validation patterns on the projector to visually verify the warp map.

Run on mozzarella:
  DISPLAY=:0 python validate_calibration.py [--pattern PATTERN]

Patterns:
  grid        - azimuth/altitude grid lines (default)
  azimuths    - vertical lines at ±80°, ±40°, 0°
  contrast    - equal-contrast patches at multiple azimuths
  stim        - test stimulus at a specific location

Press Q or ESC to quit. Press SPACE to cycle patterns.
"""

import sys
import argparse
import numpy as np
from pathlib import Path

CAL_DIR  = Path(__file__).parent
WARP_MAP = CAL_DIR / "warp_map.npz"
GEO_FILE = CAL_DIR / "rig_geometry.yaml"


def load_resources():
    import yaml
    with open(GEO_FILE) as f:
        geo = yaml.safe_load(f)
    data = np.load(WARP_MAP)
    return geo, data


def run(pattern='grid'):
    try:
        from psychopy import visual, event, core
    except ImportError:
        print("PsychoPy not found.")
        sys.exit(1)

    geo, warp = load_resources()
    res_w, res_h = geo['projector']['resolution']

    az_samples  = warp['az_samples']
    alt_samples = warp['alt_samples']
    px_map      = warp['px_from_az']
    py_map      = warp['py_from_az']
    valid_map   = warp['valid_map']

    win = visual.Window(
        size=[res_w, res_h],
        fullscr=True,
        color=[-0.5, -0.5, -0.5],   # dark gray background
        colorSpace='rgb',
        units='pix',
        allowGUI=False,
    )

    patterns = ['grid', 'azimuths', 'contrast', 'stim']
    pat_idx = patterns.index(pattern) if pattern in patterns else 0
    stims = []

    def clear_stims():
        for s in stims:
            del s
        stims.clear()

    def draw_grid():
        clear_stims()
        # Draw azimuth isolines every 20°
        for az_target in np.arange(-100, 101, 20):
            idx = np.argmin(np.abs(az_samples - az_target))
            valid_col = valid_map[:, idx]
            if not valid_col.any():
                continue
            px_col = px_map[valid_col, idx] - res_w//2
            py_col = res_h//2 - py_map[valid_col, idx]
            verts = list(zip(px_col.tolist(), py_col.tolist()))
            if len(verts) >= 2:
                line = visual.ShapeStim(
                    win, vertices=verts, lineColor='white',
                    lineWidth=1 if az_target % 40 else 2,
                    closeShape=False, fillColor=None, units='pix'
                )
                stims.append(line)
        # Draw altitude isolines every 10°
        for alt_target in alt_samples[::max(1, len(alt_samples)//5)]:
            idx = np.argmin(np.abs(alt_samples - alt_target))
            valid_row = valid_map[idx, :]
            if not valid_row.any():
                continue
            px_row = px_map[idx, valid_row] - res_w//2
            py_row = res_h//2 - py_map[idx, valid_row]
            verts = list(zip(px_row.tolist(), py_row.tolist()))
            if len(verts) >= 2:
                line = visual.ShapeStim(
                    win, vertices=verts, lineColor='cyan',
                    lineWidth=1, closeShape=False, fillColor=None, units='pix'
                )
                stims.append(line)
        # Labels
        for az_target in [-80, -40, 0, 40, 80]:
            idx = np.argmin(np.abs(az_samples - az_target))
            mid_alt = len(alt_samples)//2
            if valid_map[mid_alt, idx]:
                px = px_map[mid_alt, idx] - res_w//2
                py = res_h//2 - py_map[mid_alt, idx] + 20
                txt = visual.TextStim(win, text=f"{az_target}°",
                    pos=(px, py), height=18, color='yellow', units='pix')
                stims.append(txt)

    def draw_azimuths():
        clear_stims()
        targets = [(-80, 'red'), (-40, 'orange'), (0, 'white'),
                   (40, 'orange'), (80, 'red')]
        for az_target, col in targets:
            idx = np.argmin(np.abs(az_samples - az_target))
            valid_col = valid_map[:, idx]
            if not valid_col.any():
                continue
            px_col = px_map[valid_col, idx] - res_w//2
            py_col = res_h//2 - py_map[valid_col, idx]
            verts = list(zip(px_col.tolist(), py_col.tolist()))
            if len(verts) >= 2:
                line = visual.ShapeStim(
                    win, vertices=verts, lineColor=col,
                    lineWidth=3, closeShape=False, fillColor=None, units='pix'
                )
                stims.append(line)
                mid = len(px_col)//2
                txt = visual.TextStim(win, text=f"{az_target}°",
                    pos=(px_col[mid]+15, py_col[mid]),
                    height=22, color=col, units='pix')
                stims.append(txt)

    def draw_contrast():
        """White patches at equal desired contrast — should look equally bright after correction."""
        clear_stims()
        # Load correction if available
        if 'lum_correction_empirical' in warp.files if hasattr(warp, 'files') else False:
            correction_az = warp['lum_az_empirical']
            correction    = warp['lum_correction_empirical']
        else:
            correction_az = warp['lum_az']
            correction    = np.ones(len(correction_az))  # no correction

        target_contrast = 0.5
        test_azs = [-80, -60, -40, -20, 0, 20, 40, 60, 80]
        alt_mid  = alt_samples[len(alt_samples)//2]

        for az in test_azs:
            idx_az  = np.argmin(np.abs(az_samples - az))
            idx_alt = np.argmin(np.abs(alt_samples - alt_mid))
            if not valid_map[idx_alt, idx_az]:
                continue

            # Apply correction
            corr = float(np.interp(abs(az), correction_az, correction))
            corrected = min(target_contrast * corr, 1.0)
            brightness = -1.0 + 2.0 * corrected   # PsychoPy -1 to 1

            px = px_map[idx_alt, idx_az] - res_w//2
            py = res_h//2 - py_map[idx_alt, idx_az]

            patch = visual.Rect(win, width=60, height=60,
                pos=(px, py), fillColor=[brightness]*3,
                lineColor=None, units='pix')
            stims.append(patch)
            txt = visual.TextStim(win, text=f"{az}°",
                pos=(px, py - 45), height=16, color='white', units='pix')
            stims.append(txt)

    def draw_test_stim():
        """Show a test stimulus at 80° azimuth (typical experiment location)."""
        clear_stims()
        from psychopy import visual as vis
        # Background
        bg = vis.Rect(win, width=res_w, height=res_h,
                      fillColor=[-0.5]*3, lineColor=None, units='pix')
        stims.append(bg)
        # Stimulus at +80°
        for az_target in [80, -80]:
            idx_az  = np.argmin(np.abs(az_samples - az_target))
            idx_alt = np.argmin(np.abs(alt_samples - 49))   # 49° altitude
            if not valid_map[idx_alt, idx_az]:
                continue
            px = px_map[idx_alt, idx_az] - res_w//2
            py = res_h//2 - py_map[idx_alt, idx_az]
            stim = vis.Rect(win, width=80, height=80,
                pos=(px, py), fillColor=[1,1,1], lineColor=None, units='pix')
            stims.append(stim)
            txt = vis.TextStim(win, text=f"Az={az_target}°\nAlt=49°",
                pos=(px, py-55), height=18, color='yellow', units='pix')
            stims.append(txt)

    pat_fns = [draw_grid, draw_azimuths, draw_contrast, draw_test_stim]
    pat_fns[pat_idx]()

    instr = visual.TextStim(
        win, text="SPACE: next pattern  |  Q/ESC: quit",
        pos=(0, -res_h//2 + 25), height=16, color='gray', units='pix'
    )

    while True:
        for s in stims:
            s.draw()
        instr.draw()
        win.flip()

        keys = event.getKeys()
        for key in keys:
            if key in ('q', 'escape'):
                win.close()
                return
            elif key == 'space':
                pat_idx = (pat_idx + 1) % len(patterns)
                pat_fns[pat_idx]()

        core.wait(0.05)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate warp calibration")
    parser.add_argument('--pattern', default='grid',
                        choices=['grid', 'azimuths', 'contrast', 'stim'])
    args = parser.parse_args()
    run(args.pattern)
