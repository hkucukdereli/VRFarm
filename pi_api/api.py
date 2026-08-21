"""
pi_api/api.py

Lightweight Flask REST API running on each Pi (port 5080).
Handles: status, file upload/download, process start/stop, data transfer,
and a GENERIC device surface — devices self-register via @register_device
(module name == device type), so adding a device needs no new endpoints:
  /api/init_device      {name, type, config}
  /api/monitor_device   {name}   /api/stop_monitor_device {name}
  /api/device_data?name=
Camera-like devices (anything implementing start_preview/start_recording/
mjpeg_stream) are driven through the device-name-keyed camera endpoints.
"""

from __future__ import annotations
import importlib
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
# shepherd health-monitor logs (matches shepherd/config.yaml output_dir default). The controller
# mirrors the latest of these to its data dir at transfer time and prunes old ones here.
SHEPHERD_LOG_DIR = Path.home() / "shepherd_logs"

# Currently running process (the leader engine)
_process: subprocess.Popen | None = None
_process_log: list[str] = []

# Persistent device instances (survive across requests)
_devices: dict = {}  # name -> initialized Device instance
_devices_lock = threading.Lock()
# Per-device open/close serialization for camera-like devices, so two concurrent
# preview_start/stop calls on the same device can't both open the capture pipeline.
# Always acquire a camera lock OUTSIDE _devices_lock (never the reverse).
_cam_locks: dict = {}
_cam_locks_guard = threading.Lock()
# Setup-UI live-monitor buffers, keyed by device name
_monitor_events: dict = {}


def _cam_lock(name: str) -> threading.Lock:
    with _cam_locks_guard:
        return _cam_locks.setdefault(name, threading.Lock())


def _ensure_rig_path():
    if str(RIG_DIR) not in sys.path:
        sys.path.insert(0, str(RIG_DIR))


def _load_device_class(dev_type: str):
    """Import devices/<type>.py (self-registers via @register_device) and return the class."""
    _ensure_rig_path()
    from devices.base import DEVICE_REGISTRY
    if dev_type not in DEVICE_REGISTRY:
        importlib.import_module(f"devices.{dev_type}")
    return DEVICE_REGISTRY[dev_type]


def _is_session_recording(dev) -> bool:
    """A real SESSION recording is writing (not a throwaway preview)."""
    return bool(getattr(dev, "_recording", False)
                and not getattr(dev, "_is_preview", True))


@app.route("/api/status")
def status():
    """Health check: running process, disk space, hostname, live session recording."""
    disk = shutil.disk_usage(str(Path.home()))
    # cameras = every stored device that looks like a camera (has _recording).
    # camera_recording/camera_frames stay as any/sum aggregates: the controller checks
    # camera_recording before a Transfer (downloading a video that is still being written
    # silently produces a truncated copy), and shepherd derives live encode fps from
    # camera_frames.
    cams = {}
    with _devices_lock:
        for name, dev in _devices.items():
            if hasattr(dev, "_recording"):
                rec = _is_session_recording(dev)
                cams[name] = {"recording": rec,
                              "frames": getattr(dev, "_frame_idx", None) if rec else None}
    any_rec = any(c["recording"] for c in cams.values())
    frames = sum(c["frames"] or 0 for c in cams.values()) if any_rec else None
    return jsonify({
        "ok": True,
        "hostname": os.uname().nodename,
        "process_running": _process is not None and _process.poll() is None,
        "cameras": cams,
        "camera_recording": any_rec,
        "camera_frames": frames,
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


ALLOWED_DOWNLOAD_DIRS = [RIG_DIR, DATA_DIR, SHEPHERD_LOG_DIR, Path("/media/vruser/ssd/video")]
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
    """List session data files available for transfer. The controller passes the rig's
    data.leader_dir (?data_dir=...) so a non-default location still transfers; falls
    back to ~/data."""
    files = []
    home = Path.home()
    subj, subj_date, _ = _parse_session_id(session_id)

    dd = request.args.get("data_dir", "").strip()
    data_base = Path(dd).expanduser() if dd else DATA_DIR
    if dd:
        _EXTRA_DOWNLOAD_DIRS.add(data_base)   # so /api/download accepts it
    # Check data dir (nested: subject/subject_date/session_id)
    for candidate in [
        data_base / subj / subj_date / session_id if subj else None,
        data_base / session_id,  # legacy flat layout
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
    # Sizes let the controller verify each download landed whole. Returned as a SEPARATE map so
    # `files` keeps its list-of-strings shape and an older controller still works unchanged.
    sizes = {}
    for rel in files:
        try:
            fp = Path(rel)
            sizes[rel] = (fp if fp.is_absolute() else home / rel).stat().st_size
        except OSError:
            pass
    return jsonify({"files": files, "sizes": sizes})


@app.route("/api/consolidate/<session_id>", methods=["POST"])
def consolidate(session_id):
    """Leader only. Fold the session's sidecars (metadata.yaml, frame_timestamps.npy, ...)
    into <session_id>.h5 and delete them, leaving one self-contained .h5 (+ video files)
    to transfer. Idempotent."""
    data = request.get_json(silent=True) or {}
    subj, subj_date, _ = _parse_session_id(session_id)
    dd = (data.get("data_dir") or "").strip()
    data_base = Path(dd).expanduser() if dd else DATA_DIR
    session_dir = (data_base / subj / subj_date / session_id) if subj else (data_base / session_id)
    vd = (data.get("video_dir") or "").strip() or "/media/vruser/ssd/video"
    video_session_dir = (Path(vd) / subj / subj_date / session_id) if subj else (Path(vd) / session_id)
    try:
        _ensure_rig_path()
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
    """Start the leader engine as a subprocess."""
    global _process, _process_log
    data = request.json
    script = data.get("script")
    args = list(data.get("args", []))

    if _process and _process.poll() is None:
        return jsonify({"ok": False, "error": "Process already running"}), 409

    if script != "leader":
        return jsonify({"ok": False, "error": f"Unknown script: {script}"}), 400

    conda_prefix = Path.home() / "miniforge3" / "envs" / "rig" / "bin"
    python = str(conda_prefix / "python")
    cmd = [python, str(RIG_DIR / "engine" / "leader.py")] + args

    # Resolve relative --rig/--task values against ~/rig so the controller never has to
    # know this Pi's username or home directory.
    for flag in ("--rig", "--task"):
        if flag in cmd:
            i = cmd.index(flag) + 1
            if i < len(cmd) and not cmd[i].startswith("/"):
                cmd[i] = str(RIG_DIR / cmd[i])

    _process_log = []
    _process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1)

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
    then FORCE-exit if still alive after 2s, in case something swallowed SIGTERM — otherwise
    stale code keeps running and Deploy / Restart-API silently no-ops."""
    def _shutdown():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)   # graceful
        time.sleep(2.0)
        os._exit(0)                            # forced: SIGTERM was swallowed

    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"ok": True, "message": "Restarting..."})


@app.route("/api/release_devices", methods=["POST"])
def release_devices():
    """Close all pi_api-managed device instances (before the engine takes over)."""
    released = []
    with _devices_lock:
        names = list(_devices.keys())
    # Camera locks OUTSIDE _devices_lock (consistent order) so closing a camera here can't
    # race a concurrent preview_start/stop and leave the capture pipeline in a bad state.
    for name in names:
        with _cam_lock(name):
            with _devices_lock:
                dev = _devices.pop(name, None)
            if dev is None:
                continue
            try:
                if hasattr(dev, 'stop_stream'):
                    dev.stop_stream()
                if hasattr(dev, 'close'):
                    dev.close()
                released.append(name)
            except Exception as e:
                released.append(f"{name} (error: {e})")
    _monitor_events.clear()
    return jsonify({"ok": True, "released": released})


def _release_one(name):
    """Release and drop a single stored device BEFORE a re-init reclaims its line/bus/port.
    Without this, re-initializing a persistent device (the per-card Reinit button, or a
    second Init Devices without a pi_api restart) leaves the old instance still holding its
    resource (GPIO line, I2C handle, serial port, capture subprocess). Pop under
    _devices_lock, close OUTSIDE it — a slow close (joins, subprocess teardown) must not
    block /api/status and every other device access."""
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


# ── Generic device endpoints ──


@app.route("/api/init_device", methods=["POST"])
def init_device():
    """Initialize (or re-initialize) one device from its rig-yaml config.
    Body: {name, type, config}. The device class comes from devices/<type>.py."""
    data = request.json or {}
    name = data.get("name")
    dev_type = data.get("type", name)
    config = data.get("config", {}) or {}
    if not name:
        return jsonify({"ok": False, "error": "missing device name"}), 400
    try:
        cls = _load_device_class(dev_type)
    except Exception as e:
        return jsonify({"ok": False, "error": f"unknown device type '{dev_type}': {e}"}), 400
    # Per-name camera lock (harmless for non-cameras): serializes this release→construct→
    # store against a concurrent camera_preview_start/stop on the same device, and keeps
    # the recording check + release atomic.
    with _cam_lock(name):
        with _devices_lock:
            existing = _devices.get(name)
        if existing is not None and _is_session_recording(existing):
            return jsonify({"ok": False,
                            "error": "Session recording in progress; not re-initialized"}), 409
        _release_one(name)   # free any prior instance so this (re)init can re-claim its resource
        try:
            dev = cls()
            dev.init(rig_config=config, task_params={})
            if dev.needs_calibration and "calibration" in config:
                dev.load_calibration(config["calibration"])
            with _devices_lock:
                _devices[name] = dev
            check = dev.check()
            ok = bool(check.get("ok", True))
            body = {"ok": ok, "message": check.get("message", "OK")}
            if not ok:
                body["error"] = check.get("message", "check failed")
            return jsonify(body), (200 if ok else 500)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/monitor_device", methods=["POST"])
def monitor_device():
    """Start a device's live stream into a per-name buffer (setup-UI monitor)."""
    name = (request.json or {}).get("name")
    with _devices_lock:
        dev = _devices.get(name)
    if not dev:
        return jsonify({"ok": False, "error": f"{name} not initialized"}), 400
    buf = _monitor_events[name] = []

    def on_event(evt):
        buf.append(evt)
        if len(buf) > 3000:
            del buf[:len(buf) - 3000]

    dev.start_stream(on_event)
    return jsonify({"ok": True})


@app.route("/api/stop_monitor_device", methods=["POST"])
def stop_monitor_device():
    """Stop a device's live-monitor stream."""
    name = (request.json or {}).get("name")
    with _devices_lock:
        dev = _devices.get(name)
    if dev:
        dev.stop_stream()
    return jsonify({"ok": True})


@app.route("/api/device_data")
def device_data():
    """Return-and-clear a device's buffered monitor events (polled by the setup UI).
    Drain IN PLACE: the monitor callback holds this exact list object in its closure,
    so rebinding _monitor_events[name] to a new list would orphan the buffer and the
    monitor would go silent after the first poll."""
    name = request.args.get("name", "")
    buf = _monitor_events.get(name)
    if buf is None:
        return jsonify({"events": []})
    events = list(buf)
    del buf[:len(events)]   # keep anything appended during the copy
    return jsonify({"events": events})


# ── Camera-like devices (device-name keyed) ──


@app.route("/api/camera_preview_start", methods=["POST"])
def camera_preview_start():
    """Start a camera device's MJPEG preview (or session recording if session_id given).
    Body: {device, type, config, session_id?, video_dir?, downsample?}."""
    data = request.get_json(silent=True) or {}   # tolerate a body-less POST (no 415)
    name = data.get("device", "camera")
    dev_type = data.get("type", name)
    config = data.get("config", {}) or {}
    # Serialize the whole check→pop→construct per device so two concurrent starts can't
    # both open the capture pipeline.
    with _cam_lock(name):
        # Don't restart if already recording (e.g. session recording started by Go)
        with _devices_lock:
            existing = _devices.get(name)
        if existing is not None and getattr(existing, "_recording", False):
            return jsonify({"ok": True, "message": f"{name} already recording"})
        # Tear down any stale (non-recording) instance before reopening — a lingering
        # capture handle can block the new open.
        with _devices_lock:
            stale = _devices.pop(name, None)
        if stale is not None:
            try:
                stale.close()
            except Exception:
                pass
        try:
            cls = _load_device_class(dev_type)
        except Exception as e:
            return jsonify({"ok": False, "error": f"unknown device type '{dev_type}': {e}"}), 400
        # A real session_id records to disk; "preview"/none is a setup-UI live view — no file.
        session_id = data.get("session_id")
        recording = bool(session_id and session_id != "preview")
        dev = None
        try:
            dev = cls()
            dev.init(rig_config=config, task_params={})
            if recording:
                video_dir = data.get("video_dir", "/media/vruser/ssd/video")
                _EXTRA_DOWNLOAD_DIRS.add(Path(video_dir))   # transfer may pull from here
                subj, subj_date, _ = _parse_session_id(session_id)
                # A non-standard session id (no subj_date_num shape) records flat under
                # video_dir instead of crashing on Path / None.
                output_dir = str(Path(video_dir) / subj / subj_date if subj
                                 else Path(video_dir))
                dev.start_recording(session_id=session_id, output_dir=output_dir)
            else:
                dev.start_preview(downsample=bool(data.get("downsample", False)))
            with _devices_lock:
                _devices[name] = dev
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
    """MJPEG stream from a camera device (?device=<name>)."""
    from flask import Response
    name = request.args.get("device", "camera")
    with _devices_lock:
        dev = _devices.get(name)
    if not dev or not hasattr(dev, "mjpeg_stream"):
        return jsonify({"ok": False, "error": f"{name} not streaming"}), 400
    return Response(dev.mjpeg_stream(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/camera_preview_stop", methods=["POST"])
def camera_preview_stop():
    """Stop a camera device's preview. Refuses to stop an active SESSION recording unless
    force=True, so a setup-UI Reinit/Stop can't silently truncate a running experiment's
    video — only the experiment UI's own end-of-session stop passes force=True."""
    data = request.get_json(silent=True) or {}   # tolerate a body-less POST (no 415)
    name = data.get("device", "camera")
    force = bool(data.get("force", False))
    with _cam_lock(name):
        with _devices_lock:
            dev = _devices.get(name)
        if dev is not None and _is_session_recording(dev) and not force:
            return jsonify({"ok": True,
                            "message": "Session recording in progress; not stopped"})
        with _devices_lock:
            dev = _devices.pop(name, None)
        if dev:
            try:
                if hasattr(dev, "stop_recording"):
                    dev.stop_recording()
            finally:
                dev.close()   # always release the pipeline, even if the flush raised
    return jsonify({"ok": True})


@app.route("/api/camera_controls", methods=["POST"])
def camera_controls():
    """Apply live exposure/gain to a running camera that supports it (?device=<name> in body).
    Serialized with start/stop via the device's camera lock."""
    data = request.json or {}
    name = data.get("device", "camera")
    with _cam_lock(name):
        with _devices_lock:
            dev = _devices.get(name)
        if dev is None:
            return jsonify({"ok": False, "error": f"{name} not running"}), 400
        if not hasattr(dev, "apply_exposure"):
            return jsonify({"ok": False,
                            "error": f"{name} has no live exposure controls"}), 400
        # Never change exposure/gain mid-experiment: a real session recording must not be
        # mutated by stale setup-UI tuning (mirrors camera_preview_stop's session guard).
        if _is_session_recording(dev):
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


# ── Shepherd (health monitor) ──


@app.route("/api/shepherd", methods=["POST"])
def shepherd_control():
    """Start or stop the shepherd health-monitor service live, so the setup-UI Monitor toggle takes
    effect immediately without a reinstall. enabled=true -> enable+start; false -> stop+disable.
    Needs root to drive systemd; pi_api runs with passwordless sudo.
    Idempotent, and no-op-safe if the unit was never installed (reports that instead of erroring)."""
    enabled = bool((request.json or {}).get("enabled", True))
    # unit presence check first, so an un-installed shepherd gives a clear message, not a systemd error
    present = subprocess.run(["systemctl", "list-unit-files", "shepherd.service"],
                             capture_output=True, text=True, timeout=10)
    if "shepherd.service" not in present.stdout:
        return jsonify({"ok": False, "running": False,
                        "error": "shepherd.service not installed (run Install on the leader first)"}), 404
    cmds = (["sudo systemctl enable shepherd", "sudo systemctl restart shepherd"] if enabled
            else ["sudo systemctl disable shepherd", "sudo systemctl stop shepherd"])
    try:
        for c in cmds:
            subprocess.run(c.split(), check=True, capture_output=True, text=True, timeout=15)
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": (e.stderr or str(e)).strip()}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    active = subprocess.run(["systemctl", "is-active", "shepherd"],
                            capture_output=True, text=True, timeout=10)
    return jsonify({"ok": True, "enabled": enabled, "running": active.stdout.strip() == "active"})


def _latest_shepherd_pair():
    """(jsonl_path, alerts_path|None) for the most recent shepherd run, or (None, None).
    'Most recent' = newest shepherd_*.jsonl by mtime — that's the currently-running log."""
    if not SHEPHERD_LOG_DIR.is_dir():
        return None, None
    jsonls = sorted(SHEPHERD_LOG_DIR.glob("shepherd_*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not jsonls:
        return None, None
    jsonl = jsonls[-1]
    alerts = jsonl.with_name(jsonl.stem + ".alerts.log")        # shepherd_<stamp>.alerts.log
    return jsonl, (alerts if alerts.exists() else None)


@app.route("/api/shepherd_logs/latest")
def shepherd_logs_latest():
    """Home-relative paths + sizes of the newest shepherd jsonl (and its alerts log) so the
    controller can pull them via /api/download. Returns files:[] when none exist yet."""
    jsonl, alerts = _latest_shepherd_pair()
    home = Path.home()
    files, sizes = [], {}
    for p in (jsonl, alerts):
        if p is not None:
            rel = str(p.relative_to(home)) if p.is_relative_to(home) else str(p)
            files.append(rel)
            sizes[rel] = p.stat().st_size
    return jsonify({"ok": True, "files": files, "sizes": sizes})


@app.route("/api/shepherd_logs/cleanup", methods=["POST"])
def shepherd_logs_cleanup():
    """Delete shepherd logs older than `days` (by mtime) to bound accumulation on the Pi. The
    newest jsonl/alerts pair (the active run) is always kept, even if the check is generous."""
    days = float((request.json or {}).get("days", 7))
    if not SHEPHERD_LOG_DIR.is_dir():
        return jsonify({"ok": True, "deleted": [], "kept": 0})
    cutoff = time.time() - days * 86400
    keep = {p for p in _latest_shepherd_pair() if p is not None}
    deleted, kept = [], 0
    for p in SHEPHERD_LOG_DIR.glob("shepherd_*"):
        if p in keep or not p.is_file():
            kept += 1
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                deleted.append(p.name)
            else:
                kept += 1
        except OSError:
            pass
    return jsonify({"ok": True, "deleted": deleted, "kept": kept, "days": days})


# ── Main ──

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="VRFarm Pi API")
    parser.add_argument("--port", type=int, default=5080)
    args = parser.parse_args()
    app.run(host="0.0.0.0", port=args.port, threaded=True)
