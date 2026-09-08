"""
controller/settings.py

Controller-wide settings that are NOT per rig: the UI port, the controller's own IP, the
event port every rig must agree on, where synced data lands, auto-purge, the rsync binary,
sync tuning and the rig groups ("super rigs"). They live in controller.yaml at the repo
root, which is gitignored (machine-specific, like the data root); controller.example.yaml
is the tracked template it is created from on first run.
"""
from __future__ import annotations
import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "controller.yaml"
EXAMPLE_PATH = ROOT / "controller.example.yaml"

DEFAULTS = {
    "ui_port": 5000,
    "controller_ip": "192.168.10.1",
    "event_port": 5571,
    "data_root": None,          # None -> $VRFARM_DATA_DIR, else ~/VRFarm/data
    "auto_purge": False,
    "rsync_path": None,         # None -> <conda env>/bin/rsync (conda-forge rsync >= 3.1)
    "sync": {
        "parallel_rigs": 4,
        "bwlimit_mbps": 0,
        "verify_checksum_before_purge": False,
        "shepherd_logs_keep_days": 7,
    },
    "groups": {},
}

_lock = threading.RLock()
_cache: dict | None = None
_rigs_dir_override: Path | None = None


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force: bool = False) -> dict:
    """Settings merged over DEFAULTS. Creates controller.yaml from the example on first run."""
    global _cache
    with _lock:
        if _cache is None or force:
            data = {}
            if not SETTINGS_PATH.exists() and EXAMPLE_PATH.exists():
                try:
                    shutil.copy(EXAMPLE_PATH, SETTINGS_PATH)
                except OSError:
                    pass
            if SETTINGS_PATH.exists():
                try:
                    data = yaml.safe_load(SETTINGS_PATH.read_text()) or {}
                except Exception as e:
                    print(f"[settings] could not parse {SETTINGS_PATH}: {e}", flush=True)
            _cache = _merge(copy.deepcopy(DEFAULTS), data)
        return copy.deepcopy(_cache)


def save(new: dict) -> dict:
    """Write the whole settings dict atomically (temp file + rename) and refresh the cache."""
    global _cache
    with _lock:
        merged = _merge(copy.deepcopy(DEFAULTS), new)
        fd, tmp = tempfile.mkstemp(prefix=".controller.", suffix=".yaml", dir=str(ROOT))
        with os.fdopen(fd, "w") as f:
            yaml.dump(merged, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, SETTINGS_PATH)
        _cache = merged
        return copy.deepcopy(_cache)


def update(**changes) -> dict:
    """Replace the given top-level keys and save. Top-level keys are REPLACED, not deep-merged:
    a deep merge could never delete a group or a sync option."""
    with _lock:
        cur = load()
        cur.update(changes)
        return save(cur)


# ── Paths ──

def set_rigs_dir(path) -> None:
    """Point the app at another rigs/ folder (tests use a scratch one)."""
    global _rigs_dir_override
    _rigs_dir_override = Path(path).expanduser().resolve() if path else None


def rigs_dir() -> Path:
    return _rigs_dir_override or (ROOT / "rigs")


def data_root() -> Path:
    """Where synced session data and the subject index live on this controller:
    controller.yaml data_root, else $VRFARM_DATA_DIR, else ~/VRFarm/data."""
    v = (load().get("data_root") or "")
    if isinstance(v, str) and v.strip():
        return Path(v.strip()).expanduser()
    env = os.environ.get("VRFARM_DATA_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / "VRFarm" / "data"


def groups() -> dict:
    g = load().get("groups") or {}
    return {str(k): [str(r) for r in (v or [])] for k, v in g.items()}


# ── rsync ──

def rsync_path() -> Path:
    v = load().get("rsync_path")
    if v:
        return Path(str(v)).expanduser()
    return Path(sys.prefix) / "bin" / "rsync"


def rsync_info() -> dict:
    """{path, version, ok, error}. ok means a real rsync >= 3.1 (Apple's openrsync is not:
    protocol 29, no --info=progress2)."""
    p = rsync_path()
    info = {"path": str(p), "version": None, "ok": False, "error": None}
    if not p.exists():
        info["error"] = f"{p} not found — run: conda install -n vrfarm -c conda-forge rsync"
        return info
    try:
        out = subprocess.run([str(p), "--version"], capture_output=True, text=True, timeout=10).stdout
    except Exception as e:
        info["error"] = f"could not run {p}: {e}"
        return info
    if "openrsync" in out:
        info["error"] = "this is Apple's openrsync (no --info=progress2); install rsync from conda-forge"
        info["version"] = "openrsync"
        return info
    m = re.search(r"rsync\s+version\s+(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not m:
        info["error"] = f"unrecognized rsync --version output: {out.splitlines()[0] if out else ''}"
        return info
    ver = tuple(int(x or 0) for x in m.groups())
    info["version"] = ".".join(str(x) for x in ver)
    info["ok"] = ver >= (3, 1, 0)
    if not info["ok"]:
        info["error"] = f"rsync {info['version']} is too old (need >= 3.1)"
    return info
