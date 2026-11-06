"""
shared/stim_generator.py

Pre-generates all stimuli for a session. Saves pixel positions,
sizes, luminance-corrected contrasts, and durations to NPZ + YAML.

Called by Leader Pi at deploy time. NPZ uploaded to Follower.
YAML trial table shared with all consumers (UI, followers).

Output arrays per trial:
  trial_idx, block_num, stim_az_deg, stim_alt_deg, contrast,
  px_x, px_y, px_size, stim_brightness (= corr_contrast), stim_lum, stim_drive, duration_s, bg_gray
"""

import numpy as np
import yaml
from pathlib import Path


def _sample_durations(rng, cfg, size):
    """Sample durations from config. A [min, max] pair => uniform random per element; a scalar (5)
    OR a single-element list ([5]) => that fixed value; anything else => 0. Returns a float32 array
    of the given size.
    """
    if isinstance(cfg, (list, tuple)):
        if len(cfg) == 2:
            lo, hi = sorted((float(cfg[0]), float(cfg[1])))   # order-insensitive; [7,5] == [5,7]
            if hi > lo:
                return rng.uniform(lo, hi, size=size).astype(np.float32)
            val = lo                                          # [5,5] => fixed 5 (no empty-range crash)
        else:
            # [5] => fixed 5 (single-value list, same as the scalar 5); [] or >2 entries => 0
            val = float(cfg[0]) if len(cfg) == 1 else 0.0
    else:
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


def light_model(warp):
    """The display's light model (shared/intensity_model) baked into warp_map.npz; identity (drive
    = brightness = light) without a warp or for a warp built before the model existed."""
    from shared.intensity_model import IntensityModel
    return IntensityModel.from_arrays(warp) if warp is not None else IntensityModel.identity()


def stimulus_for(model, contrast, bg_brightness, az_deg, metric="weber"):
    """One trial's stimulus through the light model. contrast is a fraction in `metric` (0.5 =
    50 %), bg_brightness 0..1 of the uniform range. Returns a dict:
      bg_lum, stim_lum        light (calibration units) of background and requested stimulus
      stim_brightness         what the renderer is given (may exceed 1)
      stim_drive              drive at the stimulus centre (0..1, clipped)
      contrast_measured       contrast actually delivered at the centre (after clipping)
      clipped                 the request is brighter than drive 1 gives at this azimuth
      degenerate              Weber/Michelson asked of a background without light (fell back to
                              normalized: contrast = fraction of the headroom above background)"""
    L_b = float(model.L_from_b(bg_brightness))
    L_s = float(model.stim_light(contrast, L_b, metric))
    top = float(model.max_light(az_deg))
    shown = min(L_s, top)
    return {"bg_lum": L_b, "stim_lum": L_s, "stim_brightness": float(model.b_from_L(L_s)),
            "stim_drive": float(model.drive(az_deg, L_s)),
            "contrast_measured": float(model.contrast(shown, L_b, metric)),
            "clipped": L_s > top * (1 + 1e-9), "degenerate": model.degenerate(L_b, metric)}


def _max_duration(cfg) -> float:
    """The largest value a duration config can produce (mirrors _sample_durations: scalar, [v],
    or [min, max]; anything else is 0)."""
    if isinstance(cfg, (list, tuple)):
        vals = [float(v) for v in cfg if isinstance(v, (int, float))]
        return max(vals) if vals and len(cfg) <= 2 else 0.0
    return float(cfg) if isinstance(cfg, (int, float)) else 0.0


def switch_trial_rule(task_config: dict) -> str | None:
    """'lowest' | 'highest' when the first trial of EVERY block (the first one included) is pinned
    to that contrast, else None. Read from stimulus.switch_trial_contrast (absent / null /
    'random' = None). The rule is only in force while blocks exist as such — block_size > 1 or
    a block_delay > 0 — and never with randomize_blocks, which dissolves the blocks. The
    Experiment tab greys its drop-down out under this SAME test and saves 'random' then, so the
    YAML never promises what the generated list will not do."""
    sess = task_config.get("session", {}) or {}
    stim = task_config.get("stimulus", {}) or {}
    rule = str(stim.get("switch_trial_contrast") or "random").strip().lower()
    if rule not in ("lowest", "highest") or sess.get("randomize_blocks", False):
        return None
    bsize = int(sess.get("block_size", 25))
    return rule if (bsize > 1 or _max_duration(sess.get("block_delay", 0)) > 0) else None


def is_go_trial(az_deg: float, go_rule) -> bool:
    """Whether a trial at this azimuth is a GO trial under stimulus.go_rule: 'all' -> every trial,
    'right' -> az > 0, anything else ('left', the engine's default) -> az < 0. ONE rule for the
    engine (Leader._classify_go), the controller (registry.event_is_go) and the opto assignment."""
    rule = str(go_rule or "left").strip().lower()
    if rule == "all":
        return True
    if rule == "right":
        return float(az_deg) > 0
    return float(az_deg) < 0


def _assign_opto(trials: list[dict], opto_cfg: dict, go_rule, rng) -> None:
    """Mark the opto trials in place (t["opto"] = True/False) for a trial list in its FINAL order.
    Per block, per contrast value, among the eligible trials: round(fraction x n) with a carried
    remainder, so the light is stratified across contrasts and the block total tracks the fraction.
    min_gap_trials non-opto trials are kept between opto trials (across block boundaries too) by
    drawing candidates in a random round-robin over the contrast groups; a quota that cannot be met
    under the gap is reported, not silently widened. Decided HERE, at Deploy, so the trial table
    shows it and the plan (stimuli.npz opto_on) is the record of what was scheduled."""
    for t in trials:
        t["opto"] = False
    if not opto_cfg or not opto_cfg.get("enabled", False):
        return
    fraction = min(1.0, max(0.0, float(opto_cfg.get("fraction", 0.0) or 0.0)))
    if fraction <= 0 or not trials:
        return
    eligible = str(opto_cfg.get("eligible", "all") or "all").strip().lower()
    gap = max(0, int(opto_cfg.get("min_gap_trials", 0) or 0))

    def ok(t):
        if eligible == "go":
            return is_go_trial(t["az_deg"], go_rule)
        if eligible == "nogo":
            return not is_go_trial(t["az_deg"], go_rule)
        return True

    chosen = set()          # positions (list index = time order) already lit
    short = []              # (block, contrast, wanted, got)
    blocks = {}
    for pos, t in enumerate(trials):
        blocks.setdefault(t["block_num"], []).append(pos)
    for b in sorted(blocks):
        groups = {}         # contrast -> candidate positions
        for pos in blocks[b]:
            if ok(trials[pos]):
                groups.setdefault(float(trials[pos]["contrast"]), []).append(pos)
        carry = 0.0
        quota = {}
        for c in sorted(groups):
            exact = fraction * len(groups[c]) + carry
            k = int(round(exact))
            carry = exact - k
            quota[c] = min(k, len(groups[c]))
            cand = list(groups[c])
            rng.shuffle(cand)
            groups[c] = cand
        # A greedy draw can paint itself into a corner under the gap rule, so redraw the block
        # (fresh shuffle) until every quota is met, keeping the best attempt otherwise.
        best = None
        for attempt in range(200 if gap else 1):
            got = {c: 0 for c in groups}
            picked = set()
            cand = {c: list(groups[c]) for c in groups}
            if attempt:
                for c in cand:
                    rng.shuffle(cand[c])
            progress = True
            while progress:
                progress = False
                for c in sorted(cand):            # round-robin: no contrast starves another
                    if got[c] >= quota[c]:
                        continue
                    while cand[c]:
                        pos = cand[c].pop()
                        if all(abs(pos - q) > gap for q in chosen | picked):
                            picked.add(pos)
                            got[c] += 1
                            progress = True
                            break
            if best is None or sum(got.values()) > sum(best[0].values()):
                best = (got, picked)
            if all(got[c] >= quota[c] for c in quota):
                break
        got, picked = best
        chosen |= picked
        for c in sorted(groups):
            if got[c] < quota[c]:
                short.append((b, c, quota[c], got[c]))
    for pos in chosen:
        trials[pos]["opto"] = True
    if short:
        print("[stim_generator] WARNING: opto min_gap_trials could not be met: " +
              "; ".join(f"block {b} contrast {c:g}: {g}/{w}" for b, c, w, g in short), flush=True)


def opto_summary(task_config: dict, n_opto: int, n: int) -> str:
    """One line for the Deploy log: how many trials are lit, under which rules, in which windows."""
    o = task_config.get("opto") or {}
    if not o.get("enabled", False):
        return ""
    wins = o.get("windows") or {}
    names = [f"{k} {float(w.get('onset_s', 0)):+.2f}s/{float(w.get('duration_s', 0)):g}s"
             for k, w in wins.items() if isinstance(w, dict) and w.get("enabled")]
    pat = o.get("pattern") or {}
    mode = str(pat.get("mode", "continuous"))
    if mode == "pulsed":
        mode += f" {pat.get('pulse_hz', 20)} Hz x {pat.get('pulse_width_ms', 5)} ms"
    return (f"Opto: {n_opto}/{n} trials (fraction {float(o.get('fraction', 0)):g}, eligible "
            f"{o.get('eligible', 'all')}, min gap {int(o.get('min_gap_trials', 0) or 0)}); "
            f"windows: {', '.join(names) or 'none'}; {mode}"
            + ("; mask LED on every trial" if o.get("mask") else ""))


def build_block_trial_list(task_config: dict, seed=None) -> list[dict]:
    """Build the ordered trial list: dicts of trial_idx, block_num, az_deg, alt_deg, contrast and
    opto (the per-trial light flag, assigned last so it sees the final order). `seed` makes the
    draw reproducible (tests); Deploy leaves it None, so every Deploy draws a fresh plan."""
    rng = np.random.default_rng(seed)

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

    # First trial of every block pinned to the lowest / highest contrast (switch-trial probe).
    rule = switch_trial_rule(task_config)
    pinned = (min(contrast_vals) if rule == "lowest" else max(contrast_vals)) if rule else None

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

        # Pinned first trial: SWAP the pinned contrast into position 0 (every block, block 0
        # included), so the block keeps exactly the counts its proportions gave it. A short last
        # block may hold no trial of that contrast at all — then it is left as shuffled.
        if pinned is not None and pinned in block_contrasts:
            idx = block_contrasts.index(pinned)
            block_contrasts[0], block_contrasts[idx] = block_contrasts[idx], block_contrasts[0]

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
        if str(stim.get("switch_trial_contrast") or "random").strip().lower() != "random":
            print("WARNING: switch_trial_contrast ignored because randomize_blocks is True")
        rng.shuffle(trials)
        for i, t in enumerate(trials):
            t["trial_idx"] = i
            t["block_num"] = 0

    # Opto trials: assigned on the FINAL order (after any shuffle), so the gap rule and the
    # per-block stratification describe the trials as they will actually run.
    _assign_opto(trials, task_config.get("opto") or {}, stim.get("go_rule", "left"), rng)
    return trials


def generate_stimuli(task_config: dict, warp_map, output_dir: str,
                     contrast_metric: str = "weber", seed=None) -> dict:
    """Generate full session stimulus list.

    Args:
        task_config: parsed task YAML dict
        warp_map: loaded NPZ (np.load result) or None for fallback
        output_dir: directory to save stimuli.npz
        contrast_metric: how the config `contrast` value is interpreted — weber|michelson|normalized
            (from rig.devices.display.contrast_metric), always in LIGHT units through the warp's
            light model; background_gray is a brightness, 0..1 of the uniform range

    Returns:
        dict of arrays (same as NPZ contents) for Leader to use directly
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "stimuli.npz"

    trials = build_block_trial_list(task_config, seed=seed)
    n = len(trials)

    stim_cfg = task_config["stimulus"]
    bg = stim_cfg.get("background_gray", 0.0)
    duration = stim_cfg.get("duration_s", 2.0)
    shape = str(stim_cfg.get("shape", "square")).lower()
    contrast_metric = str(contrast_metric).lower()
    model = light_model(warp_map)
    if model.degenerate(model.L_from_b(bg), contrast_metric):
        print(f"WARNING: {contrast_metric} contrast is undefined on a background without light "
              f"(brightness {bg}, calibration {model.name or model.source}); using 'normalized' "
              f"(fraction of the headroom above the background).")
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
    stim_brightness = np.zeros(n, dtype=np.float32)     # rendered: 0..1 of the uniform range (may be >1)
    stim_lum = np.zeros(n, dtype=np.float32)            # requested stimulus light (calibration units)
    stim_drive = np.zeros(n, dtype=np.float32)          # drive at the stimulus centre
    contrast_measured = np.zeros(n, dtype=np.float32)   # contrast delivered at the centre
    duration_s = np.full(n, duration, dtype=np.float32)
    bg_gray = np.full(n, bg, dtype=np.float32)
    # visual-angle size per trial — drives the follower's spherical renderer (px_* is
    # kept for logging and the no-warp fallback)
    stim_size_deg = np.full(n, float(stim_cfg.get("size_deg", 10.0)), dtype=np.float32)
    # optogenetic light on this trial (task opto: section; decided in build_block_trial_list).
    # int8 so the engine's per-trial slice (every 1-D array of length n) picks it up unchanged.
    opto_on = np.zeros(n, dtype=np.int8)

    n_px_fallback = 0   # trials whose px_x/px_y came from the LINEAR fallback
    n_clipped = 0       # trials asking for more light than drive 1.0 gives at their azimuth
    for i, t in enumerate(trials):
        trial_idx[i] = t["trial_idx"]
        block_num[i] = t["block_num"]
        stim_az_deg[i] = t["az_deg"]
        stim_alt_deg[i] = t["alt_deg"]
        contrast[i] = t["contrast"]
        opto_on[i] = 1 if t.get("opto") else 0

        px_per_deg = proj_res[0] / (2 * 105.0)

        if warp_map is not None:
            try:
                px, py = az_alt_to_pixel(warp_map, t["az_deg"], t["alt_deg"])
                px_x[i] = px
                px_y[i] = py
            except ValueError:
                n_px_fallback += 1
                px_x[i] = proj_res[0] / 2 + t["az_deg"] * px_per_deg
                px_y[i] = proj_res[1] / 2 - t["alt_deg"] * px_per_deg
        else:
            n_px_fallback += 1
            px_x[i] = proj_res[0] / 2 + t["az_deg"] * px_per_deg
            px_y[i] = proj_res[1] / 2 - t["alt_deg"] * px_per_deg

        # Contrast is defined on LIGHT (the warp's light model): background brightness -> light,
        # contrast -> stimulus light -> the brightness the renderer is given. The renderer turns
        # brightness into per-pixel drive with the same model, so delivered light is uniform.
        st = stimulus_for(model, t["contrast"], bg, t["az_deg"], contrast_metric)
        n_clipped += int(st["clipped"])
        stim_brightness[i] = st["stim_brightness"]
        stim_lum[i] = st["stim_lum"]
        stim_drive[i] = st["stim_drive"]
        contrast_measured[i] = st["contrast_measured"]
        corr_contrast[i] = st["stim_brightness"]   # legacy field name: the rendered brightness
        if warp_map is not None:
            px_size[i] = visual_angle_to_pixels(
                t["az_deg"], t["alt_deg"],
                stim_cfg["size_deg"], warp_map, proj_res)
        else:
            px_size[i] = max(4, int(stim_cfg["size_deg"] * px_per_deg))

    if n_clipped:
        print(f"[stim_generator] WARNING: {n_clipped}/{n} trials asked for more light than drive "
              f"1.0 gives at their azimuth — shown at drive 1.0 (see contrast_measured). Use "
              f"Correct in the Experiment tab to clamp.", flush=True)
    if n_px_fallback:
        print(f"[stim_generator] WARNING: {n_px_fallback}/{n} trials used the LINEAR "
              f"az→px fallback (no warp map, or angle outside warp coverage). The linear "
              f"mapping is up to ~19° wrong mid-field — deploy a valid warp map before "
              f"running real sessions.", flush=True)

    # Pre-generate all timing durations
    rng = np.random.default_rng(seed)
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
        stim_brightness=stim_brightness,
        stim_lum=stim_lum,
        bg_lum=np.array([float(model.L_from_b(bg))]),
        stim_drive=stim_drive,
        contrast_measured=contrast_measured,
        intensity_calibration=np.array([model.name or model.source or "identity"]),
        light_unit=np.array([model.unit]),
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
        shape=np.array([shape]),
        n_trials=np.array([n]),
        sync_square_every_n=np.array([sync_every_n], dtype=np.int32),
        opto_on=opto_on,
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
            "contrast": float(contrast[i]),            # raw metric value (matches UI/HDF5)
            "stim_brightness": float(stim_brightness[i]),   # rendered (0..1 of uniform range)
            "stim_lum": float(stim_lum[i]),                 # requested light
            "stim_drive": float(stim_drive[i]),             # drive at the stimulus centre
            "contrast_measured": float(contrast_measured[i]),
            "duration_s": float(duration_s[i]),
            "prestim_s": float(prestim_durations[i]),
            "poststim_s": float(poststim_durations[i]),
            "iti_s": float(iti_durations[i]),
            "px_x": float(px_x[i]),
            "px_y": float(px_y[i]),
            "px_size": int(px_size[i]),
            "opto": bool(opto_on[i]),
        })
    yaml_path = output_dir / "trials.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(trial_table, f, default_flow_style=False)
    print(f"  Trial table: {yaml_path}")

    print(f"Generated {n} trials across {n_blocks_total} blocks")
    rule = switch_trial_rule(task_config)
    if rule:
        print(f"  First trial of every block: {rule} contrast")
    opto_line = opto_summary(task_config, int(opto_on.sum()), n)
    if opto_line:
        print("  " + opto_line)
    print(f"  Azimuth range: {stim_az_deg.min():.0f} to {stim_az_deg.max():.0f} deg")
    print(f"  Pixel sizes: {px_size.min()}-{px_size.max()} px")
    print(f"  Stimulus brightness: {stim_brightness.min():.3f}-{stim_brightness.max():.3f} "
          f"(light {stim_lum.min():.4g}-{stim_lum.max():.4g} {model.unit}, calibration "
          f"{model.name or model.source or 'identity'})")
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
