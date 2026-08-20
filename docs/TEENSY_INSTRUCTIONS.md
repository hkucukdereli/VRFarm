# Teensy Firmware — Photodiode Sync Detector

**Board:** Teensy 4.0 (`teensy:avr:teensy40`), USB-attached to the controller for flashing
**Last updated:** 2026-08-20
**Files:** [`teensy/photodiode_sync_v2_0/`](../teensy/photodiode_sync_v2_0/) (current) ·
[`teensy/photodiode_sync_v1_0/`](../teensy/photodiode_sync_v1_0/) (previous)
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
| `1` (`OUT_PIN`) | square-pulse output → RPi GPIO (idles LOW, pulses HIGH) |

> ⚠️ **Teensy 4 analog pins are 3.3 V max and NOT 5 V-tolerant.** The photodiode swings
> above 5 V, so a divider or Schottky clamp is mandatory ahead of `PD_PIN`. Set `DIVIDER`
> in the sketch to your divider ratio so the debug output prints true photodiode volts.

---

## Prerequisites

Already installed on the controller — listed here for rebuilding a machine:

```bash
sudo apt install arduino-cli               # or: https://arduino.github.io/arduino-cli
arduino-cli core install teensy:avr        # Teensy core (1.62.0 as of this writing)
```

Non-root uploads need the PJRC udev rules at `/etc/udev/rules.d/00-teensy.rules`
(from https://www.pjrc.com/teensy/00-teensy.rules) — already installed here. They set the
Teensy's USB and `ttyACM*` nodes to mode `0666`, so **no `dialout` group membership is
required**, and they run `stty raw -echo` on the port at plug-in.

---

## Build and upload

```bash
cd ~/VRFarm
arduino-cli compile --fqbn teensy:avr:teensy40 teensy/photodiode_sync_v2_0
arduino-cli upload -p usb1/1-1 --fqbn teensy:avr:teensy40 teensy/photodiode_sync_v2_0
```

Or as one step:

```bash
arduino-cli compile -u -p usb1/1-1 --fqbn teensy:avr:teensy40 teensy/photodiode_sync_v2_0
```

**Confirm the port and FQBN first** — both are machine- and board-specific:

```bash
arduino-cli board list
```

```
Port         Protocol Type              Board Name FQBN                Core
/dev/ttyACM0 serial   Serial Port (USB) Unknown
usb1/1-1     teensy   Teensy Ports      Teensy 4.0 teensy:avr:teensy40 teensy:avr
```

- Upload to the **`teensy`-protocol port** (`usb1/1-1`), *not* `/dev/ttyACM0`. The ACM
  device is the USB-serial endpoint and fails as an upload target.
- The USB path changes if you replug into a different physical port — re-run `board list`.
- The FQBN must match the board: `teensy40` for a Teensy 4.0, `teensy41` for a 4.1.
  Flashing a 4.1 with the 4.0 FQBN mostly works but misconfigures the PSRAM/ethernet pins.

A successful upload prints `Opening Teensy Loader...` and the board re-enumerates on USB
(verify with `lsusb | grep 16c0` — the device number increments).

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
| `Failed uploading: no upload port provided` | Use the `teensy` port from `board list`, not `/dev/ttyACM0` |
| Upload hangs at `Opening Teensy Loader...` | Press the physical button on the Teensy to force program mode |
| Permission denied on the port | Install the PJRC udev rules (they set mode `0666`), then replug |
| No serial output | Expected in production — `DEBUG` is 0 |
| Pi sees no sync pulses | Check `OUT_PIN` wiring and that `DEBUG` builds are not still loaded |
