# Display / screen calibration

Standalone tools for calibrating the parabolic projection screen, plus the warp-map
generator the experiment uses. The single source of truth is **`rig_geometry.yaml`**
(see [geometry_params.md](geometry_params.md) for every field). The Rig Setup UI loads
the same file into editable boxes; `cheese.yaml` points a rig at its file via
`display.geometry_file`.

```
rig_geometry.yaml ──► compute_warp_map.py ──► warp_map.npz  (used by the experiment)
        ▲  ▲
        │  └── Rig Setup UI: edit boxes, then "Save Rig"
        └───── calib_geo.py: live tuning on the projector, "Save" overwrites it
```

## Files

| File | What it is |
|------|------------|
| `rig_geometry.yaml`      | The geometry params (screen, projector, mouse, luminance). **Edited by both the live tool and the setup UI.** |
| `calib_geo.py`           | **Live geometry tool** — slider panel on your Mac, isolines drawn on the projector, Save overwrites `rig_geometry.yaml`. |
| `compute_warp_map.py`    | Builds `warp_map.npz` from `rig_geometry.yaml`. Run by the setup UI's *Generate Warp*. |
| `panel_grid.py`          | Numbered panel grid (raw, no warp) for eyeballing coverage. |
| `validate_calibration_pygame.py` | Renders the finished warp grid on the projector to check it. |
| `cal_start.sh` / `cal_stop.sh` | Start/stop a tool on mozzarella (survives the SSH session). |
| `fit_luminance_correction.py`, `validate_calibration.py`, `display_test_patches.py` | Luminance fit + offline validation helpers. |

## The two ways to set geometry

**1. Type the numbers (Rig Setup UI).** Load the rig → the Display device card shows
editable boxes for screen / projector / mouse params (incl. `horizontal_stretch` /
`vertical_stretch`). Change any value and hit **Save Rig** — that writes both the rig
config (`cheese.yaml`) and the geometry (`rig_geometry.yaml`) in one click. Then
**Generate Warp** rebuilds `warp_map.npz` and pushes it (and `rig_geometry.yaml`) to the
Pis. Toggle **Apply warp** to switch the experiment between warped / unwarped rendering.

**2. Tune it live (calib_geo.py).** Needs the projector, so it runs on mozzarella. It
draws azimuth/altitude isolines *through* the screen+projector geometry, so the screen
**curvature** (`parabola_B`) is a live slider — the thing you can only set by eye.

```bash
# on mozzarella (projector X must be up):
bash ~/rig/calibration/cal_start.sh geo      # geometry sliders, web UI on :5091
#   then open http://192.168.10.102:5091 on your Mac
bash ~/rig/calibration/cal_start.sh panel    # numbered panel grid
bash ~/rig/calibration/cal_start.sh warp     # validate the finished warp grid
bash ~/rig/calibration/cal_stop.sh           # stop whatever is running
```

Tune **`parabola_B`** until the cyan horizontal lines are straight/level on the curved
screen, then click **Save** — that overwrites `rig_geometry.yaml` *next to the script on
the Pi*.

> The `flip_h/flip_v/offset_x/offset_y` sliders only orient the live **preview** to match
> the physical projector; the experiment render path ignores them, so they are **not**
> written into `rig_geometry.yaml` (they persist to `.calib_preview.json` instead).

## Mac ↔ Pi sync

`rig_geometry.yaml` in this folder (on the Mac, in the repo) is the master copy.

- **Mac → Pi:** *Generate Warp* copies `warp_map.npz` **and** `rig_geometry.yaml` to each
  Pi's `~/rig/calibration/`.
- **Pi → Mac:** after live-tuning with `calib_geo.py`, copy the tuned file back:
  ```bash
  scp vruser@192.168.10.102:~/rig/calibration/rig_geometry.yaml display_calibration/
  ```
  then reload the rig in the setup UI (or just re-open the Display card) to see the new
  values, and **Generate Warp** to bake them in.

First-time only: deploy the tool scripts to the Pi if they aren't there yet —
`scp calib_geo.py panel_grid.py validate_calibration_pygame.py cal_start.sh cal_stop.sh
vruser@192.168.10.102:~/rig/calibration/`.
