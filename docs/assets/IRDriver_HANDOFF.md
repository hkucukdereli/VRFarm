# HANDOFF — IRDriver (KiCad project)

**For:** the Claude Code agent (or anyone) continuing this project in Hakan's repo.
**From:** the chat session that designed and generated rev 1.0 (2026-10-27/28); rev 1.1 → 3.2 by the Claude Code session of 2026-11-02/03.
**State:** rev 3.2 (**38 × 74 mm board that mounts on the back of the Raspberry Pi HQ Camera**, I²C-only over a **Qwiic / STEMMA QT** connector, SMD circuit on the camera side, LDD module lying across the board + power terminal + Qwiic receptacle + heat sink on the outer side, all SMD parts JLCPCB-stocked, JLCPCB BOM/CPL + Gerbers exported), complete and verified with KiCad 10, **not yet ordered**.
**Vocabulary:** the user calls the exposed side (LDD, terminal, Qwiic, heat sink) the *front* and the side facing the camera the *back*; KiCad's layer names are the other way round (the camera side is `F.Cu`). These docs say **camera side** and **outer side** and give the KiCad layer in brackets. **VRFarm is NOT yet updated for rev ≥2.3** (its driver still claims GPIO22 — §6.0). One physical check is still open (§6.1: confirm the LDD-1000L pin positions). Rev 2.0 (120 × 64 mm) and rev 2.3 (86 × 44 mm, stand-alone) are archived in `docs/archive/`.
**Used by:** the VRFarm rig (`/Users/hakan/VRFarm`, git): this board is its IR illuminator. Pi-side driver `devices/ir_led.py`, wiring `docs/LEADER_WIRING.md`, setup card "IR Illuminator". Change the J3 pinout or the I²C address here → change them there too.

---

## 0. TL;DR

- 2-layer **38 × 74 mm** board: **Mean Well LDD-1000L** constant-current driver → one **ILS ILH-IS01-85SN-SC201** (850 nm IR, 1 A, 20 mm star), controlled by a **Raspberry Pi over I²C only** (on-board MCP4725 DAC, 0x60) through a **4-wire Qwiic / STEMMA QT cable** (JST SH, the connector of the Bonsai Breakout); the Pi's 3.3 V on that cable is the enable.
- **Mechanics (rev 3.x):** the lower 38 × 38 mm copies the HQ Camera's four M2.5 holes (30 × 30 mm pattern, Ø2.7 here) and sits behind the camera on **4 × M2.5 × 5 mm standoffs** (4 mm minimum); the upper 36 mm sticks out past the camera's edge with the LED star on the camera side (facing the scene, same direction as the lens) and the heat sink on the outer side. Camera side (KiCad `F`): SMD circuit, LED solder pads, thermal pad — nothing taller than 1.3 mm. Outer side (KiCad `B`): LDD module lying across the board between the hole rows, 3.5 mm screw terminal and Qwiic receptacle side by side at the top edge, heat sink.
- Requirements it satisfies: (1) the LED star screws onto the thermal pad with **2 × M2 screws at 8.6 mm centres** and a **≤32 × 32 (or Ø35) heat sink** screws onto the back with the same two screws; (2) the LED is **off whenever the Pi is off/booting/floating** (hardware, not software); (3) the Pi sets the intensity **flicker-free** (analog dimming via the DAC; no PWM in the light path, because the camera would beat with it); (4) it mounts on the HQ Camera with matching holes; (5) the camera side can be **assembled by JLCPCB** (`BOM_JLCPCB.csv`, `CPL_JLCPCB.csv`: 11 placements, 9 LCSC part numbers, all in stock on 2026-11-02/03; the Qwiic receptacle on the outer side is hand-soldered or needs double-sided assembly). Rev 2.3 trade-off, accepted by the user: with no GPIO, a crashed program or a warm reboot leaves the LED at its last level until a DAC write of 0 or a power-off.
- Verified 2026-11-03 with KiCad 10.0.0 `kicad-cli` (`pcb drc --refill-zones --schematic-parity --severity-all`): **DRC 0 violations / 0 unconnected / 0 schematic-parity issues**, ERC 0 errors (only "library not in configuration" warnings, see §4). Generator DRC-lite clean (265 items, 163 holes, 12 nets).
- Files are **KiCad 7 format** (`version 20221018` / `20230121`). KiCad 8/9/10 open them and offer to upgrade on save. Fine to accept.
- Everything was produced by Python generator scripts in `IRDriver/tools/generate/`. **Decide once whether the generator or the KiCad files are the source of truth** before editing (§3). Don't mix. Every revision so far was done in the generator (workflow A).

---

## 1. Repository layout

```
LED_Driver_RPi/
├── README.md                   top-level orientation
├── HANDOFF.md                  this file
├── .gitignore
├── docs/
│   ├── photos/                 Hakan's photos of the real parts (star top side, RS bag label, LDD-1000L)
│   │                           + led_star_crop_for_measurement.png (crop used for the hole-spacing measurement)
│   ├── datasheets/             ILS SYNIOS PowerStar (visible + IR versions), ILS bare star PCB drawing,
│   │                           Fischer ICK S R 50x20 heat sink (= ILS ILA-HSINK-STAR-50X20MM),
│   │                           RPi_HQ_Camera_mechanical_drawing.pdf, JST_SH_connector_catalog_LCSC-C160404.pdf (Qwiic
│   │                           receptacle), SHOUHAN_2.0-4PWTDK_..._LCSC-C3008564.pdf (rev 3.0 Grove receptacle),
│   │                           KEFA_KF350-3.5-2P_..._LCSC-C474892.pdf (terminal),
│   │                           Grove_Base_Hat_RPi_v1.0/ (Seeed Eagle .brd/.sch + pdf + README)
│   └── archive/                rev2.0_120x64/ and rev2.3_86x44/: kicad files, BOM, previews of the earlier boards
└── IRDriver/
    ├── IRDriver.kicad_pro      project (rules: clearance 0.2, track 0.2, via 0.6/0.3, hole-to-hole 0.25, edge 0.3)
    ├── IRDriver.kicad_sch      schematic, root sheet only, all symbols embedded in lib_symbols
    ├── IRDriver.kicad_pcb      board, placed + routed, zones defined (unfilled on disk → press B)
    ├── IRDriver.kicad_sym      project symbol library (17 symbols; D_Schottky, LED, Conn_01x03/05 no longer placed)
    ├── IRDriver.pretty/        project footprint library (22 footprints; the ones not placed in rev 3.0 stay for reuse:
    │                           D_SMA, LED_0805, R/C_0805, C_1206, CP_Radial, PinHeader_1x03/05, Grove_HY2.0-4P, TerminalBlock 5.08, M3 hole)
    ├── sym-lib-table, fp-lib-table   project-local tables, URIs use ${KIPRJMOD} → folder is relocatable
    ├── BOM.csv                 full BOM with LCSC codes + notes
    ├── jlcpcb/                 the JLCPCB order set: IRDriver_rev3.2_gerbers.zip (+ gerbers/, git-ignored) from tools/export_jlcpcb.sh,
    │                           BOM_JLCPCB.csv + CPL_JLCPCB.csv written by build.py, ORDER.md checklist
    ├── README.md               user-facing: camera mounting, design rationale, thermal budget, JLCPCB ordering, Pi wiring + code
    ├── preview_top.png / preview_bottom.png / preview_schematic.png   KiCad 10 layer renders (zones filled via --check-zones)
    ├── preview_3d_top.png / preview_3d_bottom.png                     KiCad 10 `pcb render` (no 3D models, so bare board)
    ├── check_top.png / check_bottom.png / check_schematic.png         generator's own approximate renders
    └── tools/
        ├── verify_kicad.py     KiCad-7-era verification script (pcbnew python); on KiCad 8+ use kicad-cli, §4
        └── generate/           the generator (see its README.md)
```

No standard KiCad libraries are referenced anywhere; footprint IDs are `IRDriver:<name>`, symbol IDs `IRDriver:<name>`.

---

## 2. Design summary

### 2.1 Power path
`J1` (3.5 mm screw terminal KEFA KF350-3.5-2P on the **outer side**, at the top edge next to the H1 hole, wires entering from that edge; VIN 6–36 V, **9–12 V recommended**) → net `VIN` directly (no reverse-polarity diode since rev 2.2; the LDD-L lists no reverse-input protection) → `C1` 10 µF/25 V 0805 + `C2` 1 µF/50 V 0603 (camera side, next to the +Vin pins) → `U1` LDD-1000L +Vin/−Vin (outer side, input pins at the left end). `U1` ±Vout (right end) → `J2` = two solder pads (Ø1.2 mm, 3.5 mm apart, bottom-right of the camera-overlap area, labelled on both sides) for the star's 200 mm leads. LED current is fixed by the module at 1000 mA (±5 %); nothing on the board sets it.

Net names in the PCB (12): `VIN`, `GND`, `/LED+`, `/LED-`, `/3V3`, `/SDA`, `/SCL`, `/A0`, `/VDAC`, `/DIM`, `/Q1_B`, `/Q2_B`. (Leading `/` = local label on the root sheet, KiCad convention; `VIN`/`GND` are power symbols.) **`/LED-` is NOT `GND`** — the LDD output must float; the datasheet says "don't connect −Vout to −Vin". DRC would not catch that mistake, so keep it in mind if re-routing.

### 2.2 Fail-safe enable from the Pi's 3V3 + on-board DAC (the part that satisfies requirements 2 and 3)
```
3V3 (J3.3) ─R1 4k7─ Q1.B     Q1.E─GND     Q1.C ─┬─ Q2.B     Q2.E─GND     Q2.C ─┬─ DIM (U1.21)
            R2 47k ↓ GND                 R4 47k ↑ VIN                     R6 33 ─ VDAC = U2.VOUT
U2 MCP4725A0 (SOT-23-6): VDD = 3V3 (J3.3, from the Pi, + C3 100n), VSS = GND, SDA/SCL = J3.2/J3.1,
                         A0 = GND via the R7 0R strap (addr 0x60); omit R7 and strap A0 to 3V3 for 0x61.
```
- LDD DIM pin is **ON when open** (internal pull-up of unpublished value) — so a passive pull-down is not a guaranteed off. Instead `Q2` actively shorts DIM to −Vin by default; it is powered from `VIN`, not from the Pi.
- **The gate is driven by the Pi's 3.3 V on the Grove cable, not by a GPIO (since rev 2.3).** Pi off → no 3V3 → `R2` keeps `Q1` off → `R4` turns `Q2` on → DIM < 0.4 V → **LED off** (Q2 also clamps the unpowered DAC's ESD-diode leak, ≈0.6 V, that would otherwise reach DIM). Pi on → `Q1` on → `Q2` off → DIM follows `VDAC` through `R6`.
- `U2` output 0–3.3 V (12 bit, VDD is the reference) → LDD analog window **0.5 V = min … 2.5 V = 100 %** = codes 620–3102. **DAC = 0 V is the "off" state while the Pi is up**, so `R6` is 33 Ω: against a hypothetical 1 k internal DIM pull-up to 5 V it leaves DIM at 0.16 V (< 0.4 V); the MCP4725 output is internally current-limited (~15 mA) so a small R6 is safe.
- **MCP4725 factory EEPROM = mid-scale (D11 = 1 → 1.65 V at power-up ≈ 60 % LED)** (datasheet Table 5-3). With the 3V3 gate open from the first instant, a fresh chip would light the LED at boot → **program the EEPROM to 0 once on the bench before the LED is wired** (README §4 gives the `i2cset` line); the software must never write a non-zero EEPROM value.
- Not covered by hardware: program crash / kill / warm reboot with the LED on → the DAC keeps its register until a write of 0 or a power-off. The user accepted this for a 4-wire hookup (decision 17).
- `J3` = **Qwiic / STEMMA QT receptacle**: JST SH **SM04B-SRSS-TB(LF)(SN), LCSC C160404** (JLCPCB extended part, 60 k in stock, $0.22), 4 pins, 1.0 mm pitch, side entry, 2.95 mm tall; footprint `JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal` = the KiCad library footprint, identical to J11 on the Bonsai Breakout (`~/Dropbox/Hakan/lab/designs/Bonsai Breakout/`, pads 1–4 at x = −1.5…+1.5, y = −2, mechanical pads at (±2.8, 1.875), cable entry toward +Y). Pin order = the Qwiic standard **1 GND (black), 2 VCC = 3V3 (red), 3 SDA (blue), 4 SCL (yellow)**. Since rev 3.2 it sits on the **outer side** at the top edge next to J1 (cable entry at the edge, pluggable with the board mounted); it is the only SMD part on that side, so it is hand-soldered (`"PCBA": "no"` in `PARTS`) unless double-sided assembly is ordered. Silkscreen `QWIIC I2C` beside it + `1` under pin 1. No I²C pull-ups on the board (the Pi has 1.8 k). Rev 3.0 had a Grove receptacle (SHOU HAN 2.0-4PWTDK, footprint `Grove_HY2.0-4P_SMD_Horizontal_2.0-4PWTDK`, still in the library) — the user asked for the Bonsai's Qwiic connector instead in rev 3.1.

### 2.3 Thermal pad (`TP1`, footprint `LED_Star20mm_ThermalPad_ViaFarm`) — the star + heat-sink mount
Star on the front, heat sink on the back, heat goes through a via farm. Numbers behind it (also in README §2): LED ≈3.3 W electrical at 1 A (Vf 3.0–3.6 V), ≈1.4 W leaves as light → **≈2 W heat**; 137 vias Ø0.3 mm at 1.5 mm pitch ≈ 1.4 K/W through the board; with two paste interfaces ≈ 2.5–3.5 K/W star→sink → 5–7 K rise. Not thermally marginal.

**The star's mechanical data is confirmed (rev 1.1).** The ILS "Bare IMS PCB for Single LED" datasheet (`docs/datasheets/ILS_Bare-IMS-PCB-...pdf`, p.3, drawing of ILH-ON01-PCB, same 20 mm star family as the ILH-SY01 VER B board under this LED) gives: **Ø20 mm star, two Ø2.0 mm mounting holes at 8.6 mm centres** either side of the LED, 6 × Ø3.5 rim notches; the PowerStar datasheet gives 20 × 20 × **2.2 mm**. Hakan's photo of the actual star (`docs/photos/led_star_...jpg`) measures 8.4 ± 0.3 mm and ≈Ø2.1 when the star is scaled to 20 mm, consistent with the drawing. **The "mounting holes using M3 screws" line in the ILS datasheets is boilerplate — it is M2 for this star.**

Geometry constants (in `tools/generate/footprints.py`, all mm):

| constant | value | meaning |
|---|---|---|
| `STAR_D` / `STAR_NOTCH_D` | 20.0 / 3.5 | star outline drawn on F.Fab (`star_outline()`), notches at 0°, 60°, … (holes on the 0/180° axis) |
| `STAR_HOLE_PITCH` / `STAR_HOLE_D` | 8.6 / 2.0 | from the ILS drawing |
| `TOP_PAD_D` | 24.0 | exposed round copper on F.Cu under the 20 mm star |
| `BOT_PAD_W` | 30.0 | exposed square copper on B.Cu (heat-sink contact; a 30 mm sink base sits on bare copper → flat) |
| `HEATSINK_W` | 32.0 | B.CrtYd + B.Cu keep-out square reserved for the heat sink |
| `HEATSINK_D` | 35.0 | largest round sink that fits, drawn as a circle on B.Fab + Dwgs.User |
| `SLOT_X` | ±4.3 | screw slot centres = 8.6 mm nominal |
| `SLOT_DRILL` | (2.4, 3.4) | plated slot width × length → M2 clearance, accepts 7.4–9.8 mm hole pitch |
| `VIA_PITCH / VIA_DRILL / VIA_PAD` | 1.5 / 0.3 / 0.6 | via farm |

All of it is pad "1" of one footprint with **no net** (electrically floating), so KiCad accepts the overlaps. The footprint is `board_only`, excluded from BOM/pos.

Mechanical stack: star (2.2) → thermal paste → PCB (1.6) → paste/TIM → heat sink. **Two M2 screws go through the star and the PCB slots into tapped M2 holes in the heat sink.** There is no room for nuts under the PCB (the heat sink base sits there), so the sink must be tapped: any ≤32 × 32 / Ø35 sink with a ≥3 mm base, 2 holes drilled 1.6 and tapped M2 at 8.6 mm (generic 30 × 30 × 20 mm sinks, ≈8–10 K/W, are fine for 2 W). Screw length: 2.2 + 1.6 + ≥3 mm thread ≈ **M2 × 10**. Nylon washers under the screw heads (ILS datasheet requirement; the star's copper pads come close to the holes). In rev 3.0 the sink hangs off the back of the stick-out, 2 mm past the camera's edge, next to the LDD module — nothing of the camera is behind its PCB except the ribbon connector, so the back side is unconstrained.

### 2.4 Mean Well LDD-1000L footprint (`MeanWell_LDD-1000-1500L_THT`) — pin numbering assumption
Datasheet (LDD-L-SPEC): pin style, 31.8 × 20.3 × 12.2 mm body, 2.54 mm pitch, 15.24 mm row spacing, DIP-24 numbering with only 9 positions populated:

| pins | function | schematic pin type |
|---|---|---|
| 1, 2 | −Vin | power_in / passive |
| 11, 12 | −Vout | power_out / passive |
| 13, 14 | +Vout | power_out / passive |
| 21 | DIM | input |
| 23, 24 | +Vin | power_in / passive |

Pin 1 = square pad, +Y row left end (in footprint coordinates). Pads 1.7 × 2.2 mm oval, 1.0 mm drill. Silkscreen around U1 (on the back in rev 3.0) prints `+Vin −Vin DIM +Vout −Vout` at the pad groups. Hakan's photo of the module (`docs/photos/driver_LDD-1000L.jpg`, label side) shows Vin −/+ on one short edge, Vout −/+ on the other and the lone DIM pin next to Vin+, which is consistent with the footprint, but the pins themselves are not visible in the photo — **confirm pins-up before soldering** (§6.1). The lone DIM pin makes a flipped orientation obvious. The module is placed on the **outer side** (`"B"` in `PLACEMENT`), lying across the board (rev 3.1: rotation 0, long axis along x, input pins at the left end, outputs at the right end): its pins are through-hole, so they are soldered from the front and their tips (≤2 mm) stick into the camera gap — trim them.

### 2.5 Board (rev 3.2)
**38 × 74 mm**, 2 mm corner radius. `CAM_HOLE = 4`, `CAM_H = 38`: holes `H1–H4` Ø2.7 (M2.5) at (4,4), (34,4), (4,34), (34,34) = the HQ Camera's pattern; y 0–38 overlaps the camera (its ribbon-connector/tripod edge is at y = 0), y 38–74 sticks out. `GND` pours on **both** layers over y 0.6–38 (`GND_pour` on B.Cu, `GND_pour_F` on F.Cu), kept out of a **Ø6.2 mm copper-free ring around each mounting hole** (`hole_keepout_H1…H4`, rule areas on both copper layers, `HOLE_KEEPOUT_D`; the hole footprints' courtyards match) so an M2.5 screw head or hex standoff never sits on copper — the same ring the Raspberry Pi boards show; the stick-out is left to `TP1` at (19, 56) with the 32 × 32 B.Cu keep-out (`heatsink_keepout`) and the Ø35 circle on Dwgs.User. Board-relative origin is at KiCad sheet (50, 50) mm; `grid_origin` is set there. Every placement/track coordinate is in `pcb.py` with the pad positions noted in comments.

**Outer side (KiCad `B`):** `U1` LDD at (19, 19.4) rot 0, lying across the board between the hole rows (body x 3.1–34.9, y 9.25–29.55); pins −Vin (5.03/7.57, 11.78), −Vout (30.43/32.97, 11.78), +Vout (30.43/32.97, 27.02), DIM (12.65, 27.02), +Vin (5.03/7.57, 27.02). In the 8 mm strip between the top edge and the module: `J1` KF350 terminal at (9.25, 4.6) rot 0 next to the H1 standoff (VIN = pin 1 (9.25, 4.6), GND (12.75, 4.6); body x 7.5–14.5, y 0.5–7.4) and `J3` Qwiic receptacle at (20.0, 3.9) rot 0 (pads (18.5/19.5/20.5/21.5, 5.9) = GND/3V3/SDA/SCL, mechanical pads (17.2/22.8, 2.025), housing x 17–23, opening 1.3 mm in from the edge), both with their cables entering at the top edge; 2.5 mm between the two housings. Back-side footprints are placed with a 4th tuple element `"B"`: `fp_pads()/fp_shapes()` mirror y and swap `F.*`↔`B.*` layers, `embedded_footprint()` emits them on B with mirrored text; `validate.py` mirrors the courtyards the same way.

**Camera side (KiCad `F`), in the upper part of the 12.6 mm band between the LDD's pin rows (y 13.1–25.7), kept ≥ 1.3 mm from the hand-soldered pin pads:** `U2` at (9.8, 18.85) rot 180 (SDA/SCL/A0 on its left column at x 8.7, y 17.9/18.85/19.8; VDD/VSS/VOUT on the right column at 10.9), `C3` (12.6, 16.6) rot 90 — the 3V3 line runs through its pad 1 —, `R7` (7.7, 21.9) rot 270, `C1` (10.0, 21.6), `C2` (10.0, 23.5) above the +Vin pins (2 mm clear of their pads), `R6` (12.3, 21.4) rot 90, `Q2` (15.4, 23.3) rot 0 whose collector track and R6 meet at the DIM pin (12.65, 27.02), `Q1` (15.2, 18.7) rot 180, `R4` (15.0, 21.0) rot 180 on the Q2_B vertical, `R1` (17.9, 19.65) and `R2` (19.8, 19.65) rot 180 at the end of the 3V3 line. `J2` LED pads at (28.0, 35.5) `+` and (24.5, 35.5) `−` under the LDD's output end.

**Routing:** VIN on B.Cu (1.0 mm) from J1 pin 1 down to y = 7.7, along the top strip to x = 3.0 (clear of the H1 ring) and down the left edge to the +Vin pins (keeps the area under the module free), with a 0.5 mm branch along y = 21.0 to the via (16.5, 21.0) that feeds R4 on the camera side; the GND return on F.Cu from J1 pin 2 to the −Vin pins (plus both pours); LED+ / LED− on B.Cu from the output pins down to J2 (LED− crosses the lower row where it has no pins); the LDD's doubled pins bridged by short tracks. Qwiic: GND stub to the via (18.5, 7.6); 3V3 / SDA / SCL run down the outer side at x = 19.5 / 20.5 / 21.5 through the empty DIP positions of the upper row, 3V3 to the via (19.5, 12.0) and then on F.Cu down to the 3V3 line at y = 16.9; SDA and SCL turn left at y = 14.1 / 14.9 (SDA inner at both ends, so no crossing) to the vias (7.6, 14.1) / (6.4, 14.9) and drop on F.Cu into U2's left pads. Camera side otherwise: 3V3 along y = 16.9 from x 19.5 to the R1/R2 node (branch at x = 11.85 down to U2 VDD); VDAC → R6 → DIM (R6 to the pin at x = 12.3/12.65); Q2_B vertical at x = 14.2 from Q1.C to Q2.B; DIM from Q2.C straight down x = 16.4 and along y = 27.1 to the pin. Vias (7): GND (18.5, 7.6), 3V3 (19.5, 12.0), SDA (7.6, 14.1), SCL (6.4, 14.9), GND (12.2, 18.85) at U2 VSS, GND (10.0, 27.0) between the +Vin and DIM pins (ties the C1/C2 pour region), VIN (16.5, 21.0). Track widths 1.0 mm power, 0.5, 0.3 mm signal; vias 0.8/0.4. **Corners:** `TRACKS` is written with right-angle corners; `chamfer()` in `pcb.py` turns every right angle into two 45° bends at emission time (cut-back 0.6 mm for 0.3 mm tracks, 1.5 × width otherwise, at most half a leg; corners on a pad centre and 45° or near-straight corners are left alone; zero-length leftovers are dropped). `validate.py` and the geometry it checks see the chamfered segments. Silk, camera side: `IR LED DRIVER v3.2 - CAMERA SIDE` along y = 1.6, `LED` `+` `−` at J2, `850nm IR - EYE HAZARD` vertical at the right edge, the pad's own texts; outer side: `+ 9-12V −` under J1, `QWIIC` / `I2C` beside J3 and `1` under its pin 1, `IR LED DRIVER v3.2 - OUTER SIDE` under the module, `LED` `+` `−` at J2 again, the LDD pin names, heat-sink notes. `REF_HIDE` hides the silk refs of everything but the holes' (J1/J2/J3/U1 are labelled by text, the SMD parts are identified by the CPL and would collide in the band; every ref is still on F.Fab/B.Fab).

**Clearances that matter in the camera gap** (camera side, standoffs ≥ 4 mm, 5 recommended): the camera's ribbon connector 2.75 mm (at y 0–5.7, x ≈ 8–30 in this frame), SMDs ≤ 1.3 mm, the LDD pin tips, the standoff screw heads (bare board around the holes). Nothing has to be plugged in inside the gap any more.

Rev 3.1 → 3.2 changes (user request: "keep all the SMD parts on the camera side and place the I²C connector on the other side", after the trade-off was laid out): `J3` moved from the camera side's left edge to the outer side's top strip at (20.0, 3.9) next to J1 (`"B"`, rot 0) and taken out of the PCBA files (`"PCBA": "no"`); its GND/3V3/SDA/SCL reach the camera side through four new vias; VIN re-routed along the left edge so the outer side under the module is free for those tracks (its R4 branch now starts at x = 3.0); the camera-side 3V3 line now starts at the via (19.5, 14.0); the `QWIIC I2C` silk moved to the outer side and J2's labels are printed on both sides; the docs switched to "camera side / outer side" wording; all track corners chamfered to 45° (`chamfer()`, user request); the camera-side cluster moved up 2 mm after the user noticed C2 sat 0.36 mm from the +Vin pin pad (now ≥ 1.3 mm from every hand-soldered pad, 2 mm at C2). `aux_axis_origin` set to the board's lower-left corner so Gerbers, drills and the CPL share one origin; `JLCJLCJLCJLC` order-number marker on the camera side; the JLCPCB files moved to `jlcpcb/` with `tools/export_jlcpcb.sh`; Ø6.2 copper-free rings around the four holes (user request, "like the Pi"); `build.py` now admits only camera-side SMD parts with an LCSC code to the PCBA files (through-hole parts, the star and anything on the outer side are excluded structurally, not just by the `"PCBA": "no"` flag). 265 copper items, 163 holes, 12 nets, 7 vias. Verified identically (DRC 0/0/0, ERC 0 errors). Schematic rev "3.2".

Rev 3.0 → 3.1 changes (user request: "flip the LDD 90 degrees (SMD components should follow too) and replace the Grove 4-pin connector with the Qwiic-style I²C connector from the Bonsai project"): `U1` rot 270 → 0 (standing between the hole columns → lying between the hole rows), which forced `J1` from the right edge (now covered by the module) to the top strip and `J2` to the bottom-right; the whole camera-side cluster re-placed into the band between the pin rows and re-routed; `J3` → JST SH (`fp_JST_SH_1x04_H`, already in the library since rev 2.3), pin order 1 GND 2 3V3 3 SDA 4 SCL in the schematic; the KF350 courtyard tightened to the real body (−3.05…4.35); the stale schematic note about D1/PWM removed. 238 copper items, 160 holes, 12 nets, 4 vias. Schematic rev "3.1".

Rev 2.3 → 3.0 changes (user request: "fit over the RPi HQ Camera board with matching holes, the heat-sink/star part sticking out, spacers; LDD and power connector on the same side, the 4-pin where the other SMD parts are; smallest components, JLCPCB PCBA, all SMD parts available from JLCPCB; solder pads for the LED"): board 86 × 44 → 38 × 74; M3 corner holes → the camera's M2.5 pattern; two-sided placement (new `side` field in `PLACEMENT`, `fp_pads/fp_shapes(flip)`); passives 0805/1206 → 0402/0603/0805 with LCSC codes (`fp_R0402`, `fp_C0402`, `fp_C0603`); `J1` 5.08 → 3.5 mm KF350 (`fp_TerminalBlock_P350_1x02`, body from the KF350 datasheet); `J2` terminal → solder pads (`fp_WirePads_1x02`); `J3` Seeed `HW4-SMD-2.0` → SHOU HAN `2.0-4PWTDK` layout (`fp_Grove_SMD_H` rewritten, new name) and moved from x 5.7 to 10.5 because its mechanical pads reach 9.45 mm in front of the signal pads; GND pour on F.Cu too; `build.py` writes `BOM_JLCPCB.csv` / `CPL_JLCPCB.csv` (parts with an `LCSC` property and no `"PCBA": "no"`); `validate.py` handles flipped courtyards and both pours; `REF_HIDE` extended. 256 copper items, 161 holes, 12 nets, 5 vias. Verified identically (DRC 0/0/0, ERC 0 errors). Schematic rev "3.0". Rev 2.3 archived in `docs/archive/rev2.3_86x44/`.

Rev 2.2 → 2.3 changes (user request: "I²C only", then "the Grove connector of the Seeed Base Hat, side entry"): the EN GPIO and the 1×5 header are gone; `J3` became the Seeed Grove SMD side-entry receptacle (transcribed from the hat's Eagle package `HW4-SMD-2.0`; a JST SH / Qwiic variant `fp_JST_SH_1x04_H` was built first and stays in the library unused); the R1/R2 node is the `/3V3` net; `R6` 100 → 33 Ω. 12 nets, 266 copper items, 166 holes, 10 vias.

Rev 2.1 → 2.2 changes (user request): **D1** (SS34 reverse-polarity Schottky), **D2 + R5** (green status LED) and **R8** (DNP second address strap) removed; the nets `/VIN_RAW` and `/LED_ST` are gone. R7 kept as the address strap.

Rev 2.0 → 2.1 changes: compact re-layout only, no circuit change: board 120 × 64 → 86 × 44; heat-sink envelope 50 → 32 (Ø35), bottom pad 46 → 30; `REF_HIDE` introduced. Then `C1` 100 µF electrolytic → 10 µF ceramic. Old files in `docs/archive/rev2.0_120x64/`.

Rev 1.1 → 2.0 changes: on-board DAC `U2` MCP4725A0 (SOT-23-6) + `C3` 100 n + `R7`/`R8` address select; `J3` 1×3 → 1×5; the old `AN` net replaced by `VDAC`, `3V3`, `SDA`/`SCL`, `A0`; `R6` 220 → 100 Ω. New symbols `MCP4725`, `Conn_01x05_PinsRight`; `validate.py` started modelling the GND pour for connectivity (GND vias that rely on the fill are legal).

Rev 1.0 → 1.1 changes: board 110 × 60 → 120 × 64; heat-sink area 40 × 40 → 50 × 50 / Ø50; slots centred on the confirmed 8.6 mm; real star outline on F.Fab; symbol fields copied onto footprints and B.Fab text mirrored (both were KiCad 10 DRC warnings); BOM rows for the heat sink and screws.

---

## 3. Source of truth — read before editing anything

Two valid workflows. Pick one and say which in the commit message.

**A. Keep generating (parametric changes: hole spacing, heat-sink size, pad diameter, board size, re-routing in code).**
- Edit `tools/generate/*.py`, then `python3 tools/generate/validate.py [<project dir>]` (regenerates into the project root by default + DRC-lite + `check_*.png`), then KiCad DRC/ERC (§4), then re-export previews and Gerbers (§4).
- Every run writes **fresh UUIDs/tstamps**; previous generated files go to `IRDriver/.backup/<timestamp>/` (delete or move that folder afterwards; it is in `.gitignore`).
- Any edit made in the KiCad GUI is lost on the next regeneration. Don't regenerate after GUI edits unless you port them into the generator first.
- `BOM.csv` and `jlcpcb/BOM_JLCPCB.csv` / `CPL_JLCPCB.csv` are regenerated too (`build.py`); the Gerber zip is **not** — run `tools/export_jlcpcb.sh` afterwards.

**B. Freeze the generator, continue in KiCad (normal ECAD workflow).**
- Open `IRDriver.kicad_pro`, accept the format upgrade if asked, press **B**, run DRC + ERC, save. From then on `tools/generate/` is documentation only — consider moving it to `tools/generate_frozen/` or adding a note.
- Schematic ↔ PCB sync works the normal way (`Update PCB from Schematic`, F8) because footprint `path` properties reference the schematic symbol UUIDs.

All revisions so far were done in **A**. Recommendation: stay in A until the LDD pin check (§6.1) is done, then switch to B.

---

## 4. Verification — what was done and how to repeat

Done on 2026-11-03 with KiCad 10.0.0 (`/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`, macOS):

| check | result |
|---|---|
| `kicad-cli pcb drc --refill-zones --schematic-parity --severity-all` | **0 violations, 0 unconnected, 0 parity issues**; 20 × `lib_footprint_issues` warnings (see below) |
| `kicad-cli sch erc --severity-all` | **0 errors**; 47 warnings, all `lib_symbol_issues` / `footprint_link_issues` (see below) |
| generator DRC-lite (`validate.py`: clearance 0.2, hole 0.25, edge 0.3, courtyards incl. flipped ones, heat-sink keep-out, hole rings, per-net islands incl. both GND pours minus the rings, sch↔pcb nets) | clean (265 items, 163 holes, 12 nets) |
| `kicad-cli pcb export svg --check-zones`, `pcb render`, `sch export svg`, `tools/export_jlcpcb.sh` (gerbers + drill) | all load and render / plot (the `preview_*.png`, `jlcpcb/`); drill files hold 159 PTH (incl. 2 slots) + 4 NPTH = the 163 holes of the generator check |
| `kicad-cli pcb export pos --use-drill-file-origin` vs `jlcpcb/CPL_JLCPCB.csv` | identical (11 rows, positions and rotations) |
| JLCPCB parts API (stock + library type) | all 9 PCBA part numbers in stock (basic: C20526, C25900, C25792, C25105, C17168, C1525, C15849, C15850; extended: C144198 7 k); hand-soldered extras J3 C160404 (extended, 60 k) and J1 C474892 (extended, 17 k) |

The only warnings are "The current configuration does not include the footprint/symbol library 'IRDriver'": `kicad-cli` on this Mac does not pick up the project-local `fp-lib-table`/`sym-lib-table` (KiCad has never been run interactively here, so no global tables exist). All symbols and footprints are embedded in the files, so nothing is actually missing; opening the project in the KiCad GUI once makes the warnings go away.

Repeat:
```bash
cd IRDriver
K=/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
$K pcb drc --refill-zones --schematic-parity --severity-all --format report -o /tmp/drc.txt IRDriver.kicad_pcb
$K sch erc --severity-all --format report -o /tmp/erc.txt IRDriver.kicad_sch
# generator-side (no KiCad): regenerates + geometry checks + check_*.png + BOM/CPL
python3 -m venv .venv && .venv/bin/pip install shapely matplotlib kiutils && .venv/bin/python tools/generate/validate.py && rm -rf .backup
# previews (zones filled on the fly; the .kicad_pcb on disk stays unfilled)
$K pcb export svg --check-zones --layers "F.Cu,F.SilkS,F.Fab,F.Mask,Edge.Cuts,Dwgs.User" --page-size-mode 2 --exclude-drawing-sheet -o /tmp/top.svg IRDriver.kicad_pcb && rsvg-convert -w 2400 -b white /tmp/top.svg -o preview_top.png
$K pcb export svg --check-zones --layers "B.Cu,B.SilkS,B.Fab,B.Mask,Edge.Cuts,Dwgs.User" --page-size-mode 2 --exclude-drawing-sheet --mirror -o /tmp/bot.svg IRDriver.kicad_pcb && rsvg-convert -w 2400 -b white /tmp/bot.svg -o preview_bottom.png
$K pcb render --side top --quality high --floor -w 2000 -h 1100 -o preview_3d_top.png IRDriver.kicad_pcb
# fabrication: tools/export_jlcpcb.sh = Gerbers (Protel names, no X2, --check-zones because the on-disk zones are unfilled,
#   --use-drill-file-origin) + Excellon drills (--drill-origin plot) -> jlcpcb/IRDriver_rev<rev>_gerbers.zip
tools/export_jlcpcb.sh
# cross-check of the generator's CPL against KiCad's own position export (same origin: the aux axis at the board's lower-left corner)
$K pcb export pos --format csv --units mm --side front --smd-only --use-drill-file-origin -o /tmp/pos.csv IRDriver.kicad_pcb
```
Note the on-disk `.kicad_pcb` has **unfilled zones** by design (generator can't fill). **Pass `--refill-zones`** to `kicad-cli pcb drc` and **`--check-zones`** to the exports, or the pours are missing (DRC then reports the pour-connected vias/pads as unconnected); in the GUI press B first.

---

## 5. Decisions already taken (with reasons) — don't silently revert

1. **Sandwich thermal path instead of a PCB cut-out.** Chosen because the heat is only ≈2 W and the user explicitly asked for "pad on PCB, heat sink on the other side". The cut-out variant (Ø21–22 mm hole, star directly on the sink) is the better thermal design if the load ever grows; it is a 10-minute change (delete TP1, circle on Edge.Cuts, keep the keep-out).
2. **Active default-off (Q1/Q2) rather than a pull-down on DIM.** Mean Well doesn't publish the DIM pull-up; a 10 k pull-down is "probably fine", the transistor stage is certain. Costs 2 × SOT-23 + 3 resistors.
3. **Analog dimming, not PWM:** PWM at 100–500 Hz beats with camera frame rates; a DAC on the LDD's analog input (0.5–2.5 V) gives flicker-free dimming.
4. **Short slots (2.4 × 3.4) instead of round holes for the star screws**, centred on the confirmed 8.6 mm. Kept as slots because the drawing is for the sibling ILH-ON01 star, not literally the ILH-SY01 VER B, and the photo measurement has ±0.3 mm uncertainty; the slots cost nothing thermally.
5. **(rev ≤2.3, superseded by 20) 0805 passives, 5.08 mm screw terminals, M3 corner holes** — hand-solderable, hobby-fab defaults.
6. **All libraries project-local.** Deliberate, so the project opens identically on any KiCad install. If you later swap a footprint for a standard-library one, keep the pad geometry (the custom ones were copied from KiCad's own library master for the common parts; the Grove receptacle and the KF350 terminal from their datasheets).
7. **ENIG finish recommended** (flatter pad under the star than HASL). Not enforced anywhere; it is an ordering option.
8. **(rev ≤2.1, superseded by 16) Reverse-polarity protection by series Schottky (D1).**
9. **(rev 1.1, superseded by 14) Heat-sink envelope Ø50 / 50 × 50 and the board grown to 120 × 64** for the ILS/Fischer ICK S R 50x20 sink.
10. **Screws through star → PCB → tapped heat sink, M2 × 10, nylon washers.** No alternative with nuts exists because the sink base covers the slots (§2.3).
11. **On-board MCP4725 instead of an external DAC breakout (rev 2.0).** One board, four wires to the Pi, the DAC rides the I²C bus the rig already has (MPR121 0x5A, AS5600 0x36 → 0x60 is free). The A0 variant was chosen so the address is 0x60/0x61 (Adafruit breakouts are the A1 variant at 0x62/0x63 — don't mix them up when scanning).
12. **(rev 2.0–2.2, superseded by 17) EN kept as a separate GPIO, not derived from 3V3.**
13. **R6 small (100 → 33 Ω)**: low source impedance against the LDD's unpublished DIM pull-up, still safe for the DAC's output (internally current-limited).
14. **Compact board with a 32 × 32 heat-sink envelope (rev 2.1).** The 50 mm sink was overkill for ≈2 W; a 30 × 30 × 20 sink at ≈8–10 K/W keeps the junction ≈75–85 °C worst case (rated 125 °C).
15. **C1 is a 10 µF ceramic (rev 2.1).** The 100 µF electrolytic only ever served PWM input pulses; analog dimming draws steady current. The fail-safe stage, R2, C2, C3, R6 and the DAC are not optional — see README "What is optional".
16. **D1, D2 + R5 and R8 removed (rev 2.2, user's call).** No reverse-polarity protection: the supply is a fixed PSU wired once; mind J1's polarity. No on-board indicator: the Pi knows the gate state and the camera sees the light. One board per I²C bus, so the second address strap went; R7 stays as the 0x60 strap. Address clashes checked on the VRFarm side: leader I²C-1 carries MPR121 0x5A, AS5600 0x36 and this DAC 0x60; the follower's DLPC3436 (0x1B) is on the other Pi's bit-banged bus.
17. **I²C-only, 3V3-gated (rev 2.3, user's decision after the trade-off was laid out).** The user wanted "LED on only when the Pi tells it to" over the bus alone. Kept: off with the Pi unpowered (3V3 gate), off at boot (EEPROM programmed to 0), on only after a DAC write. Lost: the kernel-releases-the-pin safety on a crash or warm reboot (LED stays at its last level). Alternatives noted and not taken: an SCL-activity monostable, or a small MCU with a watchdog in place of the DAC.
18. **I²C connector history: Qwiic (rev 2.3 draft) → Grove (rev 2.3–3.0) → Qwiic again (rev 3.1).** First asked for "the 4-pin I²C connector of the Bonsai Breakout" (JST SH SM04B-SRSS-TB, Qwiic/STEMMA QT — built as `fp_JST_SH_1x04_H`), then changed to "the one on the Grove Base Hat for Raspberry Pi, side entry" (Seeed's Eagle package transcribed; rev 3.0 swapped it for the JLCPCB-stocked SHOU HAN 2.0-4PWTDK, C3008564, footprint `Grove_HY2.0-4P_SMD_Horizontal_2.0-4PWTDK`, pads not compatible with Seeed's), and in rev 3.1 back to the Bonsai's Qwiic connector (user's choice). The JST original (C160404) is used, not a clone: it is the Qwiic reference part, in stock at JLCPCB, and the footprint is the one the Bonsai Breakout was built with. Pin order follows the Qwiic standard (1 GND, 2 3V3, 3 SDA, 4 SCL). Side note from the Bonsai Breakout: its J11 pins 3/4 go to Teensy pins 18 (SDA0) and **17** (SDA1/TX4), not 19 (SCL0).
19. **HQ-Camera piggyback, two-sided (rev 3.0, user's request).** 38 × 74 mm with the camera's 30 × 30 M2.5 pattern in the lower half and the star/heat sink in the 36 mm stick-out; the camera side carries only things that fit in a ≈10 mm standoff gap (the 5.2 mm Grove receptacle is the tallest), the LDD (12.5 mm), the terminal (8.8 mm) and the heat sink go on the back. The star is centred 18 mm past the camera's edge (37 mm off the optical axis) so the lens barrel only shadows the near part of the ±60° beam. Orientation: title edge at the camera's ribbon/tripod edge, so the ribbon leaves on the side away from the LED. The 2.3 layout (J3 at x 5.7) could not host the SHOU HAN part — its mechanical pads sit 9.45 mm in front of the signal pads — so J3 moved to x 10.5, between the LDD's DIM and +Vout pins, and R7 moved below U2.
20. **JLCPCB parts (rev 3.0).** Smallest sensible packages: 0402 resistors/100 n (basic parts), 0603 1 µF, 0805 10 µF, MMBT3904 (basic), MCP4725A0T-E/CH (extended), the Grove receptacle (extended). The PCBA files cover the **front side only**; `J1` got an LCSC code (KF350-3.5-2P, C474892) but `"PCBA": "no"` keeps it out of the BOM/CPL because it is a bottom-side through-hole part — two hand-soldered parts (LDD, terminal) are cheaper than two-sided assembly. The LDD-1000L exists at LCSC (C5259137) with no stock; the user has modules.
21. **LED leads soldered to pads, no terminal (rev 3.0, user's choice "3").** The star's 200 mm leads are soldered anyway; `J2` is two Ø1.2 mm pads 3.5 mm apart at the bottom of the camera-overlap area, which saved a tall part in the gap.
22. **LDD lying across the board (rev 3.1, user's request).** The module now spans x 3.1–34.9 between the hole rows, so the power terminal could no longer sit at the right edge and moved to the top strip (wires enter at the ribbon edge, on the outer side — the ribbon itself runs on the camera side). Inputs at the left end next to the DAC cluster, outputs at the right end over the LED pads; the camera-side cluster lives in the 12.6 mm band between the pin rows, which is why every SMD silk ref is hidden.
23. **Qwiic receptacle on the outer side, hand-soldered (rev 3.2, user's decision after the trade-off was laid out).** Gains: pluggable with the board mounted (no connector inside the 5 mm camera gap), both cables at one edge away from the ribbon, standoffs down to 5 mm. Costs: the receptacle is the only SMD part on the outer side, so JLCPCB's single-sided assembly won't place it — the user chose to keep the SMD circuit on the camera side and solder the JST SH by hand (double-sided assembly remains possible by removing `"PCBA": "no"`); four vias and ~30 mm of outer-side track under the module. Alternatives not taken: Qwiic on the camera side at the top edge (collides with the camera's ribbon connector), or moving the whole SMD circuit to the outer-side strips (single-sided assembly on the outer side, but a cramped re-layout).

---

## 6. Open items before ordering (in priority order)

0. **VRFarm has not been updated for rev ≥2.3** (the user asked not to touch it yet). Needed there: `devices/ir_led.py` without the lgpio/EN claim (`on()` = DAC write, `off()` = DAC 0, `init()` must still program EEPROM = 0 and must NOT turn the LED on), `shared/config.py` `ir_led_init_payload` and the rig YAMLs without `en_gpio`, `pi_api` unchanged otherwise, `controller/setup.py` Install no longer needs GPIO for `ir_led`, docs (`LEADER_WIRING.md`: plug into a Grove Base Hat I²C port, GPIO22 is free again, the board hangs on the HQ Camera; CLAUDE.md). The hat's MCU shares the bus at 0x04/0x08.
1. **Confirm LDD-1000L pin positions** against the physical module (hold it pins-up, compare with the silkscreen `+Vin −Vin DIM +Vout −Vout` around U1 on the back) or a current datasheet drawing (§2.4). If the real part has DIM on the other row or numbering mirrored, fix `fp_LDD_1000L()` in `footprints.py` (the `populated` dict and `px()/py()`), regenerate, re-route U1's short tracks (the `TRACKS` list in `pcb.py` references pad coordinates).
2. **Dry-fit on the real camera** (paper print at 1:1 is enough): holes line up (30 × 30, Ø2.5 on the camera), the ribbon connector ends up under the title edge, the ribbon can leave past the edge, the power wires and the Qwiic cable at the top edge on the outer side have room next to the ribbon (and the tripod, if the camera sits on its boss). Standoffs 5 mm (4 mm minimum). Decide the standoff length and whether the camera's tripod boss (13.97 mm wide, below its bottom edge, flush with its PCB back) is in the way of anything in the rig.
3. **Pi side of the Qwiic cable**: the VRFarm leader has a Grove Base Hat, no Qwiic port. Options: a Qwiic SHIM/pHAT, a Qwiic-to-female-jumper cable on GPIO header pins 1/3/5/9, or a Grove-to-STEMMA QT adapter cable into a hat I²C port. Pick one and put it in `LEADER_WIRING.md`. Sharing the bus with Adafruit's MPR121 and AS5600 STEMMA QT boards (addresses, chain order, pull-ups, Pi end) is written up in `docs/I2C_DAISY_CHAIN.md`; the IR driver has a single receptacle, so it is the last device in such a chain.
4. **JLCPCB order**: the three files in `IRDriver/jlcpcb/` (checklist in `ORDER.md` there): Gerber zip, BOM, CPL; economic assembly, side **Top** (= camera side), 11 placements; order number → "Specify a location" (the `JLCJLCJLCJLC` marker is on the camera side). In their viewer check the rotations of U2 (SOT-23-6, pin 1 = VOUT bottom-right) and Q1/Q2 (collector on the lone pad) — JLCPCB's rotation convention differs from KiCad's for some packages and their reviewers fix it on request. Extended-part fee for U2. Order J3 (C160404) and J1 (C474892) from LCSC in the same shipment and solder them on the outer side (or order double-sided assembly with J3 added to the files).
5. **Optional 30-second caliper check of the star**: hole centres 8.6 mm, holes Ø2.0. Anything in 7.4–9.8 mm fits the slots; if it were ≥Ø2.5 the screws could be M2.5 and the slot width should go to 2.9 (`SLOT_DRILL[0]`).
6. **Heat sink**: any ≤32 × 32 mm (or Ø35) sink with a ≥3 mm base, ≥15–20 mm tall; drill/tap 2 × M2 at 8.6 mm. It sits on the **back** of the stick-out.
7. **Open the project once in the KiCad GUI** (accept the format upgrade), press B, run DRC + ERC there; expect the same clean result and the library warnings gone.
8. **Program the DAC EEPROM to 0 before wiring the LED** (README §4), then **bench-check the dimming curve once**: sweep the DAC and measure LED current (or camera grey level). Expect ≈0 at ≤0.5 V and 1 A at ≥2.5 V; if the LDD's DIM pull-up distorts the curve, add a per-rig lookup table to `devices/ir_led.py` (`code_for_pct`).
9. Optional polish: 3D models for the LDD / Qwiic receptacle; fiducials if JLCPCB asks for them (they usually don't for small boards).

---

## 7. Likely next tasks and where to touch

| task | where |
|---|---|
| change star hole spacing / round holes | `footprints.py` → `STAR_HOLE_PITCH`, `SLOT_DRILL`; regenerate |
| different heat sink | `footprints.py` → `HEATSINK_W` (keep-out/courtyard), `HEATSINK_D` (drawing), `BOT_PAD_W`; `pcb.py` → `BH`, `PLACEMENT["TP1"]` |
| longer/shorter stick-out, move the star | `pcb.py` → `BH`, `PLACEMENT["TP1"]` (everything heat-sink related keys off `TPX, TPY`), `SILK_TEXTS`, `J2` position |
| a different camera / hole pattern | `pcb.py` → `BW`, `CAM_H`, `CAM_HOLE`, the `H1–H4` entries, `GND_ZONE` |
| move a part to the other side | `pcb.py` → 4th element of its `PLACEMENT` tuple (`"B"`), then re-route its tracks (pad coordinates don't change, only the layer) |
| different I²C connector | `footprints.py` → `fp_JST_SH_1x04_H()` / `fp_Grove_SMD_H()` (pads from the datasheet), `design.py` → `PARTS["J3"]` (footprint name, MPN, LCSC) + the J3 wiring/pin order in `build_schematic`, `pcb.py` → `J3` position + the J3 tracks/vias (U2 is placed to line up with the connector's SDA/SCL pins) |
| replace LDD-1000L by LDD-1200L/1500L | same footprint; change `PARTS["U1"]` value in `design.py`; note the LED is rated 1.5 A max |
| drive two stars in series | LDD Vout 2–30 V allows it; needs VIN ≥ Vf_total + 2 V; second thermal pad = second `TP` footprint instance |
| regenerate fabrication outputs | `tools/export_jlcpcb.sh` (Gerbers + drills → `jlcpcb/`); `jlcpcb/BOM_JLCPCB.csv` / `CPL_JLCPCB.csv` come out of `validate.py`/`build.py` (CPL origin = the aux axis at the board's lower-left corner, Y up, rotation −180…180: identical to `kicad-cli pcb export pos --use-drill-file-origin`) |
| add/remove a part from the PCBA files | `design.py` → `PARTS[ref]` props: `"LCSC": "C…"` includes it, `"PCBA": "no"` excludes it (J1 and J3 today); `build.py`'s `pcba_part()` additionally refuses through-hole footprints and outer-side (`"B"`) parts |
| Pi-side software | VRFarm `devices/ir_led.py` (MCP4725 via smbus2); README §4 has a standalone snippet |
| second board on the same Pi | omit R7 and strap A0 to 3V3 (0x61); rig YAML `i2c_address: '0x61'`; the Grove hat has three I²C ports |
| back to a GPIO-gated enable | re-add an EN wire: R1's left end goes to the EN pin instead of 3V3 (rev 2.2 generator in Dropbox history) |

---

## 8. Reference data used (so nobody has to re-research)

**Raspberry Pi HQ Camera** (mechanical drawing, local copy `docs/datasheets/RPi_HQ_Camera_mechanical_drawing.pdf`, source https://datasheets.raspberrypi.com/hq-camera/hq-camera-mechanical-drawing.pdf): PCB 38 × 38 mm, four Ø2.5 holes 4 mm in from the edges (30 × 30 pattern), sensor centred; 15-pin FFC connector on the back at the tripod-mount edge, **2.75 mm proud of the PCB back, 5.71 mm deep along the board**; tripod boss 13.97 mm wide below that edge, flush with the PCB back; C/CS mount ring Ø36 on the front, 18.58 mm overall depth from the mount face to the connector back; back-focus ring lock tab at the opposite edge (10.16 wide, 0.8 slot).

**JST SH series, SM04B-SRSS-TB(LF)(SN)** (LCSC C160404, JLCPCB extended, $0.22, 60 738 in stock on 2026-11-03; JST catalogue pages in `docs/datasheets/`): 1.0 mm pitch crimp-style wire-to-board, side-entry SMD header 2.95 mm high, 6.25 mm deep, 1 A / 50 V, AWG 32–28, −25…85 °C; recommended layout: signal pads 0.6 × 1.8 at 1.0 mm (KiCad uses 0.6 × 1.55 at y = −2), mechanical pads 1.2 × 1.8 at ±2.8, 4 mm in front of the signal pads. This is the Qwiic (SparkFun) / STEMMA QT (Adafruit) connector; cables are 1:1, pin 1 = GND. Cheaper SH-compatible clones at LCSC: SHOU HAN 1.0-4P WT C2906270 (24 k), Megastar ZX-SH1.0-4PWT C7430446 (190 k), XYECONN XY-SM04B-SRSS-TB C51940130 (18 k).

**SHOU HAN 2.0-4PWTDK** (rev 3.0 Grove receptacle, LCSC C3008564, JLCPCB extended, $0.07, 9 133 in stock on 2026-11-02; datasheet rev A 2017-09 in `docs/datasheets/`): HY-series 2.0 mm wire-to-board receptacle, right-angle SMD with latch window, 4P housing 12.0 (DIM C) × 6.65 deep × 5.2 high, DIM A 6.0 (pin span), DIM B 8.9; recommended PCB layout: signal pads 1.2 × 3.8 at 2.0 pitch extending 0.75 mm under the housing, mechanical pads 1.2 × 3.7 at ±5.2 whose near edge is 3.85 mm in front of the signal pads (they reach 1.65 mm past the housing's front face); 2 A / 250 V, contacts phosphor bronze tin-plated, housing nylon beige, −25…85 °C, AWG 28–32, "applicable PC board thickness 0.6–1.2 mm" is a family spec for the through-hole variants (this one is SMD).

**KEFA KF350-3.5-2P** (LCSC C474892, extended, $0.12, 17 k in stock; datasheet KF350-3.50 rev A 2021-03 in `docs/datasheets/`): 3.5 mm pitch screw terminal, 2–24 poles, body 7.0 (2P) × 6.9 × 8.8 mm, pins Ø0.8 × 3.4 mm in Ø1.0 holes, pins 2.8 mm from the screw side / 4.1 mm from the wire side, 10 A / 300 V UL, wire 24–18 AWG, M2 screws 0.2 N·m, −40…105 °C, PA66 UL94V-0.

**JLCPCB part checks (2026-11-02/03, parts API)**: basic parts C20526 MMBT3904, C25900 4.7k 0402, C25792 47k 0402, C25105 33R 0402, C17168 0R 0402, C1525 100n 0402, C15849 1µ 0603, C15850 10µ 0805; extended C144198 MCP4725A0T-E/CH ($0.96, 7 k), C160404 JST SH receptacle, C474892 KF350 (and C3008564 Grove receptacle, no longer used). Mean Well LDD-1000L is C5259137 (0 stock), LDD-1000LS (SMD) C17342933 (0 stock).

**Mean Well LDD-1000L** (LDD-L series spec sheet): Vin 6–36 V, Vout 2–30 V, Io 1000 mA ±5 %, efficiency ≤95 %, switching 1 MHz (1000–1500 mA models), input filter: capacitor, max external output cap 2.2 µF. DIM (vs −Vin): **PWM** high 2.6–5.5 V or open = on, <0.4 V or short = off, 100–500 Hz; **analog** 0.5–2.5 V; quiescent 1 mA when dimmed off at 24 V. Only the 1000/1200/1500 mA L-models have analog dimming (300–700 mA are PWM-only). Pin style numbering per §2.4. Datasheet: https://www.meanwell.com/Upload/PDF/LDD-L/LDD-L-SPEC.PDF (mirror: https://www.emas.cz/files/2007909/s6dGztTo/elosos2073692.pdf). Hakan's module label: `LDD-1000L, I/P DC 6-36V, O/P DC 2-30V 1000mA`.

**ILS ILH-IS01-85SN-SC201-WIR200** (RS 191-8849, bag label photo in `docs/photos/`): ams-OSRAM SFH 4770S (SYNIOS P2720, 2.0 × 2.75 mm package) 850 nm emitter on a 20 × 20 × 2.2 mm aluminium star marked `ILH-SY01 VER B`, **Vf 3.00–3.60 V at IF = 1 A**, max continuous 1500 mA, surge 3 A, radiant intensity 370 mW/sr at 1 A, 120° half-angle, 200 mm leads (red = anode / "+", black = cathode / "−"), Tc range −40…125 °C. Datasheets (local copies in `docs/datasheets/`): IR version V1 2019 https://www.farnell.com/datasheets/3164642.pdf ; visible SYNIOS version V2 2020 https://i-led.co.uk/PDFs/SYNOIS/ILH-SY01-xxxx-SC2x1-WIR200%20V2.pdf (both: "Technical Drawing — coming soon", "mounting holes using M3 screws" = boilerplate). Notes: must be mounted on a metal heat sink with thermal grease, **insulate metal screws with synthetic washers**. The star has 2 wired pads + 2 unused duplicate pad pairs (same nets; do not need wiring).

**ILS 20 mm star mechanical drawing**: "Bare IMS PCB for Single LED, ILH-xxxx-PCB VER A" (ILS, July 2017) https://docs.rs-online.com/afb0/0900766b81630a00.pdf p.3, ILH-ON01-PCB: Ø20 (19 across the notches), **2 × Ø2 holes at 8.6 mm**, 6 × Ø3.5 notches equally spaced, 1.6 mm thick bare board (the LED version is 2.2 mm).

**Seeed Grove Base Hat for Raspberry Pi** (wiki https://wiki.seeedstudio.com/Grove_Base_Hat_for_Raspberry_Pi/, Eagle files in `docs/datasheets/Grove_Base_Hat_RPi_v1.0/`): 3 I²C Grove ports wired straight to the Pi's I²C (pad 1 SCL, 2 SDA, 3 3V3, 4 GND), **3.3 V only** ("do not input more than 3.3V"), no pull-ups on the hat; on-board ADC MCU (STM32F030 v1.1 / MM32F031 v1.0) at I²C **0x04 / 0x08**; "do not hot-plug". Grove connector = Seeed package `HW4-SMD-2.0` (value `4P-2.0;320110030`): 4 pads 1.0 × 2.5 at 2.0 mm pitch, 2 mechanical pads 2.0 × 2.5 at (±5.8, 3.123), body 12.2 × 5.1 in front of the pads. Grove I²C wire colours: yellow SCL, white SDA, red VCC, black GND (Grove System page).

**Microchip MCP4725** (DS22039D, https://ww1.microchip.com/downloads/en/DeviceDoc/22039d.pdf): 12-bit DAC, VDD 2.7–5.5 V = reference, rail-to-rail output, I²C 100/400 kHz/3.4 MHz, address `1100 A2 A1 A0` with A2A1 factory-programmed (A0 variant → 00 → 0x60/0x61), A0 from the pin. SOT-23-6: 1 VOUT, 2 VSS, 3 VDD, 4 SDA, 5 SCL, 6 A0. Fast-mode write = 2 bytes `[PD1 PD0 D11..D8][D7..D0]` (command bits 00); write DAC+EEPROM = command 0x60 + `[D11..D4][D3..D0 0000]`; 5-byte read returns status (RDY bit 7, POR bit 6), DAC, EEPROM. **Factory EEPROM: PD = 00, data = 0x800 (mid-scale).** Output short-circuit current ~15 mA.

**Heat sink (rev 2.0 choice, no longer fits)**: ILS `ILA-HSINK-STAR-50X20MM(-BLK-K)` = Fischer Elektronik **ICK S R 50 x 20**: Ø50 × 20 mm round pin heat sink, 3 mm plate, Rth 8.55 K/W natural convection. Datasheet copy in `docs/datasheets/`.

Footprint pad geometry for the generic parts (0402/0603/0805 R/C, SOT-23, SOT-23-6, M2.5/M3 holes, 1×N headers, 5.08 mm terminal block, SMA, radial cap) was copied from the KiCad footprint library master branch; courtyards/silk simplified.

---

## 9. Environment notes

- Generator deps: Python ≥3.9, `shapely`, `matplotlib`; `kiutils` optional (parse check). No KiCad needed to generate. On this Mac the system `python3` (miniforge) lacks them — use a venv (§4). `__pycache__` is git-ignored.
- Verification on this Mac: KiCad 10.0.0 at `/Applications/KiCad/KiCad.app` (`kicad-cli` works headless, no xvfb needed; its bundled Python has `pcbnew` 10.0.0-rc2 if scripting is ever needed); `rsvg-convert` (Homebrew librsvg) for SVG → PNG. KiCad 7's `pcbnew`-python path in `tools/verify_kicad.py` is legacy.
- `kicad-cli` leaves a `~IRDriver.kicad_pro.lck` file behind; it is git-ignored, delete it if it bothers you.
- Opening in KiCad 8/9/10 rewrites the files to the newer format on save — expected, harmless; after that, workflow A (regeneration) would produce KiCad-7-format files again, which still open fine.
- Generated previews: `preview_*.png` = KiCad renders (trust these); `check_*.png` = generator's matplotlib approximation (only for quick sanity checks, text/arc rendering is approximate).
- LCSC datasheet links on `lcsc.com/datasheet/...pdf` return an HTML page; the real file is the `datasheet.lcsc.com/datasheet/pdf/<hash>.pdf?productCode=…` link inside it. JLCPCB's parts search API (`jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList`, POST JSON with `keyword`) returns library type, stock, prices and `dataManualUrl` without a login.
