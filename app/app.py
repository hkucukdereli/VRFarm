"""
app/app.py

Controller-side Flask UI for running experiments.
Workflow: Setup -> Connect -> Deploy -> Running -> Ended -> Transfer

Communicates with Pi REST APIs (HTTP) for management and
UDP datagrams for real-time events/commands.
"""

from __future__ import annotations
import json
import logging
import os
import queue
import socket
import struct
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.config import (load_rig, load_task, save_task,
                           get_leader_pi,
                           make_session_id, register_session,
                           get_subject_history)
from shared.deploy_manifest import deploy_files
from shared.notify import notify, configure as _notify_configure

app = Flask(__name__)

# ── Global state ──

state = {
    "phase": "setup",       # setup, connected, deployed, running, ended
    "rig_config": None,
    "task_config": None,
    "rig_path": None,
    "task_path": None,
    "session": {},          # subject_id, date, session_num, notes
    "session_id": None,
    "deployed": False,
}

# Event queue for SSE
_event_queue = queue.Queue(maxsize=2000)

# UDP listener thread
_udp_thread = None
_udp_running = False
_cmd_sock = None
# Set by the listener on session_end; stop() checks it to avoid a duplicate notify
_session_end_seen = False

# Trial data for live display
_trials = []

# ── Logging ──

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# App-level logger (deploy, go, stop, transfer events)
_app_logger = logging.getLogger("vrfarm")
_app_logger.setLevel(logging.INFO)
_app_log_handler = logging.FileHandler(LOG_DIR / "app.log")
_app_log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
_app_logger.addHandler(_app_log_handler)


def _save_session_logs():
    """Fetch engine logs from all Pis and save to logs/engine.log."""
    rig = state["rig_config"]
    if not rig:
        return
    api_port = rig["network"]["api_port"]
    log_path = LOG_DIR / "engine.log"
    with open(log_path, "w") as f:
        f.write(f"# Session: {state['session_id']}\n")
        f.write(f"# Saved: {datetime.now().isoformat()}\n\n")
        for pi in rig["pis"]:
            f.write(f"── {pi['name']} ({pi['ip']}) ──\n")
            try:
                r = requests.get(
                    f"http://{pi['ip']}:{api_port}/api/logs?n=500",
                    timeout=5)
                for line in r.json().get("lines", []):
                    f.write(line + "\n")
            except Exception as e:
                f.write(f"Error fetching logs: {e}\n")
            f.write("\n")
    print(f"[logs] Engine logs saved to {log_path}")


# ── Routes ──

@app.route("/")
def index():
    # Discover available rigs and tasks
    rigs = sorted(Path(ROOT / "rigs").glob("*.yaml"))
    tasks = sorted(Path(ROOT / "experiments").glob("*.yaml"))
    return render_template("experiment.html",
                           rigs=[r.stem for r in rigs],
                           tasks=[t.stem for t in tasks])


def _apply_slack_config(rig_config):
    """Activate/deactivate Slack from the rig's `slack:` block (enabled + webhook_url), so the
    webhook lives in the rig config instead of a manual `export VRFARM_SLACK_WEBHOOK=...`. Sets the
    env var (the 'export') AND notify's runtime override — the override is what actually takes effect,
    since notify.py reads the env var at import time (before any rig is loaded). Disabled/blank = off."""
    slack = (rig_config or {}).get("slack", {}) or {}
    enabled = bool(slack.get("enabled"))
    url = (slack.get("webhook_url") or "").strip()
    if not enabled:
        _notify_configure("")            # explicitly off
        _app_logger.info("Slack notifications off")
    elif url:
        os.environ["VRFARM_SLACK_WEBHOOK"] = url
        _notify_configure(url)           # webhook from the rig config
        _app_logger.info("Slack notifications ON (rig webhook)")
    else:
        _notify_configure(None)          # enabled but no rig URL → use VRFARM_SLACK_WEBHOOK env var
        env_on = bool(os.environ.get("VRFARM_SLACK_WEBHOOK", "").strip())
        _app_logger.info(f"Slack notifications {'ON (env webhook)' if env_on else 'off (no webhook set)'}")


@app.route("/api/load_rig", methods=["POST"])
def api_load_rig():
    """Load rig config and connect to Pis.

    Stops any running engine processes and releases devices on all Pis
    so the rig starts from a clean state.
    """
    data = request.json
    rig_name = data.get("rig")
    rig_path = ROOT / "rigs" / f"{rig_name}.yaml"

    if not rig_path.exists():
        return jsonify({"error": f"Rig config not found: {rig_path}"}), 404

    state["rig_config"] = load_rig(rig_path)
    state["rig_path"] = str(rig_path)
    _apply_slack_config(state["rig_config"])
    state["deployed"] = False
    state["phase"] = "setup"
    state["trial_table"] = []

    # Reset all Pis: stop processes, release devices
    rig = state["rig_config"]
    api_port = rig["network"]["api_port"]
    devices = rig.get("devices", {})
    pi_results = {}
    for pi in rig["pis"]:
        name = pi["name"]
        ip = pi["ip"]
        try:
            requests.post(f"http://{ip}:{api_port}/api/stop",
                          json={}, timeout=5)
        except Exception:
            pass
        try:
            requests.post(f"http://{ip}:{api_port}/api/release_devices",
                          json={}, timeout=5)
        except Exception:
            pass
        try:
            r = requests.get(f"http://{ip}:{api_port}/api/status", timeout=3)
            pi_results[name] = {"ok": True, "status": r.json()}
        except Exception as e:
            pi_results[name] = {"ok": False, "error": str(e)}
            continue

        # Initialize devices assigned to this Pi (generic: pi_api resolves the class
        # from the device's `type` — no per-device knowledge on the controller)
        pi_devs = pi.get("devices", [])
        init_errors = []
        for dev_name in pi_devs:
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
                    init_errors.append(
                        f"{dev_name}: {j.get('error', j.get('message', 'init failed'))}")
            except requests.exceptions.Timeout:
                init_errors.append(f"{dev_name}: timed out waiting for response")
            except requests.exceptions.ConnectionError:
                init_errors.append(f"{dev_name}: lost connection to Pi")
            except Exception as e:
                init_errors.append(f"{dev_name}: {e}")

        if init_errors:
            pi_results[name] = {"ok": False, "error": "; ".join(init_errors)}

    all_pis_ok = all(r["ok"] for r in pi_results.values())
    if all_pis_ok:
        state["phase"] = "connected"

    return jsonify({
        "rig": state["rig_config"],
        "data_dir": str(_data_dir()),   # controller-resolved (env/default), not from the yaml
        "pi_results": pi_results,
        "all_pis_ok": all_pis_ok,
    })


@app.route("/api/load_task", methods=["POST"])
def api_load_task():
    """Load task/experiment config."""
    data = request.json
    task_name = data.get("task")
    task_path = ROOT / "experiments" / f"{task_name}.yaml"

    if not task_path.exists():
        return jsonify({"error": f"Task config not found: {task_path}"}), 404

    state["task_config"] = load_task(task_path)
    state["task_path"] = str(task_path)
    state["deployed"] = False

    return jsonify({
        "task": state["task_config"],
    })


@app.route("/api/update_session", methods=["POST"])
def update_session():
    """Update session params (subject, date, level, etc.)."""
    data = request.json

    # (Grace period moved to the task YAML 'session' section — saved via update_task/save_task.)
    state["session"] = data
    state["session_id"] = make_session_id(
        data["subject_id"], data["date"], int(data["session_num"]))
    state["deployed"] = False
    return jsonify({"session_id": state["session_id"]})


@app.route("/api/update_task", methods=["POST"])
def update_task():
    """Update task config sections. Invalidates deploy.

    Expects JSON like: {"stimulus": {"size_deg": 4.0}, "reward": {"level": 2}}
    """
    data = request.json
    if state["task_config"] is None:
        return jsonify({"error": "No task config loaded"}), 400

    for section, params in data.items():
        if isinstance(params, dict):
            state["task_config"].setdefault(section, {}).update(params)
        else:
            state["task_config"][section] = params

    state["deployed"] = False
    return jsonify({"ok": True})


@app.route("/api/save_task", methods=["POST"])
def save_task_route():
    """Save current task config back to YAML file."""
    if state["task_config"] is None or not state.get("task_path"):
        return jsonify({"error": "No task config loaded"}), 400
    today = date.today().isoformat()
    state["task_config"]["last_saved"] = today
    save_task(state["task_config"], state["task_path"])
    return jsonify({"ok": True, "last_saved": today})


@app.route("/api/save_task_as", methods=["POST"])
def save_task_as_route():
    """Save current task config to a new YAML file."""
    if state["task_config"] is None:
        return jsonify({"error": "No task config loaded"}), 400
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "No name provided"}), 400
    # Sanitize: allow alphanumeric, underscores, hyphens
    safe = "".join(c for c in name if c.isalnum() or c in "_-")
    if not safe:
        return jsonify({"error": "Invalid name"}), 400
    new_path = ROOT / "experiments" / f"{safe}.yaml"
    today = date.today().isoformat()
    state["task_config"]["last_saved"] = today
    save_task(state["task_config"], new_path)
    # Update state to point to the new file
    state["task_path"] = str(new_path)
    return jsonify({"ok": True, "last_saved": today, "name": safe})


@app.route("/api/deploy", methods=["POST"])
def deploy():
    """Deploy code + configs to the Pis. Enables Go button on success."""
    rig = state["rig_config"]
    task = state["task_config"]
    if not rig or not task:
        return jsonify({"error": "Config not loaded"}), 400
    if not state["session_id"]:
        return jsonify({"error": "Session not configured"}), 400

    api_port = rig["network"]["api_port"]
    leader = get_leader_pi(rig)
    steps = []

    try:
        # 0. Upload code to Pis (single shared manifest — see shared/deploy_manifest.py)
        for pi in rig["pis"]:
            for local_rel, remote_rel in deploy_files(pi["role"]):
                local = ROOT / local_rel
                if local.exists():
                    _upload_file(pi["ip"], api_port, str(local), remote_rel)
        steps.append("Uploaded code to all Pis")

        # 0b. Restart pi_api on all Pis so new code takes effect
        for pi in rig["pis"]:
            try:
                requests.post(f"http://{pi['ip']}:{api_port}/api/restart",
                              json={}, timeout=3)
            except Exception:
                pass  # Connection may drop during restart
        time.sleep(3)  # Wait for systemd to restart the services

        # Verify Pis are back
        for pi in rig["pis"]:
            alive = False
            for attempt in range(10):
                try:
                    r = requests.get(f"http://{pi['ip']}:{api_port}/api/status",
                                     timeout=2)
                    if r.status_code == 200:
                        alive = True
                        break
                except Exception:
                    pass
                time.sleep(1)
            if not alive:
                raise RuntimeError(f"{pi['name']} did not come back after restart")
        steps.append("Restarted pi_api on all Pis")

        # 1. Upload rig config to all Pis
        for pi in rig["pis"]:
            _upload_file(pi["ip"], api_port, state["rig_path"],
                         f"rigs/{Path(state['rig_path']).name}")
        steps.append("Uploaded rig config to all Pis")

        # 2. Upload task config to Leader
        _upload_file(leader["ip"], api_port, state["task_path"],
                     f"experiments/{Path(state['task_path']).name}")
        steps.append("Uploaded task config to Leader")

        state["deployed"] = True
        state["phase"] = "deployed"
        _app_logger.info(f"DEPLOY ok session={state['session_id']} steps={len(steps)}")
        return jsonify({"ok": True, "steps": steps})

    except Exception as e:
        _app_logger.error(f"DEPLOY failed session={state.get('session_id')} error={e}")
        return jsonify({"ok": False, "error": str(e), "steps": steps})


@app.route("/api/go", methods=["POST"])
def go():
    """Start engine processes on Pis, then send UDP START to Leader."""
    if not state["deployed"]:
        return jsonify({"error": "Not deployed"}), 400

    rig = state["rig_config"]
    leader = get_leader_pi(rig)
    api_port = rig["network"]["api_port"]
    cmd_port = rig["network"]["command_port"]
    session = state["session"]

    # Per-device "save data" choices from the Actions row (default: save everything). A video
    # device unchecked -> livestream only (no file); any other device unchecked -> Leader skips
    # its HDF5 datasets.
    _body = request.get_json(silent=True) or {}
    save = _body.get("save", {})
    rig_filename = Path(state["rig_path"]).name
    task_filename = Path(state["task_path"]).name

    steps = []

    # Fresh UDP event listener
    _stop_udp_listener()
    _start_udp_listener(rig["network"]["event_port"])
    print("[go] UDP listener started")

    # Stop any leftover processes, release devices on all Pis
    for pi in rig["pis"]:
        try:
            requests.post(f"http://{pi['ip']}:{api_port}/api/stop",
                          json={}, timeout=5)
        except Exception:
            pass
        try:
            r = requests.post(f"http://{pi['ip']}:{api_port}/api/release_devices",
                              json={}, timeout=3)
            if r.status_code == 200:
                res = r.json()
                released = res.get("released", [])
                if released:
                    steps.append(f"Released devices on {pi['name']}: {', '.join(released)}")
        except Exception:
            pass
    print("[go] Processes stopped, devices released")

    # Start leader engine
    print("[go] Starting leader...")
    # Devices the user chose NOT to save -> tell the Leader to skip their HDF5 datasets.
    # (Video devices are handled separately below: unchecked = preview-only, no file.)
    # `enabled` defaults True everywhere (engine, load_rig, UIs): present = on unless
    # explicitly disabled.
    skip_save = [d for d, cfg in rig.get("devices", {}).items()
                 if cfg.get("enabled", True) and not save.get(d, True)]
    # Relative paths: pi_api resolves them against ~/rig on the Pi, whatever its username.
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
        r = requests.post(
            f"http://{leader['ip']}:{api_port}/api/start",
            json={"script": "leader", "args": leader_args}, timeout=10)
        res = r.json()
        if res.get("ok"):
            steps.append(f"Leader started on {leader['name']} (pid {res.get('pid', '?')})")
        else:
            return jsonify({"ok": False, "error": f"Leader: {res.get('error', 'unknown')}", "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to start leader: {e}", "steps": steps})

    # Wait for the leader to finish device init before starting the cameras / sending START. The
    # leader prints "Waiting for START command..." once all devices are up; if it EXITS during
    # init instead, surface its error rather than a silent no-start.
    print("[go] Waiting for leader devices to init...")
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

    # Start every video device: RECORD to disk if its save-checkbox is checked, else
    # PREVIEW-only (livestream, no file). A device is a video device when its rig config
    # sets `video: true` (see _video_devices).
    for dev_name, dev_cfg, pi in _video_devices(rig):
        save_this = bool(save.get(dev_name, True))
        video_dir = rig.get("data", {}).get("video_dir") or "/media/vruser/ssd/video"   # `or`: a present-but-empty value falls back too
        payload = {
            "device": dev_name,
            "type": dev_cfg.get("type", dev_name),
            "config": dev_cfg,
        }
        if save_this:
            payload.update({"session_id": state["session_id"], "video_dir": video_dir})  # -> record
        else:
            payload["downsample"] = True   # -> preview-only livestream, no file
        try:
            requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop",
                          json={"device": dev_name}, timeout=5)
            r = requests.post(
                f"http://{pi['ip']}:{api_port}/api/camera_preview_start",
                json=payload, timeout=15)
            # A failed start may not return JSON (e.g. HTTP 500) — parse defensively.
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
                warn = (f"⚠️  {dev_name.upper()} NOT RECORDING on {pi['name']}: {err} — session "
                        f"runs WITHOUT this video. Check video_dir ({video_dir}).")
                steps.append(warn)
                _app_logger.warning(warn)
            else:
                steps.append(f"⚠️  {dev_name} livestream failed on {pi['name']}: {err}")
        except Exception as e:
            if save_this:
                warn = (f"⚠️  {dev_name.upper()} NOT RECORDING on {pi['name']}: {e} — session "
                        f"runs WITHOUT this video. Check video_dir.")
                steps.append(warn)
                _app_logger.warning(warn)
            else:
                steps.append(f"⚠️  {dev_name} livestream failed on {pi['name']}: {e}")

    # Give processes time to initialize and wait for START
    print("[go] Waiting for processes to init...")
    time.sleep(1.5)

    # Send START command via UDP
    _send_command(leader["ip"], cmd_port, {
        "cmd": "START",
        "session_id": state["session_id"],
    })
    steps.append("START command sent")

    state["phase"] = "running"
    _trials.clear()
    global _session_end_seen
    _session_end_seen = False
    _app_logger.info(f"GO session={state['session_id']}")
    _now = time.time()
    _when = f" at {time.strftime('%H:%M', time.localtime(_now))}"
    _est_ms = _body.get("estimate_ms")
    if isinstance(_est_ms, (int, float)) and not isinstance(_est_ms, bool) and _est_ms > 0:
        _end = time.strftime('%H:%M', time.localtime(_now + _est_ms / 1000))
        _when += f" and estimated to end at {_end} (in {_fmt_hm(_est_ms)})"
    notify(f"▶️ {rig['name']} — session started: "
           f"{state['session'].get('subject_id', '?')} ({state['session_id']}){_when}")
    return jsonify({"ok": True, "steps": steps})


def _video_devices(rig):
    """(name, config, pi) for every enabled rig device marked `video: true` — the devices the
    controller starts/stops through the camera endpoints (preview vs session recording).
    `enabled` defaults True, matching the engine and load_rig."""
    out = []
    for name, cfg in (rig.get("devices", {}) or {}).items():
        if not cfg.get("enabled", True) or not cfg.get("video", False):
            continue
        pi = next((p for p in rig["pis"] if name in p.get("devices", [])), None)
        if pi is not None:
            out.append((name, cfg, pi))
    return out


# Serializes end-of-session teardown so a natural session_end racing a manual STOP can only
# tear down once.
_teardown_lock = threading.Lock()


def _teardown_session(reason: str) -> bool:
    """End-of-session teardown: stop the cameras, STOP the leader, kill the engine processes,
    save the logs, and mark the session ended.

      reason "manual"      — the STOP button
      reason "session_end" — the leader reported the session finished by itself

    Idempotent: the first caller wins and later ones return False.

    MUST NOT be called on the UDP listener thread — _stop_udp_listener() joins that thread,
    which would raise "cannot join current thread". The session_end path dispatches a new
    thread for exactly this reason.
    """
    with _teardown_lock:
        if state.get("phase") != "running":
            return False           # already torn down
        rig = state["rig_config"]
        leader = get_leader_pi(rig)
        api_port = rig["network"]["api_port"]

        # Stop video recordings first (so the files are finalized). force=True: this is the
        # legitimate end-of-session stop, allowed to finalize a real session recording.
        for dev_name, _cfg, pi in _video_devices(rig):
            try:
                requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop",
                              json={"device": dev_name, "force": True}, timeout=10)
            except Exception:
                pass

        # Send STOP via UDP (graceful shutdown signal)
        _send_command(leader["ip"], rig["network"]["command_port"],
                      {"cmd": "STOP"})

        # Give leader time to finalize (write HDF5, metadata)
        time.sleep(3)

        # Stop engine processes on all Pis (SIGTERM → wait 10s → SIGKILL)
        def _kill_pi(ip):
            try:
                requests.post(f"http://{ip}:{api_port}/api/stop",
                              json={}, timeout=15)
            except Exception:
                pass

        threads = [threading.Thread(target=_kill_pi, args=(pi["ip"],))
                   for pi in rig["pis"]]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=16)

        _stop_udp_listener()
        _save_session_logs()
        _app_logger.info(f"STOP({reason}) session={state['session_id']} "
                         f"trials={len(_trials)}")
        if reason == "manual" and not _session_end_seen:
            notify(f"⏹️ {rig['name']} — stopped, but no session_end came from the leader "
                   f"({state['session_id']})")
        state["phase"] = "ended"
        return True


@app.route("/api/stop", methods=["POST"])
def stop():
    """Send STOP via UDP, wait for leader to finalize, then kill processes."""
    _teardown_session("manual")
    return jsonify({"ok": True})


@app.route("/api/browse_folder", methods=["POST"])
def browse_folder():
    """Open a native macOS folder picker (this app runs locally on the Mac) and return the
    chosen absolute path. Returns ok:False with error 'cancelled' if the dialog is dismissed."""
    import subprocess
    try:
        script = ('POSIX path of (choose folder with prompt '
                  '"Select data transfer destination")')
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return jsonify({"ok": False, "error": "cancelled"})
        return jsonify({"ok": True, "path": r.stdout.strip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


def _data_dir() -> Path:
    """Controller-local root for transferred session data + the subject database.
    Machine-specific by nature, so it does NOT come from the shared rig yaml (a
    macOS path in a git/Dropbox-shared file breaks every other controller):
    $VRFARM_DATA_DIR wins if set, else ~/VRFarm/data — the same convention on
    every OS (/Users/<u> on macOS, /home/<u> on Linux, C:\\Users\\<u> on Windows).
    The legacy data.mac_dir yaml key is intentionally ignored."""
    env = os.environ.get("VRFARM_DATA_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / "VRFarm" / "data"


def _session_dir(dest_root) -> Path:
    """Session data folder: <root>/<subject>/<subject>_<date>/<session_id> — the layout
    transfer() writes to."""
    subject_id = state["session"]["subject_id"]
    date_str = state["session"]["date"]
    return Path(dest_root) / subject_id / f"{subject_id}_{date_str}" / state["session_id"]


@app.route("/api/transfer", methods=["POST"])
def transfer():
    """Download session data from Pis to the controller."""
    req = request.get_json(silent=True) or {}
    rig = state["rig_config"]
    api_port = rig["network"]["api_port"]
    default_data_dir = _data_dir()
    # Optional per-transfer destination override (UI field / Browse picker); blank = controller default.
    dest_override = str(req.get("dest_dir", "")).strip()
    dest_root = Path(dest_override).expanduser() if dest_override else default_data_dir
    session_id = state["session_id"]
    subject_id = state["session"]["subject_id"]
    dest = _session_dir(dest_root)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify({"ok": False, "error": f"Cannot create destination {dest}: {e}"})

    # Build the final self-contained .h5 on the Leader first: merge the sidecars
    # (metadata.yaml, frame_timestamps.npy, ...) into it and delete them, so only
    # <session>.h5 + the video files remain to pull. data_dir/video_dir come from the rig
    # yaml so a non-default leader_dir still consolidates/transfers.
    steps = []
    leader = next((p for p in rig["pis"] if p.get("role") == "leader"), rig["pis"][0])
    try:
        # timeout is generous: consolidation also copy-remuxes the raw video on the leader,
        # which reads+writes the whole file (~a minute for a few GB, more for a big one).
        cj = requests.post(
            f"http://{leader['ip']}:{api_port}/api/consolidate/{session_id}",
            json={"video_dir": rig.get("data", {}).get("video_dir", ""),
                  "data_dir": rig.get("data", {}).get("leader_dir", "")},
            timeout=900).json()
        if cj.get("ok") and cj.get("skipped"):
            steps.append(f"Consolidate: {cj['skipped']}")
        elif cj.get("ok"):
            got = ', '.join(k for k, v in cj.get("merged", {}).items() if v) or "none"
            steps.append(f"Consolidated .h5 (merged {got}; removed {', '.join(cj.get('removed', [])) or 'none'})")
        else:
            steps.append(f"⚠️  Consolidate failed: {cj.get('error', '?')}")
        # Report the video remux (video.h264 -> video.mp4). The mp4 is then what the file list
        # below picks up, so the raw is never transferred.
        v = (cj or {}).get("video") or {}
        if v.get("remuxed"):
            steps.append(f"Video: remuxed -> video.mp4 @ {v.get('fps')} fps, raw .h264 deleted")
        elif v.get("error"):
            steps.append(f"⚠️  Video remux: {v['error']} (raw .h264 will transfer instead)")
        elif v.get("skipped") and v["skipped"] not in ("already mp4",):
            steps.append(f"Video remux skipped: {v['skipped']}")
    except Exception as e:
        steps.append(f"⚠️  Consolidate error (transferring raw files): {e}")

    transferred = []
    for pi in rig["pis"]:
        base = f"http://{pi['ip']}:{api_port}"
        # Never pull from a Pi that is still writing the video: the download simply ends at
        # whatever byte it has reached and the short copy replaces the good one with no error.
        # ASD109_20260814_002 lost 346 MB (4.17%) exactly this way.
        try:
            st = requests.get(f"{base}/api/status", timeout=5).json()
        except Exception as e:
            transferred.append(f"ERROR ({pi['name']}): status unreachable: {e}")
            continue
        if st.get("camera_recording"):
            transferred.append(f"ERROR ({pi['name']}): camera still recording — "
                               f"STOP the session before transferring")
            continue
        try:
            r = requests.get(
                f"{base}/api/files/{session_id}",
                params={"video_dir": rig.get("data", {}).get("video_dir", ""),
                        "data_dir": rig.get("data", {}).get("leader_dir", "")},
                timeout=5)
            r.raise_for_status()
            listing = r.json()
            files = listing.get("files", [])
            sizes = listing.get("sizes", {})   # {} from an un-redeployed Pi -> size check skipped
        except Exception as e:
            transferred.append(f"ERROR ({pi['name']}): file list failed: {e}")
            continue
        # try/except INSIDE the loop: one bad file must not abandon the rest. The old single
        # try around the whole per-Pi block is why five sessions arrived with no .h5 at all.
        for fpath in files:
            fname = Path(fpath).name
            # Preserve the path below the session dir (e.g. worldcam/video.avi vs
            # naneye/video.avi) — flattening to the basename would collide same-named
            # files from different subdirectories.
            if f"{session_id}/" in fpath:
                rel = fpath.split(f"{session_id}/", 1)[1]
            else:
                rel = fname
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            part = out.parent / (out.name + ".part")
            expect = sizes.get(fpath)
            try:
                # Video files can be large — use longer timeout. Note this is a per-read stall
                # timeout, not a deadline for the whole transfer.
                dl_timeout = 300 if fname.endswith((".h264", ".mp4", ".mkv")) else 60
                with requests.get(f"{base}/api/download/{fpath}",
                                  timeout=dl_timeout, stream=True) as r:
                    r.raise_for_status()
                    got = 0
                    with open(part, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            f.write(chunk)
                            got += len(chunk)
                if expect is not None and got != expect:
                    raise IOError(f"size mismatch: got {got:,} of {expect:,} bytes "
                                  f"({got / max(expect, 1) * 100:.2f}%)")
                # Atomic: a partial download never takes the real filename, so a failed
                # transfer leaves the previous good copy (or nothing) rather than a stub.
                os.replace(part, out)
                transferred.append(f"{out} ({got / 1e6:.1f} MB)")
            except Exception as e:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
                transferred.append(f"ERROR ({pi['name']}) {fname}: {e}")

    # ── Mirror the latest shepherd health log to the controller, then prune the leader ──
    # shepherd runs continuously on the leader and writes to ~/shepherd_logs (NOT the .h5). Copy the
    # CURRENT (newest) jsonl+alerts into <data_dir>/shepherd_logs/, overwriting the same timestamped
    # names so a still-growing file just refreshes; then delete leader logs older than 7 days (they
    # accumulate on the controller side, so the Pi copy is disposable).
    try:
        sh_base = f"http://{leader['ip']}:{api_port}"
        info = requests.get(f"{sh_base}/api/shepherd_logs/latest", timeout=10).json()
        sh_files = info.get("files", []) if info.get("ok") else []
        if sh_files:
            sh_dir = dest_root / "shepherd_logs"
            sh_dir.mkdir(parents=True, exist_ok=True)
            for fpath in sh_files:
                out = sh_dir / Path(fpath).name
                part = out.parent / (out.name + ".part")
                expect = info.get("sizes", {}).get(fpath)
                try:
                    with requests.get(f"{sh_base}/api/download/{fpath}",
                                      timeout=60, stream=True) as r:
                        r.raise_for_status()
                        got = 0
                        with open(part, "wb") as f:
                            for chunk in r.iter_content(1 << 16):
                                f.write(chunk)
                                got += len(chunk)
                    if expect is not None and got != expect:
                        raise IOError(f"size mismatch: {got:,}/{expect:,}")
                    os.replace(part, out)
                    steps.append(f"Shepherd log → {out.name} ({got / 1e6:.2f} MB)")
                except Exception as e:
                    try:
                        part.unlink(missing_ok=True)
                    except OSError:
                        pass
                    steps.append(f"⚠️  Shepherd log {Path(fpath).name}: {e}")
        else:
            steps.append("Shepherd log: none found on leader")
        cj = requests.post(f"{sh_base}/api/shepherd_logs/cleanup",
                           json={"days": 7}, timeout=15).json()
        if cj.get("ok") and cj.get("deleted"):
            steps.append(f"Shepherd cleanup: removed {len(cj['deleted'])} log(s) >7d on leader")
    except Exception as e:
        steps.append(f"⚠️  Shepherd log mirror/cleanup skipped: {e}")

    # Register in subject database — always at the controller-default location so the index
    # stays consistent even when session files are sent to an override destination.
    db_dir = default_data_dir / "subjects"
    register_session(
        subject_id=subject_id,
        session_id=session_id,
        session_num=int(state["session"]["session_num"]),
        date_str=state["session"]["date"],
        task_config=state["task_config"],
        db_dir=db_dir,
        n_trials_completed=len(_trials),
        notes=state["session"].get("notes", ""),
    )

    _app_logger.info(f"TRANSFER session={session_id} files={len(transferred)}")
    state["phase"] = "setup"
    return jsonify({"ok": True, "files": steps + transferred})


@app.route("/api/state")
def get_state():
    """Current app state for UI polling."""
    return jsonify({
        "phase": state["phase"],
        "deployed": state["deployed"],
        "session_id": state["session_id"],
        "n_trials": len(_trials),
    })


@app.route("/api/trials")
def get_trials():
    """Return trial data for the live table."""
    return jsonify(_trials)


@app.route("/api/subject_history/<subject_id>")
def subject_history(subject_id):
    rig = state["rig_config"]
    if not rig:
        return jsonify([])
    db_dir = _data_dir() / "subjects"
    return jsonify(get_subject_history(subject_id, db_dir))


def _find_video_device(rig, name):
    """(name, cfg, pi) for a named video device, or the first one when name is falsy."""
    devs = _video_devices(rig)
    if not name:
        return devs[0] if devs else None
    return next((d for d in devs if d[0] == name), None)


@app.route("/api/camera_start", methods=["POST"])
def camera_start():
    """Start a video device's preview (Pi-side guard prevents restart if already recording).
    Body: {device} — defaults to the rig's first video device."""
    rig = state["rig_config"]
    if not rig:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400
    data = request.get_json(silent=True) or {}
    found = _find_video_device(rig, data.get("device"))
    if not found:
        return jsonify({"ok": False, "error": "No video device assigned"}), 400
    dev_name, cfg, pi = found
    api_port = rig["network"]["api_port"]
    try:
        r = requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_start",
                          json={"device": dev_name,
                                "type": cfg.get("type", dev_name),
                                "config": cfg,
                                "downsample": True},
                          timeout=15)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/camera_stop", methods=["POST"])
def camera_stop():
    """Stop a video device's preview (skipped while a session is recording)."""
    rig = state["rig_config"]
    if not rig:
        return jsonify({"ok": False}), 400
    # During a running session, don't stop the session recording
    if state["phase"] == "running":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    api_port = rig["network"]["api_port"]
    for dev_name, _cfg, pi in _video_devices(rig):
        if data.get("device") and dev_name != data["device"]:
            continue
        try:
            requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop",
                          json={"device": dev_name}, timeout=5)
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/camera_feed")
def camera_feed():
    """Proxy MJPEG stream from a Pi video device (?device=<name>)."""
    rig = state["rig_config"]
    if not rig:
        return "No rig", 400
    found = _find_video_device(rig, request.args.get("device"))
    if not found:
        return "No camera", 400
    dev_name, _cfg, pi = found
    api_port = rig["network"]["api_port"]
    # Reconnecting relay: rides through pi_api restarts (Deploy), the preview->record swap (Go), and the
    # <img>-before-preview race instead of collapsing to a silent 200-blank. See shared/mjpeg_relay.py.
    from shared.mjpeg_relay import relay
    return Response(relay(f"http://{pi['ip']}:{api_port}/api/camera_stream?device={dev_name}"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/quit", methods=["POST"])
def quit_app():
    """Shut down the Flask server."""
    func = request.environ.get("werkzeug.server.shutdown")
    if func:
        func()
    else:
        os._exit(0)
    return jsonify({"ok": True})


@app.route("/api/events")
def event_stream():
    """SSE endpoint for live events from Leader."""
    def generate():
        while True:
            try:
                event = _event_queue.get(timeout=5)
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ── UDP communication ──

def _disable_udp_conn_reset(sock):
    """Windows only: stop a prior ICMP 'port unreachable' (e.g. a command sent while the
    Leader engine is down) from poisoning this UDP socket so the next recv/send raises
    ConnectionResetError (WSAECONNRESET). macOS/Linux ignore the ICMP error, so this is a
    no-op there and the whole thing is wrapped defensively."""
    if sys.platform != "win32":
        return
    try:
        sock.ioctl(socket.SIO_UDP_CONNRESET, struct.pack("I", 0))
    except (AttributeError, OSError):
        pass


def _send_command(ip: str, port: int, msg: dict):
    """Send a UDP command to Leader. Tolerant of a Windows ConnectionResetError left by a
    previous packet to a closed port: rebuild the socket and retry once (UDP is best-effort
    anyway), so one dropped command can't 500 the operator or wedge the command socket."""
    global _cmd_sock
    if _cmd_sock is None:
        _cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _disable_udp_conn_reset(_cmd_sock)
    payload = json.dumps(msg).encode()
    try:
        _cmd_sock.sendto(payload, (ip, port))
    except ConnectionResetError:
        try:
            _cmd_sock.close()
        except OSError:
            pass
        _cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _disable_udp_conn_reset(_cmd_sock)
        _cmd_sock.sendto(payload, (ip, port))


def _fmt_hm(ms) -> str:
    """'1 hour, 15 minutes' from a duration in ms (for the session-start ETA)."""
    total_min = int(round((ms or 0) / 60000))
    h, m = divmod(total_min, 60)
    return f"{h} hour{'' if h == 1 else 's'}, {m} minute{'' if m == 1 else 's'}"


def _start_udp_listener(event_port: int):
    """Start background thread listening for UDP events from Leader."""
    global _udp_thread, _udp_running
    if _udp_running:
        return

    _udp_running = True

    def listen():
        global _session_end_seen
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", event_port))
        _disable_udp_conn_reset(sock)
        sock.settimeout(1.0)
        while _udp_running:
            try:
                data, _ = sock.recvfrom(4096)
                try:
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        continue
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue   # a malformed datagram must NOT kill the listener —
                               # session_end teardown depends on this thread staying alive
                rig_name = state["rig_config"]["name"]
                # Track trials
                if event.get("type") == "trial":
                    _trials.append(event)
                elif event.get("type") == "shepherd_alert":
                    # Health alert from the shepherd monitor on a rig Pi. It flows to
                    # the SSE queue (below) for the UI log like any event; a CRITICAL
                    # also goes to Slack so it reaches you when the browser is closed.
                    if event.get("level") == "critical":
                        notify(f"🔴 {event.get('host', rig_name)} — {event.get('message', 'critical alert')}")
                elif event.get("type") == "session_end":
                    _session_end_seen = True
                    notify(f"✅ {rig_name} — session ended: {event.get('n_completed', '?')}"
                           f"/{event.get('n_planned', '?')} trials ({state['session_id']})")
                    # A natural end MUST tear down server-side. Nothing else does: the browser
                    # sets only ITS OWN phase to 'ended' (which then disables the STOP button),
                    # while the server stays 'running' so /api/camera_stop returns early — and
                    # the leader keeps recording. That is how ASD110_20260814_002 ran for 15.4 h
                    # and produced a 138 GB video, stopping only when the disk filled. It also
                    # means a closed browser leaves the camera running indefinitely.
                    # Separate thread: _teardown_session joins THIS one.
                    threading.Thread(target=_teardown_session,
                                     args=("session_end",), daemon=True).start()
                # Push to SSE queue
                try:
                    _event_queue.put_nowait(event)
                except queue.Full:
                    try:
                        _event_queue.get_nowait()
                    except queue.Empty:
                        pass
                    print("[UDP] Queue full, dropped oldest event")
                    _event_queue.put_nowait(event)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows: a stray ICMP unreachable poisoned the socket — keep listening.
                continue
            except OSError:
                break
        sock.close()

    _udp_thread = threading.Thread(target=listen, daemon=True)
    _udp_thread.start()


def _stop_udp_listener():
    """Stop the UDP listener thread and drain the event queue."""
    global _udp_running, _udp_thread
    _udp_running = False
    if _udp_thread is not None:
        _udp_thread.join(timeout=3)
        _udp_thread = None
    while not _event_queue.empty():
        try:
            _event_queue.get_nowait()
        except queue.Empty:
            break


# ── Helpers ──


def _upload_file(ip: str, port: int, local_path: str, remote_path: str):
    """Upload a file to Pi via REST API. Raises on failure — a silently failed upload
    would leave STALE code running on the Pi while Deploy reports success."""
    with open(local_path, "rb") as f:
        r = requests.post(f"http://{ip}:{port}/api/upload",
                          files={"file": f},
                          data={"path": remote_path},
                          timeout=10)
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"upload {remote_path} to {ip} failed: {j.get('error', '?')}")




# ── Main ──

def main():
    import argparse
    import webbrowser
    parser = argparse.ArgumentParser(description="VRFarm Experiment UI")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://localhost:{args.port}"
    if not args.no_browser and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    app.run(host="0.0.0.0", port=args.port, debug=args.debug,
            threaded=True)


if __name__ == "__main__":
    main()
