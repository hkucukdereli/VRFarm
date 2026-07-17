"""
shared/stim_generator.py

Pre-generates all stimuli for a session. Saves pixel positions,
sizes, luminance-corrected contrasts, and durations to NPZ + YAML.

Called by Leader Pi at deploy time. NPZ uploaded to Follower.
YAML trial table shared with all consumers (UI, followers).

Output arrays per trial:
  trial_idx, block_num, stim_az_deg, stim_alt_deg, contrast,
  px_x, px_y, px_size, corr_contrast, duration_s, bg_gray
"""

import numpy as np
import yaml
from pathlib import Path


def _sample_durations(rng, cfg, size):
    """Sample durations from config: scalar (fixed) or [min, max] (uniform random).
    Returns float32 array of given size.
    """
    if isinstance(cfg, list) and len(cfg) == 2:
        return rng.uniform(cfg[0], cfg[1], size=size).astype(np.float32)
    val = float(cfg) if isinstance(cfg, (int, float)) else 0.0
    return np.full(size, val, dtype=np.float32)


def az_alt_to_pixel(warp, az_deg, alt_deg):
    """Convert (azimuth, altitude) degrees to projector pixel coords.
    Returns (px_x, px_y) or raises ValueError if out of range.
    """
    az_samples = warp["az_samples"]
    alt_samples = warp["alt_samples"]
    px_map = warp["px_from_az"]
    py_map = warp["py_from_az"]

    az_idx = int(np.argmin(np.abs(az_samples - az_deg)))
    alt_idx = int(np.argmin(np.abs(alt_samples - alt_deg)))

    px = float(px_map[alt_idx, az_idx])
    py = float(py_map[alt_idx, az_idx])

    if np.isnan(px) or np.isnan(py):
        raise ValueError(
            f"({az_deg}, {alt_deg}) is outside the screen coverage area.")

    return px, py


def visual_angle_to_pixels(az_deg, alt_deg, size_deg, warp,
                           res=(1920, 1080)):
    """Convert stimulus size in visual degrees to pixels at a location."""
    try:
        px0, _ = az_alt_to_pixel(warp, az_deg, alt_deg)
        px1, _ = az_alt_to_pixel(warp, az_deg + size_deg / 2, alt_deg)
        px_size = abs(px1 - px0) * 2
        return max(4, int(px_size))
    except ValueError:
        px_per_deg = res[0] / (2 * 105.0)
        return max(4, int(size_deg * px_per_deg))


def get_luminance_correction(warp, az_deg):
    """Return luminance correction factor at az_deg."""
    if "lum_az_empirical" in (warp.files if hasattr(warp, "files")
                              else warp.keys()):
        az_arr = warp["lum_az_empirical"]
        corr = warp["lum_correction_empirical"]
    else:
        az_arr = warp["lum_az"]
        gain = warp["lum_gain_theoretical"]
        corr = 1.0 / np.maximum(gain, 0.05)
        corr = corr / corr[0]
    return float(np.interp(abs(az_deg), az_arr, corr))


def build_block_trial_list(task_config: dict) -> list[dict]:
    """Build ordered list of (trial_idx, block_num, az_deg, contrast)."""
    rng = np.random.default_rng()

    sess = task_config["session"]
    stim = task_config["stimulus"]
    seq = sess["block_sequence"]
    n_seq = len(seq)
    bsize = sess.get("block_size", 25)
    n_blocks = sess.get("n_blocks", sess.get("n_trials", 150) // max(bsize, 1))
    n_total = n_blocks * bsize

    contrast_cfg = stim["contrast"]
    contrast_vals = [float(x) for x in contrast_cfg["values"]]
    contrast_probs = [float(x) for x in contrast_cfg["proportions"]]
    s = sum(contrast_probs)
    contrast_probs = [p / s for p in contrast_probs]

    trials = []
    trial_idx = 0
    block_num = 0

    while trial_idx < n_total and block_num < 999:
        az = seq[block_num % n_seq]
        remaining = n_total - trial_idx
        this_block = min(bsize, remaining)

        n_each = [max(1, round(p * this_block)) for p in contrast_probs]
        while sum(n_each) > this_block:
            n_each[np.argmax(n_each)] -= 1
        while sum(n_each) < this_block:
            n_each[np.argmax(contrast_probs)] += 1

        block_contrasts = []
        for c, n in zip(contrast_vals, n_each):
            block_contrasts.extend([c] * n)
        rng.shuffle(block_contrasts)

        # Switch trial: first trial of each NEW block gets lowest contrast
        switch_rule = stim.get("switch_trial_contrast")
        if (switch_rule == "lowest" and block_num > 0
                and len(contrast_vals) > 1
                and not sess.get("randomize_blocks", False)):
            lowest = min(contrast_vals)
            try:
                idx = block_contrasts.index(lowest)
                block_contrasts[0], block_contrasts[idx] = block_contrasts[idx], block_contrasts[0]
            except ValueError:
                pass

        for c in block_contrasts:
            trials.append({
                "trial_idx": trial_idx,
                "block_num": block_num,
                "az_deg": float(az),
                "alt_deg": float(stim.get("altitude_deg", 10.0)),
                "contrast": float(c),
            })
            trial_idx += 1
        block_num += 1

    # Randomize trial order if requested (random-location paradigm)
    if sess.get("randomize_blocks", False):
        if stim.get("switch_trial_contrast"):
            print("WARNING: switch_trial_contrast ignored because randomize_blocks is True")
        rng.shuffle(trials)
        for i, t in enumerate(trials):
            t["trial_idx"] = i
            t["block_num"] = 0

    return trials


def generate_stimuli(task_config: dict, warp_map, output_dir: str) -> dict:
    """Generate full session stimulus list.

    Args:
        task_config: parsed task YAML dict
        warp_map: loaded NPZ (np.load result) or None for fallback
        output_dir: directory to save stimuli.npz

    Returns:
        dict of arrays (same as NPZ contents) for Leader to use directly
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "stimuli.npz"

    trials = build_block_trial_list(task_config)
    n = len(trials)

    stim_cfg = task_config["stimulus"]
    bg = stim_cfg.get("background_gray", 0.0)
    duration = stim_cfg.get("duration_s", 2.0)
    proj_res = (1920, 1080)

    # Output arrays
    trial_idx = np.zeros(n, dtype=np.int32)
    block_num = np.zeros(n, dtype=np.int32)
    stim_az_deg = np.zeros(n, dtype=np.float32)
    stim_alt_deg = np.zeros(n, dtype=np.float32)
    contrast = np.zeros(n, dtype=np.float32)
    px_x = np.zeros(n, dtype=np.float32)
    px_y = np.zeros(n, dtype=np.float32)
    px_size = np.zeros(n, dtype=np.int32)
    corr_contrast = np.zeros(n, dtype=np.float32)
    duration_s = np.full(n, duration, dtype=np.float32)
    bg_gray = np.full(n, bg, dtype=np.float32)
    # visual-angle size per trial — drives the follower's spherical renderer (px_* is
    # kept for logging and the no-warp fallback)
    stim_size_deg = np.full(n, float(stim_cfg.get("size_deg", 10.0)), dtype=np.float32)

    for i, t in enumerate(trials):
        trial_idx[i] = t["trial_idx"]
        block_num[i] = t["block_num"]
        stim_az_deg[i] = t["az_deg"]
        stim_alt_deg[i] = t["alt_deg"]
        contrast[i] = t["contrast"]

        px_per_deg = proj_res[0] / (2 * 105.0)

        if warp_map is not None:
            try:
                px, py = az_alt_to_pixel(warp_map, t["az_deg"], t["alt_deg"])
                px_x[i] = px
                px_y[i] = py
            except ValueError:
                px_x[i] = proj_res[0] / 2 + t["az_deg"] * px_per_deg
                px_y[i] = proj_res[1] / 2 - t["alt_deg"] * px_per_deg
        else:
            px_x[i] = proj_res[0] / 2 + t["az_deg"] * px_per_deg
            px_y[i] = proj_res[1] / 2 - t["alt_deg"] * px_per_deg

        if warp_map is not None:
            px_size[i] = visual_angle_to_pixels(
                t["az_deg"], t["alt_deg"],
                stim_cfg["size_deg"], warp_map, proj_res)
            lum_corr = get_luminance_correction(warp_map, t["az_deg"])
            corr_contrast[i] = min(t["contrast"] * lum_corr, 1.0)
        else:
            px_size[i] = max(4, int(stim_cfg["size_deg"] * px_per_deg))
            corr_contrast[i] = t["contrast"]

    # Pre-generate all timing durations
    rng = np.random.default_rng()
    sess = task_config.get("session", {})

    iti_durations = _sample_durations(rng, sess.get("iti", 8.0), n + 1)
    prestim_durations = _sample_durations(rng, sess.get("prestim_duration", 0), n)
    poststim_durations = _sample_durations(rng, sess.get("poststim_duration", 0), n)

    # Block delays: one per block
    n_blocks_total = int(block_num[-1]) + 1 if n > 0 else 1
    block_delays = _sample_durations(rng, sess.get("block_delay", 0), n_blocks_total)
    block_delay_skip_first = bool(sess.get("block_delay_skip_first", False))

    # Global delay: single value
    global_delay = _sample_durations(rng, sess.get("global_delay", 0), 1)

    # Block start indices (which trial starts each block)
    block_start_indices = np.zeros(n_blocks_total, dtype=np.int32)
    for i in range(n):
        b = int(block_num[i])
        if i == 0 or int(block_num[i - 1]) != b:
            block_start_indices[b] = i

    # Photodiode sync patch: ON every Nth frame when enabled in the stimulus config
    _sync_on = bool(stim_cfg.get("photodiode_sync_enabled", False))
    sync_every_n = int(stim_cfg.get("photodiode_sync_every_n", 5)) if _sync_on else 0

    arrays = dict(
        trial_idx=trial_idx,
        block_num=block_num,
        stim_az_deg=stim_az_deg,
        stim_alt_deg=stim_alt_deg,
        contrast=contrast,
        px_x=px_x,
        px_y=px_y,
        px_size=px_size,
        stim_size_deg=stim_size_deg,
        corr_contrast=corr_contrast,
        duration_s=duration_s,
        bg_gray=bg_gray,
        iti_durations=iti_durations,
        prestim_durations=prestim_durations,
        poststim_durations=poststim_durations,
        block_delays=block_delays,
        block_delay_skip_first=np.array([block_delay_skip_first]),
        block_start_indices=block_start_indices,
        global_delay=global_delay,
        background_gray=np.array([bg]),
        n_trials=np.array([n]),
        sync_square_every_n=np.array([sync_every_n], dtype=np.int32),
    )

    np.savez(out_path, **arrays)

    # Save universal trial table as YAML
    trial_table = []
    for i in range(n):
        trial_table.append({
            "trial": int(trial_idx[i]),
            "block": int(block_num[i]),
            "az": float(stim_az_deg[i]),
            "alt": float(stim_alt_deg[i]),
            "contrast": float(corr_contrast[i]),
            "duration_s": float(duration_s[i]),
            "prestim_s": float(prestim_durations[i]),
            "poststim_s": float(poststim_durations[i]),
            "iti_s": float(iti_durations[i]),
            "px_x": float(px_x[i]),
            "px_y": float(px_y[i]),
            "px_size": int(px_size[i]),
        })
    yaml_path = output_dir / "trials.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(trial_table, f, default_flow_style=False)
    print(f"  Trial table: {yaml_path}")

    print(f"Generated {n} trials across {n_blocks_total} blocks")
    print(f"  Azimuth range: {stim_az_deg.min():.0f} to {stim_az_deg.max():.0f} deg")
    print(f"  Pixel sizes: {px_size.min()}-{px_size.max()} px")
    print(f"  Contrast: {corr_contrast.min():.3f}-{corr_contrast.max():.3f}")
    print(f"  ITI: {iti_durations.min():.1f}-{iti_durations.max():.1f}s")
    print(f"  Pre-stim: {prestim_durations.min():.1f}-{prestim_durations.max():.1f}s")
    print(f"  Post-stim: {poststim_durations.min():.1f}-{poststim_durations.max():.1f}s")
    if float(global_delay[0]) > 0:
        print(f"  Global delay: {global_delay[0]:.1f}s")
    if block_delays.max() > 0:
        print(f"  Block delay: {block_delays.min():.1f}-{block_delays.max():.1f}s"
              f" (skip first={block_delay_skip_first})")
    print(f"  Saved: {out_path}")

    return arrays
