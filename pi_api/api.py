"""
pi_api/api.py

Lightweight Flask REST API running on each Pi (port 5080).
Handles: status, file upload/download, process start/stop,
stim generation, and calibration.
"""

from __future__ import annotations
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

# Base directory for rig files on this Pi
RIG_DIR = Path.home() / "rig"
DATA_DIR = Path.home() / "data"

# Currently running process (leader or follower)
_process: subprocess.Popen | None = None
_process_log: list[str] = []

# Persistent device instances (survive across requests)
_devices: dict = {}  # name -> initialized Device instance
_devices_lock = threading.Lock()
# Serializes camera open/close across the whole check→pop→construct sequence so two concurrent
# camera_preview_start/stop calls can't both open a Picamera2 and hang libcamera. Always acquire
# this OUTSIDE _devices_lock (never the reverse) to keep a consistent lock order.
_camera_lock = threading.Lock()
_lick_events: list = []
_photodiode_events: list = []
_photodiode_raw: list = []   # diagnostic raw transitions {t, level} for the setup-UI scope
_encoder_events: list = []   # running-wheel {t, speed_cms, distance_cm} for the setup-UI monitor
_reward_events: list = []


@app.route("/api/status")
def status():
    """Health check: running process, disk space, hostname."""
    disk = shutil.disk_usage(str(Path.home()))
    return jsonify({
        "ok": True,
        "hostname": os.uname().nodename,
        "process_running": _process is not None and _process.poll() is None,
        "disk_free_gb": round(disk.free / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
        "python": sys.executable,
    })


@app.route("/api/upload", methods=["POST"])
def upload():
    """Upload a file to ~/rig/<path>."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file"}), 400
    f = request.files["file"]
    rel_path = request.form.get("path", f.filename)
    dest = (RIG_DIR / rel_path).resolve()
    if not dest.is_relative_to(RIG_DIR):
        return jsonify({"ok": False, "error": "Path outside allowed directory"}), 400
    dest.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(dest))
    return jsonify({"ok": True, "path": str(dest)})


ALLOWED_DOWNLOAD_DIRS = [RIG_DIR, DATA_DIR, Path("/media/vruser/ssd/video")]
# video_dir(s) announced at runtime (camera start / list_files ?video_dir=...) — the rig
# yaml is the source of truth on the controller; the Pi learns it per-request.
_EXTRA_DOWNLOAD_DIRS: set = set()


@app.route("/api/download/<path:filepath>")
def download(filepath):
    """Download a file from the Pi."""
    # Allow absolute paths or relative to home
    p = Path(filepath)
    if not p.is_absolute():
        cand = Path.home() / filepath
        # Flask collapses the leading slash of an absolute path in the URL ("/media/x"
        # arrives as "media/x"), so if the home-relative candidate doesn't exist retry
        # root-anchored — otherwise files outside home (e.g. SSD video) 404.
        p = cand if cand.exists() else Path("/") / filepath
    p = p.resolve()
    if not any(p.is_relative_to(d)
               for d in list(ALLOWED_DOWNLOAD_DIRS) + list(_EXTRA_DOWNLOAD_DIRS)):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    if not p.exists():
        return jsonify({"ok": False, "error": f"Not found: {filepath}"}), 404
    return send_file(str(p), as_attachment=True)


def _parse_session_id(session_id: str):
    """Parse 'M1_20260413_001' → ('M1', 'M1_20260413', 'M1_20260413_001')."""
    parts = session_id.rsplit("_", 2)
    if len(parts) == 3:
        subj, date_str, _ = parts
        return subj, f"{subj}_{date_str}", session_id
    return None, None, session_id


@app.route("/api/files/<session_id>")
def list_files(session_id):
    """List session data files available for transfer."""
    files = []
    home = Path.home()
    subj, subj_date, _ = _parse_session_id(session_id)

    # Check data dir (nested: subject/subject_date/session_id)
    for candidate in [
        DATA_DIR / subj / subj_date / session_id if subj else None,
        DATA_DIR / session_id,  # legacy flat layout
    ]:
        if candidate and candidate.exists():
            for f in candidate.rglob("*"):
                if f.is_file():
                    try:
                        files.append(str(f.relative_to(home)))
                    except ValueError:
                        files.append(str(f))
            break

    # Video dir: the controller passes the rig yaml's video_dir (?video_dir=...);
    # fall back to the legacy SSD path. Register it so /api/download accepts it.
    vd = request.args.get("video_dir", "").strip()
    video_base = Path(vd) if vd else Path("/media/vruser/ssd/video")
    if vd:
        _EXTRA_DOWNLOAD_DIRS.add(video_base)
    for candidate in [
        video_base / subj / subj_date / session_id if subj else None,
        video_base / session_id,  # legacy
    ]:
        if candidate and candidate.exists():
            for f in candidate.rglob("*"):
                if f.is_file():
                    try:
                        files.append(str(f.relative_to(home)))
                    except ValueError:
                        files.append(str(f))
            break

    # video_dir may coincide with the data dir — never list (= download) a file twice
    files = list(dict.fromkeys(files))
    return jsonify({"files": files})


@app.route("/api/consolidate/<session_id>", methods=["POST"])
def consolidate(session_id):
    """Leader only. Fold the session's sidecars (metadata.yaml, stimuli.npz, trials.yaml,
    frame_timestamps.npy) into <session_id>.h5 and delete them, leaving one self-contained
    .h5 (+ video.h264) to transfer. Idempotent."""
    data = request.get_json(silent=True) or {}
    subj, subj_date, _ = _parse_session_id(session_id)
    session_dir = (DATA_DIR / subj / subj_date / session_id) if subj else (DATA_DIR / session_id)
    vd = (data.get("video_dir") or "").strip() or "/media/vruser/ssd/video"
    video_session_dir = (Path(vd) / subj / subj_date / session_id) if subj else (Path(vd) / session_id)
    try:
        if str(RIG_DIR) not in sys.path:
            sys.path.insert(0, str(RIG_DIR))
        from shared.consolidate import consolidate_session
        res = consolidate_session(
            session_dir, video_session_dir if video_session_dir.exists() else None)
        return jsonify(res)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/start", methods=["POST"])
def start():
    """Start leader.py or follower.py as a subprocess."""
    global _process, _process_log
    data = request.json
    script = data.get("script")  # "leader" or "follower"
    args = data.get("args", [])

    if _process and _process.poll() is None:
        return jsonify({"ok": False, "error": "Process already running"}), 409

    conda_prefix = Path.home() / "miniforge3" / "envs" / "rig" / "bin"
    python = str(conda_prefix / "python")

    if script == "leader":
        cmd = [python, str(RIG_DIR / "engine" / "leader.py")] + args
    elif script == "follower":
        cmd = [python, str(RIG_DIR / "engine" / "follower.py")] + args
    else:
        return jsonify({"ok": False, "error": f"Unknown script: {script}"}), 400

    _process_log = []
    env = os.environ.copy()
    if script == "follower":
        env.setdefault("DISPLAY", ":0")
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    _process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env)

    # Log reader thread
    def read_output():
        for line in _process.stdout:
            _process_log.append(line.rstrip())
            if len(_process_log) > 500:
                _process_log.pop(0)

    threading.Thread(target=read_output, daemon=True).start()

    return jsonify({"ok": True, "pid": _process.pid})


@app.route("/api/stop", methods=["POST"])
def stop():
    """Stop the running process. SIGTERM first, SIGKILL if needed."""
    global _process
    if _process and _process.poll() is None:
        _process.send_signal(signal.SIGTERM)
        try:
            _process.wait(timeout=10)  # allow time for HDF5 finalization
        except subprocess.TimeoutExpired:
            _process.kill()
            _process.wait(timeout=2)
        _process = None
        return jsonify({"ok": True})
    # Process ref exists but already exited — clean up
    if _process is not None:
        _process = None
    return jsonify({"ok": True, "message": "No process running"})


@app.route("/api/logs")
def logs():
    """Return recent process output."""
    n = request.args.get("n", 50, type=int)
    return jsonify({"lines": _process_log[-n:]})


@app.route("/api/restart", methods=["POST"])
def restart():
    """Restart the pi_api process (systemd Restart=always respawns it). Graceful SIGTERM first,
    then FORCE-exit if still alive after 2s: with the display up, SDL (pygame) can swallow SIGTERM,
    which used to leave stale code running and make Deploy / Restart-API silently no-op."""
    def _shutdown():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)   # graceful
        time.sleep(2.0)
        os._exit(0)                            # forced: SIGTERM was swallowed (e.g. SDL)

    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"ok": True, "message": "Restarting..."})


@app.route("/api/release_devices", methods=["POST"])
def release_devices():
    """Close all pi_api-managed device instances (before engine takes over)."""
    released = []
    # _camera_lock OUTSIDE _devices_lock (consistent order) so closing the camera here can't
    # race a concurrent camera_preview_start/stop and leave libcamera in a bad state.
    with _camera_lock:
        with _devices_lock:
            for name, dev in list(_devices.items()):
                try:
                    if hasattr(dev, 'stop_stream'):
                        dev.stop_stream()
                    if hasattr(dev, 'close'):
                        dev.close()
                    released.append(name)
                except Exception as e:
                    released.append(f"{name} (error: {e})")
            _devices.clear()
    _lick_events.clear()
    _photodiode_events.clear()
    _reward_events.clear()
    _encoder_events.clear()
    return jsonify({"ok": True, "released": released})


def _release_one(name):
    """Release and drop a single stored device BEFORE a re-init reclaims its line/bus.
    Without this, re-initializing a persistent device (the per-card Reinit button, or a
    second Init Devices without a pi_api restart) leaves the old instance still holding the
    GPIO line — the new claim then fails 'GPIO busy' (lgpio, exclusive) or silently orphans
    the old handle/thread (I2C). Mirrors release_devices' stop_stream()->close() order."""
    with _devices_lock:
        dev = _devices.pop(name, None)
        if dev is not None:
            try:
                if hasattr(dev, 'stop_stream'):
                    dev.stop_stream()
                if hasattr(dev, 'close'):
                    dev.close()
            except Exception:
                pass


@app.route("/api/generate_stims", methods=["POST"])
def generate_stims():
    """Leader only. Generate stimuli from task config + warp map."""
    try:
        data = request.json
        task_path = data.get("task_config")
        session_id = data.get("session_id")

        # Load task config (path is relative to ~/rig/)
        import yaml
        full_path = RIG_DIR / task_path
        with open(full_path) as f:
            task_config = yaml.safe_load(f)

        # Load warp map if available and apply_warp is enabled
        import numpy as np
        warp_map = None
        apply_warp = data.get("apply_warp", False)
        warp_warning = None
        if apply_warp:
            warp_path = Path.home() / "rig" / "calibration" / "warp_map.npz"
            if warp_path.exists():
                warp_map = np.load(str(warp_path))
            else:
                warp_warning = f"apply_warp=True but warp_map.npz not found at {warp_path}. Using centered fallback."

        # Import and run generator
        sys.path.insert(0, str(RIG_DIR))
        from shared.stim_generator import generate_stimuli

        subj, subj_date, _ = _parse_session_id(session_id)
        if subj:
            stim_dir = DATA_DIR / subj / subj_date / session_id
        else:
            stim_dir = DATA_DIR / session_id
        stim_dir.mkdir(parents=True, exist_ok=True)
        contrast_metric = data.get("contrast_metric", "weber")
        arrays = generate_stimuli(task_config, warp_map, str(stim_dir),
                                  contrast_metric=contrast_metric)

        result = {
            "ok": True,
            "n_trials": int(arrays["n_trials"][0]),
            "npz_path": str((stim_dir / "stimuli.npz").relative_to(Path.home())),
        }
        if warp_warning:
            result["warning"] = warp_warning
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/push_stims", methods=["POST"])
def push_stims():
    """Leader only. Upload stim NPZ to a Follower Pi."""
    data = request.json
    follower_ip = data["follower_ip"]
    session_id = data["session_id"]
    api_port = data.get("api_port", 5080)

    subj, subj_date, _ = _parse_session_id(session_id)
    if subj:
        npz_path = DATA_DIR / subj / subj_date / session_id / "stimuli.npz"
    else:
        npz_path = DATA_DIR / session_id / "stimuli.npz"
    if not npz_path.exists():
        return jsonify({"ok": False, "error": "Stim NPZ not found"}), 404

    # Upload to follower via its REST API
    import requests as req
    with open(npz_path, "rb") as f:
        r = req.post(f"http://{follower_ip}:{api_port}/api/upload",
                     files={"file": f},
                     data={"path": f"stims/{session_id}/stimuli.npz"},
                     timeout=10)
    return jsonify({"ok": True, "follower_response": r.json()})


@app.route("/api/calibrate", methods=["POST"])
def calibrate():
    """Run device calibration. Returns calibration data."""
    data = request.json
    device_name = data.get("device")
    params = data.get("params", {})

    # Import device classes
    sys.path.insert(0, str(RIG_DIR))
    from devices.base import DEVICE_REGISTRY

    if device_name not in DEVICE_REGISTRY:
        return jsonify({"ok": False, "error": f"Unknown device: {device_name}"}), 400

    # For reward calibration, use the calibration module
    if device_name == "reward":
        from devices.reward_calibration import run_calibration
        from devices.base import DEVICE_REGISTRY
        cls = DEVICE_REGISTRY["reward"]
        dev = cls()
        # Load rig config for pin setup
        rig_path = RIG_DIR / "rigs" / "cheese.yaml"
        if rig_path.exists():
            import yaml
            with open(rig_path) as f:
                rig = yaml.safe_load(f)
            dev_config = rig["devices"].get("reward", {})
            dev.init(rig_config=dev_config, task_params={})
        results = run_calibration(dev, **params)
        dev.close()
        return jsonify({"ok": True, "results": results})

    return jsonify({"ok": False, "error": "Calibration not implemented for this device"}), 400


# ── Device control endpoints ──


def _ensure_rig_path():
    sys.path.insert(0, str(RIG_DIR))


# Display command queue — pygame requires all calls on a single thread
import queue

_display_cmd_q: queue.Queue = queue.Queue()
_display_result_q: queue.Queue = queue.Queue()
_display_thread: threading.Thread | None = None
_sync_test_stop = threading.Event()   # set to stop the photodiode sync-square flash test


def _display_thread_fn():
    """Dedicated thread for all pygame/display operations."""
    _ensure_rig_path()
    from devices.display import Display
    dev = None

    while True:
        cmd = _display_cmd_q.get()
        if cmd is None:
            break
        action = cmd.get("action")
        try:
            if action == "init":
                os.environ["DISPLAY"] = ":0"
                dev = Display()
                dev.init(rig_config=cmd.get("rig_config", {}), task_params={})
                dev.start_display()
                # Load warp map if available
                warp_path = Path.home() / "rig" / "calibration" / "warp_map.npz"
                warp_loaded = dev.load_warp(str(warp_path))
                with _devices_lock:
                    _devices["display"] = dev
                msg = "Display initialized"
                if warp_loaded:
                    msg += " (warp map loaded)"
                _display_result_q.put({"ok": True, "message": msg})
            elif action == "blank":
                if not dev:
                    _display_result_q.put({"ok": False, "error": "Display not initialized"})
                else:
                    dev.blank_with_gray(cmd.get("gray_value", 0.0))
                    _display_result_q.put({"ok": True})
            elif action == "checkers":
                if not dev:
                    _display_result_q.put({"ok": False, "error": "Display not initialized"})
                else:
                    use_warp = cmd.get("apply_warp", True)
                    dev.show_checkers(use_warp=use_warp)
                    _display_result_q.put({"ok": True})
            elif action == "stimulus":
                if not dev:
                    _display_result_q.put({"ok": False, "error": "Display not initialized"})
                else:
                    dev.show_patch_spherical(
                        cmd.get("az_deg", 0.0), cmd.get("alt_deg", 0.0),
                        cmd.get("size_deg", 8.0), cmd.get("corr_contrast", 0.5),
                        cmd.get("bg_gray", 0.0), shape=cmd.get("shape", "square"),
                        apply_lum=cmd.get("apply_lum", True))
                    _display_result_q.put({"ok": True})
            elif action == "reload_warp":
                # Re-read warp_map.npz into the running Display so a freshly Generated warp
                # takes effect WITHOUT a full re-init (load_warp caches self._warp in memory
                # and never re-reads the file on its own).
                warp_path = Path.home() / "rig" / "calibration" / "warp_map.npz"
                if not dev:
                    _display_result_q.put({"ok": True, "reloaded": False,
                                           "message": "display not active"})
                else:
                    loaded = dev.load_warp(str(warp_path))
                    _display_result_q.put({"ok": True, "reloaded": bool(loaded),
                                           "message": ("warp reloaded" if loaded
                                                       else "warp_map.npz not found")})
            elif action == "shutdown":
                if dev:
                    dev.shutdown()
                    dev = None
                    with _devices_lock:
                        _devices.pop("display", None)
                _display_result_q.put({"ok": True})
            elif action == "sync_test":
                if not dev or getattr(dev, "_screen", None) is None:
                    _display_result_q.put({"ok": False, "error": "Display not initialized"})
                else:
                    # Ack BEFORE the (blocking) flash loop so the HTTP start returns immediately;
                    # the loop then owns this thread until _sync_test_stop is set out-of-band.
                    _sync_test_stop.clear()
                    if hasattr(dev, "set_sync_layout"):   # move the square to the current corner/size live
                        dev.set_sync_layout(cmd.get("sync_corner"), cmd.get("sync_size_px"), cmd.get("sync_brightness"))
                    _display_result_q.put({"ok": True, "message": "sync test running"})
                    try:
                        dev.run_sync_test(int(cmd.get("every_n", 5)), _sync_test_stop)
                    except Exception as e:
                        print("[display] sync_test error:", e, flush=True)
                    # result already sent; do NOT put again (would desync the result queue)
            else:
                _display_result_q.put({"ok": False, "error": f"Unknown action: {action}"})
        except Exception as e:
            _display_result_q.put({"ok": False, "error": str(e)})


def _display_command(cmd: dict, timeout: float = 10.0) -> dict:
    """Send a command to the display thread and wait for result."""
    global _display_thread
    if _display_thread is None or not _display_thread.is_alive():
        _display_thread = threading.Thread(target=_display_thread_fn, daemon=True)
        _display_thread.start()
    _display_cmd_q.put(cmd)
    try:
        return _display_result_q.get(timeout=timeout)
    except queue.Empty:
        return {"ok": False, "error": "Display command timed out"}


@app.route("/api/init_projector", methods=["POST"])
def init_projector():
    """Run start_projector.sh (GPIO ALT2, DLPC3436 init, X11)."""
    script = RIG_DIR / "start_projector.sh"
    if not script.exists():
        return jsonify({"ok": False, "error": "start_projector.sh not found"}), 404
    try:
        r = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return jsonify({"ok": True, "message": "Projector initialized"})
        else:
            detail = (r.stderr.strip() or r.stdout.strip() or
                      f"start_projector.sh exited with code {r.returncode}")
            return jsonify({
                "ok": False,
                "error": f"Projector failed to initialize: {detail[-200:]}",
            }), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timeout (30s)"}), 504


@app.route("/api/init_display", methods=["POST"])
def init_display():
    """Initialize pygame display via dedicated thread."""
    data = request.json or {}
    rig_cfg = data.get("rig_config", {"resolution": [1920, 1080]})
    result = _display_command({"action": "init", "rig_config": rig_cfg}, timeout=25)
    code = 200 if result.get("ok") else 500
    return jsonify(result), code


@app.route("/api/blank_display", methods=["POST"])
def blank_display():
    """Blank display with given gray value."""
    data = request.json or {}
    gray = data.get("gray_value", 0.0)
    result = _display_command({"action": "blank", "gray_value": gray})
    code = 200 if result.get("ok") else 500
    return jsonify(result), code


@app.route("/api/test_checkers", methods=["POST"])
def test_checkers():
    """Show checkerboard pattern on display."""
    data = request.json or {}
    result = _display_command({"action": "checkers", "apply_warp": data.get("apply_warp", True)})
    code = 200 if result.get("ok") else 500
    return jsonify(result), code


@app.route("/api/test_stimulus", methods=["POST"])
def test_stimulus():
    """Draw one test patch (square/circle) at az/alt with a contrast on the given bg.
    Contrast arrives in the rig's active metric; convert it to the normalized render
    fraction (same mapping generation uses) so the value matches the experiment card."""
    data = request.json or {}
    bg = float(data.get("bg_gray", 0.0))
    contrast = float(data.get("contrast", 0.5))
    metric = data.get("contrast_metric", "weber")
    try:
        _ensure_rig_path()
        from shared.stim_generator import metric_to_fraction
        frac = float(metric_to_fraction(contrast, bg, metric))
    except Exception:
        frac = contrast   # fall back to raw fraction if the converter isn't importable
    frac = max(0.0, min(1.0, frac))
    result = _display_command({
        "action": "stimulus",
        "az_deg": float(data.get("az_deg", 0.0)),
        "alt_deg": float(data.get("alt_deg", 0.0)),
        "size_deg": float(data.get("size_deg", 8.0)),
        "corr_contrast": frac,
        "bg_gray": bg,
        "shape": data.get("shape", "square"),
        # Intensity-cal measurement sends apply_lum:false to render RAW drive (so the meter reads
        # the uncorrected delivered luminance the correction is fit from). Normal tests correct.
        "apply_lum": bool(data.get("apply_lum", True)),
    })
    code = 200 if result.get("ok") else 500
    return jsonify(result), code


@app.route("/api/reload_warp", methods=["POST"])
def reload_warp():
    """Re-read warp_map.npz into the already-running display (after Generate Warp), without
    a full re-init. Returns reloaded=False (still ok) if the display thread isn't active —
    it will pick up the new warp on the next init_display."""
    result = _display_command({"action": "reload_warp"})
    code = 200 if result.get("ok") else 500
    return jsonify(result), code


@app.route("/api/shutdown_display", methods=["POST"])
def shutdown_display():
    """Shut down pygame display (release for follower.py)."""
    result = _display_command({"action": "shutdown"})
    code = 200 if result.get("ok") else 500
    return jsonify(result), code


@app.route("/api/init_lick", methods=["POST"])
def init_lick():
    """Initialize lick sensor (MPR121 I2C)."""
    _ensure_rig_path()
    from devices.lick_sensor import LickSensor
    data = request.json or {}
    rig_cfg = {
        "i2c_address": data.get("i2c_address", "0x5A"),
        "electrode": data.get("electrode", 4),
        "i2c_bus": data.get("i2c_bus", 1),
    }
    _release_one("lick_sensor")   # free any prior instance so this (re)init can re-claim the bus
    try:
        dev = LickSensor()
        dev.init(rig_config=rig_cfg, task_params={})
        with _devices_lock:
            _devices["lick_sensor"] = dev
        check = dev.check()
        return jsonify({"ok": True, "message": check.get("message", "OK")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/monitor_lick", methods=["POST"])
def monitor_lick():
    """Start lick monitoring (polling + event collection)."""
    global _lick_events
    with _devices_lock:
        dev = _devices.get("lick_sensor")
    if not dev:
        return jsonify({"ok": False, "error": "Lick sensor not initialized"}), 400
    _lick_events = []

    def on_lick(evt):
        _lick_events.append(evt)
        # Keep last 30 seconds (~6000 events max at 200Hz)
        if len(_lick_events) > 6000:
            _lick_events.pop(0)

    dev.start_stream(on_lick)
    return jsonify({"ok": True})


@app.route("/api/stop_monitor_lick", methods=["POST"])
def stop_monitor_lick():
    """Stop lick monitoring."""
    with _devices_lock:
        dev = _devices.get("lick_sensor")
    if dev:
        dev.stop_stream()
    return jsonify({"ok": True})


@app.route("/api/lick_data")
def lick_data():
    """Return collected lick events (polled by UI)."""
    return jsonify({"events": _lick_events})


@app.route("/api/init_encoder", methods=["POST"])
def init_encoder():
    """Initialize the running-wheel encoder (AS5600 magnetic angle sensor, I2C)."""
    _ensure_rig_path()
    from devices.encoder import Encoder
    data = request.json or {}
    rig_cfg = {
        "i2c_address": data.get("i2c_address", "0x36"),
        "i2c_bus": data.get("i2c_bus", 1),
        "wheel_diameter_cm": data.get("wheel_diameter_cm", 15.0),
        "sample_hz": data.get("sample_hz", 100),
    }
    _release_one("encoder")   # free any prior instance so this (re)init can re-claim the bus
    try:
        dev = Encoder()
        dev.init(rig_config=rig_cfg, task_params={})
        with _devices_lock:
            _devices["encoder"] = dev
        check = dev.check()
        return jsonify({"ok": True, "message": check.get("message", "OK")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/monitor_encoder", methods=["POST"])
def monitor_encoder():
    """Start running-wheel monitoring (setup-UI live speed view)."""
    global _encoder_events
    with _devices_lock:
        dev = _devices.get("encoder")
    if not dev:
        return jsonify({"ok": False, "error": "Encoder not initialized"}), 400
    _encoder_events = []

    def on_run(evt):
        _encoder_events.append(evt)
        if len(_encoder_events) > 3000:
            _encoder_events.pop(0)

    dev.start_stream(on_run)
    return jsonify({"ok": True})


@app.route("/api/stop_monitor_encoder", methods=["POST"])
def stop_monitor_encoder():
    """Stop running-wheel monitoring."""
    with _devices_lock:
        dev = _devices.get("encoder")
    if dev:
        dev.stop_stream()
    return jsonify({"ok": True})


@app.route("/api/encoder_data")
def encoder_data():
    """Return collected running-wheel samples (polled by the setup UI)."""
    return jsonify({"events": _encoder_events})


@app.route("/api/init_camera", methods=["POST"])
def init_camera():
    """Check if a camera is detected."""
    _ensure_rig_path()
    from devices.camera import Camera
    try:
        dev = Camera()
        check = dev.check()
        if check.get("ok"):
            return jsonify({"ok": True, "message": check.get("message", "OK")})
        else:
            return jsonify({"ok": False, "error": check.get("message", "No camera")}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/init_reward", methods=["POST"])
def init_reward():
    """Initialize reward: claim/verify GPIO pins via lgpio."""
    data = request.json or {}
    pins = data.get("pins", {"main": {"gpio": 18}})
    try:
        import lgpio
        chip = lgpio.gpiochip_open(0)   # 40-pin header = gpiochip0 on Pi 5 (current OS) and Pi 4
        try:
            results = []
            for name, pin_cfg in pins.items():
                gpio = pin_cfg.get("gpio", 18)
                lgpio.gpio_claim_output(chip, gpio, 0)   # claim output, drive low
                lgpio.gpio_read(chip, gpio)
                results.append(f"{name}(GPIO{gpio})=OK")
        finally:
            lgpio.gpiochip_close(chip)   # release the handle even if a claim raised
        return jsonify({"ok": True, "message": ", ".join(results)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/init_photodiode", methods=["POST"])
def init_photodiode():
    """Initialize photodiode: create persistent device instance."""
    _ensure_rig_path()
    from devices.photodiode import Photodiode
    data = request.json or {}
    gpio = data.get("gpio", 24)
    _release_one("photodiode")   # free the old line so this (re)init can re-claim it (lgpio is exclusive)
    try:
        dev = Photodiode()
        dev.init(rig_config={
            "gpio": gpio,
            "glitch_enabled": data.get("glitch_enabled", True),
            "glitch_ms": data.get("glitch_ms", 0.5),
            "debounce_enabled": data.get("debounce_enabled", True),
            "debounce_ms": data.get("debounce_ms", 5),
        }, task_params={})
        with _devices_lock:
            _devices["photodiode"] = dev
        check = dev.check()
        return jsonify({"ok": True, "message": check.get("message", "OK")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/init_calibration_probe", methods=["POST"])
def init_calibration_probe():
    """Initialize the calibration probe: create a persistent GPIO-latch device."""
    _ensure_rig_path()
    from devices.calibration_probe import CalibrationProbe
    data = request.json or {}
    gpio = data.get("gpio", 22)
    _release_one("calibration_probe")   # free the old line so this (re)init can re-claim it (lgpio is exclusive)
    try:
        dev = CalibrationProbe()
        dev.init(rig_config={"gpio": gpio}, task_params={})
        with _devices_lock:
            _devices["calibration_probe"] = dev
        check = dev.check()
        return jsonify({"ok": True, "message": check.get("message", "OK")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/camera_preview_start", methods=["POST"])
def camera_preview_start():
    """Start camera MJPEG preview (or session recording if session_id given)."""
    data = request.get_json(silent=True) or {}   # tolerate a body-less POST (no 415)
    # Serialize the whole check→pop→construct so two concurrent starts can't both open a
    # Picamera2 and hang libcamera.
    with _camera_lock:
        # Don't restart if already recording (e.g. session recording started by Go)
        with _devices_lock:
            existing = _devices.get("camera")
        if existing and existing._recording:
            return jsonify({"ok": True, "message": "Camera already recording"})
        # Tear down any stale (non-recording) camera before reopening. Constructing a new
        # Picamera2 while a previous sensor handle lingers hangs libcamera — this is what froze
        # the camera on res/fps changes.
        with _devices_lock:
            stale = _devices.pop("camera", None)
        if stale is not None:
            try:
                stale.close()   # close() settles internally so libcamera releases the sensor
            except Exception:
                pass
        _ensure_rig_path()
        from devices.camera import Camera
        rig_cfg = {
            "resolution": data.get("resolution", [1280, 720]),
            "fps": data.get("fps", 50),
            "bitrate_mbps": data.get("bitrate_mbps", 8),
            "auto_exposure": data.get("auto_exposure", True),
            "exposure_ms": data.get("exposure_ms", 10),
            "gain": data.get("gain", 1.0),
            "live_preset": data.get("live_preset", "med"),
        }
        # A real session_id records to disk; "preview"/none is a setup-UI live view — no file, and
        # a faithful full-fps preview (start_preview runs with no encoder to starve).
        session_id = data.get("session_id")
        recording = bool(session_id and session_id != "preview")
        dev = None
        try:
            dev = Camera()
            dev.init(rig_config=rig_cfg, task_params={})
            if recording:
                video_dir = data.get("video_dir", "/media/vruser/ssd/video")
                _EXTRA_DOWNLOAD_DIRS.add(Path(video_dir))   # transfer may pull from here
                subj, subj_date, _ = _parse_session_id(session_id)
                output_dir = str(Path(video_dir) / subj / subj_date)
                dev.start_recording(session_id=session_id, output_dir=output_dir)
            else:
                dev.start_preview(downsample=bool(data.get("downsample", False)))
            with _devices_lock:
                _devices["camera"] = dev
            return jsonify({"ok": True})
        except Exception as e:
            # Never leave a half-opened camera outside _devices — it would hang the next open.
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
            return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/camera_stream")
def camera_stream():
    """MJPEG stream from camera."""
    from flask import Response
    with _devices_lock:
        dev = _devices.get("camera")
    if not dev:
        return jsonify({"ok": False, "error": "Camera not streaming"}), 400
    return Response(dev.mjpeg_stream(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/camera_preview_stop", methods=["POST"])
def camera_preview_stop():
    """Stop camera preview. Refuses to stop an active SESSION recording unless force=True, so a
    setup-UI Reinit/Stop can't silently truncate a running experiment's video — only the
    experiment UI's own end-of-session stop passes force=True."""
    data = request.get_json(silent=True) or {}   # tolerate a body-less POST (no 415)
    force = bool(data.get("force", False))
    with _camera_lock:
        with _devices_lock:
            dev = _devices.get("camera")
        if (dev is not None and getattr(dev, "_recording", False)
                and not getattr(dev, "_is_preview", True) and not force):
            return jsonify({"ok": True,
                            "message": "Session recording in progress; not stopped"})
        with _devices_lock:
            dev = _devices.pop("camera", None)
        if dev:
            dev.stop_recording()
            dev.close()
    return jsonify({"ok": True})


@app.route("/api/camera_controls", methods=["POST"])
def camera_controls():
    """Apply live exposure/gain to the running camera (setup-UI banding tuning). Serialized with
    start/stop via _camera_lock so we never set controls on a camera being torn down."""
    data = request.json or {}
    with _camera_lock:
        with _devices_lock:
            dev = _devices.get("camera")
        if dev is None or getattr(dev, "_cam", None) is None:
            return jsonify({"ok": False, "error": "Camera not running"}), 400
        # Never change exposure/gain mid-experiment: a real session recording must not be mutated
        # by stale setup-UI tuning (mirrors camera_preview_stop's session guard).
        if getattr(dev, "_recording", False) and not getattr(dev, "_is_preview", True):
            return jsonify({"ok": False,
                            "error": "Session recording in progress; controls locked"}), 409
        try:
            applied = dev.apply_exposure(
                auto_exposure=data.get("auto_exposure"),
                exposure_ms=data.get("exposure_ms"),
                gain=data.get("gain"),
            )
            return jsonify({"ok": True, "applied": {k: str(v) for k, v in applied.items()}})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500


def _find_camera_i2c():
    """Locate the bound CSI sensor's i2c driver + device node, e.g. ('/sys/.../imx477', '10-001a').
    Rig-agnostic: scans every imx* driver for a bound device (a symlink named bus-addr). Returns
    (driver_dir, node) or (None, None)."""
    import glob
    for drv in sorted(glob.glob("/sys/bus/i2c/drivers/imx*")):
        for node in sorted(glob.glob(drv + "/*-*")):   # device nodes are symlinks like 10-001a
            if os.path.islink(node):
                return drv, os.path.basename(node)
    return None, None


@app.route("/api/camera_reset", methods=["POST"])
def camera_reset():
    """Recover a wedged CSI camera by rebinding its i2c sensor driver — the escalation when a soft
    reinit can't clear a libcamera hang (capture stuck mid-frame). Closes any open Picamera2 first
    (under _camera_lock), then unbind+bind the sensor node so the next open re-probes a clean
    sensor. Needs root to write bind/unbind; pi_api runs as vruser with passwordless sudo. Refuses
    while a real session recording is live so a stray click can't kill an experiment's video."""
    with _camera_lock:
        with _devices_lock:
            dev = _devices.get("camera")
        if (dev is not None and getattr(dev, "_recording", False)
                and not getattr(dev, "_is_preview", True)):
            return jsonify({"ok": False,
                            "error": "Session recording in progress; not reset"}), 409
        with _devices_lock:
            dev = _devices.pop("camera", None)
        if dev is not None:
            try:
                dev.close()   # settles internally so libcamera releases (or tries to) before unbind
            except Exception:
                pass
        drv, node = _find_camera_i2c()
        if not node:
            return jsonify({"ok": False, "error": "no bound imx camera i2c node found"}), 500
        try:
            subprocess.run(["sudo", "sh", "-c", f"echo {node} > {drv}/unbind"],
                           check=True, capture_output=True, text=True, timeout=10)
            time.sleep(0.5)
            subprocess.run(["sudo", "sh", "-c", f"echo {node} > {drv}/bind"],
                           check=True, capture_output=True, text=True, timeout=10)
            time.sleep(1.0)   # let the sensor re-probe before the next Picamera2() open
        except subprocess.CalledProcessError as e:
            return jsonify({"ok": False,
                            "error": (e.stderr or str(e)).strip()}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "driver": os.path.basename(drv), "node": node})


@app.route("/api/monitor_photodiode", methods=["POST"])
def monitor_photodiode():
    """Start photodiode pulse monitoring (rising edge detection)."""
    global _photodiode_events
    _ensure_rig_path()
    from devices.photodiode import Photodiode

    data = request.json or {}
    gpio = data.get("gpio", 24)

    # Create persistent device if not already initialized
    with _devices_lock:
        dev = _devices.get("photodiode")
    if not dev:
        try:
            dev = Photodiode()
            dev.init(rig_config={
                "gpio": gpio,
                "glitch_enabled": data.get("glitch_enabled", True),
                "glitch_ms": data.get("glitch_ms", 0.5),
                "debounce_enabled": data.get("debounce_enabled", True),
                "debounce_ms": data.get("debounce_ms", 5),
            }, task_params={})
            with _devices_lock:
                _devices["photodiode"] = dev
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        # Reusing an already-inited device: a prior raw scope may have left the daemon glitch
        # filter at 0. Re-assert the configured steady filter so the detected monitor is correct.
        try:
            dev._pi.set_glitch_filter(dev.gpio, getattr(dev, "_glitch_us", 0))
        except Exception:
            pass

    _photodiode_events = []

    def on_pulse(evt):
        _photodiode_events.append(evt)
        if len(_photodiode_events) > 6000:
            _photodiode_events.pop(0)

    dev.start_stream(on_pulse)
    return jsonify({"ok": True})


@app.route("/api/stop_monitor_photodiode", methods=["POST"])
def stop_monitor_photodiode():
    """Stop photodiode pulse monitoring."""
    with _devices_lock:
        dev = _devices.get("photodiode")
    if dev:
        dev.stop_stream()
    return jsonify({"ok": True})


@app.route("/api/probe_on", methods=["POST"])
def probe_on():
    """Latch the calibration probe GPIO HIGH."""
    with _devices_lock:
        dev = _devices.get("calibration_probe")
    if dev is None:
        return jsonify({"ok": False, "error": "calibration probe not initialized"}), 400
    dev.set_state(True)
    return jsonify({"ok": True, "high": True})


@app.route("/api/probe_off", methods=["POST"])
def probe_off():
    """Latch the calibration probe GPIO LOW."""
    with _devices_lock:
        dev = _devices.get("calibration_probe")
    if dev is None:
        return jsonify({"ok": False, "error": "calibration probe not initialized"}), 400
    dev.set_state(False)
    return jsonify({"ok": True, "high": False})


@app.route("/api/photodiode_data")
def photodiode_data():
    """Return collected photodiode pulse events (polled by UI)."""
    return jsonify({"events": _photodiode_events})


@app.route("/api/photodiode_raw_start", methods=["POST"])
def photodiode_raw_start():
    """Start RAW transition capture (both edges, glitch filter OFF) for the setup-UI diagnostic
    scope. Reuses the initialized photodiode device; lazily inits from the posted config if needed."""
    global _photodiode_raw
    _ensure_rig_path()
    data = request.json or {}
    with _devices_lock:
        dev = _devices.get("photodiode")
    if not dev:
        from devices.photodiode import Photodiode
        try:
            dev = Photodiode()
            dev.init(rig_config={
                "gpio": data.get("gpio", 24),
                "glitch_enabled": data.get("glitch_enabled", True),
                "glitch_ms": data.get("glitch_ms", 0.5),
                "debounce_enabled": data.get("debounce_enabled", True),
                "debounce_ms": data.get("debounce_ms", 5),
            }, task_params={})
            with _devices_lock:
                _devices["photodiode"] = dev
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    _photodiode_raw = []

    def on_tr(evt):
        _photodiode_raw.append(evt)
        if len(_photodiode_raw) > 20000:      # cap: keep the most recent transitions
            del _photodiode_raw[:len(_photodiode_raw) - 20000]

    try:
        dev.start_raw_capture(on_tr)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/photodiode_raw_data")
def photodiode_raw_data():
    """Return-and-clear raw transitions buffered since the last poll (the scope accumulates them
    client-side into a rolling window)."""
    global _photodiode_raw
    buf = _photodiode_raw
    _photodiode_raw = []
    return jsonify({"ok": True, "transitions": buf})


@app.route("/api/photodiode_raw_stop", methods=["POST"])
def photodiode_raw_stop():
    """Stop raw capture and restore the device's configured glitch filter."""
    with _devices_lock:
        dev = _devices.get("photodiode")
    if dev:
        try:
            dev.stop_raw_capture()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/photodiode_test_start", methods=["POST"])
def photodiode_test_start():
    """Flash the red photodiode sync square every N frames on THIS Pi's display (the follower),
    simulating a session's sync pulses so the photodiode/scope can be exercised from the setup UI.
    Runs in the display thread; stop via /api/photodiode_test_stop."""
    data = request.json or {}
    every_n = int(data.get("every_n", 5))
    with _devices_lock:
        dev = _devices.get("display")
    if not dev or getattr(dev, "_screen", None) is None:
        return jsonify({"ok": False, "error": "Display not initialized on this Pi"}), 400
    _sync_test_stop.set()      # stop any prior run and let the display thread return to the queue
    time.sleep(0.05)
    cmd = {"action": "sync_test", "every_n": every_n}
    for k in ("sync_corner", "sync_size_px", "sync_brightness"):   # apply the current controls live, no re-deploy needed
        if k in data:
            cmd[k] = data[k]
    return jsonify(_display_command(cmd, timeout=5))


@app.route("/api/photodiode_test_stop", methods=["POST"])
def photodiode_test_stop():
    """Stop the sync-square flash test (sets the stop Event out-of-band from the command queue)."""
    _sync_test_stop.set()
    return jsonify({"ok": True})


@app.route("/api/deliver_reward", methods=["POST"])
def deliver_reward():
    """Deliver a single reward pulse and log the event."""
    data = request.json or {}
    duration_ms = data.get("duration_ms", 10)
    gpio = data.get("gpio", 18)
    try:
        import lgpio
        import time
        import threading
        chip = lgpio.gpiochip_open(0)   # 40-pin header = gpiochip0 on Pi 5 (current OS) and Pi 4
        lgpio.gpio_claim_output(chip, gpio, 0)

        t_fire = time.time()
        _reward_events.append({"event": "reward", "t": t_fire, "duration_ms": duration_ms})
        if len(_reward_events) > 6000:
            _reward_events.pop(0)

        def pulse():
            try:
                lgpio.gpio_write(chip, gpio, 1)
                time.sleep(duration_ms / 1000.0)
            finally:
                # always close the valve + release the gpiochip, even if a write/sleep raised
                try:
                    lgpio.gpio_write(chip, gpio, 0)
                except Exception:
                    pass
                try:
                    lgpio.gpiochip_close(chip)
                except Exception:
                    pass

        threading.Thread(target=pulse, daemon=True).start()
        return jsonify({"ok": True, "duration_ms": duration_ms, "gpio": gpio})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/reward_data")
def reward_data():
    """Return collected reward pulse events (polled by UI)."""
    return jsonify({"events": _reward_events})


# ── Main ──

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="VRFarm Pi API")
    parser.add_argument("--port", type=int, default=5080)
    args = parser.parse_args()
    app.run(host="0.0.0.0", port=args.port, threaded=True)
