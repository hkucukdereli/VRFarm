# Leader — Hardware Wiring Reference

Pin assignments for **the Leader Pi** on rig **cheese** (192.168.10.101).
Source of truth for the live pins is [`rigs/cheese.yaml`](../rigs/cheese.yaml) — if you
change a pin there, update this table. The **Future expansion** section below reserves
pins on paper for hardware not yet wired (no device class yet, so nothing is in
`cheese.yaml` for those rows).

**GPIO numbering is BCM**, not physical pin position. The 40-pin header pinout is
**identical on Pi 4B and Pi 5**, so this map holds for either board — only the GPIO
*library* differs (see [Software prerequisites](#software-prerequisites)).

---

## Currently wired

| Device        | Signal          | Dir   | BCM GPIO | Phys | Config key                          | Notes |
|---------------|-----------------|-------|----------|------|--------------------------------------|-------|
| Reward valve  | Main solenoid   | OUT   | **23**   | 16   | `devices.reward.pins.main.gpio`      | Active-high pulse; gate of a FET/driver, not the valve supply. Boots LOW. |
| Reward valve  | Home solenoid   | OUT   | **24**   | 18   | `devices.reward.pins.home.gpio`      | Second valve line, same driver pattern. Boots LOW. |
| Photodiode    | Sync pulse (Teensy) | IN | **16**  | 36   | `devices.photodiode.gpio`            | Clean 3.3 V square pulse from the **Teensy** (below) — one rising edge per sync frame. No on-Pi filtering. |
| Lick sensor   | MPR121 SDA/SCL  | I²C-1 | **2 / 3**| 3/5  | `devices.lick_sensor.i2c_address` `0x5A` | Touch electrode 4 on the MPR121, not a Pi GPIO. Shares the bus (below). |
| Wheel encoder | AS5600 SDA/SCL  | I²C-1 | **2 / 3**| 3/5  | `devices.encoder.i2c_address` `0x36` | Magnetic angle sensor on the running wheel. Same bus as the lick sensor (below). |
| Camera        | CSI ribbon      | CSI   | —        | CSI  | `devices.camera`                     | Flat ribbon to the CSI connector, not the GPIO header. |

> The **stimulus display** lives on **the Follower Pi** (192.168.10.102) over
> HDMI/DPI — nothing on the leader's header. The photodiode is taped over the sync patch on
> the follower's screen; its analog signal runs to a **Teensy**, and only the Teensy's clean
> digital pulse returns to **the leader's GPIO16**.

> **Retired:** the `calibration_probe` TTL output (was GPIO22) is no longer a device —
> GPIO22 is a free spare. Reward moved off GPIO17/18 to **23/24**, freeing 17/18 too.

### Photodiode signal conditioning — the Teensy

The photodiode swings **>5 V**, which is unsafe both for the Pi (3.3 V, not 5 V-tolerant)
and for a naïve threshold. A **Teensy 4** sits between the photodiode and the leader and does
all the conditioning — the leader just timestamps the resulting clean edges.

| Teensy pin      | Signal                         | Notes |
|-----------------|--------------------------------|-------|
| **A1** (analog in) | ← photodiode                | **Divide/clamp to ≤ 3.3 V before this pin** — Teensy 4 analog pins are 3.3 V max, not 5 V-tolerant. |
| **pin 1** (OUT) | → Leader **GPIO16**           | Clean **5 ms** (`OUT_PULSE_US`), 3.3 V square pulse per sync frame; idles LOW, pulses HIGH. Drives the Pi input directly (no level shift). |
| **pin 13** (LED)| onboard LED mirrors OUT        | Quick visual check that pulses are firing — **`DEBUG` builds only**. |
| **GND**         | ↔ Leader GND                  | Common ground is essential — tie Teensy GND to a Pi GND pin. |
| USB             | power                          | Bench supply or a Pi USB port. |

Firmware: [`teensy/photodiode_sync_v2_0/`](../teensy/photodiode_sync_v2_0/) — build, flash
and tuning instructions in **[TEENSY_INSTRUCTIONS.md](TEENSY_INSTRUCTIONS.md)**. It is a
Schmitt trigger plus the same two filters the old on-Pi model used (`STEADY_US = 150` glitch
reject, `HOLDOFF_US = 5000` → one pulse per frame). Since v2_0 the two thresholds are
**adaptive** by default (`ADAPTIVE = true`): they ride a live baseline/peak estimate so they
follow the diode's drifting idle level. Set `ADAPTIVE = false` for the fixed pair
(`THRESHOLD_HI_V = 1.8` / `THRESHOLD_LO_V = 0.6`). Flash with `DEBUG = 0` for production.

This **replaces the old on-Pi debounce/glitch model**: the
`debounce_*` / `glitch_*` keys still present in `cheese.yaml` are now **inert**, and
`photodiode.py` explicitly clears the Pi glitch filter so the Teensy edges pass untouched.

### I²C bus (both sensors — daisy-chained)

Both sensors sit on the **one hardware I²C-1 bus** (GPIO2 = SDA, GPIO3 = SCL). Daisy-chain
them: run **3V3 / GND / SDA / SCL** from the header to the first breakout, then the same
four nets on to the second. This works only because the addresses differ:

| Sensor        | Chip   | Address | Role                          |
|---------------|--------|---------|-------------------------------|
| Lick sensor   | MPR121 | `0x5A`  | Capacitive touch (licks)      |
| Wheel encoder | AS5600 | `0x36`  | Running-wheel angle           |

- GPIO2/3 have **onboard ~1.8 kΩ pull-ups** to 3V3. Many breakouts add their own pull-ups
  too; two in parallel is usually fine at 100 kHz. If the bus looks marginal
  (`i2cdetect` flaky, NAKs), remove the pull-ups on **one** board.
- Verify both after wiring: `i2cdetect -y 1` should show **`5a`** and **`36`**.
- Keep the daisy-chain leads short; for a long run to the wheel, twist SDA/SCL with GND.

---

## Future expansion (reserved on paper)

Planned headroom: **+3 general-purpose TTL inputs** and **+3 TTL outputs**. Pins are chosen
below by the [selection rules](#gpio-selection-rules); they are **not** in `cheese.yaml`
yet (no `ttl_in` / `ttl_out` device class exists — add one modeled on `photodiode.py` for
inputs and a latched-output helper for outputs, then register the pins).

| Line       | Dir | BCM GPIO | Phys | Boot state | Why this pin |
|------------|-----|----------|------|------------|--------------|
| TTL out 1  | OUT | **17**   | 11   | LOW        | Boot-low; freed when reward moved to 23/24. Reward-cluster neighbour, GND at phys 14. |
| TTL out 2  | OUT | **18**   | 12   | LOW        | Boot-low; also freed by the reward move. |
| TTL out 3  | OUT | **27**   | 13   | LOW        | Boot-low; completes the phys 11–13 output cluster beside GND phys 14. |
| TTL in 1   | IN  | **19**   | 35   | LOW        | Contiguous input block at the header end; GND at phys 39. |
| TTL in 2   | IN  | **20**   | 38   | LOW        | Same block. |
| TTL in 3   | IN  | **21**   | 40   | LOW        | Same block; GND at phys 39 adjacent. |

> ⚠️ **Pi GPIO is 3.3 V and NOT 5 V-tolerant.** Lab TTL sources (Master-8, Arduino, DAQ,
> BNC TTL) are usually **5 V** — feed the three TTL **inputs** through a level shifter,
> resistor divider, or (preferred) an **opto-isolator**, which also breaks ground loops.
> Pi **outputs** swing 0–3.3 V (most 5 V-logic inputs still read that as HIGH) and source
> ~8 mA safely — drive anything heavier (LED, relay, second solenoid) through a
> transistor/buffer, as the reward valve already is.

Software-wise the TTL inputs should follow the **photodiode pattern**: claim the line as an
input with a software pull-down + edge callback tagged with the hardware tick. TTL outputs
should follow the **reward pattern**: claim as output-low, then a latch or a pulse thread.

Still-free spares after this: **GPIO22** (phys 15, freed from the calibration probe), and
boot-LOW **GPIO12/13/25/26** (12 is PWM0, 13 is PWM1). **GPIO4/5/6** boot HIGH — use them
only as inputs/spares.

---

## Full 40-pin header map

```
                 3V3  (1) (2)  5V
      SDA  GPIO2  I²C  (3) (4)  5V
      SCL  GPIO3  I²C  (5) (6)  GND ──┐ sensor gnd
           GPIO4 spare (7) (8)  GPIO14  UART TXD (reserved)
                 GND  (9) (10) GPIO15  UART RXD (reserved)
 ttl-out1 GPIO17 OUT (11) (12) GPIO18  OUT  ttl-out 2
 ttl-out3 GPIO27 OUT (13) (14) GND
 spare    GPIO22  —  (15) (16) GPIO23  OUT  reward main
                 3V3 (17) (18) GPIO24  OUT  reward home
 SPI     GPIO10 rsvd (19) (20) GND
 SPI      GPIO9 rsvd (21) (22) GPIO25  spare (boot-low)
 SPI     GPIO11 rsvd (23) (24) GPIO8   SPI CE0 (reserved)
                 GND (25) (26) GPIO7   SPI CE1 (reserved)
 EEPROM   GPIO0 rsvd (27) (28) GPIO1   EEPROM  (reserved)
           GPIO5 spare(29)(30) GND
           GPIO6 spare(31)(32) GPIO12  spare (PWM0, boot-low)
          GPIO13 spare(33)(34) GND
 ttl-in1 GPIO19  IN  (35) (36) GPIO16  IN   photodiode (Teensy pulse)
          GPIO26 spare(37)(38) GPIO20  IN   ttl-in 2
                 GND (39) (40) GPIO21  IN   ttl-in 3
```

Legend: **OUT/IN** = wired or reserved signal · **I²C** = shared sensor bus ·
**rsvd** = kept free for a bus (SPI0 for a future photodiode ADC, UART console, HAT EEPROM) ·
**spare** = free for expansion.

---

## GPIO selection rules

Why the pins landed where they did — reuse these when adding hardware:

1. **Outputs must boot LOW.** GPIO **0–8 power up with pull-ups (float HIGH)**; GPIO
   **9–27 power up pull-down (LOW)**. Every output — both reward lines (23/24) and all
   reserved TTL-out (17/18/27) — is in 9–27 so a valve or marker can't glitch active before
   software claims the line.
2. **Don't squat on a bus you might want.** GPIO2/3 are I²C (in use). GPIO7–11 (SPI0) are
   left free for a possible **MCP3008 photodiode ADC** (analog sync trace + UI-adjustable
   threshold — an alternative to the Teensy front-end). GPIO0/1 (HAT ID EEPROM) and
   GPIO14/15 (serial console) are left alone.
3. **Group by function and hug a ground.** The reward pair sits on the right column
   (phys 16/18) next to GND phys 20; the three TTL inputs form a contiguous block at the
   header end (phys 35/38/40) beside GND phys 39; the reserved TTL outputs cluster at
   phys 11–13 beside GND phys 14 — short, tidy runs to a terminal block.
4. **One clean digital line per external signal.** The photodiode's analog conditioning
   lives on the Teensy; the leader sees only a boot-low digital input on GPIO16.

---

## Power / ground

- **3V3 logic only** on the header (phys 1/17). MPR121, AS5600, the Teensy output, and all
  TTL I/O must stay within 0–3.3 V. GPIO16 and the TTL-in pins are **not** 5 V-tolerant.
- The photodiode's **>5 V** analog swing is divided/clamped **on the Teensy side** (before
  its A1 pin) — the Pi never sees it. Tie the Teensy GND to a Pi GND pin.
- Tie every device ground to a Pi **GND** pin (phys 6, 9, 14, 20, 25, 30, 34, or 39).
  Use a common star ground; opto-isolate external TTL to avoid ground loops.
- The reward solenoids run off a **separate supply** through a transistor/FET + flyback
  diode — GPIO23/24 only switch the gate, they do not power the valves.

> **Planned: breakout PCB.** A future hat/breakout board is the intended home for all of
> this — a ground plane retires the star-ground juggling, and the same board consolidates
> the signal conditioning this doc calls out per-line: I²C pull-ups + connectors for the
> daisy-chain, opto-isolators / level shifters on the 5 V TTL inputs, FET/buffer drivers on
> the outputs, and the photodiode front-end (Teensy or ADC). Until then, wire it discretely
> per the tables above; the pin map is chosen to survive the transition unchanged.

---

## Software prerequisites

- **I²C enabled**: `raspi-config` → Interface Options → I²C. Verify with `i2cdetect -y 1`
  → both `5a` (MPR121) and `36` (AS5600) present.
- **GPIO library** (reward, photodiode — the only header-touching devices now):
  - **Pi 4B (`main` branch — the leader today)** — `pigpio` with the `pigpiod` daemon running
    (`sudo pigpiod` / the `pigpiod` service).
  - **Pi 5 (`dev-pi5` branch)** — `lgpio`, opening **`gpiochip0`** (the 40-pin
    header). No daemon; works on the Pi 4 too. Override the chip in `cheese.yaml` with
    `gpiochip:` if an early Pi 5 image enumerates the header as `gpiochip4`; confirm with
    `gpiodetect`. The pin map is identical. (Background: `PI5_LEADER_FEASIBILITY.md` in the
    local-only `docs/assets/` stash.)
- **Teensy firmware**: flash `teensy/photodiode_sync/photodiode_sync.ino` with
  Arduino + Teensyduino (`PD_PIN = A1`, `OUT_PIN = 1`, `DEBUG = 0`).

---

## Settings that depend on this wiring

- `devices.reward.calibration.main` (`[[10, 4]]` → 10 ms pulse ≈ 4 µL); see
  [`reward_calibration.py`](../devices/reward_calibration.py).
- `devices.photodiode.pulse_every_n_frames` (5) must match the sync-patch cadence on
  the follower (`stimulus.photodiode_sync_every_n` in the task YAML).
- `devices.photodiode.sync_corner` / `sync_size_px` / `sync_brightness` set the on-screen
  sync patch that **the follower** draws (the photodiode is taped over it) — they ride into
  the follower's display config, they are not the leader's GPIO settings.
- `devices.encoder.wheel_diameter_cm` (14.4) and `sample_hz` (500) set the wheel angle →
  distance/speed conversion and the running-wheel logging rate.
