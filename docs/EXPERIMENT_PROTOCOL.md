# Experiment Setup and Running Protocol

**Rig:** cheddar (Leader Pi) + mozzarella (Follower Pi) + Mac/Ubuntu (UI)
**Last updated:** 2026-08-03

---

## Overview

```
Controller (app/app.py Flask, localhost:5000)
  ↔ REST API (HTTP, port 5080) — deploy, config, start/stop, data transfer
  ← UDP :5571 — events from Leader (trial, lick, reward, stim, sync)
  → UDP :5572 — commands to Leader (START, STOP, REWARD)

cheddar — Leader (engine/leader.py)
  - Imperative trial loop + all GPIO devices (lick, reward, camera, photodiode)
  → UDP :5575 — display commands to Follower

mozzarella — Follower (engine/follower.py)
  - pygame display, renders from pre-generated stim NPZ
```

Ports are set in the rig config (`rigs/cheese.yaml`, `network:` block). Each trial
runs the imperative loop in `engine/leader.py`:
**ITI → pre-stim → stim onset → response delay → response window → post-stim → outcome.**
All timing synchronized via NTP (chrony). Data is saved locally on the Leader Pi and
transferred to the controller after the session ends.

---

## One-Time Setup (do once per rig)

### 1. NTP clock synchronization

All machines must share a common clock.

**On each Pi** (sync to internet NTP):
```bash
sudo apt install chrony -y
chronyc tracking
# "System time" should be <1ms
```

### 2. Passwordless SSH from controller to Pis

Required for initial Pi setup. After that, the REST API is used.

```bash
ssh-keygen -t ed25519   # if you don't have a key yet
ssh-copy-id vruser@192.168.10.101   # cheddar
ssh-copy-id vruser@192.168.10.102   # mozzarella

# Test:
ssh vruser@192.168.10.101 echo "cheddar OK"
ssh vruser@192.168.10.102 echo "mozzarella OK"
```

### 3. Install dependencies

**Controller** (`conda activate vrfarm`):
```bash
pip install flask requests scipy matplotlib numpy h5py pyyaml
```

**cheddar / Leader** (`conda activate rig`):
```bash
pip install flask pyyaml numpy scipy h5py smbus2 pigpio
```
Optional: `picamera2` (camera, enable in rig config).

**mozzarella / Follower** (`conda activate rig`):
```bash
pip install flask pyyaml numpy pygame
```

Both Pis run Debian trixie. The `rig` conda env's Python **must match the system
Python** (3.13) — the camera bindings are apt-built for the system Python and symlinked
into the env, so a version mismatch breaks `import picamera2`. On trixie the `pigpio`
apt package is gone: `pigpiod` is built from source (installed at `/usr/local/bin/pigpiod`
with a systemd unit at `/etc/systemd/system/pigpiod.service`, enabled); the Python client
is `pip install pigpio`.

### 4. Deploy code and install systemd service

Use the rig setup UI:
```bash
conda activate vrfarm
python setup/app.py    # localhost:4999
```

Deploy code to each Pi and install the `pi_api/vrfarm.service` systemd unit
(`restart=always`), so the REST API on port 5080 comes up on boot. (Code is also
re-uploaded automatically on every **Deploy** in the experiment UI — see below.)

### 5. SSD for video (cheddar, optional)

Camera video is written to the path in the rig config (`data.video_dir`). Point it at a
mounted SSD when recording sustained sessions — the Leader's SD card is too small to hold
video.

```bash
# On cheddar:
sudo mkdir -p /media/vruser/ssd
sudo mount /dev/sda1 /media/vruser/ssd
# Auto-mount:
echo "/dev/sda1 /media/vruser/ssd ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
```

Then set `data.video_dir` in `rigs/cheese.yaml` to the video directory on the SSD.

### 6. Screen calibration

See `docs/CALIBRATION_PROTOCOL.md`. Produces the geometry warp map and per-pixel
luminance/intensity correction used by `shared/stim_generator.py` when it renders each
stimulus. The rig is **rear-projected** (projector behind the screen, image viewed from
the front), which the geometry model accounts for.

---

## Before Each Session

### 1. Check hardware

- [ ] Ethernet switch powered on, all devices connected
- [ ] Projector on and warmed up (>15 min for stable brightness)
- [ ] Water reservoir filled and tubing primed
- [ ] Lick spout positioned correctly
- [ ] Mouse weighed and recorded
- [ ] Room lights set to experiment condition

### 2. Confirm pigpiod on cheddar

`pigpiod` runs as a systemd service. Check it:
```bash
ssh vruser@192.168.10.101 "systemctl status pigpiod"
```
Start it manually if needed: `ssh vruser@192.168.10.101 "sudo pigpiod"`.

---

## Running an Experiment

The experiment UI drives the workflow left-to-right:
**Load Rig → Load Experiment → (session params) → Deploy → GO → STOP → Transfer.**

### Step 1 — Launch the UI

```bash
conda activate vrfarm
cd ~/VRFarm
python app/app.py    # opens localhost:5000
```

### Step 2 — Load Rig

Select the rig (`cheese`) from the dropdown and click **Load Rig**. This connects to
both Pis over the REST API **and initializes every enabled device** (display/projector,
lick sensor, reward valve, camera, photodiode) in one step — there is no separate
"Connect" button. Pis show green when they respond; any device that fails to init reports
its error here.

### Step 3 — Load Experiment

Select the task from the dropdown (e.g. `go_nogo_v1`) and click **Load Experiment**.
Tasks are the YAML files in `experiments/`. Loading populates the editable parameter
panels (stimulus / reward / session). You can tweak any parameter inline; **Save** /
**Save As** write it back to a task YAML.

### Step 4 — Set session params

Fill in: **Subject ID**, **Date**, **Session #**, **Notes**. These form the
`session_id = <subject>_<YYYYMMDD>_<NNN>`. (Note: the paradigm **Level** is a *reward*
task parameter edited in the REWARD panel, not a session field.)

### Step 5 — Choose per-device saving

In the Actions row, one checkbox per data-producing device (Camera, Lick, Reward,
Photodiode) — all checked by default. Read at **GO**:
- **Camera unchecked** → livestreams to the preview but writes **no video file**.
- **A behavioral device unchecked** → the Leader skips that device's detailed HDF5
  datasets. Core per-trial outcomes are always written.

### Step 6 — Deploy

Click **Deploy**. This:
1. Uploads the current code + configs to both Pis and restarts `pi_api`.
2. Re-initializes the projector on the Follower.
3. Leader generates the stimulus plan (applying the warp/intensity correction).
4. Leader pushes the stim NPZ to the Follower.
5. Controller renders thumbnails for preview.

**GO enables only after a successful deploy.** Any parameter change in the UI invalidates
deploy (GO grays out again).

### Step 7 — GO

Click **GO**. The session starts:
- Leader runs the imperative trial loop trial by trial.
- Follower renders stimuli on the projector.
- Camera records to disk (or previews only, per Step 5).
- Events stream to the controller via UDP for the dashboard.

### Step 8 — Monitor

| Indicator | What it means |
|---|---|
| Session monitor | Current trial / block count |
| Lick Sensor | Total lick count |
| Reward | Last pulse duration |
| Display | Current trial on Follower |
| Events raster | Green ticks = licks, blue = rewards, purple = timeouts, stim on/off |
| Trial table | Per-trial outcome (hit/miss) |

The session aborts on its own if `session.global_timeout_trials` is reached (mouse dry
for N consecutive trials — see below).

**To abort manually:** Click **STOP** — ends the trial loop cleanly.

### Step 9 — Transfer data

Click **Transfer**. This first POSTs `/api/consolidate/<session_id>` to the Leader, which
merges all sidecars into a single self-contained `<session>.h5`, then downloads
`<session>.h5` (+ `video.h264` if recorded) to the controller.

**Destination:** `$VRFARM_DATA_DIR` if set, else `~/VRFarm/data` — or a per-transfer
override via the **Transfer dest** field / **Browse…** picker. Files land at:
```
<dest>/<subject>/<subject>_<date>/<session_id>/
  <session_id>.h5      <- consolidated: trials, stim plan, per-device data, camera ts, metadata
  video.h264           <- behavior video (only if camera was saved)
```

The subject index is always written to the **default** data dir (not the override):
`~/VRFarm/data/subjects/<subject_id>.json` — a running log across the subject's sessions.

---

## Task Parameters

A task YAML has four sections. Values below are the current defaults from
`experiments/template.yaml` / `go_nogo_v1.yaml`; the code that reads them is
`engine/leader.py`, `shared/stim_generator.py`, and `devices/reward.py`.

### `stimulus:` — what to show

| Param | Meaning |
|---|---|
| `background_gray` | Background luminance, plain **0..1** (0 = black, 1 = white). |
| `size_deg` | Stimulus size in visual degrees. |
| `shape` | `square` (etc.). |
| `duration_s` | Total projector on-time for the stimulus. |
| `altitude_deg` | Stimulus elevation. |
| `go_rule` | `all` \| `left` \| `right` — which azimuths count as **go**: `left` = az<0, `right` = az>0, `all` = every trial (`engine/leader.py:_classify_go`). |
| `contrast` | `{values: [...], proportions: [...]}` — contrast level(s) and their relative proportions (auto-normalized). Contrast is authored in a **selectable metric** (Weber default / Michelson / Normalized), configured on the display device in the rig. |
| `switch_trial_contrast` | Optional (e.g. `lowest`): the first trial of each new block uses this contrast. Ignored when `randomize_blocks` is on. |
| `photodiode_sync_enabled` | Toggle photodiode stim-onset sync (needs a photodiode in the rig). |
| `photodiode_sync_every_n` | Red sync patch ON every Nth display frame. |

### `reward:` — when to reward

| Param | Default | Meaning |
|---|---|---|
| `level` | 1 | Reward rule, **GO trials only** (no-go is never rewarded): **1** = free reward at response-window start; **2** = operant + Pavlovian rescue at `pav_delay_s`; **3** = pure operant (lick-gated, no rescue); **2.5** = adaptive (per-trial draw between L2 and L3). |
| `amount_mode` | `volume` | `volume` = one pulse, duration interpolated from the reward calibration for `amount_ul`. `count` = `amount_count` repeats of the **base pulse** (first calibration row). |
| `amount_ul` | 4.0 | Reward volume (µL) in `volume` mode. |
| `amount_count` | 1 | `count` mode: number of base pulses per reward. |
| `pulse_gap_ms` | 150 | `count` mode: off-time (ms) between pulses so the valve fully closes. |
| `resp_delay_s` | 0.6 | Delay after (true, photodiode-corrected) stim onset before the response window opens. **(Renamed from `reward_delay_s`; UI label "Resp delay (s)". The Leader still reads the old key as a fallback.)** |
| `response_window` | 1.4 | Response-window duration, after the response delay (may extend past stim off). |
| `pav_delay_s` | 0.5 | **L2 only:** seconds from response-window start to wait for a lick before delivering a free "Pavlovian rescue" reward. **0 = off** (L2 becomes pure operant). L3 ignores this. |
| `timeout_rule` | `rate` / `none` | `none` = no enforcement; `rate` = restart the period if the lick rate in the last 1 s reaches `max_lick_rate`; `count` = restart if licks this period exceed `max_lick_rate × duration`. |
| `max_lick_rate` | 0.3 | Lick-rate threshold (Hz) for the timeout rule. |
| `timeout_phases` | `[iti]` | Which phases the timeout rule enforces: any of `prestim`, `poststim`, `iti`. |

**Reward delivery by level** (`engine/leader.py`, GO trials only — no-go is never rewarded; all
timing is relative to the response-window start): **L1** delivers a free (Pavlovian) reward at
window entry. **L2** is operant — the first in-window lick rewards and ends the window early; if no
lick by `pav_delay_s`, a free "Pavlovian rescue" reward fires and the window runs out. **L3** is
pure operant — the first lick anywhere in the window rewards, no rescue. **L2.5** draws L2-vs-L3
per trial with probability = the adaptive state (P(L3) rises with in-window responses). In every
case a **hit** = any lick in the response window, and **reaction time** = first in-window lick
minus the window start.

### `session:` — trial structure

| Param | Meaning |
|---|---|
| `n_blocks` | Number of blocks (defaults to `n_trials // block_size` if omitted). |
| `block_size` | Trials per block. |
| `block_sequence` | Azimuth (deg) per block, cycled to fill `n_blocks`. |
| `iti` | `[min, max]` for a random ITI, or a scalar for fixed (seconds). |
| `prestim_duration` | Baseline period before stimulus onset (0 = immediate). |
| `poststim_duration` | Period after the stimulus. |
| `global_delay` | One-time delay at session start (scalar or `[min,max]`). |
| `block_delay` | Delay before each block (scalar or `[min,max]`). |
| `block_delay_skip_first` | Skip the block delay on the first block. |
| `randomize_blocks` | Optional: shuffle all trials (disables `switch_trial_contrast`). |
| `global_timeout_trials` | Abort the session after N consecutive **dry** trials (no licks in the checked phases); 0 = off. |
| `global_timeout_phases` | Phases checked for licks by the global timeout: any of `prestim`, `stim`, `poststim`, `iti`. |

### `adaptive:` — level 2.5 only

| Param | Meaning |
|---|---|
| `enabled` | Turn the adaptive staircase on (used when `reward.level` = 2.5). |
| `initial_state` | Starting P(L3) — probability a trial is drawn as operant. |
| `step_up` | Add to the state after a licked trial. |
| `step_down` | Subtract after an unlicked trial. State is clamped to [0, 1]. |

---

## Data Output

A transferred session is **one self-contained HDF5 file** (`format_version: 2`) plus the
raw video — built on the Leader at Transfer by merging the run-time sidecars
(`metadata.yaml`, `stimuli.npz`, `trials.yaml`, `frame_timestamps.npy`) into
`<session>.h5` and deleting them (`shared/consolidate.py`, `pi_api /api/consolidate`).

```
<dest>/<subject>/<subject>_<date>/<session_id>/
├── <session_id>.h5     # trials, stimulus plan, per-device data, camera timestamps, metadata
└── video.h264          # raw H.264 (only if the camera was saved)
```

The file groups per-trial data under `/trials`, the plan under `/stimulus`, and each saved
device under `/lick`, `/reward`, `/photodiode`, `/camera`; the full task YAML is stored in
the root `task_config` attribute. **See [`docs/DATA_FORMAT.md`](DATA_FORMAT.md)** for the
complete layout, the loader helpers (`analysis/loaders.py`), and the subject-index format.

All timestamps are Unix time (`time.time()`) in seconds, NTP-synced.

---

## Timing Reference

| Event | Timestamp source | Precision |
|---|---|---|
| Stimulus onset | Follower ack UDP timestamp | ~1ms |
| True onset | Photodiode-corrected (`true_onset_t`) | ~0.1ms |
| Lick onset | MPR121 polling on Leader (200Hz) | ~5ms |
| Reward delivery | `time.time()` on Leader after GPIO | <1ms |
| Camera frames | picamera2 callback (`wall_clock_s`) | ~1ms |
| Sync pulses | pigpio hardware tick, NTP-referenced | ~0.1ms |

Lick-to-reward is on the same Pi (Leader), so latency is <1ms (no network hop).

---

## Troubleshooting

**Load Rig fails / a Pi is red:**
- Check the Pi is on the network: `ping 192.168.10.101`
- Check the REST API is running: `curl http://192.168.10.101:5080/api/status`
- If the API is not running, SSH in and check: `sudo systemctl status vrfarm`

**No licks detected:**
- Check MPR121 on I2C: `ssh vruser@192.168.10.101 i2cdetect -y 1` — should show `5a`
- Check the electrode number in the rig config matches wiring

**Reward not firing:**
- Check solenoid wiring to the reward GPIO (rig `devices.reward.pins`)
- Check pigpiod is running: `ssh vruser@192.168.10.101 pgrep pigpiod`

**Display not showing stimulus:**
- Check the projector is on and connected to mozzarella
- Run the projector startup sequence: `~/rig/start_projector.sh`
- Check the Follower process: `curl http://192.168.10.102:5080/api/status`

**Video has dropped frames / no file:**
- Check the SSD is mounted: `df -h` on cheddar, and that `data.video_dir` points to it
- Confirm the **Camera** save checkbox was checked at GO (unchecked = preview only)
- Reduce the frame rate in the rig config (`devices.camera.fps`)

---

## Config Quick Reference

**Task config** (`experiments/*.yaml`) — four sections, tunable in the experiment UI:
```yaml
stimulus:
  background_gray: 0.75
  size_deg: 8.0
  shape: square
  duration_s: 2.0
  go_rule: right
  contrast: {values: [0.25, 0.15, 0.125], proportions: [0.33, 0.33, 0.33]}
reward:
  level: 2
  amount_mode: volume
  amount_ul: 4.0
  resp_delay_s: 0.6
  response_window: 1.4
  timeout_rule: rate
  max_lick_rate: 0.3
  timeout_phases: [iti]
session:
  n_blocks: 6
  block_size: 25
  block_sequence: [80, -40, 40, -80, 80, -40]
  iti: [8, 16]
adaptive:
  enabled: false
```

**Rig config** (`rigs/cheese.yaml`): hardware-fixed values (pins, calibrations, Pi
IPs/roles, network ports, `data.video_dir`). Edited in the setup UI, not per-session.
