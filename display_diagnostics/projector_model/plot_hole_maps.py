"""Hole maps of the DLPDLCR230NPEVM as 2D drawings (PNG), from dlpdlcr230npevm_dims.py.

    conda activate vrfarm && python plot_hole_maps.py

Four panels: bottom view (what you see when you bolt it down), top view (PCB + flange + engine
plate), and the two long walls seen from outside. Coordinates are TI's assembly frame (mm).
"""
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle
import dlpdlcr230npevm_dims as D

HERE = Path(__file__).resolve().parent

def rect(ax, xr, yr, **kw):
    ax.add_patch(Rectangle((xr[0], yr[0]), xr[1] - xr[0], yr[1] - yr[0], fill=False, **kw))

def hole(ax, x, y, dia, label, color, csk=None):
    ax.add_patch(Circle((x, y), dia / 2, fill=False, color=color, lw=1.2))
    if csk:
        ax.add_patch(Circle((x, y), csk / 2, fill=False, color=color, lw=0.6, ls="--"))
    ax.plot(x, y, "+", color=color, ms=6, mew=0.8)
    ax.annotate(label, (x, y), xytext=(4, 4), textcoords="offset points", fontsize=6.5, color=color)

def finish(ax, title, xl, yl):
    ax.set_aspect("equal"); ax.grid(True, lw=0.3, alpha=0.5); ax.set_title(title, fontsize=10)
    ax.set_xlabel(xl, fontsize=8); ax.set_ylabel(yl, fontsize=8); ax.tick_params(labelsize=7)
    ax.autoscale_view(); ax.margins(0.06)

def bottom_view(ax):
    """Viewer below the assembly looking up (+Y): right = +X, up = +Z."""
    B, F, FB = D.BASE, D.FORMATTER_BASE, D.FAN_BRACKET
    rect(ax, B["sheet"]["X"], B["sheet"]["Z"], color="k", lw=1.2, label="MCH051 base sheet")
    rect(ax, B["cutout"]["X"], B["cutout"]["Z"], color="k", lw=0.6, ls=":")
    for k in ("back", "side_negZ", "front_short", "side_posZ_short"):
        w = F["walls"][k]; rect(ax, w["X"], w["Z"], color="tab:gray", lw=1.0)
    rect(ax, FB["tab"]["X"], FB["tab"]["Z"], color="tab:blue", lw=0.8)
    p = B["tabs"]["pad"] / 2
    for h in B["csk_holes"]:
        x, z = h["XZ"]; rect(ax, (x - p, x + p), (z - p, z + p), color="tab:red", lw=0.6)
        hole(ax, x, z, D.CSK["through"], f"CSK M2.5\n({x}, {z})", "tab:red", csk=D.CSK["csk_dia"])
    for x, z in B["m16_sheet_holes"]:
        hole(ax, x, z, D.TAP["M1.6x0.35"], "M1.6 thd", "tab:purple")
    ax.text(B["sheet"]["X"][0], B["sheet"]["Z"][1] + 4,
            "standing plane Y = -4.82: base tabs (red, countersink opens downward) + bracket wall edges (grey)",
            fontsize=7)
    finish(ax, "BOTTOM view (from below): base, bracket walls, fan-bracket tab", "X (mm)  lens is at -X", "Z (mm)")

def top_view(ax):
    """Viewer above looking down (-Y): right = +X, up = -Z (plotted as Z axis inverted)."""
    P, F, E = D.PCB, D.FORMATTER_BASE, D.ENGINE
    rect(ax, P["box"]["X"], P["box"]["Z"], color="tab:red", lw=1.2)
    for c in P["components"].values(): rect(ax, c["X"], c["Z"], color="tab:red", lw=0.5, ls=":")
    for h in P["mount_holes"]:
        x, z = h["XZ"]; hole(ax, x, z, h["dia"], f"PCB {h['type']}\n({x}, {z})", "tab:red")
    for leg in ("back_leg", "side_leg"):
        l = F["flange"][leg]; rect(ax, l["X"], l["Z"], color="tab:gray", lw=1.0, ls="--")
    for h in F["flange_holes"]:
        x, z = h["XZ"]; hole(ax, x, z, D.TAP[h["thread"]], f"flange {h['thread']} thd", "tab:gray")
    rect(ax, E["body"]["X"], E["body"]["Z"], color="k", lw=1.0)
    rect(ax, E["lens_hood"]["X"], E["lens_hood"]["Z"], color="k", lw=0.6)
    for x, z in D.engine_plate_holes_XZ():
        hole(ax, x, z, E["plate_dia"], f"engine d1.66 (M2)\n({x}, {z})*", "tab:green")
    ax.invert_yaxis()
    finish(ax, "TOP view: PCB (red) over the bracket flange (grey) and the engine (black)  *orientation inferred",
           "X (mm)  lens is at -X", "Z (mm), +Z down")

def back_wall_view(ax):
    """Back wall (X = 72.05) seen from behind (+X): right = -Z, up = +Y."""
    B, F = D.BASE, D.FORMATTER_BASE
    w = F["walls"]["back"]; rect(ax, (-w["Z"][1], -w["Z"][0]), F["walls"]["Y"], color="tab:gray", lw=1.0)
    rect(ax, (-F["flange"]["back_leg"]["Z"][1], -F["flange"]["back_leg"]["Z"][0]), F["flange"]["Y"], color="tab:gray", lw=0.6)
    for h in F["wall_m2_holes"]:
        if h["wall"] == "back": y, z = h["YZ"]; hole(ax, -z, y, D.TAP["M2x0.4"], f"M2 thd (Y {y}, Z {z})", "tab:gray")
    for h in F["wall_csk_holes"]:
        if h["wall"] == "back":
            y, z = h["YZ"]; hole(ax, -z, y, D.CSK["through"], f"CSK M2.5 (Y {y}, Z {z})", "tab:red", csk=D.CSK["csk_dia"])
    finish(ax, "BACK wall (MCH053, X = 72.05) seen from behind", "-Z (mm)", "Y (mm)")

def side_wall_view(ax):
    """-Z side wall (Z = -65.98) seen from the -Z side: right = +X, up = +Y."""
    F = D.FORMATTER_BASE
    w = F["walls"]["side_negZ"]; rect(ax, w["X"], F["walls"]["Y"], color="tab:gray", lw=1.0)
    rect(ax, F["flange"]["side_leg"]["X"], F["flange"]["Y"], color="tab:gray", lw=0.6)
    for h in F["wall_m2_holes"]:
        if h["wall"] == "side_negZ": x, y = h["XY"]; hole(ax, x, y, D.TAP["M2x0.4"], f"M2 thd ({x}, {y})", "tab:gray")
    for h in F["wall_csk_holes"]:
        if h["wall"] == "side_negZ":
            x, y = h["XY"]; hole(ax, x, y, D.CSK["through"], f"CSK M2.5 ({x}, {y})", "tab:red", csk=D.CSK["csk_dia"])
    finish(ax, "-Z SIDE wall (MCH053, Z = -65.98, header side) seen from outside", "X (mm)  lens is at -X", "Y (mm)")

def main():
    fig, axs = plt.subplots(2, 2, figsize=(15, 13))
    bottom_view(axs[0, 0]); top_view(axs[0, 1]); back_wall_view(axs[1, 0]); side_wall_view(axs[1, 1])
    fig.suptitle("TI DLPDLCR230NPEVM — hole map, TI assembly frame (mm). Threads drawn at tap-drill size; "
                 "dashed = 5.50 mm countersink", fontsize=11)
    fig.tight_layout()
    out = HERE / "dlpdlcr230npevm_hole_maps.png"
    fig.savefig(out, dpi=130); print("wrote", out)

if __name__ == "__main__":
    main()
