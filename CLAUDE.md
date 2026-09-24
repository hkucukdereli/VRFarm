# VRFarm dev-catchme — Claude Code Handoff

This is the **catchme branch**: the barebones leader-only template plus the four catchme
devices, running on rig **catchme** (`rigs/catchme.yaml`) — one Pi 4, hostname rpi-demo0,
192.168.10.103, user **pi**, Debian 11 bullseye (system python 3.9 — keep Pi-deployed
code 3.9-compatible). The full attention-paradigm system lives on `main`; the stripped
template this builds on is branch `barebones`, which now runs on the same four-tab
controller as `main` (Network / Setup / Experiment / Data, one app on :5000).

## The catchme devices

| device (type)     | hardware                                            | data |
|-------------------|-----------------------------------------------------|------|
| `worldcam`        | LogiLink USB grabber (MacroSilicon MS210x, UVC) — ffmpeg MJPEG stream-copy subprocess | video.mjpeg → video.avi + frame_timestamps + preview |
| `naneye`          | NanEyeM via "vbridge" CSI bridge — CUSTOM libcamera at /usr/local/bin/libcamera-vid, MJPEG subprocess | same |
| `gyroscope_nano`  | ICM-42670-P 6-axis IMU on an Arduino Nano (FT232 → /dev/ttyUSB0), CSV @ ~100 Hz — flash `arduino/gyroscope_nano/gyroscope_nano.ino` | /imu continuous (raw int16 + scale factors) |
| `gyroscope_i2c`   | the SAME sensor rewired to the Pi's i2c-1, polled via smbus2 | same |

Only ONE gyro variant is physically wired at a time — flip the two `enabled:` flags in
`rigs/catchme.yaml` together with the wiring. Shared pieces: `devices/mjpeg_pipe.py`
(subprocess MJPEG source + PipeCameraBase) and `devices/icm42670.py` (registers + ImuBase).
Frame timestamps are pipe-arrival (`ts_source` in the metadata); NanEye fps is 30 for v1
(sensor does 186 — raise after `measured_fps` proves headroom). Video devices record into
per-device subdirs (`worldcam/`, `naneye/`) under the session's video dir; consolidation
remuxes each and folds its sidecars (`shared/consolidate.py`). The Experiment tab shows an
IMU |a| strip during a session and the Setup tab a live gyro trace on the device card.

---

## What is VRFarm (catchme)

A behavioral-experiment scaffold: one Raspberry Pi ("leader") per rig, any number of rigs,
all driven from ONE Flask app on the Controller machine. The trial engine is a simple
4-phase loop (**ITI → pre-stim → stim → post-stim**) with no stimulus hardware and no
response contingency — the stim phase is a timed placeholder window a real task grows
into. Devices are pluggable and fully generic; this branch ships the four catchme devices above (the
template it builds on, `barebones`, ships none).

**Controller:** `python controller/app.py` → http://localhost:5000, four tabs:
- **Network** — rigs (Add / edit / rename / delete), Pi identities (name, IP, role, user), rig
  groups ("super rigs"), the IP allocator (leaders `.101, .103 …`).
- **Setup** — per rig: Check / Install / Deploy / Restart API / Reboot / Shutdown for each Pi,
  the rig YAML (Save Rig), generic device cards with a Live monitor, the shepherd toggle.
- **Experiment** — per rig: Connect, task and session, Deploy, GO / STOP, live events, one
  video panel per `video: true` device, a save-checkbox per device.
- **Data** — every leader's date folders over SSH: Sync Now, Sync & Poweroff, Purge Data,
  Auto purge, all rigs into one data root.

Several rigs run side by side: each loaded rig is a sub-tab of Setup and Experiment, and a
group loads several at once. Rigs do NOT need different ports — the controller opens ONE UDP
socket and sorts datagrams by the sender's IP, falling back to the `rig` field every leader
event carries (how two mocks on 127.0.0.1 are told apart). Conda env `vrfarm`.

Controller-wide settings live in `controller/configs/controller.yaml` (gitignored; created on
first run from `controller.example.yaml` beside it): UI port, the controller's IP, the event
port, data root, auto-purge, `rsync_path`, sync tuning, groups. `VRFARM_SETTINGS=<path>`
points the app at another file (the smoke tests use a scratch one).

**Pi:** conda env `rig` (created by Install, pinned to the system python), pi_api on :5080,
shepherd health monitor as its own systemd service. `pis[].user` is honoured everywhere: the
systemd units are rendered for it at Install (`{{USER}}` / `{{HOME}}`), pi_api resolves the
relative `--rig` / `--task` paths against `~/rig`, and the Data tab's SSH/rsync use it.

---

## Project structure

```
VRFarm/
├── experiments/
│   ├── template.yaml           <- task config: session (trial timing) + devices (per-device params)
│   └── catchme_smoke.yaml      <- 3 short trials for a bench run
├── rigs/
│   ├── catchme.yaml            <- THE catchme rig: rpi-demo0 (.103, user pi), two cameras + IMU
│   ├── _template.yaml          <- what Add rig starts from ('_' files are not rigs)
│   ├── demo.yaml  demo2.yaml   <- loopback mock rigs (tools/mock_pi.py on :5080 / :5081)
│   └── .trash/                 <- deleted rigs (gitignored)
├── devices/
│   ├── base.py                 <- Device base class, IOType, DEVICE_REGISTRY (@register_device)
│   ├── mjpeg_pipe.py  icm42670.py       <- shared bases: subprocess MJPEG camera, ICM-42670 IMU
│   └── worldcam.py  naneye.py  gyroscope_nano.py  gyroscope_i2c.py
├── arduino/gyroscope_nano/     <- Nano sketch for the serial IMU variant
├── engine/
│   └── leader.py               <- 4-phase trial loop, generic device init/stream/HDF5
├── controller/                 <- THE controller, localhost:5000
│   ├── app.py                  <- shell (4 tabs) + global routes; opens the ONE UDP socket (5571)
│   ├── registry.py  events.py  <- a RigState per loaded rig; UDP demux by sender IP; SSE fan-out
│   ├── experiment.py setup.py  <- /api/rigs/<rig>/...  and  /api/rigs/<rig>/setup/...
│   ├── network.py  data.py     <- rigs/Pis/groups CRUD; Data tab (SSH/rsync sync, purge, poweroff)
│   ├── sync.py  jobs.py  ssh.py  pi_restart.py  settings.py
│   ├── configs/                <- controller.yaml (gitignored) + controller.example.yaml
│   └── templates/ static/      <- shell.html + one page per tab (per-rig pages run in iframes)
├── pi_api/
│   ├── api.py                  <- Flask REST API on the Pi (:5080) — generic device surface
│   └── vrfarm.service          <- systemd unit template ({{USER}}/{{HOME}}, rendered at Install)
├── shared/
│   ├── config.py               <- rig/task loaders + subject DB
│   ├── consolidate.py          <- fold sidecars into the session .h5, remux video
│   ├── deploy_manifest.py      <- THE single list of files deployed to ~/rig
│   ├── leader_data.py          <- Pi side of the Data tab: inventory / consolidate / purge (over SSH)
│   ├── mjpeg_relay.py          <- reconnecting MJPEG proxy (controller-side)
│   └── notify.py               <- Slack notifications
├── shepherd/                   <- independent health monitor (leader; per-rig toggle)
├── tools/
│   ├── mock_pi.py              <- fake Pi + fake leader for hardware-free end-to-end runs
│   ├── smoke_multirig.py       <- two mock rigs through connect → deploy → GO → ended
│   └── smoke_data.py           <- the Data tab against a scratch "Pi" data tree
└── data/                       <- synced sessions + subjects/ (gitignored)
```

On the Pi: `~/rig/` (code, deployed), `~/data/` (HDF5 sessions), video under the rig's
`data.video_dir` (default `~/video`). On the controller: `<data root>/<subject>/<subject>_<date>/
<session_id>/`, the same tree for every rig.

---

## Architecture

```
Controller (controller/app.py :5000 — Network / Setup / Experiment / Data)
  ↔ pi_api REST (:5080)   deploy, install, device init/monitor, start/stop, video preview
  ← UDP :5571 events      from EVERY rig's leader (trial, stim, session_end, device streams,
                          shepherd alerts) — one socket, sorted by sender IP + `rig` field
  → UDP :5572 commands    START, STOP
  ↔ SSH / rsync           Install, and the Data tab (shared/leader_data.py, sync, purge, poweroff)

Leader Pi (engine/leader.py)
  - 4-phase trial loop, durations from the task yaml `session:` block
  - devices from DEVICE_REGISTRY; per-trial + session-level HDF5 via the Device contract
  - HDF5 written locally per trial; consolidated on the Pi when the Data tab syncs
```

Session events: `grace_period`, `countdown`, `experiment_start`,
`trial_start {trial, t, iti}`, `stim {on, trial, t}`, `trial {trial_num, t, stim_on_t,
duration_s}`, `session_end {n_completed, n_planned}` — plus every device stream event,
republished generically as `{type: <event-or-device-name>, ...}`. A natural `session_end`
tears the session down server-side (stops recordings, saves logs, records the subject).

## Generic device surface (the core of this template)

Devices self-register: `devices/<type>.py` defines a class with `@register_device` and
**module name == device type**. Nothing else in the system knows device names:

- **pi_api**: `POST /api/init_device {name, type, config}` (imports `devices/<type>.py`,
  constructs from the registry, `init(config, {})`, stores it);
  `POST /api/monitor_device {name}` / `GET /api/device_data?name=` /
  `POST /api/stop_monitor_device {name}` — per-name event buffers for the Setup monitor.
- **Camera-like devices** (rig config sets `video: true`): driven through the device-name-
  keyed camera endpoints — `POST /api/camera_preview_start {device, type, config,
  session_id?, video_dir?, downsample?}` (a session_id records; else preview),
  `GET /api/camera_stream?device=`, `POST /api/camera_preview_stop {device, force?}`.
  `/api/status` reports `cameras: {name: {recording, frames}}` + aggregate
  `camera_recording`/`camera_frames`.
- **engine/leader.py**: iterates the rig yaml's enabled devices, imports by type, passes
  `task.devices.<name>` as task_params, starts every stream through one generic callback,
  and writes `hdf5_datasets()` per trial + `hdf5_session_data()` at session end.
- **Setup tab**: device cards are fully generic — editable rig-config fields + a Live
  monitor (video devices get the MJPEG preview; everything else a JSON event readout).
  The catalog comes from `/api/device_schemas`, which scans `devices/*.py`.
- **Experiment tab**: one video panel per `video: true` device; a save-checkbox per enabled
  device (video unchecked = livestream only; other devices unchecked = HDF5 skipped).

### Adding a device — the whole checklist

1. Write `devices/<type>.py` (`@register_device`, `DeviceInfo`, `init/check/close`,
   optionally `start_stream/stop_stream`, `hdf5_datasets/hdf5_trial_data/
   hdf5_session_data`, `reset_trial`; camera-likes add `start_preview/start_recording/
   stop_recording/mjpeg_stream` and `_recording/_is_preview/_frame_idx` attrs).
   The class must construct with no args and touch no hardware before `init()`.
2. Add it to `shared/deploy_manifest.py` (or it never reaches the Pi).
3. If it needs packages: add to `DEVICE_PACKAGES` (pip) / `DEVICE_APT_PACKAGES` (apt) in
   `controller/setup.py`, and to `I2C_DEVICE_TYPES` if it needs the I2C bus. A `video: true`
   device gets ffmpeg + v4l-utils automatically. `DeviceInfo.required_packages` is display-only.
   The gyros are there already (`gyroscope_nano` → pyserial; `gyroscope_i2c` → smbus2 + I2C).
4. Rig yaml: `devices.<name>: {type: <type>, enabled: true, ...hardware config}`
   (+ `video: true` for a camera-like) and add `<name>` to the Pi's `devices:` list — both in
   the Setup tab; Pi identity itself is edited in Network.
5. Optional: per-device tunables under the task yaml's `devices:` map.
That's it — no new endpoints, no UI edits (a bespoke card body is optional polish).

## Config system

| What                         | Where               |
|------------------------------|---------------------|
| Device hardware config       | rig yaml `devices:` (Setup tab) |
| Pi identity (ip, user, role) | rig yaml `pis:` (Network tab) |
| Pi-side data paths           | rig yaml `data.leader_dir`, `data.video_dir` (+ `data.video_mount` if video sits on its own drive) |
| Trial timing                 | task yaml `session:` (grace_period_s, n_trials, iti_s, prestim_s, stim_s, poststim_s) |
| Per-device tunables          | task yaml `devices:` |
| Subject/date/session#        | runtime (Experiment tab fields) |
| Data root, ports, groups, rsync, auto-purge | `controller/configs/controller.yaml` |

The rig FILENAME is the rig's identity (a disagreeing `name:` is overridden on load).

## Experiment workflow

Network (Add rig, Pi rows) → Setup (**Install** once per Pi; **Deploy** after every code
change — uploads `shared/deploy_manifest.py`, restarts pi_api and, on a leader, shepherd) →
Experiment (**Connect** inits every device through `/api/init_device` → task → session →
**Deploy** → **GO**: start the engine, wait for the literal log line `"Waiting for START
command..."`, start video recordings, UDP START → Running → Ended) → **Data** tab: **Sync
Now** or, at the end of the day, **Sync & Poweroff**.

**Data tab** (SSH only, through `shared/leader_data.py` protocol 2, which ships with Deploy):
a date folder `<subject>/<subject>_<date>` is **green** when every tree that holds it (data
dir, video dir) has nothing left to copy in a strict rsync dry run, **red** when something is
pending, **missing** when a session recorded `camera_saved: true` but its video is not on the
Pi, **grey** when the check can't be trusted (a failed dry run, an unmounted
`data.video_mount`, a leader that needs a Deploy). No nonzero rsync exit counts as clean.
**Sync Now** consolidates on the Pi, copies only the trees that hold the folder, verifies
against a fresh inventory plus a second dry run, and writes the ledger
`<data root>/.vrfarm_sync_ledger.json`. **Sync & Poweroff** copies everything not green, then
`sudo poweroff`s the Pis of each rig whose folders all verified. **Purge Data** deletes green
+ consolidated folders on the Pi, each tree's copy re-verified right before and named to the
Pi with `--verified`, which refuses anything else; **Auto purge** does that after each verified
sync. The barebones engine records no `camera_saved` flag (it has no camera concept), so
sessions read as "video unknown" and are never marked missing; a project with a video device
can write `camera_saved` into `metadata.yaml` to get that check.

## Dry-run without hardware

```bash
python tools/mock_pi.py                 # fake pi_api :5080 + fake leader (rig demo; catchme needs the hardware)
python controller/app.py --no-browser   # Experiment tab -> demo -> Connect -> Deploy -> GO
python tools/smoke_multirig.py          # two mock rigs end to end, ~30 s
python tools/smoke_data.py              # the Data tab against a scratch data tree, ~60 s
```

## Known gotchas

- `conda` not in PATH for non-interactive SSH — Install and the Data tab use
  `source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig`.
- On a macOS controller, port 5000 is taken by AirPlay Receiver — `--port 5055`.
- pi_api reload = `POST /api/restart` (self-kill; systemd `Restart=always` respawns) —
  Deploy and Restart API do this and **time it**: the measured outage ×1.5 (5–60 s) is
  handed to the leader's shepherd (`/api/shepherd_grace` → `~/rig/shepherd/api_grace.json`),
  which alerts "pi_api not responding" only once pi_api has been away that long — but at once
  during a session. Deploy also restarts shepherd, so a `shepherd.py` change takes effect.
  Devices must be re-initialised after any restart.
- **Every Pi-side file goes through Install / Deploy**: files in `shared/deploy_manifest.py`,
  apt/pip needs in `controller/setup.py`'s per-type maps. A leader deployed before the Data
  tab existed needs a Deploy (ships `shared/leader_data.py`) and `rsync` (Install adds it).
- The Data tab needs a real `rsync` ≥ 3.1 on the controller: `rsync_path: null` looks only in
  the `vrfarm` env (`conda install -n vrfarm -c conda-forge rsync`); on Linux
  `rsync_path: /usr/bin/rsync` works too. macOS's `/usr/bin/rsync` is openrsync and is rejected.
- `pkill -f PATTERN` self-matches any shell whose command line names the pattern — never
  check for a process that way (`shared/leader_data.py` scans `/proc` from Python instead).
- A rig yaml's `slack.webhook_url` is a secret: keep live rig files out of git (`.gitignore`
  them by name, as `main` does) or leave the webhook empty in tracked ones.
- rpi-demo0 is bullseye = python 3.9: no `match`, no runtime `X | Y` unions in Pi-deployed code.
  Annotations are fine behind `from __future__ import annotations`.
- NanEye: after a power cycle the vbridge FPGA must be reloaded before frames flow, and
  enumeration alone does not mean the sensor streams — see the comment in `rigs/catchme.yaml`.

## Style / conventions

- Flask SSE to the browser; UDP datagrams for real-time Pi communication
- REST (pi_api) for management; SSH/rsync for data; systemd for Pi process lifecycle
- HDF5 per trial on the leader; all timestamps `time.time()` Unix seconds (NTP-synced)
- Imperative trial loop in engine/leader.py, tuned by task-yaml `session:` params
- Device abstraction: base class + one self-registering file per device type
