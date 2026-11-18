# IR Illuminator

**Device:** `devices/ir_led.py` — rig YAML `devices.ir_led` (I2C address, intensity), optional task
YAML `devices: ir_led:` (intensity for this task, on or off for the session)
**Last updated:** 2026-11-18 — ported to `main` from `dev-catchme`; **not yet run on a board or a
Pi** (see [Status](#status))
**Files:** [`devices/ir_led.py`](../devices/ir_led.py) · [`tools/test_ir_led.py`](../tools/test_ir_led.py) ·
`pi_api/api.py` (`/api/init_ir_led`, `/api/monitor_ir_led`, `/api/stop_monitor_ir_led`,
`/api/device_set`, the safe-state block) · `engine/leader.py` (on at START, off at the end) ·
`controller/experiment.py` (Connect, the camera preview) · `controller/events.py` (Slack) ·
`shared/config.py` (`rig_ir_led_device`, `ir_led_init_payload`) · `shared/consolidate.py` (`/ir_led`)
**Related:** [LEADER_WIRING.md](LEADER_WIRING.md) (the I²C bus) ·
[SETUP_UI.md § IR Illuminator](SETUP_UI.md#ir-illuminator) (the rig card) ·
[EXPERIMENT_UI.md § Camera controls](EXPERIMENT_UI.md#camera-controls) (the preview) ·
[DATA_FORMAT.md](DATA_FORMAT.md) (the `/ir_led` group) ·
[assets/IRDriver_HANDOFF.md](assets/IRDriver_HANDOFF.md) (the board: schematic, layout, decisions)

---

## Overview

One 850 nm LED star lights the mouse for the behaviour camera. It sits on the **IRDriver board**
(rev 3.2), which screws to the back of the Raspberry Pi HQ Camera: a Mean Well LDD-1000L
constant-current module drives the LED (ILS ILH-IS01-85SN, 1 A), and an MCP4725 12-bit DAC on the
board sets the module's dimming voltage. The leader Pi writes that DAC over I²C. There is no
GPIO and no PWM: the light is steady, so it cannot beat against the camera's frame rate.

| Where | What | Edited in |
|---|---|---|
| Rig YAML `devices.ir_led` | `i2c_address` (`0x60`), `i2c_bus` (1), `intensity_pct` (50); optional `curve`, and the board constants `v_min` (0.5), `v_max` (2.5), `dac_vdd` (3.3) | Setup tab → **IR Illuminator** card (address, intensity); the rest in the YAML |
| Task YAML `devices: ir_led:` (optional) | `intensity_pct` — this task's intensity instead of the rig's; `on_during_session` (default true) | the YAML file. The Experiment tab has no field for it and keeps the block when it saves the task |
| `engine/leader.py`, `pi_api/api.py`, `controller/` | when the LED is lit (below) | — |

**The LED is off unless something asks for light**: a session, the Setup card's **Live**, or the
Experiment tab's camera preview.

## Wiring

**One 4-wire Qwiic / STEMMA QT cable** (JST SH) from the board to the leader's I²C-1 carries
everything on the Pi side. The LED current comes from a separate supply.

| Qwiic pin | Colour | Signal | Leader header (no hat) |
|---|---|---|---|
| 1 | black | GND | pin 9 |
| 2 | red | 3V3 | pin 1 |
| 3 | blue | SDA | pin 3 (GPIO2) |
| 4 | yellow | SCL | pin 5 (GPIO3) |

- **Without a hat:** a Qwiic-to-female-jumper cable on the four header pins above.
- **On a Seeed Grove Base Hat:** a Grove-to-Qwiic cable into one of the hat's I²C ports. Grove
  orders the wires differently (1 SCL yellow, 2 SDA white, 3 VCC red, 4 GND black) and the cable
  crosses them, so **match colours** (Grove white = Qwiic blue = SDA). The hat's own MCU answers
  on the same bus at `0x04` (hat v1.0: `0x08`).
- **Bus addresses:** MCP4725 `0x60`, next to the lick sensor's MPR121 (`0x5A`) and the wheel
  encoder's AS5600 (`0x36`) — no conflict.
- **LED supply:** 9–12 V recommended (the module takes 6–36 V) on the board's screw terminal J1.
- **Plug and unplug the cable with the Pi off** (Seeed: the Grove hat must not be hot-plugged).
- There is **no enable GPIO** (board rev ≥ 2.3). The Pi's 3V3 on the cable is the hardware gate:
  without it the board shorts the dimming input and the LED is off. GPIO22 stays a free spare.

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
- **pi_api, after the engine exits** — however it exits, the SIGKILL from Stop included. A watcher
  thread sees the exit within ~0.2 s. Log: `[safe_state] ir_led: safe state applied (engine exited
  with code …)`.
- **pi_api at its own start-up** (after a crash, a restart, a reboot) and **on its SIGTERM**
  (systemd stop, Restart API, Deploy). The device's config is remembered in
  `~/rig/safe_state.json` on the leader, so the start-up pass needs no rig YAML.

What they do **not** cover:

- A SIGKILL of pi_api itself, or a kernel hang, while the LED is on: lit until the next pi_api
  start or a power cycle.
- A board that stops answering on I²C while lit (cable pulled, bus fault): the write fails
  (`safe state FAILED` in the pi_api log; a Slack line if a session was ending) and the LED stays
  as it was until the next Load Rig / Initialize reaches the chip, or the Pi's 3V3 drops.
- A leader on which the device was never opened: nothing is remembered yet, so the start-up pass
  has nothing to do. The first Load Rig, Initialize or GO fills it in.
- An engine started by hand over SSH: pi_api's start-up and SIGTERM passes skip while one runs
  (they would darken its session).

The EEPROM is only ever written with 0. Never store a non-zero power-up value.

## Putting it on a rig

1. **Rig YAML.** A rig made from `rigs/_template.yaml` already has the block with
   `enabled: false`. On an older rig: Setup tab → Device Catalog → **IR Illuminator** → Add. Then
   switch it on, open its tab and click the **leader's** chip to assign it, and **Save Rig**:

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
     devices: [..., ir_led]
   ```

2. **Install** only on a Pi that never had an I²C device: it enables the bus and adds `smbus2`.
   A leader with a lick sensor or an encoder has both already.
3. **Deploy** the rig (ships `devices/ir_led.py`, the new pi_api and engine).
4. **Initialize** in Setup, or **Load Rig** in Experiment (its connect step). A rig that lists the device but has no
   board fails here (`MCP4725 at 0x60 on I2C-1: …`), and a session on it would not start: set
   `enabled: false` until the board is fitted.

## Bench bring-up

Once per board, **LED unplugged and LED supply off**, Qwiic cable in, Pi up:

```bash
i2cdetect -y 1          # expect 60 (plus 5a, 36 and the hat's 04 if those are fitted)
python3 -c "import smbus2; smbus2.SMBus(1).write_i2c_block_data(0x60, 0x60, [0, 0])"
```

The second line stores 0 V as the DAC's power-up value. Load Rig or Initialize does the same and
reports it (`EEPROM 0` in the device's check line; `[ir_led] MCP4725 EEPROM power-up value set to
0 (was 2048)` in the pi_api log the first time). Then power-cycle the Pi with the LED fitted and
confirm it stays dark through the boot.

## When the LED is lit

| Moment | LED | Path |
|---|---|---|
| **Load Rig** (Experiment: the connect step), **Initialize** / **Reinit** (Setup) | off | pi_api `/api/init_ir_led`: DAC 0 V, EEPROM 0, read back |
| Setup card **Live** … **Stop** | on at the slider's value … off | `/api/monitor_ir_led` … `/api/stop_monitor_ir_led` |
| Experiment tab **camera preview** on … off | on at the rig's intensity … off | controller `/camera_start` → `/api/monitor_ir_led`; `/camera_stop` → `/api/stop_monitor_ir_led` |
| **GO** | off for a few seconds | the controller releases pi_api's devices, the engine opens its own (off) |
| **START** (grace period and every trial) | on at the task's, else the rig's intensity | engine `start_stream()`; event `ir_led` → log line *IR LED on at 50 %* |
| Session end, **STOP**, abort | off | engine `stop_stream()` + `close()`; then pi_api's pass after the engine exit |

- **The video opens dark.** The camera starts recording about 1.5 s before START, so the first
  frames are unlit and the step to lit marks `ir_led_on_t` in the video. With auto exposure the
  frames right after the step can be over-exposed until it settles; fixed exposure and gain
  (rig YAML `devices.camera`) avoid that and keep sessions comparable.
- **The preview shows what the recording will.** Only the Experiment tab's preview is linked to
  the LED; the Setup tab's Camera card is not — use the IR card's Live next to it. If pi_api has
  no open device when the preview starts (after a Deploy or Restart API), the preview opens it
  from the rig YAML. A preview left running (browser tab closed) leaves the LED lit until the
  next GO, Load Rig or pi_api restart.
- **During a session the engine owns the LED.** `/api/init_ir_led` and `/api/monitor_ir_led`
  answer 409 (*a session is running — the engine owns the IR LED*) while pi_api's engine runs: a
  second instance would start by writing 0 V and darken the video. The Setup card cannot change
  the intensity of a running session.
- `on_during_session: false` in the task keeps the LED off for that session (the record then
  says `on = 0`).
- The five session scalars are written whatever the **Save** checkboxes say; the LED has no
  checkbox.

## The Setup card

**I2C address**, an **Intensity** slider (0–100 %), **Live** / **Stop**. Full description:
[SETUP_UI.md § IR Illuminator](SETUP_UI.md#ir-illuminator).

- **Live** lights the LED at the slider's value (*LED ON* in red on the card) and the Log prints
  `IR LED ON at 60 % (DAC 1.7 V). 850 nm is invisible: never look into the LED.`
- The slider is a **live parameter**: while lit, a move is written to the DAC at once
  (`/api/device_set`; the card answers e.g. `80 % = 2.10 V, LED ON`). **Save Rig** keeps the
  value as the rig's `intensity_pct`. Before Initialize the slider only edits the rig value.
- **Initialize** and **Reinit** always leave the LED off, also when it was lit.
- A leader that was not deployed since the port answers 404, and the card says *the pi_api on
  <pi> has no IR LED routes — Deploy this rig.*

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
intensity per rig with the camera's exposure and gain fixed, and keep it.

If a bench sweep shows the module's dimming input bends that line, put the measured table in the
rig YAML; it replaces the linear map (at least two points, volts not decreasing, between `v_min`
and `dac_vdd`, linear in between, clamped at the ends):

```yaml
devices:
  ir_led:
    curve: [[0, 0.6], [50, 1.2], [100, 2.4]]     # [percent, DAC volts]
```

## When it fails

| What happens | What you see | The session |
|---|---|---|
| No board, wrong address, cable out at **Load Rig / Initialize** | red dot (the leader in Experiment, the device tab in Setup); `MCP4725 at 0x60 on I2C-1: …` | — |
| The same at **GO** (engine start) | the engine exits during device init; GO answers *Leader init failed — …* | does not start |
| The board fails **at START** (switch-on) | Log: *IR LED did NOT switch on: … — the video will be dark*; Slack: `🔴 <rig> — IR LED did not switch on: … — the video of this session will be dark`; engine log: `WARNING: IR LED did not switch on (…)` | **runs on** — the LED only lights the camera, so the behaviour data is kept |
| A lit LED cannot be switched **off** at the end | Log: *IR LED did NOT switch off: … — it may still be lit*; Slack: `🔴 <rig> — IR LED did not switch off: … — it may still be lit` | ended normally; check the LED |
| The preview could not light it | Log: *IR LED not lit for the preview: …* | — |
| EEPROM not 0 (write failed) | Initialize fails: `EEPROM write to 0 failed … — the LED would light at boot` | — |

The Slack lines need the rig's `slack:` block enabled. The page's log lines for START and the end
arrive with the session's event stream, which is open from GO.

## Data

Five scalars per session, in the group `/ir_led` (`shared/consolidate.py`):

| dataset | type | meaning |
|---|---|---|
| `ir_led_intensity_pct` | f4 | the intensity the session ran at (task override, else rig) |
| `ir_led_dac_volts` | f4 | the DAC voltage for that intensity (of the 12-bit code written) |
| `ir_led_on` | i1 | 1 = the LED was switched on in this session |
| `ir_led_on_t` | f8 | Unix time of the switch-on (NaN if it never lit) |
| `ir_led_off_t` | f8 | Unix time of the verified switch-off (NaN if it never lit, or the switch-off failed) |

```python
from analysis.loaders import Session
Session(session_dir).ir_led
# {'intensity_pct': 50.0, 'dac_volts': 1.4997, 'on': 1, 'on_t': 1795000000.1, 'off_t': 1795003600.4}
#   (dac_volts is the voltage of the DAC code actually written: 1861 / 4095 x 3.3 V)
# {} for a session from a rig without the device
```

`on_t` and `off_t` are on the same clock as the trial events and the camera's `wall_clock_s`, so
`camera.wall_clock_s >= ir_led['on_t']` selects the lit frames.

## Commissioning a rig

| # | Step | Expect |
|---|---|---|
| 1 | [Bench bring-up](#bench-bring-up), LED unplugged | `i2cdetect` shows `60`; EEPROM 0 |
| 2 | Wire the LED and its supply; boot the Pi | LED dark through the boot (check with the camera) |
| 3 | Setup → Initialize | green dot; check line `MCP4725 0x60 on I2C-1: DAC 0/4095 (0.00 V), EEPROM 0, off, set 50 % = 1.50 V` |
| 4 | IR card → Live, camera preview next to it | image lit; move the slider through 0 / 50 / 100 % and watch the image, the LED current and the heat sink |
| 5 | Stop | image dark |
| 6 | Sweep the intensity at fixed exposure and gain; note mean image brightness per step | monotonic; if it bends, add a `curve`. Pick the rig's `intensity_pct`, Save Rig |
| 7 | Live, then **Restart API** on the leader | LED off within a few seconds (pi_api's SIGTERM pass, else its start-up pass) |
| 8 | A short session (Deploy, GO, STOP after a few trials) | Log *IR LED on at … %* at START and *IR LED off* at the end; the video opens dark and steps to lit |
| 9 | A session, **STOP** in the middle of a trial | LED off; pi_api log `[safe_state] ir_led: safe state applied (engine exited with code …)` |
| 10 | Sync the session, open it | `Session(...).ir_led` has `on = 1` and both times |

## Tests

```bash
conda activate vrfarm
python tools/test_ir_led.py            # ~20 s; --quick = driver and config only, --keep = keep the logs
```

No Pi and no board: a fake `smbus2` with one MCP4725 stands in, and the real pi_api, a real
engine and a scratch controller run against it on loopback (driver and its failure paths, the
routes, the safe-state passes and their limits, whole sessions including SIGKILL and a board that
vanishes, Load Rig and the preview link). Run it on its own, not next to `tools/smoke_data.py`.
For this test `pi_api/api.py` takes `--host` and `tools/mock_pi.py` reads `API_HOST`.

## Status

| What | State (2026-11-18) |
|---|---|
| Driver, pi_api routes and safe state, engine, controller, Setup card, data group | on `main`, tested against a fake MCP4725 (`tools/test_ir_led.py`) |
| Setup card and Experiment page in a browser | checked in headless Firefox against a mock Pi |
| **A real board on a real Pi** | **not tried.** Nothing here has driven an MCP4725, an LDD-1000L or an LED on `main`. Do the [commissioning](#commissioning-a-rig) steps first |
| The board itself | the handoff copy (2026-11-03) says rev 3.2 was verified in KiCad and not yet ordered — see [assets/IRDriver_HANDOFF.md](assets/IRDriver_HANDOFF.md) for its open items |
| Rigs | no rig YAML has the device yet; each needs the block, a Deploy and step 1 |
| Intensity `curve`, eye-safety assessment, heat at 100 % | open: to be measured on the first board |
