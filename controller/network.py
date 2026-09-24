"""
controller/network.py

The Network tab's backend: the list of rigs and their Pis, creating / editing / renaming /
deleting rig YAMLs, reachability checks, the topology view, and rig groups ("super rigs")
stored in controller.yaml. Pi identity (name, IP, role, user) is edited HERE, not in Setup.
"""
from __future__ import annotations
import ipaddress
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request

from shared.config import load_rig, save_rig_atomic, get_leader_pi
from controller import settings, events
from controller.registry import registry
from controller.ssh import ssh_ok, target as ssh_target

bp = Blueprint("network", __name__, url_prefix="/api/network")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_check_cache: dict[str, dict] = {}       # rig -> {pi_name: {ip, ssh, api, t}} for unloaded rigs


def _rig_file(name: str) -> Path:
    return settings.rigs_dir() / f"{name}.yaml"


def _template() -> dict:
    tpl = settings.rigs_dir() / "_template.yaml"
    if tpl.exists():
        return load_rig(tpl)
    return {"pis": [], "devices": {}, "data": {"leader_dir": "/home/vruser/data",
                                                "video_dir": "/media/vruser/ssd/video"},
            "network": {"api_port": 5080, "event_port": 5571, "command_port": 5572, "ack_port": 5573,
                        "display_port": 5575, "displayd_pd_port": 5582, "camera_port": 5001},
            "slack": {"enabled": False, "webhook_url": ""}, "shepherd": {"enabled": True}}


def _all_ips(exclude: str | None = None) -> dict[str, str]:
    """ip -> rig name across every rig file (for uniqueness checks and IP suggestions)."""
    out = {}
    for name in registry.list_rig_files():
        if name == exclude:
            continue
        try:
            cfg = load_rig(_rig_file(name))
        except Exception:
            continue
        for pi in cfg.get("pis") or []:
            if pi.get("ip"):
                out[pi["ip"]] = name
    return out


def _validate_pis(pis, rig_name: str):
    if not isinstance(pis, list) or not pis:
        raise ValueError("a rig needs at least one Pi")
    seen_ips, seen_names, leaders = set(), set(), 0
    others = _all_ips(exclude=rig_name)
    clean = []
    for pi in pis:
        if not isinstance(pi, dict):
            raise ValueError("bad Pi entry")
        name = str(pi.get("name") or "").strip()
        ip = str(pi.get("ip") or "").strip()
        role = str(pi.get("role") or "").strip().lower()
        user = str(pi.get("user") or "vruser").strip()
        devices = [str(d) for d in (pi.get("devices") or [])]
        if not name:
            raise ValueError("every Pi needs a name")
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError(f"'{ip}' is not a valid IPv4 address ({name})")
        if role not in ("leader", "follower"):
            raise ValueError(f"{name}: role must be leader or follower")
        if name.lower() in seen_names:
            raise ValueError(f"two Pis named {name}")
        if ip in seen_ips:
            raise ValueError(f"IP {ip} used twice in this rig")
        if ip in others and not ipaddress.ip_address(ip).is_loopback:
            raise ValueError(f"IP {ip} already belongs to rig '{others[ip]}'")
        seen_names.add(name.lower())
        seen_ips.add(ip)
        leaders += role == "leader"
        clean.append({"name": name, "ip": ip, "role": role, "user": user, "devices": devices})
    if leaders != 1:
        raise ValueError("a rig needs exactly one leader")
    return clean


def _rig_row(name: str) -> dict:
    cfg = load_rig(_rig_file(name))
    rs = registry.rigs.get(name)
    checks = rs.pi_status if rs else _check_cache.get(name, {})
    return {
        "name": name,
        "loaded": rs is not None,
        "phase": rs.phase if rs else None,
        "busy": rs.busy_kind if rs else None,
        "pis": [{**pi, "check": checks.get(pi.get("name"))} for pi in (cfg.get("pis") or [])],
        "network": cfg.get("network") or {},
        "data": cfg.get("data") or {},
        "devices": sorted((cfg.get("devices") or {}).keys()),
        "slack": bool((cfg.get("slack") or {}).get("enabled")),
    }


# ── rigs ──

@bp.route("/rigs")
def api_rigs():
    rows = []
    for name in registry.list_rig_files():
        try:
            rows.append(_rig_row(name))
        except Exception as e:
            rows.append({"name": name, "error": str(e), "pis": []})
    return jsonify({"rigs": rows, "groups": settings.groups()})


@bp.route("/rigs", methods=["POST"])
def api_create_rig():
    data = request.json or {}
    name = str(data.get("name") or "").strip().lower()
    if not _NAME_RE.match(name) or name.startswith("_"):
        return jsonify({"ok": False, "error": "rig name: lowercase letters, digits, - or _ (max 32)"}), 400
    path = _rig_file(name)
    if path.exists():
        return jsonify({"ok": False, "error": f"rig '{name}' already exists"}), 409
    try:
        pis = _validate_pis(data.get("pis"), name)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    cfg = _template()
    cfg["name"] = name
    cfg["pis"] = pis
    save_rig_atomic(cfg, path)
    return jsonify({"ok": True, "rig": _rig_row(name)}), 201


@bp.route("/rigs/<name>", methods=["PATCH"])
def api_edit_rig(name):
    path = _rig_file(name)
    if not path.exists():
        return jsonify({"ok": False, "error": "no such rig"}), 404
    rs = registry.rigs.get(name)
    if rs and rs.phase == "running":
        return jsonify({"ok": False, "error": f"rig '{name}' is running a session"}), 409
    data = request.json or {}
    cfg = load_rig(path)
    if "pis" in data:
        try:
            cfg["pis"] = _validate_pis(data["pis"], name)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    for key in ("data", "network"):
        if isinstance(data.get(key), dict):
            cfg.setdefault(key, {}).update(data[key])
    save_rig_atomic(cfg, path)
    if rs:
        registry.reload_config(name)
    return jsonify({"ok": True, "rig": _rig_row(name)})


@bp.route("/rigs/<name>/rename", methods=["POST"])
def api_rename_rig(name):
    new = str((request.json or {}).get("new") or "").strip().lower()
    if not _NAME_RE.match(new) or new.startswith("_"):
        return jsonify({"ok": False, "error": "bad new name"}), 400
    if name in registry.rigs:
        return jsonify({"ok": False, "error": "unload the rig from Setup/Experiment first"}), 409
    src, dst = _rig_file(name), _rig_file(new)
    if not src.exists():
        return jsonify({"ok": False, "error": "no such rig"}), 404
    if dst.exists():
        return jsonify({"ok": False, "error": f"rig '{new}' already exists"}), 409
    cfg = load_rig(src)
    cfg["name"] = new
    save_rig_atomic(cfg, dst)
    src.unlink()
    groups = settings.groups()
    for gname, members in groups.items():
        groups[gname] = [new if m == name else m for m in members]
    settings.update(groups=groups)
    # per-rig calibration folder follows the rig
    old_cal = settings.ROOT / "display_calibration" / name
    if old_cal.is_dir():
        old_cal.rename(settings.ROOT / "display_calibration" / new)
    return jsonify({"ok": True, "rig": _rig_row(new)})


@bp.route("/rigs/<name>", methods=["DELETE"])
def api_delete_rig(name):
    """Soft delete: the YAML moves to rigs/.trash/<name>.<stamp>.yaml."""
    if name in registry.rigs:
        return jsonify({"ok": False, "error": "unload the rig from Setup/Experiment first"}), 409
    path = _rig_file(name)
    if not path.exists():
        return jsonify({"ok": False, "error": "no such rig"}), 404
    trash = settings.rigs_dir() / ".trash"
    trash.mkdir(exist_ok=True)
    dst = trash / f"{name}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
    shutil.move(str(path), str(dst))
    groups = settings.groups()
    changed = False
    for gname, members in list(groups.items()):
        if name in members:
            groups[gname] = [m for m in members if m != name]
            changed = True
    if changed:
        settings.update(groups=groups)
    _check_cache.pop(name, None)
    return jsonify({"ok": True, "trashed": str(dst.relative_to(settings.ROOT))})


@bp.route("/suggest_ips")
def api_suggest_ips():
    """The next free leader/follower IP pair on the rig subnet: .101/.102, .103/.104, ..."""
    used = set(_all_ips().keys())
    cfg = settings.load()
    base = ".".join(str(cfg.get("controller_ip", "192.168.10.1")).split(".")[:3])
    for host in range(101, 250, 2):
        a, b = f"{base}.{host}", f"{base}.{host + 1}"
        if a not in used and b not in used:
            return jsonify({"leader": a, "follower": b})
    return jsonify({"leader": "", "follower": ""})


# ── reachability ──

def _check_one(pi: dict, api_port: int) -> dict:
    out = {"ip": pi.get("ip"), "ssh": False, "api": False, "status": None, "t": time.time()}
    out["ssh"] = ssh_ok(ssh_target(pi))
    try:
        r = requests.get(f"http://{pi['ip']}:{api_port}/api/status", timeout=3)
        out["api"] = r.status_code == 200
        out["status"] = r.json()
    except Exception:
        pass
    out["ok"] = out["api"]
    return out


@bp.route("/check", methods=["POST"])
def api_check():
    """SSH + API check of every Pi of one rig (or all rigs), in parallel."""
    data = request.json or {}
    names = [data["rig"]] if data.get("rig") else registry.list_rig_files()
    results = {}
    threads = []
    lock = threading.Lock()

    def run(rig_name, pi, api_port):
        res = _check_one(pi, api_port)
        with lock:
            results.setdefault(rig_name, {})[pi["name"]] = res

    for name in names:
        try:
            cfg = load_rig(_rig_file(name))
        except Exception:
            continue
        api_port = int((cfg.get("network") or {}).get("api_port", 5080))
        for pi in cfg.get("pis") or []:
            t = threading.Thread(target=run, args=(name, pi, api_port), daemon=True)
            t.start()
            threads.append(t)
    for t in threads:
        t.join(timeout=15)
    for name, checks in results.items():
        rs = registry.rigs.get(name)
        if rs:
            rs.pi_status.update(checks)
        else:
            _check_cache[name] = checks
    return jsonify({"ok": True, "results": results})


@bp.route("/topology")
def api_topology():
    cfg = settings.load()
    rigs = []
    for name in registry.list_rig_files():
        try:
            rigs.append(_rig_row(name))
        except Exception as e:
            rigs.append({"name": name, "error": str(e), "pis": []})
    d = events.demux
    return jsonify({"controller_ip": cfg.get("controller_ip"), "event_port": cfg.get("event_port"),
                    "ui_port": cfg.get("ui_port"), "rigs": rigs,
                    "unknown_senders": registry.unknown_senders,
                    "udp": d.stats if d else None, "groups": settings.groups()})


# ── groups ──

@bp.route("/groups")
def api_groups():
    return jsonify(settings.groups())


@bp.route("/groups/<gname>", methods=["PUT"])
def api_put_group(gname):
    gname = gname.strip().lower()
    if not _NAME_RE.match(gname):
        return jsonify({"ok": False, "error": "group name: lowercase letters, digits, - or _"}), 400
    members = [str(m) for m in ((request.json or {}).get("rigs") or [])]
    known = set(registry.list_rig_files())
    bad = [m for m in members if m not in known]
    if bad:
        return jsonify({"ok": False, "error": f"unknown rigs: {', '.join(bad)}"}), 400
    if gname in known:
        return jsonify({"ok": False, "error": "a rig already has that name"}), 400
    groups = settings.groups()
    groups[gname] = sorted(set(members))
    settings.update(groups=groups)
    return jsonify({"ok": True, "groups": groups})


@bp.route("/groups/<gname>", methods=["DELETE"])
def api_delete_group(gname):
    groups = settings.groups()
    groups.pop(gname, None)
    settings.update(groups=groups)
    return jsonify({"ok": True, "groups": groups})
