# VRFarm barebones — Claude Code Handoff

This is the **barebones template branch**: a leader-only, device-agnostic skeleton of the
VRFarm system. The full attention-paradigm system (follower/display/stimulus, photodiode
sync, lick/reward contingency) lives on `main`; this branch strips all of it and keeps the
generic machinery — setup UI, experiment UI, session engine, data saving, livestreams,
deploy/install — so new projects fork from here and add their own devices.

---

## What is VRFarm (barebones)

A behavioral-experiment scaffold: one Raspberry Pi 4 ("leader") per rig, controlled from a
Controller machine via two Flask web UIs. The trial engine is a simple 4-phase loop
(**ITI → pre-stim → stim → post-stim**) with no stimulus hardware and no response
contingency — the stim phase is a timed placeholder window a real task grows into.
Devices are pluggable and fully generic: this branch ships **zero** concrete devices.

**Controller:** Flask UIs (`app/` experiment, `setup/` rig setup), conda env `vrfarm`
**Pi:** conda env `rig` (created by Install, pinned to the system python), pi_api on :5080

---

## Project structure

```
VRFarm/
├── experiments/template.yaml   <- task config: session (trial timing) + devices (per-device params)
├── rigs/demo.yaml              <- loopback mock rig (use with tools/mock_pi.py)
├── devices/
│   └── base.py                 <- Device base class, IOType, DEVICE_REGISTRY (@register_device)
├── engine/
│   └── leader.py               <- 4-phase trial loop, generic device init/stream/HDF5
├── app/                        <- experiment UI (localhost:5000)
├── setup/                      <- rig setup UI (localhost:4999)
├── pi_api/
│   ├── api.py                  <- Flask REST API on the Pi (:5080) — generic device surface
│   └── vrfarm.service          <- systemd unit
├── shared/
│   ├── config.py               <- rig/task loaders + subject DB
│   ├── consolidate.py          <- fold sidecars into the session .h5, remux video
│   ├── deploy_manifest.py      <- THE single list of files deployed to ~/rig (both UIs use it)
│   ├── mjpeg_relay.py          <- reconnecting MJPEG proxy (controller-side)
│   └── notify.py               <- Slack notifications
├── shepherd/                   <- independent health monitor (leader; per-rig toggle)
├── tools/mock_pi.py            <- fake Pi + fake leader for hardware-free end-to-end runs
└── data/subjects/              <- session history JSONs (auto-created)
```

On the Pi: `~/rig/` (code, deployed), `~/data/` (HDF5 sessions), video under the rig's
`data.video_dir`.

---

## Architecture

```
Controller (app/app.py :5000, setup/app.py :4999)
  ↔ pi_api REST (:5080)  — deploy, install, device init/monitor, start/stop, transfer
  ← UDP :5571 events from the leader engine (trial, stim, session_end, device streams)
  → UDP :5572 commands  (START, STOP)

Leader Pi (engine/leader.py)
  - 4-phase trial loop, durations from the task yaml `session:` block
  - devices from DEVICE_REGISTRY; per-trial + session-level HDF5 via the Device contract
  - HDF5 written locally per trial; consolidated at session end; transferred after
```

Session events: `grace_period`, `countdown`, `experiment_start`,
`trial_start {trial, t, iti}`, `stim {on, trial, t}`, `trial {trial_num, t, stim_on_t,
duration_s}`, `session_end {n_completed, n_planned}` — plus every device stream event,
republished generically as `{type: <event-or-device-name>, ...}`.

## Generic device surface (the core of this template)

Devices self-register: `devices/<type>.py` defines a class with `@register_device` and
**module name == device type**. Nothing else in the system knows device names:

- **pi_api**: `POST /api/init_device {name, type, config}` (imports `devices/<type>.py`,
  constructs from the registry, `init(config, {})`, stores it);
  `POST /api/monitor_device {name}` / `GET /api/device_data?name=` /
  `POST /api/stop_monitor_device {name}` — per-name event buffers for the setup-UI monitor.
- **Camera-like devices** (rig config sets `video: true`): driven through the device-name-
  keyed camera endpoints — `POST /api/camera_preview_start {device, type, config,
  session_id?, video_dir?, downsample?}` (a session_id records; else preview),
  `GET /api/camera_stream?device=`, `POST /api/camera_preview_stop {device, force?}`,
  `POST /api/camera_controls {device}` (only if the device has `apply_exposure`).
  `/api/status` reports `cameras: {name: {recording, frames}}` + aggregate
  `camera_recording`/`camera_frames`.
- **engine/leader.py**: iterates the rig yaml's enabled devices, imports by type, passes
  `task.devices.<name>` as task_params, starts every stream through one generic callback,
  and writes `hdf5_datasets()` per trial + `hdf5_session_data()` at session end.
- **setup UI**: device cards are fully generic — editable rig-config fields + a Live
  monitor (video devices get the MJPEG preview; everything else a JSON event readout).
- **experiment UI**: one video panel per `video: true` device; a save-checkbox per enabled
  device (video unchecked = livestream only; other devices unchecked = HDF5 skipped).

### Adding a device — the whole checklist

1. Write `devices/<type>.py` (`@register_device`, `DeviceInfo`, `init/check/close`,
   optionally `start_stream/stop_stream`, `hdf5_datasets/hdf5_trial_data/
   hdf5_session_data`, `reset_trial`; camera-likes add `start_preview/start_recording/
   stop_recording/mjpeg_stream` and `_recording/_is_preview/_frame_idx` attrs).
   The class must construct with no args and touch no hardware before `init()`.
2. Add it to `shared/deploy_manifest.py` (or it never reaches the Pi).
3. If it needs pip packages: add to `DEVICE_PACKAGES` in `setup/app.py` (and
   `I2C_DEVICE_TYPES` if it needs the I2C bus). `DeviceInfo.required_packages` is
   display-only.
4. Rig yaml: `devices.<name>: {type: <type>, enabled: true, ...hardware config}`
   (+ `video: true` for a camera-like) and add `<name>` to the Pi's `devices:` list.
5. Optional: per-device tunables under the task yaml's `devices:` map.
That's it — no new endpoints, no UI edits (a bespoke card body is optional polish).

## Config system

| What                         | Where               |
|------------------------------|---------------------|
| Device hardware config       | rig yaml `devices:` |
| Pi identity (ip, user, role) | rig yaml `pis:`     |
| Trial timing                 | task yaml `session:` (grace_period_s, n_trials, iti_s, prestim_s, stim_s, poststim_s) |
| Per-device tunables          | task yaml `devices:` |
| Subject/date/session#        | runtime (UI fields) |

The rig FILENAME is the rig's identity (`load_rig` overrides a disagreeing `name:`).
`pis[].user` is honored everywhere (SSH probe, install, reboot); pi_api resolves relative
`--rig`/`--task` args against `~/rig`, so the controller never hardcodes a Pi's home path.

## Experiment workflow

Setup → Load Rig (init devices) → **Deploy** (code via `shared/deploy_manifest.py` + rig +
task yamls; restarts pi_api) → GO (start engine, wait for the literal log line
`"Waiting for START command..."`, start video recordings, UDP START) → Running → Ended
(teardown stops recordings, saves logs) → Transfer (consolidate on the Pi, size-verified
downloads preserving subdirectories, shepherd log mirror, subject DB record).

## Dry-run without hardware

```bash
python tools/mock_pi.py           # fake pi_api :5080 + fake leader on UDP
python app/app.py --no-browser    # rig = demo → Load Rig → Deploy → GO
```

## Known gotchas

- `conda` not in PATH for non-interactive SSH — Install uses
  `source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig`
- On a macOS controller, port 5000 is taken by AirPlay Receiver — disable it
- pi_api reload = `POST /api/restart` (self-kill; systemd `Restart=always` respawns) —
  Deploy does this automatically; devices must be re-initialized after
- Keep Pi-deployed code compatible with the OLDEST Pi python in the fleet (bullseye = 3.9:
  no `match`, no runtime `X | Y` unions)

## Style / conventions

- Flask SSE to the browser; UDP datagrams for real-time Pi communication
- REST (pi_api) for management; systemd for Pi process lifecycle
- HDF5 per trial on the leader; all timestamps `time.time()` Unix seconds (NTP-synced)
- Imperative trial loop in engine/leader.py, tuned by task-yaml `session:` params
- Device abstraction: base class + one self-registering file per device type
