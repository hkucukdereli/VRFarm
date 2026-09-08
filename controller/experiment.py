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
                           get_subject_history, photodiode_init_payload)
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


def _warp_path(rs: RigState) -> Path:
    """Per-rig warp map (display_calibration/<rig>/warp_map.npz) with the legacy single-file
    location as fallback until every rig has been migrated."""
    per_rig = ROOT / "display_calibration" / rs.name / "warp_map.npz"
    return per_rig if per_rig.exists() else ROOT / "display_calibration" / "warp_map.npz"


def _cam_exposure(rs: RigState, cam_cfg: dict) -> dict:
    """Effective camera exposure/gain: the runtime override merged over the rig-yaml defaults."""
    ov = rs.camera_override or {}
    return {
        "auto_exposure": ov.get("auto_exposure", cam_cfg.get("auto_exposure", True)),
        "exposure_ms": ov.get("exposure_ms", cam_cfg.get("exposure_ms", 10)),
        "gain": ov.get("gain", cam_cfg.get("gain", 1.0)),
    }


def _camera_ip(rs: RigState):
    return next((pi["ip"] for pi in rs.pis if "camera" in pi.get("devices", [])), None)


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
    rs.trial_table = []
    rs.camera_override = {}   # drop runtime exposure tweaks -> revert to the rig yaml

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

        init_errors = []
        for dev_name in pi.get("devices", []):
            dev_cfg = devices.get(dev_name, {})
            if not dev_cfg.get("enabled", True):
                continue
            try:
                if dev_name == "display":
                    requests.post(f"http://{ip}:{api_port}/api/shutdown_display", json={}, timeout=5)
                    r = requests.post(f"http://{ip}:{api_port}/api/init_projector", json={}, timeout=35)
                    if not r.json().get("ok"):
                        init_errors.append(r.json().get(
                            "error", "Projector failed to initialize — is it powered on?"))
                elif dev_name == "lick_sensor":
                    r = requests.post(f"http://{ip}:{api_port}/api/init_lick", json={
                        "i2c_address": dev_cfg.get("i2c_address", "0x5A"),
                        "electrode": dev_cfg.get("electrode", 4),
                    }, timeout=10)
                    if not r.json().get("ok"):
                        init_errors.append(r.json().get("error", "Lick sensor failed — check I2C connection"))
                elif dev_name == "reward":
                    r = requests.post(f"http://{ip}:{api_port}/api/init_reward", json={
                        "pins": dev_cfg.get("pins", {"main": {"gpio": 18}}),
                    }, timeout=10)
                    if not r.json().get("ok"):
                        init_errors.append(r.json().get("error", "Reward valve failed — check GPIO pins/wiring"))
                elif dev_name == "camera":
                    r = requests.post(f"http://{ip}:{api_port}/api/init_camera", json={}, timeout=10)
                    if not r.json().get("ok"):
                        init_errors.append(r.json().get("error", "Camera not detected — check CSI cable"))
                elif dev_name == "photodiode":
                    # No follower_ip here: the experiment path verifies later, at leader-engine
                    # init, over UDP (the setup UI shut this display down at Load Rig).
                    r = requests.post(f"http://{ip}:{api_port}/api/init_photodiode",
                                      json=photodiode_init_payload(dev_cfg), timeout=10)
                    resp = r.json()
                    if not resp.get("ok"):
                        init_errors.append(resp.get("error", "Photodiode failed — check the GPIO pin/wiring"))
                elif dev_name == "encoder":
                    r = requests.post(f"http://{ip}:{api_port}/api/init_encoder", json={
                        "i2c_address": dev_cfg.get("i2c_address", "0x36"),
                        "i2c_bus": dev_cfg.get("i2c_bus", 1),
                        "wheel_diameter_cm": dev_cfg.get("wheel_diameter_cm", 15.0),
                        "sample_hz": dev_cfg.get("sample_hz", 100),
                    }, timeout=10)
                    if not r.json().get("ok"):
                        init_errors.append(r.json().get("error", "Encoder failed — check I2C 0x36 / magnet"))
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
    rs.rt_hits_path = None
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
    """Upload code + configs, generate stims on the Leader, push them to the Follower(s)."""
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
    followers = rs.followers()
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

        # 0b. Restart pi_api on all Pis so new code takes effect
        for pi in rs.pis:
            try:
                requests.post(f"http://{pi['ip']}:{api_port}/api/restart", json={}, timeout=3)
            except Exception:
                pass
        time.sleep(3)
        for pi in rs.pis:
            alive = False
            for _ in range(10):
                try:
                    if requests.get(f"http://{pi['ip']}:{api_port}/api/status", timeout=2).status_code == 200:
                        alive = True
                        break
                except Exception:
                    pass
                time.sleep(1)
            if not alive:
                raise RuntimeError(f"{pi['name']} did not come back after restart")
        steps.append("Restarted pi_api on all Pis")

        # 0c. Restart displayd on any Pi that runs it (new displayd/renderer code takes effect).
        for pi in rs.pis:
            try:
                r = requests.post(f"http://{pi['ip']}:{api_port}/api/restart_displayd", json={}, timeout=120)
                d = r.json() if r.ok else {}
                if d.get("skipped"):
                    continue
                steps.append(f"displayd restarted on {pi['name']} ({d.get('state', '?')})" if d.get("ok")
                             else f"WARNING: displayd restart failed on {pi['name']}: {d.get('error', r.status_code)}")
            except Exception as e:
                steps.append(f"WARNING: displayd restart error on {pi['name']}: {e}")

        # 0d. Confirm the projector is up on each follower.
        for fpi in followers:
            display_cfg = rig.get("devices", {}).get("display", {})
            if display_cfg.get("enabled", True) and "display" in fpi.get("devices", []):
                try:
                    r = requests.post(f"http://{fpi['ip']}:{api_port}/api/init_projector", json={}, timeout=35)
                    if r.json().get("ok"):
                        steps.append(f"Re-initialized projector on {fpi['name']}")
                    else:
                        steps.append(f"Projector re-init warning on {fpi['name']}: {r.json().get('error', '?')}")
                except Exception as e:
                    steps.append(f"Projector re-init failed on {fpi['name']}: {e}")

        # 1. Rig config to all Pis (under its own filename: pi_api sees rigs/<rig>.yaml)
        for pi in rs.pis:
            _upload_file(pi["ip"], api_port, str(rs.path), f"rigs/{rs.path.name}")
        steps.append("Uploaded rig config to all Pis")

        # 2. Task config to the Leader, then generate stims there
        remote_task_path = f"experiments/{Path(rs.task_path).name}"
        _upload_file(leader["ip"], api_port, rs.task_path, remote_task_path)
        steps.append("Uploaded task config to Leader")
        r = requests.post(
            f"http://{leader['ip']}:{api_port}/api/generate_stims",
            json={"task_config": remote_task_path,
                  "session_id": rs.session_id,
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

        # 3. NPZ from the Leader to the Follower(s)
        npz_remote = stim_result.get("npz_path", "")
        if npz_remote:
            r = requests.get(f"http://{leader['ip']}:{api_port}/api/download/{npz_remote}",
                             timeout=15, stream=True)
            tmp_npz = Path(tempfile.mktemp(suffix=".npz"))
            with open(tmp_npz, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            for fpi in followers:
                try:
                    _upload_file(fpi["ip"], api_port, str(tmp_npz), f"stims/{rs.session_id}/stimuli.npz")
                    steps.append(f"Pushed stims to {fpi['name']}")
                except Exception as e:
                    steps.append(f"Push stims to {fpi['name']} FAILED: {e}")
            tmp_npz.unlink(missing_ok=True)

            # 4. Universal trial table (YAML) from the Leader
            yaml_remote = npz_remote.replace("stimuli.npz", "trials.yaml")
            try:
                r = requests.get(f"http://{leader['ip']}:{api_port}/api/download/{yaml_remote}", timeout=10)
                r.raise_for_status()
                table = yaml.safe_load(r.content)
                if not isinstance(table, list):
                    raise ValueError(f"Expected list, got {type(table).__name__}")
                rs.trial_table = table
                steps.append(f"Trial table: {len(table)} trials")
            except Exception as e:
                print(f"Trial table download failed: {e}")
                rs.trial_table = []

        rs.deployed = True
        rs.phase = "deployed"
        rs.logger.info(f"DEPLOY ok session={rs.session_id} steps={len(steps)}")
        return jsonify({"ok": True, "steps": steps})
    except Exception as e:
        rs.logger.error(f"DEPLOY failed session={rs.session_id} error={e}")
        return jsonify({"ok": False, "error": str(e), "steps": steps})
    finally:
        rs.release_busy()


@bp.route("/correct_contrast", methods=["POST"])
def correct_contrast():
    """Clamp entered contrast value(s) to the achievable ceiling in the active metric (matches
    what stim generation bakes in: per-azimuth luminance when apply_warp + a warp map exist)."""
    import numpy as np
    from shared.stim_generator import (get_luminance_correction, fraction_to_metric,
                                       snap_contrast_to_bitcode)
    rs: RigState = g.rs
    data = request.get_json(silent=True) or {}
    try:
        values = [float(v) for v in (data.get("values") or [])]
        bg = float(data.get("background_gray") or 0.0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid values or background_gray"}), 400
    block_seq = data.get("block_sequence") or [0.0]
    display_cfg = (rs.config or {}).get("devices", {}).get("display", {})
    metric = display_cfg.get("contrast_metric", "weber")
    apply_warp = display_cfg.get("apply_warp", False)

    max_lum = 1.0
    note = ("apply_warp off; global display ceiling" if not apply_warp
            else "no warp map; global display ceiling")
    warp_path = _warp_path(rs)
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
    c_ceiling = min(fraction_to_metric(f_ceiling, bg, metric), 1.0)
    corrected = [round(snap_contrast_to_bitcode(min(v, c_ceiling), bg, metric), 4) for v in values]
    c_ceiling = snap_contrast_to_bitcode(c_ceiling, bg, metric)
    return jsonify({"ok": True, "corrected": corrected,
                    "ceiling": round(float(c_ceiling), 4), "metric": metric, "note": note})


@bp.route("/trial_table")
def get_trial_table():
    return jsonify({"trials": g.rs.trial_table or []})


@bp.route("/drain_events", methods=["POST"])
def drain_events():
    return jsonify({"ok": True, "drained": g.rs.drain()})


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
    followers = rs.followers()
    api_port = rs.api_port
    cmd_port = rig["network"]["command_port"]
    session = rs.session

    _body = request.get_json(silent=True) or {}
    save = _body.get("save", {})
    # Create the session data folder NOW under the controller's data root so the live plots
    # can be saved into it at end/stop; the Data tab later syncs the Pi's files into the same place.
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

    # Live-RT temp file for this session (hit-trial RTs, appended by the demux).
    rs.rt_hits_path = os.path.join(tempfile.gettempdir(), f"vrfarm_rt_{rs.name}_{rs.session_id}.txt")
    try:
        with open(rs.rt_hits_path, "w") as f:
            f.write("# response_time_ms (hit trials)\n")
    except Exception as e:
        print(f"[go:{rs.name}] could not open RT temp file: {e}")
        rs.rt_hits_path = None

    # Stop leftover processes, release devices on all Pis
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

    # Projector up on followers, pi_api's display lease released
    for fpi in followers:
        for ep, to in (("shutdown_display", 3), ("init_projector", 35)):
            try:
                requests.post(f"http://{fpi['ip']}:{api_port}/api/{ep}", json={}, timeout=to)
            except Exception:
                pass

    # Follower engine(s)
    for fpi in followers:
        stim_path = f"/home/vruser/rig/stims/{rs.session_id}/stimuli.npz"
        try:
            r = requests.post(f"http://{fpi['ip']}:{api_port}/api/start",
                              json={"script": "follower",
                                    "args": ["--rig", f"/home/vruser/rig/rigs/{rig_filename}",
                                             "--stims", stim_path]}, timeout=10)
            res = r.json()
            if res.get("ok"):
                steps.append(f"Follower started on {fpi['name']} (pid {res.get('pid', '?')})")
            else:
                return jsonify({"ok": False, "error": f"Follower on {fpi['name']}: {res.get('error', 'unknown')}", "steps": steps})
        except Exception as e:
            return jsonify({"ok": False, "error": f"Failed to start follower on {fpi['name']}: {e}", "steps": steps})

    # Leader engine. Behavioral devices the user chose NOT to save -> --no-save.
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
        r = requests.post(f"http://{leader['ip']}:{api_port}/api/start",
                          json={"script": "leader", "args": leader_args}, timeout=10)
        res = r.json()
        if res.get("ok"):
            steps.append(f"Leader started on {leader['name']} (pid {res.get('pid', '?')})")
        else:
            return jsonify({"ok": False, "error": f"Leader: {res.get('error', 'unknown')}", "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to start leader: {e}", "steps": steps})

    # Wait for the leader to finish device init ("Waiting for START command..."); surface its
    # error if it exits during init instead.
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

    # Camera: RECORD to disk if 'camera' is checked, else PREVIEW-only (livestream, no file).
    cam_cfg = rig.get("devices", {}).get("camera", {})
    save_camera = bool(save.get("camera", True))
    cam_recording = False
    if cam_cfg.get("enabled", False):
        video_dir = (rig.get("data") or {}).get("video_dir") or "/media/vruser/ssd/video"
        payload = {
            "resolution": cam_cfg.get("resolution", [1280, 720]),
            "fps": cam_cfg.get("fps", 50),
            "bitrate_mbps": cam_cfg.get("bitrate_mbps", 4),
            "h264_profile": cam_cfg.get("h264_profile", "main"),
            "gop_s": cam_cfg.get("gop_s", 5.0),
            "sensor_mode": cam_cfg.get("sensor_mode"),
            "bit_depth": cam_cfg.get("bit_depth"),
            "live_preset": cam_cfg.get("live_preset", "med"),
            **_cam_exposure(rs, cam_cfg),
        }
        if save_camera:
            payload.update({"session_id": rs.session_id, "video_dir": video_dir})
        else:
            payload["downsample"] = True
        for pi in rs.pis:
            if "camera" not in pi.get("devices", []):
                continue
            try:
                requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_stop", json={}, timeout=5)
                r = requests.post(f"http://{pi['ip']}:{api_port}/api/camera_preview_start",
                                  json=payload, timeout=10)
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
                    rs.logger.warning(warn)
                else:
                    steps.append(f"⚠️  Camera livestream failed on {pi['name']}: {err}")
            except Exception as e:
                if save_camera:
                    warn = (f"⚠️  CAMERA NOT RECORDING on {pi['name']}: {e} — session runs "
                            f"WITHOUT video. Check the SSD / video_dir.")
                    steps.append(warn)
                    rs.logger.warning(warn)
                else:
                    steps.append(f"⚠️  Camera livestream failed on {pi['name']}: {e}")
            break
        print(f"[go:{rs.name}] Camera: {'recording' if cam_recording else ('livestream (not saved)' if not save_camera else 'FAILED — no video')}")

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


@bp.route("/stop", methods=["POST"])
def stop():
    teardown_session(g.rs, "manual")
    return jsonify({"ok": True})


@bp.route("/reward", methods=["POST"])
def manual_reward():
    rs: RigState = g.rs
    send_command(rs.leader()["ip"], rs.config["network"]["command_port"], {"cmd": "REWARD"})
    return jsonify({"ok": True})


@bp.route("/save_plots", methods=["POST"])
def save_plots():
    """Write the browser's live-plot PNGs into the session folder (created at Go)."""
    rs: RigState = g.rs
    req = request.get_json(silent=True) or {}
    plots = req.get("plots", {})
    sess_dir = rs.session_dir
    if not sess_dir:
        if not rs.session_id:
            return jsonify({"ok": False, "error": "No active session"}), 400
        sess_dir = str(_session_dir(rs, settings.data_root()))
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


@bp.route("/trials")
def get_trials():
    return jsonify(g.rs.trials)


@bp.route("/subject_history/<subject_id>")
def subject_history(subject_id):
    return jsonify(get_subject_history(subject_id, settings.data_root() / "subjects"))


@bp.route("/engine_logs")
def engine_logs():
    rs: RigState = g.rs
    n = request.args.get("n", 50, type=int)
    result = {}
    for pi in rs.pis:
        try:
            r = requests.get(f"http://{pi['ip']}:{rs.api_port}/api/logs?n={n}", timeout=3)
            result[pi["name"]] = r.json().get("lines", [])
        except Exception as e:
            result[pi["name"]] = [f"Error fetching logs: {e}"]
    return jsonify(result)


# ── camera ──

@bp.route("/camera_controls", methods=["POST"])
def camera_controls():
    """Live exposure/gain tweak: runtime-only override, pushed to the running preview.
    Refused once recording has started (phase 'running')."""
    rs: RigState = g.rs
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "locked during recording"}), 409
    data = request.json or {}
    ov = {k: data[k] for k in ("auto_exposure", "exposure_ms", "gain") if k in data}
    rs.camera_override.update(ov)
    ip = _camera_ip(rs)
    if not ip:
        return jsonify({"ok": True, "stored": ov})
    try:
        r = requests.post(f"http://{ip}:{rs.api_port}/api/camera_controls", json=ov, timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": True, "stored": ov, "warn": str(e)})


@bp.route("/camera_start", methods=["POST"])
def camera_start():
    rs: RigState = g.rs
    cam_cfg = rs.config.get("devices", {}).get("camera", {})
    ip = _camera_ip(rs)
    if not ip:
        return jsonify({"ok": False, "error": "Camera not assigned"}), 400
    try:
        r = requests.post(f"http://{ip}:{rs.api_port}/api/camera_preview_start",
                          json={"resolution": cam_cfg.get("resolution", [1280, 720]),
                                "fps": cam_cfg.get("fps", 50),
                                "downsample": True,
                                "live_preset": cam_cfg.get("live_preset", "med"),
                                **_cam_exposure(rs, cam_cfg)},
                          timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/camera_stop", methods=["POST"])
def camera_stop():
    rs: RigState = g.rs
    if rs.phase == "running":
        return jsonify({"ok": True})      # never stop the session camera mid-run
    for pi in rs.pis:
        if "camera" in pi.get("devices", []):
            try:
                requests.post(f"http://{pi['ip']}:{rs.api_port}/api/camera_preview_stop", json={}, timeout=5)
            except Exception:
                pass
    return jsonify({"ok": True})


@bp.route("/camera_feed")
def camera_feed():
    rs: RigState = g.rs
    ip = _camera_ip(rs)
    if not ip:
        return "No camera", 400
    return Response(relay(f"http://{ip}:{rs.api_port}/api/camera_stream"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@bp.route("/camera_reset", methods=["POST"])
def camera_reset():
    rs: RigState = g.rs
    ip = _camera_ip(rs)
    if not ip:
        return jsonify({"ok": False, "error": "Camera not assigned"}), 400
    try:
        r = requests.post(f"http://{ip}:{rs.api_port}/api/camera_reset", timeout=20)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── live events ──

@bp.route("/events")
def event_stream():
    return sse_response(g.rs)
