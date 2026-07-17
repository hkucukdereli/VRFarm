# Geometry Parameters Reference

All parameters are in `rig_geometry.yaml` in this folder (`display_calibration/`).

## Screen

The screen is a parabolic cylinder. Cross-section from above follows `y = A - B*x²`,
where the mouse eye is at the origin, y = depth (forward), x = lateral.

| Field | Unit | Meaning |
|-------|------|---------|
| `parabola_A` | cm | Depth of screen at its closest point (center, 0° azimuth). Distance from mouse eye to screen vertex. |
| `parabola_B` | 1/cm | Curvature of the parabola. Higher = more curved. |
| `azimuth_max_deg` | ° | Screen spans this angle in each direction horizontally (±105° = 210° total). |
| `altitude_min_deg` | ° | Bottom edge elevation angle from mouse eye level (horizon). In the live calibrator (`calib_geo.py`) this is *derived* from **Eye above screen bottom** (h, cm): `altitude_min = atan(-h / parabola_A)`. |
| `altitude_max_deg` | ° | Top edge elevation angle. Screen covers altitude_min to altitude_max vertically. |
| `height_cm` | cm | Physical height of the screen. |

## Projector

| Field | Unit | Meaning |
|-------|------|---------|
| `throw_ratio` | — | Throw distance / image width. Standard projector spec. |
| `throw_distance_cm` | cm | Total optical path length from lens to screen center (including mirror folds). |
| `optical_axis_elevation_deg` | ° | Elevation angle where the image center hits the screen, measured from horizontal at the mouse eye. |
| `lens_offset_vertical` | fraction | Vertical lens shift as fraction of image height. 0 = symmetric throw, 1.0 = 100% offset (image projects entirely upward from lens axis). |
| `lateral_offset_cm` | cm | Horizontal offset of projector from mouse midline. 0 = centered. |
