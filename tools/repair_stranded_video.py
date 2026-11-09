#!/usr/bin/env python3
"""tools/repair_stranded_video.py — finish video remuxes that were stranded in the controller's copies.

Until 2026-11 the Leader remuxed video.h264 -> video.mp4 at the engine's exit, and pi_api SIGKILLed the
engine 10 s after SIGTERM — so every recording longer than ~75 min (>= ~6 GB) was left as video.h264 plus
a complete but never renamed video.mp4.part, and the Data tab copied BOTH to the controller (11 cheddar
sessions, 77 GB of duplicates, 2026-09-18..11-07). This walks the data root and, per session folder, runs
the same shared.consolidate._remux_and_prune_video the Pi runs at sync — adopt-only, no ffmpeg:

  video.h264 + complete video.mp4.part   -> rename the .part to video.mp4 (its mtime is kept, so the Pi's
                                            own adopted copy still matches it for rsync), delete the raw
  video.mp4 + leftovers (.h264 / .part)  -> verify video.mp4, delete the leftovers; a BROKEN video.mp4 (no
                                            index) is replaced by a complete .part
  anything else that does not verify     -> reported, nothing touched (a complete video.mp4 that is not this
                                            recording — a reused session id — is never deleted)

Verification: size >= 95 % of the raw, a readable MP4 index (ffprobe), and at least as many frames as the
h5's /camera frame_count. DRY RUN by default; --apply makes the changes.

    conda activate vrfarm && python tools/repair_stranded_video.py            # report only
    conda activate vrfarm && python tools/repair_stranded_video.py --apply    # do it

--remux-raw also remuxes sessions that have ONLY a raw video.h264 (recordings from before the remux
existed, 2026-08: 17 sessions, one of 138 GB) with the controller's ffmpeg — slow, off by default.
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.consolidate import (PART_SETTLE_S, _remux_and_prune_video, _verify_mp4,   # noqa: E402
                                _video_h5_info)


def _gb(n):
    return f"{(n or 0) / 1e9:6.2f}"


def plan(d: Path, remux_raw: bool) -> dict | None:
    """What a run would do in this session dir (None when there is nothing to do)."""
    raw, mp4, part = d / "video.h264", d / "video.mp4", d / "video.mp4.part"
    have_raw, have_mp4, have_part = raw.exists(), mp4.exists(), part.exists()
    if not (have_part or (have_raw and (have_mp4 or remux_raw))):
        return None
    h5s = sorted(d.glob("*.h5"))
    info = _video_h5_info(h5s[0]) if h5s else {"fps": None, "frame_count": None}
    raw_size = raw.stat().st_size if have_raw else None
    row = {"dir": d, "raw": raw_size, "part": part.stat().st_size if have_part else None,
           "mp4": mp4.stat().st_size if have_mp4 else None, "h5": bool(h5s),
           "frame_count": info["frame_count"], "frames": None, "verdict": "", "action": "", "frees": 0}
    if have_part and time.time() - part.stat().st_mtime < PART_SETTLE_S:
        row.update(verdict="part is fresh", action="leave (remux in progress?)")
        return row
    if have_mp4:
        ok, reason, frames = _verify_mp4(mp4, raw_size, info["frame_count"])
        row["frames"] = frames
        if ok:
            row.update(verdict="mp4 verified", action="delete leftovers",
                       frees=(raw_size or 0) + (row["part"] or 0))
            return row
        if reason != "incomplete":
            row.update(verdict=f"mp4 is not this recording ({reason}: {frames} frames vs {info['frame_count']} logged)",
                       action="leave — inspect (session id reused?)")
            return row
        pok, preason, pframes = _verify_mp4(part, raw_size, info["frame_count"]) if have_part else (False, None, None)
        if pok:
            row.update(frames=pframes, verdict=f"mp4 {reason}, part complete",
                       action="adopt the part over the mp4" + (", delete raw" if have_raw else ""),
                       frees=(raw_size or 0) + row["mp4"])
        elif have_raw and remux_raw:
            row.update(verdict=f"mp4 {reason}" + (f", part {preason}" if have_part else ""), action="remux the raw over the mp4")
        else:
            row.update(verdict=f"mp4 {reason}" + (f", part {preason}" if have_part else ""), action="leave")
        return row
    if have_part:
        ok, reason, frames = _verify_mp4(part, raw_size, info["frame_count"])
        row["frames"] = frames
        if ok:
            row.update(verdict="part complete", action="adopt as video.mp4" + (", delete raw" if have_raw else ""),
                       frees=raw_size or 0)
        elif reason in ("too_small", "incomplete") and have_raw and remux_raw:
            row.update(verdict=f"part {reason}", action="delete part, remux raw")
        else:
            row.update(verdict=f"part {reason}", action="leave")
        return row
    row.update(verdict="raw only", action="remux with ffmpeg" if remux_raw else "leave", frees=0)
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=None, help="default: the controller's data root (controller.yaml / $VRFARM_DATA_DIR / ~/VRFarm/data)")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: report only)")
    ap.add_argument("--remux-raw", action="store_true", help="also remux raw-only sessions with ffmpeg (slow)")
    a = ap.parse_args(argv)
    if a.data_root:
        root = Path(a.data_root).expanduser()
    else:
        from controller import settings
        root = settings.data_root()
    if not root.is_dir():
        print(f"error: data root {root} is not a directory")
        return 1

    rows = []
    for subj in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for date_dir in sorted(p for p in subj.iterdir() if p.is_dir()):
            for sd in sorted(p for p in date_dir.iterdir() if p.is_dir() and p.name != ".rsync-partial"):
                r = plan(sd, a.remux_raw)
                if r:
                    rows.append(r)
    if not rows:
        print(f"nothing to repair under {root}")
        return 0

    print(f"{'session':26s} {'raw GB':>7s} {'part GB':>8s} {'mp4 GB':>7s} {'h5 fr':>8s} {'mp4 fr':>8s}  verdict / action")
    frees = 0
    for r in rows:
        print(f"{r['dir'].name:26s} {_gb(r['raw']) if r['raw'] else '      -':>7s} {_gb(r['part']) if r['part'] else '       -':>8s} "
              f"{_gb(r['mp4']) if r['mp4'] else '      -':>7s} {str(r['frame_count'] or '-'):>8s} {str(r['frames'] or '-'):>8s}  "
              f"{r['verdict']} / {r['action']}" + ("" if r["h5"] else "  (no .h5)"))
        frees += r["frees"]
    print(f"\n{len(rows)} session(s); {frees / 1e9:.1f} GB to free")
    if not a.apply:
        print("dry run — rerun with --apply to make these changes")
        return 0

    bad = 0
    for r in rows:
        d = r["dir"]
        h5s = sorted(d.glob("*.h5"))
        res = _remux_and_prune_video(d, h5s[0] if h5s else d / "none.h5", run_ffmpeg=a.remux_raw)
        tag = "adopted" if res.get("adopted") else "remuxed" if res.get("remuxed") else "pruned" if res.get("pruned") \
            else "skipped" if res.get("skipped") else "ERROR"
        if tag == "ERROR":
            bad += 1
        print(f"  {d.name:26s} {tag:8s} {res}")
    left = [p for p in root.glob("*/*/*/video.mp4.part")]
    print(f"\ndone: {len(rows) - bad} ok, {bad} with errors; {len(left)} video.mp4.part left under {root}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
