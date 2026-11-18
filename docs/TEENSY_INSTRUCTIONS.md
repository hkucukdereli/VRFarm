# Teensy Firmware — Photodiode Sync Detector

**Board:** Teensy 4.0 (`teensy:avr:teensy40`), USB-attached to the **leader Pi** of its rig, where it
is also flashed (Setup tab → photodiode card → **Upload**)
**Last updated:** 2026-11-17
**Files:** [`teensy/photodiode_sync_v2_3/`](../teensy/photodiode_sync_v2_3/) (current; each rig's own copy
under `teensy/<rig>/`, earlier versions in the git history) ·
[`teensy/00-teensy.rules`](../teensy/00-teensy.rules) (PJRC udev rules Install puts on the Pi) ·
[`teensy/teensy_hid_reboot.py`](../teensy/teensy_hid_reboot.py) (reboots a fresh RawHID board into its bootloader)
**Related:** [LEADER_WIRING.md](LEADER_WIRING.md) (where `OUT_PIN` lands on the Pi header) ·
[CALIBRATION_PROTOCOL.md](CALIBRATION_PROTOCOL.md)

---

## What it does

The Teensy reads the photodiode voltage on an analog pin, thresholds it with the same two
filters as the setup UI (steady/glitch + hold-off), and emits a clean **3.3 V square pulse**
on `OUT_PIN` to the RPi GPIO for every detected sync pulse. This solves the "photodiode ~2 V
< Pi logic-high" problem — the Teensy's 3.3 V logic drives the Pi input directly.

`v2_0` adds **adaptive thresholds**: the Schmitt thresholds ride a live baseline/peak
estimate instead of being fixed voltages, so they self-adjust as the diode's idle level
drifts. Set `ADAPTIVE 0` in the sketch to fall back to the `v1_0`-style fixed thresholds.

| Pin | Role |
|---|---|
| `A1` (`PD_PIN`) | photodiode analog input — **must be ≤ 3.3 V at the pin** |
| `16` (`OUT_PIN`) | square-pulse output → RPi GPIO16 (idles LOW, pulses HIGH) — scope-verified on the rig 2026-08-23; older docs said pin 1 |

> ⚠️ **Teensy 4 analog pins are 3.3 V max and NOT 5 V-tolerant.** The photodiode swings
> above 5 V, so a divider or Schottky clamp is mandatory ahead of `PD_PIN`. Set `DIVIDER`
> in the sketch to your divider ratio so the debug output prints true photodiode volts.

---

## The photodiode card in the Setup tab (the normal way)

The sketch is compiled and flashed **on the leader Pi that owns the photodiode device** — the Teensy
hangs off that Pi's USB, one Teensy per rig. Nothing on the controller is needed. Below the rig's own
photodiode fields the card has two rows for the Teensy:

| Control | What it does |
|---|---|
| **Teensy sketch** · **Browse** | The `.ino` of this rig, relative to the repo or absolute. The choice is stored at once in the rig YAML (`devices.photodiode.teensy_sketch`), so the card comes back with it. Empty = the newest sketch under `teensy/<rig>/`, else the newest shared one under `teensy/`. |
| **Upload** | Writes the five parameters of the row below into the sketch file, compiles it with `DEBUG 0` and flashes it: the build sessions run on, and the file is the record of it. Debug Mode goes off and the Debug window closes. Refused while a session runs, and before anything is written when the Pi's USB shows no Teensy. |
| **Debug** | Debug Mode off: compiles a copy of the sketch with `DEBUG 1` and the row's parameters (the file is not changed), flashes it and opens the Debug window. Debug Mode on: only shows or hides the window — the DEBUG build stays on the Teensy until the next Upload. |
| **Debug Mode** | An indicator, not a switch: which build the Teensy runs. Read from the Teensy itself (its `I` line) whenever the photodiode is open on the Pi; grey while not known. |
| **Flash Test** | Flashes the sync square on the display every N frames until **Stop test**, and shows the last pulse the Teensy accepted. Needs the display initialized; works with or without the Debug window. |
| `MIN_PULSE_V` `START_FRAC` `END_FRAC` `STEADY_US` `HOLDOFF_US` · **Update** | The sketch's detection tunables, filled from the sketch file. **Update** (enabled while the Debug window is open) sends them to the running DEBUG build over serial: they act at once and live in the Teensy's RAM until the next upload or power cycle. **Upload is what makes them permanent.** |

The line under the parameter row says what the Teensy runs (version, build, its five values; amber
where they differ from the card). After a page reload with a tuned DEBUG build on the Teensy the card
shows the running values, not the file's, so an Upload does not quietly flash the old ones.

**Initialize and the flash check.** Initialize / Reinit flashes the sync square for about a second and
counts the pulses on the Pi's GPIO. Missed flashes are an init failure — red dot, `Init failed: …
missed flashes: detected 0 of 12 emitted` — but the photodiode stays open on the Pi, so Debug, Flash
Test and Update keep working. With the Debug window open, Reinit shows that same flash burst in the
trace, which is the quick way to see why it failed.

**Per-rig copies.** Pins and thresholds differ between rigs, so each rig carries its own copy of the
current sketch in `teensy/<rig>/<sketch>/<sketch>.ino`, and Upload writes that rig's parameters into it
(cheddar: stock A1 / pin 16 / `MIN_PULSE_V` 0.30; morbier: A0 / pin 3 / 0.10, measured 2026-11-16 at
`sync_brightness` 1: flash tops 0.19 V on a 0.04 V floor, the projector's black-level red segments up
to 0.09 V; pecorino: A0 / pin 3 / 0.15). When a new sketch version lands in `teensy/`, copy it into
each rig folder and re-apply that rig's pin lines and parameters. A comment behind a parameter line
is kept when Upload changes the value, so correct it by hand if it quotes the old measurement.

**Toolchain on the Pi.** Install puts it on the Pi that has the photodiode (step 4b), and Upload
installs whatever is missing before compiling, so a leader that was installed earlier needs no
re-Install. The Pi downloads these itself over the institute WiFi:

| Part | Where | What for |
|---|---|---|
| `teensy-loader-cli` (apt) | `/usr/bin` | flashes the `.hex` over USB (HalfKay bootloader) |
| `arduino-cli` (ARM64 build) | `~/bin/arduino-cli` | compiles the sketch |
| Teensy core `teensy:avr` (PJRC board index) | `~/.arduino15` (~650 MB) | board support + compiler |
| PJRC udev rules | `/etc/udev/rules.d/00-teensy.rules` | Teensy USB and `ttyACM*` nodes mode `0666`, `stty raw -echo` at plug-in, ModemManager kept off the port |

**First flash of a fresh Teensy.** A new board enumerates as `16c0:0486 Teensyduino RawHID` with no
`/dev/ttyACM0`. The loader's own soft reboot only speaks to the Serial USB identity, so Upload first
sends the Teensyduino reboot request over HID ([`teensy/teensy_hid_reboot.py`](../teensy/teensy_hid_reboot.py),
the same 4-byte feature report the Teensy Loader GUI uses); the board drops into its HalfKay
bootloader and is programmed with no button press. Only if that fails does the log ask you to
press the button (the loader then waits about two minutes). Once our sketch runs the board is
`16c0:0483 Teensyduino Serial` with `/dev/ttyACM0`, and every later Upload soft-reboots it.

## Manual build on the Pi (fallback)

```bash
ssh vruser@<leader>
~/bin/arduino-cli compile --fqbn teensy:avr:teensy40 --output-dir ~/teensy_build/out ~/teensy_build/photodiode_sync_v2_3
teensy_loader_cli --mcu=TEENSY40 -s -w -v ~/teensy_build/out/photodiode_sync_v2_3.ino.hex
```

Upload leaves the sketch in `~/teensy_build/<sketch>/` on the Pi, so the paths above exist after
one Upload. `-s` soft-reboots a Serial-mode board, `-w` waits for the bootloader (button) otherwise.
`arduino-cli board list` shows the Teensy as a `teensy`-protocol port (e.g. `usb1/1-1`); the
`/dev/ttyACM0` line is the serial endpoint, not an upload target.

Reference build for `v2_0` on a Teensy 4.0:

```
FLASH: code:9252, data:3016, headers:8208   free for files:2011140
 RAM1: variables:3520, code:7536, padding:25232   free for local variables:488000
 RAM2: variables:12416  free for malloc/new:511872
```

---

## Serial lines (v2_3)

115200 baud on the Teensy's USB serial; `devices/photodiode.py` on the Pi is the one reader.

| Line | Build | Meaning |
|---|---|---|
| `P <median_V> <peak_V> <baseline_V> <width_us>` | every | one per accepted pulse |
| `B <floor_V> <ceil_V>` | every | once per second: the analog front end, with or without pulses |
| `I v2_3 DEBUG=<0\|1> MIN_PULSE_V=… START_FRAC=… END_FRAC=… STEADY_US=… HOLDOFF_US=…` | every | at boot, on `?` and after every `S`: what runs there |
| `X <text>` | every | a refused command |
| `D <t_us> <V> <start_V> <end_V>` | DEBUG | every 200 µs: the analog input and the two live thresholds |
| `E <t_us>` | DEBUG | the moment `OUT_PIN` went high |

The Teensy accepts `?` (answer with the `I` line) and, in a DEBUG build only, `S <NAME> <value>` for
the five tunables (RAM only). A DEBUG build still sends `P` and `B`, so the init check, the session
telemetry and the heartbeat diagnosis work the same in both builds; its price is ~130 kB/s of serial
traffic and the onboard LED mirroring the output. **Sessions run on the production build: press
Upload when the tuning is done.**

## Debug window and live tuning

**Debug** on the photodiode card opens a live trace, all on the Teensy's clock:

| In the window | What it is |
|---|---|
| blue trace | the voltage on the Teensy's analog pin (the `D` lines) |
| orange / purple lines | the START and END thresholds the Teensy is using right now |
| yellow triangle | the Teensy accepted a pulse and raised `OUT_PIN` (its `E` line) |
| green vertical line | a rising edge on the Pi's sync GPIO — what the engine sees |

Views: **Scroll 5 s / 1 s**, and **Pulse 200 / 50 / 20 ms**, which hold on the newest pulse like a
scope trigger; **Freeze** stops the picture. The status line counts both sides per second (`Teensy
accepted N pulses/s · Pi GPIO16 saw N edges/s`) and says how the green lines were placed: from
matched pulses (exact) or, before the first match, by serial timing (± ~1 ms).

Reading it, with **Flash Test** running:

- Flashes in the blue trace, no triangles → the Teensy refuses them. The flash does not reach the
  START threshold: lower `MIN_PULSE_V` (the threshold cannot sink below baseline + `MIN_PULSE_V`) or
  `START_FRAC`, press **Update**, watch again.
- Triangles while the square is dark, or thresholds hugging the noise → raise `MIN_PULSE_V`.
- Several triangles per flash → `HOLDOFF_US` is shorter than the DLP's sub-pulse train (4 red
  sub-pulses 4.34 ms apart per frame); it must stay below the flash period (`every_n` / 57.46 Hz).
- Triangles without green lines → the pulse leaves the Teensy and does not reach the Pi: `OUT_PIN`
  wiring, or the rig YAML's `gpio`.
- A flat blue trace at 0 V with the square flashing → nothing arrives at the analog pin (diode,
  amplifier, cable, or a covered diode).

When the values are right press **Upload**: it writes them into the rig's sketch and flashes the
production build.

By hand, with pi_api's photodiode released (one serial reader per port):

```bash
stty -F /dev/ttyACM0 115200    # udev already applied `raw -echo` at plug-in
cat /dev/ttyACM0               # P / B lines; `echo '?' > /dev/ttyACM0` asks for the I line
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Upload log: `arduino-cli: No such file` or `teensy_loader_cli: not found` | The toolchain install failed (no internet on the Pi?) — Upload retries it; check the Pi's WiFi |
| Loader waits, nothing happens | The HID reboot did not take (log says so): press the Teensy's button once; or the udev rules are missing — Upload installs them, then replug the Teensy |
| `Unable to open /dev/ttyACM0 for reboot request` | pi_api's photodiode holds the port — the loader falls back to waiting for the button; press it |
| Card: `Teensy runs a firmware older than v2_3` | It never answers `?`: no Debug window, no Update — Upload the v2_3 sketch |
| Debug Mode grey | The photodiode is not open on the Pi, so nobody asked the Teensy — Initialize, or press Debug (it opens the photodiode itself) |
| Debug window: `…'s pi_api has no Debug window — Deploy this rig` | The Pi runs code from before 2026-11-17 — Deploy |
| No serial output at all | Since v2_2 every build sends a `B` line per second: check the port (`serial_port` in the rig YAML, by-id name) and that no second process reads it |
| Pi sees no sync pulses | Open Debug with Flash Test running: triangles without green lines = `OUT_PIN` wiring / GPIO; no triangles = thresholds (see above) |
