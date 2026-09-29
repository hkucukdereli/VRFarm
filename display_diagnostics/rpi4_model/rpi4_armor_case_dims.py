"""Raspberry Pi 4 Model B + Joy-IT Armor Case "BLOCK" (RB-AlucaseP4+07) — dimensions as plain data,
shared by build_model.py (CadQuery) and plot_hole_maps.py (matplotlib). No CAD imports here.

FRAME (Raspberry Pi drawing convention):
  X = along the 85 mm edge, 0 at the microSD end, 85 at the USB/Ethernet end
  Y = along the 56 mm edge, 0 at the USB-C/HDMI/audio edge, 56 at the GPIO-header edge
  Z = up, 0 = PCB bottom face, 1.4 = PCB top face

SOURCES
  [P] Raspberry Pi 4 Model B mechanical drawing (datasheets.raspberrypi.com/rpi4/
      raspberry-pi-4-mechanical-drawing.pdf): outline, corner radius, holes, connector centres,
      component heights ("Z=") — exact.
  [J] Joy-IT RB-AlucaseP4+07 datasheet + manual: only the two envelopes (top 69 x 56 x 15.5,
      bottom 87 x 56 x 7.5), "four delivered screws", allen key, thermal pads. Everything else
      about the case (fins, slots, posts, vertical stack-up) is ESTIMATED from the product
      photos — see CASE_ESTIMATED below and measure before relying on it.
  Connector protrusions beyond the board edge are typical values (+-0.5 mm), not from [P].
"""

# --- Raspberry Pi 4 B board [P] ---------------------------------------------------------------
BOARD = {"X": (0.0, 85.0), "Y": (0.0, 56.0), "Z": (0.0, 1.4), "corner_radius": 3.0}
HOLES = {  # dia 2.7 through, M2.5 clearance; 58 x 49 pattern, 3.5 from the edges
    "dia": 2.7,
    "XY": [(3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)],
}
# boxes: X, Y ranges on the board, Z range absolute (PCB top = 1.4). Centres from [P].
CONNECTORS = {
    "USB_C_power":      {"X": (6.7, 15.7),   "Y": (-1.3, 6.1),  "Z": (1.4, 4.6),  "note": "centre x 11.2, Z=3.2 [P]"},
    "micro_HDMI_0":     {"X": (22.3, 29.7),  "Y": (-0.8, 6.9),  "Z": (1.4, 4.4),  "note": "centre x 26.0, Z=3.0 [P]"},
    "micro_HDMI_1":     {"X": (35.8, 43.2),  "Y": (-0.8, 6.9),  "Z": (1.4, 4.4),  "note": "centre x 39.5, Z=3.0 [P]"},
    "audio_jack":       {"X": (50.5, 56.5),  "Y": (-2.5, 9.5),  "Z": (1.4, 7.4),  "note": "centre x ~53.5 (read off drawing), Z=6.0 [P]"},
    "RJ45_ethernet":    {"X": (65.5, 87.0),  "Y": (37.75, 53.75), "Z": (1.4, 14.9), "note": "centre y 45.75, 16 wide, Z=13.5 [P]; +2.0 past edge"},
    "USB_A_stack_1":    {"X": (69.5, 87.5),  "Y": (20.45, 33.55), "Z": (1.4, 17.4), "note": "centre y 27, 13.1 wide, Z=16.0 [P]; +2.5 past edge"},
    "USB_A_stack_2":    {"X": (69.5, 87.5),  "Y": (2.45, 15.55),  "Z": (1.4, 17.4), "note": "centre y 9, 13.1 wide, Z=16.0 [P]; +2.5 past edge"},
    "GPIO_header_2x20": {"X": (7.1, 57.9),   "Y": (49.96, 55.04), "Z": (1.4, 9.9),  "note": "centre x 32.5, 50.8 x 5.08, Z=8.5 [P]; pin 1 = inner row, x 8.4"},
    "PoE_header_2x2":   {"X": (57.5, 62.6),  "Y": (45.0, 50.1),   "Z": (1.4, 9.9),  "note": "approximate position"},
    "CSI_camera_FPC":   {"X": (43.0, 47.0),  "Y": (1.5, 23.5),    "Z": (1.4, 6.9),  "note": "Z=5.5 [P]; position read off drawing"},
    "DSI_display_FPC":  {"X": (2.0, 6.0),    "Y": (20.0, 42.0),   "Z": (1.4, 6.9),  "note": "Z=5.5 [P]; position read off drawing"},
    "microSD_card":     {"X": (-2.5, 12.5),  "Y": (22.0, 34.0),   "Z": (-1.5, 0.0), "note": "under the board, card protrudes 2.5"},
    "SoC_BCM2711":      {"X": (21.75, 36.75), "Y": (25.9, 40.9),  "Z": (1.4, 3.8),  "note": "Z=2.4 [P]; centre read off drawing"},
    "RAM":              {"X": (38.5, 50.5),  "Y": (25.5, 40.5),   "Z": (1.4, 2.6),  "note": "approximate"},
    "WiFi_can":         {"X": (6.5, 17.0),   "Y": (37.3, 49.8),   "Z": (1.4, 2.9),  "note": "approximate"},
}

# --- Joy-IT Armor Case "BLOCK", RB-AlucaseP4+07 [J] ------------------------------------------
CASE_PUBLISHED = {
    "top_block":    {"L": 69.0, "W": 56.0, "H": 15.5},     # "Dimensions topside 69 x 56 x 15.5 mm"
    "bottom_plate": {"L": 87.0, "W": 56.0, "H": 7.5},      # "Dimensions underside 87 x 56 x 7.5 mm"
    "weight_g": 107, "material": "CNC milled aluminium alloy, black",
    "fixing": "four delivered screws through the Pi's four holes, from below, allen key (socket head)",
}
CASE_ESTIMATED = {   # <-- measure these on the real part; +-1 mm laterally, +-3 mm vertically
    "top_block_X0": 0.0,          # block starts at the microSD edge and ends at 69 (USB end open)
    "block_bottom_gap": 3.0,      # block underside above the PCB top (photos: level with USB-C top)
    "screw": "M2.5 socket head, ~25 mm, from below into the top block's threaded posts",
    "post_dia": 6.0, "post_thread": "M2.5x0.45", "post_thread_depth": 8.0,
    "fins": {"direction": "along X", "n": 10, "first_center_Y": 5.5, "pitch": 5.0,
             "groove_width": 2.6, "groove_depth": 9.0, "X_margin": 3.0},
    "gpio_slot": {"X": (6.5, 58.5), "Y": (49.5, 56.0)},           # header pokes through / exposed
    "csi_ribbon_slot": {"X": (43.5, 46.5), "Y": (6.0, 30.0)},     # over the camera connector
    "front_pocket": {"Y": (0.0, 9.0), "height_above_pcb": 7.0},  # clears audio jack / HDMI / USB-C
    "bottom_plate_X0": -1.0,      # 87 long over an 85 board: 1 mm each end
    "plate_thickness": 3.5, "boss_dia": 6.0, "boss_height": 4.0, # 3.5 + 4.0 = 7.5 published
    "screw_head": {"dia": 4.5, "height": 2.5, "counterbore_dia": 4.8},
}
TAP = {"M2.5x0.45": 2.05}

def top_block_Z():
    z0 = BOARD["Z"][1] + CASE_ESTIMATED["block_bottom_gap"]
    return (z0, z0 + CASE_PUBLISHED["top_block"]["H"])

def bottom_plate_Z():
    h = CASE_PUBLISHED["bottom_plate"]["H"]
    return (-h, -h + CASE_ESTIMATED["plate_thickness"])   # plate slab; bosses fill the rest up to 0

ENVELOPE_NOTE = ("about 90 x 57 x 27 mm including the microSD card, the USB/Ethernet overhang and the "
                 "estimated 3 mm block gap; the two case parts alone are 87 x 56 x (7.5 + 1.4 + 3.0 + 15.5)")
