# VRFarm — Claude Code Handoff

This document is for Claude Code to pick up the VRFarm project.
Read this entire file before touching anything.

---

## What is VRFarm

A behavioral neuroscience experiment system for mice. Two Raspberry Pi 4Bs
per rig, controlled from a Mac via Flask web UIs. Named after cheese.

**Current rig:** cheddar (control Pi) + mozzarella (stim Pi)  
**Mac:** balthazar (hakan@balthazar), conda env `vrfarm`, Python 3.11  
**Both Pis:** conda env `rig`, Python 3.11, user `vruser`

---

## Project structure

```
~/VRFarm/                          ← project root on Mac
├── config/
│   └── HK001_day07.yaml          ← session config (references rig by name)
├── rig_setup/
│   ├── rig_setup_ui.py           ← Flask UI localhost:4999 — one-time Pi setup
│   ├── rig_cheese.json           ← full rig hardware config (pins, cal, ports)
│   └── templates/
│       └── rig_setup.html        ← setup UI HTML/JS
├── experiment/
│   ├── experiment_ui.py          ← Flask UI localhost:5000 — run experiments
│   └── templates/
│       └── experiment.html       ← experiment UI HTML/JS
├── shared/
│   └── protocol.py               ← config dataclass, copied to both Pis
├── stim/
│   ├── task.py                   ← trial loop + PsychoPy (deployed to mozzarella)
│   ├── stim_generator.py         ← pre-renders stimuli (deployed to mozzarella)
│   └── STIM_NOTES.md             ← projector/PsychoPy/stimulus docs
├── control/
│   ├── worker.py                 ← lick/reward/camera (deployed to cheddar)
│   └── CONTROL_NOTES.md          ← GPIO/lick/reward/camera docs
├── calibration/
│   ├── compute_warp_map.py       ← generates warp_map.npz
│   ├── rig_geometry.yaml         ← screen physical measurements
│   ├── display_test_patches.py   ← photometer measurement tool
│   ├── fit_luminance_correction.py
│   └── validate_calibration.py
├── data/
│   └── subjects/                 ← HK001.json session history (auto-created)
├── docs/
│   ├── FUTURE_WORK.md            ← deferred items tracker
│   ├── INITIAL_SETUP_REFERENCE.md← environment setup reference
│   ├── EXPERIMENT_PROTOCOL.md    ← experiment design & data format
│   ├── CALIBRATION_PROTOCOL.md   ← screen calibration steps
│   └── AUDIT_20260331.md         ← codebase audit
└── rig.json                      ← Pi connection info (minimal, saved by rig_setup_ui.py)
```

**On mozzarella (stim Pi, 192.168.10.102):**
```
~/rig/                            ← task.py, stim_generator.py, protocol.py
~/rig/calibration/                ← compute_warp_map.py, rig_geometry.yaml etc
~/stims/                          ← pre-generated stimuli per session
```

**On cheddar (control Pi, 192.168.10.101):**
```
~/rig/                            ← worker.py, protocol.py
/media/vruser/ssd/video/          ← video recordings (SSD mounted here)
```

---

## Network

```
Gigabit ethernet switch (experiment traffic)
├── Mac balthazar     192.168.10.1   (en5)
├── mozzarella        192.168.10.102 (eth0 static)
└── cheddar           192.168.10.101 (eth0 static)

All Pis also on institute WiFi (wlan0) for internet/NTP.
```

SSH keys: Mac `~/.ssh/id_rsa.pub` copied to both Pis. Passwordless SSH works.
```bash
ssh vruser@192.168.10.101 echo "cheddar OK"   # works
ssh vruser@192.168.10.102 echo "mozzarella OK" # works
```

---

## Architecture

```
Mac (experiment_ui.py Flask)
  ↕ SSH + paramiko (deploy files, start processes)
  ↕ ZMQ SUB (receives trial events for dashboard)
  ↕ ZMQ PUSH (sends GO/STOP/REWARD commands)

mozzarella (task.py)
  - PsychoPy window (DISPLAY=:0)
  - Trial state machine
  - ZMQ ROUTER (port 5570) ← cheddar connects here
  - ZMQ PUB (port 5571) → Mac monitor subscribes
  - ZMQ PULL (port 5581) ← Mac sends commands

cheddar (worker.py)
  - MPR121 lick detection (I2C 0x5A, electrode 4, 200Hz polling)
  - Solenoid reward (GPIO18, pigpio)
  - Camera (picamera2, optional)
  - Photodiode TTL input (GPIO24, optional, disabled for now)
  - ZMQ DEALER → mozzarella:5570
```

**Data flow:**
- Lick event: cheddar → ZMQ → mozzarella → ZMQ PUB → Mac dashboard
- Reward command: Mac → ZMQ PUSH → mozzarella → ZMQ ROUTER → cheddar
- Trial data: written to HDF5 on Mac incrementally per trial
- Video: saved to cheddar SSD, rsync'd to Mac at session end
- Stimuli: pre-generated on mozzarella, rsync'd to Mac at session end

---

## Current status (as of 2026-03-30)

### Done ✓
- Both Pis set up: conda env `rig`, all packages installed
- pigpio built from source on cheddar, daemon running
- Passwordless SSH from Mac to both Pis
- NTP: both Pis syncing to internet Stratum 1-2 servers (~6µs accuracy)
- `rig_setup_ui.py` working: both Pis green, files deployed
- `experiment_ui.py` running, mozzarella connects successfully
- Config loads, mozzarella SSH deploy works

### Broken / not yet tested
1. **Cheddar SSH fails from paramiko** — system `ssh` works fine but
   paramiko times out connecting to 192.168.10.101. Increased timeout
   in latest `experiment_ui.py` but not yet retested. Try:
   ```bash
   ssh-keyscan 192.168.10.101 >> ~/.ssh/known_hosts
   ```
   Then retest the Connect control Pi button.

2. **Config reload after editing YAML** — ✓ FIXED. Config loading now
   resolves absolute paths and auto-discovers rig JSON from the `rig.name`
   field in the YAML config (e.g. `rig.name: "cheese"` → `rig_setup/rig_cheese.json`).

3. **Reward delivery not confirmed** — reward button sends ZMQ to
   mozzarella which forwards to cheddar. If cheddar worker.py isn't
   running, nothing fires. Fix cheddar connect first.

4. **Lick detection not tested** — MPR121 not yet verified working.
   Once cheddar connects, touch the lick spout and check the raster.

5. **Warp map not generated** — `warp_map.npz` doesn't exist on
   mozzarella yet. task.py has a fallback (center stimulus) so it won't
   crash, but stimuli won't be at correct screen positions. Generate via
   the "Generate warp map" button in rig_setup_ui.py once basic flow works.

6. **PsychoPy window** ✓ — confirmed working. V3D GPU (hardware), 24-bit visuals.
   Requires projector startup sequence before task.py: run `~/rig/start_projector.sh`
   (sets GPIO ALT2, GPIO25 high, runs init_parallel_mode.py, starts X :0).

---

## Key config file

`config/HK001_day07.yaml` — edit this per session.
Key fields:
```yaml
network:
  stim_ip: "192.168.10.102"
  control_ip: "192.168.10.101"
hardware:
  use_camera: false      # keep false until basic flow works
  use_photodiode: false  # keep false, hardware not connected yet
```

---

## Packages

**Mac (conda env vrfarm):**
```
flask paramiko pyzmq scipy matplotlib numpy h5py pyyaml
```

**Mozzarella (conda env rig):**
```
pyzmq h5py pyyaml flask numpy scipy
psychopy (installed --no-deps + pyglet==1.5.27 pillow moviepy imageio
          imageio-ffmpeg pyopengl requests packaging psutil six json_tricks
          pandas pyserial python-bidi arabic-reshaper freetype-py)
```
Note: pyglet MUST be 1.5.27 — 2.x fails to create GL context under FKMS+DPI.

**Cheddar (conda env rig):**
```
pyzmq smbus2 pyyaml flask numpy picamera2
pigpio (built from source: ~/pigpio/, then pip install pigpio)
```

**System packages:**
- mozzarella: `libx11-dev libxext-dev libxi-dev xserver-xorg-core libgl1-mesa-dri libglu1-mesa mesa-utils`
- cheddar: `libcap-dev`

---

## Immediate next steps

1. ✓ Cheddar: paramiko SSH, worker.py, GPIO solenoid, MPR121 lick — all working
2. ✓ Mozzarella: PsychoPy window opens, V3D GPU confirmed
3. Run start_projector.sh on mozzarella before deploying task.py
4. Connect experiment_ui.py to mozzarella — deploy task.py via SSH and verify it starts
5. Test lick events appearing in UI raster (cheddar→mozzarella ZMQ→Mac)
6. Run 5-trial session (camera off, photodiode off, warp fallback ok)
7. Generate warp map via rig_setup_ui.py
8. Run proper session with correct stimulus positions

---

## Known issues / gotchas

- `conda` not in PATH for non-interactive SSH — fixed in rig_setup_ui.py
  by using `source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig`
- Same fix needed anywhere else that SSHes and runs Python
- macOS port 5000 taken by AirPlay Receiver — disable in System Settings
  → General → AirDrop & Handoff → AirPlay Receiver off
- psychtoolbox fails to build on aarch64 — install psychopy with
  `--no-deps` and add deps manually (already done)
- pigpio `sudo make install` fails on Python step (distutils missing in
  3.11) — harmless, C library installed fine, use `pip install pigpio`

---

## Docs

- `docs/SETUP_REFERENCE.md` — full environment setup, all commands
- `docs/FUTURE_WORK.md` — deferred items (multi-rig, Mac NTP, etc.)
- `calibration/CALIBRATION_PROTOCOL.md` — screen calibration steps

---

## Style / conventions

- Flask SSE for real-time log streaming to browser
- ZMQ for inter-Pi communication (ROUTER/DEALER + PUB/SUB)
- HDF5 for trial data, written incrementally (one row per trial, flushed)
- All timestamps: `time.time()` Unix seconds, NTP-synced
- Config: YAML → `shared/protocol.py` dataclasses
- Pi code deployed via paramiko SFTP, started via SSH nohup
- Logs go to `/tmp/stim.log` (mozzarella) and `/tmp/control.log` (cheddar)
