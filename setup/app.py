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
from flask import Flask, Response, jsonify, render_template, request

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
            # NB: no mac_dir — the controller's data root is machine-specific and
            # resolved locally by app/app.py ($VRFARM_DATA_DIR, else ~/VRFarm/data).
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
    needs_camera = "camera" in devices
    needs_gpio = any(d in devices for d in ["reward", "photodiode", "calibration_probe"])
    needs_i2c = any(d in devices for d in ["lick_sensor", "encoder", "display"])

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

        # 2. System packages (apt) — camera bindings + lgpio (its pip build needs swig, so use the
        #    apt-built binding and symlink it into the env like the camera bindings; no daemon).
        apt_packages = []
        if needs_camera:
            apt_packages.extend(["python3-libcamera", "python3-picamera2"])
        if needs_gpio:
            apt_packages.append("python3-lgpio")
        if apt_packages:
            apt_str = " ".join(apt_packages)
            _ssh(ssh_prefix,
                 f"sudo apt-get update -qq && sudo apt-get install -y {apt_str}",
                 timeout=300)
            steps.append(f"Installed system packages: {apt_str}")

        # 2b. Enable I2C — MPR121 lick + AS5600 encoder (leader), and the DLPC projector (follower,
        #     via dlp/linuxi2c.py raw ioctl on /dev/i2c-*). Idempotent.
        if needs_i2c:
            _ssh(ssh_prefix, "sudo raspi-config nonint do_i2c 0", timeout=20)
            steps.append("Enabled I2C")

        # 3. Symlink apt-built Python bindings into the conda env (camera + lgpio).
        #    picamera2/libcamera and python3-lgpio are apt-installed for the SYSTEM python and
        #    ship compiled .so files (e.g. _libcamera.cpython-3XX-*.so, _lgpio.cpython-3XX-*.so).
        #    The symlink only loads if the env python minor version == system python.
        if needs_camera or needs_gpio:
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
                    f"Setup aborted: conda 'rig' env python is {envpy} but system python "
                    f"is {syspy}. The apt-built bindings (picamera2/libcamera, lgpio) are "
                    f"compiled for {syspy} and won't import in a {envpy} env. Recreate the env "
                    f"to match: conda create -n rig python={syspy} -y (then reinstall deps)."
                )
            link_names, so_globs = [], []
            if needs_camera:
                link_names += ["libcamera", "picamera2", "pykms", "pidng", "videodev2", "prctl.py"]
                so_globs.append("_prctl*.so")
            if needs_gpio:
                link_names += ["lgpio.py"]
                so_globs.append("_lgpio*.so")
            cmd = (f"{conda_activate}"
                   "SITE=$(python -c 'import site; print(site.getsitepackages()[0])') && "
                   "SYS=/usr/lib/python3/dist-packages && "
                   + " && ".join(f"ln -sfn $SYS/{pkg} $SITE/{pkg}" for pkg in link_names))
            for g in so_globs:
                cmd += f" && for f in $SYS/{g}; do ln -sfn $f $SITE/$(basename $f); done"
            _ssh(ssh_prefix, cmd)
            steps.append(f"Symlinked apt bindings into conda env (py{envpy}): "
                         + ", ".join(link_names))

        # 4. Python packages (pip)
        packages = {"flask", "pyyaml", "numpy"}
        device_packages = {
            "lick_sensor": ["smbus2"],
            "reward": ["scipy"],        # lgpio comes from apt + symlink (step 2/3), not pip
            "camera": ["h5py", "pillow", "simplejpeg", "piexif", "av"],
            "photodiode": [],           # lgpio via apt + symlink; the DLPC needs no pip pkg (raw ioctl)
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

        # 5. Upload project files. scp won't create the ~/rig subdirs (shared/, devices/,
        #    engine/, pi_api/, calibration/), so make them first — otherwise the first file
        #    into a not-yet-existing subdir fails on a fresh Pi ("dest open ...: No such file").
        files_to_deploy = _get_deploy_files(role)
        remote_dirs = sorted({os.path.dirname(remote) for _, remote in files_to_deploy
                              if os.path.dirname(remote)})
        if remote_dirs:
            _ssh(ssh_prefix, "mkdir -p " + " ".join(f"~/rig/{d}" for d in remote_dirs))
        for local, remote in files_to_deploy:
            _scp(str(ROOT / local), f"{ssh_prefix}:~/rig/{remote}")
        steps.append(f"Deployed {len(files_to_deploy)} files")

        # 5b. Follower projector setup (static config, so it rides Install — not every Deploy):
        #     (a) push the vendored DLPC SDK to ~/dlp (it lives OUTSIDE ~/rig, so scp, not the
        #         REST /api/upload); (b) add the DLPC control-bus device-tree overlay to config.txt.
        if role == "follower":
            dlp_local = ROOT / "dlp"
            if dlp_local.exists():
                try:
                    _ssh(ssh_prefix, "mkdir -p ~/dlp", timeout=15)
                    items = [str(p) for p in dlp_local.iterdir() if p.name != "__pycache__"]
                    scp = subprocess.run(
                        ["scp", "-r", "-o", "ConnectTimeout=5", *items, f"{ssh_prefix}:~/dlp/"],
                        capture_output=True, text=True, timeout=90)
                    steps.append("Pushed ~/dlp/ (projector init)" if scp.returncode == 0
                                 else f"WARN ~/dlp/ scp: {scp.stderr.strip()[:100]}")
                except Exception as e:
                    steps.append(f"WARN ~/dlp/ push skipped: {e}")
            # The DLPC control I2C is a bit-banged bus on GPIO23(SDA)/22(SCL), exposed as /dev/i2c-22
            # (dlp/i2c.py opens bus 22) — created by this device-tree overlay. GPIO22/23 are outside
            # the DPI range (0-21), so no clash with the projector video. Needs a REBOOT to take effect.
            try:
                overlay = "dtoverlay=i2c-gpio,bus=22,i2c_gpio_sda=23,i2c_gpio_scl=22,i2c_gpio_delay_us=2"
                cfg = "/boot/firmware/config.txt"
                # Delete any prior i2c-gpio overlay on these pins (e.g. an earlier auto-bus variant)
                # and re-append the canonical bus=22 line — so re-running Install always leaves exactly
                # one, never a conflicting pair. The tail-byte check guarantees a newline boundary so
                # the append can't glue onto a config.txt that lacks a trailing newline.
                _ssh(ssh_prefix,
                     f"sudo sed -i '/^dtoverlay=i2c-gpio,.*i2c_gpio_scl=22/d' {cfg}; "
                     f"[ -n \"$(sudo tail -c1 {cfg})\" ] && echo | sudo tee -a {cfg} >/dev/null; "
                     f"echo '{overlay}' | sudo tee -a {cfg} >/dev/null",
                     timeout=15)
                steps.append("Set DLPC i2c-gpio overlay (bus=22) in config.txt — REBOOT the follower for /dev/i2c-22")
            except Exception as e:
                steps.append(f"WARN: DLPC overlay NOT set ({e}) — /dev/i2c-22 will be ABSENT and the projector "
                             "control bus won't work; fix passwordless sudo, then re-run Install and reboot")

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

        # (lgpio needs no daemon — it talks to /dev/gpiochip directly, so there's no pigpiod to enable.)
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

        # ~/dlp/ (projector DLPC-init SDK) is pushed at Install now (it's static) — Deploy no longer
        # re-sends it. Reflashed a follower? Re-run Install to restore ~/dlp/.

        # Restart pi_api so the just-uploaded code actually RUNS. A long-running Python
        # process won't pick up overwritten files on its own — this is why a redeploy alone
        # left the old code (and stale in-memory warp) in effect. /api/restart self-kills and
        # systemd (Restart=always) respawns with the new code. Re-init the display afterwards.
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


@app.route("/api/camera_feed")
def camera_feed():
    """Proxy the Pi's MJPEG camera stream through the setup app (same-origin), so the setup UI
    preview works from any browser that can reach this app — mirrors app/app.py:camera_feed.
    The setup app runs threaded=True, so this long-lived stream doesn't block other requests."""
    if not _rig_config:
        return "No rig", 400
    api_port = _rig_config["network"]["api_port"]
    ip = None
    for pi in _rig_config["pis"]:
        if "camera" in pi.get("devices", []):
            ip = pi["ip"]
            break
    if not ip:
        return "No camera", 400

    # Reconnecting relay: don't collapse a transient Pi hiccup (preview not started yet, Reinit,
    # pi_api restart) into a silent 200-blank the setup <img> can't recover from. See
    # shared/mjpeg_relay.py.
    from shared.mjpeg_relay import relay
    return Response(relay(f"http://{ip}:{api_port}/api/camera_stream"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


# Device init order (display/projector first so the screen is up before the rest).
_INIT_ORDER = ["display", "lick_sensor", "reward", "camera",
               "photodiode", "encoder", "calibration_probe"]


def _init_one_device(dev_name, devices, post):
    """Run the Pi-side init for a SINGLE device. `post(endpoint, payload, label, timeout) -> (ok, msg)`
    is supplied by the caller (it POSTs to the device's Pi and logs to the caller's steps). Returns
    (ok, msg). Shared by /api/init_devices (loop over all) and /api/reinit_device (one card)."""
    cfg = devices.get(dev_name, {})
    if dev_name == "display":
        ok1, _ = post("/api/init_projector", {}, "Projector", 35)
        ok2, msg = post("/api/init_display", {"rig_config": _display_init_cfg(devices)}, "Display init", 30)
        return (ok1 and ok2), msg
    if dev_name == "camera":
        # Release any stuck/streaming camera first so a frozen preview recovers, then check the sensor.
        post("/api/camera_preview_stop", {}, "Camera release", 15)
        return post("/api/init_camera", {}, "Camera", 15)
    if dev_name == "lick_sensor":
        return post("/api/init_lick",
                    {"i2c_address": cfg.get("i2c_address", "0x5A"), "electrode": cfg.get("electrode", 4)},
                    "Lick sensor", 10)
    if dev_name == "reward":
        return post("/api/init_reward",
                    {"pins": cfg.get("pins", {"main": {"gpio": 18}})}, "Reward", 10)
    if dev_name == "photodiode":
        return post("/api/init_photodiode", {
            "gpio": cfg.get("gpio", 24),
            "glitch_enabled": cfg.get("glitch_enabled", True),
            "glitch_ms": cfg.get("glitch_ms", 0.5),
            "debounce_enabled": cfg.get("debounce_enabled", True),
            "debounce_ms": cfg.get("debounce_ms", 5),
        }, "Photodiode", 10)
    if dev_name == "encoder":
        return post("/api/init_encoder", {
            "i2c_address": cfg.get("i2c_address", "0x36"),
            "i2c_bus": cfg.get("i2c_bus", 1),
            "wheel_diameter_cm": cfg.get("wheel_diameter_cm", 15.0),
            "sample_hz": cfg.get("sample_hz", 100),
        }, "Encoder", 10)
    if dev_name == "calibration_probe":
        return post("/api/init_calibration_probe", {"gpio": cfg.get("gpio", 22)}, "Calibration probe", 10)
    return False, f"no init handler for '{dev_name}'"


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

    for dev_name in _INIT_ORDER:
        if dev_name in devices and devices[dev_name].get("enabled"):
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
    if dev_name not in devices or not devices[dev_name].get("enabled"):
        return jsonify({"ok": False, "error": f"{dev_name} is not enabled"}), 400
    ip = next((pi["ip"] for pi in _rig_config.get("pis", [])
               if dev_name in pi.get("devices", [])), None)
    if not ip:
        return jsonify({"ok": False, "error": f"{dev_name} is not assigned to a Pi"}), 400

    steps = []
    ok, _msg = _init_one_device(
        dev_name, devices, _mk_post(ip, _rig_config["network"]["api_port"], steps))
    return jsonify({"ok": ok, "device": dev_name, "steps": steps})


def _display_lum_mode():
    """The rig's stored display luminance-correction mode (empirical|theoretical|none)."""
    try:
        return (_rig_config or {}).get("devices", {}).get("display", {}).get(
            "luminance_correction", "theoretical")
    except Exception:
        return "theoretical"


def _generate_and_deploy_warp(geo_file, lum_mode="theoretical"):
    """Build warp_map.npz on the Mac from `geo_file` with the given luminance mode, then
    ATOMICALLY scp it + the geometry to every Pi and reload the live display. Shared by the
    Regenerate Warp button and the intensity-cal Apply. Returns (ok, steps, error)."""
    steps = []
    # 1. Run compute_warp_map.py locally on the controller, from the SELECTED geometry file,
    #    baking in the luminance mode (empirical re-injects luminance_cal_latest.yaml, so a
    #    measured correction survives regeneration).
    # Use the interpreter running this setup UI (the vrfarm env on Mac or Linux), not a
    # hardcoded macOS miniforge path — that env has numpy/scipy/yaml for the warp build.
    python = sys.executable
    script = str(GEO_DIR / "compute_warp_map.py")
    r = subprocess.run(
        [python, script, "--geo", str(geo_file), "--lum-mode", lum_mode],
        capture_output=True, text=True, timeout=60, cwd=str(GEO_DIR))
    if r.returncode != 0:
        return False, steps, r.stderr[-500:]
    steps.append(f"Generated warp map on Mac from {geo_file.name} (luminance: {lum_mode})")

    npz_path = GEO_DIR / "warp_map.npz"
    if not npz_path.exists():
        return False, steps, "warp_map.npz not created"

    # 2. Copy to all Pis — ATOMIC install: scp to a .tmp name, then mv into place. A plain scp
    #    overwrites in place, so a reader (display init -> load_warp, or stim-gen np.load) that
    #    touches it mid-write sees a half-written zip -> "Bad CRC-32". mv is atomic.
    if _rig_config:
        for pi in _rig_config.get("pis", []):
            user = pi.get("user", "vruser")
            ip = pi["ip"]
            try:
                _ssh(f"{user}@{ip}", "mkdir -p ~/rig/calibration")
                _scp(str(npz_path), f"{user}@{ip}:~/rig/calibration/warp_map.npz.tmp")
                # Push the SAME geometry the warp was built from, so the live calib_geo.py
                # tool on the Pi starts from these values.
                _scp(str(geo_file), f"{user}@{ip}:~/rig/calibration/rig_geometry.yaml.tmp")
                _ssh(f"{user}@{ip}",
                     "mv ~/rig/calibration/warp_map.npz.tmp ~/rig/calibration/warp_map.npz && "
                     "mv ~/rig/calibration/rig_geometry.yaml.tmp ~/rig/calibration/rig_geometry.yaml")
                steps.append(f"Copied warp_map.npz + rig_geometry.yaml to {pi['name']} ({ip})")
                # Tell the RUNNING display to re-read the new warp (load_warp is one-shot at init).
                try:
                    api_port = (_rig_config.get("network") or {}).get("api_port", 5080)
                    rr = requests.post(f"http://{ip}:{api_port}/api/reload_warp", timeout=5)
                    if rr.ok and rr.json().get("reloaded"):
                        steps.append(f"Reloaded warp into live display on {pi['name']}")
                    else:
                        steps.append(f"Warp saved on {pi['name']} — display not active, "
                                     f"loads on next Init Display")
                except Exception as e:
                    steps.append(f"(warp reload skipped on {pi['name']}: {e})")
            except Exception as e:
                steps.append(f"Failed to copy to {pi['name']}: {e}")
    return True, steps, None


@app.route("/api/generate_warp", methods=["POST"])
def api_generate_warp():
    """Generate warp map from rig_geometry.yaml, then SCP to Leader Pi."""
    data = request.json or {}
    # Resolve the geometry file SERVER-SIDE under GEO_DIR (don't trust a client cwd-relative
    # path). Prefer a bare filename; fall back to a path resolved against the repo root.
    name = data.get("geometry")
    if name:
        geo_file = (GEO_DIR / Path(name).name).resolve()
    else:
        gp = data.get("geometry_path", str(GEO_DIR / "rig_geometry.yaml"))
        geo_file = (Path(gp) if Path(gp).is_absolute() else (ROOT / gp)).resolve()

    if not geo_file.exists():
        return jsonify({"ok": False, "error": f"Geometry file not found: {geo_file}"}), 404

    # Luminance mode: an explicit request wins, else the rig's stored display setting.
    lum_mode = data.get("lum_mode") or _display_lum_mode()
    try:
        ok, steps, err = _generate_and_deploy_warp(geo_file, lum_mode)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": []})
    return jsonify({"ok": ok, "error": err, "steps": steps})


# ── Intensity (luminance) calibration ──

def _read_pm100d():
    """Return one power reading (W) from a USB-connected Thorlabs PM100D on the Mac. Optional
    dependency (pyvisa + ThorlabsPM100); raises on any failure so the caller degrades to manual
    entry. Thorlabs USB vendor id is 0x1313."""
    import pyvisa
    from ThorlabsPM100 import ThorlabsPM100
    rm = pyvisa.ResourceManager()
    resources = list(rm.list_resources())
    thor = [r for r in resources if "0x1313" in r.upper() or "::1313::" in r]
    candidates = thor or [r for r in resources if "USB" in r.upper()]
    if not candidates:
        raise RuntimeError("no USB instrument found (is the PM100D connected?)")
    inst = rm.open_resource(candidates[0], timeout=3000)
    try:
        return float(ThorlabsPM100(inst=inst).read)
    finally:
        inst.close()


@app.route("/api/lum_read", methods=["POST"])
def api_lum_read():
    """Read the PM100D once. Returns {ok, value}. On any failure returns {ok:false} so the
    intensity-cal panel silently falls back to manual entry — the USB driver is not required."""
    try:
        return jsonify({"ok": True, "value": _read_pm100d()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/lum_apply", methods=["POST"])
def api_lum_apply():
    """Apply a luminance-correction mode and redeploy the warp. Body:
      {mode: empirical|theoretical|none, measurements?: [{az_deg, reading}], geometry?: <file>}
    For `empirical`, fit the measurements → luminance_cal_latest.yaml first; then rebuild the
    warp with --lum-mode and ship it to the Pis via the shared deploy helper."""
    if not _rig_config:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400
    data = request.json or {}
    mode = data.get("mode", "theoretical")
    if mode not in ("empirical", "theoretical", "none"):
        return jsonify({"ok": False, "error": f"Unknown mode: {mode}"}), 400

    name = data.get("geometry")
    geo_file = ((GEO_DIR / Path(name).name) if name
                else (GEO_DIR / "rig_geometry.yaml")).resolve()
    if not geo_file.exists():
        return jsonify({"ok": False, "error": f"Geometry file not found: {geo_file}"}), 404

    steps = []
    fit = None
    if mode == "empirical":
        valid = [m for m in (data.get("measurements") or [])
                 if m.get("reading") not in (None, "") and m.get("az_deg") is not None]
        if len(valid) < 2:
            return jsonify({"ok": False,
                            "error": "Need at least 2 azimuth readings to fit."}), 400
        try:
            sys.path.insert(0, str(GEO_DIR))
            from fit_luminance_correction import fit_luminance, save_luminance_cal
            az, gain, corr = fit_luminance(valid)
            out = save_luminance_cal(az, gain, corr, source_file="setup-ui")
            steps.append(f"Fitted {len(valid)} readings → {out.name}")
            fit = {"az": [float(x) for x in az],
                   "gain": [float(x) for x in gain],
                   "correction": [float(x) for x in corr]}
        except Exception as e:
            return jsonify({"ok": False, "error": f"Fit failed: {e}", "steps": steps}), 500

    try:
        ok, dsteps, err = _generate_and_deploy_warp(geo_file, mode)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})
    steps += dsteps

    # Reflect the applied mode in the in-memory rig so a later Regenerate Warp matches; the
    # client also sets it in the rig object and Save Rig persists it to disk.
    try:
        _rig_config["devices"]["display"]["luminance_correction"] = mode
    except Exception:
        pass

    return jsonify({"ok": ok, "error": err, "mode": mode, "fit": fit, "steps": steps})


def _reinit_projector(target):
    """Force a full projector bring-up on the display Pi over SSH: start_projector.sh sets
    GPIO ALT2, re-inits the DLPC parallel input (fixes the common blank-projector case), and
    kills+restarts X. Non-blocking (X starts backgrounded). Returns (ok, last-line-message)."""
    try:
        out = _ssh(target, "bash ~/rig/start_projector.sh", timeout=60)
        return True, (out.strip().splitlines() or ["projector re-initialized"])[-1]
    except Exception as e:
        return False, str(e)


def _drive_calibration_probe(high: bool):
    """Latch the calibration-probe TTL high/low via its Pi's pi_api endpoint (same
    persistent device the manual card button drives, so they stay in sync). Returns a
    step string, or None if no probe is configured. Non-fatal: geometry calibration
    proceeds regardless of the probe."""
    if not _rig_config:
        return None
    probe_pi = next((pi for pi in _rig_config.get("pis", [])
                     if "calibration_probe" in pi.get("devices", [])), None)
    if not probe_pi:
        return None   # no probe on this rig — nothing to drive
    api_port = _rig_config["network"]["api_port"]
    endpoint = "/api/probe_on" if high else "/api/probe_off"
    edge = "HIGH" if high else "LOW"
    try:
        r = requests.post(f"http://{probe_pi['ip']}:{api_port}{endpoint}", timeout=5)
        if r.json().get("ok"):
            return f"Calibration probe {edge}"
        return f"WARN calibration probe {edge} skipped: {r.json().get('error', '')} (run Init Devices)"
    except Exception as e:
        return f"WARN calibration probe {edge} failed: {e}"


@app.route("/api/start_calibration", methods=["POST"])
def api_start_calibration():
    """Deploy the geometry-calibration tools to the display Pi and launch calib_geo
    (live sliders on :5091). Returns the sliders URL for the browser to open."""
    if not _rig_config:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400
    target_pi = next((pi for pi in _rig_config.get("pis", [])
                      if "display" in pi.get("devices", [])), None)
    if not target_pi:
        return jsonify({"ok": False, "error": "No Pi has the display device"}), 400
    user = target_pi.get("user", "vruser")
    ip = target_pi["ip"]
    target = f"{user}@{ip}"

    name = (request.json or {}).get("geometry") or "rig_geometry.yaml"
    geo_data = (request.json or {}).get("geometry_data")   # live Display-card params
    geo_file = (GEO_DIR / Path(name).name).resolve()
    tools = ["calib_geo.py", "cal_start.sh", "cal_stop.sh",
             "panel_grid.py", "validate_calibration_pygame.py"]
    steps = []
    try:
        _ssh(target, "mkdir -p ~/rig/calibration")
        srcs = [str(GEO_DIR / t) for t in tools if (GEO_DIR / t).exists()]
        if srcs:
            r = subprocess.run(["scp", "-o", "ConnectTimeout=5", *srcs,
                                f"{target}:~/rig/calibration/"],
                               capture_output=True, text=True, timeout=45)
            if r.returncode != 0:
                raise RuntimeError(f"scp tools failed: {r.stderr.strip()}")
        steps.append(f"Deployed calibration tools to {target_pi['name']} ({ip})")

        # Initialize calib_geo with the LIVE Display-card params: overwrite the Pi's
        # rig_geometry.yaml so Calibrate always starts from what's on the card. (Load
        # defaults inside the tool stays a separate, manual action.)
        if geo_data:
            try:
                g = dict(geo_data)
                if isinstance(g.get("projector"), dict) and "resolution" in g["projector"]:
                    g["projector"]["resolution"] = _FlowList(g["projector"]["resolution"])
                import tempfile
                fd, tmp = tempfile.mkstemp(suffix=".yaml"); os.close(fd)
                with open(tmp, "w") as f:
                    yaml.dump(g, f, default_flow_style=False, sort_keys=True)
                _scp(tmp, f"{target}:~/rig/calibration/rig_geometry.yaml")
                os.unlink(tmp)
                steps.append("Initialized projector geometry from the Display card")
            except Exception as e:
                steps.append(f"WARN: geometry init failed ({e}); using the Pi's existing file")
        else:
            # Fallback (no live data sent): seed only if the Pi has none — never clobber.
            chk = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", target,
                 "test -e ~/rig/calibration/rig_geometry.yaml"],
                capture_output=True, text=True, timeout=15)
            if chk.returncode != 0 and geo_file.exists():
                _scp(str(geo_file), f"{target}:~/rig/calibration/rig_geometry.yaml")
                steps.append(f"Seeded rig_geometry.yaml from {geo_file.name}")

        # Force a projector re-init so calib_geo opens on a fresh display with the DLPC
        # parallel input correctly configured (requested: reinit on Calibrate/Stop). If it
        # fails it's non-fatal — cal_start.sh also inits the projector when X is down.
        ok, msg = _reinit_projector(target)
        steps.append(f"Projector re-init: {msg}" if ok else f"WARN projector re-init: {msg}")

        out = _ssh(target, "bash ~/rig/calibration/cal_start.sh geo", timeout=45)
        steps.append((out.strip().splitlines() or ["started"])[0])
        note = _drive_calibration_probe(True)   # TTL HIGH while calibrating
        if note:
            steps.append(note)
        return jsonify({"ok": True, "url": f"http://{ip}:5091", "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


@app.route("/api/stop_calibration", methods=["POST"])
def api_stop_calibration():
    """Stop the geometry-calibration tool on the display Pi (cal_stop.sh)."""
    if not _rig_config:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400
    target_pi = next((pi for pi in _rig_config.get("pis", [])
                      if "display" in pi.get("devices", [])), None)
    if not target_pi:
        return jsonify({"ok": False, "error": "No Pi has the display device"}), 400
    target = f"{target_pi.get('user', 'vruser')}@{target_pi['ip']}"
    steps = []
    try:
        out = _ssh(target, "bash ~/rig/calibration/cal_stop.sh", timeout=20)
        steps.append((out.strip().splitlines() or ["Calibration tool stopped"])[0])
        note = _drive_calibration_probe(False)  # TTL LOW when calibration stops
        if note:
            steps.append(note)
        # Re-init the projector so the normal display comes back cleanly (requested).
        ok, msg = _reinit_projector(target)
        steps.append(f"Projector re-init: {msg}" if ok else f"WARN projector re-init: {msg}")
        return jsonify({"ok": True, "msg": (out.strip() or "stopped"), "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


_cwm_mod = None


def _cwm():
    """Lazily import display_calibration/compute_warp_map.py (numpy/scipy geometry)."""
    global _cwm_mod
    if _cwm_mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "compute_warp_map", str(GEO_DIR / "compute_warp_map.py"))
        _cwm_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_cwm_mod)
    return _cwm_mod


@app.route("/api/derived_geometry", methods=["POST"])
def api_derived_geometry():
    """Az/alt range the warp actually fills for the given geometry (ray-traced within the
    frame) — feeds the Display card's derived Az/Alt readouts."""
    geo = (request.json or {}).get("geometry_data")
    if not geo:
        return jsonify({"ok": False, "error": "no geometry_data"}), 400
    try:
        return jsonify({"ok": True, **_cwm().derived_angle_ranges(geo)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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
    path = GEO_DIR / name
    with open(path, "w") as f:
        # sort_keys=True keeps this writer byte-for-byte consistent with the
        # standalone calib_geo.py dumper, so saving from either side gives no diff churn.
        yaml.dump(geo, f, default_flow_style=False, sort_keys=True)
    return jsonify({"ok": True, "name": name})


@app.route("/api/receive_geometry", methods=["POST"])
def api_receive_geometry():
    """Called by calib_geo on the projector Pi when the user hits Save: lands the tuned
    geometry in the repo as the canonical rig_geometry.yaml PLUS a timestamped archive
    (rig_geometry_YYYYMMDD_HHMM.yaml) so the most recent is obvious."""
    from datetime import datetime
    text = (request.json or {}).get("yaml")
    if not text:
        return jsonify({"ok": False, "error": "No yaml"}), 400
    try:
        yaml.safe_load(text)  # validate it parses before writing
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid YAML: {e}"}), 400
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    archive = f"rig_geometry_{stamp}.yaml"
    (GEO_DIR / archive).write_text(text)
    (GEO_DIR / "rig_geometry.yaml").write_text(text)
    print(f"[receive_geometry] archived {archive} + updated canonical rig_geometry.yaml")
    return jsonify({"ok": True, "archived": archive, "canonical": "rig_geometry.yaml"})


@app.route("/api/send_geometry", methods=["POST"])
def api_send_geometry():
    """Push a chosen (usually timestamped) geometry from the repo to the projector Pi as
    its canonical rig_geometry.yaml, so the next warp/deploy uses it."""
    if not _rig_config:
        return jsonify({"ok": False, "error": "No rig loaded"}), 400
    name = Path((request.json or {}).get("name", "")).name
    src = GEO_DIR / name
    if not name or not src.exists():
        return jsonify({"ok": False, "error": f"Not found: {name}"}), 400
    target_pi = next((pi for pi in _rig_config.get("pis", [])
                      if "display" in pi.get("devices", [])), None)
    if not target_pi:
        return jsonify({"ok": False, "error": "No Pi has the display device"}), 400
    target = f"{target_pi.get('user', 'vruser')}@{target_pi['ip']}"
    try:
        _ssh(target, "mkdir -p ~/rig/calibration")
        _scp(str(src), f"{target}:~/rig/calibration/rig_geometry.yaml")
        return jsonify({"ok": True, "sent": name, "to": target_pi["name"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
    import devices.calibration_probe, devices.encoder  # noqa

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

def _display_init_cfg(devices: dict) -> dict:
    """Display init payload: the display block plus the photodiode card's sync-square
    prefs (corner/size/brightness) — the square is drawn by the display renderer, but authored in
    the photodiode card. Mirrors engine/follower.py's injection at session start."""
    pd = devices.get("photodiode", {}) or {}
    cfg = dict(devices.get("display", {}) or {})
    for k in ("sync_corner", "sync_size_px", "sync_brightness"):
        if k in pd:
            cfg[k] = pd[k]
    return cfg


def _get_deploy_files(role: str) -> list[tuple[str, str]]:
    """Return list of (local_path, remote_path) for deployment."""
    files = [
        # Shared
        ("shared/__init__.py", "shared/__init__.py"),
        ("shared/config.py", "shared/config.py"),
        ("shared/stim_generator.py", "shared/stim_generator.py"),
        ("shared/consolidate.py", "shared/consolidate.py"),   # data consolidation at Transfer
        # Devices
        ("devices/__init__.py", "devices/__init__.py"),
        ("devices/base.py", "devices/base.py"),
        ("devices/reward.py", "devices/reward.py"),
        ("devices/reward_calibration.py", "devices/reward_calibration.py"),
        ("devices/lick_sensor.py", "devices/lick_sensor.py"),
        ("devices/camera.py", "devices/camera.py"),
        ("devices/photodiode.py", "devices/photodiode.py"),
        ("devices/display.py", "devices/display.py"),
        ("devices/calibration_probe.py", "devices/calibration_probe.py"),
        ("devices/encoder.py", "devices/encoder.py"),
        # Pi API
        ("pi_api/api.py", "pi_api/api.py"),
    ]
    if role == "leader":
        files += [
            ("engine/__init__.py", "engine/__init__.py"),
            ("engine/leader.py", "engine/leader.py"),
        ]
    elif role == "follower":
        files += [
            ("engine/__init__.py", "engine/__init__.py"),
            ("engine/follower.py", "engine/follower.py"),
            # The display Pi (follower) also gets projector bring-up + the calibration tools:
            # start_projector.sh -> ~/rig/; the rest -> ~/rig/calibration/ (same place the
            # Calibrate button uses). NOTE: start_projector.sh calls ~/dlp/init_parallel_mode.py.
            # The dlp/ SDK is vendored in this repo but pushed to ~/dlp/ at INSTALL, not Deploy
            # (it's static — see step 5b) — it lives outside ~/rig so it rides scp, not /api/upload.
            ("display_calibration/start_projector.sh", "start_projector.sh"),
            ("display_calibration/calib_geo.py", "calibration/calib_geo.py"),
            ("display_calibration/cal_start.sh", "calibration/cal_start.sh"),
            ("display_calibration/cal_stop.sh", "calibration/cal_stop.sh"),
            ("display_calibration/panel_grid.py", "calibration/panel_grid.py"),
            ("display_calibration/validate_calibration_pygame.py",
             "calibration/validate_calibration_pygame.py"),
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
