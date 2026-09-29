# DLPDLCR230NPEVM — simplified 3D model and hole map

A simplified, parametric model of the projector every VRFarm follower drives: the TI **DLP
LightCrafter Display 230NP EVM** (formatter board DLP038 + Young Optics FLA12-F engine on TI's
sheet-metal base). Boxes and cylinders only, but every part sits at TI's coordinates and every
screw hole is at TI's position, diameter and type. Made for designing mounts and enclosures.

| File | What |
|---|---|
| `dlpdlcr230npevm_simplified.step` | assembly STEP, one named solid per part, coloured |
| `dlpdlcr230npevm_simplified.stl` | the same as a mesh (viewers, slicers) |
| `dlpdlcr230npevm_simplified_iso.svg` | isometric line drawing |
| `dlpdlcr230npevm_hole_maps.png` | 2D hole maps: bottom, top, back wall, side wall |
| `dlpdlcr230npevm_dims.py` | **the numbers** — every dimension and hole, with what it is for |
| `build_model.py` | CadQuery script that builds and exports the model |
| `plot_hole_maps.py` | matplotlib script for the PNG |

## Frame

TI's own "DLP038" assembly frame, so the numbers can be checked against TI's files:
**X = depth** (lens points to −X, lens front at X = −15.37) · **Y = up** (the assembly stands on
Y = −4.82; PCB top at Y = 32.85) · **Z = width** (40-pin header along the −Z edge). For a Z-up CAD
system rotate −90° about X. Envelope **87.4 (X) × 48.2 (Y) × 81.4 (Z) mm**, matching TI's
assembly drawing (81.40 × 87.96 × 48.17).

## Holes, by part

Threads are listed as TI tapped them; the model draws them at tap-drill size (M1.6 → Ø1.25,
M2 → Ø1.60, M2.5 → Ø2.05), exactly as TI's STEP does. "CSK" = Ø2.90 through + Ø5.50 × 90°
countersink for an M2.5 flat-head screw.

**There is no dedicated external mounting hole on this EVM.** Every hole below is used by a TI
fastener. Bottom-side candidates for a rig mount are the three base tabs (occupied by flat-head
screws you can replace with longer ones), the bracket's wall edges (clamp), or the tapped M2.5/M2
wall holes (occupied; replace screws).

### MCH051 base (1.63 mm aluminium sheet, footprint 69.82 × 77.31, standing plane Y = −4.82)
| # | Type | Axis | Position (mm) | TI use |
|---|---|---|---|---|
| 3 | Ø2.90 through, Ø5.50 90° CSK opening **downward** | Y | (X, Z) = (3.50, 4.77), (51.81, −23.95), (62.31, −23.95) | M2.5 × 5 flat-heads from below into the fan-bracket tab (2) and the PCB corner standoff (1) |
| 4 | M2.5 × 0.45 tapped, horizontal | X / Z | back wall X≈70: (Y −0.82, Z −23.95); −Z wall Z≈−63.9: X 12.64 and 57.27 at Y −0.82; +Z wall Z≈13.4: X 57.27 at Y −0.82 | formatter-base wall screws (M2.5 × 5 flat-heads, CSK on the bracket) |
| 3 | M1.6 × 0.35 tapped | Y | (10.76, −42.42), (39.06, −39.72), (39.06, −15.22) | engine to base (M1.6 × 12 socket heads) — table only, inside the simplified cutout |

### MCH053 formatter base (2.05 mm L-bracket, 78.10 × 81.40, walls Y −4.82…22.83, flange top Y = 24.88)
| # | Type | Axis | Position (mm) | TI use |
|---|---|---|---|---|
| 3 | M2.5 × 0.45 tapped, top flange | Y | (X, Z) = (3.50, −35.50), (16.84, −60.26), (60.40, −51.74) | PCB: M3 6 mm spacer + M2.5 × 10 screw |
| 2 | M2 × 0.4 tapped, top flange | Y | (49.11, 3.73), (66.11, 3.73) | PCB: M3 5 mm spacer + M2 × 12 screw |
| 6 | M2 × 0.4 tapped, walls, all at Y = 20.46 | X / Z | back wall: Z 1.37, −51.92 · front short wall: Z −48.25 · −Z wall: X 57.28, 12.62 · +Z short wall: X 57.28 | (TI: 8× M2 total with the two above) |
| 4 | Ø2.90 + Ø5.50 CSK on the **outer** face, all at Y = −0.80 | X / Z | back wall: Z −23.96 · −Z wall: X 57.28, 12.62 · +Z short wall: X 57.28 | M2.5 × 5 flat-heads into the base's tapped wall holes |

### MCH052 fan bracket (1.63 mm) — fan 25 × 25 × 10 at X 44.5…69.6, Z −42.1…−32.0
| # | Type | Axis | Position | TI use |
|---|---|---|---|---|
| 4 | M2 × 0.4 tapped | Z | (X, Y) = (57.07 ± 10, 9.76 ± 10), 20 × 20 pattern, Ø24 bore | fan screws |
| 2 | M2.5 × 0.45 tapped, bottom tab | Y | (X, Z) = (51.81, −23.95), (62.31, −23.95) | under the base's two CSK holes |

### DLP038 PCB (70.00 × 72.76 × 2.04, at Y 30.81…32.85, X 0…70, Z −63.73…9.03)
| # | Type | Position (X, Z) | Support underneath |
|---|---|---|---|
| 4 | Ø2.69 (M2.5 clearance) | (3.50, −35.50), (16.84, −60.26), (60.40, −51.74) | M3 6 mm spacers on the flange |
|   |                        | (3.50, 4.77) | M2.5 × 23 hex + M2.5 × 10 M-F standoff column down to the base tab |
| 2 | Ø2.39 (M2 clearance) | (49.11, 3.73), (66.11, 3.73) | M3 5 mm spacers on the flange |

40-pin header J2: X 63.97…69.05, Z −62.85…−12.05 (pin rows at X 65.24 / 67.78, 2.54 pitch), 8.5 mm
tall. Barrel jack: X 0.94…12.94, Z −61.20…−45.95.

### Young Optics FLA12-F engine (body 33.28 × 23.07 × 53.03 at X 7.19…40.46, Y −2.97…20.09, Z −45.90…7.12)
Lens axis at (Y 10.39, Z −11.32), barrel Ø16.9, front hood 30.3 × 15.5 reaching X = −15.37.
Top plate per TI's Figure 5-1 (34.13 × 54.99): **4× Ø1.66** (M2 thread-forming; TI uses M2 × 12
pan heads) at plate coordinates (3.63, 3.54), (31.42, 6.24), (31.42, 30.73), (7.53, 50.64).
The plate's orientation in the assembly frame is inferred (v = 0 at the engine's +Z end); check
against the unit before relying on those four positions. All other holes above are exact.

## Provenance

- **TI TIDM757**, "CAD/CAE Symbol" download on the [TIDA-080009](https://www.ti.com/tool/TIDA-080009)
  page: `DLP038_Mechanical_092420.zip` with MCH051/052/053 drawings and STEP files and the full
  `DLP038 - REVF.STEP` assembly. Positions here were measured on the STEP cylinder faces.
- **TI TIDM755**, assembly drawing "P23 EVM TOP ASSEMBLY" DLP038 rev F (2020-09-24): envelope and
  the fastener BOM quoted in `dlpdlcr230npevm_dims.py`.
- **TI DLPU103B**, DLP LightCrafter Display 230NP EVM User's Guide, Figure 5-1: engine drawing.

TI's files are licensed for use in development with TI parts and may not be redistributed, so
they are **not** in this repo — download them from the link above. The 81 × 87 mm "business card"
size on TI's product page is the bracket footprint, not the PCB (70.00 × 72.76).

## Regenerate

```bash
python -m venv ~/cadenv && ~/cadenv/bin/pip install cadquery      # ~1.8 GB, once; keep out of vrfarm
cd display_diagnostics/projector_model
~/cadenv/bin/python build_model.py                                # STEP + STL + SVG
conda activate vrfarm && python plot_hole_maps.py                 # PNG
```
Edit numbers only in `dlpdlcr230npevm_dims.py`; both scripts read from it.

## Simplifications

Sheet-metal bends, reliefs and slots, the base's exact tab outlines, the DMD flex tab, LED cables,
the FPD-Link connector and small components are omitted. Walls and flanges are full-length boxes.
The engine is a box with the lens barrel and the three LED heat sinks; its internal features are not
modelled.
