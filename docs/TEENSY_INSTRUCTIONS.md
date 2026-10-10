# Teensy Firmware — Photodiode Sync Detector

**Board:** Teensy 4.0 (`teensy:avr:teensy40`), USB-attached to the **leader Pi** of its rig, where it
is also flashed (Setup UI → photodiode card → **Teensy firmware → Upload**)
**Last updated:** 2026-10-07
**Files:** [`teensy/photodiode_sync_v2_2/`](../teensy/photodiode_sync_v2_2/) (current) · earlier
versions alongside · [`teensy/00-teensy.rules`](../teensy/00-teensy.rules) (PJRC udev rules Install puts on the Pi) ·
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

## Flashing from the Setup UI (the normal way)

The sketch is compiled and flashed **on the leader Pi that owns the photodiode device** — the Teensy
hangs off that Pi's USB, one Teensy per rig. In the Setup tab, photodiode card, **Teensy firmware**
row: **Browse** picks a local `.ino`; with no file chosen the server flashes the newest sketch under
`teensy/<rig>/` when that rig has its own copies, else the newest shared `teensy/` sketch. **Debug**
patches `#define DEBUG`, **Upload** does the rest and logs each step. Nothing on the controller is
needed.

**Per-rig copies.** Pins and thresholds differ between rigs, so a rig can carry its own copy of the
current sketch in `teensy/<rig>/<sketch>/<sketch>.ino` (cheddar: stock A1 / pin 16 / `MIN_PULSE_V`
0.30; morbier: A0 / pin 3 / 0.15, measured 2026-10-10: flashes 0.34 V above a 0.04 V floor, the
projector's black-level red segments 0.07 V). When a new sketch version lands in `teensy/`, copy it
into each rig folder and re-apply that rig's three lines.

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
~/bin/arduino-cli compile --fqbn teensy:avr:teensy40 --output-dir ~/teensy_build/out ~/teensy_build/photodiode_sync_v2_2
teensy_loader_cli --mcu=TEENSY40 -s -w -v ~/teensy_build/out/photodiode_sync_v2_2.ino.hex
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

## Debug / tuning mode

Production builds are **silent on serial** — `#define DEBUG 0` in the sketch. Seeing no
serial output after a flash is expected, not a fault.

Set `DEBUG 1` and reflash to stream four space-separated traces at 115200 baud for the
Arduino Serial Plotter (the onboard LED also mirrors the output):

| Trace | Signal |
|---|---|
| 1 | raw photodiode volts |
| 2 | live START threshold (crossed up to begin a pulse) |
| 3 | live END threshold (crossed back down to end it) |
| 4 | detection marker — full-scale while a pulse is accepted |

Read it from the command line with:

```bash
stty -F /dev/ttyACM0 115200    # udev already applied `raw -echo` at plug-in
cat /dev/ttyACM0
```

Tuning, while running the setup-UI photodiode **Test**:

- Flash never reaches trace 2 → lower `START_FRAC` (adaptive) or `THRESHOLD_HI_V` (fixed).
- Traces 2/3 hug the noise and trace 4 flickers while idle → raise `MIN_PULSE_V`.
- Thresholds sag when flashes pause, then snap back → that's the `MIN_PULSE_V` floor
  working; raise it only if the sag dips too low.

**Set `DEBUG` back to 0 and reflash before running an experiment.**

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Upload log: `arduino-cli: No such file` or `teensy_loader_cli: not found` | The toolchain install failed (no internet on the Pi?) — Upload retries it; check the Pi's WiFi |
| Loader waits, nothing happens | The HID reboot did not take (log says so): press the Teensy's button once; or the udev rules are missing — Upload installs them, then replug the Teensy |
| `Unable to open /dev/ttyACM0 for reboot request` | pi_api's photodiode holds the port — the loader falls back to waiting for the button; press it |
| No serial output | Expected in production — `DEBUG` is 0 |
| Pi sees no sync pulses | Check `OUT_PIN` wiring and that `DEBUG` builds are not still loaded |
