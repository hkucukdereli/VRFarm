# Optogenetic Stimulation

**Device:** `devices/opto.py` — rig YAML `devices.opto` (pins, cap, latency seed), task YAML `opto:`
(which trials, which windows, the pattern)
**Last updated:** 2026-11-06 — built on branch `dev-opto`; **not yet run on a rig** (see [Status](#status))
**Files:** [`devices/opto.py`](../devices/opto.py) · [`tools/test_opto.py`](../tools/test_opto.py) ·
`shared/stim_generator.py` (`_assign_opto`, the lit-trial draw at Deploy) · `shared/config.py`
(`validate_opto`, `rig_opto_device`) · `engine/leader.py` (anchors, heartbeat guard) ·
`pi_api/api.py` (`/api/init_opto`, `/api/opto_pulse`, `/api/opto_off`)
**Related:** [LEADER_WIRING.md](LEADER_WIRING.md) (pins 17 / 27) ·
[SETUP_UI.md § Optogenetics](SETUP_UI.md#optogenetics) (the rig card) ·
[EXPERIMENT_PROTOCOL.md § opto](EXPERIMENT_PROTOCOL.md#opto--optogenetic-stimulation) (task fields) ·
[EXPERIMENT_UI.md](EXPERIMENT_UI.md) (the form, the raster, the Opto column) ·
[DATA_FORMAT.md](DATA_FORMAT.md) (the `/opto` group)

---

## Overview

A leader-Pi GPIO drives the laser (or LED) driver's TTL input; an optional second GPIO drives a
masking LED. The Pi shapes the pattern (continuous or pulsed); the light power is set on the
driver and only logged (`power_mw`, `wavelength_nm` in the task, which travels into the HDF5's
`task_config`). The feature lives in three places:

| Where | What | Edited in |
|---|---|---|
| Rig YAML `devices.opto` | hardware: `pins.laser` (GPIO17), `pins.mask` (GPIO27, optional), `gpiochip`, the per-window cap `max_on_s`, the latency seed `show_latency_s`, `source` (free text) | Setup tab → Optogenetics card: pins, cap, **Test laser** / **Test mask** |
| Task YAML `opto:` | paradigm: `enabled`, `fraction`, `eligible`, `min_gap_trials`, the five `windows`, `pattern`, `mask`, `power_mw`, `wavelength_nm` | Experiment tab → OPTOGENETICS section |
| `engine/leader.py` + `devices/opto.py` | fires the windows during the session and records what fired | — |

The OPTOGENETICS section is **shown only** when the loaded rig's `devices.opto` is `enabled` and
listed on the leader (`shared/config.rig_opto_device`, the same test the engine makes). On any
other rig it is hidden and the task is saved with `opto.enabled: false` — what you see is what
runs. Deploy refuses an opto-enabled task on a rig without the device, a window longer than
`max_on_s` and a bad pulse pattern, and warns when photodiode sync is off.

**Which trials are lit** is drawn at Deploy (`shared/stim_generator.py`): `fraction` of the
eligible trials (`all` | `go` | `nogo`), stratified per contrast within each block with a carried
remainder, with at least `min_gap_trials` unlit trials between lit ones. The draw goes into
`stimuli.npz` (`opto_on`) and the trial table (`opto`), and survives into the HDF5 as
`/stimulus/opto_planned` even when the device was not saved. On a lit trial **every enabled
window fires**; the masking LED fires on **every** trial with the same windows and pattern, so
the mouse cannot tell lit trials by the light.

## Timing model

| Window anchor | The engine knows the anchor time… | Who writes the ON edge |
|---|---|---|
| `iti_start`, `trial_start`, `response_window` | at the event, from its own clock (`iti_start_t`, `trial_start_t`, `rw_start_t`) | the main thread at once (`onset_s = 0`), else the device's scheduler thread at `t + onset_s` |
| `reward` | inside `_action_deliver_reward` | same; the trial's **first** reward only, never the manual REWARD button |
| `stim_onset`, `onset_s ≥ 0` | from the **photodiode edge** of the first stimulus frame | lgpio's notify thread, inside the edge callback (~1 ms after the photons); `onset_s > 0` is scheduled from the edge. **Fallback** when no edge comes: `min(sync_timeout, max(L + 0.02, 0.12))` after SHOW with photodiode sync on, `L` after SHOW with sync off |
| `stim_onset`, `onset_s < 0` (a lead, ≤ 1 s) | before SHOW, predicted as `t_show + L` | the scheduler: SHOW is planned at `t_show_planned = now + max(0, lead − L)`, the laser fires at `t_show_planned + L − lead`, and the engine holds SHOW until `t_show_planned`. The precision is the display's jitter (±25 ms), not the Pi's |

`L` is the running median of the last 20 photodiode-confirmed `display_latency_s` values
(`LATENCY_WINDOW`), seeded by the rig's `devices.opto.show_latency_s` (default 0.09 s).
Measured across the rigs (40 sessions, 2026-11): the genuine SHOW → photons latency starts at
~57 ms, median 94 ms, p95 107 ms, max 118 ms. A laser timed from the SHOW command alone would
therefore jitter by ±25 ms — hence the edge lock, and hence: **turn
`stimulus.photodiode_sync_enabled` on for opto sessions.**

Edges are `lgpio.gpio_write` calls at absolute deadlines from the device's scheduler thread (an
edge that is already due is written on the calling thread). Python-timed edges on the Pi land
0.1–1 ms late typically and ≤ 5 ms in the worst case, which sets the floors `pulse_width_ms ≥ 2`
and `pulse_hz ≤ 100`. A window requested while that pin is still ON is dropped and counted
(`opto_n_dropped`), never queued. Every window is capped at `max_on_s`.

What was achieved is in the data: the live trial table's **Opto** column (`opto_rel_ms`) and
the HDF5's `opto_lat_ms` (`analysis/loaders.py`) are the laser ON edge minus the true stimulus
onset, and `/opto/opto_window_anchor` names the source of every window — `edge`
(photodiode-locked), `pred` (a lead timed from `L`), `fallback` (no edge came), `anchor` (the
other anchors).

## Design notes — two findings

**`lgpio.tx_pulse` cannot time a one-shot window.** It is a phase-locked PWM engine: in lgpio
0.2.2.0 `src/lgPthTx.c` (`lgGpioCreateTxRec`) the first edge waits for the next boundary of the
cycle grid, so a 2 s "pulse" can start up to 2 s late, and `tx_pulse(…, 0, 0)` stops a train
without driving the pin LOW. The device never calls it (the fake-lgpio unit test asserts so) and
writes each edge itself at an absolute deadline. Measured in `tools/test_opto.py`: OFF edges
within ±0.3 ms of their deadline, a 20 Hz train with a mean period of 50.00 ms and no drift.

**Heartbeat mis-lock (pre-existing, fixed 2026-11-06).** The L3 display heartbeat flashes the
sync square every 5 s between stimuli. When a flash was commanded less than ~100 ms before SHOW,
its photons landed *after* SHOW and the engine took the first sub-pulse for the stimulus onset:
69 of 5888 photodiode-confirmed trials (1.2 %, all rigs, 2026-11) record a `display_latency_s`
of 2.7–40 ms, which is physically impossible. On those trials `true_onset_t` and the response
window were early by up to 90 ms, and an edge-locked laser would have fired 50–90 ms before the
stimulus. The fix is in `engine/leader.py`: `HB_SETTLE_S = 0.15` holds SHOW until a flash
commanded less than 150 ms earlier has landed, and `SYNC_MIN_LATENCY_S = 0.045` makes the engine
and the opto device ignore any edge earlier than 45 ms after SHOW. Side effect: on the few
trials whose SHOW falls right after a heartbeat, the pre-stimulus period is up to ~150 ms
longer. Archived sessions keep the mis-locked rows — see the caveat in
[DATA_FORMAT.md](DATA_FORMAT.md).

## Data

Per trial in the HDF5 `/opto` group (column list in [DATA_FORMAT.md](DATA_FORMAT.md)):
`opto_on`, `opto_mask_on`, `opto_onset_t`, `opto_n_windows`, `opto_n_dropped`, `opto_window_t`
(`[on, off, …]` per laser window), `opto_window_anchor` (`anchor:source,…`). The plan is
`/stimulus/opto_planned`; the task's `opto:` block travels in the root `task_config` attribute.

```python
from analysis.loaders import Session
s = Session("/path/to/<session_id>")
s.trials[["trial_num", "opto", "opto_lat_ms", "opto_anchors"]]   # one row per trial
s.opto_windows                                                    # one row per laser window
```

## Safety

- `max_on_s` (rig YAML) caps every window: the device refuses longer ones at init, Deploy
  refuses them before that.
- The light is off on STOP, display-fault abort, global timeout, session end and device close;
  the controller's teardown also POSTs `/api/opto_off` to the leader, because a SIGKILLed engine
  leaves the pin at its last level.
- Pins 17 and 27 boot LOW; the TTL is active-high only.
- **The Pi is not an interlock.** Use the driver's own maximum-on-time or a one-shot on the TTL
  line, and a pull-down at the driver input. Run the first bench tests with emission disabled on
  the driver and only the TTL on the scope.

## Commissioning a rig (bench recipe)

Nothing below has been run yet (see [Status](#status)). Preconditions: a Deploy to the rig (it
ships `devices/opto.py` and the new engine); the driver's TTL on GPIO17 (phys 11), the masking
LED on GPIO27 (phys 13), grounds tied; scope ch1 = the Teensy sync pulse (Pi GPIO16, phys 36),
ch2 = the laser TTL; laser emission disabled on the driver; Setup tab → Optogenetics card:
Enabled, pins, `max_on_s` → **Save Rig** → **Initialize** (both pins read back LOW).

| # | Step | Pass when |
|---|---|---|
| a | Setup card **Test mask**, then **Test laser**: 100 ms continuous, then pulsed 20 Hz / 5 ms over 1000 ms | ch2 width as requested ±1 ms; pulsed: 20 pulses, period 50 ±2 ms, width 5 ±2 ms |
| b | Task: `fraction 1.0`, `stim_onset {onset 0, duration 0.5}`, every other window off, photodiode sync **on**; a short session | ch2 rises 0.2–1 ms after ch1's first edge after SHOW; `opto_lat_ms` median < 1 ms, max < 3 ms on `sync_ok == 1`; **zero** trials with `display_latency_s < 0.045` |
| c | Same with `onset −0.05` | `opto_lat_ms ≈ −50 ± 25` (display jitter); `opto_window_anchor` reads `stim_onset:pred` |
| d | Same with `pattern pulsed 20 Hz / 5 ms`, `duration 1.0` | the train of (a) on the scope, locked to ch1 |
| e | Same as (b) with photodiode sync **off** | `opto_lat_ms` spread ≈ `L − display_latency_s` (tens of ms); `stim_onset:fallback` |
| f | STOP in the middle of a window; then `kill -9` the engine in the middle of a window | ch2 LOW within 50 ms of STOP; after the kill note the pin level — teardown's `/api/opto_off` must clear it |
| g | `fraction 0.5` with `mask: true` | unlit rows: `opto_on = 0`, `opto_onset_t` NaN, and a mask record on every trial (`opto_mask_on = 1`) |

Afterwards: fill the status table below with the measured numbers and the date, and record the
kill -9 pin level in [LEADER_WIRING.md](LEADER_WIRING.md).

## Status

| Item | State (2026-11-06) |
|---|---|
| Code | built on branch `dev-opto` (commits 23e46cb, 0ac47ee, c324b39); in `main` since 2026-11-12 |
| Software verification | `tools/test_opto.py` (fake lgpio: pattern math, spec validation, edge lock, fallback, lead, drop, reward guard, pulsed train, mask-only, cancel, close; generator statistics over 60 seeds; HDF5 round trip; consolidate + `analysis.loaders.Session`) — 0 failures · `tools/smoke_multirig.py` PASS · pyflakes clean · both pages' JavaScript parses after Jinja rendering |
| Hardware | **nothing has run on a Pi**; no laser is wired on any rig; bench rows (a)–(g) pending |
| Rig YAMLs | cheddar, morbier, pecorino carry `devices.opto` **disabled** with `opto` on the leader's device list; nothing changes until the card is enabled in Setup |
| Before the first opto session | Deploy on the rig (new engine + device), enable the device (Save Rig + Initialize), run the bench recipe |
