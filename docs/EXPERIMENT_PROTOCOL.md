# Experiment Setup and Running Protocol

**Rig:** mozzarella (stimulus Pi) + cheddar (hardware Pi) + Mac (control)  
**Last updated:** 2026-03-30

---

## Overview

```
Mac (setup.py + Flask UI)
  ↕ SSH + ZMQ
mozzarella (task.py — PsychoPy stimuli, trial loop)
  ↕ ZMQ
cheddar (worker.py — licks, reward, camera, [photodiode])
```

All timing is synchronized via NTP (chrony). All data is saved locally on each
machine and transferred to the Mac at session end.

---

## One-Time Setup (do once per rig)

### 1. NTP clock synchronization

All machines must share a common clock. Run once when the rig is first set up,
and again after any major reconfiguration.

**On Mac** (enable NTP server):
```bash
# Check NTP is running
sudo systemsetup -getnetworktimeserver
# Should return a time server. If not:
sudo systemsetup -setnetworktimeserver time.apple.com
sudo systemsetup -setusingnetworktime on
```

**On each Pi** (sync to Mac):
```bash
sudo apt install chrony -y
sudo tee /etc/chrony/sources.d/mac.conf > /dev/null << 'EOF'
server 192.168.10.1 iburst prefer minpoll 4 maxpoll 4
EOF
sudo systemctl restart chrony
# Wait ~30 seconds, then check:
chronyc tracking
# "System time" should be <1ms
```

### 2. Passwordless SSH from Mac to Pis

Required for the Connect buttons to work automatically.

```bash
# On Mac (run once per Pi):
ssh-keygen -t ed25519   # if you don't have a key yet
ssh-copy-id vruser@192.168.10.101   # cheddar
ssh-copy-id vruser@192.168.10.102   # mozzarella

# Test:
ssh vruser@192.168.10.101 echo "cheddar OK"
ssh vruser@192.168.10.102 echo "mozzarella OK"
```

### 3. Install dependencies

**Mac:**
```bash
conda activate autopilot
pip install flask paramiko pyzmq scipy matplotlib h5py pyyaml
```

**mozzarella** (`conda activate rig`):
```bash
pip install pyzmq h5py pyyaml psychopy numpy scipy
```

**cheddar** (`conda activate autopilot`):
```bash
pip install pyzmq smbus2 pigpio pyyaml numpy flask picamera2
sudo pigpiod   # start GPIO daemon
```

### 4. SSD for video (cheddar)

```bash
# On cheddar: find the SSD
lsblk
# Mount it (replace sda1 with your device)
sudo mkdir -p /media/vruser/ssd
sudo mount /dev/sda1 /media/vruser/ssd
# For auto-mount on boot, add to /etc/fstab:
echo "/dev/sda1 /media/vruser/ssd ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
```

### 5. Screen calibration

Run once when the rig is built. See `calibration/CALIBRATION_PROTOCOL.md`.

```bash
# On mozzarella:
conda activate rig
cd ~/rig/calibration
python compute_warp_map.py --validate   # generates warp_map.npz
DISPLAY=:0 python validate_calibration.py  # visually verify on projector
```

---

## Before Each Session

### 1. Check hardware

- [ ] Ethernet switch powered on, all devices connected (check LEDs on switch)
- [ ] Projector on and warmed up (≥15 min for stable brightness)
- [ ] Water reservoir filled and tubing primed (no air bubbles)
- [ ] Lick spout positioned correctly
- [ ] Camera positioned and focused (check preview in UI)
- [ ] Mouse weighed and recorded
- [ ] Room lights set to experiment condition

### 2. Start pigpiod on cheddar

```bash
ssh vruser@192.168.10.101 "sudo pigpiod"
```

Or it starts automatically if you added a boot script.

### 3. Write the session config file

Copy the template and edit for today:

```bash
cp ~/rig/config/HK001_day07.yaml ~/rig/config/HK001_day08.yaml
```

Edit the key fields:
```yaml
notes: "Day 8. Mouse at 87% body weight."
session:
  level: 2.5
  n_trials: 150
  block_sequence: [-80, 40, -40, 80, -80, 40]   # alternate sequence
stimulus:
  size_deg: 8.0
  contrast:
    values: [0.25, 0.15, 0.125]
    proportions: [0.33, 0.33, 0.33]
timing:
  iti_range_s: [8, 16]
lick:
  max_lick_rate: 0.3
reward:
  amount_ul: 4.0
```

---

## Running an Experiment

### Step 1 — Launch the UI

On your Mac:

```bash
conda activate autopilot
cd ~/rig
python mac/setup.py --config config/HK001_day08.yaml
```

A browser window opens automatically at `http://localhost:5000`.

### Step 2 — Load config

The config path is pre-filled. Click **Load Config**.

Check the session summary panel:
- Subject ID and auto-generated session ID (e.g. `HK001_20260330_001`)
- Level, n_trials, block sequence
- Stimulus size and contrast
- **Reward calibration plot** — verify the computed pulse duration looks
  reasonable (it should sit cleanly on the calibration curve)

### Step 3 — Connect to Pis

Click **Connect mozzarella** then **Connect cheddar**.

Each button turns green when connected. The process:
1. SSH into the Pi
2. Transfers the config and Python files
3. Starts the task/worker process remotely
4. mozzarella: generates stimuli, opens PsychoPy window, waits for GO
5. cheddar: initialises MPR121, solenoid, camera preview, waits for commands

**If a connection fails:** check that pigpiod is running on cheddar, and that
SSH keys are set up (see One-Time Setup).

### Step 4 — Verify hardware

Once both Pis are connected:

- **Camera stream** — appears in the main panel. Check mouse is in frame and
  image is focused. Adjust camera if needed.
- **Deliver Reward** button — click to fire a test reward. Listen/watch for
  the solenoid click and water drop. Adjust pulse duration in config if volume
  is wrong.
- **Lick raster** — have someone touch the lick spout; a blue tick should
  appear in the raster panel.

### Step 5 — Place mouse and GO

1. Place the mouse on the ball
2. Allow 30–60 seconds for it to settle
3. Click **GO — Start Experiment**

The experiment starts immediately. The trial loop runs entirely on mozzarella.

### Step 6 — Monitor

During the session, watch:

| Indicator | What it means |
|---|---|
| **STIM** (yellow) | Stimulus is on screen |
| **REWARD** (green) | Solenoid just fired |
| Blue ticks (lick raster) | Lick events in last 5 seconds |
| Trial counter | Running trial count |
| Hit rate | % trials with lick in response window |
| Block | Current block number |
| Adaptive | Current adaptive state (level 2.5 only) |

**To abort early:** Click **End Session** — this stops the trial loop cleanly
and saves all completed trials. Do NOT close the browser or kill the terminal.

### Step 7 — End session and transfer data

Click **End Session & Transfer Data**.

This will:
1. Send STOP to mozzarella → trial loop ends, HDF5 flushed and closed
2. Stop camera recording on cheddar → frame timestamps saved
3. Register session in subject database
4. `rsync` stimuli from mozzarella → Mac
5. `rsync` video + timestamps from cheddar → Mac

Transfer of ~600 MB takes ~6 seconds on the gigabit switch.

**Data lands at:**
```
~/experiment_data/
  HK001/
    HK001_20260330_001.h5        ← trial data (behavior)
    stims/
      HK001_20260330_001/
        stimuli.npz              ← pre-generated stimulus parameters
    video/
      HK001_20260330_001/
        video.h264               ← behavior video
        frame_timestamps.npy     ← (frame_idx, unix_timestamp) pairs
```

Subject history is at `~/experiment_data/subjects/HK001.json`.

---

## Data Format Reference

### HDF5 trial data (`HK001_20260330_001.h5`)

Open in Python:
```python
import h5py, numpy as np

with h5py.File('HK001_20260330_001.h5', 'r') as f:
    # Session metadata
    print(dict(f.attrs))

    # Per-trial arrays
    stim_onset  = f['stim_onset_t'][:]     # Unix timestamps (float64)
    reward_t    = f['reward_t'][:]          # NaN if no reward
    lick_times  = f['lick_times'][:]        # variable-length arrays, rel. to stim onset
    outcomes    = f['trial_outcome'][:]     # 'hit', 'miss', 'abort'
    azimuths    = f['stim_az_deg'][:]
    contrasts   = f['contrast'][:]
    blocks      = f['block_num'][:]
    adaptive    = f['adaptive_state'][:]    # NaN if not level 2.5
```

All timestamps are Unix time (`time.time()`) in seconds, synchronized across
machines via NTP. Lick times and sync pulse times are relative to `stim_onset_t`.

### Frame timestamps (`frame_timestamps.npy`)

```python
import numpy as np
frames = np.load('frame_timestamps.npy')
# Shape: (N_frames, 2)
# Column 0: frame index (0-based)
# Column 1: Unix timestamp of frame capture
frame_idx = frames[:, 0].astype(int)
frame_t   = frames[:, 1]
```

### Sync pulses (if photodiode enabled)

Stored as a dataset in the HDF5 file:
```python
with h5py.File('...h5', 'r') as f:
    pulses = f['sync_pulses'][:]   # (pulse_idx, unix_timestamp) per trial
```

`pulse_idx=1` is the first stimulus frame. Subsequent pulses are every N frames
(set by `photodiode_pulse_every_n_frames` in config).

---

## Timing Reference

| Event | Timestamp source | Precision |
|---|---|---|
| Stimulus onset | PsychoPy `win.flip()` return value | ±20ms (one frame period) |
| Lick onset | pigpio hardware callback on cheddar | ±0.1ms |
| Reward delivery | `time.time()` on cheddar after solenoid fires | ±0.5ms |
| Camera frames | `time.time()` in picamera2 callback | ±1ms |
| Sync pulses (photodiode) | pigpio hardware tick, NTP-referenced | ±0.1ms |

All timestamps are in the same reference frame (NTP-synced Unix time).

---

## Troubleshooting

**Connect button fails immediately:**
- Check SSH keys: `ssh vruser@192.168.10.101 echo ok`
- Check Pi is on the network: `ping 192.168.10.101`
- Check pigpiod is running: `ssh vruser@192.168.10.101 pgrep pigpiod`

**cheddar shows READY but no licks detected:**
- Check MPR121 is on I2C: `ssh vruser@192.168.10.101 i2cdetect -y 1` — should show `5a`
- Check electrode number in config matches physical wiring

**Reward not firing:**
- Check solenoid wiring to GPIO18
- Test with Deliver Reward button in UI
- Check pulse duration: `gpio readall` on cheddar

**PsychoPy window not opening on mozzarella:**
- Check `DISPLAY=:0` is set in the SSH command
- Check projector is on and connected
- SSH into mozzarella and check: `DISPLAY=:0 python -c "from psychopy import visual"`

**Video has dropped frames:**
- Check SSD is mounted: `df -h` on cheddar
- Reduce frame rate in config (`camera_fps: 30`)
- Check SSD write speed: `dd if=/dev/zero of=/media/vruser/ssd/test bs=1M count=200`

**Data transfer fails:**
- Check rsync is installed: `which rsync`
- Check destination directory exists and is writable
- Transfer manually: `rsync -avz vruser@192.168.10.101:/home/vruser/autopilot/data/ ~/experiment_data/`

---

## Adding the Photodiode (future)

When you have the photodiode hardware ready:

1. Wire photodiode output to GPIO24 on cheddar (or change `photodiode_gpio` in config)
2. Tape photodiode to the sync patch corner of the projector screen
3. In config, set: `use_photodiode: true`
4. Verify pulses in the event log during a test trial

The sync patch is drawn by mozzarella/task.py in the top-right corner of the
projector image on the first stimulus frame and every 5th frame thereafter
(configurable: `photodiode_pulse_every_n_frames`).

---

## Config Quick Reference

```yaml
session:
  level:           # 1, 2, 2.5, 3
  n_trials:        # total trials
  block_size:      # trials per location block
  block_sequence:  # azimuths in degrees, e.g. [80, -40, 40, -80]

stimulus:
  size_deg:        # visual angle of square
  contrast:        # single value or {values: [...], proportions: [...]}
  background_gray: # -1 (black) to 1 (white), 0 = mid-gray

timing:
  iti_range_s:     # [min, max] baseline duration
  response_window_s:
  reward_delay_s:  # free reward delay after stim onset

lick:
  max_lick_rate:   # licks/sec threshold during baseline

reward:
  amount_ul:       # target volume
  # calibration table determines pulse duration automatically

hardware:
  use_camera: true/false
  use_photodiode: true/false   # enable when hardware connected
  camera_fps: 50
```
