"""
setup/app.py

Rig setup Flask UI (localhost:4999).
Manage Pis, assign Leader/Follower roles, configure devices,
run calibrations, deploy code via REST API.

Replaces rig_setup/rig_setup_ui.py.
"""

from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

import requests
import yaml
from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.config import load_rig, save_rig

GEO_DIR = ROOT / "display_calibration"

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
            "event_port": 5571, "command_port": 5572,
            "display_port": 5575, "camera_port": 5001, "api_port": 5080,
        },
        "devices": {},
        "data": {
            "mac_dir": str(Path.home() / "VRFarm" / "data"),
            "leader_dir": "/home/vruser/data",
            "video_dir": "/media/vruser/ssd/video",
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


@app.route("/api/install_pi", methods=["POST"])
def api_install_pi():
    """First-time Pi setup via SSH: install system + Python packages, systemd service."""
    data = request.json
    ip = data["ip"]
    user = data.get("user", "vruser")
    role = data.get("role", "follower")
    devices = data.get("devices", [])

    steps = []
    ssh_prefix = f"{user}@{ip}"
    conda_activate = ("source ~/miniforge3/etc/profile.d/conda.sh && "
                      "conda activate rig && ")
    needs_pigpio = any(d in devices for d in ["reward", "photodiode"])
    needs_camera = "camera" in devices

    try:
        # 1. Create directories
        _ssh(ssh_prefix, "mkdir -p ~/rig ~/data")
        steps.append("Created ~/rig ~/data")

        # 1b. Ensure the conda 'rig' env exists, pinned to the SYSTEM python
        #     version. The camera bindings (libcamera/picamera2) are apt-built
        #     for the system python ABI and only load in an env of the SAME
        #     minor version, so the env must track system python (3.11 on
        #     bookworm, 3.13 on trixie, ...). Also makes Install self-contained
        #     instead of assuming a pre-existing env.
        _ssh(ssh_prefix,
             "source ~/miniforge3/etc/profile.d/conda.sh && "
             "SYSPY=$(python3 -c 'import sys; print(str(sys.version_info.major)+\".\"+str(sys.version_info.minor))') && "
             "conda env list | awk '{print $1}' | grep -qx rig || "
             "conda create -n rig python=$SYSPY -y",
             timeout=400)
        steps.append("Ensured conda 'rig' env (matched to system python)")

        # 2. System packages (apt)
        apt_packages = []
        if needs_pigpio:
            apt_packages.append("pigpio-tools")  # pigpiod daemon
        if needs_camera:
            apt_packages.extend(["python3-libcamera", "python3-picamera2"])
        if apt_packages:
            apt_str = " ".join(apt_packages)
            _ssh(ssh_prefix,
                 f"sudo apt-get update -qq && sudo apt-get install -y {apt_str}",
                 timeout=300)
            steps.append(f"Installed system packages: {apt_str}")

        # 3. Symlink system Python packages into conda env (camera only)
        #    picamera2 + libcamera are apt-installed for the SYSTEM python and
        #    ship compiled .so files (e.g. _libcamera.cpython-3XX-*.so). The
        #    symlink only loads if the env python minor version == system python.
        if needs_camera:
            # Guard: env python must match system python, else the symlinked
            # .so bindings have the wrong ABI and import fails silently later.
            envpy, syspy = _ssh(
                ssh_prefix,
                conda_activate +
                "python -c 'import sys; print(str(sys.version_info.major)+\".\"+str(sys.version_info.minor))' && "
                "python3 -c 'import sys; print(str(sys.version_info.major)+\".\"+str(sys.version_info.minor))'"
            ).split()
            if envpy != syspy:
                raise RuntimeError(
                    f"Camera setup aborted: conda 'rig' env python is {envpy} but "
                    f"system python is {syspy}. libcamera/picamera2 are apt-built "
                    f"for {syspy} and won't import in a {envpy} env. Recreate the env "
                    f"to match: conda create -n rig python={syspy} -y (then reinstall deps)."
                )
            symlink_packages = [
                "libcamera", "picamera2", "pykms",       # dirs
                "pidng", "videodev2",                      # dirs
                "prctl.py",                                # single file
            ]
            # Also symlink any .so files for prctl
            _ssh(ssh_prefix,
                 f"{conda_activate}"
                 "SITE=$(python -c 'import site; print(site.getsitepackages()[0])') && "
                 "SYS=/usr/lib/python3/dist-packages && "
                 + " && ".join(
                     f"ln -sfn $SYS/{pkg} $SITE/{pkg}" for pkg in symlink_packages
                 ) + " && "
                 "for f in $SYS/_prctl*.so; do ln -sfn $f $SITE/$(basename $f); done")
            steps.append(f"Symlinked picamera2 system packages into conda env (py{envpy})")

        # 4. Python packages (pip)
        packages = {"flask", "pyyaml", "numpy"}
        device_packages = {
            "lick_sensor": ["smbus2"],
            "reward": ["pigpio", "scipy"],
            "camera": ["h5py", "pillow", "simplejpeg", "piexif", "av"],
            "photodiode": ["pigpio"],
            "display": ["pygame"],
        }
        for dev in devices:
            packages.update(device_packages.get(dev, []))
        if role == "leader":
            packages.add("h5py")

        pkg_str = " ".join(sorted(packages))
        _ssh(ssh_prefix,
             f"{conda_activate} pip install {pkg_str}",
             timeout=120)
        steps.append(f"Installed Python packages: {pkg_str}")

        # 5. Upload project files
        files_to_deploy = _get_deploy_files(role)
        for local, remote in files_to_deploy:
            _scp(str(ROOT / local), f"{ssh_prefix}:~/rig/{remote}")
        steps.append(f"Deployed {len(files_to_deploy)} files")

        # 6. Upload and enable systemd service
        _scp(str(ROOT / "pi_api" / "vrfarm.service"),
             f"{ssh_prefix}:/tmp/vrfarm.service")
        _ssh(ssh_prefix,
             "sudo cp /tmp/vrfarm.service /etc/systemd/system/ && "
             "sudo systemctl daemon-reload && "
             "sudo systemctl enable vrfarm && "
             "sudo systemctl kill vrfarm 2>/dev/null; "
             "sudo systemctl restart vrfarm",
             timeout=20)
        steps.append("Installed systemd service")

        # 7. Enable pigpiod on boot
        if needs_pigpio:
            _ssh(ssh_prefix,
                 "sudo systemctl enable pigpiod && "
                 "sudo systemctl start pigpiod")
            steps.append("Enabled pigpiod")

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


@app.route("/api/deploy_pi", methods=["POST"])
def api_deploy_pi():
    """Deploy code to Pi via REST API (after initial SSH install)."""
    data = request.json
    ip = data["ip"]
    role = data.get("role", "follower")
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
            steps.append(f"Uploaded {remote}")

        return jsonify({"ok": True, "steps": steps})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


@app.route("/api/calibrate", methods=["POST"])
def api_calibrate():
    """Run calibration on a Pi via its REST API."""
    data = request.json
    ip = data["ip"]
    port = data.get("api_port", 5080)
    device = data["device"]
    params = data.get("params", {})

    try:
        r = requests.post(
            f"http://{ip}:{port}/api/calibrate",
            json={"device": device, "params": params},
            timeout=60)
        result = r.json()

        # Save calibration to rig config
        if result.get("ok") and _rig_config and data.get("save_to_rig"):
            cal_data = result.get("results", {})
            _rig_config.setdefault("devices", {}) \
                       .setdefault(device, {})["calibration"] = cal_data
            if _rig_path:
                save_rig(_rig_config, _rig_path)

        return jsonify(result)

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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


@app.route("/api/init_devices", methods=["POST"])
def api_init_devices():
    """Initialize all enabled devices on their assigned Pis.
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

    def _init(dev_name, ip, endpoint, payload, label, timeout=10):
        try:
            r = requests.post(f"http://{ip}:{api_port}{endpoint}",
                              json=payload, timeout=timeout)
            res = r.json()
            ok = res.get("ok", False)
            msg = res.get("message", res.get("error", ""))
            steps.append(f"{label}: {'OK' if ok else 'FAIL'} {msg}")
            return ok, msg
        except Exception as e:
            steps.append(f"{label}: FAIL ({e})")
            return False, str(e)

    # Display: init projector then display
    if "display" in devices and devices["display"].get("enabled"):
        ip = dev_to_ip.get("display")
        if ip:
            ok1, _ = _init("display", ip, "/api/init_projector", {},
                           "Projector", timeout=35)
            ok2, msg = _init("display", ip, "/api/init_display",
                             {"rig_config": devices["display"]},
                             "Display init", timeout=30)
            results["display"] = {"ok": ok1 and ok2, "message": msg}

    # Lick sensor: I2C init
    if "lick_sensor" in devices and devices["lick_sensor"].get("enabled"):
        ip = dev_to_ip.get("lick_sensor")
        if ip:
            cfg = devices["lick_sensor"]
            ok, msg = _init("lick_sensor", ip, "/api/init_lick", {
                "i2c_address": cfg.get("i2c_address", "0x5A"),
                "electrode": cfg.get("electrode", 4),
            }, "Lick sensor")
            results["lick_sensor"] = {"ok": ok, "message": msg}

    # Reward: pigpiod + GPIO check
    if "reward" in devices and devices["reward"].get("enabled"):
        ip = dev_to_ip.get("reward")
        if ip:
            ok, msg = _init("reward", ip, "/api/init_reward", {
                "pins": devices["reward"].get("pins", {"main": {"gpio": 18}}),
            }, "Reward")
            results["reward"] = {"ok": ok, "message": msg}

    # Camera: detection check
    if "camera" in devices and devices["camera"].get("enabled"):
        ip = dev_to_ip.get("camera")
        if ip:
            ok, msg = _init("camera", ip, "/api/init_camera", {},
                            "Camera")
            results["camera"] = {"ok": ok, "message": msg}

    # Photodiode: pigpiod + GPIO check
    if "photodiode" in devices and devices["photodiode"].get("enabled"):
        ip = dev_to_ip.get("photodiode")
        if ip:
            ok, msg = _init("photodiode", ip, "/api/init_photodiode", {
                "gpio": devices["photodiode"].get("gpio", 24),
            }, "Photodiode")
            results["photodiode"] = {"ok": ok, "message": msg}

    all_ok = all(r["ok"] for r in results.values())
    return jsonify({"ok": all_ok, "steps": steps, "results": results})


@app.route("/api/generate_warp", methods=["POST"])
def api_generate_warp():
    """Generate warp map from rig_geometry.yaml, then SCP to Leader Pi."""
    data = request.json or {}
    geometry_path = data.get("geometry_path", str(ROOT / "display_calibration" / "rig_geometry.yaml"))

    if not Path(geometry_path).exists():
        return jsonify({"ok": False, "error": f"Geometry file not found: {geometry_path}"}), 404

    steps = []
    try:
        # 1. Run compute_warp_map.py on Mac
        conda_prefix = Path("/opt/homebrew/Caskroom/miniforge/base/envs/vrfarm/bin")
        python = str(conda_prefix / "python")
        script = str(ROOT / "display_calibration" / "compute_warp_map.py")
        r = subprocess.run(
            [python, script],
            capture_output=True, text=True, timeout=60,
            cwd=str(ROOT / "display_calibration"))
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr[-500:], "steps": steps})
        steps.append("Generated warp map on Mac")

        npz_path = ROOT / "display_calibration" / "warp_map.npz"
        if not npz_path.exists():
            return jsonify({"ok": False, "error": "warp_map.npz not created", "steps": steps})

        # 2. Copy to all Pis
        if _rig_config:
            for pi in _rig_config.get("pis", []):
                user = pi.get("user", "vruser")
                ip = pi["ip"]
                try:
                    _ssh(f"{user}@{ip}", "mkdir -p ~/rig/calibration")
                    _scp(str(npz_path), f"{user}@{ip}:~/rig/calibration/warp_map.npz")
                    steps.append(f"Copied warp_map.npz to {pi['name']} ({ip})")
                except Exception as e:
                    steps.append(f"Failed to copy to {pi['name']}: {e}")

        return jsonify({"ok": True, "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


@app.route("/api/check_warp")
def api_check_warp():
    """Check if warp_map.npz exists on Leader Pi."""
    if not _rig_config:
        return jsonify({"exists": False})
    for pi in _rig_config.get("pis", []):
        if pi.get("role") == "leader":
            user = pi.get("user", "vruser")
            ip = pi["ip"]
            try:
                r = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=3", f"{user}@{ip}",
                     "test -f ~/rig/calibration/warp_map.npz && echo yes || echo no"],
                    capture_output=True, text=True, timeout=5)
                exists = "yes" in r.stdout
                return jsonify({"exists": exists})
            except Exception:
                return jsonify({"exists": False})
    return jsonify({"exists": False})


# ── Geometry files ──

@app.route("/api/list_geometries")
def api_list_geometries():
    """List rig_geometry*.yaml files in display_calibration/."""
    files = sorted(GEO_DIR.glob("rig_geometry*.yaml"))
    return jsonify([f.name for f in files])


@app.route("/api/load_geometry", methods=["POST"])
def api_load_geometry():
    """Load a geometry YAML and return as JSON."""
    name = request.json.get("name", "rig_geometry.yaml")
    path = GEO_DIR / name
    if not path.exists():
        return jsonify({"error": f"Not found: {name}"}), 404
    with open(path) as f:
        geo = yaml.safe_load(f)
    return jsonify({"name": name, "geometry": geo})


class _FlowList(list):
    """List subclass that yaml.dump renders inline: [a, b, c]."""
    pass

def _flow_list_repr(dumper, data):
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)

yaml.add_representer(_FlowList, _flow_list_repr)


@app.route("/api/save_geometry", methods=["POST"])
def api_save_geometry():
    """Save geometry params to a YAML file."""
    data = request.json
    name = data.get("name", "rig_geometry.yaml")
    geo = data.get("geometry")
    if not geo:
        return jsonify({"ok": False, "error": "No geometry data"}), 400
    # Keep short lists inline in YAML output
    if "projector" in geo and "resolution" in geo["projector"]:
        geo["projector"]["resolution"] = _FlowList(geo["projector"]["resolution"])
    if "luminance_reference" in geo:
        geo["luminance_reference"] = [_FlowList(p) for p in geo["luminance_reference"]]
    path = GEO_DIR / name
    with open(path, "w") as f:
        yaml.dump(geo, f, default_flow_style=False, sort_keys=False)
    return jsonify({"ok": True, "name": name})


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
    """Return task_params_schema for all registered devices."""
    from devices.base import DEVICE_REGISTRY
    import devices.lick_sensor, devices.reward, devices.camera  # noqa
    import devices.photodiode, devices.display  # noqa

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

def _get_deploy_files(role: str) -> list[tuple[str, str]]:
    """Return list of (local_path, remote_path) for deployment."""
    files = [
        # Shared
        ("shared/config.py", "shared/config.py"),
        ("shared/stim_generator.py", "shared/stim_generator.py"),
        # Devices
        ("devices/__init__.py", "devices/__init__.py"),
        ("devices/base.py", "devices/base.py"),
        ("devices/reward.py", "devices/reward.py"),
        ("devices/reward_calibration.py", "devices/reward_calibration.py"),
        ("devices/lick_sensor.py", "devices/lick_sensor.py"),
        ("devices/camera.py", "devices/camera.py"),
        ("devices/photodiode.py", "devices/photodiode.py"),
        ("devices/display.py", "devices/display.py"),
        # Pi API
        ("pi_api/api.py", "pi_api/api.py"),
    ]
    if role == "leader":
        files += [
            ("engine/__init__.py", "engine/__init__.py"),
            ("engine/state_machine.py", "engine/state_machine.py"),
            ("engine/leader.py", "engine/leader.py"),
        ]
    elif role == "follower":
        files += [
            ("engine/__init__.py", "engine/__init__.py"),
            ("engine/follower.py", "engine/follower.py"),
        ]
    return files


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
