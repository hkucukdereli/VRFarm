# VRFarm — Claude Code Handoff

This document is for Claude Code to pick up the VRFarm project.
Read this entire file before touching anything.

---

## What is VRFarm

A behavioral neuroscience experiment system for mice. Two Raspberry Pis per rig
(Leader + Follower), controlled from the Controller via Flask web UIs. Paradigms are
tuned via task YAML (stimulus/reward/session/adaptive params); the trial engine is the
imperative loop in `engine/leader.py`. Devices are pluggable. Named after cheese.

**Current rig:** `cheddar` — Leader `cheddar` (192.168.10.101, **Pi 5**),
Follower `cheddar-dlp` (192.168.10.102, **Pi 4**, drives the projector).
Live rig config is `rigs/cheddar.yaml` (NOT cheese.yaml, which is its tracked twin).

**Controller:** `fystyk` (192.168.10.1), env `vrfarm` (Python 3.11) under the Homebrew miniforge at
`/opt/homebrew/Caskroom/miniforge/base/envs/vrfarm` (`~/miniforge3` is a second install holding only
`base`/`caiman`). ONE web app runs everything: `python controller/app.py` → http://localhost:5000 with
four tabs, Network / Setup / Experiment / Data. Controller-wide settings (data root, auto purge, rig
groups) live in `controller.yaml` (gitignored; template `controller.example.yaml`). Full guide:
`docs/MULTI_RIG.md`.
**Both Pis:** Debian 13 (trixie), conda env `rig`, user `vruser`. The `rig` env Python
**must match the system Python** (3.13 on trixie) — the camera bindings
(`python3-libcamera`/`python3-picamera2`) are apt-built for the system Python and symlinked
into the env, so a version mismatch breaks `import picamera2`. Create with
`conda create -n rig python=$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')`.

> **The display does NOT run in the conda env.** The projector runs under full KMS, and the
> conda env's SDL has **no kmsdrm backend** — it silently falls back to a null driver that
> renders nothing at all. Everything that opens the display (`displayd/renderer.py`,
> `display_calibration/calib_geo.py`) runs on the **system `/usr/bin/python3`**.

---

## Project structure

```
~/VRFarm/                              <- project root on the Controller
├── rigs/
│   ├── cheddar.yaml                   <- rig hardware config (pins, cal, ports, roles) — YAML, one per rig
│   └── _template.yaml                 <- new-rig template (Network tab -> Add rig); '_' files are not rigs
├── experiments/
│   └── *.yaml                         <- task/paradigm configs (stimulus/reward/session/adaptive)
├── devices/                           <- one file per device type (base.py = Device, DEVICE_REGISTRY)
│   ├── lick_sensor.py  reward.py  camera.py  photodiode.py  encoder.py  display.py
├── engine/
│   └── leader.py                      <- Leader Pi main process (imperative trial loop)
├── displayd/                          <- KMS display daemon (owns the projector)
│   ├── displayd.py                    <- control daemon: lifecycle, L1 poll, supervisor, REST
│   ├── renderer.py                    <- renderer child, sole DRM master (system python3)
│   ├── dlpc.py                        <- defensive DLPC3436 I2C wrapper
│   ├── PROTOCOL.md                    <- THE interface contract — read before changing anything
│   └── displayd.service
├── controller/                        <- THE controller UI, localhost:5000 (replaced app/ and setup/)
│   ├── app.py                         <- shell (4 tabs) + global routes; opens the ONE UDP socket (5571)
│   ├── registry.py  events.py         <- a RigState per loaded rig; UDP demux by sender IP; SSE fan-out
│   ├── experiment.py  setup.py        <- /api/rigs/<rig>/...  and  /api/rigs/<rig>/setup/...
│   ├── network.py  data.py            <- rigs/Pis/groups CRUD; Data tab (SSH/rsync sync, purge, poweroff)
│   ├── sync.py  jobs.py  ssh.py       <- Data-tab engine, background jobs, ssh/scp helpers
│   └── templates/ static/             <- shell.html + one page per tab (per-rig pages run in iframes)
├── controller.yaml                    <- controller-wide settings (gitignored; see controller.example.yaml)
├── pi_api/api.py                      <- Flask REST API on each Pi, port 5080
├── shared/
│   ├── config.py  stim_generator.py  notify.py
│   ├── leader_data.py                 <- Pi-side inventory / consolidate / purge for the Data tab (over SSH)
│   └── deploy_manifest.py             <- single deploy file list (Install and Deploy)
├── display_calibration/               <- geometry calibration (kmsdrm; calib_geo.py + cal_start/stop.sh)
├── shepherd/                          <- Pi-side health monitor
└── data/subjects/                     <- session history JSONs
```

**On the Leader (.101):** `~/rig/` (code), `~/data/<subject>/<subject_date>/<session_id>/`
(HDF5; sidecars are folded into the .h5 by the consolidate step), video on the SSD.
**On the Follower (.102):** `~/rig/` (code incl. `displayd/`), `~/rig/calibration/`,
`~/rig/stims/<session_id>/stimuli.npz`.

---

## Network

```
Gigabit ethernet switch (experiment traffic)
├── Controller  192.168.10.1
├── Leader      192.168.10.101 (eth0 static)
└── Follower    192.168.10.102 (eth0 static)
```
Both Pis are also on institute WiFi for internet/NTP. Passwordless SSH + sudo from the
Controller to both Pis.

| Port | Direction | Purpose |
|------|-----------|---------|
| 5000 | browser → Controller | the one controller UI (Network / Setup / Experiment / Data) |
| 22 | Controller → Pi | SSH: Install, calibration hand-off, and the Data tab (rsync sync, purge, poweroff) |
| 5080 | Controller → Pi | pi_api REST (deploy, start/stop, device init, camera) |
| 5571 | Leader → Controller | events (trial, lick, reward, sync, display_health, display_abort) from EVERY rig — one socket, sorted by sender IP (+ the `rig` field each event carries) |
| 5572 | Controller → Leader | commands (START, STOP, REWARD) |
| 5575 | Leader → displayd | session channel (SHOW, LOAD_STIMS, HB_FLASH, SYNC_TEST) |
| 5573 | displayd → Leader | acks: stim_onset, hb_flash (flip times), display_health |
| 5581 | localhost only | displayd control REST (/status /bringup /standby /resume /render /lease) |
| 5582 | Leader → displayd | photodiode ingest + hb_verdict (drives `optics` in /status) |
| 5091 | browser → Follower | calib_geo sliders, only while calibrating |

---

## Display architecture (KMS)

The projector is a TI **DLPDLCR230NPEVM** (DLPC3436 controller, DLP230NP DMD, XPR-4 pixel
shift). Video is **DPI parallel RGB666 on GPIO0-21 — not HDMI**. The DLPC is configured over
a bit-banged software I2C bus (GPIO23=SDA, GPIO22=SCL) at 8-bit address 0x36.

Full KMS (`vc4-kms-v3d` + `vc4-kms-dpi-generic`), 1920×1080 @ **57.46 Hz**, 125 MHz pixel
clock. There is **no X server anywhere** on the rig.

> **pygame needs the `SCALED` (or `OPENGL`) flag for `vsync=1` to be honored.** Without it
> `vsync=1` is silently ignored — this was the root cause of years of unlocked flips
> (44 fps against a 57.46 Hz scanout). See `devices/display.py`.

**displayd** owns the display: a control daemon plus a renderer child that is the sole DRM
master. Two DRM masters is a black screen, so there is deliberately **no second path** to
the framebuffer — pi_api's display endpoints forward to displayd or return 503.

Health layers:
- **L1** — DLPC I2C poll at 1 Hz (LED/DMD/sequencer status; fault bits latch and clear-on-read).
  Live input-loss detector is `ActuatorWatchdogTimerTimeout`, *not* AutoFraming.
- **L2** — renderer flip watchdog + startup self-test (backend, fps, flip-block).
- **L3** — photodiode heartbeat: the leader flashes the sync square on **its own clock** every
  5 s between stimuli and correlates the pulse against the flip time displayd reports back.
  Counts alone are not enough (mains flicker ~100 Hz and ground-fault hum ~50 Hz both fake them).
- **L4** — camera arbiter. Not built.

A lost heartbeat is localized from the Teensy's 1 Hz `B <floor> <ceil>` idle line, which reports
the analog front end independently of pulse detection: `sensor_dead` (B absent), `ttl_dead`
(light seen, no TTL edge), `no_light` (front end alive, genuinely dark), `no_telemetry`.
`no_light` does **not** claim the projector failed — a covered diode is equally dark; only L4
could separate those. Alarms reach Slack via the Controller and, if `session.abort_on_display_fault`
is set (default true), end the session at a **trial boundary** with `end_reason: display_fault`.

---

## Config system

| What | Where | Example |
|------|-------|---------|
| Pins, I2C addresses, calibration tables, roles, IPs, ports | rig YAML | `gpio: 16`, `refresh_hz: 57.46` |
| Paradigm / trial params, adaptive rules | task YAML | `stimulus.duration_s`, `reward.amount_ul` |
| Subject, date, session # | runtime (UI) | set in the experiment UI |

## Device abstraction

Adding a device = one file in `devices/`, subclassing `Device` from `devices/base.py`:
`info`, `init()`, `check()`, `task_params_schema()`, `hdf5_datasets()`/`hdf5_trial_data()`,
`start_stream()`/`stop_stream()`, optional `needs_calibration`/`calibrate()`.
`@register_device` adds it to `DEVICE_REGISTRY`.

---

## Experiment workflow

Setup → Connect → **Deploy** → Go → Ended → New session; the data is pulled later from the **Data** tab.
Deploy uploads code (list lives in `shared/deploy_manifest.py`), generates stimuli on the
Leader, pushes the NPZ to the Follower, and renders thumbnails. Any parameter change
invalidates the deploy and greys out Go.

Several rigs run side by side: each loaded rig is a sub-tab of Setup and Experiment, and a rig
**group** (Network tab) loads several at once. Rigs do NOT need different ports: the controller
opens ONE UDP socket and sorts datagrams by the sender's IP (falling back to the `rig` field every
leader event carries). Pi identity (name / IP / role / user) is edited in the Network tab.

**Data tab** (SSH only, talks to leaders): a date folder `<mouse>/<mouse>_<date>` is green when an
rsync dry run finds nothing left to copy in either tree, red otherwise. **Sync Now** copies chosen
folders (consolidate on the Pi first, verify with a second dry run, ledger at
`<data root>/.vrfarm_sync_ledger.json`); **Sync & Poweroff** copies everything red, then
`sudo poweroff`s every Pi of the selected rigs; **Purge Data** deletes green + consolidated folders
on the Pi; **Auto purge** does that right after each verified sync. Data lands subject-first in one
tree for all rigs: `<data root>/<mouse>/<mouse>_<date>/<session_id>/`.

---

## Packages

**Controller** (`conda activate vrfarm`): `flask requests scipy matplotlib numpy h5py pyyaml`, plus
`rsync` >= 3.1 from conda-forge (`conda install -n vrfarm -c conda-forge rsync`) — Apple's
`/usr/bin/rsync` is openrsync and the Data tab rejects it.
**Leader** (`rig` env): `flask pyyaml numpy scipy h5py smbus2 pigpio lgpio pyserial` + `picamera2`;
`rsync` from apt on every Pi (the Install step adds it).
**Follower**: `rig` env for pi_api; **system python3** for displayd/renderer and calib_geo
(`python3-pygame`, `python3-numpy`, `python3-yaml` from apt — no Flask, and calib_geo's web
UI is stdlib `http.server` for exactly that reason).

pigpiod is built from source (`/usr/local/bin/pigpiod`, unit at
`/etc/systemd/system/pigpiod.service`, enabled) — the apt package is gone on trixie.

```bash
conda activate vrfarm
python controller/app.py            # the controller UI, localhost:5000 (--port 5055 if AirPlay squats 5000)
python tools/smoke_multirig.py      # two fake rigs end to end (mock Pis)
python tools/smoke_data.py          # the Data tab against a scratch data tree
```
Slack comes from the rig YAML's `slack:` block (`enabled` + `webhook_url`).

---

## Known issues / gotchas

- `conda` is not in PATH for non-interactive SSH — use
  `source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig`
- **Never run the display from the conda env** (no kmsdrm backend; silent null driver).
- **Never start X on the display Pi.** It takes DRM master from displayd and blacks the rig.
  Geometry calibration hands over properly instead: `cal_start.sh` POSTs `:5581/standby`,
  `cal_stop.sh` POSTs `/resume`. The setup UI refuses any X path while displayd is alive.
- `pkill -f PATTERN` **self-matches** any wrapper whose command line mentions the literal —
  always issue the kill separately from commands that name the file. (The Data tab's
  engine-running check therefore scans `/proc` from inside Python, `shared/leader_data.py`.)
- **Every new package or Pi-side file goes through Install / Deploy**: apt packages in the Install
  step (`controller/setup.py`), Pi-side files in `shared/deploy_manifest.py`, controller packages
  in the list above. A leader installed before the multi-rig work needs a Deploy (ships
  `shared/leader_data.py`) and a re-Install or `sudo apt install rsync`.
- Calibration files are per rig once `display_calibration/<rig>/` exists
  (`python tools/migrate_calibration_dir.py cheddar`); until then the shared folder is used.
- **One serial reader.** The Teensy's `/dev/ttyACM0` gives its bytes to exactly one process;
  a second reader silently steals them. Release pi_api's devices before bench tools.
- The Teensy sync output is **pin 16** (scope-verified), analog in is **A1**.
- Leader is on a small SD card — reclaim with `conda clean -a -y` / `pip cache purge`.
- Pis are firewall-gated off the institute WiFi. To install packages, run a proxy on the
  Controller (`python -m proxy --hostname 192.168.10.1 --port 8899`) and set
  `HTTPS_PROXY=http://192.168.10.1:8899` on the Pi.
- A barrel-jack pull on the EVM has been observed to take out DLPC I2C as well as the light
  (L1 goes unreachable). Suite A documented the opposite — logic back-feeding through the Pi
  header while the room went dark — so do not assume either signature.

---

## Style / conventions

- UDP datagrams for all real-time Pi communication; REST (Flask) for management; SSE to browsers
- systemd for Pi process lifecycle (`vrfarm.service`, `displayd.service`, `pigpiod.service`)
- HDF5 for trial data, written incrementally per trial on the Leader
- All timestamps `time.time()` Unix seconds, NTP-synced
- Trial engine: imperative loop in `engine/leader.py`, tuned by task-YAML params
- `displayd/PROTOCOL.md` is the interface contract — update it in the same commit as the code
