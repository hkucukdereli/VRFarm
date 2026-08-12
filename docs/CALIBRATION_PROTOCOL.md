# Rig Calibration Protocol

**Rig:** cheese — display on the Follower (RPi4 + DLP **rear-projector**) + parabolic screen; reward valve on the Leader
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

The geometric warp and the luminance correction are both stored in `warp_map.npz` and referenced by every experiment. The reward calibration is a small ms→µL table stored per-rig in the rig JSON.

Everything is driven from the **setup UI** (`setup/app.py`, localhost:4999): the display card (RENDERING / TESTS / **CALIBRATION** / GEOMETRY sub-sections) and the reward card. The command-line tools still exist for manual runs.

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
- Projector X is up on the follower (`~/rig/start_projector.sh`)

### Step 1 — Calibrate the geometry (landmark registration)

Geometry is no longer hand-tuned from abstract stretch factors. The **`calib_geo.py`** landmark tool registers the projected grid to physical reality and back-solves the geometry.

In the setup UI, display card → **CALIBRATION → "Calibrate"**. This re-inits the projector, deploys the calibration tools to the display Pi, launches `calib_geo`, and opens its live sliders at `http://<display-pi>:5091` (`setup/app.py:api_start_calibration`; a `calibration_probe`, if configured, is latched TTL HIGH while calibrating).

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

In the setup UI, display card → **GEOMETRY**: pick the geometry **File** in the dropdown, then click **"Generate Warp"** / **"Regenerate Warp"** (`setup/app.py:api_generate_warp`). This ray-traces the selected `rig_geometry.yaml` into `warp_map.npz` **on the controller**, atomically copies the NPZ *and* the geometry to every Pi, and reloads the live display. The luminance mode baked in is the rig's stored `display.luminance_correction` (Part 2). The status line shows whether `warp_map.npz` is present on the Leader.

Manual/CLI equivalent (on the controller, in `display_calibration/`):

```bash
conda activate vrfarm
python compute_warp_map.py --validate            # → warp_map.npz + warp_map_validation.png
python compute_warp_map.py --geo rig_geometry.yaml --lum-mode theoretical
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
DISPLAY=:0 ~/miniforge3/envs/rig/bin/python validate_calibration_pygame.py \
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

## Part 2 — Luminance (Intensity) Correction

### What it does

Compensates for the fact that the projector delivers less effective luminance to oblique screen positions. Without correction, a stimulus at 80° azimuth appears dimmer than the same stimulus at 0°, making apparent contrast location-dependent.

The correction is a **per-azimuth gain curve** baked into `warp_map.npz`. It is applied **per pixel at render time** by the Follower's renderer (`devices/display.py:_build_corr_map`) over the **whole field** — background, stimulus, and ITI blank alike. Stimulus generation therefore stores the *uncorrected* headroom fraction (`shared/stim_generator.py:generate_stimuli`); the renderer equalizes delivered luminance. **Which curve is applied is chosen by a mode**, stored per-rig at `rig.devices.display.luminance_correction` and baked into the warp as the `lum_correction_mode` key. (`shared/stim_generator.py:get_luminance_correction` — a scalar version of the same curve — backs the experiment-UI "Correct" button, not stimulus generation.)

### The four modes

Set in the **setup UI → display card → CALIBRATION → "Intensity" dropdown**, applied with the **"Intensity Cal"** button:

| Mode | Dropdown option | What it does |
|---|---|---|
| **auto** | `auto (measure)` | Measure per-azimuth luminance with the meter; the panel **auto-advances** to the next azimuth after each reading. |
| **manual** | `manual (measure)` | Same measurement, but you **Show** each azimuth explicitly and can jump around / redo. |
| **theoretical** | `theoretical` | No measurement — apply the geometric projector-incidence model (monotone falloff). The default / fallback. |
| **none** | `none` | No correction (flat, unity gain). |

`auto` and `manual` both produce the stored mode **`empirical`** (`setup/app.py:api_lum_apply`). Choosing `theoretical`/`none` and clicking Intensity Cal applies immediately (rebuild + redeploy the warp). The active mode persists via **Save Rig**.

### Equipment (empirical modes)

- **Thorlabs PM100D** power meter (or any linear-in-luminance meter / spot photometer). Absolute units don't matter — the gain is normalized to 1.0 at center, so a power reading in **W** works as well as cd/m². Plug the console into the **controller** for automated reads (`setup/app.py:_read_pm100d` uses `pyvisa` + `ThorlabsPM100`; the panel silently falls back to manual entry if the driver or meter is missing); otherwise just type the reading off the meter's display.
- Projector on and warmed up (≥15 minutes), room lights off, mouse out of the setup.

### Procedure (auto / manual)

1. Load Rig → **Initialize the display**.
2. Display card → CALIBRATION → set **Intensity = auto** (or manual) → click **Intensity Cal**. A measurement panel opens: one row per azimuth (**0, 20, 40, 60, 80, 100°**) and a patch **size** (default **15°** — big enough to overfill the sensor aperture).
3. For each azimuth a **white patch** lights on the projector at that location — rendered **raw** (`apply_lum:false`, contrast 1 on a black background) so the meter reads the true delivered luminance the fit is built from. Aim the meter at it, then click **Read** (auto-fills from the PM100D) or type the value. In **auto** mode the panel advances to the next azimuth automatically; in **manual** you click **Show** on the next row.
4. When at least 2 azimuths are read, click **Fit & Apply**. This fits the gain curve (`fit_luminance_correction.fit_luminance`), writes `luminance_cal_latest.yaml`, rebuilds `warp_map.npz` with `--lum-mode empirical`, and ships it to the Pis (+ reloads the live display).

Because the correction rides the normal warp pipeline, it **survives Regenerate Warp**: `compute_warp_map.py --lum-mode empirical` re-injects `luminance_cal_latest.yaml` on every build, so it is never clobbered.

### What good output looks like

- Readings **descend toward the edges** (edges are dimmer).
- The fitted gain is a smooth curve, 1.0 at center → lower at the edges.
- The **correction = min(gain)/gain** *attenuates* rather than boosts: it is **1.0 (full drive) at the dimmest/outermost azimuth** and **< 1 (darker) at center**, so the bright center is dimmed down to match the edges. Delivered luminance (`drive × gain`) is then uniform across azimuth, and nothing ever clips.

> **Full-field, per-column correction.** The correction is applied **per pixel at render time** to the **whole field — background, stimulus, and the ITI blank alike** (all pixels in an azimuth column get the same factor `C(az) = min(gain)/gain`; altitude doesn't matter). Because the background and stimulus scale together, this equalizes **absolute delivered luminance** *and* keeps **Weber/Michelson contrast** uniform — you get both. The only cost is peak brightness: **Bg = 1 is the brightest *uniform* level**, limited by the dimmest (outermost) azimuth. Set Intensity = **none** only if you want the raw, non-uniform projector output.

### Legacy CLI (optional, on the follower)

The old standalone scripts still exist: `display_test_patches.py` (PsychoPy patch stepper, hand-typed cd/m²) → `fit_luminance_correction.py`. The fit accepts either the neutral `reading` field or the legacy `luminance_cdm2`. The setup-UI flow is preferred — no PsychoPy, automated meter, and it deploys for you.

---

## Display gray scale & contrast metric

These are not calibration steps, but they set the units the calibrated screen is *driven* in, so they belong here.

### 0..1 gray scale

Display gray / blank is plain **0..1** luminance drive — **0 = fully dark, 1 = fully bright** — clamped to that range (`devices/display.py:blank_with_gray`). It is **not** the PsychoPy `[-1, 1]` scale. The stimulus `background_gray` (0..1) and the TESTS **Blank → Bg** field both use it. With a warp loaded, even a uniform blank is luminance-corrected per pixel so its *delivered* luminance is uniform across azimuth.

### Contrast metric (Weber / Michelson / Normalized)

Contrast in a task YAML's `stimulus.contrast.values` is interpreted in a **selectable metric**, set per-rig at `rig.devices.display.contrast_metric` via the display card **RENDERING → "Contrast metric"** dropdown:

- **Weber** (default) — `C = (L_stim − L_bg) / L_bg`
- **Michelson** — `C = (L_stim − L_bg) / (L_stim + L_bg)`
- **Normalized** — the raw headroom fraction `f`, where `L_stim = L_bg + f·(1 − L_bg)`

Conversions live in `shared/stim_generator.py` (`metric_to_fraction` / `fraction_to_metric`). Weber and Michelson divide by the background, so at a near-black or near-white background they degenerate and fall back to the normalized identity (`metric_degenerate`). The generator rounds each authored contrast **down to the nearest 8-bit-exact luminance code** (`snap_contrast_to_bitcode`) so the reported value matches what the renderer shows. The setup card's TESTS **"Correct"** button (and the experiment-UI equivalent) clamp a test contrast to the uniform-field ceiling at the current Bg in the active metric (Weber `(1−bg)/bg`, Michelson `(1−bg)/(1+bg)`, Normalized `1`).

---

## Part 3 — Reward (Valve) Calibration

The reward valve (`devices/reward.py`, on the Leader) maps **pulse duration (ms) → dispensed volume (µL)** via a small calibration table. The old automated routine (`devices/reward_calibration.py`) is **deprecated**; the editable table in the setup UI is the default workflow.

### Where it lives

Setup UI → **reward card**. The calibration is an editable **ms / µL** table (per-pulse volume), stored in the rig JSON at `rig.devices.reward.calibration.main = [[ms, µL], …]`.

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
├── fit_luminance_correction.py     # Luminance fit — fit_luminance() reused by the setup UI
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
│     lum_correction_mode (str)    empirical | theoretical | none — which curve the
│                                  runtime applies (get_luminance_correction / _build_corr_map)
│     lum_az           (N,)         Azimuth points for the theoretical luminance arrays
│     lum_gain_theoretical (N,)     Projector-incidence falloff (monotone) — the fallback
│     lum_az_empirical    (N,)      Present only in empirical mode (measured)
│     lum_gain_empirical  (N,)      Measured gain (empirical mode)
│     lum_correction_empirical (N,) min(gain)/gain correction factor (empirical mode)
│
├── luminance_cal_YYYY-MM-DD.yaml   # Fitted correction (dated)
├── luminance_cal_latest.yaml       # Symlink → most recent (re-injected on every warp build)
└── warp_map_validation.png         # Last --validate plot
```

Reward calibration is **not** a file here — it lives in the rig JSON under `devices.reward.calibration`.

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
python compute_warp_map.py --geo rig_geometry.yaml --lum-mode theoretical

# On-projector visual validation (on the follower, projector X up):
DISPLAY=:0 ~/miniforge3/envs/rig/bin/python validate_calibration_pygame.py \
    --warp ~/rig/calibration/warp_map.npz [--flip-h] [--flip-v]

# ── Luminance (intensity): PREFERRED path is the setup UI ───────────────────────
#   Display card → CALIBRATION → Intensity dropdown (auto / manual / theoretical /
#   none) → "Intensity Cal" button.

# Rebuild the warp with a chosen luminance mode (what the UI does under the hood):
python compute_warp_map.py --lum-mode empirical             # or theoretical / none

# Legacy CLI (on the follower): measure with a photometer, then fit:
DISPLAY=:0 python display_test_patches.py
python fit_luminance_correction.py [luminance_measurements_YYYY-MM-DD.yaml]

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

**Intensity Cal / Read does nothing or errors:**
The display must be **Initialized** first (the patch is shown through the live renderer). For automated reads the PM100D must be USB-connected to the controller with `pyvisa`/`ThorlabsPM100` installed — otherwise the panel still works with **manual entry** (type the meter's displayed value).

**Reward volume is off / drifts:**
Re-weigh at a few durations and update the ms/µL table. Use ≥2 rows so the engine interpolates instead of proportionally scaling from a single point, and make sure the **first** row is the base pulse you want count-mode rewards to repeat.
