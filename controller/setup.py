"""
controller/setup.py

The Setup tab's backend: every route setup/app.py had, now under
/api/rigs/<rig>/setup/... and reading the rig from the registry instead of the
`_rig_config` global. Rig creation and Pi identity editing moved to the Network tab
(controller/network.py); the device catalog, device init, calibration, warp and
luminance work, Install / Deploy / Restart / Reboot / Shutdown per Pi all stay here.

Calibration data lives per rig in display_calibration/<rig>/{geometry,intensity}/ plus
display_calibration/<rig>/warp_map.npz — see controller/calib_paths.py. The scripts live in the
shared display_calibration/ folder.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml
from flask import Blueprint, Response, g, jsonify, request

from shared.config import save_rig_atomic, photodiode_init_payload
from shared.mjpeg_relay import relay
from controller import settings, calib_paths
from controller.registry import registry, RigState
from controller.ssh import ssh, scp, ssh_merged, ssh_ok, target as ssh_target

ROOT = settings.ROOT
TOOLS_DIR = ROOT / "display_calibration"      # the calibration scripts (shared by all rigs)

bp = Blueprint("setup", __name__, url_prefix="/api/rigs/<rig>/setup")


@bp.url_value_preprocessor
def _pull_rig(endpoint, values):
    g.rig_name = (values or {}).pop("rig", None)


@bp.before_request
def _require_loaded():
    try:
        g.rs = registry.get(g.rig_name)
    except KeyError:
        try:
            g.rs = registry.load(g.rig_name)     # Setup only needs the config; no Pi contact
        except FileNotFoundError:
            return jsonify({"ok": False, "error": f"rig '{g.rig_name}' not found"}), 404
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)}), 409
    return None


def geo_dir(rs: RigState) -> Path:
    """display_calibration/<rig>/geometry/ (created on first use)."""
    return calib_paths.geometry_dir(rs.name)


def lum_dir(rs: RigState) -> Path:
    """display_calibration/<rig>/intensity/ (created on first use)."""
    return calib_paths.intensity_dir(rs.name)


def _geo_file(rs: RigState, name: str | None) -> Path | None:
    """Geometry `name` in this rig's folder, or the rig's current geometry when name is empty."""
    return calib_paths.resolve_geometry(rs.name, rs.config, name)


def _pi_user(rs: RigState, ip: str, fallback: str | None = None) -> str:
    for pi in rs.pis:
        if pi.get("ip") == ip:
            return pi.get("user") or "vruser"
    return fallback or "vruser"


def _own_pi(rs: RigState, ip: str) -> bool:
    return ip in rs.pi_ips()


def _display_pi(rs: RigState):
    return next((pi for pi in rs.pis if "display" in pi.get("devices", [])), None)


# ── rig config ──

@bp.route("/config")
def api_config():
    """The rig as loaded (the page's `rig` object). Also reports the data root for display."""
    rs: RigState = g.rs
    return jsonify(rs.config)


@bp.route("/save", methods=["POST"])
def api_save():
    """Write the whole posted rig back to rigs/<rig>.yaml (atomic) and re-read it into the
    registry. Refused while the rig is running a session."""
    rs: RigState = g.rs
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "rig is running a session — save after it ends"}), 409
    data = request.json or {}
    if not isinstance(data, dict) or "pis" not in data:
        return jsonify({"ok": False, "error": "not a rig config"}), 400
    data["name"] = rs.name
    save_rig_atomic(data, rs.path)
    registry.reload_config(rs.name)
    rs.logger.info("rig config saved")
    return jsonify({"ok": True})


# ── Pi checks and lifecycle ──

@bp.route("/check_pi", methods=["POST"])
def api_check_pi():
    """Is a Pi reachable? SSH (echo ok, as that Pi's own user) and the REST API status."""
    rs: RigState = g.rs
    data = request.json or {}
    ip = data.get("ip")
    if not ip:
        return jsonify({"ok": False, "error": "no ip"}), 400
    user = data.get("user") or _pi_user(rs, ip)
    port = data.get("api_port", rs.api_port)
    result = {"ip": ip, "ssh": False, "api": False}
    result["ssh"] = ssh_ok(f"{user}@{ip}")
    try:
        r = requests.get(f"http://{ip}:{port}/api/status", timeout=3)
        result["api"] = r.status_code == 200
        result["status"] = r.json()
    except Exception:
        pass
    name = next((pi["name"] for pi in rs.pis if pi.get("ip") == ip), ip)
    rs.pi_status[name] = {"ok": result["api"], "ssh": result["ssh"], "api": result["api"], "t": time.time()}
    return jsonify(result)


@bp.route("/install_pi", methods=["POST"])
def api_install_pi():
    """First-time Pi setup via SSH: conda env, system + Python packages, project files, the
    systemd units (vrfarm, displayd on followers, shepherd on leaders)."""
    rs: RigState = g.rs
    data = request.json or {}
    ip = data["ip"]
    user = data.get("user") or _pi_user(rs, ip)
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
        # 1. Directories
        ssh(ssh_prefix, "mkdir -p ~/rig ~/data")
        steps.append("Created ~/rig ~/data")

        # 1a. One time zone on every Pi, so leader and follower logs line up (recorded data is
        #     Unix time either way).
        ssh(ssh_prefix, "sudo timedatectl set-timezone Europe/Vienna", timeout=20)
        steps.append("Time zone set to Europe/Vienna")

        # 1b. conda 'rig' env pinned to the SYSTEM python version (the apt-built camera
        #     bindings only load in an env of the same minor version).
        ssh(ssh_prefix,
            "source ~/miniforge3/etc/profile.d/conda.sh && "
            "SYSPY=$(python3 -c 'import sys; print(str(sys.version_info.major)+\".\"+str(sys.version_info.minor))') && "
            "conda env list | awk '{print $1}' | grep -qx rig || "
            "conda create -n rig python=$SYSPY -y",
            timeout=400)
        steps.append("Ensured conda 'rig' env (matched to system python)")

        # 2. System packages (apt). rsync on EVERY Pi: the Data tab syncs sessions off the
        #    leaders with rsync over SSH, and the followers get it too so a role change or a
        #    future follower-side sync never needs a re-Install.
        apt_packages = ["rsync"]
        if needs_camera:
            # camera bindings + ffmpeg (consolidation remuxes video.h264 -> video.mp4 on the leader)
            apt_packages.extend(["python3-libcamera", "python3-picamera2", "ffmpeg"])
        if needs_gpio:
            apt_packages.append("python3-lgpio")
        # Deliberately NO X packages for the display: under full KMS displayd's renderer is the
        # sole DRM master. Its system pygame/numpy/yaml come in the displayd step below.
        apt_str = " ".join(apt_packages)
        ssh(ssh_prefix, f"sudo apt-get update -qq && sudo apt-get install -y {apt_str}", timeout=300)
        steps.append(f"Installed system packages: {apt_str}")

        # 2b. I2C — MPR121 lick + AS5600 encoder (leader), DLPC projector (follower). Idempotent.
        if needs_i2c:
            ssh(ssh_prefix, "sudo raspi-config nonint do_i2c 0", timeout=20)
            steps.append("Enabled I2C")

        # 3. Symlink the apt-built bindings into the conda env (camera + lgpio).
        if needs_camera or needs_gpio:
            envpy, syspy = ssh(
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
                    f"to match: conda create -n rig python={syspy} -y (then reinstall deps).")
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
            for gpat in so_globs:
                cmd += f" && for f in $SYS/{gpat}; do ln -sfn $f $SITE/$(basename $f); done"
            ssh(ssh_prefix, cmd)
            steps.append(f"Symlinked apt bindings into conda env (py{envpy}): " + ", ".join(link_names))

        # 4. Python packages (pip). NO pygame in the conda env: everything that opens the
        #    display runs on the SYSTEM python3 (the env's SDL has no kmsdrm backend).
        packages = {"flask", "pyyaml", "numpy"}
        device_packages = {
            "lick_sensor": ["smbus2"],
            "reward": ["scipy"],
            "camera": ["h5py", "pillow", "simplejpeg", "piexif", "av"],
            "photodiode": ["pyserial"],
            "display": [],
        }
        for dev in devices:
            packages.update(device_packages.get(dev, []))
        if role == "leader":
            packages.add("h5py")
        pkg_str = " ".join(sorted(packages))
        ssh(ssh_prefix, f"{conda_activate} pip install {pkg_str}", timeout=120)
        steps.append(f"Installed Python packages: {pkg_str}")

        # 4b. Teensy toolchain on the Pi that owns the photodiode: the Setup UI compiles and
        #     flashes the sync-detector sketch THERE (the Teensy is on its USB). Needs internet
        #     on the Pi; a failure is a warning — Upload retries the install itself.
        if "photodiode" in devices:
            try:
                _install_teensy_toolchain(ssh_prefix, steps)
            except Exception as e:
                steps.append(f"WARN: Teensy toolchain not installed ({e}) — Upload will retry")

        # 5. Project files (the deploy manifest). Make the ~/rig subdirs first.
        from shared.deploy_manifest import deploy_files
        files_to_deploy = deploy_files(role)
        remote_dirs = sorted({os.path.dirname(remote) for _, remote in files_to_deploy
                              if os.path.dirname(remote)})
        if remote_dirs:
            ssh(ssh_prefix, "mkdir -p " + " ".join(f"~/rig/{d}" for d in remote_dirs))
        for local, remote in files_to_deploy:
            scp(str(ROOT / local), f"{ssh_prefix}:~/rig/{remote}")
        steps.append(f"Deployed {len(files_to_deploy)} files")

        # 5b. Follower projector setup (static, so it rides Install): the DLPC SDK to ~/dlp and
        #     the full-KMS + DPI boot config. Needs a REBOOT.
        if role == "follower":
            dlp_local = ROOT / "dlp"
            if dlp_local.exists():
                try:
                    ssh(ssh_prefix, "mkdir -p ~/dlp", timeout=15)
                    items = [str(p) for p in dlp_local.iterdir() if p.name != "__pycache__"]
                    r = subprocess.run(["scp", "-r", "-o", "ConnectTimeout=5", *items, f"{ssh_prefix}:~/dlp/"],
                                       capture_output=True, text=True, timeout=90)
                    steps.append("Pushed ~/dlp/ (projector init)" if r.returncode == 0
                                 else f"WARN ~/dlp/ scp: {r.stderr.strip()[:100]}")
                except Exception as e:
                    steps.append(f"WARN ~/dlp/ push skipped: {e}")
            try:
                ssh(ssh_prefix,
                    "sudo cp -n /boot/firmware/config.txt /boot/firmware/config_default.txt; "
                    "sudo cp ~/dlp/sample_config/config_kms.txt /boot/firmware/config.txt; "
                    "sudo cp ~/dlp/sample_config/config_dlp.txt /boot/firmware/config_fkms_backup.txt; "
                    "sudo rm -f /etc/X11/xorg.conf; "
                    "sudo systemctl set-default multi-user.target",
                    timeout=25)
                steps.append("Projector: wrote config.txt (FULL KMS + DPI, i2c-gpio bus=22) + console boot; "
                             "stock saved as config_default.txt, FKMS kept as config_fkms_backup.txt; "
                             "any /etc/X11/xorg.conf removed — REBOOT the follower")
            except Exception as e:
                steps.append(f"WARN: projector config NOT applied ({e}) — no DPI/i2c-22, the DLP will only show "
                             "its test pattern. Fix passwordless sudo, then re-run Install and reboot")

            # 5c. displayd prerequisites (system python3 pygame/numpy/yaml, held) + its unit.
            #     The Mesa EGL/GLES stack is spelled out because a Lite image lacks it and
            #     libsdl2 does not depend on it: without libegl1 + the v3d DRI driver SDL's
            #     kmsdrm backend cannot create an EGL context, falls back to a null driver, and
            #     the renderer dies with NULL_BACKEND "EGL not initialized" (morbier-dlp, 2026-09-29).
            ssh(ssh_prefix,
                "sudo apt-get update -qq && sudo apt-get install -y "
                "python3-pygame python3-numpy python3-yaml libdrm-tests "
                "libegl1 libegl-mesa0 libgles2 libgl1 libgl1-mesa-dri && "
                "sudo apt-mark hold libsdl2-2.0-0 python3-pygame",
                timeout=300)
            steps.append("Installed displayd system packages (pygame/numpy/yaml/libdrm-tests + Mesa "
                         "EGL/GL: libegl1 libgles2 libgl1 libgl1-mesa-dri; libsdl2 + python3-pygame held)")
            svc_text = (ROOT / "displayd" / "displayd.service").read_text()
            svc_text = svc_text.replace("<name>", rs.name)     # the rig FILENAME is its identity
            if user != "vruser":
                svc_text = svc_text.replace("User=vruser", f"User={user}")
                steps.append(f"displayd.service User set to {user}")
            with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as f:
                f.write(svc_text)
                svc_tmp = f.name
            try:
                scp(svc_tmp, f"{ssh_prefix}:/tmp/displayd.service")
            finally:
                os.unlink(svc_tmp)
            ssh(ssh_prefix,
                "sudo mv /tmp/displayd.service /etc/systemd/system/displayd.service && "
                "sudo systemctl daemon-reload && sudo systemctl enable displayd",
                timeout=20)
            steps.append("Installed + enabled displayd.service (starts on the reboot below)")
            if not (ROOT / "dlp" / "sample_config" / "config_kms.txt").exists():
                steps.append("WARN: dlp/sample_config/config_kms.txt missing locally — the "
                             "follower will NOT get a KMS boot config")

        # 6. vrfarm.service (pi_api)
        scp(str(ROOT / "pi_api" / "vrfarm.service"), f"{ssh_prefix}:/tmp/vrfarm.service")
        ssh(ssh_prefix,
            "sudo cp /tmp/vrfarm.service /etc/systemd/system/ && "
            "sudo systemctl daemon-reload && sudo systemctl enable vrfarm && "
            "sudo systemctl kill vrfarm 2>/dev/null; sudo systemctl restart vrfarm",
            timeout=20)
        steps.append("Installed systemd service")

        # 6b. shepherd (leader only): its config.yaml is seeded with cp -n so Pi-side edits
        #     survive a re-Install; the Monitor toggle decides up or down.
        if role == "leader":
            shepherd_on = data.get("shepherd_enabled", True)
            scp(str(ROOT / "shepherd" / "shepherd.service"), f"{ssh_prefix}:/tmp/shepherd.service")
            scp(str(ROOT / "shepherd" / "config_template.yaml"), f"{ssh_prefix}:/tmp/shepherd.config.yaml")
            svc_cmd = ("sudo systemctl enable shepherd && sudo systemctl restart shepherd"
                       if shepherd_on else
                       "sudo systemctl disable shepherd 2>/dev/null; sudo systemctl stop shepherd 2>/dev/null || true")
            ssh(ssh_prefix,
                "mkdir -p ~/rig/shepherd; "
                "cp -n /tmp/shepherd.config.yaml ~/rig/shepherd/config.yaml 2>/dev/null || true; "
                "sudo cp /tmp/shepherd.service /etc/systemd/system/ && "
                "sudo systemctl daemon-reload && "
                f"{svc_cmd}",
                timeout=20)
            steps.append("Installed shepherd health monitor (running)" if shepherd_on
                         else "Installed shepherd health monitor (stopped+disabled — Monitor OFF)")
        rs.logger.info(f"INSTALL {ip} role={role} ok")
        return jsonify({"ok": True, "steps": steps})
    except Exception as e:
        rs.logger.error(f"INSTALL {ip} failed: {e}")
        return jsonify({"ok": False, "error": str(e), "steps": steps})


@bp.route("/reboot_pi", methods=["POST"])
def api_reboot_pi():
    rs: RigState = g.rs
    data = request.json or {}
    ip = data["ip"]
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "rig is running a session"}), 409
    try:
        ssh(f"{data.get('user') or _pi_user(rs, ip)}@{ip}", "sudo reboot", timeout=5)
    except Exception:
        pass  # SSH drops when the Pi reboots
    return jsonify({"ok": True})


@bp.route("/shutdown_pi", methods=["POST"])
def api_shutdown_pi():
    rs: RigState = g.rs
    data = request.json or {}
    ip = data["ip"]
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "rig is running a session"}), 409
    try:
        ssh(f"{data.get('user') or _pi_user(rs, ip)}@{ip}", "sudo shutdown -h now", timeout=5)
    except Exception:
        pass
    return jsonify({"ok": True})


def _pi_entry(rs: RigState, ip: str, role: str | None = None) -> dict:
    """{name, ip, role} of this rig's Pi at ip, the shape controller/pi_restart.py takes."""
    pi = next((p for p in rs.pis if p.get("ip") == ip), {})
    return {"name": pi.get("name", ip), "ip": ip, "role": role or pi.get("role", "follower")}


@bp.route("/restart_pi", methods=["POST"])
def api_restart_pi():
    """Restart pi_api on one Pi (reloads deployed code) and wait for it to respawn. The restart
    is timed, and on a leader shepherd's grace period follows it (controller/pi_restart.py)."""
    rs: RigState = g.rs
    data = request.json or {}
    ip = data.get("ip")
    if not ip:
        return jsonify({"ok": False, "error": "no ip"}), 400
    port = data.get("api_port", rs.api_port)
    from controller import pi_restart
    pi = _pi_entry(rs, ip)
    results, steps = pi_restart.restart_timed([pi], port)
    res = results[pi["name"]]
    return jsonify({"ok": res["ok"], "steps": steps, **({"error": res["error"]} if not res["ok"] else {})})


@bp.route("/deploy_pi", methods=["POST"])
def api_deploy_pi():
    """Push the deploy manifest to one Pi over its REST API, then restart pi_api and displayd."""
    rs: RigState = g.rs
    data = request.json or {}
    ip = data["ip"]
    role = data.get("role", "follower")
    port = data.get("api_port", rs.api_port)
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "rig is running a session"}), 409
    steps = []
    from shared.deploy_manifest import deploy_files
    try:
        for local, remote in deploy_files(role):
            local_path = ROOT / local
            if not local_path.exists():
                continue
            with open(local_path, "rb") as f:
                requests.post(f"http://{ip}:{port}/api/upload", files={"file": f},
                              data={"path": remote}, timeout=10)
            steps.append(f"Uploaded {remote}")
        # Ship the rig yaml too, so pi_api's reward calibration finds rigs/<rig>.yaml.
        with open(rs.path, "rb") as f:
            requests.post(f"http://{ip}:{port}/api/upload", files={"file": f},
                          data={"path": f"rigs/{rs.path.name}"}, timeout=10)
        steps.append(f"Uploaded rigs/{rs.path.name}")

        if data.get("restart", True):
            # Timed: on a leader shepherd is restarted first (so the shepherd.py just uploaded
            # runs) and its grace period follows the measured outage (controller/pi_restart.py).
            from controller import pi_restart
            pi = _pi_entry(rs, ip, role)
            results, restart_steps = pi_restart.restart_timed([pi], port, reload_shepherd=True)
            steps.extend(restart_steps)
            steps.append("pi_api back online — re-initialize devices" if results[pi["name"]]["ok"] else
                         "WARN pi_api did not respond after restart — re-check the Pi")
            try:
                r = requests.post(f"http://{ip}:{port}/api/restart_displayd", json={}, timeout=120)
                d = r.json() if r.ok else {}
                if not d.get("skipped"):
                    steps.append(f"displayd restarted ({d.get('state', '?')})" if d.get("ok")
                                 else f"WARN displayd restart failed: {d.get('error', r.status_code)}")
            except Exception as e:
                steps.append(f"WARN displayd restart error: {e}")
        rs.logger.info(f"DEPLOY_PI {ip} ok")
        return jsonify({"ok": True, "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


# ── devices ──

@bp.route("/calibrate", methods=["POST"])
def api_calibrate():
    rs: RigState = g.rs
    data = request.json or {}
    ip = data["ip"]
    port = data.get("api_port", rs.api_port)
    device = data["device"]
    params = data.get("params", {})
    try:
        r = requests.post(f"http://{ip}:{port}/api/calibrate",
                          json={"device": device, "params": params, "rig": rs.name}, timeout=60)
        result = r.json()
        if result.get("ok") and data.get("save_to_rig"):
            rs.config.setdefault("devices", {}).setdefault(device, {})["calibration"] = result.get("results", {})
            save_rig_atomic(rs.config, rs.path)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/proxy", methods=["POST"])
def api_proxy():
    """Forward a request to one of THIS rig's Pis."""
    rs: RigState = g.rs
    data = request.json or {}
    ip = data.get("ip")
    if not ip or not _own_pi(rs, ip):
        return jsonify({"ok": False, "error": f"{ip} is not a Pi of rig {rs.name}"}), 403
    port = data.get("port", rs.api_port)
    endpoint = data["endpoint"]
    method = data.get("method", "POST").upper()
    payload = data.get("payload", {})
    timeout = data.get("timeout", 30)
    try:
        if method == "GET":
            r = requests.get(f"http://{ip}:{port}{endpoint}", timeout=timeout)
        else:
            r = requests.post(f"http://{ip}:{port}{endpoint}", json=payload, timeout=timeout)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/camera_feed")
def camera_feed():
    rs: RigState = g.rs
    ip = next((pi["ip"] for pi in rs.pis if "camera" in pi.get("devices", [])), None)
    if not ip:
        return "No camera", 400
    return Response(relay(f"http://{ip}:{rs.api_port}/api/camera_stream"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


# Device init order (display first so the screen is up before the rest).
_INIT_ORDER = ["display", "lick_sensor", "reward", "camera", "photodiode", "encoder", "calibration_probe"]


def _display_init_cfg(devices: dict) -> dict:
    """Display init payload: the display block plus the photodiode card's sync-square prefs."""
    pd = devices.get("photodiode", {}) or {}
    cfg = dict(devices.get("display", {}) or {})
    for k in ("sync_corner", "sync_size_px", "sync_brightness"):
        if k in pd:
            cfg[k] = pd[k]
    return cfg


def _init_one_device(rs: RigState, dev_name, devices, post):
    cfg = devices.get(dev_name, {})
    if dev_name == "display":
        ok1, _ = post("/api/init_projector", {}, "Projector", 35)
        ok2, msg = post("/api/init_display", {"rig_config": _display_init_cfg(devices)}, "Display init", 30)
        return (ok1 and ok2), msg
    if dev_name == "camera":
        post("/api/camera_preview_stop", {}, "Camera release", 15)
        return post("/api/init_camera", {}, "Camera", 15)
    if dev_name == "lick_sensor":
        return post("/api/init_lick",
                    {"i2c_address": cfg.get("i2c_address", "0x5A"), "electrode": cfg.get("electrode", 4)},
                    "Lick sensor", 10)
    if dev_name == "reward":
        return post("/api/init_reward", {"pins": cfg.get("pins", {"main": {"gpio": 18}})}, "Reward", 10)
    if dev_name == "photodiode":
        payload = photodiode_init_payload(cfg)
        fol = next((p for p in rs.pis if p.get("role") == "follower"), None)
        if fol:
            payload["follower_ip"] = fol["ip"]
            payload["follower_api_port"] = rs.api_port
        return post("/api/init_photodiode", payload, "Photodiode", 25)
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


@bp.route("/init_devices", methods=["POST"])
def api_init_devices():
    rs: RigState = g.rs
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "rig is running a session"}), 409
    api_port = rs.api_port
    steps, results = [], {}
    dev_to_ip = {d: pi["ip"] for pi in rs.pis for d in pi.get("devices", [])}
    devices = rs.config.get("devices", {})
    for dev_name in _INIT_ORDER:
        if dev_name in devices and devices[dev_name].get("enabled"):
            ip = dev_to_ip.get(dev_name)
            if ip:
                ok, msg = _init_one_device(rs, dev_name, devices, _mk_post(ip, api_port, steps))
                results[dev_name] = {"ok": ok, "message": msg}
    all_ok = bool(results) and all(r["ok"] for r in results.values())
    return jsonify({"ok": all_ok, "steps": steps, "results": results})


@bp.route("/reinit_device", methods=["POST"])
def api_reinit_device():
    rs: RigState = g.rs
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "rig is running a session"}), 409
    dev_name = (request.json or {}).get("device")
    devices = rs.config.get("devices", {})
    if dev_name not in devices or not devices[dev_name].get("enabled"):
        return jsonify({"ok": False, "error": f"{dev_name} is not enabled"}), 400
    ip = next((pi["ip"] for pi in rs.pis if dev_name in pi.get("devices", [])), None)
    if not ip:
        return jsonify({"ok": False, "error": f"{dev_name} is not assigned to a Pi"}), 400
    steps = []
    ok, _msg = _init_one_device(rs, dev_name, devices, _mk_post(ip, rs.api_port, steps))
    return jsonify({"ok": ok, "device": dev_name, "steps": steps})


# ── warp map / intensity calibration ──
#
# Intensity calibration files: display_calibration/<rig>/intensity/intensity_cal_*.yaml; the rig
# YAML's devices.display.intensity_calibration names the one baked into the warp (default: the
# theoretical mock of the current geometry). See display_calibration/intensity_cal.py.

def _cal_mod():
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    import intensity_cal
    return intensity_cal


def _ensure_mock_cals(rs: RigState, geo_file: Path) -> str:
    """Write the theoretical mock for this geometry and the none mock; return the theoretical name."""
    ic = _cal_mod()
    geo = yaml.safe_load(geo_file.read_text())
    az, gain = _cwm().theoretical_luminance_curve(geo)
    for n in ic.upgrade_mocks(lum_dir(rs)):                 # mocks written in the earlier format
        rs.log(f"intensity: rewrote mock {n} in the current format")
    ic.write_none(lum_dir(rs))
    return ic.write_theoretical(lum_dir(rs), az, gain, geo_file.name).name


def _active_intensity_cal(rs: RigState, geo_file: Path) -> str:
    """The rig's chosen intensity calibration if it exists, else the theoretical mock."""
    theo = _ensure_mock_cals(rs, geo_file)
    want = ((rs.config or {}).get("devices", {}).get("display", {}) or {}).get("intensity_calibration")
    if want and (lum_dir(rs) / Path(want).name).exists():
        return Path(want).name
    return theo


def _generate_and_deploy_warp(rs: RigState, geo_file: Path, cal_name: str | None = None):
    """Build warp_map.npz on the controller from `geo_file` with the light model of `cal_name`
    (default: the rig's active intensity calibration), then ATOMICALLY copy it + the geometry to
    every Pi and reload the live display. Returns (ok, steps, error)."""
    steps = []
    rdir = calib_paths.rig_dir(rs.name)
    cal_name = Path(cal_name).name if cal_name else _active_intensity_cal(rs, geo_file)
    cal_path = lum_dir(rs) / cal_name
    if not cal_path.exists():
        return False, steps, f"intensity calibration {cal_name} not found"
    argv = [sys.executable, str(TOOLS_DIR / "compute_warp_map.py"), "--geo", str(geo_file),
            "--intensity-cal", str(cal_path), "--cal-dir", str(rdir)]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=str(rdir))
    if r.returncode != 0:
        return False, steps, r.stderr[-500:]
    steps.append(f"Generated warp map from {geo_file.name} (intensity: {cal_name})")
    npz_path = calib_paths.warp_path(rs.name)
    if not npz_path.exists():
        return False, steps, "warp_map.npz not created"
    for pi in rs.pis:
        tgt = ssh_target(pi)
        ip = pi["ip"]
        try:
            ssh(tgt, "mkdir -p ~/rig/calibration")
            scp(str(npz_path), f"{tgt}:~/rig/calibration/warp_map.npz.tmp")
            scp(str(geo_file), f"{tgt}:~/rig/calibration/rig_geometry.yaml.tmp")
            ssh(tgt,
                "mv ~/rig/calibration/warp_map.npz.tmp ~/rig/calibration/warp_map.npz && "
                "mv ~/rig/calibration/rig_geometry.yaml.tmp ~/rig/calibration/rig_geometry.yaml")
            steps.append(f"Copied warp_map.npz + rig_geometry.yaml to {pi['name']} ({ip})")
            try:
                rr = requests.post(f"http://{ip}:{rs.api_port}/api/reload_warp", timeout=5)
                if rr.ok and rr.json().get("reloaded"):
                    steps.append(f"Reloaded warp into live display on {pi['name']}")
                else:
                    steps.append(f"Warp saved on {pi['name']} — display not active, loads on next Init Display")
            except Exception as e:
                steps.append(f"(warp reload skipped on {pi['name']}: {e})")
        except Exception as e:
            steps.append(f"Failed to copy to {pi['name']}: {e}")
    return True, steps, None


@bp.route("/generate_warp", methods=["POST"])
def api_generate_warp():
    rs: RigState = g.rs
    data = request.json or {}
    geo_file = _geo_file(rs, data.get("geometry"))
    if geo_file is None or not geo_file.exists():
        return jsonify({"ok": False, "error": f"Geometry file not found: {geo_file}"}), 404
    try:
        ok, steps, err = _generate_and_deploy_warp(rs, geo_file)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": []})
    return jsonify({"ok": ok, "error": err, "steps": steps})


def _read_pm100d():
    """One power reading (W) from a USB Thorlabs PM100D. Optional dependency; raises on failure."""
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


@bp.route("/lum_read", methods=["POST"])
def api_lum_read():
    try:
        return jsonify({"ok": True, "value": _read_pm100d()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/intensity_cals")
def api_intensity_cals():
    """This rig's intensity calibrations (mocks created for the current geometry) + the active one."""
    rs: RigState = g.rs
    try:
        geo_file = _geo_file(rs, request.args.get("geometry"))
        active = _active_intensity_cal(rs, geo_file) if geo_file and geo_file.exists() else None
        return jsonify({"ok": True, "active": active, "files": _cal_mod().list_cals(lum_dir(rs))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "files": []})


@bp.route("/save_intensity_cal", methods=["POST"])
def api_save_intensity_cal():
    """{alt_deg, size_deg, unit, method, level_sweeps: [{az_deg, measurements: [{level, reading}]}]}
    -> intensity/intensity_cal_<now>.yaml + a fit report. Nothing is deployed (Apply does that).
    An `azimuth_sweep` from the earlier panel is ignored."""
    rs: RigState = g.rs
    d = request.json or {}
    try:
        path, report = _cal_mod().save_measured(
            lum_dir(rs), d.get("alt_deg", 0.0), d.get("level_sweeps") or [],
            size_deg=d.get("size_deg"), method=d.get("method"), unit=d.get("unit") or "lux")
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "name": path.name, "report": report})


@bp.route("/intensity_apply", methods=["POST"])
def api_intensity_apply():
    """{name, geometry?}: rebuild + deploy the warp with this calibration's light model and record
    it as the rig's devices.display.intensity_calibration (rig YAML saved at once — the warp on
    the Pis now depends on it)."""
    rs: RigState = g.rs
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "rig is running a session"}), 409
    d = request.json or {}
    geo_file = _geo_file(rs, d.get("geometry"))
    if geo_file is None or not geo_file.exists():
        return jsonify({"ok": False, "error": f"Geometry file not found: {geo_file}"}), 404
    name = d.get("name") or _active_intensity_cal(rs, geo_file)
    try:
        _cal_mod().load(name, lum_dir(rs))                      # readable error before building
        ok, steps, err = _generate_and_deploy_warp(rs, geo_file, name)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": []})
    if ok:
        rs.config.setdefault("devices", {}).setdefault("display", {})["intensity_calibration"] = Path(name).name
        for k in ("luminance_correction", "contrast_calibration"):       # superseded settings
            rs.config["devices"]["display"].pop(k, None)
        save_rig_atomic(rs.config, rs.path)
        registry.reload_config(rs.name)
        steps.append(f"Rig now uses {Path(name).name} (saved to {rs.path.name})")
    return jsonify({"ok": ok, "error": err, "steps": steps, "active": Path(name).name})


@bp.route("/contrast_preview", methods=["POST"])
def api_contrast_preview():
    """Setup's TESTS row: {background_gray, values, az_deg} -> controller.experiment.contrast_preview
    at that azimuth, with the light model of the warp this rig runs."""
    from controller.experiment import contrast_preview
    rs: RigState = g.rs
    d = request.json or {}
    try:
        az = float(d.get("az_deg") or 0.0)
        return jsonify({"ok": True, **contrast_preview(rs, d.get("background_gray") or 0.0,
                                                       d.get("values") or [], [az])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── geometry calibration (calib_geo on the display Pi) ──

def _displayd_active(tgt) -> bool:
    """True when the KMS display daemon owns the display on this Pi (probed, never assumed)."""
    try:
        out = ssh(tgt, "curl -s -m 2 http://127.0.0.1:5581/status || true", timeout=15)
        return '"state"' in (out or "")
    except Exception:
        return False


def _reinit_projector(tgt):
    if not _displayd_active(tgt):
        return False, ("no displayd on this Pi — since phase 4 it is the only thing that can "
                       "bring the projector up. Check: sudo systemctl status displayd")
    try:
        out = ssh(tgt, "curl -s -m 90 -X POST http://127.0.0.1:5581/bringup", timeout=110)
        try:
            st = json.loads(out).get("state", "?")
        except Exception:
            st = (out.strip().splitlines() or ["?"])[-1][:80]
        return st in ("RENDERER_UP", "OPTICS_OK"), f"displayd bringup -> {st}"
    except Exception as e:
        return False, str(e)


def _drive_calibration_probe(rs: RigState, high: bool):
    probe_pi = next((pi for pi in rs.pis if "calibration_probe" in pi.get("devices", [])), None)
    if not probe_pi:
        return None
    endpoint = "/api/probe_on" if high else "/api/probe_off"
    edge = "HIGH" if high else "LOW"
    try:
        r = requests.post(f"http://{probe_pi['ip']}:{rs.api_port}{endpoint}", timeout=5)
        if r.json().get("ok"):
            return f"Calibration probe {edge}"
        return f"WARN calibration probe {edge} skipped: {r.json().get('error', '')} (run Init Devices)"
    except Exception as e:
        return f"WARN calibration probe {edge} failed: {e}"


class _FlowList(list):
    """List subclass that yaml.dump renders inline: [a, b, c]."""


def _flow_list_repr(dumper, data):
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


yaml.add_representer(_FlowList, _flow_list_repr)


@bp.route("/start_calibration", methods=["POST"])
def api_start_calibration():
    """Deploy the geometry-calibration tools to the display Pi and launch calib_geo (sliders
    on :5091). The tool reports saved geometry back to THIS app at this rig's address."""
    rs: RigState = g.rs
    target_pi = _display_pi(rs)
    if not target_pi:
        return jsonify({"ok": False, "error": "No Pi has the display device"}), 400
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "rig is running a session"}), 409
    tgt = ssh_target(target_pi)
    ip = target_pi["ip"]
    geo_data = (request.json or {}).get("geometry_data")
    geo_file = _geo_file(rs, (request.json or {}).get("geometry"))
    tools = ["calib_geo.py", "cal_start.sh", "cal_stop.sh", "panel_grid.py", "validate_calibration_pygame.py"]
    steps = []
    try:
        ssh(tgt, "mkdir -p ~/rig/calibration")
        srcs = [str(TOOLS_DIR / t) for t in tools if (TOOLS_DIR / t).exists()]
        if srcs:
            r = subprocess.run(["scp", "-o", "ConnectTimeout=5", *srcs, f"{tgt}:~/rig/calibration/"],
                               capture_output=True, text=True, timeout=45)
            if r.returncode != 0:
                raise RuntimeError(f"scp tools failed: {r.stderr.strip()}")
        steps.append(f"Deployed calibration tools to {target_pi['name']} ({ip})")
        if geo_data:
            try:
                gd = dict(geo_data)
                if isinstance(gd.get("projector"), dict) and "resolution" in gd["projector"]:
                    gd["projector"]["resolution"] = _FlowList(gd["projector"]["resolution"])
                fd, tmp = tempfile.mkstemp(suffix=".yaml")
                os.close(fd)
                with open(tmp, "w") as f:
                    yaml.dump(gd, f, default_flow_style=False, sort_keys=True)
                scp(tmp, f"{tgt}:~/rig/calibration/rig_geometry.yaml")
                os.unlink(tmp)
                steps.append("Initialized projector geometry from the Display card")
            except Exception as e:
                steps.append(f"WARN: geometry init failed ({e}); using the Pi's existing file")
        else:
            chk = subprocess.run(["ssh", "-o", "ConnectTimeout=5", tgt, "test -e ~/rig/calibration/rig_geometry.yaml"],
                                 capture_output=True, text=True, timeout=15)
            if chk.returncode != 0 and geo_file is not None and geo_file.exists():
                scp(str(geo_file), f"{tgt}:~/rig/calibration/rig_geometry.yaml")
                steps.append(f"Seeded rig_geometry.yaml from {geo_file.name}")
        if _displayd_active(tgt):
            steps.append("KMS: cal_start.sh will put displayd in STANDBY (no X re-init)")
        else:
            ok, msg = _reinit_projector(tgt)
            steps.append(f"Projector re-init: {msg}" if ok else f"WARN projector re-init: {msg}")
        cfg = settings.load()
        mac_url = (f"http://{cfg.get('controller_ip', '192.168.10.1')}:{cfg.get('ui_port', 5000)}"
                   f"/api/rigs/{rs.name}/setup/receive_geometry")
        out = ssh(tgt, f"CAL_MAC_URL='{mac_url}' bash ~/rig/calibration/cal_start.sh geo", timeout=45)
        steps.append((out.strip().splitlines() or ["started"])[0])
        note = _drive_calibration_probe(rs, True)
        if note:
            steps.append(note)
        return jsonify({"ok": True, "url": f"http://{ip}:5091", "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


@bp.route("/stop_calibration", methods=["POST"])
def api_stop_calibration():
    rs: RigState = g.rs
    target_pi = _display_pi(rs)
    if not target_pi:
        return jsonify({"ok": False, "error": "No Pi has the display device"}), 400
    tgt = ssh_target(target_pi)
    steps = []
    try:
        out = ssh(tgt, "bash ~/rig/calibration/cal_stop.sh", timeout=20)
        steps.append((out.strip().splitlines() or ["Calibration tool stopped"])[0])
        note = _drive_calibration_probe(rs, False)
        if note:
            steps.append(note)
        if _displayd_active(tgt):
            steps.append("KMS: cal_stop.sh returned the display to displayd (/resume)")
        else:
            ok, msg = _reinit_projector(tgt)
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
        spec = importlib.util.spec_from_file_location("compute_warp_map", str(TOOLS_DIR / "compute_warp_map.py"))
        _cwm_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_cwm_mod)
    return _cwm_mod


@bp.route("/derived_geometry", methods=["POST"])
def api_derived_geometry():
    geo = (request.json or {}).get("geometry_data")
    if not geo:
        return jsonify({"ok": False, "error": "no geometry_data"}), 400
    try:
        return jsonify({"ok": True, **_cwm().derived_angle_ranges(geo)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/check_warp")
def api_check_warp():
    rs: RigState = g.rs
    try:
        leader = rs.leader()
    except Exception:
        return jsonify({"exists": False})
    try:
        out = ssh(ssh_target(leader), "test -f ~/rig/calibration/warp_map.npz && echo yes || echo no", timeout=8)
        return jsonify({"exists": "yes" in out})
    except Exception:
        return jsonify({"exists": False})


# ── geometry files ──

@bp.route("/list_geometries")
def api_list_geometries():
    rs: RigState = g.rs
    return jsonify(calib_paths.geometry_files(rs.name))


@bp.route("/load_geometry", methods=["POST"])
def api_load_geometry():
    rs: RigState = g.rs
    path = _geo_file(rs, (request.json or {}).get("name"))
    if path is None or not path.exists():
        return jsonify({"error": f"Not found: {path.name if path else '(no geometry files)'}"}), 404
    with open(path) as f:
        geo = yaml.safe_load(f)
    return jsonify({"name": path.name, "geometry": geo})


@bp.route("/save_geometry", methods=["POST"])
def api_save_geometry():
    """Save edited geometry. Dated geometry files are records of a calibration, so an edit never
    rewrites one: unchanged values -> nothing written; changed values -> a NEW
    rig_geometry_<now>.yaml, whose name is returned so the UI selects it (and Save Rig makes it
    the rig's display.geometry_file)."""
    rs: RigState = g.rs
    data = request.json or {}
    geo = data.get("geometry")
    if not geo:
        return jsonify({"ok": False, "error": "No geometry data"}), 400
    base = _geo_file(rs, data.get("name"))
    if base is not None and base.exists():
        try:
            if json.dumps(yaml.safe_load(base.read_text()), sort_keys=True) == json.dumps(geo, sort_keys=True):
                return jsonify({"ok": True, "name": base.name, "unchanged": True})
        except Exception:
            pass
    if "projector" in geo and "resolution" in geo["projector"]:
        geo["projector"]["resolution"] = _FlowList(geo["projector"]["resolution"])
    name = calib_paths.new_geometry_name(rs.name)
    with open(geo_dir(rs) / name, "w") as f:
        if base is not None:
            f.write(f"# edited in the Setup tab from {base.name}\n")
        yaml.dump(geo, f, default_flow_style=False, sort_keys=True)
    return jsonify({"ok": True, "name": name, "created": True, "from": base.name if base else None})


@bp.route("/receive_geometry", methods=["POST"])
def api_receive_geometry():
    """Called by calib_geo on the projector Pi when the user hits Save: archive + canonical."""
    from datetime import datetime
    rs: RigState = g.rs
    text = (request.json or {}).get("yaml")
    if not text:
        return jsonify({"ok": False, "error": "No yaml"}), 400
    try:
        yaml.safe_load(text)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid YAML: {e}"}), 400
    archive = calib_paths.new_geometry_name(rs.name)
    (geo_dir(rs) / archive).write_text(text)
    print(f"[receive_geometry:{rs.name}] saved {archive}")
    return jsonify({"ok": True, "archived": archive})


@bp.route("/send_geometry", methods=["POST"])
def api_send_geometry():
    rs: RigState = g.rs
    name = Path((request.json or {}).get("name", "")).name
    src = geo_dir(rs) / name if name else None
    if not name or not src.exists():
        return jsonify({"ok": False, "error": f"Not found: {name}"}), 400
    target_pi = _display_pi(rs)
    if not target_pi:
        return jsonify({"ok": False, "error": "No Pi has the display device"}), 400
    tgt = ssh_target(target_pi)
    try:
        ssh(tgt, "mkdir -p ~/rig/calibration")
        scp(str(src), f"{tgt}:~/rig/calibration/rig_geometry.yaml")
        return jsonify({"ok": True, "sent": name, "to": target_pi["name"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Teensy firmware (photodiode sync pulse detector) ──

TEENSY_FQBN = "teensy:avr:teensy40"
TEENSY_MCU = "TEENSY40"
ARDUINO_CLI_URL = "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Linux_ARM64.tar.gz"
TEENSY_INDEX_URL = "https://www.pjrc.com/teensy/package_teensy_index.json"
TEENSY_RULES = ROOT / "teensy" / "00-teensy.rules"
TEENSY_HID_REBOOT = ROOT / "teensy" / "teensy_hid_reboot.py"
TEENSY_STATUS_CMD = ("test -x ~/bin/arduino-cli && echo CLI; "
                     "test -d ~/.arduino15/packages/teensy/hardware/avr && echo CORE; "
                     "command -v teensy_loader_cli >/dev/null && echo LOADER; "
                     "test -f /etc/udev/rules.d/00-teensy.rules && echo RULES; "
                     "echo USB:$(lsusb -d 16c0: 2>/dev/null | grep -o 'ID 16c0:[0-9a-f]*' | cut -d: -f2 | tr '\\n' ,)")


def _teensy_status(tgt) -> dict:
    """Toolchain parts present on the Pi + the Teensy's USB product ids (0483 = Serial, 0486 =
    RawHID as a fresh board enumerates, 0478 = HalfKay bootloader; empty = no Teensy seen)."""
    out = ssh_merged(tgt, TEENSY_STATUS_CMD, timeout=20)
    toks = out.split()
    usb = next((t[4:] for t in toks if t.startswith("USB:")), "")
    st = {k.lower(): (k in toks) for k in ("CLI", "CORE", "LOADER", "RULES")}
    st["usb_pids"] = [p for p in usb.split(",") if p]
    st["serial_mode"] = "0483" in st["usb_pids"]
    st["toolchain_ok"] = all(st[k] for k in ("cli", "core", "loader", "rules"))
    return st


def _install_teensy_toolchain(tgt, steps, status=None):
    """Idempotent: whatever is missing of teensy-loader-cli (apt), arduino-cli (~/bin, ARM64 build),
    the PJRC teensy:avr core (~/.arduino15, via PJRC's board index) and the PJRC udev rules. The
    Pi downloads these itself (institute WiFi). Install runs this for the Pi that owns the
    photodiode; Upload runs it too, so a fresh leader flashes without a re-Install."""
    st = status or _teensy_status(tgt)
    if st["toolchain_ok"]:
        steps.append("Teensy toolchain present (arduino-cli + teensy:avr core, teensy_loader_cli, udev rules)")
        return st
    if not st["loader"]:
        ssh(tgt, "sudo apt-get update -qq && sudo apt-get install -y teensy-loader-cli", timeout=300)
        steps.append("Installed teensy-loader-cli (apt)")
    if not st["cli"]:
        ssh(tgt, f"mkdir -p ~/bin && curl -fsSL {ARDUINO_CLI_URL} | tar -xz -C ~/bin arduino-cli && "
                 "chmod +x ~/bin/arduino-cli", timeout=300)
        ver = ssh_merged(tgt, "~/bin/arduino-cli version", timeout=20).strip().splitlines()
        steps.append("Installed ~/bin/" + (ver[-1] if ver else "arduino-cli"))
    if not st["core"]:
        ssh(tgt, f"test -f ~/.arduino15/arduino-cli.yaml || ~/bin/arduino-cli config init --additional-urls {TEENSY_INDEX_URL}; "
                 f"~/bin/arduino-cli core update-index --additional-urls {TEENSY_INDEX_URL} && "
                 f"~/bin/arduino-cli core install teensy:avr --additional-urls {TEENSY_INDEX_URL}", timeout=1800)
        core = ssh_merged(tgt, "~/bin/arduino-cli core list 2>/dev/null | grep teensy:avr", timeout=30).split()
        steps.append(f"Installed Teensy core teensy:avr {core[1] if len(core) > 1 else ''} (PJRC board index)")
    if not st["rules"]:
        scp(str(TEENSY_RULES), f"{tgt}:/tmp/00-teensy.rules")
        ssh(tgt, "sudo cp /tmp/00-teensy.rules /etc/udev/rules.d/00-teensy.rules && "
                 "sudo udevadm control --reload-rules && sudo udevadm trigger", timeout=30)
        steps.append("Installed PJRC udev rules (Teensy USB nodes world-writable)")
    return _teensy_status(tgt)


def _default_sketch(rig: str) -> Path:
    """The sketch Upload flashes when no file is chosen: the newest (by name) sketch folder under
    teensy/<rig>/ if that rig has its own copies (pins and thresholds differ per rig), else the
    newest shared teensy/<sketch>/<sketch>.ino."""
    for base in (ROOT / "teensy" / rig, ROOT / "teensy"):
        versions = sorted(p for p in base.iterdir() if base.is_dir() and p.is_dir()
                          and (p / (p.name + ".ino")).exists()) if base.is_dir() else []
        if versions:
            return versions[-1] / (versions[-1].name + ".ino")
    raise RuntimeError("no sketches under teensy/")


def _photodiode_pi(rs: RigState):
    return next((pi for pi in rs.pis if "photodiode" in pi.get("devices", [])), None)


@bp.route("/teensy_status")
def api_teensy_status():
    """Toolchain + Teensy USB mode on the Pi that owns the photodiode (what Upload will meet)."""
    rs: RigState = g.rs
    pd_pi = _photodiode_pi(rs)
    if not pd_pi:
        return jsonify({"ok": False, "error": "No Pi has the photodiode device"}), 400
    try:
        return jsonify({"ok": True, "pi": pd_pi["name"], **_teensy_status(ssh_target(pd_pi))})
    except Exception as e:
        return jsonify({"ok": False, "pi": pd_pi["name"], "error": str(e)})


@bp.route("/teensy_upload", methods=["POST"])
def api_teensy_upload():
    """Compile the photodiode Teensy sketch and flash it on the Pi that owns the photodiode."""
    rs: RigState = g.rs
    pd_pi = _photodiode_pi(rs)
    if not pd_pi:
        return jsonify({"ok": False, "error": "No Pi has the photodiode device"}), 400
    import re as _re
    data = request.json or {}
    debug = bool(data.get("debug"))
    ino_text = data.get("ino_text")
    ino_name = data.get("ino_name") or ""
    steps = []
    try:
        if not ino_text:
            src = _default_sketch(rs.name)
            ino_text = src.read_text()
            ino_name = src.name
            steps.append(f"No file chosen — using {src.relative_to(ROOT)}")
        sketch = ino_name[:-4] if ino_name.endswith(".ino") else ino_name
        if not sketch:
            raise RuntimeError("cannot derive sketch name from the filename")
        ino_text, n = _re.subn(r"#define\s+DEBUG\s+\d+", f"#define DEBUG {1 if debug else 0}", ino_text, count=1)
        steps.append(f"DEBUG set to {1 if debug else 0}" if n else
                     "WARNING: no '#define DEBUG' line found — uploading as-is")
        tgt = ssh_target(pd_pi)
        st = _install_teensy_toolchain(tgt, steps)            # no-op when everything is there
        if not st["usb_pids"]:
            raise RuntimeError(f"no Teensy on {pd_pi['name']}'s USB (lsusb shows no 16c0 device)")
        rdir = f"~/teensy_build/{sketch}"
        ssh(tgt, f"mkdir -p {rdir}")
        with tempfile.NamedTemporaryFile("w", suffix=".ino", delete=False) as f:
            f.write(ino_text)
            tmp = f.name
        try:
            scp(tmp, f"{tgt}:{rdir}/{sketch}.ino")
        finally:
            os.unlink(tmp)
        steps.append(f"Sketch on {pd_pi['name']}: {rdir}/{sketch}.ino")
        out = ssh_merged(tgt, f"~/bin/arduino-cli compile --fqbn {TEENSY_FQBN} --output-dir {rdir}/out {rdir}",
                         timeout=240)
        mem = [l.strip() for l in out.splitlines() if "FLASH:" in l or "RAM1:" in l]
        steps += ["  " + l for l in mem[:2]] or ["Compiled."]
        if not st["serial_mode"]:
            # A board that is not in Serial USB mode (a fresh one is RawHID) cannot be soft-rebooted
            # by the loader; the Teensyduino HID reboot request does it. Button as the last resort.
            scp(str(TEENSY_HID_REBOOT), f"{tgt}:~/teensy_build/teensy_hid_reboot.py")
            out = ssh_merged(tgt, "python3 ~/teensy_build/teensy_hid_reboot.py", timeout=30)
            line = (out.strip().splitlines() or ["?"])[-1]
            steps.append(f"Teensy not in Serial mode (16c0:{','.join(st['usb_pids'])}): {line}"
                         + ("" if "HalfKay" in out else " — PRESS THE TEENSY BUTTON (the loader waits ~2 min)"))
        out = ssh_merged(tgt, f"teensy_loader_cli --mcu={TEENSY_MCU} -s -w -v {rdir}/out/{sketch}.ino.hex",
                         timeout=150)
        steps.append((out.strip().splitlines() or ["Flashed."])[-1])
        if "Booting" not in out and "Programming" not in out:
            raise RuntimeError("teensy_loader_cli did not program the board: " + out.strip()[-300:])
        return jsonify({"ok": True, "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})
