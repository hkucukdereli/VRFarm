"""Dimensions of the TI DLPDLCR230NPEVM (DLP LightCrafter Display 230NP EVM) — the projector
on every VRFarm follower — as plain data, shared by build_model.py (CadQuery) and
plot_hole_maps.py (matplotlib). No CAD imports here on purpose.

FRAME (TI's own "DLP038" assembly frame, so every number can be checked against TI's files):
  X = depth, the lens points to -X (lens front at X = -15.37)
  Y = up, the assembly stands on Y = -4.82 (base tabs + bracket wall edges), PCB top at 32.85
  Z = width, the 40-pin header side is -Z
For Z-up CAD, rotate -90 deg about X (Y -> Z).

SOURCES (numbers are TI's, read off their STEP geometry and drawings — see README.md):
  [A] TIDM757 "DLP038_Mechanical_092420" zip on the TIDA-080009 page: MCH051/052/053 drawings +
      STEP, and "DLP038 - REVF.STEP" (full assembly). Holes were measured on the STEP cylinders.
  [B] TIDM755 "P23 EVM TOP ASSEMBLY" drawing DLP038 rev F: envelope + fastener BOM.
  [C] DLPU103B user's guide, Figure 5-1: Young Optics FLA12-F engine drawing (4x dia 1.66).
Tolerance of the STEP read-off: +-0.02 mm on positions; drawing values are quoted verbatim.
"""

# --- envelope [B]: 81.40 wide x 87.96 deep (incl. lens) x 48.17 tall -------------------------
ENVELOPE = {"X": (-15.37, 72.05), "Y": (-4.82, 43.35), "Z": (-65.98, 15.42)}

# Modelled hole diameters follow TI's STEP: tapped holes are drawn at tap-drill size.
TAP = {"M1.6x0.35": 1.25, "M2x0.4": 1.60, "M2.5x0.45": 2.05}
CSK = {"through": 2.90, "csk_dia": 5.50, "csk_angle_deg": 90}   # M2.5 flat-head, TI item 4/23

# --- MCH051 BASE, 1.63 mm (gauge 14) black-anodised aluminium sheet [A] ---------------------
BASE = {
    "sheet": {"X": (0.18, 70.00), "Y": (0.00, 1.63), "Z": (-63.94, 13.37)},
    # rectangular opening under the engine (drawing 28.00..61.37 x 15.53..61.81, mapped)
    "cutout": {"X": (8.63, 42.00), "Z": (-48.41, -2.13)},
    # three pads lowered to the standing plane, each with one countersunk M2.5 clearance hole,
    # countersink opening DOWNWARD (Y = -4.82): flat-head screws go in from below into the
    # fan-bracket tab (2x) and the PCB corner standoff (1x). Not free for mounting.
    "tabs": {"Y": (-4.82, -3.19), "pad": 10.0},
    "csk_holes": [  # (X, Z), axis Y, TI "3X dia 2.90 / dia 5.50 90deg CSK"
        {"XZ": (3.50, 4.77), "for": "PCB corner standoff (M2.5 x 23 hex + M2.5 x 10 M-F)"},
        {"XZ": (51.81, -23.95), "for": "fan bracket MCH052 tab"},
        {"XZ": (62.31, -23.95), "for": "fan bracket MCH052 tab"},
    ],
    # vertical walls carry 4 tapped M2.5 holes at Y = -0.82, mating the formatter base's CSK screws
    "walls": {"Y": (-4.82, 1.63), "t": 1.63,
              "back": {"X": (68.37, 70.00), "Z": (-63.94, 13.37)},
              "side_negZ": {"Z": (-63.94, -62.31), "X": (0.18, 70.00)},
              "side_posZ": {"Z": (11.74, 13.37), "X": (44.55, 70.00)}},
    "m25_wall_holes": [  # tapped M2.5x0.45, horizontal
        {"wall": "back", "axis": "X", "YZ": (-0.82, -23.95)},
        {"wall": "side_negZ", "axis": "Z", "XY": (12.64, -0.82)},
        {"wall": "side_negZ", "axis": "Z", "XY": (57.27, -0.82)},
        {"wall": "side_posZ", "axis": "Z", "XY": (57.27, -0.82)},
    ],
    # tapped M1.6x0.35 in the sheet (engine fixing, TI item 17: 3x M1.6 x 12 socket head).
    # Table only — they sit inside the simplified cutout, so they are not cut in the solid.
    "m16_sheet_holes": [(10.76, -42.42), (39.06, -39.72), (39.06, -15.22)],
}

# --- MCH053 FORMATTER BASE, 2.05 mm (gauge 12) L-bracket [A] -------------------------------
# Two full walls (back, -Z side) + two short walls, L-shaped top flange that carries the PCB.
FORMATTER_BASE = {
    "t": 2.05,
    "walls": {"Y": (-4.82, 22.83),
              "back": {"X": (70.00, 72.05), "Z": (-65.98, 15.42)},
              "side_negZ": {"Z": (-65.98, -63.93), "X": (-6.05, 72.05)},
              "front_short": {"X": (-6.05, -4.00), "Z": (-65.98, -31.23)},
              "side_posZ_short": {"Z": (13.37, 15.42), "X": (44.55, 72.05)}},
    "flange": {"Y": (22.83, 24.88),
               "back_leg": {"X": (44.55, 72.05), "Z": (-65.98, 15.42)},     # 27.50 wide
               "side_leg": {"X": (-6.05, 72.05), "Z": (-65.98, -31.23)}},   # 34.75 wide
    "flange_holes": [  # axis Y, through the top flange
        {"XZ": (3.50, -35.50), "thread": "M2.5x0.45", "for": "PCB spacer (M3 round spacer 6 mm + M2.5 x 10 screw)"},
        {"XZ": (16.84, -60.26), "thread": "M2.5x0.45", "for": "PCB spacer (as above)"},
        {"XZ": (60.40, -51.74), "thread": "M2.5x0.45", "for": "PCB spacer (as above)"},
        {"XZ": (49.11, 3.73), "thread": "M2x0.4", "for": "PCB support (M3 spacer 5 mm + M2 x 12 screw)"},
        {"XZ": (66.11, 3.73), "thread": "M2x0.4", "for": "PCB support (as above)"},
    ],
    "wall_m2_holes": [  # tapped M2x0.4, horizontal, at Y = 20.46 (TI: 8X M2 incl. the 2 above)
        {"wall": "back", "axis": "X", "YZ": (20.46, 1.37)},
        {"wall": "back", "axis": "X", "YZ": (20.46, -51.92)},
        {"wall": "front_short", "axis": "X", "YZ": (20.46, -48.25)},
        {"wall": "side_negZ", "axis": "Z", "XY": (57.28, 20.46)},
        {"wall": "side_negZ", "axis": "Z", "XY": (12.62, 20.46)},
        {"wall": "side_posZ_short", "axis": "Z", "XY": (57.28, 20.46)},
    ],
    "wall_csk_holes": [  # dia 2.90 + dia 5.50 90deg CSK on the OUTER face, at Y = -0.80,
                         # mating the base's tapped M2.5 wall holes (M2.5 x 5 flat-head)
        {"wall": "back", "axis": "X", "YZ": (-0.80, -23.96)},
        {"wall": "side_negZ", "axis": "Z", "XY": (57.28, -0.80)},
        {"wall": "side_negZ", "axis": "Z", "XY": (12.62, -0.80)},
        {"wall": "side_posZ_short", "axis": "Z", "XY": (57.28, -0.80)},
    ],
}

# --- MCH052 FAN BRACKET (1.63 mm) + 25 x 25 x 10 fan [A][B] ----------------------------------
FAN = {"box": {"X": (44.51, 69.62), "Y": (-2.80, 22.31), "Z": (-42.07, -31.95)},
       "center_XY": (57.07, 9.76), "bore_dia": 24.0}
FAN_BRACKET = {
    "plate": {"X": (44.56, 69.57), "Y": (-3.19, 22.31), "Z": (-31.95, -30.32)},
    "tab": {"X": (44.56, 69.57), "Y": (-3.19, -1.56), "Z": (-31.95, -18.95)},
    "fan_holes_M2": [(57.07 + dx, 9.76 + dy) for dx in (-10, 10) for dy in (-10, 10)],  # (X, Y), axis Z
    "tab_holes_M25": [(51.81, -23.95), (62.31, -23.95)],   # (X, Z), axis Y, under the base CSK holes
}

# --- DLP038 PCB (formatter board) [A] --------------------------------------------------------
PCB = {
    "box": {"X": (0.00, 70.00), "Y": (30.81, 32.85), "Z": (-63.73, 9.03)},   # 70.00 x 72.76 x 2.04
    "mount_holes": [  # axis Y
        {"XZ": (3.50, -35.50), "dia": 2.69, "type": "M2.5 clearance", "support": "6 mm spacer on flange"},
        {"XZ": (3.50, 4.77), "dia": 2.69, "type": "M2.5 clearance", "support": "hex standoff column from base tab"},
        {"XZ": (16.84, -60.26), "dia": 2.69, "type": "M2.5 clearance", "support": "6 mm spacer on flange"},
        {"XZ": (60.40, -51.74), "dia": 2.69, "type": "M2.5 clearance", "support": "6 mm spacer on flange"},
        {"XZ": (49.11, 3.73), "dia": 2.39, "type": "M2 clearance", "support": "5 mm spacer on flange"},
        {"XZ": (66.11, 3.73), "dia": 2.39, "type": "M2 clearance", "support": "5 mm spacer on flange"},
    ],
    "components": {  # boxes on the top face (Y from 32.85)
        "J2_40pin_header": {"X": (63.97, 69.05), "Y": (32.85, 41.38), "Z": (-62.85, -12.05)},
        "JPWR1_barrel_jack": {"X": (0.94, 12.94), "Y": (32.85, 42.85), "Z": (-61.20, -45.95)},
        "J4_fan_connector": {"X": (5.14, 18.14), "Y": (32.85, 43.35), "Z": (-45.57, -35.57)},
    },
    "caps": [  # dia 10 electrolytics, axis Y, Y 32.85..43.35
        {"XZ": (5.23, -13.96), "dia": 10.0}, {"XZ": (5.23, -26.41), "dia": 10.0}],
}
STANDOFFS = [  # cylinders, axis Y
    {"XZ": (3.50, -35.50), "dia": 4.75, "Y": (24.88, 30.81), "what": "M3 round spacer 6 mm"},
    {"XZ": (16.84, -60.26), "dia": 4.75, "Y": (24.88, 30.81), "what": "M3 round spacer 6 mm"},
    {"XZ": (60.40, -51.74), "dia": 4.75, "Y": (24.88, 30.81), "what": "M3 round spacer 6 mm"},
    {"XZ": (49.11, 3.73), "dia": 5.00, "Y": (24.88, 29.86), "what": "M3 round spacer 5 mm"},
    {"XZ": (66.11, 3.73), "dia": 5.00, "Y": (24.88, 29.86), "what": "M3 round spacer 5 mm"},
    {"XZ": (3.50, 4.77), "dia": 4.50, "Y": (-3.19, 30.81), "what": "M2.5 x 23 hex F-F + M2.5 x 10 M-F standoff"},
]

# --- Young Optics FLA12-F optical engine [A][C] ---------------------------------------------
ENGINE = {
    "body": {"X": (7.19, 40.46), "Y": (-2.97, 20.09), "Z": (-45.90, 7.12)},   # 33.28 x 23.07 x 53.03
    "lens": {"axis_YZ": (10.39, -11.32), "dia": 16.9, "X": (-9.27, 7.19)},
    "lens_hood": {"X": (-15.37, -9.27), "Y": (2.64, 18.14), "Z": (-26.46, 3.82)},
    "heatsinks": {  # LED heat sinks MCH056/057 (blue) and 2x (red/green)
        "red_green_1": {"X": (6.76, 25.76), "Y": (2.18, 21.18), "Z": (-61.45, -46.45)},
        "red_green_2": {"X": (26.26, 45.26), "Y": (2.18, 21.18), "Z": (-61.45, -46.45)},
        "blue": {"X": (-0.37, 6.63), "Y": (2.18, 21.18), "Z": (-45.22, -26.22)},
    },
    # [C] Figure 5-1, top view of the engine's 34.13 x 54.99 top plate: 4X dia 1.66 (M2 thread-
    # forming, TI item 7: 4x M2 x 12). Plate coords (u along +X from the lens-side edge, v across).
    # Mapping to the assembly frame is INFERRED (v = 0 at the engine's +Z end); confirm on the unit.
    "plate_holes_uv": [(3.63, 3.54), (31.42, 6.24), (31.42, 30.73), (7.53, 50.64)],
    "plate_dia": 1.66,
    "plate_origin_X": 7.19, "plate_origin_Z": 7.12, "plate_v_sign": -1,
}

def engine_plate_holes_XZ():
    ox, oz, s = ENGINE["plate_origin_X"], ENGINE["plate_origin_Z"], ENGINE["plate_v_sign"]
    return [(round(ox + u, 2), round(oz + s * v, 2)) for u, v in ENGINE["plate_holes_uv"]]

# --- fastener BOM, TIDM755 sheet 2 [B] (what each hole type is for) --------------------------
FASTENERS_TI_BOM = [
    ("2", "801160", "M2.5 x 10 mm M-F standoff", 1),
    ("3", "801810", "4.5 mm OD hex standoff F-F, M2.5-0.45 x 23 mm, aluminium", 1),
    ("4", "513005", "M2.5-0.45 x 5 mm screw", 7),
    ("7", "507775", "M2-0.4 x 12 mm pan-head machine screw (engine)", 4),
    ("12", "R30-6200614", "round spacer M3 aluminium 6 mm", 3),
    ("13", "507855", "M2.5-0.45 x 10 mm machine screw (PCB)", 3),
    ("14", "512978", "M2-0.4 x 12 mm machine screw (PCB, M2 supports)", 2),
    ("15", "96817A746", "M1.2 x 4 mm screw (heat sinks)", 6),
    ("16", "336255", "#2 x 1/4 in x .032 nylon flat washer", 4),
    ("17", "91290A043", "M1.6 x 12 mm socket-head screw (engine to base)", 3),
    ("22", "R30-6200514", "round spacer M3 aluminium 5 mm", 2),
    ("23", "507823", "M2.5-0.45 x 5 mm machine screw", 1),
]
