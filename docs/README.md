# VRFarm documentation

Everything here is task-shaped: find the row that matches what you are trying to do.
Project overview and repo layout are in the [root README](../README.md).

## Start here

| I want to… | Read |
|---|---|
| Set up a **new controller** machine (network, SSH, conda, data dir) | [CONTROLLER_SETUP.md](CONTROLLER_SETUP.md) |
| Bring up a **new or reflashed Pi**, or build a rig from scratch | [INITIAL_SETUP_REFERENCE.md](INITIAL_SETUP_REFERENCE.md) |
| Understand the **two UIs** before touching anything | [SETUP_UI.md](SETUP_UI.md) · [EXPERIMENT_UI.md](EXPERIMENT_UI.md) |

## Run experiments

| I want to… | Read |
|---|---|
| Run a session end to end, and know what to check first | [EXPERIMENT_PROTOCOL.md](EXPERIMENT_PROTOCOL.md) |
| Find a button, or work out why one is greyed out | [EXPERIMENT_UI.md](EXPERIMENT_UI.md) |
| Understand reward levels, timeouts, task YAML fields | [EXPERIMENT_PROTOCOL.md § Task Parameters](EXPERIMENT_PROTOCOL.md#task-parameters) |
| Load and analyse the data afterwards | [DATA_FORMAT.md](DATA_FORMAT.md) |

## Maintain the rig

| I want to… | Read |
|---|---|
| Add a device, change pins, deploy code, service a Pi | [SETUP_UI.md](SETUP_UI.md) |
| Calibrate the reward valve, projector geometry, or luminance | [CALIBRATION_PROTOCOL.md](CALIBRATION_PROTOCOL.md) |
| Look up geometry parameters | [display_calibration/geometry_params.md](../display_calibration/geometry_params.md) |
| Check wiring, pins and the 40-pin header map | [LEADER_WIRING.md](LEADER_WIRING.md) |
| Build, flash or tune the **Teensy** photodiode sync firmware | [TEENSY_INSTRUCTIONS.md](TEENSY_INSTRUCTIONS.md) |
| Watch rig health during a session (temperature, disk, encode rate) | [shepherd/README.md](../shepherd/README.md) |

## Map of the system

```
Controller  (app/app.py :5000, setup/app.py :4999)
  │  REST 5080  ─ deploy, config, device init, camera
  │  UDP  5572 →  START / STOP / REWARD
  │  UDP  5571 ←  trial, lick, reward, stim, sync events
  ▼
Leader Pi (engine/leader.py) ── trial loop + lick, reward, camera, photodiode, encoder
  │  UDP 5575 → SHOW / QUIT
  ▼
Follower Pi (engine/follower.py) ── pygame renderer on the DLP projector
```

## Conventions

- **Rig YAML** (`rigs/*.yaml`) holds hardware facts: pins, I²C addresses, calibration
  tables, Pi roles and IPs, ports. Edited in the setup UI.
- **Task YAML** (`experiments/*.yaml`) holds the paradigm in four sections — `stimulus`,
  `reward`, `session`, `adaptive`. Edited in the experiment UI.
- **Machine-specific paths** are never in YAML: the controller data root comes from
  `$VRFARM_DATA_DIR`, else `~/VRFarm/data`.
- All timestamps are `time.time()` Unix seconds, NTP-synced across all three machines.

## Screenshots

UI images live in `docs/images/` and are generated, not hand-taken:

```bash
python tools/capture_ui_shots.py            # all of them
python tools/capture_ui_shots.py --only exp-07
```

The script starts `tools/mock_pi.py` and both UIs on loopback and drives them with
Playwright (`pip install playwright && playwright install chromium`). Re-run it after any
UI change so the docs do not drift. Shots needing real hardware — a live camera frame, the
projector, the geometry landmark tool — must still be taken on the rig.

> `docs/assets/` is a **local-only** stash (audits, hardware design notes, vendor manuals)
> and is gitignored — nothing in the tracked docs links to it.
