#!/usr/bin/env python3
"""
tools/consolidate_session.py

Manually consolidate ONE session folder on the Leader Pi (cheddar) — folds the
sidecars (metadata.yaml, stimuli.npz, trials.yaml, frame_timestamps.npy) into a
single self-contained <session_id>.h5 and deletes them. This is exactly what the
app's Transfer button does via pi_api; use this when you skipped Transfer and want
the merged file before copying it off the Pi.

Run under the `rig` conda env (needs h5py, numpy, pyyaml):

    source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig
    python ~/rig/tools/consolidate_session.py ~/data/ASD109/ASD109_20260806/ASD109_20260806_001

Idempotent: a folder already at format v2 is left untouched. Camera timestamps are
merged automatically if frame_timestamps.npy is found (in the session folder, next
to <video_session_dir>, or on the SSD); pass a second arg to point at it explicitly.
"""
from __future__ import annotations
import sys
from pathlib import Path

# Find the rig root (the dir containing shared/consolidate.py), whether this script
# sits in <rig>/, <rig>/tools/, or anywhere above shared/ — then make `shared` importable.
_here = Path(__file__).resolve().parent
for _cand in (_here, *_here.parents):
    if (_cand / "shared" / "consolidate.py").exists():
        sys.path.insert(0, str(_cand))
        break


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: consolidate_session.py <session_dir> [video_session_dir]")
        print("  <session_dir>       folder holding <session_id>.h5 + metadata.yaml/stimuli.npz")
        print("  [video_session_dir] folder holding frame_timestamps.npy (auto-detected if omitted)")
        return 2

    session_dir = Path(argv[0]).expanduser().resolve()
    if not session_dir.is_dir():
        print(f"error: not a directory: {session_dir}")
        return 1

    # Locate frame_timestamps.npy for the /camera merge.
    if len(argv) > 1:
        video_dir = Path(argv[1]).expanduser().resolve()
    elif (session_dir / "frame_timestamps.npy").exists():
        video_dir = session_dir                                   # video written alongside data
    else:
        # standard rig keeps video on the SSD, mirroring the <subj>/<subj_date>/<session_id> tree
        parts = session_dir.parts
        guess = Path("/media/vruser/ssd/video", *parts[-3:]) if len(parts) >= 3 else None
        video_dir = guess if (guess and (guess / "frame_timestamps.npy").exists()) else None

    if video_dir is None:
        print("note: no frame_timestamps.npy found — camera timestamps will not be merged.")

    try:
        from shared.consolidate import consolidate_session
    except ImportError as e:
        print(f"error: cannot import shared.consolidate ({e}). Run from the rig tree, "
              f"in the `rig` env (h5py/numpy/pyyaml).")
        return 1

    res = consolidate_session(session_dir, video_dir)
    print("result:", res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
