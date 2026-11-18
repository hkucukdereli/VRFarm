# IR Illuminator (dev-catchme)

**Device:** `devices/ir_led.py` — rig YAML `devices.ir_led` (I2C address, intensity), optional task
YAML `devices: ir_led:` (intensity for this task, on or off for the session)
**Last updated:** 2026-11-18 — describes the code on `dev-catchme` as it is; no change to the code.
See [Status](#status) for what has and has not been tried
**Files:** [`devices/ir_led.py`](../devices/ir_led.py) (driver; the safety model is in its
docstring) · [`pi_api/api.py`](../pi_api/api.py) (the generic device routes, the safe-state
block) · [`engine/leader.py`](../engine/leader.py) (on at session start, off at the end) ·
[`controller/setup.py`](../controller/setup.py) (`DEVICE_PACKAGES`, `I2C_DEVICE_TYPES`) ·
[`shared/consolidate.py`](../shared/consolidate.py) (where the scalars end up) ·
[`rigs/catchme.yaml`](../rigs/catchme.yaml)
**Related:** [`CLAUDE.md`](../CLAUDE.md) ("The catchme devices", "Generic device surface") ·
[assets/IRDriver_HANDOFF.md](assets/IRDriver_HANDOFF.md) (the board: schematic, layout, decisions)
**On `main`:** the same driver was ported on 2026-11-18 with more around it (camera preview link,
Slack lines, a session that survives a switch-on failure, an `/ir_led` data group, tests). See
`docs/IR_LED.md` on `main`, and [Differences from main](#differences-from-main) below.

---

## Overview

One 850 nm LED star lights the scene for the cameras. It sits on the **IRDriver board** (rev 3.2),
made to screw to the back of a Raspberry Pi HQ Camera: a Mean Well LDD-1000L constant-current
module drives the LED (ILS ILH-IS01-85SN, 1 A), and an MCP4725 12-bit DAC on the board sets the
module's dimming voltage. The Pi writes that DAC over I²C. There is no GPIO and no PWM: the light
is steady, so it cannot beat against a camera's frame rate.

On this branch nothing knows the device by name. It is one more entry of the generic device
surface: pi_api's `/api/init_device`, `/api/monitor_device`, `/api/device_set`, the engine's
"start every stream" loop, and a generic Setup card.

| Where | What | Edited in |
|---|---|---|
| Rig YAML `devices.ir_led` | `i2c_address` (`0x60`), `i2c_bus` (1), `intensity_pct` (50); optional `curve`, and the board constants `v_min` (0.5), `v_max` (2.5), `dac_vdd` (3.3) | Setup tab → the `ir_led` card; `curve` and the constants in the YAML |
| Task YAML `devices: ir_led:` (optional) | `intensity_pct` — this task's intensity instead of the rig's; `on_during_session` (default true) | the YAML file. The Experiment tab's task form has no field for it and keeps the block when it saves |

**The LED is off unless something asks for light**: a session, or the Setup card's **Live**.

## Wiring

**One 4-wire Qwiic / STEMMA QT cable** (JST SH) from the board to the Pi's I²C-1 carries
everything on the Pi side. The LED current comes from a separate supply.

| Qwiic pin | Colour | Signal | Pi header (no hat) |
|---|---|---|---|
| 1 | black | GND | pin 9 |
| 2 | red | 3V3 | pin 1 |
| 3 | blue | SDA | pin 3 (GPIO2) |
| 4 | yellow | SCL | pin 5 (GPIO3) |

- **On catchme: the Seeed Grove Base Hat.** A Grove-to-Qwiic cable into one of the hat's I²C
  ports, next to the IMU's. Grove orders the wires differently (1 SCL yellow, 2 SDA white, 3 VCC
  red, 4 GND black) and the cable crosses them, so **match colours** (Grove white = Qwiic blue =
  SDA).
- **Without a hat:** a Qwiic-to-female-jumper cable on the four header pins above.
- **Bus addresses:** MCP4725 `0x60`, the IMU (LSM6DSV16X) `0x6B`, the hat's own MCU `0x04`
  (hat v1.0: `0x08`) — no conflict.
- **LED supply:** 9–12 V recommended (the module takes 6–36 V) on the board's screw terminal J1.
- **Plug and unplug the cable with the Pi off.** Seeed says the hat must not be hot-plugged, and
  a hot-plugged cable has taken this Pi down (`CLAUDE.md`).
- There is **no enable GPIO** (board rev ≥ 2.3). The Pi's 3V3 on the cable is the hardware gate:
  without it the board shorts the dimming input and the LED is off.

## Safety

**850 nm is invisible** (a faint red glow at most). The star is a 1 A emitter: never look into it
and do not judge "off" by eye — read the card or a camera image.

| State | LED | What makes it so |
|---|---|---|
| Pi off or unpowered | off | Hardware: no 3V3, the board's Q1/Q2 stage shorts DIM |
| Pi powers up | off | The DAC starts at its EEPROM value, and `init()` makes that 0 V on first contact. **A factory-fresh MCP4725 starts at mid-scale (1.65 V ≈ 60 % LED)**: program it before the LED is wired ([Bench bring-up](#bench-bring-up)) |
| Pi up, nobody asks for light | off | DAC 0 V. Every switch-off is read back from the chip and retried once |
| A process dies with the LED on | **lit until software or power clears it** | The DAC holds its last value, and a warm reboot keeps the 3V3. This is the accepted gap of the I²C-only board; the passes below close most of it |

What switches a left-on LED off (`Device.safe_state`: one DAC write of 0 V, EEPROM untouched):

- `devices/ir_led.py` itself: `close()`, and an exit hook for any normal interpreter exit.
- **pi_api, after the engine exits** — however it exits, the SIGKILL from `/api/stop` included.
  Log: `[safe_state] ir_led: safe state applied (engine exited with code …)`.
- **pi_api at its own start-up** (after a crash, a restart, a reboot) and **on its SIGTERM**
  (systemd stop, Restart API, Deploy). The device's config is remembered in
  `~/rig/safe_state.json` on the Pi, so the start-up pass needs no rig YAML.

What they do **not** cover:

- A SIGKILL of pi_api itself, or a kernel hang, while the LED is on: lit until the next pi_api
  start or a power cycle.
- A board that stops answering on I²C while lit (cable pulled, bus fault): the write fails
  (`[safe_state] ir_led: safe state FAILED (…)` in pi_api's own log, nothing on the controller) and
  the LED stays as it was until the next Connect / Initialize reaches the chip, or the Pi's 3V3
  drops.
- A Pi on which the device was never opened: nothing is remembered yet, so the start-up pass has
  nothing to do. The first Connect, Initialize or GO fills it in.
- An engine started by hand over SSH: pi_api's start-up and SIGTERM passes skip while one runs
  (they would darken its session).
- A Setup **Live** left running (browser tab closed): the LED stays lit until Stop, the next
  Connect, Initialize or GO, or a pi_api restart.

The EEPROM is only ever written with 0. Never store a non-zero power-up value.

## On the catchme rig

`rigs/catchme.yaml` carries the device already, assigned to the one Pi:

```yaml
devices:
  ir_led:
    type: ir_led
    enabled: true
    i2c_address: '0x60'
    i2c_bus: 1
    intensity_pct: 50
pis:
- role: leader
  devices: [worldcam, naneye, imu, ir_led]
```

- **Install** enables the I²C bus and adds `smbus2` for it (`I2C_DEVICE_TYPES`, `DEVICE_PACKAGES`
  in `controller/setup.py`); the IMU needs the same two, so a Pi installed for the IMU has them.
- **Deploy** ships `devices/ir_led.py` (`shared/deploy_manifest.py`).
- **While the board is not fitted, set `enabled: false`** (Setup → click the card's
  `ir_led (on)` chip so it reads `(off)` → Save Rig). With the device enabled and no chip at `0x60`, Connect and Initialize report
  `ir_led: MCP4725 at 0x60 on I2C-1: …` and GO answers *Leader init failed — …*: no session
  starts.
- On another rig of this branch: Setup → Device Catalog → **IR Illuminator** → Add Device, switch
  it on, assign it to the Pi, Save Rig. `rigs/_template.yaml` does not carry the block.

## Bench bring-up

Once per board, **LED unplugged and LED supply off**, Qwiic cable in, Pi up:

```bash
i2cdetect -y 1          # expect 60 (plus 6b for the IMU and 04 for the hat)
python3 -c "import smbus2; smbus2.SMBus(1).write_i2c_block_data(0x60, 0x60, [0, 0])"
```

The second line stores 0 V as the DAC's power-up value. Connect or Initialize does the same and
reports it (`EEPROM 0` in the device's check line; `[ir_led] MCP4725 EEPROM power-up value set to
0 (was 2048)` in pi_api's own log the first time). Then power-cycle the Pi with the LED fitted and
confirm it stays dark through the boot.

## When the LED is lit

| Moment | LED | Path |
|---|---|---|
| **Connect** (Experiment), **Initialize** / **Reinit** (Setup) | off | pi_api `/api/init_device` → `init()`: EEPROM 0, DAC 0 V, read back |
| Setup card **Live** … **Stop** | on at the slider's value … off | `/api/monitor_device` → `start_stream()` … `/api/stop_monitor_device` → `stop_stream()` |
| **GO** | off for a few seconds | the controller releases pi_api's devices (`/api/release_devices`), the engine opens its own (off) |
| **START** (grace period and every trial) | on at the task's, else the rig's intensity | `run_session()` starts every device's stream as its first step, before the grace period |
| Session end, **STOP** | off | `_finalize_session()` `stop_stream()`, then `close()`; then pi_api's pass after the engine exit |
| Experiment tab **Live** (the video panels) | unchanged | the previews are **not** linked to the LED on this branch |

- **The videos open dark.** GO starts the recordings at least 1.5 s before START, so the first
  frames are unlit and the step to lit marks `ir_led_on_t` in a camera that sees 850 nm.
- **To see a lit preview before a session**, use the `ir_led` card's Live in the Setup tab next
  to a camera card's Live, and press Stop before GO (GO would switch it off anyway).
- **During a session the engine owns the LED.** The Setup tab refuses Initialize and Reinit while
  the rig runs a session, and the card's Live only logs *Monitor failed: ir_led not initialized*,
  because GO released pi_api's instance. pi_api itself does not refuse `/api/init_device` for
  this device while its engine runs, so do not post it by hand: a second instance starts by
  writing 0 V and darkens the session.
- The page's Log has **no line for the LED**. The engine publishes an `ir_led` event at switch-on
  and switch-off, and the Experiment page only prints events that carry a `message`. The record
  is the video and the five scalars under [Data](#data).
- `on_during_session: false` in the task keeps the LED off for that session (`ir_led_on` = 0).

## The Setup card

The generic device card, under the tab `ir_led`:

- **`i2c_address`**, **`i2c_bus`** as text fields; **`intensity_pct`** as a slider (0–100 %) with
  the note *Live lights the LED at this level; Stop switches it off. 850 nm is invisible: never
  look into the LED.*
- **Live** lights the LED (`ir_led monitoring started.` in the Log) and opens a JSON readout of
  the device's last event, e.g. `{"event": "ir_led", "on": true, "intensity_pct": 50.0,
  "dac_code": 1861, "dac_volts": 1.5, …}`. **Stop** switches it off.
- The slider is a **live parameter** (`Device.live_params()` / `set_live()`): while the device is
  initialised a move is written through `/api/device_set`, and the card answers e.g.
  `80 % = 2.10 V, LED ON`, or `80 % = 2.10 V, LED off (Live lights it)` when it is not lit.
  **Save Rig** keeps the value as the rig's `intensity_pct`. Before Initialize the card's
  controls are disabled (*Press Initialize to enable device controls.*); if this device's own
  init failed, a move only edits the rig value (*not initialized: applies on Initialize/Reinit
  after Save Rig*).
- **Initialize** and **Reinit** always leave the LED off, also when it was lit. The Log prints the
  check line: `ir_led: OK MCP4725 0x60 on I2C-1: DAC 0/4095 (0.00 V), EEPROM 0, off, set 50 %
  = 1.50 V`.

## Intensity

`intensity_pct` is a **percentage of the driver's analog dimming window**, not calibrated light:

| % | DAC volts | DAC code (of 4095) |
|---|---|---|
| off | 0 | 0 |
| 0 (the dimmest lit level) | 0.50 | 620 |
| 25 | 1.00 | 1241 |
| 50 | 1.50 | 1861 |
| 75 | 2.00 | 2482 |
| 100 (≈ 1 A) | 2.50 | 3102 |

`volts = v_min + (v_max − v_min) × pct / 100`, `code = round(volts / dac_vdd × 4095)`. Pick one
intensity per rig with the cameras' exposure and gain fixed, and keep it.

If a bench sweep shows the module's dimming input bends that line, put the measured table in the
rig YAML; it replaces the linear map (at least two points, volts not decreasing, between `v_min`
and `dac_vdd`, linear in between, clamped at the ends):

```yaml
devices:
  ir_led:
    curve: [[0, 0.6], [50, 1.2], [100, 2.4]]     # [percent, DAC volts]
```

## When it fails

Two logs are meant below. The **engine output** is what `engine/leader.py` prints: pi_api serves
it at `/api/logs`, and the controller saves it at STOP as `logs/catchme/engine_<session>.log`.
**pi_api's own log** is the journal of its unit on the Pi (`journalctl -u vrfarm`).

| What happens | What you see | The session |
|---|---|---|
| No board, wrong address, cable out at **Connect** | Log: `rpi-demo0: ir_led: MCP4725 at 0x60 on I2C-1: …`; the rig does not reach *connected* | — |
| The same at **Initialize** | Log: `ir_led: FAIL MCP4725 at 0x60 on I2C-1: …`, then *Some devices failed.* | — |
| The same at **GO** (engine start) | the engine exits during device init; GO answers *Leader init failed — …* | does not start |
| The board fails **at START** (switch-on) | the engine exits with a traceback in the engine output; **the page shows nothing** and stays in *running* with no trials | **ends before any trial; no HDF5 is written.** Press STOP to end it on the page. The video recordings GO started are pi_api's and are not stopped by the engine's exit |
| A lit LED cannot be switched **off** at the end | engine output: `[ir_led] WARNING: could not switch the LED off on close: …`. pi_api then tries once more after the engine's exit and prints `[safe_state] ir_led: safe state applied (…)` to its own log, or `… safe state FAILED (…)` if the chip still does not answer; `ir_led_off_t` is NaN in the file | ended normally; **the LED may still be lit** — check with a camera |
| EEPROM not 0 (write failed) | Initialize fails: `EEPROM write to 0 failed … — the LED would light at boot` | — |

Nothing here reaches Slack on this branch.

## Data

Five scalars per session. The engine writes them at session end; consolidation moves them, like
every dataset it has no group for, under `/trials`:

| dataset | type | meaning |
|---|---|---|
| `/trials/ir_led_intensity_pct` | f4 | the intensity the session ran at (task override, else rig) |
| `/trials/ir_led_dac_volts` | f4 | the DAC voltage for that intensity (of the 12-bit code written) |
| `/trials/ir_led_on` | i1 | 1 = the LED was switched on in this session |
| `/trials/ir_led_on_t` | f8 | Unix time of the switch-on (NaN if it never lit) |
| `/trials/ir_led_off_t` | f8 | Unix time of the verified switch-off (NaN if it never lit, or the switch-off failed) |

```python
import h5py
with h5py.File(h5_path, "r") as f:
    ir = {k[len("ir_led_"):]: f["trials"][k][()] for k in f["trials"] if k.startswith("ir_led_")}
# {'dac_volts': 1.4997, 'intensity_pct': 50.0, 'off_t': 1795003600.4, 'on': 1, 'on_t': 1795000000.1}
```

- They are scalars, not per-trial arrays, although they sit in `/trials`.
- **The Save row applies.** Unticking `ir_led` in the Experiment tab's *Save:* row leaves the five
  scalars out of the file (`skipped_devices` in the metadata names it); the LED lights all the
  same.
- `on_t` and `off_t` are Unix time, like the trial times.

## Commissioning

| # | Step | Expect |
|---|---|---|
| 1 | [Bench bring-up](#bench-bring-up), LED unplugged | `i2cdetect` shows `60`; EEPROM 0 |
| 2 | Wire the LED and its supply; boot the Pi | LED dark through the boot (check with a camera) |
| 3 | Setup → Initialize | `ir_led: OK MCP4725 0x60 on I2C-1: DAC 0/4095 (0.00 V), EEPROM 0, off, set 50 % = 1.50 V` |
| 4 | `ir_led` card → Live, a camera card's Live next to it | image lit; move the slider through 0 / 50 / 100 % and watch the image, the LED current and the heat sink |
| 5 | Stop | image dark |
| 6 | Sweep the intensity at fixed exposure and gain; note mean image brightness per step | monotonic; if it bends, add a `curve`. Pick the rig's `intensity_pct`, Save Rig |
| 7 | Live, then Setup → **Restart API** on the Pi's row | LED off within a few seconds (pi_api's SIGTERM pass, else its start-up pass) |
| 8 | A short session (Deploy, GO, STOP after a few trials) | the videos open dark and step to lit; dark again at the end |
| 9 | A session, **STOP** in the middle of a trial | LED off; pi_api's own log (`journalctl -u vrfarm`): `[safe_state] ir_led: safe state applied (engine exited with code …)` |
| 10 | Sync the session, open the `.h5` | `/trials/ir_led_on` = 1 and both times set |

## Differences from main

`main` got the driver on 2026-11-18 as a port, because `main` has no generic device surface.
What differs for a user:

| | `dev-catchme` | `main` |
|---|---|---|
| pi_api routes | generic: `/api/init_device`, `/api/monitor_device`, `/api/stop_monitor_device`, `/api/device_set` | `/api/init_ir_led`, `/api/monitor_ir_led`, `/api/stop_monitor_ir_led`, `/api/device_set`; 409 while the engine runs |
| Setup card | generic card, JSON readout | its own card (*LED ON* in red) |
| Experiment tab preview | not linked to the LED | the camera preview lights the LED |
| Switch-on failure at START | the engine exits, no data | the session runs on; Log and Slack say the video will be dark |
| Switch-off failure at the end | engine output and pi_api's own log only | Log and Slack line |
| Data | `/trials/ir_led_*`, subject to the Save row | group `/ir_led`, always written; `Session(...).ir_led` |
| Tests | none for this device | `tools/test_ir_led.py` (fake MCP4725) |

## Status

| What | State (2026-11-18) |
|---|---|
| Driver, pi_api safe state, live slider, Install / Deploy wiring | on `dev-catchme` since 2026-11-17 (`c016766`, `d90c57d`, `acd91fe`, `74ac7f6`, `48a8d00`, `3e76151`) |
| This document | written from the code on 2026-11-18. The consolidation path was run on a scratch file; the failure rows above were read from the code, not provoked |
| **A real board on the catchme Pi** | **no trial is recorded in this repository.** The handoff copy (2026-11-03) says rev 3.2 was verified in KiCad and not yet ordered — see [assets/IRDriver_HANDOFF.md](assets/IRDriver_HANDOFF.md). Do the [commissioning](#commissioning) steps on the first board, and note the result here |
| `rigs/catchme.yaml` | has the device `enabled: true`: without a board at `0x60`, Connect fails and GO does not start a session (see [On the catchme rig](#on-the-catchme-rig)) |
| Intensity `curve`, eye-safety assessment, heat at 100 % | open: to be measured on the first board |
