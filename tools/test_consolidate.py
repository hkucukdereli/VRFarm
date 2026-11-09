#!/usr/bin/env python3
"""tools/test_consolidate.py — the video remux / adopt / prune logic of shared/consolidate.py and the
raw-video fields of shared/leader_data.py, on tiny real H.264 fixtures. Needs ffmpeg + ffprobe on this
machine for most cases (they are skipped, not failed, without them). No hardware.
    conda activate vrfarm && python tools/test_consolidate.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import h5py

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared import consolidate as C                                            # noqa: E402
from shared.consolidate import _remux_and_prune_video, _verify_mp4, consolidate_session   # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


FF = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FPS, N = 10, 10
TMP = Path(tempfile.mkdtemp(prefix="vrfarm_consolidate_test_"))
FX = TMP / "fx"
FX.mkdir()


def make_h5(path, frame_count=N, consolidated=True, camera=True):
    with h5py.File(str(path), "w") as f:
        f.create_dataset("trial_num", data=[0, 1, 2])
        if consolidated:
            f.attrs["format_version"] = 2
            f.create_group("trials")
        if camera:
            g = f.create_group("camera")
            g.attrs["requested_fps"] = FPS
            if frame_count is not None:
                g.attrs["frame_count"] = frame_count


def age(p, seconds=C.PART_SETTLE_S + 600):
    t = time.time() - seconds
    os.utime(p, (t, t))


def session(name, raw=False, part=None, mp4=False, frame_count=N, aged=True):
    """A session dir with its h5 and the asked-for video files. part: None | 'complete' | 'truncated'."""
    d = TMP / name
    d.mkdir()
    make_h5(d / f"{name}.h5", frame_count)
    if raw:
        shutil.copy2(FX / "video.h264", d / "video.h264")
    if mp4:
        shutil.copy2(FX / "video.mp4.part", d / "video.mp4")
    if part == "complete":
        shutil.copy2(FX / "video.mp4.part", d / "video.mp4.part")
    elif part == "truncated":
        data = (FX / "video.mp4.part").read_bytes()
        (d / "video.mp4.part").write_bytes(data[: len(data) // 2])
    if part and aged:
        age(d / "video.mp4.part")
    return d


def files(d):
    return sorted(p.name for p in d.iterdir() if p.name.startswith("video"))


def run(d, **kw):
    return _remux_and_prune_video(d, next(d.glob("*.h5")), **kw)


print(f"fixtures in {TMP}  (ffmpeg/ffprobe: {'yes' if FF else 'NO — remux cases skipped'})")
if FF:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration=1:size=64x64:rate={FPS}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "5", "-f", "h264", str(FX / "video.h264")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-fflags", "+genpts", "-r", str(FPS), "-i", str(FX / "video.h264"),
                    "-c:v", "copy", "-an", "-f", "mp4", str(FX / "video.mp4.part")], check=True)
    raw_size = (FX / "video.h264").stat().st_size
    part_size = (FX / "video.mp4.part").stat().st_size
    check(raw_size > 1000 and part_size >= raw_size * 0.95, f"fixture: raw {raw_size} B, mp4 {part_size} B")

    print("— _verify_mp4")
    ok, reason, frames = _verify_mp4(FX / "video.mp4.part", raw_size, N)
    check(ok and reason is None and frames == N, f"complete part verifies ({reason}, {frames} frames)")
    trunc = FX / "trunc.part"
    trunc.write_bytes((FX / "video.mp4.part").read_bytes()[: part_size // 2])
    ok, reason, _ = _verify_mp4(trunc, raw_size, N)
    check(not ok and reason in ("too_small", "incomplete"), f"truncated part fails ({reason})")
    ok, reason, frames = _verify_mp4(FX / "video.mp4.part", raw_size, 50)
    check(not ok and reason == "frames_short" and frames == N, f"short vs h5 frame_count 50 -> {reason}")
    ok, reason, frames = _verify_mp4(FX / "video.mp4.part", None, None)
    check(ok and frames == N, "no raw, no frame_count: index alone verifies")

    print("— raw only")
    d = session("A_raw", raw=True)
    r = run(d)
    check(r.get("remuxed") == "video.mp4" and r.get("removed_raw") and r.get("frames") == N and files(d) == ["video.mp4"],
          f"raw -> remuxed, raw gone: {r}")
    check(run(d) == {"skipped": "already mp4"}, "second pass: already mp4")

    print("— raw + complete settled .part: adopt")
    d = session("B_adopt", raw=True, part="complete")
    ino, mt = (d / "video.mp4.part").stat().st_ino, (d / "video.mp4.part").stat().st_mtime
    r = run(d)
    st = (d / "video.mp4").stat()
    check(r.get("adopted") == "video.mp4" and r.get("removed_raw") and r.get("frames") == N and files(d) == ["video.mp4"],
          f"adopted, raw gone: {r}")
    check(st.st_ino == ino and abs(st.st_mtime - mt) < 1e-6, "adoption is a rename: same inode, mtime kept")

    print("— raw + truncated .part")
    d = session("C_trunc", raw=True, part="truncated")
    r = run(d, run_ffmpeg=False)
    check("left for inspection" in r.get("error", "") and files(d) == ["video.h264", "video.mp4.part"],
          f"adopt-only: a truncated part is reported, not deleted: {r}")
    r = run(d)
    check(r.get("remuxed") == "video.mp4" and files(d) == ["video.mp4"] and C._mp4_frames(d / "video.mp4") == N,
          f"ffmpeg mode: truncated part replaced by a fresh remux: {r}")

    print("— raw + .part shorter than the camera's log (h5 frame_count 50)")
    d = session("D_short", raw=True, part="complete", frame_count=50)
    r = run(d, run_ffmpeg=False)
    check("left for inspection" in r.get("error", "") and files(d) == ["video.h264", "video.mp4.part"],
          f"adopt-only mode leaves it: {r}")
    r = run(d)
    check(r.get("remuxed") and r.get("warning") and files(d) == ["video.mp4"], f"ffmpeg mode re-remuxes with a warning: {r}")

    print("— raw + fresh .part (someone's ffmpeg)")
    d = session("E_fresh", raw=True, part="complete", aged=False)
    r = run(d)
    check(r == {"skipped": "remux in progress"} and files(d) == ["video.h264", "video.mp4.part"], f"left alone: {r}")

    print("— mp4 + leftovers")
    d = session("F_mp4_raw", raw=True, mp4=True)
    r = run(d)
    check(r.get("pruned") and r.get("removed_raw") and files(d) == ["video.mp4"], f"raw next to a verified mp4 pruned: {r}")
    d = session("G_mp4_all", raw=True, mp4=True, part="complete")
    r = run(d)
    check(r.get("pruned") and r.get("removed_part") and files(d) == ["video.mp4"], f"raw + part next to a verified mp4 pruned: {r}")
    d = session("G2_bad_mp4", raw=True)
    (d / "video.mp4").write_bytes(b"\0" * 100)
    r = run(d, run_ffmpeg=False)
    check("nothing here can replace it" in r.get("error", "") and files(d) == ["video.h264", "video.mp4"], f"adopt-only: a broken mp4 keeps everything: {r}")
    r = run(d)
    check(r.get("remuxed") and r.get("replaced_mp4") == "incomplete" and files(d) == ["video.mp4"] and C._mp4_frames(d / "video.mp4") == N,
          f"ffmpeg mode: the raw is remuxed over the broken mp4: {r}")
    d = session("G3_bad_mp4_part", raw=True, part="complete")
    (d / "video.mp4").write_bytes((FX / "video.mp4.part").read_bytes()[: part_size // 2])
    ino = (d / "video.mp4.part").stat().st_ino
    r = run(d, run_ffmpeg=False)
    check(r.get("adopted") and r.get("replaced_mp4") == "incomplete" and r.get("removed_raw") and files(d) == ["video.mp4"]
          and (d / "video.mp4").stat().st_ino == ino, f"a complete part is adopted over a truncated mp4: {r}")
    # a COMPLETE mp4 of a different (shorter) recording — a reused session id: never touched
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration=0.5:size=64x64:rate={FPS}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "5", "-f", "mp4", str(FX / "other.mp4")], check=True)
    d = session("G5_other_mp4", raw=True, part="complete")
    shutil.copy2(FX / "other.mp4", d / "video.mp4")
    for mode in (False, True):
        r = run(d, run_ffmpeg=mode)
        check("does not match this recording" in r.get("error", "") and files(d) == ["video.h264", "video.mp4", "video.mp4.part"],
              f"run_ffmpeg={mode}: a complete mp4 of another recording is left alone with the pair: {r}")
    d = session("G4_bad_mp4_bad_part", raw=True, part="truncated")
    (d / "video.mp4").write_bytes(b"\0" * 100)
    r = run(d, run_ffmpeg=False)
    check("left for inspection" in r.get("error", "") and "video.mp4 fails too" in r["error"]
          and files(d) == ["video.h264", "video.mp4", "video.mp4.part"], f"adopt-only: nothing verifies, nothing deleted: {r}")

    print("— .part only")
    d = session("I_part_only", part="complete")
    r = run(d)
    check(r.get("adopted") and files(d) == ["video.mp4"], f"adopted without a raw: {r}")

    print("— consolidate_session")
    d = session("L_defer", raw=True)
    res = consolidate_session(d, d, remux_video=False)
    check(res["video"] == {"skipped": "deferred to the Data-tab sync"} and files(d) == ["video.h264"],
          f"remux_video=False leaves the raw (the engine's exit): {res['video']}")
    res = consolidate_session(d, d)
    check(res["video"].get("remuxed") and files(d) == ["video.mp4"], f"default remuxes on an already-consolidated h5: {res['video']}")

print("— without ffmpeg/ffprobe (shutil.which patched)")
if FF:
    d1 = session("K_raw_noff", raw=True)
    d2 = session("K_part_noff", raw=True, part="complete")
else:
    d1 = session("K_raw_noff")
    (d1 / "video.h264").write_bytes(b"x" * 5000)
    d2 = session("K_part_noff")
    (d2 / "video.h264").write_bytes(b"x" * 5000)
    (d2 / "video.mp4.part").write_bytes(b"y" * 5000)
    age(d2 / "video.mp4.part")
orig_which = shutil.which
shutil.which = lambda name, *a, **k: None
try:
    r1, r2 = run(d1), run(d2)
finally:
    shutil.which = orig_which
check(r1 == {"error": "ffmpeg not found; kept raw .h264"} and files(d1) == ["video.h264"], f"raw kept: {r1}")
check("no ffprobe" in r2.get("error", "") and files(d2) == ["video.h264", "video.mp4.part"], f"unverifiable part left as is: {r2}")

print("— adopt-only mode on a raw")
d = session("J_adopt_only")
(d / "video.h264").write_bytes(b"x" * 5000)
r = run(d, run_ffmpeg=False)
check(r == {"skipped": "adopt only"} and files(d) == ["video.h264"], f"nothing touched: {r}")

print("— leader_data inventory / consolidate")
PY = sys.executable


def leader_data(*args):
    r = subprocess.run([PY, "-m", "shared.leader_data", *args], cwd=str(ROOT), capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": None, "error": (r.stderr or r.stdout)[-400:]}


tree = TMP / "tree"
pi_data, pi_video = tree / "data", tree / "video"
for sid, kind in (("M1_20260901_001", "raw"), ("M1_20260901_002", "mp4"), ("M1_20260901_003", "part")):
    dd = pi_data / "M1" / "M1_20260901" / sid
    dd.mkdir(parents=True)
    make_h5(dd / f"{sid}.h5")
    vd = pi_video / "M1" / "M1_20260901" / sid
    vd.mkdir(parents=True)
    if FF:
        shutil.copy2(FX / "video.h264", vd / ("video.h264" if kind != "mp4" else "video.mp4"))
        if kind == "part":
            shutil.copy2(FX / "video.mp4.part", vd / "video.mp4.part")
            age(vd / "video.mp4.part")
    else:
        (vd / ("video.h264" if kind != "mp4" else "video.mp4")).write_bytes(b"x" * 3000)
        if kind == "part":
            (vd / "video.mp4.part").write_bytes(b"y" * 3000)
inv = leader_data("inventory", "--leader-dir", str(pi_data), "--video-dir", str(pi_video))
f1 = inv["folders"][0] if inv.get("folders") else {}
by = {s["id"]: s for s in f1.get("sessions", [])}
check(inv.get("ok") and f1.get("n_raw_video") == 2 and f1.get("n_unconsolidated") == 0,
      f"separate tree: n_raw_video 2 (raw + part), n_unconsolidated 0: {f1.get('n_raw_video')}, {inv.get('error')}")
check(by.get("M1_20260901_001", {}).get("video_raw") is True and by.get("M1_20260901_002", {}).get("video_raw") is False
      and by.get("M1_20260901_003", {}).get("video_raw") is True and all(s["has_video"] for s in by.values()),
      "per-session video_raw flags; a raw still counts as has_video")
# same-tree layout: video in the session dir, no --video-dir
same = TMP / "same"
sd = same / "M2" / "M2_20260902" / "M2_20260902_001"
sd.mkdir(parents=True)
make_h5(sd / "M2_20260902_001.h5")
if FF:
    shutil.copy2(FX / "video.h264", sd / "video.h264")
else:
    (sd / "video.h264").write_bytes(b"x" * 3000)
inv2 = leader_data("inventory", "--leader-dir", str(same))
f2 = inv2["folders"][0] if inv2.get("folders") else {}
check(inv2.get("ok") and f2.get("n_raw_video") == 1 and f2["sessions"][0].get("video_raw") is True, f"same-tree layout flags the raw: {f2.get('n_raw_video')}")
if FF:
    c = leader_data("consolidate", "--leader-dir", str(pi_data), "--video-dir", str(pi_video), "--folder", "M1/M1_20260901")
    check(c.get("ok") and c.get("video_remuxed") == 1 and c.get("video_adopted") == 1 and not c.get("video_errors"),
          f"consolidate: 1 remuxed, 1 adopted, no errors: {c.get('video_remuxed')}, {c.get('video_adopted')}, {c.get('video_errors')}")
    vids = {sid: files(pi_video / "M1" / "M1_20260901" / sid) for sid in by}
    check(all(v == ["video.mp4"] for v in vids.values()), f"every session now has video.mp4 only: {vids}")
    inv = leader_data("inventory", "--leader-dir", str(pi_data), "--video-dir", str(pi_video))
    check(inv["folders"][0].get("n_raw_video") == 0, "n_raw_video 0 afterwards")
    c2 = leader_data("consolidate", "--leader-dir", str(same))
    check(c2.get("ok") and c2.get("video_remuxed") == 1 and files(sd) == ["video.mp4"], f"same-tree consolidate remuxes in the session dir: {c2.get('video_remuxed')}")
else:
    print("  skip consolidate cases (no ffmpeg)")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'PASS' if not fails else f'{fails} FAILURE(S)'}")
sys.exit(1 if fails else 0)
