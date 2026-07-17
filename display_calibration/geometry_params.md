# Geometry Parameters Reference

All parameters are in `rig_geometry.yaml` in this folder (`display_calibration/`).

The screen is a parabolic **cylinder**: cross-section from above follows `y = A - B*x²`,
with the mouse eye at the origin (x = lateral, y = depth/forward, z = up). It is curved
horizontally and straight vertically.

## Landmark calibration workflow (`calib_geo.py`)

Rather than eyeballing stretch factors, you register the projected grid to the physical
screen and the tool back-solves the geometry:

1. **frame_x / frame_y** — slide the thin cyan boundary lines onto where the projector
   light actually meets the screen. The enclosed rectangle = the usable pixels; it gates
   which pixels the warp map treats as on-screen.
2. **offset_x / offset_y** — move the green 0° cross (vertical = 0° azimuth meridian,
   horizontal = 0° altitude / eye level) onto physical straight-ahead. That intersection
   is the coordinate origin.
3. **azimuth_height** — measure IRL from the horizontal green line to the screen bottom
   and type it in. With `height_cm` and `parabola_A` it *derives* the visual field.
4. **az90_x** — drag the green ±90° lines onto the physical 90° marks. The tool rescales
   `horizontal_stretch` so the model projects ±90° exactly there, and `vertical_stretch`
   so the altitude range fills the usable area.
5. **parabola_B** — tune only the mid-field curvature against the cyan 30° grid; the ±90°
   and altitude endpoints stay anchored.

Save overwrites `rig_geometry.yaml` (solved stretches, derived altitudes, `azimuth_height`,
and a `calibration:` block) and `compute_warp_map.py` builds `warp_map.npz` from it.

## Screen

| Field | Unit | Meaning |
|-------|------|---------|
| `parabola_A` | cm | Depth of screen at its closest point (center, 0° azimuth). Eye→vertex distance. |
| `parabola_B` | 1/cm | Curvature of the parabola. Higher = more curved. Tune against the 30° grid. |
| `height_cm` | cm | Physical height of the screen. |
| `azimuth_height` | cm | IRL distance from the 0° azimuth line (eye level) to the screen bottom. Measured. **Drives** the altitudes below. |
| `altitude_min_deg` | ° | *Derived*: `atan(-azimuth_height / parabola_A)` — the screen bottom (below eye). |
| `altitude_max_deg` | ° | *Derived*: `atan((height_cm - azimuth_height) / parabola_A)` — the screen top (above eye). |

`azimuth_max_deg` is **gone** — the horizontal extent now comes from the registered ±90°
landmark and the usable-pixel frame, not a stored max azimuth.

## Projector

| Field | Unit | Meaning |
|-------|------|---------|
| `throw_ratio` | — | Throw distance / image width. Standard projector spec. |
| `throw_distance_cm` | cm | Total optical path length from lens to screen center (including mirror folds). |
| `optical_axis_elevation_deg` | ° | Elevation angle where the image center hits the screen, from horizontal at the eye. |
| `lens_offset_vertical` | fraction | Vertical lens shift as a fraction of image height. 0 = symmetric, 1.0 = 100% upward. |
| `lateral_offset_cm` | cm | Horizontal offset of projector from mouse midline. 0 = centered. |
| `horizontal_stretch` | — | Anamorphic horizontal scale. **Solved** from the ±90° landmark (not hand-tuned). |
| `vertical_stretch` | — | Anamorphic vertical scale. **Solved** so the altitude range fills the usable area. |

## Calibration (orientation + landmarks)

Written by `calib_geo.py`; carried into `warp_map.npz` so the runtime renderer reproduces
what was tuned.

| Field | Unit | Meaning |
|-------|------|---------|
| `flip_h` / `flip_v` | bool | Framebuffer mirror to match the physical projector mounting. |
| `offset_x` / `offset_y` | px | Shift placing the 0° origin cross at physical straight-ahead. |
| `frame_x` / `frame_y` | px | Symmetric left/right and top/bottom insets marking the usable-pixel rectangle. |
| `az90_x` | px | Display column of the +90° azimuth landmark line (the ±90° registration). |

All projector-drawn colors are B/G only (R = 0) — the red LED is deactivated. Green = 0°
vertical, 0° horizontal, ±90° verticals; cyan = every other 30° line and the frame.
