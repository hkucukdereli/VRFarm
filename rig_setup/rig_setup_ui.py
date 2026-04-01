"""
rig_setup/rig_setup_ui.py

Flask UI for rig setup and management:
  - Add/remove rigs (each saved as rig_<name>.json)
  - Add/remove Pis per rig (stim | control roles)
  - Load existing rig JSON files
  - Two setup actions per Pi:
    1. Install — apt + pip packages (first-time, or reinstall)
    2. Deploy  — transfer code, create folders, init hardware
  - Track install/deploy status in rig JSON
  - Generate warp map on stim Pi

Run on Mac:
  cd ~/VRFarm
  python rig_setup/rig_setup_ui.py

Open: http://localhost:4999
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

ROOT     = Path(__file__).parent.parent
SETUP_DIR = Path(__file__).parent
RIG_JSON = ROOT / 'rig.json'           # minimal — kept for experiment_ui compat

app   = Flask(__name__, template_folder=str(SETUP_DIR / 'templates'))
_log  = []
_lock = threading.Lock()

def log(msg, cls='info'):
    with _lock:
        _log.append({'msg': msg, 'cls': cls, 't': time.strftime('%H:%M:%S')})
    print(f"[setup] {msg}")


# ── Rig JSON management ──────────────────────────────────────────────────────

def rig_path(name):
    """Path for a rig config file: rig_setup/rig_<name>.json"""
    return SETUP_DIR / f'rig_{name}.json'


def list_rigs():
    """Return list of rig names (from rig_*.json files in rig_setup/)."""
    rigs = []
    for f in sorted(SETUP_DIR.glob('rig_*.json')):
        name = f.stem.removeprefix('rig_')
        rigs.append(name)
    return rigs


def load_rig(name):
    """Load a rig config by name. Returns dict or None.
    Ensures all Pi entries have install/deploy tracking fields."""
    p = rig_path(name)
    if not p.exists():
        return None
    with open(p) as f:
        data = json.load(f)
    # Backfill missing tracking fields on Pi entries
    for pi in data.get('pis', []):
        pi.setdefault('status', 'unknown')
        pi.setdefault('installed', False)
        pi.setdefault('installed_at', None)
        pi.setdefault('deployed', False)
        pi.setdefault('deployed_at', None)
    return data


def save_rig(name, data):
    """Save rig config. Ensures name field matches."""
    data['name'] = name
    p = rig_path(name)
    with open(p, 'w') as f:
        json.dump(data, f, indent=2)
    # Also update minimal rig.json for experiment_ui compatibility
    _update_compat_rig_json(data)


def _update_compat_rig_json(data):
    """Write minimal rig.json (used by experiment_ui.py)."""
    compat = {'pis': []}
    for pi in data.get('pis', []):
        compat['pis'].append({
            'ip':   pi['ip'],
            'name': pi['name'],
            'role': pi['role'],
            'user': pi.get('user', 'vruser'),
            'status': pi.get('status', 'unknown'),
        })
    with open(RIG_JSON, 'w') as f:
        json.dump(compat, f, indent=2)


def new_rig_template(name):
    """Create a blank rig config."""
    return {
        'name': name,
        'pis': [],
        'network': {
            'worker_port':  5570,
            'monitor_port': 5571,
            'control_port': 5572,
            'flask_port':   5000,
            'camera_port':  5001,
        },
        'data': {
            'behavior_dir': '/Users/hakan/VRFarm/data',
            'stim_dir':     '/home/vruser/stims',
            'video_dir':    '/media/vruser/ssd/video',
        },
        'lick': {
            'i2c_address': '0x5A',
            'electrode': 4,
        },
        'reward': {
            'pins': {'main': {'gpio': 18, 'label': 'reward'}},
            'calibration': {'main': [[10, 2.1], [20, 4.3], [30, 6.8], [50, 11.2]]},
        },
        'camera': {
            'resolution': [1280, 720],
            'fps': 50,
        },
        'photodiode': {'gpio': 24},
    }


def new_pi_template(role='control'):
    """Create a blank Pi entry."""
    return {
        'name': '',
        'ip': '192.168.10.',
        'user': 'vruser',
        'role': role,
        'status': 'unknown',
        'installed': False,
        'installed_at': None,
        'deployed': False,
        'deployed_at': None,
    }


# ── SSH helpers ───────────────────────────────────────────────────────────────

def ssh_run(ip, user, cmd, timeout=30):
    """Run a command on a Pi via SSH. Returns (success, stdout, stderr)."""
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(ip, username=user, timeout=timeout,
                       key_filename=os.path.expanduser('~/.ssh/id_rsa'))
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        client.close()
        return True, out, err
    except Exception as e:
        return False, '', str(e)


def ssh_put(ip, user, local_path, remote_path, timeout=15):
    """Copy a file to a Pi via SFTP."""
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(ip, username=user, timeout=timeout,
                       key_filename=os.path.expanduser('~/.ssh/id_rsa'))
        sftp = client.open_sftp()
        sftp.put(str(local_path), str(remote_path))
        sftp.close()
        client.close()
        return True, ''
    except Exception as e:
        return False, str(e)


CONDA_PREFIX = ('source ~/miniforge3/etc/profile.d/conda.sh && '
                'conda activate rig && ')

# ── Package definitions ───────────────────────────────────────────────────────

SYSTEM_PKGS = {
    'stim': [
        'libx11-dev', 'libxext-dev', 'libxi-dev',
        'xserver-xorg-core', 'libgl1-mesa-dri', 'libglu1-mesa', 'mesa-utils',
        'chrony',
    ],
    'control': [
        'libcap-dev',
        'chrony',
    ],
}

PIP_PKGS = {
    'stim': {
        'base': 'pyzmq h5py pyyaml flask numpy scipy',
        'psychopy': 'psychopy --no-deps',
        'psychopy_deps': ('"pyglet==1.5.27" pillow moviepy imageio imageio-ffmpeg '
                          'pyopengl requests packaging psutil six json_tricks '
                          'pandas pyserial python-bidi arabic-reshaper freetype-py'),
    },
    'control': {
        'base': 'pyzmq smbus2 pyyaml flask numpy picamera2',
        'pigpio': 'pigpio',
    },
}

# ── Code files to deploy per role ─────────────────────────────────────────────

FILES_BY_ROLE = {
    'stim': [
        ROOT / 'shared'  / 'protocol.py',
        ROOT / 'stim'    / 'task.py',
        ROOT / 'stim'    / 'stim_generator.py',
    ],
    'control': [
        ROOT / 'shared'  / 'protocol.py',
        ROOT / 'control' / 'worker.py',
    ],
}

CAL_FILES = [
    ROOT / 'calibration' / 'compute_warp_map.py',
    ROOT / 'calibration' / 'rig_geometry.yaml',
    ROOT / 'calibration' / 'display_test_patches.py',
    ROOT / 'calibration' / 'fit_luminance_correction.py',
    ROOT / 'calibration' / 'validate_calibration.py',
]


# ── Install: system + pip packages (first-time setup) ────────────────────────

def _install_pi(pi, rig_name):
    ip, user, role, name = pi['ip'], pi['user'], pi['role'], pi['name']
    log(f"=== Installing packages on {name} ({ip}) role={role} ===")

    # 0. SSH connection test
    log(f"  Testing SSH connection...")
    ok, out, err = ssh_run(ip, user, 'echo ok && hostname', timeout=10)
    if ok:
        pi['status'] = 'ok'
        log(f"  SSH: connected ({out})", 'ok')
    else:
        pi['status'] = 'error'
        log(f"  SSH failed: {err}", 'err')
        _save_pi_state(rig_name, pi)
        return False

    # 1. System packages
    apt_pkgs = ' '.join(SYSTEM_PKGS.get(role, []))
    if apt_pkgs:
        log(f"  Installing system packages: {apt_pkgs}")
        ok, out, err = ssh_run(ip, user,
                               f'sudo apt-get update -qq && sudo apt-get install -y {apt_pkgs} 2>&1 | tail -5',
                               timeout=120)
        if ok:
            log(f"  System packages: done", 'ok')
        else:
            log(f"  System packages error: {err}", 'err')
            return False

    # 2. Create conda env if missing
    log(f"  Checking conda environment...")
    ok, out, err = ssh_run(ip, user,
                           'source ~/miniforge3/etc/profile.d/conda.sh && '
                           'conda env list 2>&1 | grep -q "^rig " && echo exists || '
                           '(conda create -n rig python=3.11 -y 2>&1 | tail -3 && echo created)',
                           timeout=120)
    if ok:
        log(f"  Conda env: {out.splitlines()[-1] if out else 'ok'}", 'ok')
    else:
        log(f"  Conda env error: {err}", 'err')
        return False

    # 3. Pip packages
    pkgs = PIP_PKGS.get(role, {})

    # Base packages
    if pkgs.get('base'):
        log(f"  Installing base pip packages...")
        ok, out, err = ssh_run(ip, user,
                               f'{CONDA_PREFIX}pip install {pkgs["base"]} --quiet 2>&1 | tail -5',
                               timeout=180)
        log(f"  pip base: {out or err or 'done'}", 'ok' if ok else 'err')

    # PsychoPy (stim only — must be --no-deps)
    if pkgs.get('psychopy'):
        log(f"  Installing PsychoPy (--no-deps)...")
        ok, out, err = ssh_run(ip, user,
                               f'{CONDA_PREFIX}pip install {pkgs["psychopy"]} --quiet 2>&1 | tail -5',
                               timeout=180)
        log(f"  pip psychopy: {out or err or 'done'}", 'ok' if ok else 'err')

    # PsychoPy deps (stim only — pyglet==1.5.27 pinned)
    if pkgs.get('psychopy_deps'):
        log(f"  Installing PsychoPy dependencies (pyglet==1.5.27 pinned)...")
        ok, out, err = ssh_run(ip, user,
                               f'{CONDA_PREFIX}pip install {pkgs["psychopy_deps"]} --quiet 2>&1 | tail -5',
                               timeout=180)
        log(f"  pip psychopy deps: {out or err or 'done'}", 'ok' if ok else 'err')

    # pigpio (control only)
    if pkgs.get('pigpio'):
        log(f"  Installing pigpio...")
        ok, out, err = ssh_run(ip, user,
                               f'{CONDA_PREFIX}pip install {pkgs["pigpio"]} --quiet 2>&1 | tail -5',
                               timeout=60)
        log(f"  pip pigpio: {out or err or 'done'}", 'ok' if ok else 'err')

    # 4. Role-specific system setup
    if role == 'control':
        # Start pigpiod
        ok, out, err = ssh_run(ip, user, 'sudo pigpiod 2>/dev/null; echo pigpiod_ok')
        log(f"  pigpiod: started/already running", 'ok')

    if role == 'stim':
        # Write xorg.conf for modesetting driver (24-bit visuals for PsychoPy)
        xorg_cmd = (
            "sudo tee /etc/X11/xorg.conf > /dev/null << 'XEOF'\n"
            'Section "Device"\n'
            '    Identifier "drm"\n'
            '    Driver "modesetting"\n'
            'EndSection\n'
            '\n'
            'Section "Screen"\n'
            '    Identifier "Default Screen"\n'
            '    Device "drm"\n'
            '    DefaultDepth 24\n'
            '    SubSection "Display"\n'
            '        Depth 24\n'
            '    EndSubSection\n'
            'EndSection\n'
            'XEOF'
        )
        ok, out, err = ssh_run(ip, user, xorg_cmd)
        log(f"  xorg.conf: {'written' if ok else err}", 'ok' if ok else 'warn')

    # Mark installed
    pi['installed'] = True
    pi['installed_at'] = time.strftime('%Y-%m-%d %H:%M')
    _save_pi_state(rig_name, pi)

    log(f"  {name} install complete", 'ok')
    return True


# ── Deploy: transfer files, create folders, init hardware ─────────────────────

def _deploy_pi(pi, rig_name):
    ip, user, role, name = pi['ip'], pi['user'], pi['role'], pi['name']
    log(f"=== Deploying to {name} ({ip}) role={role} ===")

    # 0. SSH connection test
    log(f"  Testing SSH connection...")
    ok, out, err = ssh_run(ip, user, 'echo ok && hostname', timeout=10)
    if ok:
        pi['status'] = 'ok'
        log(f"  SSH: connected ({out})", 'ok')
    else:
        pi['status'] = 'error'
        log(f"  SSH failed: {err}", 'err')
        _save_pi_state(rig_name, pi)
        return False

    # 1. Create folders
    dirs = '~/rig'
    if role == 'stim':
        dirs = '~/rig ~/rig/calibration ~/stims'
    elif role == 'control':
        dirs = '~/rig /media/vruser/ssd/video'
    ok, out, err = ssh_run(ip, user, f'mkdir -p {dirs}')
    if ok:
        log(f"  Folders: {dirs}", 'ok')
    else:
        log(f"  Folder error: {err}", 'err')
        return False

    # 2. Transfer code files
    for local in FILES_BY_ROLE.get(role, []):
        if not local.exists():
            log(f"  Missing: {local}", 'warn')
            continue
        remote = f'/home/{user}/rig/{local.name}'
        ok, err = ssh_put(ip, user, local, remote)
        if ok:
            log(f"  Sent: {local.name}", 'ok')
        else:
            log(f"  Failed {local.name}: {err}", 'err')

    # 3. Transfer calibration files (stim only)
    if role == 'stim':
        for local in CAL_FILES:
            if not local.exists():
                log(f"  Calibration missing: {local.name}", 'warn')
                continue
            remote = f'/home/{user}/rig/calibration/{local.name}'
            ok, err = ssh_put(ip, user, local, remote)
            if ok:
                log(f"  Sent calibration: {local.name}", 'ok')
            else:
                log(f"  Failed {local.name}: {err}", 'err')

    # 4. Hardware init
    if role == 'control':
        # Ensure pigpiod is running
        ok, out, err = ssh_run(ip, user, 'sudo pigpiod 2>/dev/null; echo ok')
        log(f"  pigpiod: started/already running", 'ok')

    if role == 'stim':
        # Initialize DLP projector: GPIO ALT2, video buffer, DLPC3436, X11
        log(f"  Initializing projector...")
        init_cmd = (
            'for i in $(seq 0 21); do pinctrl set $i a2; done && '
            'pinctrl set 25 op dh && '
            f'{CONDA_PREFIX}'
            'cd ~/dlp && python3 init_parallel_mode.py 2>&1 && '
            'if ! pgrep -x Xorg > /dev/null; then '
            '  sudo X :0 -ac > /tmp/xorg.log 2>&1 & sleep 3; '
            'fi && '
            'echo "projector_init_ok"'
        )
        ok, out, err = ssh_run(ip, user, init_cmd, timeout=30)
        if ok and 'projector_init_ok' in out:
            log(f"  Projector: initialized, X11 running", 'ok')
        else:
            log(f"  Projector init warning: {err or out}", 'warn')

    # Mark deployed
    pi['deployed'] = True
    pi['deployed_at'] = time.strftime('%Y-%m-%d %H:%M')
    _save_pi_state(rig_name, pi)

    log(f"  {name} deploy complete", 'ok')
    return True


def _check_deployed(pi):
    """Check if code files are already on the Pi. Returns dict of results."""
    ip, user, role = pi['ip'], pi['user'], pi['role']
    files = FILES_BY_ROLE.get(role, [])
    found = []
    for local in files:
        remote = f'/home/{user}/rig/{local.name}'
        ok, out, _ = ssh_run(ip, user, f'test -f {remote} && echo yes || echo no', timeout=10)
        if ok and 'yes' in out:
            found.append(local.name)
    return {'files_expected': [f.name for f in files], 'files_found': found,
            'all_present': len(found) == len(files)}


def _check_installed(pi):
    """Check if packages are already installed on a Pi. Returns dict of results."""
    ip, user, role = pi['ip'], pi['user'], pi['role']
    results = {'conda_env': False, 'system_pkgs': False, 'pip_pkgs': False}

    # Check conda env exists
    ok, out, err = ssh_run(ip, user,
                           'source ~/miniforge3/etc/profile.d/conda.sh && '
                           'conda env list 2>&1 | grep -q "^rig " && echo yes || echo no')
    results['conda_env'] = ok and 'yes' in out

    # Check key pip packages
    if role == 'stim':
        check_pkg = 'psychopy'
    else:
        check_pkg = 'smbus2'
    ok, out, err = ssh_run(ip, user,
                           f'{CONDA_PREFIX}python -c "import {check_pkg}; print(\'ok\')" 2>&1')
    results['pip_pkgs'] = ok and 'ok' in out

    # Check a system package
    apt_check = SYSTEM_PKGS.get(role, ['chrony'])[0]
    ok, out, err = ssh_run(ip, user,
                           f'dpkg -s {apt_check} 2>/dev/null | grep -q "ok installed" && echo yes || echo no')
    results['system_pkgs'] = ok and 'yes' in out

    return results


def _save_pi_state(rig_name, pi):
    """Update a single Pi's state in the rig JSON."""
    data = load_rig(rig_name)
    if not data:
        return
    for p in data.get('pis', []):
        if p['name'] == pi['name'] and p['ip'] == pi['ip']:
            p.update({
                'installed': pi.get('installed', False),
                'installed_at': pi.get('installed_at'),
                'deployed': pi.get('deployed', False),
                'deployed_at': pi.get('deployed_at'),
                'status': pi.get('status', 'unknown'),
            })
            break
    save_rig(rig_name, data)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('rig_setup.html')


@app.route('/stream')
def stream():
    def gen():
        last = 0
        while True:
            with _lock:
                events = _log[last:]
            for e in events:
                yield f"data: {json.dumps(e)}\n\n"
            last += len(events)
            time.sleep(0.1)
    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── Rig CRUD ──────────────────────────────────────────────────────────────────

@app.route('/api/rigs', methods=['GET'])
def api_list_rigs():
    """List all available rig names."""
    return jsonify({'rigs': list_rigs()})


@app.route('/api/rig/<name>', methods=['GET'])
def api_get_rig(name):
    """Load a rig config by name."""
    data = load_rig(name)
    if data:
        return jsonify(data)
    return jsonify({'error': f'Rig "{name}" not found'}), 404


@app.route('/api/rig/<name>', methods=['POST'])
def api_save_rig_route(name):
    """Save a rig config."""
    data = request.json
    save_rig(name, data)
    log(f'Rig "{name}" saved → {rig_path(name).name}', 'ok')
    return jsonify({'ok': True})


@app.route('/api/rig/<name>', methods=['DELETE'])
def api_delete_rig(name):
    """Delete a rig config file."""
    p = rig_path(name)
    if p.exists():
        p.unlink()
        log(f'Rig "{name}" deleted', 'warn')
        return jsonify({'ok': True})
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/rig/new', methods=['POST'])
def api_new_rig():
    """Create a new rig with default template."""
    name = request.json.get('name', '').strip().lower()
    if not name or not name.replace('_', '').isalnum():
        return jsonify({'error': 'Invalid rig name (alphanumeric + underscore only)'}), 400
    if rig_path(name).exists():
        return jsonify({'error': f'Rig "{name}" already exists'}), 409
    data = new_rig_template(name)
    save_rig(name, data)
    log(f'Created new rig: {name}', 'ok')
    return jsonify({'ok': True, 'rig': data})


# ── Pi actions ────────────────────────────────────────────────────────────────

@app.route('/api/test', methods=['POST'])
def api_test():
    """Test SSH connectivity to a Pi."""
    pi = request.json['pi']
    ok, out, err = ssh_run(pi['ip'], pi.get('user', 'vruser'),
                           'echo ok && python3 --version && hostname',
                           timeout=10)
    if ok:
        return jsonify({'ok': True, 'info': out})
    return jsonify({'ok': False, 'error': err})


@app.route('/api/check_installed', methods=['POST'])
def api_check_installed():
    """Check if packages are installed on a Pi."""
    pi = request.json['pi']
    results = _check_installed(pi)
    all_ok = all(results.values())
    return jsonify({'ok': True, 'installed': all_ok, 'details': results})


@app.route('/api/check_deployed', methods=['POST'])
def api_check_deployed():
    """Check if code files are already on a Pi."""
    pi = request.json['pi']
    results = _check_deployed(pi)
    return jsonify({'ok': True, **results})


@app.route('/api/install', methods=['POST'])
def api_install():
    """Install system + pip packages on a Pi (background thread)."""
    pi = request.json['pi']
    rig_name = request.json.get('rig_name', '')
    def _run():
        ok = _install_pi(pi, rig_name)
        log(f"Install {'complete' if ok else 'FAILED'} for {pi['name']}",
            'ok' if ok else 'err')
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/deploy', methods=['POST'])
def api_deploy():
    """Deploy code + init hardware on a Pi (background thread)."""
    pi = request.json['pi']
    rig_name = request.json.get('rig_name', '')
    def _run():
        ok = _deploy_pi(pi, rig_name)
        log(f"Deploy {'complete' if ok else 'FAILED'} for {pi['name']}",
            'ok' if ok else 'err')
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/warp_map', methods=['POST'])
def api_warp_map():
    """Generate warp map on stim Pi."""
    pi = request.json['pi']
    def _run():
        log(f"Running compute_warp_map.py on {pi['name']}...")
        ok, out, err = ssh_run(
            pi['ip'], pi.get('user', 'vruser'),
            (f'{CONDA_PREFIX}'
             'python ~/rig/calibration/compute_warp_map.py 2>&1'),
            timeout=300
        )
        if ok:
            for line in out.splitlines():
                if line.strip():
                    log(f"  {line}", 'ok' if 'Saved' in line else 'info')
            log("Warp map complete", 'ok')
        else:
            log(f"Warp map failed: {err}", 'err')
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=310)
    return jsonify({'ok': not t.is_alive(), 'error': None if not t.is_alive() else 'timeout'})


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import webbrowser, threading as _t
    _t.Timer(1.0, lambda: webbrowser.open('http://localhost:4999')).start()
    print("\nVRFarm Rig Setup: http://localhost:4999\n")
    app.run(host='0.0.0.0', port=4999, debug=False,
            threaded=True, use_reloader=False)
