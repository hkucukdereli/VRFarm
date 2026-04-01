"""
shared/protocol.py

Config dataclass, YAML loader, and subject database.
Copied to each Pi automatically by rig_setup_ui.py.
"""

from __future__ import annotations
import json
import yaml
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional, Union


# ── Config dataclasses ─────────────────────────────────────────────────────────

@dataclass
class RewardPin:
    gpio: int
    label: str = "water"


@dataclass
class RewardConfig:
    amount_ul: float
    pins: dict                      # name → RewardPin
    calibration: dict               # name → list of (pulse_ms, volume_ul)
    _pulse_ms: dict = field(default_factory=dict, init=False)

    def compute_pulse_durations(self):
        from scipy.interpolate import interp1d
        for name, cal in self.calibration.items():
            cal_arr   = np.array(cal)
            fn        = interp1d(cal_arr[:, 1], cal_arr[:, 0],
                                 kind='linear', fill_value='extrapolate')
            self._pulse_ms[name] = float(fn(self.amount_ul))
        return self._pulse_ms

    def pulse_ms(self, name='main') -> float:
        if not self._pulse_ms:
            self.compute_pulse_durations()
        return self._pulse_ms[name]


@dataclass
class StimulusConfig:
    size_deg: float
    duration_s: float
    background_gray: float
    altitude_deg: float
    contrast_values: List[float]
    contrast_probs: List[float]


@dataclass
class SessionConfig:
    level: float
    n_trials: int
    block_size: Optional[int]
    block_sequence: List[float]


@dataclass
class TimingConfig:
    iti_range_s: List[float]
    response_window_s: float
    reward_delay_s: float


@dataclass
class LickConfig:
    i2c_address: int
    electrode: int
    max_lick_rate: float


@dataclass
class AdaptiveConfig:
    enabled: bool
    initial_state: float = 0.0
    step_up: float = 0.2
    step_down: float = 0.2


@dataclass
class HardwareConfig:
    use_stim: bool
    use_reward: bool
    use_licks: bool
    use_camera: bool
    use_photodiode: bool
    camera_resolution: List[int]
    camera_fps: int
    photodiode_gpio: int = 24
    photodiode_pulse_every_n_frames: int = 5


@dataclass
class NetworkConfig:
    stim_ip: str                    # stim Pi IP
    control_ip: str                 # control Pi IP
    worker_port: int = 5570
    monitor_port: int = 5571
    control_port: int = 5572  # direct Mac → cheddar commands
    flask_port: int = 5000
    camera_port: int = 5001


@dataclass
class DataConfig:
    behavior_dir: str               # on Mac
    stim_dir: str                   # on stim Pi
    video_dir: str                  # on control Pi


@dataclass
class ExperimentConfig:
    subject_id: str
    session_id: str
    session_num: int
    date: str

    session:   SessionConfig
    stimulus:  StimulusConfig
    timing:    TimingConfig
    lick:      LickConfig
    reward:    RewardConfig
    adaptive:  AdaptiveConfig
    hardware:  HardwareConfig
    network:   NetworkConfig
    data:      DataConfig
    notes: str = ""


# ── Rig JSON loader ──────────────────────────────────────────────────────────

def load_rig_config(rig_json_path: Union[str, Path]) -> dict:
    """Load rig JSON and derive stim_ip/control_ip from pis array."""
    with open(rig_json_path) as f:
        rig = json.load(f)
    ip_by_role = {pi['role']: pi['ip'] for pi in rig['pis']}
    net = rig.get('network', {})
    net['stim_ip'] = ip_by_role.get('stim', '')
    net['control_ip'] = ip_by_role.get('control', '')
    rig['network'] = net
    return rig


# ── YAML loader ────────────────────────────────────────────────────────────────

def load_config(yaml_path: Union[str, Path],
                subject_id: Optional[str] = None,
                session_date: Optional[str] = None,
                session_num: Optional[int] = None,
                subject_db_dir: Optional[Union[str, Path]] = None,
                rig_json_path: Optional[Union[str, Path]] = None) -> ExperimentConfig:
    with open(yaml_path) as f:
        raw = yaml.safe_load(f)

    # Load rig JSON and overlay onto raw dict
    rig = None
    if rig_json_path:
        rig = load_rig_config(rig_json_path)

    # Subject/date/session: prefer explicit params, fall back to YAML, then defaults
    subj_id = (subject_id
               or raw.get('subject', {}).get('id', '')
               or 'UNKNOWN')
    today = (session_date
             or date.today().strftime('%Y%m%d'))
    if session_num is None:
        if subject_db_dir:
            session_num = _next_session_num(subj_id, today, Path(subject_db_dir))
        else:
            session_num = int(raw.get('subject', {}).get('session_num', 1))
    session_num = int(session_num)
    session_id = f"{subj_id}_{today}_{session_num:03d}"

    # ── Reward ────────────────────────────────────────────────────────────
    # Pins and calibration come from rig JSON if available, session YAML
    # only has amount_ul (and optional label override).
    rew_raw = raw.get('reward', {})
    if rig and 'reward' in rig:
        rig_rew = rig['reward']
        pins_raw = rig_rew.get('pins', rew_raw.get('pins', {}))
        cal_raw  = rig_rew.get('calibration', rew_raw.get('calibration', {}))
    else:
        pins_raw = rew_raw.get('pins', {})
        cal_raw  = rew_raw.get('calibration', {})
    reward_pins = {n: RewardPin(**v) for n, v in pins_raw.items()}
    reward_cal  = {n: [tuple(r) for r in rows]
                   for n, rows in cal_raw.items()}
    reward = RewardConfig(amount_ul=rew_raw.get('amount_ul', 4.0),
                          pins=reward_pins, calibration=reward_cal)

    # ── Contrasts ─────────────────────────────────────────────────────────
    stim_raw = raw['stimulus']
    c = stim_raw['contrast']
    if isinstance(c, (int, float)):
        cv, cp = [float(c)], [1.0]
    else:
        cv = [float(x) for x in c['values']]
        cp = [float(x) for x in c['proportions']]
        s  = sum(cp); cp = [p / s for p in cp]

    # ── Lick ──────────────────────────────────────────────────────────────
    lick_raw = raw.get('lick', {})
    if rig and 'lick' in rig:
        rig_lick = rig['lick']
        lick_addr = rig_lick.get('i2c_address', lick_raw.get('i2c_address', '0x5A'))
        lick_elec = rig_lick.get('electrode', lick_raw.get('electrode', 4))
    else:
        lick_addr = lick_raw.get('i2c_address', '0x5A')
        lick_elec = lick_raw.get('electrode', 4)

    # ── Hardware toggles + camera/photodiode ──────────────────────────────
    hw_raw = raw.get('hardware', {})
    cam_raw = raw.get('camera', {})
    pd_raw  = raw.get('photodiode', {})

    # Camera resolution/fps: session YAML can override rig defaults
    if rig and 'camera' in rig:
        cam_res = cam_raw.get('camera_resolution',
                    cam_raw.get('resolution', rig['camera'].get('resolution', [1280, 720])))
        cam_fps = cam_raw.get('camera_fps',
                    cam_raw.get('fps', rig['camera'].get('fps', 50)))
    else:
        cam_res = cam_raw.get('camera_resolution',
                    cam_raw.get('resolution',
                      hw_raw.get('camera_resolution', [1280, 720])))
        cam_fps = cam_raw.get('camera_fps',
                    cam_raw.get('fps',
                      hw_raw.get('camera_fps', 50)))

    # Photodiode GPIO: from rig JSON or session YAML or old hardware section
    if rig and 'photodiode' in rig:
        pd_gpio = rig['photodiode'].get('gpio',
                    pd_raw.get('photodiode_gpio',
                      hw_raw.get('photodiode_gpio', 24)))
    else:
        pd_gpio = pd_raw.get('photodiode_gpio',
                    hw_raw.get('photodiode_gpio', 24))

    pd_n_frames = pd_raw.get('photodiode_pulse_every_n_frames',
                    hw_raw.get('photodiode_pulse_every_n_frames', 5))

    hardware = HardwareConfig(
        use_stim        = hw_raw.get('use_stim', True),
        use_reward      = hw_raw.get('use_reward', True),
        use_licks       = hw_raw.get('use_licks', True),
        use_camera      = hw_raw.get('use_camera', False),
        use_photodiode  = pd_raw.get('use_photodiode',
                            hw_raw.get('use_photodiode', False)),
        camera_resolution = cam_res,
        camera_fps        = cam_fps,
        photodiode_gpio   = pd_gpio,
        photodiode_pulse_every_n_frames = pd_n_frames,
    )

    # ── Network ───────────────────────────────────────────────────────────
    if rig and 'network' in rig:
        net = rig['network']
    elif 'network' in raw:
        net = raw['network']
    else:
        raise ValueError("No network config: provide rig JSON or network section in YAML")

    network = NetworkConfig(
        stim_ip      = net.get('stim_ip', ''),
        control_ip   = net.get('control_ip', ''),
        worker_port  = net.get('worker_port', 5570),
        monitor_port = net.get('monitor_port', 5571),
        control_port = net.get('control_port', 5572),
        flask_port   = net.get('flask_port', 5000),
        camera_port  = net.get('camera_port', 5001),
    )

    # ── Data paths ────────────────────────────────────────────────────────
    if rig and 'data' in rig:
        data_raw = rig['data']
    elif 'data' in raw:
        data_raw = raw['data']
    else:
        raise ValueError("No data config: provide rig JSON or data section in YAML")

    return ExperimentConfig(
        subject_id  = subj_id,
        session_id  = session_id,
        session_num = session_num,
        date        = today,
        notes       = raw.get('notes', ''),
        session  = SessionConfig(
            level          = float(raw['session']['level']),
            n_trials       = raw['session']['n_trials'],
            block_size     = raw['session'].get('block_size'),
            block_sequence = [float(x) for x in raw['session']['block_sequence']],
        ),
        stimulus = StimulusConfig(
            size_deg        = stim_raw['size_deg'],
            duration_s      = stim_raw['duration_s'],
            background_gray = stim_raw['background_gray'],
            altitude_deg    = stim_raw['altitude_deg'],
            contrast_values = cv,
            contrast_probs  = cp,
        ),
        timing = TimingConfig(
            iti_range_s       = raw['timing']['iti_range_s'],
            response_window_s = raw['timing']['response_window_s'],
            reward_delay_s    = raw['timing']['reward_delay_s'],
        ),
        lick = LickConfig(
            i2c_address   = int(lick_addr, 16)
                            if isinstance(lick_addr, str)
                            else lick_addr,
            electrode     = lick_elec,
            max_lick_rate = lick_raw.get('max_lick_rate', 0.3),
        ),
        adaptive = AdaptiveConfig(
            enabled       = raw['adaptive']['enabled'],
            initial_state = raw['adaptive'].get('initial_state', 0.0),
            step_up       = raw['adaptive'].get('step_up', 0.2),
            step_down     = raw['adaptive'].get('step_down', 0.2),
        ),
        hardware = hardware,
        network  = network,
        data     = DataConfig(**data_raw),
        reward   = reward,
    )


# ── Subject database ───────────────────────────────────────────────────────────

def _next_session_num(subject_id, today, db_dir):
    db_file = Path(db_dir) / f"{subject_id}.json"
    if not db_file.exists():
        return 1
    with open(db_file) as f:
        db = json.load(f)
    same_day = [s for s in db.get('sessions', [])
                if s['date'].replace('-', '') == today]
    return len(same_day) + 1


def register_session(config: ExperimentConfig,
                     db_dir: Union[str, Path],
                     n_trials_completed: int = 0,
                     notes: str = ""):
    db_dir  = Path(db_dir)
    db_dir.mkdir(parents=True, exist_ok=True)
    db_file = db_dir / f"{config.subject_id}.json"
    if db_file.exists():
        with open(db_file) as f:
            db = json.load(f)
    else:
        db = {'subject_id': config.subject_id,
              'created': date.today().isoformat(),
              'sessions': []}
    db['sessions'].append({
        'session_id':         config.session_id,
        'date':               f"{config.date[:4]}-{config.date[4:6]}-{config.date[6:]}",
        'session_num':        config.session_num,
        'level':              config.session.level,
        'n_trials_planned':   config.session.n_trials,
        'n_trials_completed': n_trials_completed,
        'block_sequence':     config.session.block_sequence,
        'stim_size_deg':      config.stimulus.size_deg,
        'contrast_values':    config.stimulus.contrast_values,
        'notes':              notes,
        'timestamp':          datetime.now().isoformat(),
    })
    with open(db_file, 'w') as f:
        json.dump(db, f, indent=2)


def get_subject_history(subject_id, db_dir):
    db_file = Path(db_dir) / f"{subject_id}.json"
    if not db_file.exists():
        return []
    with open(db_file) as f:
        return json.load(f).get('sessions', [])
