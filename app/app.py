"""
app/app.py

Mac-side Flask UI for running experiments.
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
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

import requests
import yaml
from flask import Flask, Response, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.config import (load_rig, load_task, save_task,
                           get_leader_pi, get_follower_pis,
                           make_session_id, register_session,
                           get_subject_history, next_session_num)

app = Flask(__name__)

# ── Global state ──

state = {
    "phase": "setup",       # setup, connected, deployed, running, ended
    "rig_config": None,
    "task_config": None,
    "rig_path": None,
    "task_path": None,
    "session": {},          # subject_id, date, session_num, level, notes
    "session_id": None,
    "deployed": False,
}

# Event queue for SSE
_event_queue = queue.Queue(maxsize=2000)

# UDP listener thread
_udp_thread = None
_udp_running = False
_cmd_sock = None

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

        # Initialize devices assigned to this Pi
        pi_devs = pi.get("devices", [])
        init_errors = []
        for dev_name in pi_devs:
            dev_cfg = devices.get(dev_name, {})
            if not dev_cfg.get("enabled", True):
                continue
            try:
                if dev_name == "display":
                    # Follower: shutdown old display, init projector hardware
                    requests.post(f"http://{ip}:{api_port}/api/shutdown_display",
                                  json={}, timeout=5)
                    r = requests.post(f"http://{ip}:{api_port}/api/init_projector",
                                      json={}, timeout=35)
                    if not r.json().get("ok"):
                        err = r.json().get("error",
                              "Projector failed to initialize — is it powered on?")
                        init_errors.append(err)
                elif dev_name == "lick_sensor":
                    r = requests.post(f"http://{ip}:{api_port}/api/init_lick", json={
                        "i2c_address": dev_cfg.get("i2c_address", "0x5A"),
                        "electrode": dev_cfg.get("electrode", 4),
                    }, timeout=10)
                    if not r.json().get("ok"):
                        err = r.json().get("error",
                              "Lick sensor failed — check I2C connection")
                        init_errors.append(err)
                elif dev_name == "reward":
                    r = requests.post(f"http://{ip}:{api_port}/api/init_reward", json={
                        "pins": dev_cfg.get("pins", {"main": {"gpio": 18}}),
                    }, timeout=10)
                    if not r.json().get("ok"):
                        err = r.json().get("error",
                              "Reward valve failed — is pigpiod running?")
                        init_errors.append(err)
                elif dev_name == "camera":
                    r = requests.post(f"http://{ip}:{api_port}/api/init_camera",
                                      json={}, timeout=10)
                    if not r.json().get("ok"):
                        err = r.json().get("error",
                              "Camera not detected — check CSI cable")
                        init_errors.append(err)
                elif dev_name == "photodiode":
                    r = requests.post(f"http://{ip}:{api_port}/api/init_photodiode", json={
                        "gpio": dev_cfg.get("gpio", 24),
                    }, timeout=10)
                    if not r.json().get("ok"):
                        err = r.json().get("error",
                              "Photodiode failed — is pigpiod running?")
                        init_errors.append(err)
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

    # Save grace period to rig config (persists across sessions)
    grace = data.pop("grace_period_s", None)
    if grace is not None and state["rig_config"]:
        state["rig_config"]["grace_period_s"] = int(grace)
        if state.get("rig_path"):
            with open(state["rig_path"], "w") as f:
                yaml.dump(state["rig_config"], f, default_flow_style=False)

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


@app.route("/api/connect", methods=["POST"])
def connect():
    """Check Pi REST APIs are online."""
    rig = state["rig_config"]
    if not rig:
        return jsonify({"error": "No rig config loaded"}), 400

    api_port = rig["network"]["api_port"]
    results = {}

    for pi in rig["pis"]:
        name = pi["name"]
        ip = pi["ip"]
        try:
            r = requests.get(f"http://{ip}:{api_port}/api/status", timeout=3)
            results[name] = {"ok": True, "status": r.json()}
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)}

    all_ok = all(r["ok"] for r in results.values())
    if all_ok:
        state["phase"] = "connected"

    return jsonify({"results": results, "all_ok": all_ok})


@app.route("/api/deploy", methods=["POST"])
def deploy():
    """Deploy configs and generate stims. Enables Go button on success."""
    rig = state["rig_config"]
    task = state["task_config"]
    if not rig or not task:
        return jsonify({"error": "Config not loaded"}), 400
    if not state["session_id"]:
        return jsonify({"error": "Session not configured"}), 400

    api_port = rig["network"]["api_port"]
    leader = get_leader_pi(rig)
    followers = get_follower_pis(rig)
    steps = []

    try:
        # 0. Upload code to Pis
        code_files = {
            "leader": [
                "engine/leader.py",
                "engine/state_machine.py",
                "shared/stim_generator.py",
                "shared/config.py",
                "devices/base.py",
                "devices/lick_sensor.py",
                "devices/reward.py",
                "devices/camera.py",
                "devices/photodiode.py",
                "devices/reward_calibration.py",
                "pi_api/api.py",
            ],
            "follower": [
                "engine/follower.py",
                "devices/base.py",
                "devices/display.py",
                "pi_api/api.py",
            ],
        }
        for pi in rig["pis"]:
            role = pi["role"]
            for rel_path in code_files.get(role, []):
                local = ROOT / rel_path
                if local.exists():
                    _upload_file(pi["ip"], api_port, str(local), rel_path)
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

        # 0c. Re-initialize projector on followers (restart killed Xorg)
        for fpi in followers:
            display_cfg = rig.get("devices", {}).get("display", {})
            if display_cfg.get("enabled", True) and "display" in fpi.get("devices", []):
                try:
                    r = requests.post(
                        f"http://{fpi['ip']}:{api_port}/api/init_projector",
                        json={}, timeout=35)
                    if r.json().get("ok"):
                        steps.append(f"Re-initialized projector on {fpi['name']}")
                    else:
                        steps.append(f"Projector re-init warning on {fpi['name']}: {r.json().get('error', '?')}")
                except Exception as e:
                    steps.append(f"Projector re-init failed on {fpi['name']}: {e}")

        # 1. Upload rig config to all Pis
        for pi in rig["pis"]:
            _upload_file(pi["ip"], api_port, state["rig_path"],
                         f"rigs/{Path(state['rig_path']).name}")
        steps.append("Uploaded rig config to all Pis")

        # 2. Upload task config to Leader
        _upload_file(leader["ip"], api_port, state["task_path"],
                     f"experiments/{Path(state['task_path']).name}")
        steps.append("Uploaded task config to Leader")

        # 2. Generate stims on Leader
        remote_task_path = f"experiments/{Path(state['task_path']).name}"
        r = requests.post(
            f"http://{leader['ip']}:{api_port}/api/generate_stims",
            json={"task_config": remote_task_path,
                  "session_id": state["session_id"],
                  "apply_warp": rig.get("devices", {}).get("display", {}).get("apply_warp", False),
                  "contrast_metric": rig.get("devices", {}).get("display", {}).get("contrast_metric", "weber")},
            timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Stim generation failed (HTTP {r.status_code}): {r.text[:500]}")
        try:
            stim_result = r.json()
        except Exception:
            raise RuntimeError(f"Stim generation returned invalid response: {r.text[:500]}")
        if not stim_result.get("ok"):
            raise RuntimeError(f"Stim generation error: {stim_result.get('error', 'unknown')}")
        steps.append(f"Generated stims: {stim_result.get('n_trials', '?')} trials")

        # 3. Download stim NPZ from Leader, then upload to Follower(s)
        npz_remote = stim_result.get("npz_path", "")
        if npz_remote:
            # Download NPZ to Mac temp
            import tempfile
            r = requests.get(
                f"http://{leader['ip']}:{api_port}/api/download/{npz_remote}",
                timeout=15, stream=True)
            tmp_npz = Path(tempfile.mktemp(suffix=".npz"))
            with open(tmp_npz, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)

            for fpi in followers:
                try:
                    _upload_file(fpi["ip"], api_port, str(tmp_npz),
                                 f"stims/{state['session_id']}/stimuli.npz")
                    steps.append(f"Pushed stims to {fpi['name']}")
                except Exception as e:
                    steps.append(f"Push stims to {fpi['name']} FAILED: {e}")
            tmp_npz.unlink(missing_ok=True)

        # 4. Download universal trial table (YAML) from Leader
        if npz_remote:
            yaml_remote = npz_remote.replace("stimuli.npz", "trials.yaml")
            try:
                r = requests.get(
                    f"http://{leader['ip']}:{api_port}/api/download/{yaml_remote}",
                    timeout=10)
                r.raise_for_status()
                table = yaml.safe_load(r.content)
                if not isinstance(table, list):
                    raise ValueError(f"Expected list, got {type(table).__name__}")
                state["trial_table"] = table
                steps.append(f"Trial table: {len(table)} trials")
            except Exception as e:
                print(f"Trial table download failed: {e}")
                state["trial_table"] = []

        state["deployed"] = True
        state["phase"] = "deployed"
        _app_logger.info(f"DEPLOY ok session={state['session_id']} steps={len(steps)}")
        return jsonify({"ok": True, "steps": steps})

    except Exception as e:
        _app_logger.error(f"DEPLOY failed session={state.get('session_id')} error={e}")
        return jsonify({"ok": False, "error": str(e), "steps": steps})


_cwm_mod = None


def _cwm():
    """Lazily import display_calibration/compute_warp_map.py (numpy/scipy geometry)."""
    global _cwm_mod
    if _cwm_mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "compute_warp_map", str(ROOT / "display_calibration" / "compute_warp_map.py"))
        _cwm_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_cwm_mod)
    return _cwm_mod


@app.route("/api/correct_contrast", methods=["POST"])
def correct_contrast():
    """Clamp entered contrast value(s) to the achievable ceiling in the active metric.

    When apply_warp is on and the warp map exists, the ceiling is the UNIFORM value achievable across
    the session's azimuths, using the SAME per-azimuth luminance curve generation bakes in
    (get_luminance_correction on warp_map.npz — empirical if the warp carries it, else theoretical).
    When apply_warp is off (or no warp), generation applies no per-azimuth correction, so the ceiling
    is the plain display ceiling at the background. Values in and out are in the active metric, 0..1.
    """
    import numpy as np
    from shared.stim_generator import (get_luminance_correction, fraction_to_metric,
                                       snap_contrast_to_bitcode)

    data = request.get_json(silent=True) or {}
    try:
        values = [float(v) for v in (data.get("values") or [])]
        bg = float(data.get("background_gray") or 0.0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid values or background_gray"}), 400
    block_seq = data.get("block_sequence") or [0.0]
    rig = state.get("rig_config") or {}
    display_cfg = rig.get("devices", {}).get("display", {})
    metric = display_cfg.get("contrast_metric", "weber")
    apply_warp = display_cfg.get("apply_warp", False)

    # Match generation exactly: the per-azimuth luminance boost is applied ONLY when apply_warp is on
    # AND a warp map exists (else generation uses corr_contrast = min(f, 1.0), no boost). Use the same
    # warp + get_luminance_correction (empirical-preferring) so the reported ceiling can't drift from
    # what generation bakes in.
    max_lum = 1.0
    note = ("apply_warp off; global display ceiling" if not apply_warp
            else "no warp map; global display ceiling")
    warp_path = ROOT / "display_calibration" / "warp_map.npz"
    if apply_warp and warp_path.exists():
        try:
            warp = np.load(str(warp_path))
            azs = set()
            for az in block_seq:
                try:
                    azs.add(abs(float(az)))
                except (TypeError, ValueError):
                    pass
            azs = azs or {0.0}
            max_lum = max(get_luminance_correction(warp, az) for az in azs)
            note = ("per-azimuth luminance (empirical)" if "lum_az_empirical" in warp.files
                    else "per-azimuth luminance (theoretical)")
        except Exception as e:
            note = f"luminance unavailable ({e}); global display ceiling"
            max_lum = 1.0

    f_ceiling = 1.0 / max_lum if max_lum > 0 else 1.0
    # Contrast input is 0..1: cap at 1.0 so a metric ceiling above 1 (e.g. Weber at a dim bg) just
    # means the whole 0..1 range is usable, and the global/no-calibration case still clamps within
    # 0..1 rather than being a no-op.
    c_ceiling = min(fraction_to_metric(f_ceiling, bg, metric), 1.0)
    # Clamp to the ceiling, then snap DOWN to the nearest achievable 8-bit code (perceived contrast
    # can't be finer than one code; floor => never exceed the ceiling).
    corrected = [round(snap_contrast_to_bitcode(min(v, c_ceiling), bg, metric), 4) for v in values]
    c_ceiling = snap_contrast_to_bitcode(c_ceiling, bg, metric)
    return jsonify({"ok": True, "corrected": corrected,
                    "ceiling": round(float(c_ceiling), 4), "metric": metric, "note": note})


@app.route("/api/trial_table")
def get_trial_table():
    """Return pre-computed trial table."""
    return jsonify({"trials": state.get("trial_table", [])})


@app.route("/api/drain_events", methods=["POST"])
def drain_events():
    """Drain buffered SSE events so Live starts fresh."""
    count = 0
    while not _event_queue.empty():
        try:
            _event_queue.get_nowait()
            count += 1
        except queue.Empty:
            break
    return jsonify({"ok": True, "drained": count})


@app.route("/api/go", methods=["POST"])
def go():
    """Start engine processes on Pis, then send UDP START to Leader."""
    if not state["deployed"]:
        return jsonify({"error": "Not deployed"}), 400

    rig = state["rig_config"]
    leader = get_leader_pi(rig)
    followers = get_follower_pis(rig)
    api_port = rig["network"]["api_port"]
    cmd_port = rig["network"]["command_port"]
    session = state["session"]
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

    # Ensure projector + X11 running on followers, then release pi_api's pygame
    for fpi in followers:
        try:
            requests.post(f"http://{fpi['ip']}:{api_port}/api/shutdown_display",
                          json={}, timeout=3)
        except Exception:
            pass
        try:
            requests.post(f"http://{fpi['ip']}:{api_port}/api/init_projector",
                          json={}, timeout=35)
        except Exception:
            pass
    print("[go] Projector initialized, display released")

    # Start follower engine on each follower Pi
    print("[go] Starting followers...")
    for fpi in followers:
        stim_path = f"/home/vruser/rig/stims/{state['session_id']}/stimuli.npz"
        try:
            r = requests.post(
                f"http://{fpi['ip']}:{api_port}/api/start",
                json={
                    "script": "follower",
                    "args": [
                        "--rig", f"/home/vruser/rig/rigs/{rig_filename}",
                        "--stims", stim_path,
                    ]
                }, timeout=10)
            res = r.json()
            if res.get("ok"):
                steps.append(f"Follower started on {fpi['name']} (pid {res.get('pid', '?')})")
            else:
                return jsonify({"ok": False, "error": f"Follower on {fpi['name']}: {res.get('error', 'unknown')}", "steps": steps})
        except Exception as e:
            return jsonify({"ok": False, "error": f"Failed to start follower on {fpi['name']}: {e}", "steps": steps})

    # Start leader engine
    print("[go] Starting leader...")
    try:
        r = requests.post(
            f"http://{leader['ip']}:{api_port}/api/start",
            json={
                "script": "leader",
                "args": [
                    "--rig", f"/home/vruser/rig/rigs/{rig_filename}",
                    "--task", f"/home/vruser/rig/experiments/{task_filename}",
                    "--subject", session["subject_id"],
                    "--date", session["date"],
                    "--session-num", str(int(session["session_num"])),
                    "--notes", session.get("notes", ""),
                ]
            }, timeout=10)
        res = r.json()
        if res.get("ok"):
            steps.append(f"Leader started on {leader['name']} (pid {res.get('pid', '?')})")
        else:
            return jsonify({"ok": False, "error": f"Leader: {res.get('error', 'unknown')}", "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to start leader: {e}", "steps": steps})

    # Start camera recording for session (if camera enabled)
    cam_cfg = rig.get("devices", {}).get("camera", {})
    cam_recording = False
    if cam_cfg.get("enabled", False):
        video_dir = rig["data"].get("video_dir", "/media/vruser/ssd/video")
        for pi in rig["pis"]:
            if "camera" in pi.get("devices", []):
                try:
                    # Stop any existing preview
                    requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop",
                                  json={}, timeout=5)
                    # Start session recording at full resolution
                    r = requests.post(
                        f"http://{pi['ip']}:{api_port}/api/camera_preview_start",
                        json={
                            "session_id": state["session_id"],
                            "video_dir": video_dir,
                            "resolution": cam_cfg.get("resolution", [1280, 720]),
                            "fps": cam_cfg.get("fps", 50),
                            "bitrate_mbps": cam_cfg.get("bitrate_mbps", 8),
                            "auto_exposure": cam_cfg.get("auto_exposure", True),
                            "exposure_us": cam_cfg.get("exposure_us", 10000),
                            "gain": cam_cfg.get("gain", 1.0),
                            "live_preset": cam_cfg.get("live_preset", "med"),
                        }, timeout=10)
                    # A failed record start (e.g. HTTP 500 when video_dir is unwritable /
                    # the SSD isn't mounted) may not return JSON — parse defensively.
                    ok, err = False, f"HTTP {r.status_code}"
                    try:
                        j = r.json()
                        ok, err = bool(j.get("ok")), j.get("error", err)
                    except Exception:
                        pass
                    if ok:
                        cam_recording = True
                        steps.append(f"Camera recording on {pi['name']}")
                    else:
                        warn = (f"⚠️  CAMERA NOT RECORDING on {pi['name']}: {err} — "
                                f"session runs WITHOUT video. Check the SSD / "
                                f"video_dir ({video_dir}).")
                        steps.append(warn)
                        _app_logger.warning(warn)
                except Exception as e:
                    warn = (f"⚠️  CAMERA NOT RECORDING on {pi['name']}: {e} — "
                            f"session runs WITHOUT video. Check the SSD / video_dir.")
                    steps.append(warn)
                    _app_logger.warning(warn)
                break
        print(f"[go] Camera recording {'started' if cam_recording else 'FAILED — no video'}")
    else:
        print("[go] Camera not enabled — skipping recording")

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
    _app_logger.info(f"GO session={state['session_id']}")
    return jsonify({"ok": True, "steps": steps})


@app.route("/api/stop", methods=["POST"])
def stop():
    """Send STOP via UDP, wait for leader to finalize, then kill processes."""
    rig = state["rig_config"]
    leader = get_leader_pi(rig)
    api_port = rig["network"]["api_port"]

    # Stop camera recording first (so video file is finalized). force=True: this is the
    # legitimate end-of-session stop, allowed to finalize a real session recording.
    for pi in rig["pis"]:
        if "camera" in pi.get("devices", []):
            try:
                requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop",
                              json={"force": True}, timeout=10)
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
    _app_logger.info(f"STOP session={state['session_id']} trials={len(_trials)}")
    state["phase"] = "ended"
    return jsonify({"ok": True})


@app.route("/api/reward", methods=["POST"])
def manual_reward():
    """Send manual reward command."""
    rig = state["rig_config"]
    leader = get_leader_pi(rig)
    _send_command(leader["ip"], rig["network"]["command_port"],
                  {"cmd": "REWARD"})
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


@app.route("/api/transfer", methods=["POST"])
def transfer():
    """Download session data from Pis to Mac."""
    req = request.get_json(silent=True) or {}
    rig = state["rig_config"]
    api_port = rig["network"]["api_port"]
    default_mac_dir = Path(rig["data"]["mac_dir"])
    # Optional per-transfer destination override (UI field / Browse picker); blank = rig default.
    dest_override = str(req.get("dest_dir", "")).strip()
    mac_dir = Path(dest_override).expanduser() if dest_override else default_mac_dir
    session_id = state["session_id"]
    subject_id = state["session"]["subject_id"]
    date_str = state["session"]["date"]
    subject_date = f"{subject_id}_{date_str}"
    dest = mac_dir / subject_id / subject_date / session_id
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify({"ok": False, "error": f"Cannot create destination {dest}: {e}"})

    transferred = []
    for pi in rig["pis"]:
        try:
            r = requests.get(
                f"http://{pi['ip']}:{api_port}/api/files/{session_id}",
                params={"video_dir": rig.get("data", {}).get("video_dir", "")},
                timeout=5)
            files = r.json().get("files", [])
            for fpath in files:
                fname = Path(fpath).name
                # Video files can be large — use longer timeout
                dl_timeout = 300 if fname.endswith(".h264") else 60
                r = requests.get(
                    f"http://{pi['ip']}:{api_port}/api/download/{fpath}",
                    timeout=dl_timeout, stream=True)
                out = dest / fname
                with open(out, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                size_mb = out.stat().st_size / 1e6
                transferred.append(f"{out} ({size_mb:.1f} MB)")
        except Exception as e:
            transferred.append(f"ERROR ({pi['name']}): {e}")

    # Register in subject database — always at the rig-default location so the index stays
    # consistent even when session files are sent to an override destination.
    db_dir = default_mac_dir / "subjects"
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
    return jsonify({"ok": True, "files": transferred})


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
    db_dir = Path(rig["data"]["mac_dir"]) / "subjects"
    return jsonify(get_subject_history(subject_id, db_dir))


@app.route("/api/engine_logs")
def engine_logs():
    """Fetch engine process logs from all Pis."""
    rig = state["rig_config"]
    if not rig:
        return jsonify({})
    api_port = rig["network"]["api_port"]
    n = request.args.get("n", 50, type=int)
    result = {}
    for pi in rig["pis"]:
        try:
            r = requests.get(f"http://{pi['ip']}:{api_port}/api/logs?n={n}",
                             timeout=3)
            result[pi["name"]] = r.json().get("lines", [])
        except Exception as e:
            result[pi["name"]] = [f"Error fetching logs: {e}"]
    return jsonify(result)


@app.route("/api/camera_start", methods=["POST"])
def camera_start():
    """Start camera preview on leader Pi (Pi-side guard prevents restart if already recording)."""
    rig = state["rig_config"]
    if not rig:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400
    api_port = rig["network"]["api_port"]
    cam_cfg = rig.get("devices", {}).get("camera", {})
    ip = None
    for pi in rig["pis"]:
        if "camera" in pi.get("devices", []):
            ip = pi["ip"]
            break
    if not ip:
        return jsonify({"ok": False, "error": "Camera not assigned"}), 400
    try:
        # Experiment preview: preview-only (no recording), downsampled to the live preset so the view
        # is light during the run. The Pi derives the preset res/fps from the full recording
        # resolution; exposure/gain carry over so the preview matches what will be recorded.
        r = requests.post(f"http://{ip}:{api_port}/api/camera_preview_start",
                          json={"resolution": cam_cfg.get("resolution", [1280, 720]),
                                "fps": cam_cfg.get("fps", 50),
                                "downsample": True,
                                "live_preset": cam_cfg.get("live_preset", "med"),
                                "auto_exposure": cam_cfg.get("auto_exposure", True),
                                "exposure_us": cam_cfg.get("exposure_us", 10000),
                                "gain": cam_cfg.get("gain", 1.0)},
                          timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/camera_stop", methods=["POST"])
def camera_stop():
    """Stop camera preview on leader Pi (skipped if session is recording)."""
    rig = state["rig_config"]
    if not rig:
        return jsonify({"ok": False}), 400
    # During a running session, don't stop the session camera
    if state["phase"] == "running":
        return jsonify({"ok": True})
    api_port = rig["network"]["api_port"]
    for pi in rig["pis"]:
        if "camera" in pi.get("devices", []):
            try:
                requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop",
                              json={}, timeout=5)
            except Exception:
                pass
    return jsonify({"ok": True})


@app.route("/api/camera_feed")
def camera_feed():
    """Proxy MJPEG stream from Pi camera."""
    rig = state["rig_config"]
    if not rig:
        return "No rig", 400
    api_port = rig["network"]["api_port"]
    ip = None
    for pi in rig["pis"]:
        if "camera" in pi.get("devices", []):
            ip = pi["ip"]
            break
    if not ip:
        return "No camera", 400
    def generate():
        try:
            r = requests.get(f"http://{ip}:{api_port}/api/camera_stream",
                             stream=True, timeout=5)
            for chunk in r.iter_content(chunk_size=4096):
                yield chunk
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                Exception):
            return  # stream ended — silently close

    return Response(generate(),
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

def _send_command(ip: str, port: int, msg: dict):
    """Send a UDP command to Leader."""
    global _cmd_sock
    if _cmd_sock is None:
        _cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _cmd_sock.sendto(json.dumps(msg).encode(), (ip, port))


def _start_udp_listener(event_port: int):
    """Start background thread listening for UDP events from Leader."""
    global _udp_thread, _udp_running
    if _udp_running:
        return

    _udp_running = True

    def listen():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", event_port))
        sock.settimeout(1.0)
        while _udp_running:
            try:
                data, _ = sock.recvfrom(4096)
                event = json.loads(data)
                # Track trials
                if event.get("type") == "trial":
                    _trials.append(event)
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
    """Upload a file to Pi via REST API."""
    with open(local_path, "rb") as f:
        requests.post(f"http://{ip}:{port}/api/upload",
                      files={"file": f},
                      data={"path": remote_path},
                      timeout=10)




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
