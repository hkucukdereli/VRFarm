#!/usr/bin/env python3
"""
shared/leader_data.py — the leader Pi's side of the controller's Data tab.

The controller talks to leaders over SSH only (no pi_api) and runs this script for anything
that needs to look at or change files on the Pi. rsync does the copying itself. Runs under
the rig conda env (h5py for the format check):

    cd ~/rig && python -m shared.leader_data inventory   --leader-dir ~/data --video-dir /media/vruser/ssd/video [--video-mount /media/vruser/ssd] [--folder S/S_YYYYMMDD ...]
    cd ~/rig && python -m shared.leader_data consolidate --leader-dir ... --video-dir ... [--video-mount ...] [--folder S/S_YYYYMMDD ...]
    cd ~/rig && python -m shared.leader_data purge       --leader-dir ... --video-dir ... [--video-mount ...] --folder S/S_YYYYMMDD ... --verified data:S/S_YYYYMMDD ...
    cd ~/rig && python -m shared.leader_data hash        --leader-dir ... --video-dir ... [--video-mount ...] --folder S/S_YYYYMMDD ...
    cd ~/rig && python -m shared.leader_data read        --leader-dir ... --video-dir ... [--video-mount ...] --folder S/S_YYYYMMDD --tree data|video --file <path inside the folder>
    cd ~/rig && python -m shared.leader_data prune-logs  --days 7

Every command prints ONE JSON object on stdout, always carrying `proto` (PROTO below) — except
`read`, whose stdout is the file itself. Folder names are always `<subject>/<subject>_<date>`
("date folders").

Protocol 2 — sessions without video, and a purge that only deletes what was verified:
- A date folder may exist in the data tree, the video tree, or both. A session recorded with the
  camera unchecked has no video folder at all; that is normal, not an error. The inventory reports
  per folder which trees hold it (`in_data`, `in_video`) and per session whether video exists
  (`has_video`) and whether the controller recorded that the camera was saving (`camera_saved`,
  from the .h5 root attrs or metadata.yaml; None = unknown, e.g. sessions before protocol 2).
- `video_root.available` says whether the video tree can be trusted. By default `video_dir` is a
  plain directory (cheddar: a folder on the boot NVMe). With `--video-mount`, that path must be a
  mountpoint and `video_dir` must sit under it — the guard against an unmounted external SSD.
- purge deletes a tree's copy of a folder ONLY if the controller names that tree with
  `--verified TREE:FOLDER` (it re-verified the copy moments before). A folder present in a tree
  that was not verified, or a verified tree whose folder has vanished, is refused whole. A caller
  that sends no --verified at all (an older controller) is refused outright.
- Video remux (optional additions to protocol 2, 2026-11): the engine no longer remuxes at its exit,
  so a fresh session holds a raw `video.h264`. The inventory reports per session `video_raw` (a
  `video.h264` or `video.mp4.part` is present) and per folder `n_raw_video`; `consolidate` turns
  those into `video.mp4` (shared/consolidate.py: stream copy, or adoption of a complete leftover
  `.part`) and summarizes `video_adopted` / `video_remuxed` / `video_errors`. A leader without these
  fields (older deploy) simply reports nothing, and the controller takes no video action for it.
  `n_unconsolidated` is untouched by any of this: a raw video never blocks a purge.

Protocol 3 — the content of a copy is checked, not only its size and time (2026-11):
- rsync calls a file copied when size and mtime agree, and its sender reads through the page cache.
  On the Pi 5 kernel 6.18.34 that cache handed out wrong bytes once memory had filled (morbier and
  pecorino, found 2026-11-12; reads with O_DIRECT were always right, 6.18.39 is clean), so a copy
  could hold wrong bytes and still verify — rsync's checksum mode included, it hashes what the same
  cache hands out. `hash` reads every file the rsync copies (same excludes) with O_DIRECT and reports
  its SHA-256; the controller hashes its own copy the same way and compares. `read` streams one file,
  again with O_DIRECT: how the controller fetches a file whose copy does not match.
- The inventory carries `fp` per folder: one hash per tree over the names, sizes and mtimes (ns) of
  those files. The controller stores it with a passed content check; the same value in a later
  inventory means the Pi's files are still the ones that were checked.
"""
from __future__ import annotations
import argparse
import errno
import hashlib
import json
import mmap
import os
import re
import shutil
import socket
import stat
import sys
import time
from pathlib import Path

PROTO = 3
FOLDER_RE = re.compile(r"^([A-Za-z0-9-]+)/\1_(\d{8})$")
ENGINE_MARK = "engine/leader.py"
VIDEO_NAMES = ("video.mp4", "video.h264")
RAW_VIDEO_NAMES = ("video.h264", "video.mp4.part")   # not yet remuxed — `consolidate` turns these into video.mp4
TREES = ("data", "video")
HASH_ALGO = "sha256"
READ_BUF = 4 << 20             # direct reads want a buffer that is a multiple of every block and page size
# What the controller's rsync leaves out of a copy (controller/sync.py: TEMP_EXCLUDES, .DS_Store and
# rsync's own partial dir) — the content check has to look at the same set of files.
SKIP_NAMES = (".DS_Store",)
SKIP_SUFFIXES = (".part", ".consolidating")
SKIP_DIRS = (".rsync-partial",)


def _out(obj):
    obj.setdefault("proto", PROTO)
    print(json.dumps(obj), flush=True)


def engine_running() -> bool:
    """Is engine/leader.py running? Scans process command lines from inside Python — never a
    shell `pgrep -f`, which would match the shell that carries the pattern (CLAUDE.md)."""
    me = os.getpid()
    proc = Path("/proc")
    if proc.is_dir():
        for p in proc.iterdir():
            if not p.name.isdigit() or int(p.name) == me:
                continue
            try:
                cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except Exception:
                continue
            if ENGINE_MARK in cmd:
                return True
        return False
    # macOS (the smoke test runs this locally): ps, again excluding ourselves
    try:
        import subprocess
        out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) != me and ENGINE_MARK in parts[1]:
                return True
    except Exception:
        pass
    return False


def _du(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _disk(path: Path):
    try:
        u = shutil.disk_usage(str(path))
        return {"free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1), "mount": str(path)}
    except Exception:
        return None


def _as_bool(v):
    """A stored flag as True/False, or None when absent or not a boolean. YAML gives a bool; h5py
    gives a numpy bool scalar, whose type is `numpy.bool` on numpy 2 (`numpy.bool_` on 1.x), so
    it is recognised by dtype, not by type name. Strings, ints and "" are not trusted."""
    if isinstance(v, bool):
        return v
    dt = getattr(v, "dtype", None)
    if dt is not None and dt.kind == "b" and getattr(v, "shape", None) == ():
        return bool(v)
    return None


def _h5_info(h5: Path) -> dict:
    """format_version plus the camera flags from the .h5 root attrs, in one open."""
    out = {"format_version": None, "camera_saved": None, "camera_requested": None}
    try:
        import h5py
        with h5py.File(str(h5), "r") as f:
            v = f.attrs.get("format_version", 0)
            if v == 0 and "trials" in f:
                v = 2
            out["format_version"] = int(v)
            for k in ("camera_saved", "camera_requested"):
                out[k] = _as_bool(f.attrs.get(k))
    except Exception as e:
        out["format_version"] = f"error: {e}"
    return out


def _format_version(h5: Path):
    return _h5_info(h5)["format_version"]


def _meta_camera(sd: Path) -> dict:
    """The camera flags from an unconsolidated session's metadata.yaml (None when unknown)."""
    out = {"camera_saved": None, "camera_requested": None}
    p = sd / "metadata.yaml"
    if not p.is_file():
        return out
    try:
        import yaml
        m = yaml.safe_load(p.read_text()) or {}
        for k in out:
            out[k] = _as_bool(m.get(k))
    except Exception:
        pass
    return out


def _folders(root: Path | None):
    """All <subject>/<subject>_<date> folders under a tree, as (rel, subject, date, path)."""
    out = []
    if root is None or not root.is_dir():
        return out
    for subj in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            children = sorted(p for p in subj.iterdir() if p.is_dir())
        except OSError:
            continue
        for d in children:
            rel = f"{subj.name}/{d.name}"
            m = FOLDER_RE.match(rel)
            if m:
                out.append((rel, m.group(1), m.group(2), d))
    return out


def _same_path(a: Path, b: Path) -> bool:
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


def _is_under(child: Path, parent: Path) -> bool:
    c, p = os.path.realpath(str(child)), os.path.realpath(str(parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _dirs(a):
    leader_dir = Path(a.leader_dir).expanduser()
    video_dir = Path(a.video_dir).expanduser() if a.video_dir else None
    video_mount = Path(a.video_mount).expanduser() if getattr(a, "video_mount", None) else None
    return leader_dir, video_dir, video_mount


def _roots(leader_dir: Path, video_dir: Path | None, video_mount: Path | None) -> dict:
    """Can each tree be trusted right now?

    data: the directory must exist and be readable.
    video: `separate` is False when video lives in the data session dirs (no video_dir, or it is
    the data dir) — then there is no second tree and it is trivially available. Otherwise, with a
    configured video_mount the mount must be mounted and video_dir must sit under it; the video
    dir itself may not exist yet (nothing recorded), but if it exists it must be readable."""
    data = {"path": str(leader_dir), "available": True, "reason": None}
    if not leader_dir.is_dir():
        data.update(available=False, reason=f"{leader_dir} does not exist")
    elif not os.access(leader_dir, os.R_OK | os.X_OK):
        data.update(available=False, reason=f"{leader_dir} is not readable")

    separate = video_dir is not None and not _same_path(video_dir, leader_dir)
    video = {"path": (str(video_dir) if video_dir else None), "separate": separate,
             "mount": (str(video_mount) if video_mount else None), "exists": False,
             "available": True, "reason": None}
    if separate:
        video["exists"] = video_dir.is_dir()
        if video_mount is not None and not os.path.ismount(str(video_mount)):
            video.update(available=False, reason=f"video drive not mounted: {video_mount} is not a mountpoint")
        elif video_mount is not None and not _is_under(video_dir, video_mount):
            video.update(available=False, reason=f"{video_dir} is not under the video mount {video_mount}")
        elif video["exists"] and not os.access(video_dir, os.R_OK | os.X_OK):
            video.update(available=False, reason=f"{video_dir} is not readable")
    return {"data": data, "video": video}


def _skipped(name: str, is_dir: bool) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES) or (is_dir and name in SKIP_DIRS)


def _archive_files(folder) -> tuple[list, list, list]:
    """Everything under a date folder that the controller's rsync copies (-rl, no -D): regular files
    as (rel, size, mtime_ns) and symlinks as (rel, target), both sorted by rel, plus the errors met
    on the way (an unreadable directory, a file that vanished). Links are never followed."""
    files, links, errors = [], [], []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        try:
            with os.scandir(os.path.join(folder, rel_dir) if rel_dir else folder) as it:
                entries = list(it)
        except OSError as e:
            errors.append(f"{rel_dir or '.'}: {e.strerror or e}")
            continue
        for de in entries:
            rel = f"{rel_dir}/{de.name}" if rel_dir else de.name
            try:
                if de.is_symlink():
                    if not _skipped(de.name, False):
                        links.append((rel, os.readlink(de.path)))
                elif de.is_dir(follow_symlinks=False):
                    if not _skipped(de.name, True):
                        stack.append(rel)
                elif de.is_file(follow_symlinks=False) and not _skipped(de.name, False):
                    st = de.stat(follow_symlinks=False)
                    files.append((rel, st.st_size, st.st_mtime_ns))
            except OSError as e:
                errors.append(f"{rel}: {e.strerror or e}")
    files.sort()
    links.sort()
    return files, links, errors


def _fingerprint(files, links) -> str:
    """One hash over what _archive_files lists. Equal fingerprints = the same files with the same
    sizes and mtimes; it says nothing about content (that is what `hash` is for)."""
    h = hashlib.sha256()
    for rel, size, mtime_ns in files:
        h.update(f"F\0{rel}\0{size}\0{mtime_ns}\n".encode("utf-8", "surrogateescape"))
    for rel, target in links:
        h.update(f"L\0{rel}\0{target}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _tree_fp(path):
    """A date folder's fingerprint in one tree, or None when it could not be listed in full."""
    files, links, errors = _archive_files(path)
    return None if errors else _fingerprint(files, links)


def read_file(path, sink, direct: bool = True) -> tuple[int, bool]:
    """Feed a file's bytes to sink(chunk); returns (bytes read, whether the read was direct).

    direct=True reads with O_DIRECT, straight from the device and past the page cache — the cache is
    what returned wrong bytes on the Pi 5 kernel 6.18.34, and a hash made through it would certify
    exactly the bytes rsync got. Where a filesystem has no direct I/O (tmpfs on older kernels, some
    network mounts) the read falls back to a buffered one and the flag says so. The loop stops at
    the first short read; the caller compares the byte count with the size it expects, so a short
    read in the middle of a file is an error there and never a wrong hash. A chunk is only valid
    during the sink call."""
    flag = getattr(os, "O_DIRECT", 0) if direct else 0
    if flag:
        try:
            fd = os.open(path, os.O_RDONLY | flag)
        except OSError as e:
            if e.errno != errno.EINVAL:
                raise
            flag = 0
    if flag:
        buf = mmap.mmap(-1, READ_BUF)              # page-aligned, which O_DIRECT needs
        mv = memoryview(buf)
        total = 0
        try:
            with os.fdopen(fd, "rb", buffering=0) as f:
                while True:
                    try:
                        n = f.readinto(mv)
                    except OSError as e:
                        if e.errno == errno.EINVAL and total == 0:
                            flag = 0                # opened, but this filesystem refuses direct reads
                            break
                        raise
                    if not n:
                        break
                    total += n
                    sink(mv[:n])
                    if n < READ_BUF:
                        break
        finally:
            mv.release()
            try:
                buf.close()
            except BufferError:                     # a chunk is still referenced (a sink that raised)
                pass
        if flag:
            return total, True
    total = 0
    with open(path, "rb", buffering=0) as f:
        while True:
            chunk = f.read(READ_BUF)
            if not chunk:
                break
            total += len(chunk)
            sink(chunk)
    return total, False


def file_digest(path, direct: bool = True, tick=None) -> tuple[str, int, bool]:
    """(SHA-256 hex, bytes read, direct) of a file read through read_file; tick(n) per chunk. The
    controller hashes its own copies with this same function."""
    h = hashlib.new(HASH_ALGO)

    def sink(chunk):
        h.update(chunk)
        if tick is not None:
            tick(len(chunk))

    total, was_direct = read_file(path, sink, direct=direct)
    return h.hexdigest(), total, was_direct


def cmd_inventory(a):
    leader_dir, video_dir, video_mount = _dirs(a)
    roots = _roots(leader_dir, video_dir, video_mount)
    separate = roots["video"]["separate"]
    vroot_ok = roots["video"]["available"]
    data_f = {rel: (s, d, p) for rel, s, d, p in _folders(leader_dir)} if roots["data"]["available"] else {}
    video_f = ({rel: (s, d, p) for rel, s, d, p in _folders(video_dir)}
               if (separate and vroot_ok) else {})
    keys = sorted(set(data_f) | set(video_f))
    want = set(a.folder or [])
    if want:
        keys = [k for k in keys if k in want]

    folders = []
    for rel in keys:
        in_data, in_video = rel in data_f, rel in video_f
        subject, date = (data_f.get(rel) or video_f.get(rel))[:2]
        dd = data_f[rel][2] if in_data else None
        vd = video_f[rel][2] if in_video else None
        ids = set()
        if dd is not None:
            ids |= {p.name for p in dd.iterdir() if p.is_dir()}
        if vd is not None:
            ids |= {p.name for p in vd.iterdir() if p.is_dir()}
        sessions = []
        for sid in sorted(ids):
            sd = dd / sid if (dd is not None and (dd / sid).is_dir()) else None
            vsd = vd / sid if (vd is not None and (vd / sid).is_dir()) else None
            entry = {"id": sid, "in_data": sd is not None, "has_h5": False, "format_version": None,
                     "sidecars": [], "bytes": 0, "mtime": None,
                     "camera_saved": None, "camera_requested": None, "has_video": None,
                     "video_raw": None}
            if sd is not None:
                h5s = sorted(sd.glob("*.h5"))
                entry["has_h5"] = bool(h5s)
                entry["sidecars"] = [f.name for f in sd.iterdir()
                                     if f.is_file() and f.suffix in (".yaml", ".npz", ".npy", ".json")]
                entry["bytes"] = _du(sd)
                entry["mtime"] = sd.stat().st_mtime
                info = _h5_info(h5s[0]) if h5s else {}
                if h5s:
                    entry["format_version"] = info["format_version"]
                meta = _meta_camera(sd)
                for k in ("camera_saved", "camera_requested"):
                    entry[k] = info.get(k) if info.get(k) is not None else meta[k]
            if separate:
                if vsd is not None:
                    vfiles = sorted(f.name for f in vsd.iterdir() if f.is_file())
                    entry["video_files"] = vfiles
                    entry["bytes_video"] = _du(vsd)
                    entry["has_video"] = any(n in VIDEO_NAMES for n in vfiles)
                    entry["video_raw"] = any(n in RAW_VIDEO_NAMES for n in vfiles)
                elif vroot_ok:
                    entry["has_video"] = False          # tree trusted, no folder: no video
                    entry["video_raw"] = False
                # tree unavailable: has_video stays None (unknown)
            elif sd is not None:
                entry["has_video"] = any((sd / n).is_file() for n in VIDEO_NAMES)
                entry["video_raw"] = any((sd / n).is_file() for n in RAW_VIDEO_NAMES)
            sessions.append(entry)
        missing = [s["id"] for s in sessions if s["camera_saved"] is True and s["has_video"] is False]
        mt = (dd.stat().st_mtime if dd is not None else vd.stat().st_mtime)
        f = {"key": rel, "subject": subject, "date": date, "sessions": sessions,
             "in_data": in_data, "in_video": in_video,
             "bytes_data": _du(dd) if dd is not None else 0,
             "bytes_video": _du(vd) if vd is not None else 0,
             "mtime": mt,
             "n_unconsolidated": sum(1 for s in sessions if s["has_h5"] and s["format_version"] != 2),
             "n_raw_video": sum(1 for s in sessions if s.get("video_raw")),
             "missing_video": missing,
             # per tree: the fingerprint of what rsync copies (None: could not be listed in full)
             "fp": {t: _tree_fp(p) for t, p in (("data", dd), ("video", vd)) if p is not None}}
        if in_video and not in_data:
            f["video_only"] = True
        folders.append(f)

    _out({"ok": True, "hostname": socket.gethostname(), "t": time.time(),
          "engine_running": engine_running(),
          "leader_dir": str(leader_dir), "video_dir": (str(video_dir) if video_dir else None),
          # kept for controllers older than protocol 2: "copy a separate video tree"
          "video_tree": bool(separate and vroot_ok and roots["video"]["exists"]),
          "data_root": roots["data"], "video_root": roots["video"],
          "disk_data": _disk(leader_dir if leader_dir.exists() else Path.home()),
          "disk_video": (_disk(video_dir) if (separate and vroot_ok and roots["video"]["exists"]) else None),
          "folders": folders})


def cmd_consolidate(a):
    """Fold sidecars into the .h5 for every session that is not yet at format version 2, and
    remux every session's raw video.h264 into video.mp4 (adopting a complete leftover video.mp4.part
    where one exists — see shared/consolidate.py). Idempotent. Refused while the engine runs (a live
    .h5 is being written), and while the video tree cannot be trusted: consolidating then would write
    a v2 .h5 without /camera, and an already-consolidated file is never folded again. A video that
    cannot be remuxed is reported in `video_errors` and copied raw; it never fails the command."""
    if engine_running():
        _out({"ok": False, "error": "engine/leader.py is running"})
        return 2
    leader_dir, video_dir, video_mount = _dirs(a)
    roots = _roots(leader_dir, video_dir, video_mount)
    if not roots["data"]["available"]:
        _out({"ok": False, "error": f"data tree unavailable: {roots['data']['reason']}"})
        return 2
    if roots["video"]["separate"] and not roots["video"]["available"]:
        _out({"ok": False, "error": f"video tree unavailable ({roots['video']['reason']}) — not consolidating"})
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from shared.consolidate import consolidate_session
    want = set(a.folder or [])
    results = {}
    n_adopted = n_remuxed = 0
    video_errors = []
    for rel, _s, _d, d in _folders(leader_dir):
        if want and rel not in want:
            continue
        for sd in sorted(p for p in d.iterdir() if p.is_dir()):
            h5s = sorted(sd.glob("*.h5"))
            if not h5s:
                results[f"{rel}/{sd.name}"] = {"ok": True, "skipped": "no h5"}
                continue
            # without a separate video tree the video sits in the session dir (the engine's layout)
            vd = (video_dir or leader_dir) / rel / sd.name
            try:
                res = consolidate_session(sd, vd if vd.is_dir() else None)
                v = res.get("video") or {}
                results[f"{rel}/{sd.name}"] = {"ok": bool(res.get("ok", True)), "skipped": res.get("skipped"),
                                               "video": v}
                if v.get("adopted"):
                    n_adopted += 1
                elif v.get("remuxed"):
                    n_remuxed += 1
                if v.get("error"):
                    video_errors.append(f"{sd.name}: {v['error']}")
            except Exception as e:
                results[f"{rel}/{sd.name}"] = {"ok": False, "error": str(e)}
    _out({"ok": all(r.get("ok") for r in results.values()), "results": results,
          "video_adopted": n_adopted, "video_remuxed": n_remuxed, "video_errors": video_errors})
    return 0


def _safe_folder(root: Path, rel: str) -> Path | None:
    if not FOLDER_RE.match(rel):
        return None
    root = root.expanduser().resolve()
    p = (root / rel)
    try:
        rp = p.resolve()
    except Exception:
        return None
    if rp.parent.parent != root or p.is_symlink():
        return None
    return rp


def cmd_purge(a):
    """Delete verified tree copies of date folders. Only `<subject>/<subject>_<date>` names, only
    inside the configured roots, never while the engine runs, never while a root is unavailable,
    and only the trees the controller verified (see the module docstring)."""
    if engine_running():
        _out({"ok": False, "error": "engine/leader.py is running — refusing to delete anything"})
        return 2
    verified = {}
    for v in a.verified or []:
        tree, _, rel = v.partition(":")
        if tree not in TREES or not FOLDER_RE.match(rel):
            _out({"ok": False, "error": f"refusing: malformed --verified {v!r} (want data:S/S_YYYYMMDD or video:...)"})
            return 2
        verified.setdefault(rel, set()).add(tree)
    if not verified:
        _out({"ok": False, "error": "refusing: no --verified trees. The controller that sent this predates "
                                    "leader_data protocol 2 — update the controller"})
        return 2
    leader_dir, video_dir, video_mount = _dirs(a)
    roots = _roots(leader_dir, video_dir, video_mount)
    if not roots["data"]["available"]:
        _out({"ok": False, "error": f"refusing: data tree unavailable ({roots['data']['reason']})"})
        return 2
    separate = roots["video"]["separate"]
    if separate and not roots["video"]["available"]:
        _out({"ok": False, "error": f"refusing: video tree unavailable ({roots['video']['reason']})"})
        return 2
    tree_roots = {"data": leader_dir}
    if separate:
        tree_roots["video"] = video_dir

    results = {}
    for rel in a.folder or []:
        vt = verified.get(rel, set())
        errors, plan = [], []
        if not vt:
            errors.append("refused: no tree of this folder was verified")
        for t in sorted(vt - set(tree_roots)):
            errors.append(f"refused: verified the {t} tree, which is not configured on this rig")
        for tree, root in tree_roots.items():
            p = _safe_folder(root, rel)
            if p is None:
                errors.append(f"refused: {rel} is not a date folder inside {root}")
                continue
            present = p.is_dir()
            if present and tree not in vt:
                errors.append(f"refused: present in the {tree} tree but that copy was not verified")
            elif tree in vt and not present:
                errors.append(f"refused: the {tree} tree changed since verification (folder is gone)")
            elif present:
                plan.append((tree, p))
        if errors:
            results[rel] = {"ok": False, "bytes_freed": 0, "removed": [], "trees_removed": [], "errors": errors}
            continue
        freed, removed, trees_removed, errs = 0, [], [], []
        for tree, p in plan:
            try:
                freed += _du(p)
                shutil.rmtree(p)
                removed.append(str(p))
                trees_removed.append(tree)
                subj = p.parent
                if subj.is_dir() and not any(subj.iterdir()):
                    subj.rmdir()
            except Exception as e:
                errs.append(f"{p}: {e}")
        results[rel] = {"ok": not errs, "bytes_freed": freed, "removed": removed,
                        "trees_removed": trees_removed, "errors": errs}
    _out({"ok": bool(results) and all(r["ok"] for r in results.values()), "results": results})
    return 0


def _progress(done: int, total: int):
    """One `P <bytes done> <bytes total>` line on stderr: progress for the UI and a sign of life for
    the controller's stall timer. A write that fails means the controller hung up (cancel, a dropped
    link) — stop reading the disk for nobody."""
    try:
        sys.stderr.write(f"P {done} {total}\n")
        sys.stderr.flush()
    except OSError:
        os._exit(1)


def cmd_hash(a):
    """SHA-256 of every file the controller's rsync copies from the named date folders, each read
    with O_DIRECT (read_file). A stat-only pass first, so progress has a total; then the reads. Per
    folder and tree the reply carries the fingerprint (the inventory's `fp`), whether every read was
    direct, and `files` as [rel, size, mtime_ns, sha256]. A file that changes while it is read, or
    cannot be read, fails its folder (`ok: false`, `errors`), never the whole command. Refused while
    the engine runs: this reads the whole folder off the disk a session would be writing to."""
    if engine_running():
        _out({"ok": False, "error": "engine/leader.py is running"})
        return 2
    want = list(dict.fromkeys(a.folder or []))
    if not want:
        _out({"ok": False, "error": "hash needs at least one --folder"})
        return 2
    leader_dir, video_dir, video_mount = _dirs(a)
    roots = _roots(leader_dir, video_dir, video_mount)
    if not roots["data"]["available"]:
        _out({"ok": False, "error": f"data tree unavailable: {roots['data']['reason']}"})
        return 2
    separate = roots["video"]["separate"]
    if separate and not roots["video"]["available"]:
        _out({"ok": False, "error": f"video tree unavailable ({roots['video']['reason']})"})
        return 2
    tree_roots = {"data": leader_dir}
    if separate:
        tree_roots["video"] = video_dir

    plan, paths, total = {}, {}, 0
    for rel in want:
        entry = plan[rel] = {"ok": True, "errors": [], "trees": {}}
        for tree, root in tree_roots.items():
            p = _safe_folder(root, rel)
            if p is None:
                entry["errors"].append(f"{tree}: {rel} is not a date folder inside {root}")
                continue
            if not p.is_dir():
                continue
            files, links, errors = _archive_files(p)
            entry["errors"] += [f"{tree}: {e}" for e in errors]
            entry["trees"][tree] = {"fp": None if errors else _fingerprint(files, links), "direct": True,
                                    "bytes": 0, "files": [list(f) for f in files],
                                    "links": [list(ln) for ln in links]}
            paths[(rel, tree)] = p
            total += sum(f[1] for f in files)

    done, last = 0, time.monotonic()

    def tick(n):
        nonlocal done, last
        done += n
        now = time.monotonic()
        if now - last >= 1.0:
            last = now
            _progress(done, total)

    _progress(0, total)
    for rel, entry in plan.items():
        for tree, t in entry["trees"].items():
            for f in t["files"]:
                name, size, mtime_ns = f
                fpath = os.path.join(paths[(rel, tree)], name)
                try:
                    digest, n, was_direct = file_digest(fpath, tick=tick)
                    st = os.lstat(fpath)
                except OSError as e:
                    entry["errors"].append(f"{tree}: {name}: {e.strerror or e}")
                    continue
                if n != size or st.st_size != size or st.st_mtime_ns != mtime_ns:
                    entry["errors"].append(f"{tree}: {name}: changed while it was read")
                    continue
                f.append(digest)
                t["bytes"] += n
                t["direct"] = t["direct"] and was_direct
        entry["ok"] = not entry["errors"]
    _progress(done, total)
    _out({"ok": all(e["ok"] for e in plan.values()), "algo": HASH_ALGO, "folders": plan})
    return 0


def cmd_read(a):
    """Stream one file of a date folder to stdout, read with O_DIRECT (read_file) — how the
    controller fetches a file again when its copy does not match the hash. stdout carries the raw
    bytes and nothing else, so every refusal goes to stderr with exit code 3, never as JSON. Only
    a regular file reached without any symlink, inside a date folder of the named tree."""
    def refuse(msg):
        sys.stderr.write(f"read: {msg}\n")
        return 3

    if engine_running():
        return refuse("engine/leader.py is running")
    if len(a.folder or []) != 1:
        return refuse("needs exactly one --folder")
    folder = a.folder[0]
    leader_dir, video_dir, video_mount = _dirs(a)
    roots = _roots(leader_dir, video_dir, video_mount)
    if a.tree == "data":
        if not roots["data"]["available"]:
            return refuse(f"data tree unavailable: {roots['data']['reason']}")
        root = leader_dir
    else:
        if not roots["video"]["separate"]:
            return refuse("this rig has no separate video tree")
        if not roots["video"]["available"]:
            return refuse(f"video tree unavailable ({roots['video']['reason']})")
        root = video_dir
    base = _safe_folder(root, folder)
    if base is None or not base.is_dir():
        return refuse(f"{folder} is not a date folder inside {root}")
    rel = a.file
    if not rel or rel.startswith("/") or any(c in ("", ".", "..") for c in rel.split("/")):
        return refuse(f"bad --file {rel!r}")
    p = os.path.join(base, rel)
    try:
        st = os.lstat(p)
    except OSError as e:
        return refuse(f"{rel}: {e.strerror or e}")
    if not stat.S_ISREG(st.st_mode) or os.path.realpath(p) != p:
        return refuse(f"{rel} is not a regular file inside {folder}")
    out = sys.stdout.buffer
    try:
        read_file(p, out.write)
        out.flush()
    except BrokenPipeError:
        os._exit(1)                                 # the controller hung up
    except OSError as e:
        return refuse(f"{rel}: {e.strerror or e}")
    return 0


def cmd_prune_logs(a):
    """Delete shepherd logs older than --days, always keeping the newest two files."""
    d = Path(a.log_dir).expanduser()
    if not d.is_dir():
        _out({"ok": True, "removed": [], "note": "no log dir"})
        return 0
    files = sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = time.time() - a.days * 86400
    removed = []
    for p in files[2:]:
        if p.stat().st_mtime < cutoff:
            try:
                p.unlink()
                removed.append(p.name)
            except OSError:
                pass
    _out({"ok": True, "removed": removed})
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("inventory", "consolidate", "purge", "hash", "read"):
        sp = sub.add_parser(name)
        sp.add_argument("--leader-dir", required=True)
        sp.add_argument("--video-dir", default=None)
        sp.add_argument("--video-mount", default=None,
                        help="mountpoint that must be mounted for the video tree to be trusted")
        sp.add_argument("--folder", action="append", help="<subject>/<subject>_<YYYYMMDD>, repeatable")
        if name == "consolidate":
            sp.add_argument("--force", action="store_true")
        if name == "purge":
            sp.add_argument("--verified", action="append",
                            help="TREE:<subject>/<subject>_<YYYYMMDD> (TREE = data|video), repeatable")
        if name == "read":
            sp.add_argument("--tree", choices=TREES, required=True)
            sp.add_argument("--file", required=True, help="path of one file inside the date folder")
    sp = sub.add_parser("prune-logs")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--log-dir", default="~/shepherd_logs")
    a = ap.parse_args(argv)
    try:
        rc = {"inventory": cmd_inventory, "consolidate": cmd_consolidate, "purge": cmd_purge,
              "hash": cmd_hash, "read": cmd_read, "prune-logs": cmd_prune_logs}[a.cmd](a)
    except Exception as e:
        if a.cmd == "read":                         # its stdout is the file: never JSON there
            sys.stderr.write(f"read: {type(e).__name__}: {e}\n")
            return 3
        _out({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
