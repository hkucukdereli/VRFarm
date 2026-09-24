"""
controller/setup.py

The Setup tab's backend, under /api/rigs/<rig>/setup/..., reading its rig from the registry.
Rig creation and Pi identity live in the Network tab (controller/network.py); this keeps the
per-Pi lifecycle (Check / Install / Deploy / Restart / Reboot / Shutdown), the rig YAML read and
save, generic device init through pi_api's /api/init_device, the proxy the device cards talk
through, and the per-device video preview relay. Nothing here knows any concrete device: this
branch ships none, and a project's devices/*.py self-register on the Pi.
"""
from __future__ import annotations
import os
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml
from flask import Blueprint, Response, g, jsonify, request

from shared.config import save_rig_atomic
from shared.mjpeg_relay import relay
from controller import settings
from controller.registry import registry, RigState
from controller.ssh import ssh, scp, ssh_merged, ssh_ok, target as ssh_target

ROOT = settings.ROOT

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


def _pi_user(rs: RigState, ip: str, fallback: str | None = None) -> str:
    for pi in rs.pis:
        if pi.get("ip") == ip:
            return pi.get("user") or "vruser"
    return fallback or "vruser"


def _own_pi(rs: RigState, ip: str) -> bool:
    return ip in rs.pi_ips()


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


# Per device TYPE, what Install puts on the Pi. This branch ships no concrete devices, so the
# maps are empty; a project adds entries beside its devices/*.py. The key is the device's `type`
# in the rig YAML, which defaults to the device's name.
DEVICE_PACKAGES: dict = {}        # type -> pip packages, into the rig conda env
DEVICE_APT_PACKAGES: dict = {}    # type -> apt packages
I2C_DEVICE_TYPES: set = set()     # types that need the Pi's I2C bus enabled


@bp.route("/install_pi", methods=["POST"])
def api_install_pi():
    """First-time Pi setup via SSH: time zone, conda env, system and Python packages per device
    type, project files, vrfarm.service, and shepherd on a leader."""
    rs: RigState = g.rs
    data = request.json or {}
    ip = data["ip"]
    user = data.get("user") or _pi_user(rs, ip)
    role = data.get("role", "leader")
    devices = data.get("devices", [])
    dev_cfgs = rs.config.get("devices") or {}
    dev_types = {d: (dev_cfgs.get(d) or {}).get("type", d) for d in devices}

    steps = []
    ssh_prefix = f"{user}@{ip}"
    conda_activate = ("source ~/miniforge3/etc/profile.d/conda.sh && "
                      "conda activate rig && ")
    needs_i2c = any(t in I2C_DEVICE_TYPES for t in dev_types.values())
    # camera-like devices (rig config `video: true`) capture through ffmpeg / v4l2
    needs_video = any((dev_cfgs.get(d) or {}).get("video") for d in devices)

    try:
        # 1. Directories (~/video is where video devices record by default), and the
        #    hardware-access groups the Pi user needs (idempotent; usually already set).
        ssh(ssh_prefix, "mkdir -p ~/rig ~/data ~/video")
        steps.append("Created ~/rig ~/data ~/video")
        ssh(ssh_prefix, f"sudo usermod -aG dialout,i2c,video,gpio {user} || true", timeout=15)

        # 1a. One time zone on every Pi, so their logs line up (recorded data is Unix time).
        ssh(ssh_prefix, "sudo timedatectl set-timezone Europe/Vienna", timeout=20)
        steps.append("Time zone set to Europe/Vienna")

        # 1b. conda 'rig' env pinned to the SYSTEM python version, so apt-built bindings a
        #     device may need can be symlinked in without an ABI mismatch.
        ssh(ssh_prefix,
            "source ~/miniforge3/etc/profile.d/conda.sh && "
            "SYSPY=$(python3 -c 'import sys; print(str(sys.version_info.major)+\".\"+str(sys.version_info.minor))') && "
            "conda env list | awk '{print $1}' | grep -qx rig || "
            "conda create -n rig python=$SYSPY -y",
            timeout=400)
        steps.append("Ensured conda 'rig' env (matched to system python)")

        # 2. System packages. rsync on every Pi: the Data tab syncs sessions off the leaders with
        #    rsync over SSH. Then whatever this Pi's device types declare.
        apt_packages = ["rsync"]
        if needs_video:
            # ffmpeg captures and does the consolidation remux; v4l-utils probes formats
            apt_packages += ["ffmpeg", "v4l-utils"]
        for t in dev_types.values():
            apt_packages += [p for p in DEVICE_APT_PACKAGES.get(t, []) if p not in apt_packages]
        apt_str = " ".join(apt_packages)
        ssh(ssh_prefix, f"sudo apt-get update -qq && sudo apt-get install -y {apt_str}", timeout=300)
        steps.append(f"Installed system packages: {apt_str}")

        # 2b. I2C when a device type needs the bus. Idempotent.
        if needs_i2c:
            ssh(ssh_prefix, "sudo raspi-config nonint do_i2c 0", timeout=20)
            steps.append("Enabled I2C")

        # 3. Python packages (pip): the base set plus per-device-type extras.
        packages = {"flask", "pyyaml", "numpy"}
        for t in dev_types.values():
            packages.update(DEVICE_PACKAGES.get(t, []))
        if role == "leader":
            packages.add("h5py")
        pkg_str = " ".join(sorted(packages))
        ssh(ssh_prefix, f"{conda_activate} pip install {pkg_str}", timeout=120)
        steps.append(f"Installed Python packages: {pkg_str}")

        # 4. Project files (the deploy manifest). Make the ~/rig subdirs first: scp does not.
        from shared.deploy_manifest import deploy_files
        files_to_deploy = deploy_files(role)
        remote_dirs = sorted({os.path.dirname(remote) for _, remote in files_to_deploy
                              if os.path.dirname(remote)})
        if remote_dirs:
            ssh(ssh_prefix, "mkdir -p " + " ".join(f"~/rig/{d}" for d in remote_dirs))
        for local, remote in files_to_deploy:
            scp(str(ROOT / local), f"{ssh_prefix}:~/rig/{remote}")
        steps.append(f"Deployed {len(files_to_deploy)} files")

        # 5. vrfarm.service (pi_api), rendered for this Pi's user
        unit = _render_unit(ROOT / "pi_api" / "vrfarm.service", user)
        try:
            scp(unit, f"{ssh_prefix}:/tmp/vrfarm.service")
        finally:
            os.unlink(unit)
        ssh(ssh_prefix,
            "sudo cp /tmp/vrfarm.service /etc/systemd/system/ && "
            "sudo systemctl daemon-reload && sudo systemctl enable vrfarm && "
            "sudo systemctl kill vrfarm 2>/dev/null; sudo systemctl restart vrfarm",
            timeout=20)
        steps.append("Installed systemd service")

        # 5b. shepherd (leader only): a separate process from pi_api by design, so a pi_api
        #     stall cannot take the watchdog down. Its config.yaml is seeded with cp -n so
        #     Pi-side edits survive a re-Install; the Monitor toggle decides up or down.
        if role == "leader":
            shepherd_on = data.get("shepherd_enabled", True)
            unit = _render_unit(ROOT / "shepherd" / "shepherd.service", user)
            try:
                scp(unit, f"{ssh_prefix}:/tmp/shepherd.service")
            finally:
                os.unlink(unit)
            scp(str(ROOT / "shepherd" / "config.yaml"), f"{ssh_prefix}:/tmp/shepherd.config.yaml")
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


def _render_unit(local_path, user: str) -> str:
    """A systemd unit rendered for one Pi: {{USER}} and {{HOME}} in the template become the
    rig YAML's pis[].user and /home/<user>, so a Pi whose user is not vruser gets a working
    unit. Returns the temp file to scp; the caller removes it."""
    text = Path(local_path).read_text().replace("{{USER}}", user).replace("{{HOME}}", f"/home/{user}")
    fd, tmp = tempfile.mkstemp(suffix=".service")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return tmp


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
    """Push the deploy manifest to one Pi over its REST API, then restart pi_api (and, on a
    leader, shepherd) so the new code runs."""
    rs: RigState = g.rs
    data = request.json or {}
    ip = data["ip"]
    role = data.get("role", "leader")
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
        # Ship the rig yaml too, so pi_api finds rigs/<rig>.yaml.
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
        rs.logger.info(f"DEPLOY_PI {ip} ok")
        return jsonify({"ok": True, "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps})


# ── devices ──

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
    """Proxy a Pi video device's MJPEG stream (?device=<name>) same-origin, so the preview works
    from any browser that reaches the controller. The relay reconnects through pi_api restarts
    and Reinit instead of collapsing to a silent blank (shared/mjpeg_relay.py)."""
    rs: RigState = g.rs
    name = request.args.get("device", "")
    ip = next((pi["ip"] for pi in rs.pis if name and name in pi.get("devices", [])), None)
    if not ip:
        return "No such device", 400
    return Response(relay(f"http://{ip}:{rs.api_port}/api/camera_stream?device={name}"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


def _init_one_device(dev_name, devices, post):
    """Run the Pi-side init for ONE device through the generic /api/init_device; pi_api resolves
    the class from the device's `type`. `post(endpoint, payload, label, timeout) -> (ok, msg)` is
    bound to that device's Pi by the caller. Shared by init_devices and reinit_device."""
    cfg = devices.get(dev_name, {}) or {}
    if cfg.get("video"):
        # release any stuck or streaming pipeline first, so a frozen preview recovers
        post("/api/camera_preview_stop", {"device": dev_name}, f"{dev_name} release", 15)
    return post("/api/init_device",
                {"name": dev_name, "type": cfg.get("type", dev_name), "config": cfg},
                dev_name, 25)


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
    for dev_name, cfg in devices.items():          # rig-YAML order; `enabled` defaults True
        if not (cfg or {}).get("enabled", True):
            continue
        ip = dev_to_ip.get(dev_name)
        if ip:
            ok, msg = _init_one_device(dev_name, devices, _mk_post(ip, api_port, steps))
            results[dev_name] = {"ok": ok, "message": msg}
    all_ok = all(r["ok"] for r in results.values())      # a rig with no devices is fine
    return jsonify({"ok": all_ok, "steps": steps, "results": results})


@bp.route("/reinit_device", methods=["POST"])
def api_reinit_device():
    rs: RigState = g.rs
    if rs.phase == "running":
        return jsonify({"ok": False, "error": "rig is running a session"}), 409
    dev_name = (request.json or {}).get("device")
    devices = rs.config.get("devices", {})
    if dev_name not in devices or not devices[dev_name].get("enabled", True):
        return jsonify({"ok": False, "error": f"{dev_name} is not enabled"}), 400
    ip = next((pi["ip"] for pi in rs.pis if dev_name in pi.get("devices", [])), None)
    if not ip:
        return jsonify({"ok": False, "error": f"{dev_name} is not assigned to a Pi"}), 400
    steps = []
    ok, _msg = _init_one_device(dev_name, devices, _mk_post(ip, rs.api_port, steps))
    return jsonify({"ok": ok, "device": dev_name, "steps": steps})
