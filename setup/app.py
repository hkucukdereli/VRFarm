"""
setup/app.py

Rig setup Flask UI (localhost:4999).
Manage Pis, assign the Leader role, configure devices, install/deploy code.

Devices are fully generic: cards come from the rig yaml + DEVICE_REGISTRY
(devices/*.py self-register; module name == device type), and init goes through
pi_api's /api/init_device — no per-device endpoints on the controller.
"""

from __future__ import annotations
import importlib
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.config import load_rig, save_rig
from shared.deploy_manifest import deploy_files as _get_deploy_files

app = Flask(__name__)

# ── State ──

_rig_config = None
_rig_path = None


@app.route("/")
def index():
    rigs = sorted(Path(ROOT / "rigs").glob("*.yaml"))
    return render_template("setup.html",
                           rigs=[r.stem for r in rigs])


@app.route("/api/load_rig", methods=["POST"])
def api_load_rig():
    name = request.json.get("name")
    global _rig_config, _rig_path
    _rig_path = ROOT / "rigs" / f"{name}.yaml"
    if not _rig_path.exists():
        return jsonify({"error": f"Not found: {_rig_path}"}), 404
    _rig_config = load_rig(_rig_path)
    return jsonify(_rig_config)


@app.route("/api/save_rig", methods=["POST"])
def api_save_rig():
    global _rig_config
    data = request.json
    _rig_config = data
    if _rig_path:
        save_rig(_rig_config, _rig_path)
    return jsonify({"ok": True})


@app.route("/api/create_rig", methods=["POST"])
def api_create_rig():
    """Create a new rig config from template."""
    global _rig_config, _rig_path
    name = request.json.get("name", "new_rig")
    _rig_path = ROOT / "rigs" / f"{name}.yaml"
    _rig_config = {
        "name": name,
        "pis": [],
        "network": {
            "event_port": 5571, "command_port": 5572, "api_port": 5080,
        },
        "devices": {},
        "data": {
            # NB: no controller dir — the controller's data root is machine-specific and
            # resolved locally by app/app.py ($VRFARM_DATA_DIR, else ~/VRFarm/data).
            "leader_dir": "",
            "video_dir": "",
        },
    }
    save_rig(_rig_config, _rig_path)
    return jsonify(_rig_config)


@app.route("/api/check_pi", methods=["POST"])
def api_check_pi():
    """Check if a Pi is reachable (SSH first, then REST API)."""
    ip = request.json.get("ip")
    user = request.json.get("user", "vruser")
    port = request.json.get("api_port", 5080)

    result = {"ip": ip, "ssh": False, "api": False}

    # Check SSH
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
             f"{user}@{ip}", "echo ok"],
            capture_output=True, text=True, timeout=5)
        result["ssh"] = r.returncode == 0
    except Exception:
        pass

    # Check REST API
    try:
        r = requests.get(f"http://{ip}:{port}/api/status", timeout=3)
        result["api"] = r.status_code == 200
        result["status"] = r.json()
    except Exception:
        pass

    return jsonify(result)


# pip packages needed per device TYPE, installed by /api/install_pi. (DeviceInfo.required_packages
# is display-only in the catalog — THIS map is what actually installs.)
DEVICE_PACKAGES: dict = {
    "gyroscope_nano": ["pyserial"],
    "gyroscope_i2c": ["smbus2"],
}
# device types that need the Pi's I2C bus enabled
I2C_DEVICE_TYPES: set = {"gyroscope_i2c"}


@app.route("/api/install_pi", methods=["POST"])
def api_install_pi():
    """First-time Pi setup via SSH: install system + Python packages, systemd service."""
    data = request.json
    ip = data["ip"]
    user = data.get("user", "vruser")
    role = data.get("role", "leader")
    devices = data.get("devices", [])
    dev_cfgs = (_rig_config or {}).get("devices", {})
    dev_types = {d: (dev_cfgs.get(d, {}) or {}).get("type", d) for d in devices}

    steps = []
    ssh_prefix = f"{user}@{ip}"
    conda_activate = ("source ~/miniforge3/etc/profile.d/conda.sh && "
                      "conda activate rig && ")
    needs_i2c = any(t in I2C_DEVICE_TYPES for t in dev_types.values())
    # Video devices (rig config `video: true`) capture via ffmpeg / v4l2
    needs_video = any((dev_cfgs.get(d, {}) or {}).get("video") for d in devices)

    try:
        # 1. Create directories
        _ssh(ssh_prefix, "mkdir -p ~/rig ~/data ~/video")
        steps.append("Created ~/rig ~/data ~/video")

        # 1a. Hardware-access groups for the Pi user (idempotent; usually already set)
        _ssh(ssh_prefix, f"sudo usermod -aG dialout,i2c,video,gpio {user} || true", timeout=15)

        # 1b. Ensure the conda 'rig' env exists, pinned to the SYSTEM python version, so any
        #     apt-built bindings a device later needs can be symlinked in without an ABI
        #     mismatch. Also makes Install self-contained instead of assuming an existing env.
        _ssh(ssh_prefix,
             "source ~/miniforge3/etc/profile.d/conda.sh && "
             "SYSPY=$(python3 -c 'import sys; print(str(sys.version_info.major)+\".\"+str(sys.version_info.minor))') && "
             "conda env list | awk '{print $1}' | grep -qx rig || "
             "conda create -n rig python=$SYSPY -y",
             timeout=400)
        steps.append("Ensured conda 'rig' env (matched to system python)")

        # 2. Enable I2C when a device needs the bus. Idempotent.
        if needs_i2c:
            _ssh(ssh_prefix, "sudo raspi-config nonint do_i2c 0", timeout=20)
            steps.append("Enabled I2C")

        # 2b. Video-capture tooling for camera-like devices (ffmpeg for capture +
        #     consolidation remux; v4l-utils for format probing/diagnostics).
        if needs_video:
            _ssh(ssh_prefix,
                 "sudo apt-get update -qq && sudo apt-get install -y ffmpeg v4l-utils",
                 timeout=300)
            steps.append("Installed ffmpeg + v4l-utils")

        # 3. Python packages (pip): base set + per-device-type extras.
        packages = {"flask", "pyyaml", "numpy"}
        for t in dev_types.values():
            packages.update(DEVICE_PACKAGES.get(t, []))
        if role == "leader":
            packages.add("h5py")

        pkg_str = " ".join(sorted(packages))
        _ssh(ssh_prefix,
             f"{conda_activate} pip install {pkg_str}",
             timeout=120)
        steps.append(f"Installed Python packages: {pkg_str}")

        # 4. Upload project files. scp won't create the ~/rig subdirs, so make them first —
        #    otherwise the first file into a not-yet-existing subdir fails on a fresh Pi.
        files_to_deploy = _get_deploy_files(role)
        remote_dirs = sorted({os.path.dirname(remote) for _, remote in files_to_deploy
                              if os.path.dirname(remote)})
        if remote_dirs:
            _ssh(ssh_prefix, "mkdir -p " + " ".join(f"~/rig/{d}" for d in remote_dirs))
        for local, remote in files_to_deploy:
            _scp(str(ROOT / local), f"{ssh_prefix}:~/rig/{remote}")
        steps.append(f"Deployed {len(files_to_deploy)} files")

        # 5. Upload and enable systemd service (rendered for this Pi's user)
        _scp(_render_unit(ROOT / "pi_api" / "vrfarm.service", user),
             f"{ssh_prefix}:/tmp/vrfarm.service")
        _ssh(ssh_prefix,
             "sudo cp /tmp/vrfarm.service /etc/systemd/system/ && "
             "sudo systemctl daemon-reload && "
             "sudo systemctl enable vrfarm && "
             "sudo systemctl kill vrfarm 2>/dev/null; "
             "sudo systemctl restart vrfarm",
             timeout=20)
        steps.append("Installed systemd service")

        # 5b. shepherd health monitor (leader only) — a SEPARATE process from pi_api by design, so
        #     a pi_api stall can't take the watchdog down (and shepherd's API probe DETECTS that
        #     stall). shepherd.py rode step 4; here we seed its config and install its own service.
        #     config.yaml is seeded with `cp -n` so thresholds/messages edited on the Pi survive a
        #     re-Install/Deploy. The Monitor toggle (rig.shepherd.enabled) decides whether Install
        #     brings shepherd UP (enable+restart) or takes it DOWN (stop+disable).
        if role == "leader":
            shepherd_on = data.get("shepherd_enabled", True)
            _scp(_render_unit(ROOT / "shepherd" / "shepherd.service", user),
                 f"{ssh_prefix}:/tmp/shepherd.service")
            _scp(str(ROOT / "shepherd" / "config.yaml"),
                 f"{ssh_prefix}:/tmp/shepherd.config.yaml")
            svc_cmd = ("sudo systemctl enable shepherd && sudo systemctl restart shepherd"
                       if shepherd_on else
                       "sudo systemctl disable shepherd 2>/dev/null; sudo systemctl stop shepherd 2>/dev/null || true")
            _ssh(ssh_prefix,
                 "mkdir -p ~/rig/shepherd; "
                 "cp -n /tmp/shepherd.config.yaml ~/rig/shepherd/config.yaml 2>/dev/null || true; "
                 "sudo cp /tmp/shepherd.service /etc/systemd/system/ && "
                 "sudo systemctl daemon-reload && "
                 f"{svc_cmd}",
                 timeout=20)
            steps.append("Installed shepherd health monitor (running)" if shepherd_on
                         else "Installed shepherd health monitor (stopped+disabled — Monitor OFF)")

        return jsonify({"ok": True, "steps": steps})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


@app.route("/api/reboot_pi", methods=["POST"])
def api_reboot_pi():
    """Reboot a Pi via SSH."""
    data = request.json
    ip = data["ip"]
    user = data.get("user", "vruser")
    try:
        _ssh(f"{user}@{ip}", "sudo reboot", timeout=5)
    except Exception:
        pass  # SSH drops when Pi reboots
    return jsonify({"ok": True})


@app.route("/api/shutdown_pi", methods=["POST"])
def api_shutdown_pi():
    """Shutdown a Pi via SSH."""
    data = request.json
    ip = data["ip"]
    user = data.get("user", "vruser")
    try:
        _ssh(f"{user}@{ip}", "sudo shutdown -h now", timeout=5)
    except Exception:
        pass  # SSH drops when Pi shuts down
    return jsonify({"ok": True})


@app.route("/api/restart_pi", methods=["POST"])
def api_restart_pi():
    """Restart pi_api on one Pi (reloads deployed code without a full reboot) and wait for it
    to respawn. Use when new code was deployed but the running process still has the old
    modules cached in memory."""
    data = request.json or {}
    ip = data.get("ip")
    if not ip:
        return jsonify({"ok": False, "error": "no ip"}), 400
    port = data.get("api_port", 5080)
    steps = []
    try:
        requests.post(f"http://{ip}:{port}/api/restart", timeout=5)
        steps.append("Restart requested (pi_api self-kills; systemd respawns)")
        import time as _time
        _time.sleep(2.0)          # let the old process exit first
        back = False
        for _ in range(12):
            try:
                if requests.get(f"http://{ip}:{port}/api/logs?n=1", timeout=2).ok:
                    back = True
                    break
            except Exception:
                pass
            _time.sleep(1.0)
        steps.append("pi_api back online" if back else
                     "WARN pi_api did not respond after restart — re-check the Pi")
        return jsonify({"ok": back, "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


@app.route("/api/deploy_pi", methods=["POST"])
def api_deploy_pi():
    """Deploy code to Pi via REST API (after initial SSH install)."""
    data = request.json
    ip = data["ip"]
    role = data.get("role", "leader")
    port = data.get("api_port", 5080)

    steps = []
    files = _get_deploy_files(role)

    try:
        for local, remote in files:
            local_path = ROOT / local
            if not local_path.exists():
                continue
            with open(local_path, "rb") as f:
                r = requests.post(
                    f"http://{ip}:{port}/api/upload",
                    files={"file": f},
                    data={"path": remote},
                    timeout=10)
            # A failed upload must fail the deploy — otherwise stale code keeps running
            # on the Pi while the log says everything was uploaded.
            if not (r.ok and r.json().get("ok")):
                raise RuntimeError(f"upload {remote} failed: "
                                   f"HTTP {r.status_code} {r.text[:200]}")
            steps.append(f"Uploaded {remote}")

        # Restart pi_api so the just-uploaded code actually RUNS. A long-running Python
        # process won't pick up overwritten files on its own. /api/restart self-kills and
        # systemd (Restart=always) respawns with the new code.
        if data.get("restart", True):
            try:
                requests.post(f"http://{ip}:{port}/api/restart", timeout=5)
                steps.append("Restarted pi_api to load new code")
                # Wait for it to respawn so the UI isn't left hitting a down Pi. The restart
                # drops all initialized devices — the client resets their state after deploy.
                import time as _time
                _time.sleep(2.0)          # let the old process exit first
                back = False
                for _ in range(12):
                    try:
                        if requests.get(f"http://{ip}:{port}/api/logs?n=1", timeout=2).ok:
                            back = True
                            break
                    except Exception:
                        pass
                    _time.sleep(1.0)
                steps.append("pi_api back online — re-initialize devices" if back else
                             "WARN pi_api did not respond after restart — re-check the Pi")
            except Exception as e:
                steps.append(f"(pi_api restart skipped: {e})")

        return jsonify({"ok": True, "steps": steps})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


@app.route("/api/proxy", methods=["POST"])
def api_proxy():
    """Generic proxy to forward requests to a Pi's REST API."""
    data = request.json
    ip = data["ip"]
    port = data.get("port", 5080)
    endpoint = data["endpoint"]
    method = data.get("method", "POST").upper()
    payload = data.get("payload", {})
    timeout = data.get("timeout", 30)

    try:
        if method == "GET":
            r = requests.get(f"http://{ip}:{port}{endpoint}",
                             timeout=timeout)
        else:
            r = requests.post(f"http://{ip}:{port}{endpoint}",
                              json=payload, timeout=timeout)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/camera_feed")
def camera_feed():
    """Proxy a Pi video device's MJPEG stream through the setup app (same-origin), so the
    preview works from any browser that can reach this app (?device=<name>). The setup app
    runs threaded=True, so this long-lived stream doesn't block other requests."""
    if not _rig_config:
        return "No rig", 400
    name = request.args.get("device", "")
    api_port = _rig_config["network"]["api_port"]
    ip = None
    for pi in _rig_config["pis"]:
        if name and name in pi.get("devices", []):
            ip = pi["ip"]
            break
    if not ip:
        return "No such device", 400

    # Reconnecting relay: don't collapse a transient Pi hiccup (preview not started yet, Reinit,
    # pi_api restart) into a silent 200-blank the setup <img> can't recover from. See
    # shared/mjpeg_relay.py.
    from shared.mjpeg_relay import relay
    return Response(relay(f"http://{ip}:{api_port}/api/camera_stream?device={name}"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


def _init_one_device(dev_name, devices, post):
    """Run the Pi-side init for a SINGLE device via the generic /api/init_device. `post(endpoint,
    payload, label, timeout) -> (ok, msg)` is supplied by the caller (it POSTs to the device's Pi
    and logs to the caller's steps). Returns (ok, msg). Shared by /api/init_devices (loop over
    all) and /api/reinit_device (one card)."""
    cfg = devices.get(dev_name, {}) or {}
    if cfg.get("video"):
        # Release any stuck/streaming pipeline first so a frozen preview recovers.
        post("/api/camera_preview_stop", {"device": dev_name}, f"{dev_name} release", 15)
    return post("/api/init_device",
                {"name": dev_name, "type": cfg.get("type", dev_name), "config": cfg},
                dev_name, 25)


def _mk_post(ip, api_port, steps):
    """A post(endpoint, payload, label, timeout) -> (ok, msg) bound to one Pi, logging to `steps`."""
    def _post(endpoint, payload, label, timeout=10):
        try:
            r = requests.post(f"http://{ip}:{api_port}{endpoint}", json=payload, timeout=timeout)
            res = r.json()
            ok = res.get("ok", False)
            msg = res.get("message", res.get("error", ""))
            steps.append(f"{label}: {'OK' if ok else 'FAIL'} {msg}")
            return ok, msg
        except Exception as e:
            steps.append(f"{label}: FAIL ({e})")
            return False, str(e)
    return _post


@app.route("/api/init_devices", methods=["POST"])
def api_init_devices():
    """Initialize all enabled devices on their assigned Pis (rig-yaml order).
    Returns per-device results for status tracking."""
    if not _rig_config:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400

    api_port = _rig_config["network"]["api_port"]
    steps = []
    results = {}  # device_name -> {ok, message}

    # Map device -> assigned Pi IP
    dev_to_ip = {}
    for pi in _rig_config.get("pis", []):
        for dev_name in pi.get("devices", []):
            dev_to_ip[dev_name] = pi["ip"]

    devices = _rig_config.get("devices", {})

    for dev_name, cfg in devices.items():
        # `enabled` defaults True everywhere (engine, controllers, UIs): present = on
        # unless explicitly disabled.
        if not (cfg or {}).get("enabled", True):
            continue
        ip = dev_to_ip.get(dev_name)
        if ip:
            ok, msg = _init_one_device(dev_name, devices, _mk_post(ip, api_port, steps))
            results[dev_name] = {"ok": ok, "message": msg}

    all_ok = all(r["ok"] for r in results.values())
    return jsonify({"ok": all_ok, "steps": steps, "results": results})


@app.route("/api/reinit_device", methods=["POST"])
def api_reinit_device():
    """Reinitialize a SINGLE enabled device on its Pi, without touching the others — the same
    per-device flow as /api/init_devices, for the per-card Reinit buttons (any device type)."""
    if not _rig_config:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400
    dev_name = (request.json or {}).get("device")
    devices = _rig_config.get("devices", {})
    if dev_name not in devices or not devices[dev_name].get("enabled", True):
        return jsonify({"ok": False, "error": f"{dev_name} is not enabled"}), 400
    ip = next((pi["ip"] for pi in _rig_config.get("pis", [])
               if dev_name in pi.get("devices", [])), None)
    if not ip:
        return jsonify({"ok": False, "error": f"{dev_name} is not assigned to a Pi"}), 400

    steps = []
    ok, _msg = _init_one_device(
        dev_name, devices, _mk_post(ip, _rig_config["network"]["api_port"], steps))
    return jsonify({"ok": ok, "device": dev_name, "steps": steps})


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """Shut down the Flask server."""
    func = request.environ.get("werkzeug.server.shutdown")
    if func:
        func()
    else:
        os._exit(0)
    return jsonify({"ok": True})


@app.route("/api/device_schemas")
def api_device_schemas():
    """Return task_params_schema for all registered devices. Scans devices/*.py — every module
    self-registers via @register_device, so a new device file appears in the Add-Device catalog
    with no controller edit."""
    from devices.base import DEVICE_REGISTRY
    import devices as _devices_pkg
    for m in pkgutil.iter_modules(_devices_pkg.__path__):
        if m.name == "base" or m.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"devices.{m.name}")
        except Exception:
            pass   # a module that needs Pi-only deps still registers on the Pi; skip here

    schemas = {}
    for name, cls in DEVICE_REGISTRY.items():
        schemas[name] = {
            "label": cls.info.label,
            "io_type": cls.info.io_type.value,
            "required_packages": cls.info.required_packages,
            "needs_calibration": cls().needs_calibration,
            "task_params": cls.task_params_schema(),
        }
    return jsonify(schemas)


# ── Helpers ──

def _render_unit(local_path, user: str) -> str:
    """Render a systemd unit template ({{USER}}/{{HOME}} tokens) for a Pi user and
    return the temp-file path to scp. Assumes the standard /home/<user> layout."""
    import tempfile
    text = Path(local_path).read_text()
    text = text.replace("{{USER}}", user).replace("{{HOME}}", f"/home/{user}")
    fd, tmp = tempfile.mkstemp(suffix=".service")
    os.close(fd)
    Path(tmp).write_text(text)
    return tmp


def _ssh(target: str, cmd: str, timeout: int = 60):
    """Run a command on a Pi via SSH."""
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", target, cmd],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"SSH failed: {r.stderr.strip()}")
    return r.stdout


def _scp(local: str, remote: str):
    """Copy a file to a Pi via SCP."""
    r = subprocess.run(
        ["scp", "-o", "ConnectTimeout=5", local, remote],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"SCP failed: {r.stderr.strip()}")


# ── Main ──

if __name__ == "__main__":
    import threading
    import webbrowser
    # Only open browser in the main process (not the reloader child)
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.5, lambda: webbrowser.open(
            "http://localhost:4999")).start()
    app.run(host="0.0.0.0", port=4999, debug=True, threaded=True)
