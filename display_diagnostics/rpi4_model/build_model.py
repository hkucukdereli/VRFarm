"""Build the simplified Raspberry Pi 4 B + Joy-IT Armor Case "BLOCK" model; export STEP/STL/SVG.

    ~/cadenv/bin/python build_model.py      # same venv as ../projector_model (pip install cadquery)

Boxes and cylinders at the coordinates in rpi4_armor_case_dims.py. Frame: X along the 85 mm edge
(microSD at 0), Y along the 56 mm edge (USB-C edge at 0), Z up with the PCB bottom at 0.
"""
from pathlib import Path
import cadquery as cq
from cadquery import Vector as V
import rpi4_armor_case_dims as D

HERE = Path(__file__).resolve().parent
OUT = "rpi4b_joyit_armor_case_simplified"

def box(x, y, z):
    return cq.Solid.makeBox(x[1] - x[0], y[1] - y[0], z[1] - z[0], pnt=V(x[0], y[0], z[0]))

def cyl_z(x, y, z0, z1, dia):
    return cq.Solid.makeCylinder(dia / 2, z1 - z0, pnt=V(x, y, z0), dir=V(0, 0, 1))

def fuse(*s):
    out = s[0]
    for o in s[1:]:
        out = out.fuse(o)
    return out

def cut(s, *tools):
    for t in tools:
        s = s.cut(t)
    return s

# ---- Raspberry Pi 4 B --------------------------------------------------------------------------
def build_pcb():
    B = D.BOARD; r = B["corner_radius"]
    pcb = (cq.Workplane("XY").box(B["X"][1], B["Y"][1], B["Z"][1], centered=False)
           .edges("|Z").fillet(r).val())
    for x, y in D.HOLES["XY"]:
        pcb = cut(pcb, cyl_z(x, y, -1, 3, D.HOLES["dia"]))
    return pcb

def build_connectors():
    return fuse(*[box(c["X"], c["Y"], c["Z"]) for c in D.CONNECTORS.values()])

# ---- Joy-IT Armor Case "BLOCK" -----------------------------------------------------------------
def build_top_block():
    C, E = D.CASE_PUBLISHED["top_block"], D.CASE_ESTIMATED
    z0, z1 = D.top_block_Z()
    x0 = E["top_block_X0"]; X = (x0, x0 + C["L"]); Y = (0.0, C["W"])
    blk = box(X, Y, (z0, z1))
    f = E["fins"]
    for k in range(f["n"]):                       # grooves along X, from the top face down
        yc = f["first_center_Y"] + k * f["pitch"]
        if yc + f["groove_width"] / 2 > E["gpio_slot"]["Y"][0] - 1.0:
            continue                              # the GPIO slot region carries no groove
        blk = cut(blk, box((X[0] + f["X_margin"], X[1] - f["X_margin"]),
                           (yc - f["groove_width"] / 2, yc + f["groove_width"] / 2),
                           (z1 - f["groove_depth"], z1 + 1)))
    g = E["gpio_slot"]; blk = cut(blk, box(g["X"], (g["Y"][0], g["Y"][1] + 1), (z0 - 1, z1 + 1)))
    s = E["csi_ribbon_slot"]; blk = cut(blk, box(s["X"], s["Y"], (z0 - 1, z1 + 1)))
    p = E["front_pocket"]; ztop = D.BOARD["Z"][1] + p["height_above_pcb"]
    blk = cut(blk, box((X[0] - 1, X[1] + 1), (p["Y"][0] - 1, p["Y"][1]), (z0 - 1, ztop)))
    # threaded posts down to the PCB top at the four holes
    pcb_top = D.BOARD["Z"][1]
    for x, y in D.HOLES["XY"]:
        blk = fuse(blk, cyl_z(x, y, pcb_top, z0 + 0.01, E["post_dia"]))
        blk = cut(blk, cyl_z(x, y, pcb_top - 1, pcb_top + E["post_thread_depth"], D.TAP[E["post_thread"]]))
    return blk

def build_bottom_plate():
    C, E = D.CASE_PUBLISHED["bottom_plate"], D.CASE_ESTIMATED
    zp = D.bottom_plate_Z(); x0 = E["bottom_plate_X0"]
    plate = box((x0, x0 + C["L"]), (0.0, C["W"]), zp)
    for x, y in D.HOLES["XY"]:
        plate = fuse(plate, cyl_z(x, y, zp[1] - 0.01, 0.0, E["boss_dia"]))
        sh = E["screw_head"]
        plate = cut(plate, cyl_z(x, y, zp[0] - 1, 1.0, 2.7),
                    cyl_z(x, y, zp[0] - 1, zp[0] + sh["height"], sh["counterbore_dia"]))
    return plate

def build_screws():
    E = D.CASE_ESTIMATED; zp = D.bottom_plate_Z(); sh = E["screw_head"]
    pcb_top = D.BOARD["Z"][1]
    parts = []
    for x, y in D.HOLES["XY"]:
        parts.append(cyl_z(x, y, zp[0], zp[0] + sh["height"], sh["dia"]))                      # head
        parts.append(cyl_z(x, y, zp[0] + sh["height"], pcb_top + E["post_thread_depth"], 2.5))  # shank
    return fuse(*parts)

PARTS = [
    ("rpi4b_pcb", build_pcb, (0.05, 0.40, 0.15)),
    ("rpi4b_connectors", build_connectors, (0.70, 0.70, 0.72)),
    ("joyit_top_block", build_top_block, (0.12, 0.12, 0.12)),
    ("joyit_bottom_plate", build_bottom_plate, (0.18, 0.18, 0.18)),
    ("screws_M2.5", build_screws, (0.55, 0.55, 0.58)),
]

def main():
    assy = cq.Assembly(name="RPi4B_JoyIT_ArmorCase_simplified"); shapes = []
    for name, builder, rgb in PARTS:
        s = builder(); shapes.append(s)
        assy.add(cq.Workplane().add(s), name=name, color=cq.Color(*rgb))
        print(f"  {name:22s} volume {s.Volume():9.0f} mm^3")
    comp = cq.Compound.makeCompound(shapes); bb = comp.BoundingBox()
    print(f"model envelope X {bb.xmin:.2f}..{bb.xmax:.2f} ({bb.xlen:.2f})  Y {bb.ymin:.2f}..{bb.ymax:.2f} ({bb.ylen:.2f})  Z {bb.zmin:.2f}..{bb.zmax:.2f} ({bb.zlen:.2f})")
    assy.save(str(HERE / f"{OUT}.step"))
    wp = cq.Workplane().add(comp)
    cq.exporters.export(wp, str(HERE / f"{OUT}.stl"), tolerance=0.05, angularTolerance=0.15)
    cq.exporters.export(wp, str(HERE / f"{OUT}_iso.svg"),
                        opt={"width": 1100, "height": 750, "marginLeft": 10, "marginTop": 10, "showAxes": True,
                             "projectionDir": (-1.4, -1.8, 1.3), "strokeWidth": 0.4, "showHidden": False})
    print("wrote", f"{OUT}.step", f"{OUT}.stl", f"{OUT}_iso.svg")

if __name__ == "__main__":
    main()
