"""
shared/consolidate.py

Fold a session's sidecar files into the single HDF5, reorganize it into groups,
then delete the sidecars — so the archived artifact is one self-contained
`<session_id>.h5` plus `video.mp4` (the raw video.h264 stream-copied into a container).

Runs on the Leader. The .h5 fold happens at the engine's exit (engine/leader.py
`_consolidate_at_exit`, remux_video=False) and again, idempotently, from the Data tab's
sync (shared/leader_data.py `consolidate`), which is ALSO where the video remux runs —
never at the engine's exit, where pi_api's 10 s SIGKILL escalation cut it short.

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
for _n in ("opto_on", "opto_mask_on", "opto_onset_t", "opto_n_windows", "opto_n_dropped",
           "opto_window_t", "opto_window_anchor"):
    _GROUP[_n] = "opto"          # optogenetics device (devices/opto.py)

# stimuli.npz per-trial arrays -> /stimulus dataset name (renamed for clarity)
_NPZ_DATASET = {
    "stim_alt_deg": "stim_alt_deg", "px_x": "px_x", "px_y": "px_y", "px_size": "px_size",
    "duration_s": "duration_s", "prestim_durations": "prestim_s",
    "poststim_durations": "poststim_s", "iti_durations": "iti_planned_s",
    "block_delays": "block_delays", "block_start_indices": "block_start_indices",
    # light model (shared/intensity_model): what each stimulus was in light, per trial
    "stim_brightness": "stim_brightness", "stim_lum": "stim_lum", "stim_drive": "stim_drive",
    "contrast_measured": "contrast_measured",
    # which trials the Deploy scheduled for opto light (what FIRED is in /opto, when saved)
    "opto_on": "opto_planned",
}
# stimuli.npz scalars -> /stimulus group attrs
_NPZ_ATTR = ("background_gray", "global_delay", "shape",
             "block_delay_skip_first", "sync_square_every_n",
             "bg_lum", "intensity_calibration", "light_unit")
# stimuli.npz arrays that duplicate recorded /trials data -> dropped
_NPZ_SKIP = {"trial_idx", "block_num", "stim_az_deg", "contrast", "corr_contrast",
             "bg_gray", "n_trials"}


# A video.mp4.part younger than this is an ffmpeg still writing (a manual tools/consolidate_session.py,
# or a Data-tab consolidate whose SSH session died mid-remux): left alone, the next pass adopts or
# removes it. Anything older is finished — or orphaned — and is judged on its content.
PART_SETTLE_S = 60
# A stream copy lands at ~the raw's size (the MP4 index adds ~1 MB per 300k frames); far smaller = truncated.
MIN_SIZE_FRACTION = 0.95


def _video_h5_info(h5_path) -> dict:
    """The /camera facts the remux needs, in one open: fps (requested, else measured, else 30 — the
    raw stream's own rate is unreliable, ffprobe reads 120/25 on our files) and frame_count (frames
    the camera logged; None when unknown)."""
    fps, frame_count = 30, None
    try:
        import h5py
        with h5py.File(h5_path, "r") as f:
            cam = dict(f["camera"].attrs) if "camera" in f else {}
        fps = int(round(float(cam.get("requested_fps") or cam.get("measured_fps") or 30)))
        fc = cam.get("frame_count")
        frame_count = int(fc) if fc is not None else None
    except Exception:
        pass
    if fps <= 0:
        fps = 30
    return {"fps": fps, "frame_count": frame_count}


def _mp4_frames(path) -> int | None:
    """Frames in an MP4's video track, from its index (ffprobe reads the moov — no decode, a seek on
    a 13 GB file). None when the file has no usable index: a remux killed before it finalized."""
    import subprocess
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=nb_frames", "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=120)
        return int(r.stdout.strip().splitlines()[0]) if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None            # "N/A", no output, ffprobe crashed or timed out


def _verify_mp4(path, raw_size, frame_count):
    """Is this MP4 a complete copy of the recording? Returns (ok, reason, frames):
      unverifiable  no ffprobe on this machine
      incomplete    no readable index — a remux killed before it finalized, or a truncated copy
      too_small     a complete file far below the raw's size: a different (shorter) recording
      frames_short  a complete file with fewer frames than the camera logged
    frames is what the index says whenever it could be read, so a caller can tell a broken file
    (incomplete) from a complete file that is simply not THIS recording (the other two — e.g. a
    session id reused on another day, cheddar ASD150_20261030_001)."""
    import shutil
    if not Path(path).is_file():
        return False, "incomplete", None
    if shutil.which("ffprobe") is None:
        return False, "unverifiable", None
    frames = _mp4_frames(path)
    if frames is None:
        return False, "incomplete", None
    if raw_size and Path(path).stat().st_size < raw_size * MIN_SIZE_FRACTION:
        return False, "too_small", frames
    if frame_count is not None and frames < frame_count:
        return False, "frames_short", frames
    return True, None, frames


def _remux_and_prune_video(video_session_dir, h5_path, run_ffmpeg=True):
    """Turn the raw video.h264 into video.mp4 (container only, NO re-encode) and delete the raw once
    the mp4 is verified. Runs on the Leader from the Data tab's consolidate step (shared/leader_data.py
    `consolidate`), right before the copy — NOT at the engine's exit any more: the controller's teardown
    has pi_api SIGKILL the engine 10 s after SIGTERM, which cut off every remux longer than that and
    left video.h264 next to a complete but never renamed video.mp4.part (cheddar, every recording over
    ~75 min, 2026-09..11). With run_ffmpeg=False the same function is what tools/repair_stranded_video.py
    runs over the controller's copies (adopt and prune only).

    Why remux at all: raw .h264 is what the encoder writes (crash-safe, append-only), but it has no
    container/timing — players and DeepLabCut read the wrong fps and can't seek. A stream copy into
    MP4 is near-free (no re-encode, ~disk-speed) and fixes that. The frame rate is forced from the h5.

    Verification = size >= 95 % of the raw, a readable MP4 index, and at least as many frames as the
    camera logged (a finished copy has a few MORE: the encoder flushes after the logger stops).
      video.mp4 only                    -> nothing
      video.mp4 + raw (+ .part)         -> verify the mp4; ok: delete the leftovers. A BROKEN mp4 (no index:
                                           a truncated copy) is replaced by a complete .part or a fresh remux
                                           of the raw; a complete mp4 that is NOT this recording (a reused
                                           session id) is never touched — everything is kept and reported
      raw + a complete, settled .part   -> ADOPT it: rename (no ffmpeg; the inode and its mtime are kept,
                                           so the controller's rsynced copy of it still matches) + delete raw
      raw + a truncated .part           -> delete the .part, then as "raw only"
      raw + a .part younger than 60 s   -> someone's ffmpeg is still writing: leave everything
      raw only                          -> ffmpeg -> .part -> verify -> rename -> delete the raw
                                           (run_ffmpeg=False: report, touch nothing)
      .part only (raw already gone)     -> verify (no size check) and adopt
    Never raises: on ANY problem it leaves the raw .h264 in place and reports why, so a bad remux can
    never cost a recording. Idempotent."""
    import shutil
    import subprocess
    import time

    if not video_session_dir:
        return {"skipped": "no video dir"}
    vdir = Path(video_session_dir)
    raw = vdir / "video.h264"
    mp4 = vdir / "video.mp4"
    part = vdir / "video.mp4.part"
    have_raw, have_mp4, have_part = raw.exists(), mp4.exists(), part.exists()
    if have_mp4 and not have_raw and not have_part:
        return {"skipped": "already mp4"}
    if not have_raw and not have_part:
        return {"skipped": "no raw video"}

    info = _video_h5_info(h5_path)
    fps, frame_count = info["fps"], info["frame_count"]
    raw_size = raw.stat().st_size if have_raw else None

    def _rm(p):
        try:
            p.unlink()
            return True
        except OSError:
            return False

    def _settled(p):
        try:
            return time.time() - p.stat().st_mtime >= PART_SETTLE_S
        except OSError:
            return True

    # 1. The mp4 is already there. Good: only leftovers to clean. Bad (a truncated copy, say): a
    #    complete .part or a fresh remux from the raw replaces it below; with neither, keep everything.
    bad_mp4 = None
    if have_mp4:
        if have_part and not _settled(part):
            return {"skipped": "remux in progress"}
        ok, reason, frames = _verify_mp4(mp4, raw_size, frame_count)
        if ok:
            res = {"pruned": True, "fps": fps, "frames": frames, "frame_count": frame_count,
                   "mp4_bytes": mp4.stat().st_size, "removed_raw": bool(have_raw and _rm(raw))}
            if have_part:
                res["removed_part"] = _rm(part)
            return res
        if reason == "unverifiable":
            return {"error": "cannot verify video.mp4 (no ffprobe here); leftovers kept", "fps": fps}
        if reason != "incomplete":
            # a complete MP4 that is not this recording (a session id reused on another day?): never overwritten
            return {"error": f"video.mp4 does not match this recording ({reason}: {frames} frames in it vs "
                             f"{frame_count} logged by the camera, {mp4.stat().st_size} vs raw {raw_size} bytes); "
                             f"left for inspection", "fps": fps}
        if not have_part and not (have_raw and run_ffmpeg):
            return {"error": "video.mp4 fails verification (incomplete) and nothing here can replace it; "
                             "leftovers kept", "fps": fps}
        bad_mp4 = reason
    also_bad = f"; video.mp4 fails too ({bad_mp4})" if bad_mp4 else ""

    # 2. A .part next to the raw (or alone): a remux someone started. Complete -> adopt (over a bad mp4,
    #    if there is one); truncated -> dropped only when a fresh remux from the raw follows.
    if have_part:
        if not _settled(part):
            return {"skipped": "remux in progress"}
        ok, reason, frames = _verify_mp4(part, raw_size, frame_count)
        if ok:
            os.replace(part, mp4)
            res = {"adopted": "video.mp4", "fps": fps, "frames": frames, "frame_count": frame_count,
                   "mp4_bytes": mp4.stat().st_size}
            if bad_mp4:
                res["replaced_mp4"] = bad_mp4
            if have_raw:
                res["removed_raw"] = _rm(raw)
            return res
        if reason == "unverifiable":
            return {"error": "cannot verify video.mp4.part (no ffprobe here); left as is", "fps": fps}
        what = (f"stale video.mp4.part has {frames} < {frame_count} logged frames" if reason == "frames_short"
                else f"video.mp4.part fails verification ({reason})")
        if not have_raw:
            return {"error": f"{what} and the raw is gone; left for inspection{also_bad}", "fps": fps}
        if not run_ffmpeg:
            return {"error": f"{what}; left for inspection{also_bad}", "fps": fps}
        _rm(part)                       # a fresh remux from the raw replaces it

    # 3. Raw only (or raw + a bad mp4 to replace).
    if not run_ffmpeg:
        if bad_mp4:
            return {"error": f"video.mp4 fails verification ({bad_mp4}) and there is no complete .part to "
                             f"adopt; leftovers kept", "fps": fps}
        return {"skipped": "adopt only"}
    if shutil.which("ffmpeg") is None:
        return {"error": f"ffmpeg not found; kept raw .h264{also_bad}"}
    # -r before -i forces the input rate; -c:v copy = stream copy (no re-encode); -f mp4 names the
    # muxer explicitly (the temp file's .part extension can't be auto-detected); no +faststart (it
    # would rewrite the whole file — pointless for local/DLC use); -an = no audio.
    cmd = ["ffmpeg", "-y", "-v", "error", "-fflags", "+genpts", "-r", str(fps),
           "-i", str(raw), "-c:v", "copy", "-an", "-f", "mp4", str(part)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3600)
    except Exception as e:
        _rm(part)
        err = getattr(e, "stderr", "") or str(e)
        return {"error": f"remux failed ({str(err)[:120]}); kept raw .h264", "fps": fps}
    ok, reason, frames = _verify_mp4(part, raw_size, frame_count)
    if not ok and reason not in ("frames_short", "unverifiable"):
        _rm(part)
        return {"error": f"remux output {reason}; kept raw .h264", "fps": fps}
    os.replace(part, mp4)
    raw.unlink()
    res = {"remuxed": "video.mp4", "fps": fps, "removed_raw": True, "mp4_bytes": mp4.stat().st_size,
           "frames": frames, "frame_count": frame_count}
    if bad_mp4:
        res["replaced_mp4"] = bad_mp4
    if reason == "frames_short":
        # the raw itself is short — the camera logged frames the encoder never emitted; nothing to recover
        res["warning"] = f"{frames} frames in the video, {frame_count} logged by the camera"
    return res

def consolidate_session(session_dir, video_session_dir=None, delete_sidecars=True, remux_video=True):
    """Build the final self-contained HDF5 for a session and remove sidecars.

    session_dir: dir holding <session_id>.h5, metadata.yaml, stimuli.npz, trials.yaml.
    video_session_dir: dir holding frame_timestamps.npy (+ video.h264); optional.
    remux_video: copy-remux video.h264 -> video.mp4 and delete the raw (default True; the engine
                 passes False at its exit and leaves the remux to the Data tab's consolidate step).
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
        already = f.attrs.get("format_version", 0) == FORMAT_VERSION or "trials" in f
    if already:
        # h5 is already consolidated, but still (re)try the video remux — a prior run may have
        # been unable to (ffmpeg missing, interrupted), and we'd rather finish it on a re-transfer
        # than leave the raw .h264 stranded.
        vres = _remux_and_prune_video(video_session_dir, h5_path) if remux_video else {"skipped": "deferred to the Data-tab sync"}
        return {"ok": True, "skipped": "already consolidated", "h5": str(h5_path), "video": vres}

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

    # Turn the raw .h264 into a playable/DLC-ready video.mp4 (stream copy) and drop the raw.
    vres = _remux_and_prune_video(video_session_dir, h5_path) if remux_video else {"skipped": "deferred to the Data-tab sync"}
    return {"ok": True, "h5": str(h5_path), "merged": merged, "removed": removed, "video": vres}
