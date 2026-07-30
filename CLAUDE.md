# VRFarm — Claude Code Handoff

This document is for Claude Code to pick up the VRFarm project.
Read this entire file before touching anything.

---

## What is VRFarm

A behavioral neuroscience experiment system for mice. Two Raspberry Pi 4Bs
per rig (Leader + Follower), controlled from a Mac via Flask web UIs.
Paradigms are tuned via task YAML (stimulus/reward/session/adaptive params); the trial
engine is the imperative loop in engine/leader.py. Devices are pluggable.
Named after cheese.

**Current rig:** cheese (cheddar = Leader, mozzarella = Follower)
**Mac:** balthazar (hakan@balthazar), conda env `vrfarm`, Python 3.11
**Both Pis:** Debian 13 (trixie), conda env `rig`, user `vruser`. The `rig` env
Python **must match the system Python** (3.13 on trixie) — the camera bindings
(`python3-libcamera`/`python3-picamera2`) are apt-built for the system Python and are
symlinked into the env, so a version mismatch breaks `import picamera2`. Create with
`conda create -n rig python=$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')`.

---

## Project structure

```
~/VRFarm/                              <- project root on Mac
├── config/
│   └── go_nogo_v1.yaml               <- task/paradigm config (stimulus/reward/session/adaptive)
├── rigs/
│   └── cheese.json                    <- rig hardware config (pins, cal, ports, roles)
├── devices/
│   ├── base.py                        <- Device base class, IOType, DEVICE_REGISTRY
│   ├── lick_sensor.py                 <- MPR121 capacitive touch (I2C)
│   ├── reward.py                      <- Reward valve (GPIO output)
│   ├── reward_calibration.py          <- Reward pulse-volume calibration routine
│   ├── camera.py                      <- picamera2 (CSI)
│   ├── photodiode.py                  <- TTL sync input (GPIO input)
│   └── display.py                     <- pygame fullscreen renderer (HDMI)
├── engine/
│   ├── leader.py                      <- Leader Pi main process (imperative trial loop)
│   └── follower.py                    <- Follower Pi main process
├── app/
│   ├── app.py                         <- Flask experiment UI localhost:5000
│   └── templates/
│       └── experiment.html            <- Experiment dashboard
├── setup/
│   ├── app.py                         <- Flask rig setup UI localhost:4999
│   └── templates/
│       └── setup.html                 <- Setup dashboard
├── pi_api/
│   ├── api.py                         <- Flask REST API (runs on each Pi, port 5080)
│   └── vrfarm.service                 <- systemd unit file
├── shared/
│   ├── config.py                      <- Config loaders + subject database
│   └── stim_generator.py              <- Pre-compute stimuli (NPZ output)
├── display_calibration/               <- Display/projector calibration scripts
│   ├── compute_warp_map.py
│   ├── rig_geometry.yaml
│   ├── display_test_patches.py
│   ├── fit_luminance_correction.py
│   └── validate_calibration.py
├── data/
│   └── subjects/                      <- Session history JSONs (auto-created)
└── docs/
    ├── FUTURE_WORK.md
    ├── INITIAL_SETUP_REFERENCE.md
    ├── EXPERIMENT_PROTOCOL.md
    ├── CALIBRATION_PROTOCOL.md
    └── AUDIT_20260331.md
```

**On cheddar (Leader Pi, 192.168.10.101):**
```
~/rig/                                 <- engine/leader.py, devices/*.py, shared/*.py
~/rig/pi_api/api.py                    <- REST API
~/data/<session_id>/                   <- HDF5 + metadata (local, transferred after)
/media/vruser/ssd/video/               <- video recordings (SSD)
```

**On mozzarella (Follower Pi, 192.168.10.102):**
```
~/rig/                                 <- engine/follower.py, devices/display.py
~/rig/pi_api/api.py                    <- REST API
~/rig/stims/<session_id>/stimuli.npz   <- pre-generated stim params
```

---

## Network

```
Gigabit ethernet switch (experiment traffic)
├── Mac balthazar     192.168.10.1   (en5)
├── cheddar           192.168.10.101 (eth0 static) — Leader
└── mozzarella        192.168.10.102 (eth0 static) — Follower

All Pis also on institute WiFi (wlan0) for internet/NTP.
```

SSH keys: Mac `~/.ssh/id_rsa.pub` copied to both Pis. Passwordless SSH works.

---

## Architecture

```
Mac (app/app.py Flask, localhost:5000)
  ↔ REST API (HTTP, port 5080) for deploy, config, start/stop, data transfer
  ← UDP :5571 events from Leader (trial, lick, reward, stim, sync)
  → UDP :5572 commands to Leader (START, STOP, REWARD)

cheddar — Leader (engine/leader.py)
  - Imperative trial loop (ITI → pre-stim → stim → reward-delay → response window → post-stim)
  - All GPIO devices: lick sensor, reward, camera, photodiode
  - HDF5 data saved locally, transferred to Mac after session
  → UDP :5575 display commands to Follower (SHOW, QUIT)
  → UDP :5571 events to Mac

mozzarella — Follower (engine/follower.py)
  - pygame display (HDMI/DPI)
  - Loads pre-generated stim NPZ at session start
  - On SHOW: look up trial params, render, wait duration, blank
  ← UDP :5575 commands from Leader
```

**Key design:** Leader sends `{"cmd": "SHOW", "trial": N}` — Follower handles
the full show->duration->blank cycle from NPZ data.

**Real-time:** All inter-Pi communication uses Python `socket` module (UDP
datagrams, ~0.1ms on local gigabit). No ZMQ dependency.

**Management:** Flask REST API on each Pi (port 5080) for file upload/download,
process start/stop, stim generation. Replaces paramiko SSH.

---

## Config system

Two config files with clean separation:

| What                          | Where              | Example                              |
|-------------------------------|--------------------|--------------------------------------|
| Pin assignments, I2C addr     | Rig JSON           | `"gpio": 18`, `"i2c_address": "0x5A"` |
| Calibration tables            | Rig JSON           | `"calibration": [[10,2.1],...]`       |
| Pi roles, IPs                 | Rig JSON           | `"role": "leader"`                    |
| Paradigm / trial params       | Task YAML          | stimulus/reward/session/adaptive sections |
| Experiment-tunable params     | Task YAML `reward` | `amount_ul: 4.0`, `max_lick_rate: 0.3`|
| Subject, date, session#       | Runtime (UI)       | set in experiment UI fields          |

## Device abstraction

Adding a new device = one file in `devices/`. Each device subclasses `Device`
from `devices/base.py` and declares:
- `info`: DeviceInfo (name, label, IOType, required_packages)
- `init(rig_config, task_params)`: hardware init
- `check()`: health check
- `task_params_schema()`: experiment-tunable params (editable in UI)
- `hdf5_datasets()` / `hdf5_trial_data()`: per-trial data saving
- `start_stream(callback)` / `stop_stream()`: live data
- `needs_calibration` / `calibrate()` / `load_calibration()`: optional

`@register_device` decorator adds the class to `DEVICE_REGISTRY`.

---

## Experiment workflow

Setup -> Connect -> **Deploy** -> Running -> Ended -> Transfer

- **Deploy** is required before Go: uploads configs, generates stims on Leader,
  pushes NPZ to Follower, renders thumbnails on Mac.
- Any parameter change in UI invalidates deploy (Go grays out).

---

## Packages

**Mac (conda env vrfarm):**
Conda base at `/opt/homebrew/Caskroom/miniforge/base`.
```bash
conda activate vrfarm
pip install flask requests scipy matplotlib numpy h5py pyyaml
```
No longer needed: `paramiko`, `pyzmq` (replaced by REST API + UDP).

**Cheddar / Leader (conda env rig):**
```bash
pip install flask pyyaml numpy scipy h5py smbus2 pigpio
```
Optional: `picamera2` (camera, enable in rig JSON)

**Mozzarella / Follower (conda env rig):**
```bash
pip install flask pyyaml numpy pygame
```

pigpiod daemon: on trixie the `pigpio` apt package is gone, so the daemon is built
from source — already installed at `/usr/local/bin/pigpiod` with a unit at
`/etc/systemd/system/pigpiod.service` (enabled). The Python client is `pip install
pigpio` (in the `rig` env). pigpiod must be running: it's a service now
(`systemctl status pigpiod`), or `sudo pigpiod` manually.

**Running the UIs:**
```bash
conda activate vrfarm
python app/app.py          # experiment UI, localhost:5000
python setup/app.py        # rig setup UI, localhost:4999
```

---

## Known issues / gotchas

- `conda` not in PATH for non-interactive SSH — use
  `source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig`
- macOS port 5000 taken by AirPlay Receiver — disable in System Settings
  -> General -> AirDrop & Handoff -> AirPlay Receiver off
- pigpio `sudo make install` fails on its Python step (distutils missing) —
  harmless, C library + `pigpiod` install fine; use `pip install pigpio` for the client
- cheddar is on a tiny ~8 GB SD card (often >90% full) — reclaim with
  `conda clean -a -y` / `pip cache purge`; a larger card is needed for sustained use
- Pis are firewall-gated off the `public` WiFi (associated but no egress). To install
  packages, run an HTTP proxy on the Mac (`python -m proxy --hostname 192.168.10.1
  --port 8899`) and set `HTTPS_PROXY=http://192.168.10.1:8899` on the Pi
- Projector startup sequence needed on mozzarella before display:
  `~/rig/start_projector.sh` (sets GPIO ALT2, GPIO25 high, starts X :0)
- Warp map not yet generated — stim_generator uses linear approximation fallback

---

## Next steps

1. Deploy new code to both Pis via setup UI (setup/app.py localhost:4999)
2. Install systemd service on each Pi (pi_api/vrfarm.service)
3. Test Connect in experiment UI — verify both Pis respond on REST API
4. Deploy experiment, run 5-trial session (camera off, photodiode off)
5. Test lick -> reward latency (should be <1ms, same-Pi GPIO)
6. Generate warp map via display_calibration/
7. Run full session with correct stimulus positions

---

## Style / conventions

- Flask SSE for real-time event streaming to browser
- UDP datagrams for all real-time Pi communication
- REST API (Flask on each Pi) for management operations
- systemd services for Pi process lifecycle
- HDF5 for trial data, written incrementally per trial on Leader
- All timestamps: `time.time()` Unix seconds, NTP-synced
- Config: rig JSON (hardware) + task YAML (paradigm params)
- Trial engine: imperative loop in engine/leader.py, tuned by task-YAML params
- Device abstraction: base class + one file per device type
