"""
mozzarella/stim_generator.py

Pre-generates all stimuli for a session at setup time.
Saves PsychoPy-ready stimulus parameters (position, size in pixels,
luminance-corrected contrast) to a numpy array.

Run before task.py, or called from task.py on startup.

Stimulus: white square on gray background.
  - Position determined by warp map (azimuth, altitude) → pixel coords
  - Size in pixels computed from visual angle at that screen location
  - Contrast adjusted for local luminance falloff
  - Background gray set by config

Output: {stim_dir}/{session_id}/stimuli.npz
  stim_list: array of N dicts (as structured array):
    - trial_idx, block_num, az_deg, alt_deg, contrast, px_x, px_y,
      px_size, corrected_contrast
  trial_sequence: array of trial indices in order
"""

import numpy as np
from pathlib import Path


# On Mac: stim/ → VRFarm/calibration (parent.parent)
# On Pi:  ~/rig/ → ~/rig/calibration (parent)
_here = Path(__file__).parent
CAL_DIR = (_here / 'calibration' if (_here / 'calibration').exists()
           else _here.parent / 'calibration')


def load_warp_map():
    warp_path = CAL_DIR / 'warp_map.npz'
    if not warp_path.exists():
        raise FileNotFoundError(
            f"Warp map not found at {warp_path}. "
            "Run calibration/compute_warp_map.py first.")
    return np.load(warp_path)


def az_alt_to_pixel(warp, az_deg, alt_deg):
    """
    Convert (azimuth, altitude) in degrees to projector pixel coordinates.
    Uses nearest-neighbour lookup in the forward map.

    Returns (px_x, px_y) as floats, or raises ValueError if out of range.
    """
    az_samples  = warp['az_samples']
    alt_samples = warp['alt_samples']
    px_map      = warp['px_from_az']
    py_map      = warp['py_from_az']
    valid_map   = warp['valid_map']

    az_idx  = int(np.argmin(np.abs(az_samples  - az_deg)))
    alt_idx = int(np.argmin(np.abs(alt_samples - alt_deg)))

    if not valid_map[alt_idx, az_idx]:
        raise ValueError(
            f"({az_deg}°, {alt_deg}°) is outside the screen coverage area.")

    return float(px_map[alt_idx, az_idx]), float(py_map[alt_idx, az_idx])


def visual_angle_to_pixels(az_deg, alt_deg, size_deg, warp, res=(1920, 1080)):
    """
    Convert a stimulus size in visual degrees to pixels at a given screen location.

    Approximation: compute the pixel distance between the target azimuth
    and (target_az + size_deg/2) at the same altitude. Use twice this as
    the pixel size. Accurate enough for our purposes.
    """
    try:
        px0, py0 = az_alt_to_pixel(warp, az_deg, alt_deg)
        px1, py1 = az_alt_to_pixel(warp, az_deg + size_deg / 2, alt_deg)
        px_size = abs(px1 - px0) * 2
        return max(4, int(px_size))
    except ValueError:
        # Fallback: assume uniform mapping at center
        # At 0°, pixel/degree ≈ res_w / (2 * azimuth_max_deg)
        px_per_deg = res[0] / (2 * 105.0)
        return max(4, int(size_deg * px_per_deg))


def get_luminance_correction(warp, az_deg):
    """
    Return luminance correction factor at az_deg.
    Uses empirical correction if available, falls back to theoretical.
    """
    if 'lum_az_empirical' in warp.files:
        az_arr  = warp['lum_az_empirical']
        corr    = warp['lum_correction_empirical']
    else:
        az_arr  = warp['lum_az']
        # theoretical correction: 1/gain
        gain = warp['lum_gain_theoretical']
        corr = 1.0 / np.maximum(gain, 0.05)
        corr = corr / corr[0]

    return float(np.interp(abs(az_deg), az_arr, corr))


def build_block_trial_list(config):
    """
    Build the ordered list of (trial_idx, block_num, az_deg, contrast)
    for the full session.

    - Blocks are fixed sequences of locations from config.session.block_sequence
    - Within each block, all trials are at the same location
    - Contrast is pre-shuffled within each block if multiple contrasts
    - Returns list of dicts
    """
    rng = np.random.default_rng()

    seq      = config.session.block_sequence
    n_blocks = len(seq)
    bsize    = config.session.block_size
    n_total  = config.session.n_trials

    # Contrasts and probabilities
    contrast_vals  = config.stimulus.contrast_values
    contrast_probs = config.stimulus.contrast_probs

    trials = []
    trial_idx = 0
    block_num = 0

    while trial_idx < n_total and block_num < 999:
        az = seq[block_num % n_blocks]

        # How many trials in this block
        remaining = n_total - trial_idx
        this_block = min(bsize if bsize else n_total, remaining)

        # Pre-shuffle contrasts for this block
        n_each = [max(1, round(p * this_block)) for p in contrast_probs]
        # Adjust to exactly this_block
        while sum(n_each) > this_block:
            n_each[np.argmax(n_each)] -= 1
        while sum(n_each) < this_block:
            n_each[np.argmax(contrast_probs)] += 1

        block_contrasts = []
        for c, n in zip(contrast_vals, n_each):
            block_contrasts.extend([c] * n)
        rng.shuffle(block_contrasts)

        for c in block_contrasts:
            trials.append({
                'trial_idx': trial_idx,
                'block_num': block_num,
                'az_deg':    float(az),
                'alt_deg':   float(config.stimulus.altitude_deg),
                'contrast':  float(c),
            })
            trial_idx += 1

        block_num += 1

    return trials


def generate_stimuli(config, warp, output_dir: Path) -> Path:
    """
    Generate the full trial stimulus list for a session.

    For each trial, computes:
      - pixel position (px_x, px_y) in projector coordinates
      - pixel size
      - luminance-corrected contrast
      - background gray value (PsychoPy -1 to 1)

    Saves to {output_dir}/stimuli.npz and returns the path.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / 'stimuli.npz'

    print(f"Generating stimuli for session {config.session_id}...")

    trials = build_block_trial_list(config)
    n = len(trials)

    # Arrays to store
    trial_idx  = np.zeros(n, dtype=np.int32)
    block_num  = np.zeros(n, dtype=np.int32)
    az_deg     = np.zeros(n, dtype=np.float32)
    alt_deg    = np.zeros(n, dtype=np.float32)
    contrast   = np.zeros(n, dtype=np.float32)
    px_x       = np.zeros(n, dtype=np.float32)
    px_y       = np.zeros(n, dtype=np.float32)
    px_size    = np.zeros(n, dtype=np.int32)
    corr_contrast = np.zeros(n, dtype=np.float32)

    res = tuple(config.hardware.camera_resolution)   # use projector res instead
    proj_res = (1920, 1080)

    bg = config.stimulus.background_gray

    for i, t in enumerate(trials):
        trial_idx[i] = t['trial_idx']
        block_num[i] = t['block_num']
        az_deg[i]    = t['az_deg']
        alt_deg[i]   = t['alt_deg']
        contrast[i]  = t['contrast']

        # Pixel position
        try:
            px, py = az_alt_to_pixel(warp, t['az_deg'], t['alt_deg'])
            px_x[i] = px
            px_y[i] = py
        except ValueError as e:
            print(f"  Warning trial {i}: {e} — using screen center")
            px_x[i] = proj_res[0] / 2
            px_y[i] = proj_res[1] / 2

        # Pixel size
        px_size[i] = visual_angle_to_pixels(
            t['az_deg'], t['alt_deg'],
            config.stimulus.size_deg, warp, proj_res)

        # Luminance-corrected contrast
        # Stimulus luminance = background + contrast * (max - background)
        # After correction: actual_contrast = desired / gain
        lum_corr = get_luminance_correction(warp, t['az_deg'])
        raw_c = min(t['contrast'] * lum_corr, 1.0)
        corr_contrast[i] = raw_c

    np.savez(out_path,
             trial_idx=trial_idx,
             block_num=block_num,
             az_deg=az_deg,
             alt_deg=alt_deg,
             contrast=contrast,
             px_x=px_x,
             px_y=px_y,
             px_size=px_size,
             corr_contrast=corr_contrast,
             background_gray=np.array([bg]),
             n_trials=np.array([n]))

    print(f"  {n} trials across {block_num[-1]+1} blocks")
    print(f"  Azimuth range: {az_deg.min():.0f}° to {az_deg.max():.0f}°")
    print(f"  Pixel sizes: {px_size.min()}–{px_size.max()} px")
    print(f"  Contrast range: {corr_contrast.min():.3f}–{corr_contrast.max():.3f} (after correction)")
    print(f"  Saved: {out_path}")

    return out_path


def load_stimuli(stim_path: Path) -> dict:
    """Load pre-generated stimuli from npz file."""
    data = np.load(stim_path)
    return {k: data[k] for k in data.files}
