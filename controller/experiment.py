"""
controller/experiment.py

The Experiment tab's backend: every route app/app.py had, now under /api/rigs/<rig>/... and
reading a RigState from the registry instead of module globals. Transfer and the folder
picker are gone (the Data tab owns data movement); `reset` is new (Ended -> connected,
which Transfer used to do as a side effect); the UDP listener is no longer started here
(controller/events.py opens it once for every rig).
"""
from __future__ import annotations
import base64
import os
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path

import requests
import yaml
from flask import Blueprint, Response, g, jsonify, request

from shared.config import (load_task, save_task, make_session_id, register_session,
                           get_subject_history)
from shared.mjpeg_relay import relay
from controller import settings
from controller.registry import registry, RigState
from controller.events import send_command, sse_response

ROOT = settings.ROOT

bp = Blueprint("experiment", __name__, url_prefix="/api/rigs/<rig>")


@bp.url_value_preprocessor
def _pull_rig(endpoint, values):
    g.rig_name = (values or {}).pop("rig", None)


@bp.before_request
def _require_loaded():
    """Every route except load_rig needs the rig in the registry."""
    if request.endpoint == "experiment.load_rig":
        return None
    try:
        g.rs = registry.get(g.rig_name)
    except KeyError:
        return jsonify({"ok": False, "error": f"rig '{g.rig_name}' is not loaded"}), 404
    return None


# ── helpers ──

def _upload_file(ip: str, port: int, local_path: str, remote_path: str):
    """Upload a file to a Pi via its REST API."""
    with open(local_path, "rb") as f:
        requests.post(f"http://{ip}:{port}/api/upload",
                      files={"file": f}, data={"path": remote_path}, timeout=10)


def _session_dir(rs: RigState, root: Path) -> Path:
    """<root>/<subject>/<subject>_<date>/<session_id> — the same layout the Data tab syncs
    into, so plots saved at Go land alongside the data once it is synced."""
    subject_id = rs.session["subject_id"]
    date_str = rs.session["date"]
    return Path(root) / subject_id / f"{subject_id}_{date_str}" / rs.session_id


def _fmt_hm(ms) -> str:
    """'1 hour, 15 minutes' from a duration in ms (for the session-start ETA)."""
    total_min = int(round((ms or 0) / 60000))
    h, m = divmod(total_min, 60)
    return f"{h} hour{'' if h == 1 else 's'}, {m} minute{'' if m == 1 else 's'}"


def _save_session_logs(rs: RigState):
    """Fetch engine logs from all Pis into logs/<rig>/engine_<session>.log, plus a copy into
    the session folder when one exists (one file per session, not one truncated file per rig)."""
    api_port = rs.api_port
    log_dir = ROOT / "logs" / rs.name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"engine_{rs.session_id or 'unknown'}.log"
    lines = [f"# Rig: {rs.name}", f"# Session: {rs.session_id}",
             f"# Saved: {datetime.now().isoformat()}", ""]
    for pi in rs.pis:
        lines.append(f"── {pi['name']} ({pi['ip']}) ──")
        try:
            r = requests.get(f"http://{pi['ip']}:{api_port}/api/logs?n=500", timeout=5)
            lines.extend(r.json().get("lines", []))
        except Exception as e:
            lines.append(f"Error fetching logs: {e}")
        lines.append("")
    text = "\n".join(lines) + "\n"
    try:
        log_path.write_text(text)
    except OSError as e:
        print(f"[logs] could not write {log_path}: {e}")
    if rs.session_dir:
        try:
            Path(rs.session_dir).mkdir(parents=True, exist_ok=True)
            (Path(rs.session_dir) / "engine.log").write_text(text)
        except OSError:
            pass
    print(f"[logs] Engine logs saved to {log_path}")


def _register_session(rs: RigState) -> None:
    """Record the finished session in <data_root>/subjects/<subject>.json. Used to happen at
    Transfer; now at session end, so it no longer depends on syncing."""
    try:
        sess = rs.session or {}
        if not (rs.session_id and sess.get("subject_id")):
            return
        register_session(sess["subject_id"], rs.session_id, int(sess.get("session_num", 0) or 0),
                         str(sess.get("date", "")), rs.task_config or {},
                         settings.data_root() / "subjects",
                         n_trials_completed=len(rs.trials), notes=sess.get("notes", ""),
                         rig=rs.name)
    except Exception as e:
        rs.logger.warning(f"register_session failed: {e}")


def teardown_session(rs: RigState, reason: str) -> bool:
    """End-of-session teardown: stop the cameras, STOP the leader, kill the engine processes,
    save the logs, record the session, and mark it ended.

      reason "manual"      — the STOP button
      reason "session_end" — the leader reported the session finished by itself

    Idempotent: the first caller wins and later ones return False."""
    with rs.teardown_lock:
        if rs.phase != "running":
            return False
        rig = rs.config
        leader = rs.leader()
        api_port = rs.api_port

        # Stop camera recording first (so the video file is finalized). force=True: this is the
        # legitimate end-of-session stop, allowed to finalize a real session recording.
        for pi in rs.pis:
            if "camera" in pi.get("devices", []):
                try:
                    requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop",
                                  json={"force": True}, timeout=10)
                except Exception:
                    pass

        send_command(leader["ip"], rig["network"]["command_port"], {"cmd": "STOP"})
        time.sleep(3)   # let the leader finalize (HDF5, metadata)

        def _kill_pi(ip):
            try:
                requests.post(f"http://{ip}:{api_port}/api/stop", json={}, timeout=15)
            except Exception:
                pass

        threads = [threading.Thread(target=_kill_pi, args=(pi["ip"],)) for pi in rs.pis]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=16)

        _save_session_logs(rs)
        rs.logger.info(f"STOP({reason}) session={rs.session_id} trials={len(rs.trials)}")
        if reason == "manual" and not rs.session_end_seen:
            rs.notify(f"⏹️ {rig['name']} — stopped, but no session_end came from the leader "
                      f"({rs.session_id})")
        rs.phase = "ended"
        _register_session(rs)
        return True


# ── rig lifecycle ──

@bp.route("/load_rig", methods=["POST"])
def load_rig():
    """Load (or re-read) the rig config into the registry and CONNECT: stop any running engine
    processes, release devices on all Pis and initialize each Pi's devices."""
    name = g.rig_name
    try:
        rs = registry.load(name)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 409

    rs.deployed = False
    rs.phase = "setup"

    rig = rs.config
    api_port = rs.api_port
    devices = rig.get("devices", {})
    pi_results = {}
    for pi in rs.pis:
        pname, ip = pi["name"], pi["ip"]
        try:
            requests.post(f"http://{ip}:{api_port}/api/stop", json={}, timeout=5)
        except Exception:
            pass
        try:
            requests.post(f"http://{ip}:{api_port}/api/release_devices", json={}, timeout=5)
        except Exception:
            pass
        try:
            r = requests.get(f"http://{ip}:{api_port}/api/status", timeout=3)
            pi_results[pname] = {"ok": True, "status": r.json()}
        except Exception as e:
            pi_results[pname] = {"ok": False, "error": str(e)}
            continue

        # Initialize this Pi's devices. Generic: pi_api resolves the class from each device's
        # `type`, so the controller needs no per-device knowledge — this branch ships none.
        init_errors = []
        for dev_name in pi.get("devices", []):
            dev_cfg = devices.get(dev_name, {})
            if not dev_cfg.get("enabled", True):
                continue
            try:
                r = requests.post(f"http://{ip}:{api_port}/api/init_device", json={
                    "name": dev_name,
                    "type": dev_cfg.get("type", dev_name),
                    "config": dev_cfg,
                }, timeout=25)
                j = r.json()
                if not j.get("ok"):
                    init_errors.append(f"{dev_name}: {j.get('error', j.get('message', 'init failed'))}")
            except requests.exceptions.Timeout:
                init_errors.append(f"{dev_name}: timed out waiting for response")
            except requests.exceptions.ConnectionError:
                init_errors.append(f"{dev_name}: lost connection to Pi")
            except Exception as e:
                init_errors.append(f"{dev_name}: {e}")
        if init_errors:
            pi_results[pname] = {"ok": False, "error": "; ".join(init_errors)}

    all_pis_ok = bool(pi_results) and all(r["ok"] for r in pi_results.values())
    rs.pi_status = {n: {"ok": r["ok"], "api": r["ok"], "t": time.time()} for n, r in pi_results.items()}
    if all_pis_ok:
        rs.phase = "connected"
    rs.logger.info(f"CONNECT ok={all_pis_ok}")
    return jsonify({"ok": True, "rig": rs.config, "data_dir": str(settings.data_root()),
                    "pi_results": pi_results, "all_pis_ok": all_pis_ok})


@bp.route("/unload", methods=["POST"])
def unload():
    try:
        registry.unload(g.rig_name)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True})


@bp.route("/state")
def get_state():
    """Current state for UI polling — and, at page load, for resync after a refresh."""
    rs: RigState = g.rs
    s = rs.snapshot()
    s["rig"] = rs.name
    return jsonify(s)


@bp.route("/connect", methods=["POST"])
def connect():
    """Lightweight: check the Pi REST APIs are online (no device init)."""
    rs: RigState = g.rs
    api_port = rs.api_port
    results = {}
    for pi in rs.pis:
        try:
            r = requests.get(f"http://{pi['ip']}:{api_port}/api/status", timeout=3)
            results[pi["name"]] = {"ok": True, "status": r.json()}
        except Exception as e:
            results[pi["name"]] = {"ok": False, "error": str(e)}
    all_ok = bool(results) and all(r["ok"] for r in results.values())
    if all_ok and rs.phase == "setup":
        rs.phase = "connected"
    return jsonify({"results": results, "all_ok": all_ok})


@bp.route("/reset", methods=["POST"])
def reset():
    """After a session has ended: clear the run and return the rig to 'connected' (Transfer used
    to do this). The session fields stay so the operator can bump the session number and Deploy."""
    rs: RigState = g.rs
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "session is running — STOP first"}), 409
    if rs.phase in ("ended", "deployed"):
        rs.phase = "connected"
    rs.deployed = False
    rs.trials.clear()
    rs.session_end_seen = False
    rs.session_dir = None
    return jsonify({"ok": True, **rs.snapshot()})


# ── task / session ──

@bp.route("/load_task", methods=["POST"])
def api_load_task():
    rs: RigState = g.rs
    data = request.json or {}
    task_name = data.get("task")
    task_path = ROOT / "experiments" / f"{task_name}.yaml"
    if not task_path.exists():
        return jsonify({"error": f"Task config not found: {task_path}"}), 404
    rs.task_config = load_task(task_path)
    rs.task_path = str(task_path)
    rs.deployed = False
    return jsonify({"task": rs.task_config})


@bp.route("/update_session", methods=["POST"])
def update_session():
    rs: RigState = g.rs
    data = request.json or {}
    rs.session = data
    rs.session_id = make_session_id(data["subject_id"], data["date"], int(data["session_num"]))
    rs.deployed = False
    return jsonify({"session_id": rs.session_id})


@bp.route("/update_task", methods=["POST"])
def update_task():
    """Merge task sections. Invalidates deploy. {"stimulus": {"size_deg": 4.0}, ...}"""
    rs: RigState = g.rs
    data = request.json or {}
    if rs.task_config is None:
        return jsonify({"error": "No task config loaded"}), 400
    for section, params in data.items():
        if isinstance(params, dict):
            rs.task_config.setdefault(section, {}).update(params)
        else:
            rs.task_config[section] = params
    rs.deployed = False
    return jsonify({"ok": True})


@bp.route("/save_task", methods=["POST"])
def save_task_route():
    rs: RigState = g.rs
    if rs.task_config is None or not rs.task_path:
        return jsonify({"error": "No task config loaded"}), 400
    today = date.today().isoformat()
    rs.task_config["last_saved"] = today
    save_task(rs.task_config, rs.task_path)
    return jsonify({"ok": True, "last_saved": today})


@bp.route("/save_task_as", methods=["POST"])
def save_task_as_route():
    rs: RigState = g.rs
    if rs.task_config is None:
        return jsonify({"error": "No task config loaded"}), 400
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "No name provided"}), 400
    safe = "".join(c for c in name if c.isalnum() or c in "_-")
    if not safe:
        return jsonify({"error": "Invalid name"}), 400
    new_path = ROOT / "experiments" / f"{safe}.yaml"
    today = date.today().isoformat()
    rs.task_config["last_saved"] = today
    save_task(rs.task_config, new_path)
    rs.task_path = str(new_path)
    return jsonify({"ok": True, "last_saved": today, "name": safe})


# ── deploy ──

@bp.route("/deploy", methods=["POST"])
def deploy():
    """Upload code + configs to the Pis. A successful Deploy is what enables Go."""
    rs: RigState = g.rs
    rig, task = rs.config, rs.task_config
    if not rig or not task:
        return jsonify({"error": "Config not loaded"}), 400
    if not rs.session_id:
        return jsonify({"error": "Session not configured"}), 400
    if rs.phase == "running":
        return jsonify({"error": "session is running"}), 409
    if not rs.try_busy("deploy"):
        return jsonify({"error": f"rig is busy ({rs.busy_kind})"}), 409

    api_port = rs.api_port
    leader = rs.leader()
    steps = []
    try:
        # 0. Upload code to Pis — the file list is shared/deploy_manifest.py.
        from shared.deploy_manifest import deploy_files
        for pi in rs.pis:
            for local_rel, remote_rel in deploy_files(pi["role"]):
                local = ROOT / local_rel
                if local.exists():
                    _upload_file(pi["ip"], api_port, str(local), remote_rel)
                else:
                    steps.append(f"WARNING: missing local file skipped: {local_rel}")
        steps.append("Uploaded code to all Pis")

        # 0b. Restart pi_api on all Pis so new code takes effect. Timed: the leader's shepherd
        #     gets the measured outage as its api_health grace period, so a Deploy doesn't page
        #     "pi_api not responding"; shepherd is restarted too, so the shepherd.py uploaded
        #     above is what runs (controller/pi_restart.py).
        from controller import pi_restart
        results, restart_steps = pi_restart.restart_timed(rs.pis, api_port, reload_shepherd=True)
        steps.extend(restart_steps)
        for pi in rs.pis:
            if not results[pi["name"]]["ok"]:
                raise RuntimeError(f"{pi['name']} did not come back after restart: {results[pi['name']]['error']}")
        steps.append("Restarted pi_api on all Pis")

        # 1. Rig config to all Pis (under its own filename: pi_api sees rigs/<rig>.yaml)
        for pi in rs.pis:
            _upload_file(pi["ip"], api_port, str(rs.path), f"rigs/{rs.path.name}")
        steps.append("Uploaded rig config to all Pis")

        # 2. Task config to the Leader. There is no stimulus pipeline on this branch — the engine
        #    runs the generic 4-phase loop — so nothing is generated here or pushed to a follower.
        _upload_file(leader["ip"], api_port, rs.task_path, f"experiments/{Path(rs.task_path).name}")
        steps.append("Uploaded task config to Leader")

        rs.deployed = True
        rs.phase = "deployed"
        rs.logger.info(f"DEPLOY ok session={rs.session_id} steps={len(steps)}")
        return jsonify({"ok": True, "steps": steps})
    except Exception as e:
        rs.logger.error(f"DEPLOY failed session={rs.session_id} error={e}")
        return jsonify({"ok": False, "error": str(e), "steps": steps})
    finally:
        rs.release_busy()


# ── run ──

@bp.route("/go", methods=["POST"])
def go():
    """Start engine processes on the Pis, then send UDP START to the Leader."""
    rs: RigState = g.rs
    if not rs.deployed:
        return jsonify({"error": "Not deployed"}), 400
    if rs.phase == "running":
        return jsonify({"error": "already running"}), 409
    subj = (rs.session or {}).get("subject_id")
    other = registry.subject_in_use(subj, exclude=rs.name) if subj else None
    if other:
        return jsonify({"ok": False, "error": f"subject {subj} is already running on rig '{other}'"}), 409
    if not rs.try_busy("go"):
        return jsonify({"error": f"rig is busy ({rs.busy_kind})"}), 409
    try:
        return _go(rs)
    finally:
        rs.release_busy()


def _go(rs: RigState):
    rig = rs.config
    leader = rs.leader()
    api_port = rs.api_port
    cmd_port = rig["network"]["command_port"]
    session = rs.session

    _body = request.get_json(silent=True) or {}
    save = _body.get("save", {})
    # Create the session folder under the controller's data root now, so whatever the UI saves
    # at end/stop lands with the session; the Data tab syncs the Pi's own files into it later.
    try:
        sdir = _session_dir(rs, settings.data_root())
        sdir.mkdir(parents=True, exist_ok=True)
        rs.session_dir = str(sdir)
        print(f"[go:{rs.name}] session dir: {sdir}")
    except Exception as e:
        print(f"[go:{rs.name}] could not create session dir: {e}")
        rs.session_dir = None
    rig_filename = rs.path.name
    task_filename = Path(rs.task_path).name
    steps = []

    # Stop leftover processes and release devices on every Pi
    for pi in rs.pis:
        try:
            requests.post(f"http://{pi['ip']}:{api_port}/api/stop", json={}, timeout=5)
        except Exception:
            pass
        try:
            r = requests.post(f"http://{pi['ip']}:{api_port}/api/release_devices", json={}, timeout=3)
            if r.status_code == 200:
                released = r.json().get("released", [])
                if released:
                    steps.append(f"Released devices on {pi['name']}: {', '.join(released)}")
        except Exception:
            pass

    # Leader engine. Devices unchecked in the Actions row go to --no-save, and the engine then
    # skips their HDF5 datasets. Relative paths: pi_api resolves them against ~/rig on the Pi,
    # whatever that Pi's user is called.
    skip_save = [d for d, cfg in (rig.get("devices") or {}).items()
                 if cfg.get("enabled", True) and not save.get(d, True)]
    leader_args = [
        "--rig", f"rigs/{rig_filename}",
        "--task", f"experiments/{task_filename}",
        "--subject", session["subject_id"],
        "--date", session["date"],
        "--session-num", str(int(session["session_num"])),
        "--notes", session.get("notes", ""),
    ]
    if skip_save:
        leader_args += ["--no-save", ",".join(skip_save)]
        steps.append(f"Not saving: {', '.join(skip_save)}")
    try:
        r = requests.post(f"http://{leader['ip']}:{api_port}/api/start",
                          json={"script": "leader", "args": leader_args}, timeout=10)
        res = r.json()
        if res.get("ok"):
            steps.append(f"Leader started on {leader['name']} (pid {res.get('pid', '?')})")
        else:
            return jsonify({"ok": False, "error": f"Leader: {res.get('error', 'unknown')}", "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to start leader: {e}", "steps": steps})

    # Wait for the leader to finish device init ("Waiting for START command..."); if it exits
    # during init instead, surface its error rather than a silent no-start.
    leader_ready = False
    for _ in range(60):        # up to ~30 s
        time.sleep(0.5)
        try:
            st = requests.get(f"http://{leader['ip']}:{api_port}/api/status", timeout=3).json()
            lines = requests.get(f"http://{leader['ip']}:{api_port}/api/logs",
                                 params={"n": 80}, timeout=3).json().get("lines", [])
        except Exception:
            continue
        if any("Waiting for START" in ln for ln in lines):
            leader_ready = True
            break
        if not st.get("process_running"):
            err = next((ln for ln in reversed(lines)
                        if "cannot init" in ln.lower() or "Error" in ln or "Traceback" in ln),
                       (lines[-1] if lines else "leader exited during device init"))
            return jsonify({"ok": False, "error": f"Leader init failed — {err}", "steps": steps})
    if not leader_ready:
        steps.append("⚠️  Leader readiness not confirmed after 30 s — proceeding")

    # Every video device: RECORD when its save box is checked, else preview-only livestream.
    for dev_name, dev_cfg, pi in _video_devices(rig):
        save_this = bool(save.get(dev_name, True))
        video_dir = (rig.get("data") or {}).get("video_dir") or ""
        payload = {"device": dev_name, "type": dev_cfg.get("type", dev_name), "config": dev_cfg}
        if save_this:
            payload.update({"session_id": rs.session_id, "video_dir": video_dir})   # -> record
        else:
            payload["downsample"] = True                                            # -> preview only
        try:
            requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop",
                          json={"device": dev_name}, timeout=5)
            r = requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_start",
                              json=payload, timeout=15)
            ok, err = False, f"HTTP {r.status_code}"
            try:
                j = r.json()
                ok, err = bool(j.get("ok")), j.get("error", err)
            except Exception:
                pass
            if ok and save_this:
                steps.append(f"{dev_name} recording on {pi['name']}")
            elif ok:
                steps.append(f"{dev_name} livestream (NOT saved) on {pi['name']}")
            elif save_this:
                warn = (f"⚠️  {dev_name.upper()} NOT RECORDING on {pi['name']}: {err} — the session "
                        f"runs WITHOUT this video. Check video_dir ({video_dir or 'unset'}).")
                steps.append(warn)
                rs.logger.warning(warn)
            else:
                steps.append(f"⚠️  {dev_name} livestream failed on {pi['name']}: {err}")
        except Exception as e:
            if save_this:
                warn = (f"⚠️  {dev_name.upper()} NOT RECORDING on {pi['name']}: {e} — the session "
                        f"runs WITHOUT this video. Check video_dir.")
                steps.append(warn)
                rs.logger.warning(warn)
            else:
                steps.append(f"⚠️  {dev_name} livestream failed on {pi['name']}: {e}")

    time.sleep(1.5)
    send_command(leader["ip"], cmd_port, {"cmd": "START", "session_id": rs.session_id})
    steps.append("START command sent")

    rs.phase = "running"
    rs.trials.clear()
    rs.session_end_seen = False
    rs.logger.info(f"GO session={rs.session_id}")
    _now = time.time()
    _when = f" at {time.strftime('%H:%M', time.localtime(_now))}"
    _est_ms = _body.get("estimate_ms")
    if isinstance(_est_ms, (int, float)) and not isinstance(_est_ms, bool) and _est_ms > 0:
        _end = time.strftime('%H:%M', time.localtime(_now + _est_ms / 1000))
        _when += f" and estimated to end at {_end} (in {_fmt_hm(_est_ms)})"
    rs.notify(f"▶️ {rig['name']} — session started: "
              f"{session.get('subject_id', '?')} ({rs.session_id}){_when}")
    return jsonify({"ok": True, "steps": steps})


def _video_devices(rig):
    """(name, config, pi) for every enabled device the rig marks `video: true` — the ones the
    controller previews and records through the camera endpoints below."""
    out = []
    for name, cfg in (rig.get("devices") or {}).items():
        if not cfg.get("enabled", True) or not cfg.get("video", False):
            continue
        pi = next((p for p in rig["pis"] if name in p.get("devices", [])), None)
        if pi is not None:
            out.append((name, cfg, pi))
    return out


def _find_video_device(rig, name):
    """(name, cfg, pi) for a named video device, or the first one when name is falsy."""
    devs = _video_devices(rig)
    if not name:
        return devs[0] if devs else None
    return next((d for d in devs if d[0] == name), None)


@bp.route("/stop", methods=["POST"])
def stop():
    teardown_session(g.rs, "manual")
    return jsonify({"ok": True})


@bp.route("/trials")
def get_trials():
    return jsonify(g.rs.trials)


@bp.route("/subject_history/<subject_id>")
def subject_history(subject_id):
    return jsonify(get_subject_history(subject_id, settings.data_root() / "subjects"))


# ── video devices (preview / recording) ──

@bp.route("/camera_start", methods=["POST"])
def camera_start():
    """Start a video device's preview. Body: {device}, defaulting to the rig's first video
    device. pi_api refuses to restart one that is recording a session."""
    rs: RigState = g.rs
    found = _find_video_device(rs.config, (request.get_json(silent=True) or {}).get("device"))
    if not found:
        return jsonify({"ok": False, "error": "No video device assigned"}), 400
    dev_name, cfg, pi = found
    try:
        r = requests.post(f"http://{pi['ip']}:{rs.api_port}/api/camera_preview_start",
                          json={"device": dev_name, "type": cfg.get("type", dev_name),
                                "config": cfg, "downsample": True}, timeout=15)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/camera_stop", methods=["POST"])
def camera_stop():
    """Stop previews — all of them, or just {device}. Skipped while a session is running, so
    this can never stop a recording."""
    rs: RigState = g.rs
    if rs.phase == "running":
        return jsonify({"ok": True})
    want = (request.get_json(silent=True) or {}).get("device")
    for dev_name, _cfg, pi in _video_devices(rs.config):
        if want and dev_name != want:
            continue
        try:
            requests.post(f"http://{pi['ip']}:{rs.api_port}/api/camera_preview_stop",
                          json={"device": dev_name}, timeout=5)
        except Exception:
            pass
    return jsonify({"ok": True})


@bp.route("/camera_feed")
def camera_feed():
    """Proxy a Pi video device's MJPEG stream (?device=<name>). The reconnecting relay rides
    through pi_api restarts, the preview->record swap at Go, and the <img>-before-preview race
    instead of collapsing to a silent blank."""
    rs: RigState = g.rs
    found = _find_video_device(rs.config, request.args.get("device"))
    if not found:
        return "No video device", 400
    dev_name, _cfg, pi = found
    return Response(relay(f"http://{pi['ip']}:{rs.api_port}/api/camera_stream?device={dev_name}"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


# ── live events ──

@bp.route("/events")
def event_stream():
    return sse_response(g.rs)
