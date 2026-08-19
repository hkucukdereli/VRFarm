"""
shared/consolidate.py

Fold a session's sidecar files into the single HDF5, reorganize it into groups,
then delete the sidecars — so the archived artifact is one self-contained
`<session_id>.h5` (plus the video.h264, which stays a file).

Runs on the Leader via pi_api at transfer time: the engine process that wrote
the .h5 is already gone, so a fresh process opens the closed file and rebuilds it.

Merges (then deletes the source):
  metadata.yaml        -> root attributes (+ task_config as a JSON string)
  stimuli.npz          -> /stimulus group (planned params, de-duped vs /trials)
  frame_timestamps.npy -> /camera/frame_timestamps  (Nx3, with a `columns` attr)
  camera_metadata.json -> /camera attrs (sensor mode, ScalerCrop, output size, colour space,
                          exposure, encoder settings — the geometry the video was shot with)
  trials.yaml          -> dropped (redundant with stimuli.npz)

Reorganizes the flat per-trial datasets the engine wrote into groups:
  /trials (recorded), /lick, /reward, /photodiode.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

FORMAT_VERSION = 2

# flat per-trial dataset (engine output) -> destination group
_GROUP = {}
for _n in ("trial_num", "block_num", "trial_outcome", "level_effective", "adaptive_state",
           "iti_duration_s", "stim_az_deg", "contrast", "corr_contrast",
           "iti_start_t", "stim_onset_t", "true_onset_t", "response_window_t", "outcome_t",
           "first_lick_t", "display_latency_s", "sync_ok"):
    _GROUP[_n] = "trials"
for _n in ("lick_times", "iti_lick_times", "iti_lick_count"):
    _GROUP[_n] = "lick"
for _n in ("reward_t", "reward_amount_ul", "reward_pavlovian"):
    _GROUP[_n] = "reward"
_GROUP["sync_pulses"] = "photodiode"

# stimuli.npz per-trial arrays -> /stimulus dataset name (renamed for clarity)
_NPZ_DATASET = {
    "stim_alt_deg": "stim_alt_deg", "px_x": "px_x", "px_y": "px_y", "px_size": "px_size",
    "duration_s": "duration_s", "prestim_durations": "prestim_s",
    "poststim_durations": "poststim_s", "iti_durations": "iti_planned_s",
    "block_delays": "block_delays", "block_start_indices": "block_start_indices",
}
# stimuli.npz scalars -> /stimulus group attrs
_NPZ_ATTR = ("background_gray", "global_delay", "shape",
             "block_delay_skip_first", "sync_square_every_n")
# stimuli.npz arrays that duplicate recorded /trials data -> dropped
_NPZ_SKIP = {"trial_idx", "block_num", "stim_az_deg", "contrast", "corr_contrast",
             "bg_gray", "n_trials"}


def consolidate_session(session_dir, video_session_dir=None, delete_sidecars=True):
    """Build the final self-contained HDF5 for a session and remove sidecars.

    session_dir: dir holding <session_id>.h5, metadata.yaml, stimuli.npz, trials.yaml.
    video_session_dir: dir holding frame_timestamps.npy (+ video.h264); optional.
    Returns a summary dict. Idempotent: a file already at FORMAT_VERSION is left alone.
    """
    import h5py
    import numpy as np

    session_dir = Path(session_dir)
    h5s = sorted(session_dir.glob("*.h5"))
    if not h5s:
        raise FileNotFoundError(f"No .h5 in {session_dir}")
    h5_path = h5s[0]

    with h5py.File(h5_path, "r") as f:
        if f.attrs.get("format_version", 0) == FORMAT_VERSION or "trials" in f:
            return {"ok": True, "skipped": "already consolidated", "h5": str(h5_path)}

    meta_path = session_dir / "metadata.yaml"
    npz_path = session_dir / "stimuli.npz"
    trials_yaml = session_dir / "trials.yaml"
    tslog_path = Path(video_session_dir) / "frame_timestamps.npy" if video_session_dir else None
    geom_path = Path(video_session_dir) / "camera_metadata.json" if video_session_dir else None

    merged = {"metadata": False, "stimulus": False, "camera": False, "camera_geometry": False}
    tmp = h5_path.with_name(h5_path.name + ".consolidating")
    try:
        with h5py.File(h5_path, "r") as src, h5py.File(tmp, "w") as dst:
            dst.attrs["format_version"] = FORMAT_VERSION

            # 1) reorganize the engine's flat per-trial datasets into groups
            for name in src.keys():
                if name.startswith("_"):
                    continue   # engine-internal planned arrays; handled from the NPZ below
                grp = _GROUP.get(name, "trials")   # fallback: keep unknowns under /trials
                src.copy(name, dst.require_group(grp), name=name)

            # 2) metadata.yaml -> root attributes
            if meta_path.exists():
                import yaml
                meta = yaml.safe_load(meta_path.read_text()) or {}
                for k, v in meta.items():
                    if k == "task_config":
                        dst.attrs["task_config"] = json.dumps(v)
                    elif isinstance(v, (list, dict)):
                        dst.attrs[k] = json.dumps(v)
                    elif v is None:
                        dst.attrs[k] = ""
                    else:
                        dst.attrs[k] = v
                merged["metadata"] = True

            # 3) stimuli.npz -> /stimulus (planned params, de-duped vs /trials)
            planned_iti_written = False
            if npz_path.exists():
                z = np.load(npz_path, allow_pickle=True)
                sg = dst.require_group("stimulus")
                for key in z.files:
                    if key in _NPZ_SKIP:
                        continue
                    if key in _NPZ_DATASET:
                        sg.create_dataset(_NPZ_DATASET[key], data=z[key])
                        planned_iti_written |= (key == "iti_durations")
                    elif key in _NPZ_ATTR:
                        val = z[key]
                        val = val.item() if getattr(val, "size", None) == 1 else val
                        sg.attrs[key] = val.decode() if isinstance(val, bytes) else val
                merged["stimulus"] = True
            # fallback: planned ITI from the engine's own copy if the NPZ is missing
            if not planned_iti_written and "_iti_durations_planned" in src:
                dst.require_group("stimulus").create_dataset(
                    "iti_planned_s", data=src["_iti_durations_planned"][:])

            # 4) frame_timestamps.npy -> /camera/frame_timestamps
            if tslog_path and tslog_path.exists():
                ts = np.load(tslog_path)
                d = dst.create_dataset("camera/frame_timestamps", data=ts)
                d.attrs["columns"] = ["frame_idx", "wall_clock_s", "sensor_ts_ns"]
                merged["camera"] = True

            # 5) camera_metadata.json -> /camera attrs. Sensor mode, ScalerCrop, output size,
            # colour space, exposure and encoder settings — i.e. which part of the scene each
            # pixel is and at what scale. None of it is recoverable from video.h264, so without
            # this a change of resolution or crop is invisible in the archive. require_group:
            # the geometry is worth keeping even when frame_timestamps.npy is missing.
            if geom_path and geom_path.exists():
                try:
                    geom = json.loads(geom_path.read_text())
                    cg = dst.require_group("camera")
                    for k, v in geom.items():
                        # numeric lists (sizes, crop rects) -> native array attrs, so they read
                        # back as arrays in h5py/HDFView/MATLAB rather than needing json.loads.
                        # Anything else non-scalar -> JSON string.
                        if isinstance(v, list) and v and all(isinstance(x, (int, float))
                                                             and not isinstance(x, bool)
                                                             for x in v):
                            cg.attrs[k] = np.asarray(v)
                        elif isinstance(v, (list, dict)):
                            cg.attrs[k] = json.dumps(v)
                        else:
                            cg.attrs[k] = v
                    merged["camera_geometry"] = True
                except Exception as e:
                    print(f"[consolidate] camera_metadata.json unreadable ({e}); skipping")

        os.replace(tmp, h5_path)   # atomic swap only on full success
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise

    removed = []
    if delete_sidecars:
        for p in (meta_path, npz_path, trials_yaml, tslog_path, geom_path):
            if p and Path(p).exists():
                Path(p).unlink()
                removed.append(Path(p).name)

    return {"ok": True, "h5": str(h5_path), "merged": merged, "removed": removed}
