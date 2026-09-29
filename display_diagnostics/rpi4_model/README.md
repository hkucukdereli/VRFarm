# Raspberry Pi 4 B + Joy-IT Armor Case "BLOCK" — simplified 3D model

The follower Pi as it sits on the rig: a Raspberry Pi 4 Model B inside the Joy-IT passive
aluminium cooler **RB-AlucaseP4+07 "Armor Case BLOCK"** (finned top block + bottom plate, four
M2.5 socket-head screws from below). Boxes and cylinders only; the board, its four holes and all
connector positions are exact from the Raspberry Pi drawing, the case envelope is Joy-IT's, and
the case details are estimated from photos and flagged below.

| File | What |
|---|---|
| `rpi4b_joyit_armor_case_simplified.step` | assembly STEP, one named solid per part |
| `rpi4b_joyit_armor_case_simplified.stl` | mesh |
| `rpi4b_joyit_armor_case_simplified_iso.svg` | isometric line drawing |
| `rpi4b_joyit_armor_case_hole_maps.png` | top view and stack-up side view with the holes |
| `rpi4_armor_case_dims.py` | **the numbers**, with a `CASE_ESTIMATED` block to check with calipers |
| `build_model.py`, `plot_hole_maps.py` | CadQuery build, matplotlib drawing |

## Frame

Raspberry Pi drawing convention: **X** along the 85 mm edge, 0 at the microSD end, 85 at the
USB/Ethernet end · **Y** along the 56 mm edge, 0 at the USB-C/HDMI/audio edge, 56 at the GPIO
edge · **Z** up, 0 = PCB bottom, 1.4 = PCB top. Envelope about 90 × 57 × 27 mm including the
microSD card, the USB/Ethernet overhang and the estimated 3 mm gap under the block.

## Holes

The assembly has exactly four fixing points: the Pi's mounting holes. Both case halves use them.

| # | Type | Position (X, Y) | Used by |
|---|---|---|---|
| 4 | Ø2.7 through the PCB (M2.5 clearance), 58 × 49 pattern, 3.5 from the edges | (3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5) | Joy-IT M2.5 socket-head screws from below, through the bottom plate and the PCB, into threaded posts in the top block |

No other hole is published for either case half. To mount the cased Pi, replace the four screws
with longer M2.5 screws through your bracket under the 3.5 mm bottom plate, or clamp the plate.
Confirm on the part that the plate has no extra holes before designing around that.

## Exact vs estimated

**Exact (Raspberry Pi mechanical drawing):** outline 85 × 56, corner radius 3.0, holes, connector
centres (USB-C x 11.2, micro-HDMI x 26.0 and 39.5, Ethernet y 45.75, USB stacks y 27 and 9, GPIO
header centre x 32.5), component heights above the board (USB-C 3.2, HDMI 3.0, audio 6.0, header
8.5, Ethernet 13.5, USB 16.0, SoC 2.4, FPC connectors 5.5). Connector protrusion past the board
edge is a typical value, ±0.5 mm.

**Published by Joy-IT:** top block 69 × 56 × 15.5, bottom plate 87 × 56 × 7.5, 107 g, CNC-milled
aluminium, four screws, allen key, thermal pads.

**Estimated from photos, measure before relying on it** (`CASE_ESTIMATED` in the dims file):
the block starts at the microSD edge and stops at x = 69, leaving the USB/Ethernet end open; block
underside 3.0 mm above the PCB top; ten grooves along X, 5.0 mm pitch, 2.6 wide, 9 deep; an open
slot over the GPIO header (x 6.5…58.5) and a slot over the camera connector; a pocket along the
front edge clearing the audio jack; Ø6 threaded posts at the holes; bottom plate 3.5 thick with
Ø6 × 4.0 bosses; M2.5 × ~25 socket-head screws with counterbored heads.

If the case on the rig is the closed **RB-AlucaseP4+08** box (91 × 65 × 34, cast, milled channels)
instead, say so: it is a different model, not a parameter change.

## Regenerate

```bash
~/cadenv/bin/python build_model.py            # same venv as ../projector_model (pip install cadquery)
conda activate vrfarm && python plot_hole_maps.py
```

## Sources

- Raspberry Pi 4 Model B mechanical drawing, `datasheets.raspberrypi.com/rpi4/raspberry-pi-4-mechanical-drawing.pdf`
- Joy-IT RB-AlucaseP4+07 product page, datasheet (published 2020-02-11) and manual, `joy-it.net/en/products/RB-AlucaseP4+07`
