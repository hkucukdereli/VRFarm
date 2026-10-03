# Rig Calibration Protocol

**Rig:** `rigs/cheddar.yaml` (listed as `cheddar` in the UI dropdowns) — display on the Follower (RPi4 + DLP **rear-projector**) + parabolic screen; reward valve on the Leader
**Last updated:** 2026-08-03
**Files:** `display_calibration/` in the repo (on the controller); `~/rig/calibration/` on the follower

---

## Overview

Calibration has three independent parts that can be done separately:

| Part | What it does | When to redo |
|---|---|---|
| **Geometric warp** | Maps visual angles (azimuth, altitude) to projector pixels | If projector or screen moves |
| **Luminance (intensity) correction** | Equalizes delivered luminance across screen locations | If bulb ages, screen is replaced, or geometry changes |
| **Reward valve** | Maps pulse duration (ms) to dispensed volume (µL) | If the valve, tubing, or reservoir head changes |

The geometric warp and the luminance correction are both stored in `warp_map.npz` and referenced by every experiment. The reward calibration is a small ms→µL table stored per-rig in the rig YAML.

Everything is driven from the controller's **Setup tab** (`controller/setup.py`, localhost:5000; since the
multi-rig controller each rig's calibration files live in `display_calibration/<rig>/` — see `docs/MULTI_RIG.md`): the display card (RENDERING / TESTS / **CALIBRATION** / GEOMETRY sub-sections) and the reward card. The command-line tools still exist for manual runs.

> **Rear projection.** The projector sits **behind** the screen; the image is seen from the front. The warp therefore mirrors the frame — `flip_h` / `flip_v` in the geometry `calibration:` block. The projector model (`compute_warp_map.build_projector`) places the lens *behind* the screen looking back toward the eye; the flip must be **re-registered** (Part 1, Step 3) whenever the projector or its model changes.

---

## Part 1 — Geometric Warp Map

### What it does

The parabolic screen is curved, so a flat image from the projector produces distorted positions on the screen. The warp map computes, for any target visual angle (azimuth, altitude) as seen from the mouse eye, exactly which projector pixel to illuminate. This is computed analytically from the screen's parabola equation (`y = A − B·x²`) and the projector's rear-projection throw geometry, then mirrored to display space (`display_calibration/compute_warp_map.py`).

### Where it runs

The warp is **ray-traced on the controller** (Mac/Ubuntu, the miniforge `vrfarm` env has numpy/scipy/yaml/matplotlib) and the resulting `warp_map.npz` is deployed to both Pis — the Leader generates stimuli from it (`shared/stim_generator.py`), the Follower renders through it (`devices/display.py`). The *geometry* itself is calibrated interactively **on the follower** with the projector running.

### Prerequisites

- Projector is mounted in its final rear position and warmed up
- Screen and mouse platform are in their final positions
- `displayd` is running on the follower and healthy (`curl :5581/status` → `RENDERER_UP`).
  There is no X server: `cal_start.sh` asks displayd to stand aside (POST `:5581/standby`)
  and `cal_stop.sh` gives the display back (POST `/resume`). calib_geo runs on kmsdrm under
  the **system** `/usr/bin/python3` — the conda env's SDL has no kmsdrm backend.

### Step 1 — Calibrate the geometry (landmark registration)

Geometry is no longer hand-tuned from abstract stretch factors. The **`calib_geo.py`** landmark tool registers the projected grid to physical reality and back-solves the geometry.

In the setup UI, display card → **CALIBRATION → "Calibrate"**. This deploys the calibration tools to the display Pi, has displayd release the display, launches `calib_geo`, and opens its live sliders at `http://<display-pi>:5091` (`controller/setup.py:api_start_calibration`; a `calibration_probe`, if configured, is latched TTL HIGH while calibrating).

Register these landmarks against the physical screen (`display_calibration/calib_geo.py`):

1. **`frame_x` / `frame_y_top` / `frame_y_bottom`** — slide the cyan boundary lines onto where the projector light meets the physical screen edges. The enclosed rectangle is the **usable pixel area**. `frame_x` is a single symmetric left/right inset; top and bottom are independent. These stay tied to the physical top/bottom even with `flip_v` on.
2. **`offset_x` / `offset_y`** — move the green 0° cross (vertical = az 0° meridian, horizontal = alt 0° eye-level line) onto physical straight-ahead. That intersection is the coordinate origin.
3. **`azimuth_height` (cm)** — measure IRL from the horizontal green line to the screen bottom and type it in. With `height_cm` and `parabola_A` it **derives** the altitude range: `altitude_min = atan(−h/A)`, `altitude_max = atan((height−h)/A)`.
4. **`az90_x`** — drag the green ±90° azimuth lines onto the physical 90° marks. The tool **solves** `horizontal_stretch` so the model projects ±90° exactly there, and `vertical_stretch` so the altitude range fills the usable area.
5. **`parabola_B`** — tune only the mid-field curvature against the cyan 30° grid; the ±90° and altitude endpoints stay anchored.

Set **`flip_h` / `flip_v`** to whatever makes the whole image (including text) read correctly on the physical screen — that combination is the projector's rear-projection transform.

**Save** overwrites `rig_geometry.yaml` (the deliverable) with the solved geometry, the derived altitudes, and a `calibration:` block (flip / offset / frame / az90 landmarks). Then **"Stop Calib"**.

> The display card's **SCREEN / VISUAL SPACE / PROJECTOR** rows also expose the geometry numerically (A depth cm, B curve, height cm, throw ratio/cm, H/V stretch, axis elev°, lens offset, lateral offset) with live derived Alt/Az min/max read-outs. Edits there persist to the selected geometry file on **Save Rig** — but the landmark tool is the intended way to *produce* a calibration.

### Step 2 — Regenerate the warp map

In the setup UI, display card → **GEOMETRY**: pick the geometry **File** in the dropdown, then click **"Generate Warp"** / **"Regenerate Warp"** (`controller/setup.py:api_generate_warp`). This ray-traces the selected `rig_geometry.yaml` into `warp_map.npz` **on the controller**, atomically copies the NPZ *and* the geometry to every Pi, and reloads the live display. The light model baked in is the rig's intensity calibration (`devices.display.intensity_calibration`, default: the theoretical mock of the geometry) — see Part 2.

Manual/CLI equivalent (on the controller, in `display_calibration/`):

```bash
conda activate vrfarm
python compute_warp_map.py --validate            # → warp_map.npz + warp_map_validation.png
python compute_warp_map.py --geo <rig>/geometry/rig_geometry_<stamp>.yaml --intensity-cal <rig>/intensity/intensity_cal_<stamp>.yaml --cal-dir <rig>
```

`--validate` writes `warp_map_validation.png` with four panels:
1. **Azimuth map** — grades smoothly from red (−105°) through white (0°) to blue (+105°). No sharp discontinuities.
2. **Altitude map** — smooth gradient (viridis) bottom to top.
3. **Forward map — azimuth/altitude grid** — isolines at ±80° (red), ±40° (orange), 0° (white). Lines should look plausible given the screen shape.
4. **Luminance correction (theoretical)** — smooth *monotone* falloff from 1.0 at center toward the edges (projector-incidence model). This is the fallback; a measured curve replaces it when the mode is `empirical` (Part 2).

The console prints **visible-screen coverage** (% of pixels the warp fills); very low coverage means the geometry is off.

### Step 3 — Visual validation on the projector

Run on the follower with the projector on (`display_calibration/validate_calibration_pygame.py` — pygame, since the follower has no PsychoPy; the old `validate_calibration.py` used PsychoPy and never ran on the follower):

```bash
/usr/bin/python3 validate_calibration_pygame.py \
    --warp ~/rig/calibration/warp_map.npz [--flip-h] [--flip-v]
```

It draws azimuth/altitude isolines (from the inverse `az_map`/`alt_map`/`valid_map` that `display.py` actually renders with), orientation labels, and a large asymmetric "F". Find the `--flip-h`/`--flip-v` combination that makes the **entire image read correctly** on the physical screen — that combination is the projector's transform, and it is what you set as `flip_h`/`flip_v` in Step 1 so stimulus pixels land right. Patterns cycle with SPACE (or `--cycle SECONDS` headless); Q/ESC or SIGTERM to quit.

### `rig_geometry.yaml` — the calib_geo deliverable

```yaml
screen:
  parabola_A: 13         # vertex depth (cm) — eye→screen distance at 0° azimuth
  parabola_B: 0.055      # curvature, y = A − B·x²   (mid-field landmark)
  azimuth_height: 7.5    # cm from the alt-0 line to the screen bottom (measured IRL)
  height_cm: 20          # physical screen height
  altitude_min_deg: -29.98   # DERIVED from azimuth_height/A/height_cm
  altitude_max_deg: 43.88    # DERIVED
projector:
  resolution: [1920, 1080]
  throw_ratio: 1.2
  throw_distance_cm: 48
  optical_axis_elevation_deg: -9.0     # beam elevation (negative = up)
  horizontal_stretch: 0.658            # SOLVED from az90_x landmark
  vertical_stretch: 1.140              # SOLVED so altitude fills the usable area
  lens_offset_vertical: 1              # 0=center … 1=100% upward shift
  lateral_offset_cm: 0
calibration:                # landmark block — set by calib_geo, do NOT hand-edit
  flip_h: false
  flip_v: true
  offset_x: 0
  offset_y: -405
  frame_x: 207
  frame_y_top: 4
  frame_y_bottom: 0
  az90_x: 1628
```

> `horizontal_stretch` / `vertical_stretch` are **solved** from the landmarks, not typed. `altitude_min_deg` / `altitude_max_deg` are **derived** from `azimuth_height`, `parabola_A`, `height_cm`. Older files carried an `azimuth_max_deg` — `calib_geo` drops it (the filled azimuth range is now derived from where the frame edges land on the parabola).

---

## Part 2 — Intensity Calibration (light model)

### What it does

The projector delivers less light to oblique screen positions, its light is not proportional to
its drive level, and a drive of 0 still leaves a black floor (DMD leakage + stray light). One
intensity calibration measures all three, and a light model built from it
(`shared/intensity_model.py`) lets every part of the system work in **light** rather than drive:

- the background is uniform in light across the screen,
- **Bg / background_gray is a brightness**: 0..1 of the uniform range (0 = the darkest light
  every azimuth can show, 1 = the brightest),
- contrast is computed on light (Weber, Michelson or normalized), including the floor — on a
  "black" background Weber contrast is finite and large.

The model is baked into `warp_map.npz` (arrays `int_*`) by Generate Warp / Apply and used, from
that one file, by the renderer (per-pixel drive), the Leader (stimulus generation) and the
controller (previews, Correct).

### One file, one altitude

`display_calibration/<rig>/intensity/intensity_cal_<stamp>.yaml`
(format: `display_calibration/_template/intensity/intensity_cal_template.yaml`):

| Part | What you measure |
|---|---|
| Column 1 — `azimuth_sweep` | light at **full drive** at each azimuth row you choose; measure out to the screen edge (and both sides of centre to check symmetry) |
| Level columns — `level_sweeps` | light at drive 1.0, 0.9 … 0.1, 0.05, 0.0 at an azimuth you set per column; add as many columns as you want (e.g. 0°, 45°, 85°) |

The model: `L(az, v) = F(az) + (M(az) − F(az)) · h(v; az)`. `M` is column 1's shape re-anchored to
each level column's drive-1 reading, `F` the level columns' floors, `h` their normalized
responses; all interpolated in |az| between columns and held constant beyond. The uniform range is
`max F .. min M` over the calibrated azimuths.

**Mocks.** Without a measurement the rig uses a mock in the same format, with a straight
response (drive 0 = 0, drive 1 = 1) and no floor: `intensity_cal_<geostamp>_theoretical.yaml`
(column 1 = the geometry model's cos(incidence) gain — the default, identical to the old
theoretical correction) or `intensity_cal_none.yaml` (flat — brightness is the drive). Both are
written automatically for the current geometry.

### Procedure

1. Load the rig, **Initialize** the display. Projector warmed up (≥ 15 min), room dark, meter
   (Thorlabs PM100D, or any linear meter) on a stand.
2. Display card → **INTENSITY → Measure…**. Set **Alt** (used for the whole calibration),
   **Size** (default 15°, overfills the sensor) and **Unit** (a label, e.g. lux).
3. Column 1: edit/add azimuth rows (defaults 0, ±20 … ±100). For each row press **▶** (the patch
   lights at full drive, raw) and **R** (PM100D) or type the value. R advances to the next empty row.
4. Level columns: set each column's azimuth in its header, **+ column** for more. ▶ / R per cell;
   rows are the drive levels, 0.0 being a black patch (the floor).
5. **Save** writes the file and logs a fit report (uniform range, how much the column shapes
   differ, left/right asymmetry). **Save & Apply** also applies it.
6. **Apply** (or pick any file in the **Calibration** dropdown, then Apply) rebuilds
   `warp_map.npz` with that file's light model, deploys it to the Pis, reloads a live display and
   records the choice in the rig YAML (`devices.display.intensity_calibration`, saved at once).

### What good output looks like

- Column 1 descends toward the edges; the L/R asymmetry is a few percent.
- Every level column rises monotonically from its floor to its drive-1 reading.
- Shape spread small (a few %) means one response curve fits the whole screen; large means the
  interpolation between columns is doing real work — measure a column near each azimuth you use.
- Check after Apply: TESTS → Blank at a mid brightness and read the meter at a few azimuths;
  the light should match within a few percent.

### Gray scale & contrast metric

- **Bg / `stimulus.background_gray` = brightness**, 0..1 of the uniform range (`L = Lo + b·(Hi − Lo)`).
  With the none mock it is the plain drive; with the theoretical mock it is the old corrected drive.
- **Contrast** (`stimulus.contrast.values`, typed in percent in the UI, stored as fractions) is
  defined on light, in the rig's metric (display card RENDERING → Contrast metric):
  Weber `(Ls − Lb)/Lb`, Michelson `(Ls − Lb)/(Ls + Lb)`, normalized `(Ls − Lb)/(Hi − Lb)` (fraction of
  the uniform headroom). Weber/Michelson on a background without light (a mock at brightness 0)
  fall back to normalized.
- A stimulus may be brighter than the uniform ceiling where the screen can deliver it; past
  drive 1 a pixel clips. **Correct** clamps to what every session azimuth can show.

---

## Part 3 — Reward (Valve) Calibration

The reward valve (`devices/reward.py`, on the Leader) maps **pulse duration (ms) → dispensed volume (µL)** via a small calibration table. The old automated routine (`devices/reward_calibration.py`) is **deprecated**; the editable table in the setup UI is the default workflow.

### Where it lives

Setup UI → **reward card** (see [SETUP_UI.md](SETUP_UI.md#reward-valve)). The calibration is an editable **ms / µL** table (per-pulse volume), stored in the rig YAML at `rig.devices.reward.calibration.main = [[ms, µL], …]`.

### Procedure

1. Init the reward device (Load Rig / Init Devices) so the **Deliver** button is enabled.
2. For each pulse duration you want to characterize, deliver a **known number of pulses** at that duration (the card's **Deliver** control fires the pulse `×N` at an `every … s` interval; count/train uses `pulse_gap_ms` between pulses).
3. Collect and **weigh** the dispensed water (1 µL ≈ 1 mg), divide by the pulse count → **µL per pulse**.
4. Enter the `ms` and per-pulse `µL` into a table row (**+ Row** / **− Row** to add/remove), then **"Save Calibration"** — this writes the table into the rig config (`applyCalibration` → Save Rig).

### How the engine uses it (`devices/reward.py`)

- **One calibration row** → proportional scaling (e.g. `[100, 4]` → 25 ms/µL).
- **Two or more rows** → linear interpolation with extrapolation (`load_calibration`, scipy `interp1d`).
- **Volume mode** (`amount_mode: volume`) — one pulse, its duration interpolated for `amount_ul`.
- **Count mode** (`amount_mode: count`) — `amount_count` repeats of the **base pulse** (the **first** calibration row), separated by `pulse_gap_ms`.

Aim for ≥3–4 well-spaced rows spanning the durations you actually deliver, so the interpolation is accurate over the working range.

---

## Calibration File Reference

```
display_calibration/                 (on the controller; deployed to ~/rig/calibration/ on the Pis)
├── rig_geometry.yaml               # Physical geometry + landmark block — the calib_geo deliverable
├── calib_geo.py                    # Interactive landmark geometry calibrator (sliders on :5091)
├── cal_start.sh / cal_stop.sh      # Launch/stop the on-Pi calibration tool
├── panel_grid.py                   # Projector panel grid helper (deployed with the tools)
├── compute_warp_map.py             # Ray-traces rig_geometry.yaml → warp_map.npz
├── validate_calibration_pygame.py  # On-projector warp validator (pygame; --flip-h/--flip-v)
├── validate_calibration.py         # Legacy PsychoPy validator (does not run on the follower)
├── display_test_patches.py         # Legacy CLI luminance patch stepper (setup-UI flow preferred)
├── intensity_cal.py                # intensity calibration files: save / mocks / list / load
│
├── warp_map.npz                    # Generated — used by all experiments
│   contains:
│     az_map           (H×W)        Azimuth in degrees for each pixel
│     alt_map          (H×W)        Altitude in degrees for each pixel
│     valid_map        (H×W bool)   True where pixel hits screen
│     px_from_az       (Nalt×Naz)   Pixel X for each (az, alt)
│     py_from_az       (Nalt×Naz)   Pixel Y for each (az, alt)
│     az_samples       (Naz,)       Azimuth sample points
│     alt_samples      (Nalt,)      Altitude sample points
│     lum_az, lum_gain_theoretical  Geometry gain (diagnostics/plots only)
│     int_*                         The light model (shared/intensity_model.py): floor F,
│                                   max light M, inverse responses, uniform range lo/hi,
│                                   calibration name — used by renderer, Leader, controller
│
└── warp_map_validation.png         # Last --validate plot
```

Since 2026-09-29 the data files are per rig — `display_calibration/<rig>/geometry/`,
`<rig>/intensity/` and `<rig>/warp_map.npz`; the tree above lists the scripts and the warp map's
contents. Layout and naming: [display_calibration/README.md](../display_calibration/README.md#where-the-files-live).

Reward calibration is **not** a file here — it lives in the rig YAML under `devices.reward.calibration`.

---

## When to Recalibrate

| Event | Geometric warp | Luminance | Reward |
|---|---|---|---|
| Projector moved or refocused | ✓ redo | ✓ redo | — |
| Screen moved or replaced | ✓ redo | ✓ redo | — |
| Projector bulb replaced | — | ✓ redo | — |
| Mouse platform height changed | ✓ redo | — | — |
| Valve / tubing / reservoir changed | — | — | ✓ redo |
| Routine check (every ~3 months) | — | ✓ spot check | ✓ spot check |

---

## Quick Reference — Command Cheat Sheet

```bash
# ── Geometry + warp: PREFERRED path is the setup UI ────────────────────────────
#   Display card → CALIBRATION → "Calibrate"  (landmark tool, sliders on :5091)
#   Display card → GEOMETRY   → "Regenerate Warp"  (builds warp_map.npz on the
#                                                   controller + deploys to the Pis)

# Manual warp build (on the controller, in display_calibration/, vrfarm env):
python compute_warp_map.py --validate                       # + validation plot
python compute_warp_map.py --geo <rig>/geometry/rig_geometry_<stamp>.yaml --intensity-cal <rig>/intensity/intensity_cal_<stamp>.yaml --cal-dir <rig>

# On-projector visual validation (on the follower, projector X up):
/usr/bin/python3 validate_calibration_pygame.py \
    --warp ~/rig/calibration/warp_map.npz [--flip-h] [--flip-v]

# ── Intensity: setup UI → Display card → INTENSITY → Measure… / Calibration + Apply
#   (Apply = compute_warp_map.py --intensity-cal <file> + deploy; no --intensity-cal = theoretical mock)

# ── Reward valve: setup UI → reward card → editable ms/µL table → "Save Calibration"
```

---

## Troubleshooting

**Warp coverage is very low (<50% of pixels):**
The projector geometry is off. Recheck `throw_distance_cm` and `optical_axis_elevation_deg`, and confirm the landmark frame in `calib_geo` sits on the real screen edges. Run `--validate` and check the azimuth map — it should cover most of the frame.

**Azimuth lines in validation look wrong:**
The parabola parameters may be off. Re-measure `parabola_A` (eye→screen distance at 0° azimuth) and re-tune `parabola_B` against the mid-field 30° grid in `calib_geo`.

**Whole image / text reads mirrored on the physical screen:**
Wrong flip. In `validate_calibration_pygame.py` find the `--flip-h`/`--flip-v` combination that makes the "F" and labels read correctly, then set `flip_h`/`flip_v` to match in `calib_geo` and Regenerate Warp (rear projection needs the mirror baked in).

**Luminance correction fit looks noisy:**
One or more meter readings were bad. Re-measure the outlier azimuths (in the intensity-cal panel, click that azimuth's **Read** again, or type a corrected value). The gain should be a smooth curve decreasing from 1.0 at center.

**Contrast patches still look uneven after correction:**
The meter reading may have included ambient light, or the patch didn't overfill the sensor. Turn room lights fully off, increase the patch **size**, and re-measure. Confirm the active mode is **`empirical`** (setup-UI status line), not `theoretical`/`none`.

**Measure… / R (read) does nothing or errors:**
The display must be **Initialized** first (the patch is shown through the live renderer). For automated reads the PM100D must be USB-connected to the controller with `pyvisa`/`ThorlabsPM100` installed — otherwise the panel still works with **manual entry** (type the meter's displayed value).

**Reward volume is off / drifts:**
Re-weigh at a few durations and update the ms/µL table. Use ≥2 rows so the engine interpolates instead of proportionally scaling from a single point, and make sure the **first** row is the base pulse you want count-mode rewards to repeat.
