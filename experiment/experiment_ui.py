"""
experiment/experiment_ui.py

Flask UI for running experiments:
  - Load / save / edit config YAML
  - Select hardware devices (camera, photodiode, etc.)
  - Reward calibration + manual delivery
  - Connect to Pis (SSH deploy + start processes)
  - Live camera stream, lick raster, reward/stim indicators
  - GO button to start experiment
  - End session + transfer data

Run on Mac:
  cd ~/VRFarm
  python experiment/experiment_ui.py [--config experiment/config/HK001_day07.yaml]

Open: http://localhost:5000
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import zmq
from flask import Flask, Response, jsonify, render_template, request, send_file

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from shared.protocol import (ExperimentConfig, load_config,
                              register_session)

app = Flask(__name__, template_folder=str(Path(__file__).parent / 'templates'))

# ── Global state ───────────────────────────────────────────────────────────────

G = {
    'config':       None,
    'config_path':  None,
    'state':        'setup',      # setup | connected | ready | running | ended
    'conn':         {'stim': False, 'control': False},
    'events':       [],
    'trial_data':   [],
    'n_complete':   0,
    'hit_count':    0,
}
_lock = threading.Lock()

def push(data):
    with _lock:
        G['events'].append(data)
        if len(G['events']) > 1000:
            G['events'] = G['events'][-1000:]

def log(msg, cls='info'):
    print(f"[exp] {msg}")
    push({'type': 'log', 'msg': msg, 'cls': cls,
          't': datetime.now().strftime('%H:%M:%S')})

# ── ZMQ monitor ────────────────────────────────────────────────────────────────

def zmq_monitor():
    cfg = G['config']
    if not cfg:
        return
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://{cfg.network.stim_ip}:{cfg.network.monitor_port}")
    sub.setsockopt_string(zmq.SUBSCRIBE, '')
    sub.setsockopt(zmq.RCVTIMEO, 500)
    while G['state'] not in ('ended',):
        try:
            msg = sub.recv_json()
            _handle_event(msg)
        except zmq.Again:
            continue
        except Exception as e:
            log(f"ZMQ error: {e}", 'err')
            break
    sub.close(); ctx.term()

def control_monitor():
    """Subscribe to cheddar's direct monitor PUB for lick/photodiode events."""
    cfg = G['config']
    if not cfg:
        return
    monitor_port = cfg.network.control_port + 1  # 5573
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://{cfg.network.control_ip}:{monitor_port}")
    sub.setsockopt_string(zmq.SUBSCRIBE, '')
    sub.setsockopt(zmq.RCVTIMEO, 500)
    while G.get('_control_monitor_active', False):
        try:
            msg = sub.recv_json()
            evt = msg.get('event')
            if evt == 'LICK':
                push({'type': 'lick', 't': msg.get('t', time.time())})
            elif evt == 'SYNC_PULSE':
                push({'type': 'sync_pulse', 't': msg.get('t', time.time()),
                      'pulse_idx': msg.get('pulse_idx', 0)})
            elif evt in ('CHECK_LICK_OK', 'CHECK_LICK_FAIL',
                         'CHECK_CAMERA_OK', 'CHECK_CAMERA_FAIL'):
                push({'type': evt.lower(),
                      'error': msg.get('error', '')})
        except zmq.Again:
            continue
        except Exception as e:
            log(f"Control monitor error: {e}", 'err')
            break
    sub.close(); ctx.term()

def _handle_event(msg):
    t = msg.get('type')
    if t == 'trial':
        G['trial_data'].append(msg)
        G['n_complete'] += 1
        if msg.get('outcome') == 'hit':
            G['hit_count'] += 1
        push(msg)
    elif t == 'state':
        G['state'] = msg.get('state', G['state'])
        push(msg)
    elif t == 'LICK':
        push({'type': 'lick', 't': msg.get('t', time.time())})
    elif t == 'SYNC_PULSE':
        push({'type': 'sync_pulse', 't': msg.get('t', time.time()),
              'pulse_idx': msg.get('pulse_idx', 0)})
    else:
        push(msg)

# ── SSH deploy ─────────────────────────────────────────────────────────────────

def ssh_run(ip, cmd, timeout=30, background=False):
    try:
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(ip, username='vruser', timeout=10,
                  key_filename=os.path.expanduser('~/.ssh/id_rsa'))
        _, o, e = c.exec_command(cmd, timeout=timeout)
        if background:
            c.close(); return True, '', ''
        out, err = o.read().decode().strip(), e.read().decode().strip()
        c.close(); return True, out, err
    except Exception as ex:
        return False, '', str(ex)

def ssh_put(ip, local, remote, timeout=30):
    try:
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(ip, username='vruser', timeout=10,
                  key_filename=os.path.expanduser('~/.ssh/id_rsa'))
        sftp = c.open_sftp()
        sftp.put(str(local), str(remote)); sftp.close(); c.close()
        return True, ''
    except Exception as ex:
        return False, str(ex)

def _deploy_and_start(role):
    cfg = G['config']
    ip  = cfg.network.stim_ip if role == 'stim' else cfg.network.control_ip

    log(f"Connecting to {role} Pi ({ip})...")

    # Files to deploy
    files = [ROOT / 'shared' / 'protocol.py']
    if role == 'stim':
        files += [ROOT / 'stim' / 'task.py',
                  ROOT / 'stim' / 'stim_generator.py']
    else:
        files += [ROOT / 'control' / 'worker.py']

    for f in files:
        if not f.exists():
            log(f"  Missing: {f}", 'warn'); continue
        ok, err = ssh_put(ip, f, f'/home/vruser/rig/{f.name}')
        if ok: log(f"  Sent {f.name}", 'ok')
        else:  log(f"  Failed {f.name}: {err}", 'err')

    # Write config YAML to Pi
    import yaml, tempfile
    cfg_dict = _config_to_dict(cfg)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as tmp:
        yaml.dump(cfg_dict, tmp); tmp_path = tmp.name
    ssh_put(ip, tmp_path, '/home/vruser/rig/config.yaml')
    os.unlink(tmp_path)
    log(f"  Config sent")

    # Ensure projector is initialized before starting task.py
    if role == 'stim':
        log(f"  Initializing projector...")
        init_cmd = (
            'for i in $(seq 0 21); do pinctrl set $i a2; done && '
            'pinctrl set 25 op dh && '
            'source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig && '
            'cd ~/dlp && python3 init_parallel_mode.py 2>&1 && '
            'if ! pgrep -x Xorg > /dev/null; then '
            '  sudo X :0 -ac > /tmp/xorg.log 2>&1 & sleep 3; '
            'fi && '
            'echo "projector_init_ok"'
        )
        ok, out, err = ssh_run(ip, init_cmd, timeout=30)
        if ok and 'projector_init_ok' in out:
            log(f"  Projector ready", 'ok')
        else:
            log(f"  Projector init warning: {err or out}", 'warn')

    # Start process
    script = 'task.py' if role == 'stim' else 'worker.py'
    display = 'DISPLAY=:0 ' if role == 'stim' else ''
    cmd = (f'source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig && '
           f'cd /home/vruser/rig && '
           f'nohup env {display}python {script} '
           f'--config config.yaml > /tmp/{role}.log 2>&1 &')
    ok, out, err = ssh_run(ip, cmd, background=True)
    if ok:
        log(f"  Started {script}", 'ok')
    else:
        log(f"  Failed to start {script}: {err or out}", 'err')

    G['conn'][role] = True
    push({'type': 'conn', 'role': role, 'ok': True, 'ip': ip})
    log(f"{role} ready", 'ok')

    # Start ZMQ monitor after stim connects
    if role == 'stim':
        threading.Thread(target=zmq_monitor, daemon=True).start()
        # Wait for cheddar→mozzarella handshake
        time.sleep(2)
        _check_ready()

def _check_ready():
    if G['conn']['stim'] and G['conn']['control']:
        G['state'] = 'ready'
        push({'type': 'state', 'state': 'ready'})
        log("Both Pis connected — ready to start", 'ok')

def _send_cmd(msg):
    """Send command to mozzarella (for START/STOP during experiments)."""
    cfg = G['config']
    if not cfg: return False
    try:
        ctx = zmq.Context.instance()
        s = ctx.socket(zmq.PUSH)
        s.connect(f"tcp://{cfg.network.stim_ip}:{cfg.network.monitor_port + 10}")
        s.send_json(msg, zmq.NOBLOCK)
        s.close(); return True
    except Exception as e:
        log(f"ZMQ send error: {e}", 'err'); return False

def _send_control(msg):
    """Send command directly to cheddar (for REWARD, camera, etc)."""
    cfg = G['config']
    if not cfg: return False
    try:
        ctx = zmq.Context.instance()
        s = ctx.socket(zmq.PUSH)
        s.connect(f"tcp://{cfg.network.control_ip}:{cfg.network.control_port}")
        s.send_json(msg, zmq.NOBLOCK)
        s.close(); return True
    except Exception as e:
        log(f"ZMQ control error: {e}", 'err'); return False

def _config_to_dict(cfg: ExperimentConfig) -> dict:
    """Convert ExperimentConfig back to a YAML-serializable dict."""
    d = {
        'notes': cfg.notes,
        'session': {
            'level': cfg.session.level,
            'n_trials': cfg.session.n_trials,
            'block_size': cfg.session.block_size,
            'block_sequence': cfg.session.block_sequence,
        },
        'stimulus': {
            'size_deg': cfg.stimulus.size_deg,
            'duration_s': cfg.stimulus.duration_s,
            'background_gray': cfg.stimulus.background_gray,
            'altitude_deg': cfg.stimulus.altitude_deg,
            'contrast': {
                'values': cfg.stimulus.contrast_values,
                'proportions': cfg.stimulus.contrast_probs,
            },
        },
        'timing': {
            'iti_range_s': cfg.timing.iti_range_s,
            'response_window_s': cfg.timing.response_window_s,
            'reward_delay_s': cfg.timing.reward_delay_s,
        },
        'lick': {
            'i2c_address': f'0x{cfg.lick.i2c_address:02X}',
            'electrode': cfg.lick.electrode,
            'max_lick_rate': cfg.lick.max_lick_rate,
        },
        'adaptive': {
            'enabled': cfg.adaptive.enabled,
            'initial_state': cfg.adaptive.initial_state,
            'step_up': cfg.adaptive.step_up,
            'step_down': cfg.adaptive.step_down,
        },
        'reward': {
            'amount_ul': cfg.reward.amount_ul,
            'pins': {n: {'gpio': p.gpio, 'label': p.label}
                     for n, p in cfg.reward.pins.items()},
            'calibration': {n: [list(pt) for pt in cal]
                            for n, cal in cfg.reward.calibration.items()},
        },
        'hardware': {
            'use_stim': cfg.hardware.use_stim,
            'use_reward': cfg.hardware.use_reward,
            'use_licks': cfg.hardware.use_licks,
            'use_camera': cfg.hardware.use_camera,
            'camera_resolution': cfg.hardware.camera_resolution,
            'camera_fps': cfg.hardware.camera_fps,
            'use_photodiode': cfg.hardware.use_photodiode,
            'photodiode_gpio': cfg.hardware.photodiode_gpio,
            'photodiode_pulse_every_n_frames': cfg.hardware.photodiode_pulse_every_n_frames,
        },
        'network': {
            'stim_ip': cfg.network.stim_ip,
            'control_ip': cfg.network.control_ip,
            'worker_port': cfg.network.worker_port,
            'monitor_port': cfg.network.monitor_port,
            'control_port': cfg.network.control_port,
            'flask_port': cfg.network.flask_port,
            'camera_port': cfg.network.camera_port,
        },
        'data': {
            'behavior_dir': cfg.data.behavior_dir,
            'stim_dir': cfg.data.stim_dir,
            'video_dir': cfg.data.video_dir,
        },
    }
    # Include rig name so reloading auto-discovers the rig JSON
    rig_name = G.get('rig_name', '')
    if rig_name:
        d['rig'] = {'name': rig_name}
    return d

# ── Rig discovery ─────────────────────────────────────────────────────────────

SETUP_DIR = ROOT / 'rig_setup'

def list_rigs():
    """List available rig names from rig_setup/rig_*.json files."""
    rigs = []
    for f in sorted(SETUP_DIR.glob('rig_*.json')):
        name = f.stem.removeprefix('rig_')
        rigs.append(name)
    return rigs


def load_rig_json(name):
    """Load a rig JSON by name."""
    p = SETUP_DIR / f'rig_{name}.json'
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('experiment.html', config_path=G.get('config_path', ''))

@app.route('/api/rigs', methods=['GET'])
def api_list_rigs():
    """List available rig configs found in rig_setup/."""
    rigs = list_rigs()
    return jsonify({'rigs': rigs})


@app.route('/api/rig/<name>', methods=['GET'])
def api_get_rig(name):
    """Load a rig config by name."""
    data = load_rig_json(name)
    if data:
        return jsonify(data)
    return jsonify({'error': f'Rig "{name}" not found'}), 404


@app.route('/stream')
def stream():
    def gen():
        last = 0
        while True:
            with _lock:
                evts = G['events'][last:]
            for e in evts:
                yield f"data: {json.dumps(e)}\n\n"
            last += len(evts)
            time.sleep(0.05)
    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/load_config', methods=['POST'])
def api_load_config():
    path = request.json.get('path', '')
    try:
        # Resolve absolute path relative to project root
        cfg_path = Path(path)
        if not cfg_path.is_absolute():
            cfg_path = ROOT / cfg_path
        cfg_path = cfg_path.resolve()

        # Determine rig: explicit from UI dropdown, or from YAML rig.name field
        import yaml as _yaml
        with open(cfg_path) as f:
            raw = _yaml.safe_load(f)
        rig_name = request.json.get('rig_name', '') or raw.get('rig', {}).get('name', '')
        G['rig_name'] = rig_name
        rig_json_path = None
        if rig_name:
            candidate = ROOT / 'rig_setup' / f'rig_{rig_name}.json'
            if candidate.exists():
                rig_json_path = str(candidate)
                log(f"Using rig JSON: {candidate.name}", 'ok')
            else:
                log(f"Rig JSON not found: {candidate.name}, trying rig.json", 'warn')
        if not rig_json_path:
            fallback = ROOT / 'rig.json'
            if fallback.exists():
                rig_json_path = str(fallback)
                log(f"Using fallback rig.json", 'info')

        # Subject/date/session_num: from UI request or defaults
        subj_id = request.json.get('subject_id', '') or None
        sess_date = request.json.get('date', '') or None
        sess_num = request.json.get('session_num')
        if sess_num is not None:
            sess_num = int(sess_num)

        db_dir = ROOT / 'data' / 'subjects'
        cfg = load_config(str(cfg_path),
                          subject_id=subj_id,
                          session_date=sess_date,
                          session_num=sess_num,
                          subject_db_dir=db_dir,
                          rig_json_path=rig_json_path)
        cfg.reward.compute_pulse_durations()
        G['config'] = cfg; G['config_path'] = str(cfg_path)
        summary = (
            f"Subject:  {cfg.subject_id}\n"
            f"Session:  {cfg.session_id}\n"
            f"Level:    {cfg.session.level}\n"
            f"Trials:   {cfg.session.n_trials}\n"
            f"Blocks:   {cfg.session.block_sequence}\n"
            f"Stim:     {cfg.stimulus.size_deg}° @ {cfg.stimulus.altitude_deg}° alt\n"
            f"Contrast: {cfg.stimulus.contrast_values}\n"
            f"ITI:      {cfg.timing.iti_range_s[0]}–{cfg.timing.iti_range_s[1]}s\n"
            f"MaxLick:  {cfg.lick.max_lick_rate}/s"
        )
        # Get Pi names from rig JSON for connections display
        pi_info = []
        rig_data = load_rig_json(rig_name) if rig_name else None
        if rig_data:
            for pi in rig_data.get('pis', []):
                pi_info.append({'name': pi['name'], 'ip': pi['ip'], 'role': pi['role']})
        if not pi_info:
            # Fallback: use IPs from config
            pi_info = [
                {'name': 'stim', 'ip': cfg.network.stim_ip, 'role': 'stim'},
                {'name': 'control', 'ip': cfg.network.control_ip, 'role': 'control'},
            ]
        push({'type': 'config',
              'session_id': cfg.session_id,
              'subject_id': cfg.subject_id,
              'date': cfg.date,
              'session_num': cfg.session_num,
              'rig_name': rig_name,
              'pis': pi_info,
              # Session
              'level': cfg.session.level,
              'n_trials': cfg.session.n_trials,
              'block_size': cfg.session.block_size,
              'block_sequence': cfg.session.block_sequence,
              # Stimulus
              'size_deg': cfg.stimulus.size_deg,
              'duration_s': cfg.stimulus.duration_s,
              'background_gray': cfg.stimulus.background_gray,
              'altitude_deg': cfg.stimulus.altitude_deg,
              'contrast_values': cfg.stimulus.contrast_values,
              'contrast_probs': cfg.stimulus.contrast_probs,
              # Timing
              'iti_min': cfg.timing.iti_range_s[0],
              'iti_max': cfg.timing.iti_range_s[1],
              'response_window_s': cfg.timing.response_window_s,
              'reward_delay_s': cfg.timing.reward_delay_s,
              # Lick
              'max_lick_rate': cfg.lick.max_lick_rate,
              # Adaptive
              'adaptive_enabled': cfg.adaptive.enabled,
              'adaptive_step_up': cfg.adaptive.step_up,
              'adaptive_step_down': cfg.adaptive.step_down,
              # Reward
              'reward_ul': cfg.reward.amount_ul,
              'pulse_ms': round(cfg.reward.pulse_ms('main'), 1),
              # Notes
              'notes': cfg.notes,
              # Hardware
              'use_stim': cfg.hardware.use_stim,
              'use_reward': cfg.hardware.use_reward,
              'use_licks': cfg.hardware.use_licks,
              'use_camera': cfg.hardware.use_camera,
              'use_photodiode': cfg.hardware.use_photodiode})
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/save_config', methods=['POST'])
def api_save_config():
    path = request.json.get('path', G.get('config_path', ''))
    cfg  = G['config']
    if not cfg or not path:
        return jsonify({'error': 'No config loaded or no path given'})
    try:
        import yaml
        with open(path, 'w') as f:
            yaml.dump(_config_to_dict(cfg), f, default_flow_style=False)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/update_experiment', methods=['POST'])
def api_update_experiment():
    """Update experiment parameters from the UI."""
    cfg = G['config']
    if not cfg:
        return jsonify({'ok': False, 'error': 'No config loaded'})
    d = request.json
    # Subject / date / session_num
    if 'subject_id' in d:
        cfg.subject_id = str(d['subject_id'])
    if 'date' in d:
        cfg.date = str(d['date'])
    if 'session_num' in d:
        cfg.session_num = int(d['session_num'])
    # Rebuild session_id when any of the three change
    if any(k in d for k in ('subject_id', 'date', 'session_num')):
        cfg.session_id = f"{cfg.subject_id}_{cfg.date}_{cfg.session_num:03d}"
        push({'type': 'session_id_update', 'session_id': cfg.session_id})
    # Session
    if 'level' in d:          cfg.session.level = int(d['level'])
    if 'n_trials' in d:       cfg.session.n_trials = int(d['n_trials'])
    if 'block_size' in d:     cfg.session.block_size = int(d['block_size'])
    if 'block_sequence' in d: cfg.session.block_sequence = [float(x) for x in d['block_sequence']]
    # Stimulus
    if 'size_deg' in d:       cfg.stimulus.size_deg = float(d['size_deg'])
    if 'duration_s' in d:     cfg.stimulus.duration_s = float(d['duration_s'])
    if 'background_gray' in d: cfg.stimulus.background_gray = float(d['background_gray'])
    if 'altitude_deg' in d:   cfg.stimulus.altitude_deg = float(d['altitude_deg'])
    if 'contrast_values' in d: cfg.stimulus.contrast_values = [float(x) for x in d['contrast_values']]
    if 'contrast_probs' in d: cfg.stimulus.contrast_probs = [float(x) for x in d['contrast_probs']]
    # Timing
    if 'iti_min' in d and 'iti_max' in d:
        cfg.timing.iti_range_s = [float(d['iti_min']), float(d['iti_max'])]
    if 'response_window_s' in d: cfg.timing.response_window_s = float(d['response_window_s'])
    if 'reward_delay_s' in d: cfg.timing.reward_delay_s = float(d['reward_delay_s'])
    # Lick
    if 'max_lick_rate' in d:  cfg.lick.max_lick_rate = float(d['max_lick_rate'])
    # Adaptive
    if 'adaptive_enabled' in d: cfg.adaptive.enabled = bool(d['adaptive_enabled'])
    if 'adaptive_step_up' in d: cfg.adaptive.step_up = float(d['adaptive_step_up'])
    if 'adaptive_step_down' in d: cfg.adaptive.step_down = float(d['adaptive_step_down'])
    # Notes
    if 'notes' in d:          cfg.notes = str(d['notes'])
    return jsonify({'ok': True})


@app.route('/api/update_hw', methods=['POST'])
def api_update_hw():
    cfg = G['config']
    if not cfg: return jsonify({'ok': False})
    d = request.json
    cfg.hardware.use_stim     = d.get('use_stim', True)
    cfg.hardware.use_reward   = d.get('use_reward', True)
    cfg.hardware.use_licks    = d.get('use_licks', True)
    cfg.hardware.use_camera   = d.get('use_camera', False)
    cfg.hardware.camera_fps   = d.get('camera_fps', 50)
    cfg.hardware.use_photodiode = d.get('use_photodiode', False)
    cfg.hardware.photodiode_gpio = d.get('pd_gpio', 24)
    cfg.hardware.photodiode_pulse_every_n_frames = d.get('pd_n', 5)
    res_str = d.get('camera_res', '1280,720')
    cfg.hardware.camera_resolution = [int(x) for x in res_str.split(',')]
    return jsonify({'ok': True})

@app.route('/api/connect', methods=['POST'])
def api_connect():
    role = request.json.get('role')
    if not G['config']:
        return jsonify({'error': 'Load config first'})
    threading.Thread(target=_deploy_and_start, args=(role,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/test_ssh', methods=['POST'])
def api_test_ssh():
    """Quick SSH connectivity check for a Pi."""
    ip = request.json.get('ip', '')
    if not ip:
        return jsonify({'ok': False, 'error': 'No IP given'})
    ok, out, err = ssh_run(ip, 'echo ok && hostname', timeout=8)
    return jsonify({'ok': ok, 'info': out if ok else err})


@app.route('/api/calc_pulse', methods=['POST'])
def api_calc_pulse():
    cfg = G['config']
    if not cfg: return jsonify({'error': 'No config'})
    ul = float(request.json.get('amount_ul', cfg.reward.amount_ul))
    try:
        from scipy.interpolate import interp1d
        cal = np.array(cfg.reward.calibration['main'])
        fn  = interp1d(cal[:, 1], cal[:, 0], kind='linear', fill_value='extrapolate')
        ms  = float(fn(ul))
        return jsonify({'ok': True, 'pulse_ms': round(ms, 1), 'amount_ul': ul})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/reward', methods=['POST'])
def api_reward():
    cfg = G['config']
    if not cfg: return jsonify({'error': 'No config'})
    ul = float(request.json.get('amount_ul', cfg.reward.amount_ul))
    # Recalculate pulse for requested amount
    from scipy.interpolate import interp1d
    import numpy as np
    cal = np.array(cfg.reward.calibration['main'])
    fn  = interp1d(cal[:, 1], cal[:, 0], kind='linear', fill_value='extrapolate')
    ms  = float(fn(ul))
    ok  = _send_control({'cmd': 'REWARD', 'pin': cfg.reward.pins['main'].gpio,
                         'duration_ms': ms})
    return jsonify({'ok': ok, 'pulse_ms': round(ms, 1)})

@app.route('/api/check_camera')
def api_check_camera():
    cfg = G['config']
    if not cfg: return jsonify({'error': 'No config'})
    url = f"http://{cfg.network.control_ip}:{cfg.network.camera_port}/camera"
    return jsonify({'url': url})

@app.route('/api/check_lick_sensor', methods=['POST'])
def api_check_lick_sensor():
    """Send CHECK_LICK to cheddar — response comes via monitor PUB → SSE."""
    cfg = G['config']
    if not cfg: return jsonify({'error': 'No config'})
    _ensure_control_monitor()
    ok = _send_control({'cmd': 'CHECK_LICK'})
    return jsonify({'ok': ok})

@app.route('/api/check_camera_device', methods=['POST'])
def api_check_camera_device():
    """Send CHECK_CAMERA to cheddar — response comes via monitor PUB → SSE."""
    cfg = G['config']
    if not cfg: return jsonify({'error': 'No config'})
    _ensure_control_monitor()
    ok = _send_control({'cmd': 'CHECK_CAMERA'})
    return jsonify({'ok': ok})

def _ensure_control_monitor():
    """Start control monitor thread if not already running."""
    if not G.get('_control_monitor_active'):
        G['_control_monitor_active'] = True
        threading.Thread(target=control_monitor, daemon=True).start()

@app.route('/api/monitor_licks', methods=['POST'])
def api_monitor_licks():
    cfg = G['config']
    if not cfg: return jsonify({'error': 'No config'})
    enable = request.json.get('enable', False)
    ok = _send_control({'cmd': 'MONITOR_LICKS', 'enable': enable})
    if enable:
        _ensure_control_monitor()
        log("Lick monitoring started on control Pi", 'ok')
    else:
        log("Lick monitoring stopped", 'info')
    return jsonify({'ok': ok})

@app.route('/api/monitor_photodiode', methods=['POST'])
def api_monitor_photodiode():
    cfg = G['config']
    if not cfg: return jsonify({'error': 'No config'})
    enable = request.json.get('enable', False)
    ok = _send_control({'cmd': 'MONITOR_PHOTODIODE', 'enable': enable})
    if enable:
        _ensure_control_monitor()
        log("Photodiode monitoring started on control Pi", 'ok')
    else:
        log("Photodiode monitoring stopped", 'info')
    return jsonify({'ok': ok})

@app.route('/api/generate_stim_thumbnails', methods=['POST'])
def api_generate_stim_thumbnails():
    """Generate stimuli on mozzarella, rsync NPZ back, render thumbnails on Mac."""
    cfg = G['config']
    if not cfg:
        return jsonify({'ok': False, 'error': 'No config loaded'})
    ip = cfg.network.stim_ip
    session_id = cfg.session_id

    # Run a small Python snippet on mozzarella that imports stim_generator
    # and generates the stimuli NPZ (same as task.py does on startup).
    # Pass session_id explicitly so it matches the Mac's config.
    stim_dir_pi = f'{cfg.data.stim_dir}/{session_id}'
    cmd = (
        'source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig && '
        'cd /home/vruser/rig && python3 -c "'
        'from pathlib import Path; '
        'from stim_generator import generate_stimuli, load_warp_map; '
        'from protocol import load_config; '
        'cfg = load_config(\\\"config.yaml\\\"); '
        'warp = load_warp_map(); '
        f'p = generate_stimuli(cfg, warp, Path(\\\"{stim_dir_pi}\\\")); '
        'print(\\\"STIM_OK:\\\" + str(p))'
        '" 2>&1'
    )
    ok, out, err = ssh_run(ip, cmd, timeout=120)
    if not ok:
        return jsonify({'ok': False, 'error': err or 'SSH failed'})
    if 'STIM_OK:' not in out:
        return jsonify({'ok': False, 'error': out or err or 'Stim generation failed'})

    log("Stimuli generated on mozzarella, fetching NPZ...", 'info')

    # Rsync the stimuli.npz back to Mac
    local_stim_dir = ROOT / 'data' / 'stims' / session_id
    local_stim_dir.mkdir(parents=True, exist_ok=True)
    npz_local = local_stim_dir / 'stimuli.npz'
    r = subprocess.run(
        ['rsync', '-avz',
         f'vruser@{ip}:{stim_dir_pi}/stimuli.npz',
         str(npz_local)],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0 or not npz_local.exists():
        return jsonify({'ok': False, 'error': f'NPZ rsync failed: {r.stderr}'})

    # Render thumbnails locally on Mac from the NPZ data
    count = _render_thumbnails(npz_local, local_stim_dir / 'thumbnails')
    G['_thumb_dir'] = str(local_stim_dir / 'thumbnails')
    G['_thumb_count'] = count
    log(f"Generated {count} stimulus thumbnails", 'ok')
    return jsonify({'ok': True, 'count': count})


def _render_thumbnails(npz_path, thumb_dir, size=120):
    """Render simple thumbnails from stimulus NPZ on the Mac.

    Each thumbnail is a small image showing a colored square on a gray
    background, representing the stimulus position and contrast.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches

    thumb_dir = Path(thumb_dir)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    data = np.load(npz_path)
    n = int(data['n_trials'][0])
    bg = float(data['background_gray'][0])

    # Projector resolution for scaling
    proj_w, proj_h = 1920, 1080

    for i in range(n):
        fig, ax = plt.subplots(1, 1, figsize=(1.5, 1.0), dpi=80)
        # Background: PsychoPy gray (-1..1) → matplotlib (0..1)
        bg_rgb = (bg + 1) / 2
        ax.set_facecolor((bg_rgb, bg_rgb, bg_rgb))
        ax.set_xlim(0, proj_w)
        ax.set_ylim(proj_h, 0)  # y-axis inverted

        # Stimulus square
        px = float(data['px_x'][i])
        py = float(data['px_y'][i])
        ps = int(data['px_size'][i])
        cc = float(data['corr_contrast'][i])
        # Stimulus luminance in PsychoPy: bg + contrast * (1 - bg)
        stim_gray = bg + cc * (1 - bg)
        stim_rgb = (stim_gray + 1) / 2
        stim_rgb = max(0, min(1, stim_rgb))

        rect = patches.Rectangle(
            (px - ps/2, py - ps/2), ps, ps,
            facecolor=(stim_rgb, stim_rgb, stim_rgb),
            edgecolor='white', linewidth=0.5)
        ax.add_patch(rect)

        # Label
        az = float(data['az_deg'][i])
        c = float(data['contrast'][i])
        ax.set_title(f'az={az:.0f} c={c:.2f}', fontsize=6, color='white',
                     pad=2)

        ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)

        out = thumb_dir / f'trial_{i:03d}.png'
        fig.savefig(out, bbox_inches='tight', pad_inches=0.02,
                    facecolor=(bg_rgb, bg_rgb, bg_rgb))
        plt.close(fig)

    return n


@app.route('/api/stim_thumbnail')
def api_stim_thumbnail():
    """Serve a stimulus thumbnail PNG."""
    trial = request.args.get('trial', 0, type=int)
    thumb_dir = G.get('_thumb_dir', '')
    if not thumb_dir:
        return jsonify({'error': 'No thumbnails generated'}), 404
    # Look for trial_NNN.png
    candidates = [
        Path(thumb_dir) / f'trial_{trial:03d}.png',
        Path(thumb_dir) / f'trial_{trial}.png',
        Path(thumb_dir) / f'{trial:03d}.png',
        Path(thumb_dir) / f'{trial}.png',
    ]
    for p in candidates:
        if p.exists():
            return send_file(str(p), mimetype='image/png')
    return jsonify({'error': f'Thumbnail not found for trial {trial}'}), 404


@app.route('/api/set_camera_mode', methods=['POST'])
def api_set_camera_mode():
    """Send camera resolution/fps change to cheddar."""
    cfg = G['config']
    if not cfg:
        return jsonify({'ok': False, 'error': 'No config loaded'})
    d = request.json or {}
    res = d.get('resolution', cfg.hardware.camera_resolution)
    fps = d.get('fps', cfg.hardware.camera_fps)
    ok = _send_control({'cmd': 'SET_CAMERA_MODE', 'resolution': res, 'fps': fps})
    return jsonify({'ok': ok})


@app.route('/api/start', methods=['POST'])
def api_start():
    if not all(G['conn'].values()):
        return jsonify({'error': 'Not all Pis connected'})
    _send_cmd({'cmd': 'START'})
    G['state'] = 'running'
    push({'type': 'state', 'state': 'running'})
    return jsonify({'ok': True})

@app.route('/api/end', methods=['POST'])
def api_end():
    _send_cmd({'cmd': 'STOP'})
    cfg = G['config']
    if cfg:
        db_dir = Path(cfg.data.behavior_dir) / 'subjects'
        register_session(cfg, db_dir, n_trials_completed=G['n_complete'])
        log(f"Session registered: {cfg.session_id} ({G['n_complete']} trials)", 'ok')
    # Auto-increment session_num for next run
    if cfg:
        cfg.session_num += 1
        cfg.session_id = f"{cfg.subject_id}_{cfg.date}_{cfg.session_num:03d}"
        push({'type': 'session_id_update', 'session_id': cfg.session_id,
              'session_num': cfg.session_num})
    # Reset counters for next session
    G['n_complete'] = 0
    G['hit_count'] = 0
    G['trial_data'] = []
    # Go back to ready (Pis are still connected)
    G['state'] = 'ready'
    push({'type': 'state', 'state': 'ready'})
    log("Session ended — ready for next run", 'ok')
    return jsonify({'ok': True})

@app.route('/api/transfer', methods=['POST'])
def api_transfer():
    api_end()
    time.sleep(1)
    cfg = G['config']
    if not cfg: return jsonify({'error': 'No config'})
    dst = Path(cfg.data.behavior_dir) / cfg.subject_id
    files = 0; errors = []

    def rsync(host, src, dest):
        nonlocal files
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(['rsync', '-avz', f'vruser@{host}:{src}', dest],
                           capture_output=True, text=True)
        if r.returncode == 0:
            files += 1; log(f"Transferred {src}", 'ok')
        else:
            errors.append(src); log(f"Transfer failed: {src}", 'err')

    rsync(cfg.network.stim_ip,
          cfg.data.stim_dir + '/' + cfg.session_id + '/',
          str(dst / 'stims/'))
    rsync(cfg.network.control_ip,
          cfg.data.video_dir + '/' + cfg.session_id + '/',
          str(dst / 'video/'))

    return jsonify({'ok': not errors, 'files': files,
                    'error': '; '.join(errors) if errors else None})

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='Pre-load config YAML')
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()
    if args.config:
        G['config_path'] = args.config
    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open(f'http://localhost:{args.port}')).start()
    print(f"\nVRFarm Experiment UI: http://localhost:{args.port}\n")
    app.run(host='0.0.0.0', port=args.port, debug=False,
            threaded=True, use_reloader=False)

if __name__ == '__main__':
    main()
