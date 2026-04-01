# Parabolic Screen Calibration Protocol

**Rig:** Mozzarella (RPi4 + DLP projector) + parabolic screen  
**Last updated:** 2026-03-30  
**Files:** `calibration/` directory on mozzarella

---

## Overview

Calibration has two independent parts that can be done separately:

| Part | What it does | When to redo |
|---|---|---|
| **Geometric warp** | Maps visual angles (azimuth, altitude) to projector pixels | If projector or screen moves |
| **Luminance correction** | Equalizes stimulus contrast across screen locations | If bulb ages, screen is replaced, or geometry changes |

Both outputs are stored in `warp_map.npz` and referenced by every experiment.

---

## Part 1 — Geometric Warp Map

### What it does

The parabolic screen is curved, so a flat image from the projector produces distorted positions on the screen. The warp map computes, for any target visual angle (azimuth, altitude) as seen from the mouse eye, exactly which projector pixel to illuminate. This is computed analytically from the screen's parabola equation and the projector's throw geometry.

### Prerequisites

- Projector is mounted in its final position
- Screen is in its final position
- `rig_geometry.yaml` has been verified (see section below)

### Step 1 — Verify rig_geometry.yaml

Open `calibration/rig_geometry.yaml` and confirm all values match your physical rig:

```yaml
screen:
  parabola_A: 5.0          # vertex depth (inches) — distance from mouse eye to screen at 0° azimuth
  parabola_B: 0.125        # curvature — from the equation y = A - B*x²
  azimuth_max_deg: 105.0   # screen half-width in visual degrees

projector:
  resolution: [1920, 1080]
  throw_ratio: 1.2
  throw_distance_cm: 48.0        # measure from lens to screen center
  optical_axis_elevation_deg: 49 # angle of projector beam above horizontal
```

> **Measuring throw distance:** With the projector on, use a tape measure from the front of the lens to the screen surface at 0° azimuth (the center/deepest point of the parabola).

### Step 2 — Compute the warp map

Run on mozzarella:

```bash
cd ~/rig/calibration
conda activate rig
python compute_warp_map.py --validate
```

This takes 2–5 minutes. Output:
- `warp_map.npz` — the warp map arrays (used by all experiments)
- `warp_map_validation.png` — four plots for visual inspection

**What to check in the validation plots:**
1. **Azimuth map** — colors should grade smoothly from red (−105°) through white (0°) to blue (+105°). No sharp discontinuities.
2. **Altitude map** — smooth gradient from bottom to top.
3. **Azimuth/altitude grid** — vertical red lines should be at ±80° and ±40°, white line at 0°. Lines should look plausible given the screen shape.
4. **Theoretical luminance curve** — smooth falloff from 1.0 at center toward edges. This is replaced by measurements in Part 2.

### Step 3 — Visual validation on projector

Run on mozzarella with projector on:

```bash
DISPLAY=:0 python validate_calibration.py
```

**SPACE** cycles through patterns:
1. **Grid** — white azimuth lines every 20°, cyan altitude lines. Grid should look evenly spaced in visual angle, not physical distance.
2. **Azimuths** — colored lines at ±80° (red), ±40° (orange), 0° (white). These are your typical stimulus locations — verify they land where you expect on the physical screen.
3. **Contrast patches** — white squares at equal contrast across azimuths (uses luminance correction if available). Before correction these will look dimmer toward the edges.
4. **Test stimulus** — a white square at ±80° azimuth, 49° altitude — the typical stimulus position. Verify it appears at the correct location.

> **Q / ESC** to quit.

---

## Part 2 — Luminance Correction

### What it does

Compensates for the fact that the projector delivers less effective luminance to oblique screen positions. Without correction, a stimulus at 80° azimuth appears dimmer than the same stimulus at 0°, making apparent contrast location-dependent.

The correction measures actual luminance at multiple azimuth positions and fits a smooth gain curve. At stimulus generation time, the contrast value for each stimulus is divided by the local gain so that delivered contrast is uniform across the screen.

### Equipment needed

- **Photometer** (e.g. Konica Minolta LS-100, Minolta CS-100, or similar spot photometer)
- Projector on and warmed up (≥15 minutes)
- Room lights off
- Mouse body/head not in the setup (you're measuring the screen directly)

### Step 1 — Display test patches

```bash
DISPLAY=:0 python display_test_patches.py
```

The script shows a small white square on a gray background. Controls:

| Key | Action |
|---|---|
| **LEFT / RIGHT** | Step through azimuths (0°, 20°, 40°, 60°, 80°, 100°) |
| **UP / DOWN** | Step through altitudes (bottom, center, top of screen) |
| **SPACE** | Record measurement (you'll be prompted for the reading) |
| **S** | Save measurements to file |
| **Q / ESC** | Quit and save |

### Step 2 — Measurement procedure

1. Start at **0° azimuth, center altitude** — this is your reference point.
2. Position the photometer so it points at the white patch on the screen.
3. Wait for a stable reading (1–2 seconds).
4. Press **SPACE**, type the value in cd/m², press Enter.
5. Move to **20° azimuth** (RIGHT arrow), repeat.
6. Continue through 40°, 60°, 80°, 100°.
7. Repeat for each altitude row (UP/DOWN) — measure all three rows at each azimuth.
8. Press **S** to save.

> **Important:** Keep the photometer at a fixed position relative to the screen for all measurements. The patch is small (~0.5° visual angle) — make sure the photometer aperture covers it fully.

> **Tip:** Measure both sides (positive and negative azimuth) if the projector is not perfectly centered. The script currently only shows positive azimuths; for a symmetric rig the negative side correction is mirrored.

### Step 3 — Fit correction curve

```bash
python fit_luminance_correction.py
```

This automatically uses the most recent measurement file. Output:
- Updates `warp_map.npz` with empirical correction data
- Saves `luminance_cal_YYYY-MM-DD.yaml` with the correction values
- Updates symlink `luminance_cal_latest.yaml`
- Saves a plot of the gain curve and correction factors

**What to check in the output plot:**
- Left panel (gain): should be a smooth curve from 1.0 at 0° downward. Non-monotonic bumps suggest a bad measurement — re-measure that azimuth.
- Right panel (correction): the inverse. At 80° this might be 1.5–2× — meaning a requested 25% contrast gets boosted to 37.5–50% luminance to compensate.

### Step 4 — Validate contrast uniformity

```bash
DISPLAY=:0 python validate_calibration.py --pattern contrast
```

After correction is applied, the white patches at all azimuths should appear equally bright. If the edges still look dimmer, the correction is underestimating the falloff — re-measure with more care, especially at 80° and 100°.

---

## Calibration File Reference

```
calibration/
├── rig_geometry.yaml               # Physical measurements — edit when rig changes
├── compute_warp_map.py             # Run to regenerate warp_map.npz
├── display_test_patches.py         # Run on projector to measure luminance
├── fit_luminance_correction.py     # Run after measurements to fit curve
├── validate_calibration.py         # Run on projector to visually verify
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
│     lum_az           (N,)         Azimuth points for luminance arrays
│     lum_gain_theoretical (N,)     Cosine falloff estimate
│     lum_az_empirical    (N,)      After measurement (if done)
│     lum_gain_empirical  (N,)      Measured gain (if done)
│     lum_correction_empirical (N,) 1/gain correction factor (if done)
│
├── luminance_measurements_YYYY-MM-DD.yaml   # Raw photometer readings
├── luminance_cal_YYYY-MM-DD.yaml            # Fitted correction (dated)
├── luminance_cal_latest.yaml                # Symlink → most recent
└── warp_map_validation.png                  # Last validation plot
```

---

## When to Recalibrate

| Event | Geometric warp | Luminance |
|---|---|---|
| Projector moved or refocused | ✓ redo | ✓ redo |
| Screen moved or replaced | ✓ redo | ✓ redo |
| Projector bulb replaced | — | ✓ redo |
| Mouse platform height changed | ✓ redo | — |
| Routine check (every ~3 months) | — | ✓ spot check |

---

## Quick Reference — Command Cheat Sheet

```bash
# All run from: ~/rig/calibration with conda env activated

# Compute/recompute warp map (with validation plot)
python compute_warp_map.py --validate

# Visual validation on projector
DISPLAY=:0 python validate_calibration.py

# Measure luminance with photometer
DISPLAY=:0 python display_test_patches.py

# Fit correction from measurements
python fit_luminance_correction.py

# Fit from a specific measurement file
python fit_luminance_correction.py luminance_measurements_2026-03-28.yaml
```

---

## Troubleshooting

**Warp map shows very low coverage (<50% of pixels):**  
The projector geometry is off. Check `throw_distance_cm` and `optical_axis_elevation_deg` in `rig_geometry.yaml`. Try running with `--validate` and checking the azimuth map — it should cover most of the image.

**Azimuth lines in validation look wrong:**  
The parabola parameters may be off. Re-measure `parabola_A` (distance from mouse eye to screen at 0° azimuth) with a ruler.

**Luminance correction fit looks noisy:**  
One or more photometer readings were bad. Re-run `display_test_patches.py` and re-measure the outlier azimuths. The correction should be a smooth monotonic curve.

**Contrast patches still look uneven after correction:**  
The photometer measurement may have included ambient light. Ensure room lights are fully off and measure again.
