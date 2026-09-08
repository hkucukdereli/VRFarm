#!/usr/bin/env python3
"""
shared/leader_data.py — the leader Pi's side of the controller's Data tab.

The controller talks to leaders over SSH only (no pi_api) and runs this script for anything
that needs to look at or change files on the Pi. rsync does the copying itself. Runs under
the rig conda env (h5py for the format check):

    cd ~/rig && python -m shared.leader_data inventory  --leader-dir ~/data --video-dir /media/vruser/ssd/video
    cd ~/rig && python -m shared.leader_data consolidate --leader-dir ... --video-dir ... [--folder S/S_YYYYMMDD ...]
    cd ~/rig && python -m shared.leader_data purge       --leader-dir ... --video-dir ... --folder S/S_YYYYMMDD ...
    cd ~/rig && python -m shared.leader_data prune-logs  --days 7

Every command prints ONE JSON object on stdout. Folder names are always `<subject>/<subject>_<date>`
("date folders"); purge accepts nothing else, resolves them strictly inside the two data
trees, and refuses to run while the experiment engine is alive. Deployed to leaders by
shared/deploy_manifest.py.
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

FOLDER_RE = re.compile(r"^([A-Za-z0-9-]+)/\1_(\d{8})$")
ENGINE_MARK = "engine/leader.py"


def _out(obj):
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


def _format_version(h5: Path):
    try:
        import h5py
        with h5py.File(str(h5), "r") as f:
            v = f.attrs.get("format_version", 0)
            if v == 0 and "trials" in f:
                v = 2
            return int(v)
    except Exception as e:
        return f"error: {e}"


def _folders(leader_dir: Path):
    """All <subject>/<subject>_<date> folders under the data tree."""
    out = []
    if not leader_dir.is_dir():
        return out
    for subj in sorted(p for p in leader_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for d in sorted(p for p in subj.iterdir() if p.is_dir()):
            rel = f"{subj.name}/{d.name}"
            m = FOLDER_RE.match(rel)
            if m:
                out.append((rel, m.group(1), m.group(2), d))
    return out


def cmd_inventory(a):
    leader_dir = Path(a.leader_dir).expanduser()
    video_dir = Path(a.video_dir).expanduser() if a.video_dir else None
    same_tree = video_dir is None or video_dir.resolve() == leader_dir.resolve() if leader_dir.exists() else video_dir is None
    folders = []
    for rel, subject, date, d in _folders(leader_dir):
        sessions = []
        for sd in sorted(p for p in d.iterdir() if p.is_dir()):
            h5s = sorted(sd.glob("*.h5"))
            sidecars = [f.name for f in sd.iterdir() if f.is_file() and f.suffix in (".yaml", ".npz", ".npy", ".json")]
            entry = {"id": sd.name, "has_h5": bool(h5s), "format_version": None, "sidecars": sidecars,
                     "bytes": _du(sd), "mtime": sd.stat().st_mtime}
            if h5s:
                entry["format_version"] = _format_version(h5s[0])
            vd = (video_dir / rel / sd.name) if (video_dir and not same_tree) else None
            if vd and vd.is_dir():
                entry["video_files"] = sorted(f.name for f in vd.iterdir() if f.is_file())
                entry["bytes_video"] = _du(vd)
            sessions.append(entry)
        bytes_video = 0
        if video_dir and not same_tree and (video_dir / rel).is_dir():
            bytes_video = _du(video_dir / rel)
        folders.append({"key": rel, "subject": subject, "date": date, "sessions": sessions,
                        "bytes_data": _du(d), "bytes_video": bytes_video, "mtime": d.stat().st_mtime,
                        "n_unconsolidated": sum(1 for s in sessions if s["has_h5"] and s["format_version"] != 2)})
    # video-only date folders (a session whose data folder was already purged by hand)
    if video_dir and not same_tree and video_dir.is_dir():
        known = {f["key"] for f in folders}
        for rel, subject, date, d in _folders(video_dir):
            if rel not in known:
                folders.append({"key": rel, "subject": subject, "date": date, "sessions": [],
                                "bytes_data": 0, "bytes_video": _du(d), "mtime": d.stat().st_mtime,
                                "n_unconsolidated": 0, "video_only": True})
    _out({"ok": True, "hostname": socket.gethostname(), "t": time.time(),
          "engine_running": engine_running(),
          "leader_dir": str(leader_dir), "video_dir": (str(video_dir) if video_dir else None),
          "video_tree": bool(video_dir and not same_tree and video_dir.is_dir()),
          "disk_data": _disk(leader_dir if leader_dir.exists() else Path.home()),
          "disk_video": (_disk(video_dir) if (video_dir and video_dir.exists() and not same_tree) else None),
          "folders": folders})


def cmd_consolidate(a):
    """Fold sidecars into the .h5 (and remux video) for every session that is not yet at
    format version 2. Idempotent. Refused while the engine runs (a live .h5 is being written)."""
    if engine_running():
        _out({"ok": False, "error": "engine/leader.py is running"})
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from shared.consolidate import consolidate_session
    leader_dir = Path(a.leader_dir).expanduser()
    video_dir = Path(a.video_dir).expanduser() if a.video_dir else None
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
            if _format_version(h5s[0]) == 2 and not a.force:
                # still let consolidate finish a stranded video remux
                pass
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
    """Delete date folders in BOTH trees. Only `<subject>/<subject>_<date>` names, only inside
    the two data roots, never while the engine runs."""
    if engine_running():
        _out({"ok": False, "error": "engine/leader.py is running — refusing to delete anything"})
        return 2
    leader_dir = Path(a.leader_dir).expanduser()
    video_dir = Path(a.video_dir).expanduser() if a.video_dir else None
    roots = [leader_dir] + ([video_dir] if (video_dir and video_dir.resolve() != leader_dir.resolve()) else [])
    results = {}
    for rel in a.folder or []:
        freed, removed, errors = 0, [], []
        for root in roots:
            p = _safe_folder(root, rel)
            if p is None:
                errors.append(f"refused: {rel} is not a date folder inside {root}")
                continue
            if not p.is_dir():
                continue
            try:
                freed += _du(p)
                shutil.rmtree(p)
                removed.append(str(p))
                subj = p.parent
                if subj.is_dir() and not any(subj.iterdir()):
                    subj.rmdir()
            except Exception as e:
                errors.append(f"{p}: {e}")
        results[rel] = {"ok": not errors, "bytes_freed": freed, "removed": removed, "errors": errors}
    _out({"ok": all(r["ok"] for r in results.values()), "results": results})
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
        if name != "inventory":
            sp.add_argument("--folder", action="append", help="<subject>/<subject>_<YYYYMMDD>, repeatable")
        if name == "consolidate":
            sp.add_argument("--force", action="store_true")
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
