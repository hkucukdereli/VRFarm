#!/usr/bin/env python3
"""
shared/leader_data.py — the leader Pi's side of the controller's Data tab.

The controller talks to leaders over SSH only (no pi_api) and runs this script for anything
that needs to look at or change files on the Pi. rsync does the copying itself. Runs under
the rig conda env (h5py for the format check):

    cd ~/rig && python -m shared.leader_data inventory   --leader-dir ~/data --video-dir /media/vruser/ssd/video [--video-mount /media/vruser/ssd] [--folder S/S_YYYYMMDD ...]
    cd ~/rig && python -m shared.leader_data consolidate --leader-dir ... --video-dir ... [--video-mount ...] [--folder S/S_YYYYMMDD ...]
    cd ~/rig && python -m shared.leader_data purge       --leader-dir ... --video-dir ... [--video-mount ...] --folder S/S_YYYYMMDD ... --verified data:S/S_YYYYMMDD ...
    cd ~/rig && python -m shared.leader_data prune-logs  --days 7

Every command prints ONE JSON object on stdout, always carrying `proto` (PROTO below). Folder
names are always `<subject>/<subject>_<date>` ("date folders").

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
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path

PROTO = 2
FOLDER_RE = re.compile(r"^([A-Za-z0-9-]+)/\1_(\d{8})$")
ENGINE_MARK = "engine/leader.py"
VIDEO_NAMES = ("video.mp4", "video.h264")
TREES = ("data", "video")


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
                     "camera_saved": None, "camera_requested": None, "has_video": None}
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
                elif vroot_ok:
                    entry["has_video"] = False          # tree trusted, no folder: no video
                # tree unavailable: has_video stays None (unknown)
            elif sd is not None:
                entry["has_video"] = any((sd / n).is_file() for n in VIDEO_NAMES)
            sessions.append(entry)
        missing = [s["id"] for s in sessions if s["camera_saved"] is True and s["has_video"] is False]
        mt = (dd.stat().st_mtime if dd is not None else vd.stat().st_mtime)
        f = {"key": rel, "subject": subject, "date": date, "sessions": sessions,
             "in_data": in_data, "in_video": in_video,
             "bytes_data": _du(dd) if dd is not None else 0,
             "bytes_video": _du(vd) if vd is not None else 0,
             "mtime": mt,
             "n_unconsolidated": sum(1 for s in sessions if s["has_h5"] and s["format_version"] != 2),
             "missing_video": missing}
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
    """Fold sidecars into the .h5 (and remux video) for every session that is not yet at
    format version 2. Idempotent. Refused while the engine runs (a live .h5 is being written),
    and while the video tree cannot be trusted: consolidating then would write a v2 .h5 without
    /camera, and an already-consolidated file is never folded again."""
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
    for rel, _s, _d, d in _folders(leader_dir):
        if want and rel not in want:
            continue
        for sd in sorted(p for p in d.iterdir() if p.is_dir()):
            h5s = sorted(sd.glob("*.h5"))
            if not h5s:
                results[f"{rel}/{sd.name}"] = {"ok": True, "skipped": "no h5"}
                continue
            vd = (video_dir / rel / sd.name) if video_dir else None
            try:
                res = consolidate_session(sd, vd if (vd and vd.is_dir()) else None)
                results[f"{rel}/{sd.name}"] = {"ok": bool(res.get("ok", True)), "skipped": res.get("skipped"),
                                               "video": res.get("video")}
            except Exception as e:
                results[f"{rel}/{sd.name}"] = {"ok": False, "error": str(e)}
    _out({"ok": all(r.get("ok") for r in results.values()), "results": results})
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
                                    f"leader_data protocol {PROTO} — update the controller"})
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
    for name in ("inventory", "consolidate", "purge"):
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
    sp = sub.add_parser("prune-logs")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--log-dir", default="~/shepherd_logs")
    a = ap.parse_args(argv)
    try:
        rc = {"inventory": cmd_inventory, "consolidate": cmd_consolidate,
              "purge": cmd_purge, "prune-logs": cmd_prune_logs}[a.cmd](a)
    except Exception as e:
        _out({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
