"""
shared/consolidate.py

Fold a session's sidecar files into the single HDF5, reorganize it into groups,
then delete the sidecars — so the archived artifact is one self-contained
`<session_id>.h5` (plus the video.h264, which stays a file).

Runs on the Leader via pi_api at transfer time: the engine process that wrote
the .h5 is already gone, so a fresh process opens the closed file and rebuilds it.

Merges (then deletes the source):
  metadata.yaml        -> root attributes (+ task_config as a JSON string)
  frame_timestamps.npy -> /camera/frame_timestamps  (Nx3, with a `columns` attr)
  camera_metadata.json -> /camera attrs (the geometry/settings the video was shot with)

Reorganizes the flat per-trial datasets the engine wrote into groups: anything not
listed in _GROUP lands under /trials.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

FORMAT_VERSION = 2

# flat dataset (engine output) -> destination group. Extend when a device family
# should get its own group (e.g. a continuous stream); unknown names -> /trials.
_GROUP = {}


def _remux_and_prune_video(video_session_dir, h5_path):
    """Copy-remux video.h264 -> video.mp4 (container only, NO re-encode) and delete the raw once
    the mp4 is verified. Runs on the Leader at consolidation, after recording has stopped.

    Why here: raw .h264 is what the encoder writes (crash-safe, append-only), but it has no
    container/timing — players and DeepLabCut read the wrong fps and can't seek. A stream copy
    into MP4 is near-free (no re-encode, ~disk-speed) and fixes that. We force the frame rate from
    the h5 because the raw stream's embedded rate is unreliable.

    Never raises: on ANY problem it leaves the raw .h264 in place and reports why, so a bad remux
    can never cost a recording. Idempotent — skips cleanly if the mp4 already exists or ffmpeg is
    absent."""
    import shutil
    import subprocess
    import h5py

    if not video_session_dir:
        return {"skipped": "no video dir"}
    vdir = Path(video_session_dir)
    raw = vdir / "video.h264"
    mp4 = vdir / "video.mp4"
    if mp4.exists() and not raw.exists():
        return {"skipped": "already mp4"}
    if not raw.exists():
        return {"skipped": "no raw video"}
    if shutil.which("ffmpeg") is None:
        return {"error": "ffmpeg not found; kept raw .h264"}

    # The raw stream's frame rate is unreliable (ffprobe reads 120/25 on our files), so force a
    # constant rate from the h5 — requested_fps preferred, else measured, rounded to an int.
    fps = 30
    try:
        with h5py.File(h5_path, "r") as f:
            cam = dict(f["camera"].attrs) if "camera" in f else {}
        fps = int(round(float(cam.get("requested_fps") or cam.get("measured_fps") or 30)))
    except Exception:
        pass
    if fps <= 0:
        fps = 30

    tmp = vdir / "video.mp4.part"
    raw_size = raw.stat().st_size
    # -r before -i forces the input rate; -c:v copy = stream copy (no re-encode); -f mp4 names the
    # muxer explicitly (the temp file's .part extension can't be auto-detected); no +faststart (it
    # would rewrite the whole file — pointless for local/DLC use); -an = no audio.
    cmd = ["ffmpeg", "-y", "-v", "error", "-fflags", "+genpts", "-r", str(fps),
           "-i", str(raw), "-c:v", "copy", "-an", "-f", "mp4", str(tmp)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3600)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        err = getattr(e, "stderr", "") or str(e)
        return {"error": f"remux failed ({str(err)[:120]}); kept raw .h264", "fps": fps}

    # A stream copy must land ~the same size; a much smaller file = a truncated/failed remux, and
    # we must NOT delete the raw in that case.
    got = tmp.stat().st_size if tmp.exists() else 0
    if got < raw_size * 0.95:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"error": f"remux output too small ({got}/{raw_size} bytes); kept raw .h264",
                "fps": fps}
    os.replace(tmp, mp4)
    raw.unlink()
    return {"remuxed": "video.mp4", "fps": fps, "removed_raw": True, "mp4_bytes": got}


def consolidate_session(session_dir, video_session_dir=None, delete_sidecars=True, remux_video=True):
    """Build the final self-contained HDF5 for a session and remove sidecars.

    session_dir: dir holding <session_id>.h5 + metadata.yaml.
    video_session_dir: dir holding frame_timestamps.npy (+ video.h264); optional.
    remux_video: copy-remux video.h264 -> video.mp4 and delete the raw (default True).
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
        vres = _remux_and_prune_video(video_session_dir, h5_path) if remux_video else {"skipped": "disabled"}
        return {"ok": True, "skipped": "already consolidated", "h5": str(h5_path), "video": vres}

    meta_path = session_dir / "metadata.yaml"
    tslog_path = Path(video_session_dir) / "frame_timestamps.npy" if video_session_dir else None
    geom_path = Path(video_session_dir) / "camera_metadata.json" if video_session_dir else None

    merged = {"metadata": False, "camera": False, "camera_geometry": False}
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

            # 3) frame_timestamps.npy -> /camera/frame_timestamps
            if tslog_path and tslog_path.exists():
                ts = np.load(tslog_path)
                d = dst.create_dataset("camera/frame_timestamps", data=ts)
                d.attrs["columns"] = ["frame_idx", "wall_clock_s", "sensor_ts_ns"]
                merged["camera"] = True

            # 4) camera_metadata.json -> /camera attrs. Sensor mode, ScalerCrop, output size,
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
        for p in (meta_path, tslog_path, geom_path):
            if p and Path(p).exists():
                Path(p).unlink()
                removed.append(Path(p).name)

    # Turn the raw .h264 into a playable/DLC-ready video.mp4 (stream copy) and drop the raw.
    vres = _remux_and_prune_video(video_session_dir, h5_path) if remux_video else {"skipped": "disabled"}
    return {"ok": True, "h5": str(h5_path), "merged": merged, "removed": removed, "video": vres}
