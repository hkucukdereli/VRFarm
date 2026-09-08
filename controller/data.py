"""
controller/data.py

The Data tab's routes: settings (data root, auto-purge), loading rigs or a group into cards,
the inventory with green/red folder status, and the three actions as background jobs —
Sync Now, Sync & Poweroff (preview first), Purge Data (preview first).
"""
from __future__ import annotations
import subprocess
import threading
from flask import Blueprint, jsonify, request

from controller import settings, sync
from controller.registry import registry
from controller.jobs import runner, Job
from controller.ssh import ssh_ok, target as ssh_target

bp = Blueprint("data", __name__, url_prefix="/api/data")


def _settings_view() -> dict:
    s = settings.load(force=True)      # pick up hand edits to controller.yaml on every refresh
    return {"data_root": str(settings.data_root()), "data_root_set": bool(s.get("data_root")),
            "auto_purge": bool(s.get("auto_purge")), "rsync": settings.rsync_info(),
            "sync": s.get("sync") or {}, "groups": settings.groups(),
            "rigs": registry.list_rig_files(), "loaded": sorted(registry.rigs.keys()),
            "ledger": str(sync.ledger_path())}


@bp.route("/settings")
def api_settings():
    return jsonify(_settings_view())


@bp.route("/settings", methods=["PUT"])
def api_put_settings():
    data = request.json or {}
    changes = {}
    if "data_root" in data:
        v = str(data["data_root"] or "").strip()
        changes["data_root"] = v or None
    if "auto_purge" in data:
        changes["auto_purge"] = bool(data["auto_purge"])
    if isinstance(data.get("sync"), dict):
        cur = settings.load().get("sync") or {}
        cur.update({k: data["sync"][k] for k in ("parallel_rigs", "bwlimit_mbps",
                                                  "verify_checksum_before_purge", "shepherd_logs_keep_days")
                    if k in data["sync"]})
        changes["sync"] = cur
    if changes:
        settings.update(**changes)
    return jsonify({"ok": True, **_settings_view()})


@bp.route("/browse_folder", methods=["POST"])
def browse_folder():
    """Native macOS folder picker (this app runs on the controller Mac)."""
    try:
        script = 'POSIX path of (choose folder with prompt "Select the data root for all rigs")'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return jsonify({"ok": False, "error": "cancelled"})
        return jsonify({"ok": True, "path": r.stdout.strip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/load", methods=["POST"])
def api_load():
    """Load a rig or a group into the registry (config only, no Pi contact)."""
    data = request.json or {}
    names = []
    if data.get("group"):
        names = settings.groups().get(data["group"])
        if names is None:
            return jsonify({"ok": False, "error": f"no group '{data['group']}'"}), 404
    elif data.get("rig"):
        names = [data["rig"]]
    results = {}
    for n in names:
        try:
            registry.load(n)
            results[n] = {"ok": True}
        except Exception as e:
            results[n] = {"ok": False, "error": str(e)}
    return jsonify({"ok": all(r["ok"] for r in results.values()), "rigs": names, "results": results})


def _rigs_from(data) -> list:
    return [n for n in (data.get("rigs") or []) if n in registry.rigs]


@bp.route("/inventory", methods=["POST"])
def api_inventory():
    """Fresh inventory + green/red status for the given rigs, in parallel (25 s cap each)."""
    names = _rigs_from(request.json or {})
    cards = {}
    lock = threading.Lock()

    def run(n):
        rs = registry.rigs[n]
        try:
            c = sync.inventory(rs)
        except Exception as e:
            c = {"name": n, "error": str(e), "folders": [], "ssh_ok": False}
        with lock:
            cards[n] = c

    ts = [threading.Thread(target=run, args=(n,), daemon=True) for n in names]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=40)
    for n in names:
        cards.setdefault(n, {"name": n, "error": "inventory timed out", "folders": [], "ssh_ok": False})
    return jsonify({"ok": True, "rigs": cards, "auto_purge": bool(settings.load().get("auto_purge"))})


def _busy_job():
    j = runner.current()
    return j.to_dict(log_tail=0) if j else None


@bp.route("/sync", methods=["POST"])
def api_sync():
    """Sync Now. {items: {rig: [folder,...]}}"""
    data = request.json or {}
    items = {r: list(f) for r, f in (data.get("items") or {}).items() if r in registry.rigs}
    if not items:
        return jsonify({"ok": False, "error": "nothing selected"}), 400
    if not settings.rsync_info()["ok"]:
        return jsonify({"ok": False, "error": settings.rsync_info()["error"]}), 400
    job = Job("sync", list(items.keys()), {"items": items, "auto_purge": bool(settings.load().get("auto_purge")),
                                          "poweroff": False})
    runner.submit(job, sync.job_sync)
    return jsonify({"ok": True, "job": job.to_dict(log_tail=0)})


def _preview(names, want):
    """Per rig: the folders a Sync & Poweroff (want='red') or Purge (want='green') would touch."""
    out = {}
    for n in names:
        rs = registry.rigs[n]
        card = sync.cached_card(rs)
        entry = {"folders": [], "bytes": 0, "skipped_reason": None, "followers": [],
                 "engine_running": card.get("engine_running"), "phase": rs.phase}
        if rs.phase == "running":
            entry["skipped_reason"] = "a session is running"
        elif card.get("error") or not card.get("ssh_ok"):
            entry["skipped_reason"] = card.get("error") or "leader unreachable"
        elif card.get("engine_running"):
            entry["skipped_reason"] = "the experiment engine is running on the leader"
        else:
            for f in card["folders"]:
                if want == "red" and f["status"] != "green":
                    entry["folders"].append(f)
                elif want == "green" and f["status"] == "green" and not f.get("n_unconsolidated") and not f.get("video_only"):
                    entry["folders"].append(f)
            entry["bytes"] = sum(f.get("bytes", 0) for f in entry["folders"])
        if want == "red":
            for pi in rs.followers():
                entry["followers"].append({"name": pi["name"], "ip": pi["ip"], "ssh_ok": ssh_ok(ssh_target(pi))})
            entry["leader"] = {"name": rs.leader()["name"], "ip": rs.leader()["ip"], "ssh_ok": card.get("ssh_ok")}
        out[n] = entry
    return out


@bp.route("/sync_poweroff/preview", methods=["POST"])
def api_sync_poweroff_preview():
    names = _rigs_from(request.json or {})
    return jsonify({"ok": True, "rigs": _preview(names, "red"),
                    "auto_purge": bool(settings.load().get("auto_purge"))})


@bp.route("/sync_poweroff", methods=["POST"])
def api_sync_poweroff():
    data = request.json or {}
    if not data.get("confirm"):
        return jsonify({"ok": False, "error": "confirm required"}), 400
    names = _rigs_from(data)
    if not names:
        return jsonify({"ok": False, "error": "no rigs selected"}), 400
    if not settings.rsync_info()["ok"]:
        return jsonify({"ok": False, "error": settings.rsync_info()["error"]}), 400
    job = Job("sync_poweroff", names, {"items": {}, "all_unsynced": True, "poweroff": True,
                                       "auto_purge": bool(settings.load().get("auto_purge"))})
    runner.submit(job, sync.job_sync)
    return jsonify({"ok": True, "job": job.to_dict(log_tail=0)})


@bp.route("/purge/preview", methods=["POST"])
def api_purge_preview():
    names = _rigs_from(request.json or {})
    return jsonify({"ok": True, "rigs": _preview(names, "green"), "data_root": str(settings.data_root())})


@bp.route("/purge", methods=["POST"])
def api_purge():
    data = request.json or {}
    if not data.get("confirm"):
        return jsonify({"ok": False, "error": "confirm required"}), 400
    items = {r: list(f) for r, f in (data.get("items") or {}).items() if r in registry.rigs}
    if not items:
        return jsonify({"ok": False, "error": "nothing selected"}), 400
    job = Job("purge", list(items.keys()), {"items": items})
    runner.submit(job, sync.job_purge)
    return jsonify({"ok": True, "job": job.to_dict(log_tail=0)})


@bp.route("/jobs")
def api_jobs():
    return jsonify({"jobs": runner.recent(), "current": _busy_job()})


@bp.route("/jobs/<job_id>")
def api_job(job_id):
    j = runner.get(job_id)
    if not j:
        return jsonify({"ok": False, "error": "no such job"}), 404
    return jsonify(j.to_dict())


@bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    j = runner.get(job_id)
    if not j:
        return jsonify({"ok": False, "error": "no such job"}), 404
    if j.kind == "sync_poweroff" and any(r.get("poweroff") in ("sent", "off") for r in j.result.values()):
        return jsonify({"ok": False, "error": "a power-off was already sent; cannot cancel"}), 409
    runner.cancel(job_id)
    return jsonify({"ok": True})
