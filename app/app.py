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
import struct
import sys
import tempfile
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
from shared.notify import notify, configure as _notify_configure

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
    "session_dir": None,    # controller-side data folder for this session (created at Go)
    "deployed": False,
    "camera_override": {},  # runtime-only exposure/gain tweaks from the experiment UI (not saved to yaml)
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
_rt_hits_path = None   # temp file (controller) of hit-trial response times (ms), one per line

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
    state["camera_override"] = {}   # drop runtime exposure tweaks -> revert to the rig yaml

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
                              "Reward valve failed — check GPIO pins/wiring")
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
                        "glitch_enabled": dev_cfg.get("glitch_enabled", True),
                        "glitch_ms": dev_cfg.get("glitch_ms", 0.5),
                        "debounce_enabled": dev_cfg.get("debounce_enabled", True),
                        "debounce_ms": dev_cfg.get("debounce_ms", 5),
                    }, timeout=10)
                    if not r.json().get("ok"):
                        err = r.json().get("error",
                              "Photodiode failed — check the GPIO pin/wiring")
                        init_errors.append(err)
                elif dev_name == "encoder":
                    r = requests.post(f"http://{ip}:{api_port}/api/init_encoder", json={
                        "i2c_address": dev_cfg.get("i2c_address", "0x36"),
                        "i2c_bus": dev_cfg.get("i2c_bus", 1),
                        "wheel_diameter_cm": dev_cfg.get("wheel_diameter_cm", 15.0),
                        "sample_hz": dev_cfg.get("sample_hz", 100),
                    }, timeout=10)
                    if not r.json().get("ok"):
                        err = r.json().get("error",
                              "Encoder failed — check I2C 0x36 / magnet")
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
                "shared/stim_generator.py",
                "shared/config.py",
                "shared/consolidate.py",
                "devices/base.py",
                "devices/lick_sensor.py",
                "devices/reward.py",
                "devices/camera.py",
                "devices/photodiode.py",
                "devices/encoder.py",
                "devices/calibration_probe.py",
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

    # Per-device "save data" choices from the Actions row (default: save everything). Camera unchecked
    # -> livestream only (no video file); a behavioral device unchecked -> Leader skips its HDF5 datasets.
    _body = request.get_json(silent=True) or {}
    save = _body.get("save", {})
    # Create the session data folder NOW (so the live plots can be saved into it at end/stop,
    # regardless of whether the data is later transferred). Uses the transfer destination if the
    # UI field is set, else the controller default — the same layout transfer() writes to.
    dest_override = str(_body.get("dest_dir", "")).strip()
    try:
        dest_root = Path(dest_override).expanduser() if dest_override else _data_dir()
        sdir = _session_dir(dest_root)
        sdir.mkdir(parents=True, exist_ok=True)
        state["session_dir"] = str(sdir)
        print(f"[go] session dir: {sdir}")
    except Exception as e:
        print(f"[go] could not create session dir: {e}")
        state["session_dir"] = None
    rig_filename = Path(state["rig_path"]).name
    task_filename = Path(state["task_path"]).name

    steps = []

    # Fresh UDP event listener
    _stop_udp_listener()
    _start_udp_listener(rig["network"]["event_port"])
    print("[go] UDP listener started")

    # Reset the live-RT temp file for this session (hit-trial RTs in ms, one per line, appended by
    # the UDP listener). Lightweight persistence on the controller alongside the browser histogram.
    global _rt_hits_path
    _rt_hits_path = os.path.join(tempfile.gettempdir(), f"vrfarm_rt_{state['session_id']}.txt")
    try:
        with open(_rt_hits_path, "w") as f:
            f.write("# response_time_ms (hit trials)\n")
        print(f"[go] RT temp file: {_rt_hits_path}")
    except Exception as e:
        print(f"[go] could not open RT temp file: {e}")
        _rt_hits_path = None

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
    # Behavioral devices the user chose NOT to save -> tell the Leader to skip their HDF5 datasets.
    skip_save = [d for d in ("lick_sensor", "reward", "photodiode", "encoder") if not save.get(d, True)]
    leader_args = [
        "--rig", f"/home/vruser/rig/rigs/{rig_filename}",
        "--task", f"/home/vruser/rig/experiments/{task_filename}",
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

    # Start the camera: RECORD to disk if 'camera' is checked, else PREVIEW-only (livestream, no file).
    cam_cfg = rig.get("devices", {}).get("camera", {})
    save_camera = bool(save.get("camera", True))
    cam_recording = False
    if cam_cfg.get("enabled", False):
        video_dir = rig["data"].get("video_dir") or "/media/vruser/ssd/video"   # `or`: a present-but-empty value falls back too
        payload = {
            "resolution": cam_cfg.get("resolution", [1280, 720]),
            "fps": cam_cfg.get("fps", 50),
            "bitrate_mbps": cam_cfg.get("bitrate_mbps", 4),
            "h264_profile": cam_cfg.get("h264_profile", "main"),
            "gop_s": cam_cfg.get("gop_s", 5.0),
            "live_preset": cam_cfg.get("live_preset", "med"),
            **_cam_exposure(cam_cfg),   # runtime exposure/gain override -> carried from the UI into the recording
        }
        if save_camera:
            payload.update({"session_id": state["session_id"], "video_dir": video_dir})  # -> record
        else:
            payload["downsample"] = True   # -> preview-only livestream, no file
        for pi in rig["pis"]:
            if "camera" in pi.get("devices", []):
                try:
                    requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop",
                                  json={}, timeout=5)
                    r = requests.post(
                        f"http://{pi['ip']}:{api_port}/api/camera_preview_start",
                        json=payload, timeout=10)
                    # A failed start may not return JSON (e.g. HTTP 500) — parse defensively.
                    ok, err = False, f"HTTP {r.status_code}"
                    try:
                        j = r.json()
                        ok, err = bool(j.get("ok")), j.get("error", err)
                    except Exception:
                        pass
                    if ok and save_camera:
                        cam_recording = True
                        steps.append(f"Camera recording on {pi['name']}")
                    elif ok:
                        steps.append(f"Camera livestream (NOT saved) on {pi['name']}")
                    elif save_camera:
                        warn = (f"⚠️  CAMERA NOT RECORDING on {pi['name']}: {err} — session runs "
                                f"WITHOUT video. Check the SSD / video_dir ({video_dir}).")
                        steps.append(warn)
                        _app_logger.warning(warn)
                    else:
                        steps.append(f"⚠️  Camera livestream failed on {pi['name']}: {err}")
                except Exception as e:
                    if save_camera:
                        warn = (f"⚠️  CAMERA NOT RECORDING on {pi['name']}: {e} — session runs "
                                f"WITHOUT video. Check the SSD / video_dir.")
                        steps.append(warn)
                        _app_logger.warning(warn)
                    else:
                        steps.append(f"⚠️  Camera livestream failed on {pi['name']}: {e}")
                break
        print(f"[go] Camera: {'recording' if cam_recording else ('livestream (not saved)' if not save_camera else 'FAILED — no video')}")
    else:
        print("[go] Camera not enabled — skipping")

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
    """Session data folder: <root>/<subject>/<subject>_<date>/<session_id> — the same layout used
    by transfer(), so plots saved at Go land alongside the data if/when it's transferred."""
    subject_id = state["session"]["subject_id"]
    date_str = state["session"]["date"]
    return Path(dest_root) / subject_id / f"{subject_id}_{date_str}" / state["session_id"]


@app.route("/api/save_plots", methods=["POST"])
def save_plots():
    """Write the browser's live-plot PNGs into the session folder (created at Go) so the plots
    persist for later review regardless of whether the data is transferred."""
    import base64
    req = request.get_json(silent=True) or {}
    plots = req.get("plots", {})
    sess_dir = state.get("session_dir")
    if not sess_dir:
        if not state.get("session_id"):
            return jsonify({"ok": False, "error": "No active session"}), 400
        sess_dir = str(_session_dir(_data_dir()))   # fallback: default data dir
    d = Path(sess_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify({"ok": False, "error": f"Cannot create {d}: {e}"}), 500
    written = []
    for name, dataurl in plots.items():
        if not isinstance(dataurl, str) or "," not in dataurl:
            continue
        try:
            raw = base64.b64decode(dataurl.split(",", 1)[1])
            safe = "".join(c for c in str(name) if c.isalnum() or c in "-_") or "plot"
            (d / f"{safe}.png").write_bytes(raw)
            written.append(f"{safe}.png")
        except Exception:
            pass
    return jsonify({"ok": True, "dir": str(d), "written": written})


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
    # (metadata.yaml, stimuli.npz, trials.yaml, frame_timestamps.npy) into it and delete them,
    # so only <session>.h5 + video.h264 remain to pull.
    steps = []
    leader = next((p for p in rig["pis"] if p.get("role") == "leader"), rig["pis"][0])
    try:
        cj = requests.post(
            f"http://{leader['ip']}:{api_port}/api/consolidate/{session_id}",
            json={"video_dir": rig.get("data", {}).get("video_dir", "")}, timeout=120).json()
        if cj.get("ok") and cj.get("skipped"):
            steps.append(f"Consolidate: {cj['skipped']}")
        elif cj.get("ok"):
            got = ', '.join(k for k, v in cj.get("merged", {}).items() if v) or "none"
            steps.append(f"Consolidated .h5 (merged {got}; removed {', '.join(cj.get('removed', [])) or 'none'})")
        else:
            steps.append(f"⚠️  Consolidate failed: {cj.get('error', '?')}")
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
                params={"video_dir": rig.get("data", {}).get("video_dir", "")},
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
            out = dest / fname
            part = out.parent / (out.name + ".part")
            expect = sizes.get(fpath)
            try:
                # Video files can be large — use longer timeout. Note this is a per-read stall
                # timeout, not a deadline for the whole transfer.
                dl_timeout = 300 if fname.endswith(".h264") else 60
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


def _cam_exposure(cam_cfg):
    """Effective camera exposure/gain: the experiment UI's runtime override (set via
    /api/camera_controls) merged over the rig-yaml defaults. Runtime-only — cleared on Load Rig, so
    it reverts to the yaml on relaunch. Used by both the Live preview and the GO recording start."""
    ov = state.get("camera_override", {})
    return {
        "auto_exposure": ov.get("auto_exposure", cam_cfg.get("auto_exposure", True)),
        "exposure_ms": ov.get("exposure_ms", cam_cfg.get("exposure_ms", 10)),
        "gain": ov.get("gain", cam_cfg.get("gain", 1.0)),
    }


@app.route("/api/camera_controls", methods=["POST"])
def camera_controls():
    """Live exposure/gain tweak from the experiment UI. Stores a runtime-only override (the Live
    preview + GO recording read it via _cam_exposure) and pushes it to the running camera so the
    preview updates immediately. Refused once recording has started (phase 'running') — no mid-run
    changes. Never written to the rig yaml."""
    rig = state["rig_config"]
    if not rig:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400
    if state.get("phase") == "running":
        return jsonify({"ok": False, "error": "locked during recording"}), 409
    data = request.json or {}
    ov = {k: data[k] for k in ("auto_exposure", "exposure_ms", "gain") if k in data}
    state["camera_override"].update(ov)     # runtime-only; GO + Live read this via _cam_exposure
    api_port = rig["network"]["api_port"]
    ip = next((pi["ip"] for pi in rig["pis"] if "camera" in pi.get("devices", [])), None)
    if not ip:
        return jsonify({"ok": True, "stored": ov})   # no camera Pi; just remembered for GO
    try:
        # apply live to the running preview (a no-op on the Pi if the camera isn't open yet)
        r = requests.post(f"http://{ip}:{api_port}/api/camera_controls", json=ov, timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": True, "stored": ov, "warn": str(e)})


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
                                **_cam_exposure(cam_cfg)},
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
    # Reconnecting relay: rides through pi_api restarts (Deploy), the preview->record swap (Go), and the
    # <img>-before-preview race instead of collapsing to a silent 200-blank. See shared/mjpeg_relay.py.
    from shared.mjpeg_relay import relay
    return Response(relay(f"http://{ip}:{api_port}/api/camera_stream"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/camera_reset", methods=["POST"])
def camera_reset():
    """Proxy: rebind the leader camera's i2c sensor driver to recover a frozen/wedged camera."""
    rig = state["rig_config"]
    if not rig:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400
    api_port = rig["network"]["api_port"]
    ip = None
    for pi in rig["pis"]:
        if "camera" in pi.get("devices", []):
            ip = pi["ip"]
            break
    if not ip:
        return jsonify({"ok": False, "error": "Camera not assigned"}), 400
    try:
        r = requests.post(f"http://{ip}:{api_port}/api/camera_reset", timeout=20)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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


def _event_is_go(event: dict) -> bool:
    """Whether a trial event is a GO trial (should lick), per the task go_rule + stim azimuth.
    Mirrors the engine (_classify_go) and the UI (_isGo): all->go, right->az>0, else az<0."""
    rule = (state.get("task_config") or {}).get("stimulus", {}).get("go_rule", "left")
    if rule == "all":
        return True
    az = event.get("stim_az", 0) or 0
    return az > 0 if rule == "right" else az < 0


def _fmt_hm(ms) -> str:
    """'1 hour, 15 minutes' from a duration in ms (for the session-start ETA)."""
    total_min = int(round((ms or 0) / 60000))
    h, m = divmod(total_min, 60)
    return f"{h} hour{'' if h == 1 else 's'}, {m} minute{'' if m == 1 else 's'}"


def _session_metrics(trials):
    """(hit_rate, median_rt_ms) from trial events, matching the live plots: Cumulative HR =
    go hits / go trials; median RT = median of go-hit RTs (ms). Either is None when undefined."""
    go = [t for t in trials if _event_is_go(t)]
    hits = [t for t in go if t.get("outcome") == "hit"]
    hit_rate = (len(hits) / len(go)) if go else None
    rts = sorted(t["rt_ms"] for t in hits
                 if isinstance(t.get("rt_ms"), (int, float)) and not isinstance(t.get("rt_ms"), bool)
                 and t["rt_ms"] == t["rt_ms"])
    if rts:
        n = len(rts)
        med = rts[n // 2] if n % 2 else (rts[n // 2 - 1] + rts[n // 2]) / 2
    else:
        med = None
    return hit_rate, med


def _metrics_suffix(trials) -> str:
    """', median RT=N ms and hit rate=0.xx' — appended to the end / global-timeout messages.
    Formatted to match the on-screen values (RT rounded to ms, HR as a 2-decimal ratio)."""
    hr, med = _session_metrics(trials)
    med_str = f"{round(med)} ms" if med is not None else "n/a"
    hr_str = f"{hr:.2f}" if hr is not None else "n/a"
    return f", median RT={med_str} and hit rate={hr_str}"


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
                event = json.loads(data)
                rig_name = state["rig_config"]["name"]
                # Track trials
                if event.get("type") == "trial":
                    _trials.append(event)
                    # Append GO-trial hit RTs (ms) to the live-RT temp file (matches the histogram;
                    # a nogo-trial lick is a false alarm, not a hit).
                    if _rt_hits_path and event.get("outcome") == "hit" and _event_is_go(event):
                        rt = event.get("rt_ms")
                        if isinstance(rt, (int, float)) and not isinstance(rt, bool) and rt == rt:
                            try:
                                with open(_rt_hits_path, "a") as f:
                                    f.write(f"{rt:.1f}\n")
                            except Exception:
                                pass
                elif event.get("type") == "shepherd_alert":
                    # Health alert from the shepherd monitor on a rig Pi. It flows to
                    # the SSE queue (below) for the UI log like any event; a CRITICAL
                    # also goes to Slack so it reaches you when the browser is closed.
                    if event.get("level") == "critical":
                        notify(f"🔴 {event.get('host', rig_name)} — {event.get('message', 'critical alert')}")
                elif event.get("type") == "global_timeout":
                    notify(f"⏱️ {rig_name} — GLOBAL TIMEOUT: {event.get('n_dry', '?')} dry "
                           f"trials, aborting at trial {event.get('trial', '?')} "
                           f"({state['session_id']}){_metrics_suffix(_trials)}")
                elif event.get("type") == "session_end":
                    _session_end_seen = True
                    notify(f"✅ {rig_name} — session ended: {event.get('n_completed', '?')}"
                           f"/{event.get('n_planned', '?')} trials ({state['session_id']})"
                           f"{_metrics_suffix(_trials)}")
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
