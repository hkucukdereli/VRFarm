"""
mozzarella/task.py

Main trial loop — runs on mozzarella.
Handles PsychoPy stimulus presentation and trial state machine.
Communicates with cheddar (worker.py) via ZMQ.
Publishes events to Mac monitor via ZMQ PUB.

Start via SSH from mac/setup.py, or manually:
  conda activate rig
  DISPLAY=:0 python task.py --config config.yaml
"""

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import h5py
import numpy as np
import zmq

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from protocol import ExperimentConfig, load_config
from stim_generator import (generate_stimuli, load_stimuli,
                             load_warp_map)

# ── State ──────────────────────────────────────────────────────────────────────

class TaskState:
    def __init__(self, config: ExperimentConfig):
        self.config = config
        self.trial_num       = 0
        self.adaptive_state  = config.adaptive.initial_state
        self.running         = False
        self.stop_requested  = False

        # Per-trial accumulators (reset each trial)
        self.lick_times_trial   = []   # relative to stim onset
        self.baseline_lick_count = 0
        self.reward_t           = float('nan')

        # ZMQ
        self.ctx    = zmq.Context()
        self.worker = None   # DEALER socket to cheddar
        self.pub    = None   # PUB socket to Mac monitor
        self.cmd    = None   # PULL socket from Mac (GO/STOP/REWARD commands)

        # Lick queue from cheddar (thread-safe)
        import queue
        self.lick_queue   = queue.Queue()
        self.reward_queue = queue.Queue()
        self.worker_ready = threading.Event()

    def setup_zmq(self):
        cfg = self.config.network
        # ROUTER: cheddar connects to us
        self.router = self.ctx.socket(zmq.ROUTER)
        self.router.bind(f"tcp://*:{cfg.worker_port}")

        # PUB: Mac monitor subscribes
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.bind(f"tcp://*:{cfg.monitor_port}")

        # PULL: Mac sends commands (GO, STOP, manual REWARD)
        self.cmd = self.ctx.socket(zmq.PULL)
        self.cmd.bind(f"tcp://*:{cfg.monitor_port + 10}")

        print(f"ZMQ: worker ROUTER on :{cfg.worker_port}")
        print(f"ZMQ: monitor PUB on :{cfg.monitor_port}")
        print(f"ZMQ: command PULL on :{cfg.monitor_port + 10}")

    def teardown_zmq(self):
        for s in [self.router, self.pub, self.cmd]:
            if s:
                s.close()
        self.ctx.term()

    def publish(self, data: dict):
        if not self.pub:
            return
        try:
            self.pub.send_json(data, zmq.NOBLOCK)
        except zmq.Again:
            pass

    def log(self, msg: str, cls: str = 'trial-event'):
        print(f"[task] {msg}")
        self.publish({'type': 'log', 'msg': msg, 'cls': cls})

    def send_worker(self, msg: dict):
        """Send command to cheddar via ROUTER."""
        if self._worker_id:
            self.router.send_multipart([
                self._worker_id,
                json.dumps(msg).encode()
            ])

    _worker_id = None   # cheddar's ROUTER identity


# ── Worker communication thread ────────────────────────────────────────────────

def worker_thread(state: TaskState):
    """Receive messages from cheddar, put lick/reward events in queues."""
    poller = zmq.Poller()
    poller.register(state.router, zmq.POLLIN)
    poller.register(state.cmd,    zmq.POLLIN)

    while not state.stop_requested:
        socks = dict(poller.poll(timeout=100))

        if state.router in socks:
            parts = state.router.recv_multipart()
            if len(parts) >= 2:
                identity = parts[0]
                try:
                    msg = json.loads(parts[-1].decode())
                except Exception:
                    continue

                state._worker_id = identity

                evt = msg.get('event')
                if evt == 'READY':
                    state.worker_ready.set()
                    state.log('cheddar ready', 'reward-event')
                    state.publish({'type': 'conn', 'pi': 'cheddar',
                                   'ok': True, 'ip': ''})
                elif evt == 'LICK':
                    state.lick_queue.put(msg['t'])
                    state.publish({'type': 'lick', 't': msg['t']})
                elif evt == 'REWARD_DONE':
                    state.reward_queue.put(msg['t'])
                elif evt == 'SYNC_PULSE':
                    state.publish({'type': 'sync_pulse',
                                   't': msg.get('t'),
                                   'pulse_idx': msg.get('pulse_idx', 0)})

        if state.cmd in socks:
            msg = state.cmd.recv_json()
            _handle_command(state, msg)


def _handle_command(state: TaskState, msg: dict):
    cmd = msg.get('cmd')
    if cmd == 'START':
        state.running = True
        state.log('GO received — starting experiment', 'stim-event')
        state.publish({'type': 'state', 'state': 'running'})
    elif cmd == 'STOP':
        state.stop_requested = True
        state.log('STOP received', 'stim-event')
    elif cmd == 'REWARD':
        # Manual test reward during setup
        state.send_worker({
            'cmd': 'REWARD',
            'pin': msg.get('pin', 18),
            'duration_ms': msg.get('duration_ms', 20),
        })


# ── HDF5 data writer ───────────────────────────────────────────────────────────

def open_hdf5(config: ExperimentConfig) -> h5py.File:
    """Open HDF5 file for incremental trial writing.
    Writes to stim_dir (local Pi path), Mac rsyncs at session end."""
    out_dir = Path(config.data.stim_dir) / config.session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{config.session_id}.h5"
    f = h5py.File(path, 'w')

    # Store config as attributes
    cfg = config
    f.attrs['session_id']      = cfg.session_id
    f.attrs['subject_id']      = cfg.subject_id
    f.attrs['session_num']     = cfg.session_num
    f.attrs['date']            = cfg.date
    f.attrs['level']           = cfg.session.level
    f.attrs['n_trials_planned']= cfg.session.n_trials
    f.attrs['block_sequence']  = cfg.session.block_sequence
    f.attrs['stim_size_deg']   = cfg.stimulus.size_deg
    f.attrs['background_gray'] = cfg.stimulus.background_gray
    f.attrs['altitude_deg']    = cfg.stimulus.altitude_deg
    f.attrs['iti_range_s']     = cfg.timing.iti_range_s
    f.attrs['response_window_s'] = cfg.timing.response_window_s
    f.attrs['reward_delay_s']  = cfg.timing.reward_delay_s
    f.attrs['max_lick_rate']   = cfg.lick.max_lick_rate
    f.attrs['reward_amount_ul']= cfg.reward.amount_ul
    f.attrs['notes']           = cfg.notes

    # Create extendable datasets
    dt_str = h5py.special_dtype(vlen=str)
    f.create_dataset('trial_num',       shape=(0,), maxshape=(None,), dtype='i4')
    f.create_dataset('block_num',       shape=(0,), maxshape=(None,), dtype='i4')
    f.create_dataset('stim_az_deg',     shape=(0,), maxshape=(None,), dtype='f4')
    f.create_dataset('stim_alt_deg',    shape=(0,), maxshape=(None,), dtype='f4')
    f.create_dataset('stim_size_deg',   shape=(0,), maxshape=(None,), dtype='f4')
    f.create_dataset('contrast',        shape=(0,), maxshape=(None,), dtype='f4')
    f.create_dataset('stim_onset_t',    shape=(0,), maxshape=(None,), dtype='f8')
    f.create_dataset('reward_t',        shape=(0,), maxshape=(None,), dtype='f8')
    f.create_dataset('reward_amount_ul',shape=(0,), maxshape=(None,), dtype='f4')
    f.create_dataset('baseline_licks',  shape=(0,), maxshape=(None,), dtype='i4')
    f.create_dataset('baseline_dur_s',  shape=(0,), maxshape=(None,), dtype='f4')
    f.create_dataset('trial_outcome',   shape=(0,), maxshape=(None,), dtype=dt_str)
    f.create_dataset('level_effective', shape=(0,), maxshape=(None,), dtype='f4')
    f.create_dataset('adaptive_state',  shape=(0,), maxshape=(None,), dtype='f4')

    # Variable-length lick time arrays
    dt_vlen = h5py.vlen_dtype(np.dtype('f8'))
    f.create_dataset('lick_times',      shape=(0,), maxshape=(None,), dtype=dt_vlen)

    print(f"HDF5: {path}")
    return f


def write_trial(f: h5py.File, row: dict):
    """Append one trial row to HDF5. Flushes after each write."""
    for key, val in row.items():
        ds = f[key]
        n  = ds.shape[0]
        ds.resize((n + 1,))
        ds[n] = val
    f.flush()


# ── Trial state machine ────────────────────────────────────────────────────────

def run_trial(state: TaskState, stim: dict,
              win, stim_rect, blank_rect) -> dict:
    """
    Run a single trial. Returns trial result dict.

    stim dict keys: trial_idx, block_num, az_deg, alt_deg, contrast,
                    px_x, px_y, px_size, corr_contrast, background_gray
    """
    from psychopy import core

    cfg    = state.config
    t_cfg  = cfg.timing
    l_cfg  = cfg.lick
    a_cfg  = cfg.adaptive

    # ── Baseline / ITI ────────────────────────────────────────────────────────
    rng      = np.random.default_rng()
    iti_s    = rng.uniform(*t_cfg.iti_range_s)
    max_licks = int(iti_s * l_cfg.max_lick_rate)

    state.baseline_lick_count = 0
    state.publish({'type': 'stim', 'on': False})

    # Drain lick queue (stale licks from previous trial)
    while not state.lick_queue.empty():
        state.lick_queue.get_nowait()

    baseline_start = time.monotonic()
    aborted = False

    while (time.monotonic() - baseline_start) < iti_s:
        if state.stop_requested:
            return None
        # Check for licks
        while not state.lick_queue.empty():
            state.lick_queue.get_nowait()
            state.baseline_lick_count += 1
        if state.baseline_lick_count > max_licks:
            # Too many licks — restart baseline
            state.log(f"Trial {state.trial_num}: abort (baseline licks={state.baseline_lick_count})")
            iti_s  = rng.uniform(*t_cfg.iti_range_s)
            max_licks = int(iti_s * l_cfg.max_lick_rate)
            state.baseline_lick_count = 0
            baseline_start = time.monotonic()
            aborted = True
        # Show blank
        blank_rect.draw()
        win.flip()
        core.wait(0.005)

    actual_iti = time.monotonic() - baseline_start

    # ── Determine reward type (level 2.5 adaptive) ────────────────────────────
    level = cfg.session.level
    if level == 2.5 and a_cfg.enabled:
        # adaptive_state ∈ [-1, 1]: positive → more free reward (level 2)
        # probability of level 3 = (1 - adaptive_state) / 2
        prob_l3 = max(0.0, min(1.0, (1.0 - state.adaptive_state) / 2))
        level_effective = 3 if rng.random() < prob_l3 else 2
    elif level == 2.5:
        level_effective = 2
    else:
        level_effective = int(level)

    # ── Stimulus onset ────────────────────────────────────────────────────────
    # Position stim_rect at (px_x, px_y) in PsychoPy pixel coords
    # PsychoPy: origin center, y up
    res_w, res_h = 1920, 1080
    stim_rect.pos   = (stim['px_x'] - res_w / 2,
                       res_h / 2 - stim['px_y'])
    stim_rect.size  = (stim['px_size'], stim['px_size'])

    # Compute fill color from background + corrected contrast
    bg = float(stim['background_gray'])
    c  = float(stim['corr_contrast'])
    # PsychoPy color range: -1 (black) to +1 (white)
    # bg_linear = (bg + 1) / 2  (convert to 0-1)
    # stim_linear = bg_linear + c * (1 - bg_linear)
    # stim_psychopy = stim_linear * 2 - 1
    bg_lin   = (bg + 1) / 2
    s_lin    = bg_lin + c * (1 - bg_lin)
    s_psycho = s_lin * 2 - 1
    stim_rect.fillColor = [s_psycho, s_psycho, s_psycho]

    # Ask cheddar to start monitoring licks for this trial
    state.send_worker({'cmd': 'START_TRIAL'})

    stim_rect.draw()
    stim_onset_t = win.flip()   # PsychoPy returns flip timestamp

    state.publish({'type': 'stim', 'on': True})

    # ── Response window ───────────────────────────────────────────────────────
    lick_times_trial = []
    reward_given     = False
    reward_t         = float('nan')

    stim_end    = stim_onset_t + cfg.stimulus.duration_s
    window_end  = stim_onset_t + t_cfg.response_window_s

    # Drain stale licks
    while not state.lick_queue.empty():
        state.lick_queue.get_nowait()

    clock = time.monotonic()
    wall_start = time.time()

    while time.time() < wall_start + t_cfg.response_window_s:
        if state.stop_requested:
            break

        now_wall = time.time()
        elapsed  = now_wall - wall_start

        # Blank screen after stimulus duration
        if elapsed >= cfg.stimulus.duration_s:
            blank_rect.draw()
            win.flip()
            state.publish({'type': 'stim', 'on': False})

        # Collect licks
        while not state.lick_queue.empty():
            lick_t = state.lick_queue.get_nowait()
            rel_t  = lick_t - stim_onset_t
            if rel_t > -0.05:   # ignore licks just before stim onset
                lick_times_trial.append(rel_t)

                # Level 1 / attention: reward on first lick before reward_delay
                if (not reward_given and
                        level_effective in (1, 3) and
                        rel_t < t_cfg.reward_delay_s):
                    _deliver_reward(state)
                    reward_t = lick_t
                    reward_given = True

        # Level 2: free reward at reward_delay_s
        if (not reward_given and
                level_effective == 2 and
                elapsed >= t_cfg.reward_delay_s):
            _deliver_reward(state)
            reward_t = wall_start + elapsed
            reward_given = True

        core.wait(0.002)

    # ── Trial outcome ─────────────────────────────────────────────────────────
    hit = len([t for t in lick_times_trial
               if 0 <= t <= t_cfg.response_window_s]) > 0
    outcome = 'hit' if hit else 'miss'

    # ── Update adaptive state (level 2.5) ─────────────────────────────────────
    if level == 2.5 and a_cfg.enabled:
        licked_during_stim = any(0 <= t <= cfg.stimulus.duration_s
                                 for t in lick_times_trial)
        if licked_during_stim:
            state.adaptive_state = min(1.0,
                state.adaptive_state + a_cfg.step_up)
        else:
            state.adaptive_state = max(-1.0,
                state.adaptive_state - a_cfg.step_down)

    result = {
        'trial_num':        state.trial_num,
        'block_num':        int(stim['block_num']),
        'stim_az_deg':      float(stim['az_deg']),
        'stim_alt_deg':     float(stim['alt_deg']),
        'stim_size_deg':    cfg.stimulus.size_deg,
        'contrast':         float(stim['contrast']),
        'stim_onset_t':     float(stim_onset_t),
        'reward_t':         reward_t,
        'reward_amount_ul': cfg.reward.amount_ul if reward_given else 0.0,
        'baseline_licks':   state.baseline_lick_count,
        'baseline_dur_s':   actual_iti,
        'trial_outcome':    outcome,
        'level_effective':  float(level_effective),
        'adaptive_state':   state.adaptive_state if level == 2.5 else float('nan'),
        'lick_times':       np.array(lick_times_trial, dtype=np.float64),
    }

    # Publish trial event to monitor
    state.publish({
        'type':           'trial',
        'trial_num':      result['trial_num'],
        'block_num':      result['block_num'],
        'stim_az':        result['stim_az_deg'],
        'contrast':       result['contrast'],
        'outcome':        outcome,
        'adaptive_state': result['adaptive_state'],
    })
    if reward_given:
        state.publish({'type': 'reward', 'on': True})
        time.sleep(0.1)
        state.publish({'type': 'reward', 'on': False})

    return result


def _deliver_reward(state: TaskState):
    cfg      = state.config
    pin_name = 'main'
    state.send_worker({
        'cmd':         'REWARD',
        'pin':         cfg.reward.pins[pin_name].gpio,
        'duration_ms': cfg.reward.pulse_ms(pin_name),
    })
    state.log(f"Reward: {cfg.reward.amount_ul}µl "
              f"({cfg.reward.pulse_ms(pin_name):.1f}ms)", 'reward-event')


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, help='Path to config.yaml')
    args = parser.parse_args()

    cfg   = load_config(args.config)
    state = TaskState(cfg)

    # ── PsychoPy setup ────────────────────────────────────────────────────────
    from psychopy import visual, core, monitors
    from psychopy.hardware import keyboard

    mon = monitors.Monitor('dlp_projector')
    win = visual.Window(
        size=(1920, 1080),
        fullscr=True,
        monitor=mon,
        color=[cfg.stimulus.background_gray] * 3,
        colorSpace='rgb',
        units='pix',
        allowGUI=False,
    )

    stim_rect = visual.Rect(
        win,
        width=100, height=100,
        fillColor=[1, 1, 1],
        lineColor=None,
        units='pix',
    )
    blank_rect = visual.Rect(
        win,
        width=1920, height=1080,
        fillColor=[cfg.stimulus.background_gray] * 3,
        lineColor=None,
        units='pix',
        pos=(0, 0),
    )

    state.log("PsychoPy window open")

    # ── ZMQ setup ─────────────────────────────────────────────────────────────
    state.setup_zmq()

    # Start worker communication thread
    wt = threading.Thread(target=worker_thread, args=(state,), daemon=True)
    wt.start()

    # ── Generate stimuli ──────────────────────────────────────────────────────
    state.log("Generating stimuli...")
    try:
        warp      = load_warp_map()
        stim_dir  = Path(cfg.data.stim_dir) / cfg.session_id
        stim_path = generate_stimuli(cfg, warp, stim_dir)
        stims     = load_stimuli(stim_path)
        state.log(f"Stimuli ready: {stims['n_trials'][0]} trials")
    except Exception as e:
        state.log(f"Stim generation failed: {e} — using fallback", 'err-event')
        # Fallback: simple centered stim
        stims = None

    # ── Wait for cheddar ──────────────────────────────────────────────────────
    state.log("Waiting for cheddar to connect...")
    state.publish({'type': 'state', 'state': 'ready'})
    if not state.worker_ready.wait(timeout=30):
        state.log("cheddar did not connect within 30s", 'err-event')
    else:
        state.publish({'type': 'state', 'state': 'ready'})

    # ── Wait for GO ───────────────────────────────────────────────────────────
    state.log("Ready. Waiting for GO command from monitor...")
    while not state.running and not state.stop_requested:
        blank_rect.draw()
        win.flip()
        core.wait(0.05)

    if state.stop_requested:
        win.close()
        state.teardown_zmq()
        return

    # ── Tell cheddar to start camera ──────────────────────────────────────────
    if cfg.hardware.use_camera:
        state.send_worker({
            'cmd':        'START_CAMERA',
            'session_id': cfg.session_id,
            'video_dir':  cfg.data.video_dir,
            'resolution': cfg.hardware.camera_resolution,
            'fps':        cfg.hardware.camera_fps,
        })

    # ── Open HDF5 ─────────────────────────────────────────────────────────────
    h5f = open_hdf5(cfg)

    # ── Trial loop ────────────────────────────────────────────────────────────
    n_trials = stims['n_trials'][0] if stims else cfg.session.n_trials
    state.log(f"Starting: {n_trials} trials")

    for i in range(n_trials):
        if state.stop_requested:
            break

        state.trial_num = i + 1

        # Build stim dict for this trial
        if stims:
            stim = {k: stims[k][i] for k in
                    ['trial_idx', 'block_num', 'az_deg', 'alt_deg',
                     'contrast', 'px_x', 'px_y', 'px_size',
                     'corr_contrast']}
            stim['background_gray'] = float(stims['background_gray'][0])
        else:
            stim = {
                'trial_idx': i, 'block_num': 0,
                'az_deg': 80.0, 'alt_deg': 49.0,
                'contrast': 1.0, 'px_x': 960.0, 'px_y': 540.0,
                'px_size': 100, 'corr_contrast': 1.0,
                'background_gray': cfg.stimulus.background_gray,
            }

        result = run_trial(state, stim, win, stim_rect, blank_rect)

        if result is None:
            break

        write_trial(h5f, result)

        if i % 10 == 0:
            state.log(f"Trial {i+1}/{n_trials} — "
                      f"outcome={result['trial_outcome']}")

    # ── Cleanup ───────────────────────────────────────────────────────────────
    state.log(f"Session complete: {state.trial_num} trials")

    if cfg.hardware.use_camera:
        state.send_worker({'cmd': 'STOP_CAMERA'})

    state.send_worker({'cmd': 'STOP'})
    h5f.close()

    blank_rect.draw()
    win.flip()
    core.wait(1.0)
    win.close()

    state.publish({'type': 'state', 'state': 'ended'})
    state.log("Done.")
    state.stop_requested = True
    wt.join(timeout=2)
    state.teardown_zmq()


if __name__ == '__main__':
    main()
