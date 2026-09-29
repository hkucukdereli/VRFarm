"""Build the simplified DLPDLCR230NPEVM model and export STEP / STL / SVG next to this file.

    python -m venv ~/cadenv && ~/cadenv/bin/pip install cadquery   # once (not in the vrfarm env)
    ~/cadenv/bin/python build_model.py

Every solid is a box or a cylinder placed at TI's coordinates (dlpdlcr230npevm_dims.py); holes
are cut at the diameters TI drew (tap-drill size for threads, 2.90 + 5.50 x 90deg countersink for
the M2.5 flat-heads). Sheet-metal bends, reliefs, the DMD flex tab and small components are left
out on purpose. Frame: X depth (lens -X), Y up, Z width — see the dims module docstring.
"""
from pathlib import Path
import cadquery as cq
from cadquery import Vector as V
import dlpdlcr230npevm_dims as D

HERE = Path(__file__).resolve().parent
OUT = "dlpdlcr230npevm_simplified"

# ---- primitives ----------------------------------------------------------------------------
def box(x, y, z):
    return cq.Solid.makeBox(x[1] - x[0], y[1] - y[0], z[1] - z[0], pnt=V(x[0], y[0], z[0]))

def bbox(spec):
    return box(spec["X"], spec["Y"], spec["Z"])

def cyl(axis, a, b, lo, hi, dia):
    """Cylinder along `axis`; (a, b) are the two other coordinates in X, Y, Z order."""
    r, h = dia / 2, hi - lo
    if axis == "X":
        return cq.Solid.makeCylinder(r, h, pnt=V(lo, a, b), dir=V(1, 0, 0))
    if axis == "Y":
        return cq.Solid.makeCylinder(r, h, pnt=V(a, lo, b), dir=V(0, 1, 0))
    return cq.Solid.makeCylinder(r, h, pnt=V(a, b, lo), dir=V(0, 0, 1))

def csk(axis, a, b, face, inward, d_hole=D.CSK["through"], d_csk=D.CSK["csk_dia"]):
    """90deg countersink cone starting on the outer face coordinate `face`, pointing `inward`
    (+1/-1 along `axis`), from d_csk down to d_hole."""
    h = (d_csk - d_hole) / 2
    if axis == "X":
        return cq.Solid.makeCone(d_csk / 2, d_hole / 2, h, pnt=V(face, a, b), dir=V(inward, 0, 0))
    if axis == "Y":
        return cq.Solid.makeCone(d_csk / 2, d_hole / 2, h, pnt=V(a, face, b), dir=V(0, inward, 0))
    return cq.Solid.makeCone(d_csk / 2, d_hole / 2, h, pnt=V(a, b, face), dir=V(0, 0, inward))

def fuse(*shapes):
    s = shapes[0]
    for o in shapes[1:]:
        s = s.fuse(o)
    return s

def cut(shape, *tools):
    for t in tools:
        shape = shape.cut(t)
    return shape

# ---- MCH051 base ---------------------------------------------------------------------------
def build_base():
    B = D.BASE
    sheet = bbox(B["sheet"])
    sheet = cut(sheet, box(B["cutout"]["X"], (-1, 3), B["cutout"]["Z"]))
    walls = [box(w["X"], B["walls"]["Y"], w["Z"]) for w in
             (B["walls"]["back"], B["walls"]["side_negZ"], B["walls"]["side_posZ"])]
    y0, y1 = B["tabs"]["Y"]; p = B["tabs"]["pad"] / 2
    tabs = [box((x - p, x + p), (y0, y1), (z - p, z + p)) for h in B["csk_holes"] for x, z in [h["XZ"]]]
    base = fuse(sheet, *walls, *tabs)
    for h in B["csk_holes"]:
        x, z = h["XZ"]
        base = cut(base, cyl("Y", x, z, y0 - 1, y1 + 1, D.CSK["through"]), csk("Y", x, z, y0, +1))
    d = D.TAP["M2.5x0.45"]
    for h in B["m25_wall_holes"]:
        w = B["walls"][h["wall"]]
        if h["axis"] == "X":
            y, z = h["YZ"]; base = cut(base, cyl("X", y, z, w["X"][0] - 1, w["X"][1] + 1, d))
        else:
            x, y = h["XY"]; base = cut(base, cyl("Z", x, y, w["Z"][0] - 1, w["Z"][1] + 1, d))
    return base

# ---- MCH053 formatter base -------------------------------------------------------------------
def build_formatter_base():
    F = D.FORMATTER_BASE
    W = F["walls"]; wy = W["Y"]
    parts = [box(W[k]["X"], wy, W[k]["Z"]) for k in ("back", "side_negZ", "front_short", "side_posZ_short")]
    fl = F["flange"]
    parts += [box(fl["back_leg"]["X"], fl["Y"], fl["back_leg"]["Z"]),
              box(fl["side_leg"]["X"], fl["Y"], fl["side_leg"]["Z"])]
    fb = fuse(*parts)
    for h in F["flange_holes"]:
        x, z = h["XZ"]; fb = cut(fb, cyl("Y", x, z, fl["Y"][0] - 1, fl["Y"][1] + 1, D.TAP[h["thread"]]))
    for h in F["wall_m2_holes"]:
        w = W[h["wall"]]; d = D.TAP["M2x0.4"]
        if h["axis"] == "X":
            y, z = h["YZ"]; fb = cut(fb, cyl("X", y, z, w["X"][0] - 1, w["X"][1] + 1, d))
        else:
            x, y = h["XY"]; fb = cut(fb, cyl("Z", x, y, w["Z"][0] - 1, w["Z"][1] + 1, d))
    for h in F["wall_csk_holes"]:
        w = W[h["wall"]]
        if h["axis"] == "X":     # back wall, outer face at max X
            y, z = h["YZ"]
            fb = cut(fb, cyl("X", y, z, w["X"][0] - 1, w["X"][1] + 1, D.CSK["through"]), csk("X", y, z, w["X"][1], -1))
        else:
            x, y = h["XY"]
            outer, inward = (w["Z"][0], +1) if h["wall"] == "side_negZ" else (w["Z"][1], -1)
            fb = cut(fb, cyl("Z", x, y, w["Z"][0] - 1, w["Z"][1] + 1, D.CSK["through"]), csk("Z", x, y, outer, inward))
    return fb

# ---- fan + bracket ---------------------------------------------------------------------------
def build_fan():
    f = bbox(D.FAN["box"]); cx, cy = D.FAN["center_XY"]
    return cut(f, cyl("Z", cx, cy, D.FAN["box"]["Z"][0] - 1, D.FAN["box"]["Z"][1] + 1, D.FAN["bore_dia"]))

def build_fan_bracket():
    FB = D.FAN_BRACKET
    br = fuse(bbox(FB["plate"]), bbox(FB["tab"]))
    cx, cy = D.FAN["center_XY"]; pz = FB["plate"]["Z"]
    br = cut(br, cyl("Z", cx, cy, pz[0] - 1, pz[1] + 1, D.FAN["bore_dia"]))
    for x, y in FB["fan_holes_M2"]:
        br = cut(br, cyl("Z", x, y, pz[0] - 1, pz[1] + 1, D.TAP["M2x0.4"]))
    ty = FB["tab"]["Y"]
    for x, z in FB["tab_holes_M25"]:
        br = cut(br, cyl("Y", x, z, ty[0] - 1, ty[1] + 1, D.TAP["M2.5x0.45"]))
    return br

# ---- PCB + parts on it ---------------------------------------------------------------------
def build_pcb():
    P = D.PCB; pcb = bbox(P["box"]); y = P["box"]["Y"]
    for h in P["mount_holes"]:
        x, z = h["XZ"]; pcb = cut(pcb, cyl("Y", x, z, y[0] - 1, y[1] + 1, h["dia"]))
    return pcb

def build_pcb_components():
    P = D.PCB
    comps = [bbox(c) for c in P["components"].values()]
    comps += [cyl("Y", c["XZ"][0], c["XZ"][1], 32.85, 43.35, c["dia"]) for c in P["caps"]]
    return fuse(*comps)

def build_standoffs():
    return fuse(*[cyl("Y", s["XZ"][0], s["XZ"][1], s["Y"][0], s["Y"][1], s["dia"]) for s in D.STANDOFFS])

# ---- engine ---------------------------------------------------------------------------------
def build_engine():
    E = D.ENGINE; body = bbox(E["body"])
    ytop = E["body"]["Y"][1]
    for x, z in D.engine_plate_holes_XZ():
        body = cut(body, cyl("Y", x, z, ytop - 12.0, ytop + 1, E["plate_dia"]))
    return body

def build_lens():
    E = D.ENGINE; ly, lz = E["lens"]["axis_YZ"]
    return fuse(cyl("X", ly, lz, E["lens"]["X"][0], E["lens"]["X"][1], E["lens"]["dia"]), bbox(E["lens_hood"]))

def build_heatsinks():
    return fuse(*[bbox(h) for h in D.ENGINE["heatsinks"].values()])

# ---- assemble + export ---------------------------------------------------------------------
PARTS = [  # name, builder, RGB colour
    ("MCH051_base", build_base, (0.20, 0.20, 0.22)),
    ("MCH053_formatter_base", build_formatter_base, (0.28, 0.28, 0.30)),
    ("MCH052_fan_bracket", build_fan_bracket, (0.35, 0.35, 0.37)),
    ("fan_25mm", build_fan, (0.15, 0.15, 0.15)),
    ("DLP038_pcb", build_pcb, (0.55, 0.10, 0.12)),
    ("pcb_components", build_pcb_components, (0.30, 0.30, 0.32)),
    ("standoffs", build_standoffs, (0.75, 0.75, 0.75)),
    ("FLA12F_engine", build_engine, (0.10, 0.10, 0.10)),
    ("lens", build_lens, (0.05, 0.05, 0.05)),
    ("led_heatsinks", build_heatsinks, (0.80, 0.80, 0.82)),
]

def main():
    assy = cq.Assembly(name="DLPDLCR230NPEVM_simplified")
    shapes = []
    for name, builder, rgb in PARTS:
        s = builder(); shapes.append(s)
        assy.add(cq.Workplane().add(s), name=name, color=cq.Color(*rgb))
        print(f"  {name:24s} volume {s.Volume():9.0f} mm^3")
    comp = cq.Compound.makeCompound(shapes)
    bb = comp.BoundingBox()
    print(f"model envelope X {bb.xmin:.2f}..{bb.xmax:.2f} ({bb.xlen:.2f})  Y {bb.ymin:.2f}..{bb.ymax:.2f} ({bb.ylen:.2f})  Z {bb.zmin:.2f}..{bb.zmax:.2f} ({bb.zlen:.2f})")
    assy.save(str(HERE / f"{OUT}.step"))
    wp = cq.Workplane().add(comp)
    cq.exporters.export(wp, str(HERE / f"{OUT}.stl"), tolerance=0.05, angularTolerance=0.15)
    cq.exporters.export(wp, str(HERE / f"{OUT}_iso.svg"),
                        opt={"width": 1100, "height": 750, "marginLeft": 10, "marginTop": 10,
                             "showAxes": True, "projectionDir": (-1.6, 1.2, -2.4),
                             "strokeWidth": 0.4, "showHidden": False})
    print("wrote", f"{OUT}.step", f"{OUT}.stl", f"{OUT}_iso.svg")

if __name__ == "__main__":
    main()
