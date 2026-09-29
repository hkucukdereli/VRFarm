"""2D drawing (PNG) of the Pi 4 B + Joy-IT Armor Case: top view and side view with every hole.

    conda activate vrfarm && python plot_hole_maps.py
"""
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, FancyBboxPatch
import rpi4_armor_case_dims as D

HERE = Path(__file__).resolve().parent

def rect(ax, xr, yr, **kw):
    ax.add_patch(Rectangle((xr[0], yr[0]), xr[1] - xr[0], yr[1] - yr[0], fill=False, **kw))

def top_view(ax):
    B = D.BOARD
    ax.add_patch(FancyBboxPatch((0, 0), 85, 56, boxstyle=f"round,pad=0,rounding_size={B['corner_radius']}",
                                fill=False, color="tab:green", lw=1.5))
    for name, c in D.CONNECTORS.items():
        if c["Z"][0] < 0: continue
        rect(ax, c["X"], c["Y"], color="tab:gray", lw=0.7)
        ax.text((c["X"][0] + c["X"][1]) / 2, (c["Y"][0] + c["Y"][1]) / 2, name.replace("_", "\n"), fontsize=5.5,
                ha="center", va="center", color="tab:gray")
    for x, y in D.HOLES["XY"]:
        ax.add_patch(Circle((x, y), D.HOLES["dia"] / 2, fill=False, color="tab:red", lw=1.3))
        ax.add_patch(Circle((x, y), D.CASE_ESTIMATED["post_dia"] / 2, fill=False, color="k", lw=0.6, ls="--"))
        ax.annotate(f"d2.7 ({x}, {y})\nM2.5 case screw", (x, y), xytext=(5, 5), textcoords="offset points",
                    fontsize=6.5, color="tab:red")
    C, E = D.CASE_PUBLISHED, D.CASE_ESTIMATED
    rect(ax, (E["top_block_X0"], E["top_block_X0"] + C["top_block"]["L"]), (0, C["top_block"]["W"]), color="k", lw=1.2)
    rect(ax, (E["bottom_plate_X0"], E["bottom_plate_X0"] + C["bottom_plate"]["L"]), (0, C["bottom_plate"]["W"]),
         color="k", lw=0.8, ls=":")
    g = E["gpio_slot"]; rect(ax, g["X"], g["Y"], color="k", lw=0.6, ls="--")
    s = E["csi_ribbon_slot"]; rect(ax, s["X"], s["Y"], color="k", lw=0.6, ls="--")
    ax.set_aspect("equal"); ax.grid(True, lw=0.3, alpha=0.5); ax.margins(0.05); ax.autoscale_view()
    ax.set_title("TOP view: Pi 4 B (green) with holes (red), Joy-IT top block 69 x 56 (black), bottom plate 87 x 56 (dotted)", fontsize=9)
    ax.set_xlabel("X (mm)  microSD at 0, USB/Ethernet at 85"); ax.set_ylabel("Y (mm)  USB-C edge at 0, GPIO at 56")

def side_view(ax):
    """Seen from the USB-C edge (-Y): right = +X, up = +Z."""
    B, C, E = D.BOARD, D.CASE_PUBLISHED, D.CASE_ESTIMATED
    rect(ax, B["X"], B["Z"], color="tab:green", lw=1.5)
    for name, c in D.CONNECTORS.items():
        rect(ax, c["X"], c["Z"], color="tab:gray", lw=0.6)
    zb = D.top_block_Z(); rect(ax, (E["top_block_X0"], E["top_block_X0"] + C["top_block"]["L"]), zb, color="k", lw=1.2)
    zp = D.bottom_plate_Z()
    rect(ax, (E["bottom_plate_X0"], E["bottom_plate_X0"] + C["bottom_plate"]["L"]), (zp[0], 0.0), color="k", lw=0.8, ls=":")
    rect(ax, (E["bottom_plate_X0"], E["bottom_plate_X0"] + C["bottom_plate"]["L"]), zp, color="k", lw=0.8)
    for x in sorted({x for x, _ in D.HOLES["XY"]}):
        ax.plot([x, x], [zp[0], B["Z"][1] + E["post_thread_depth"]], color="tab:red", lw=1.0)
        ax.annotate(f"M2.5 screw x={x}", (x, zp[0]), xytext=(3, -12), textcoords="offset points", fontsize=6.5, color="tab:red")
    for z, lbl in ((zb[1], f"block top Z={zb[1]:.1f} (est.)"), (zb[0], f"block underside Z={zb[0]:.1f} (est.)"),
                   (B["Z"][1], "PCB top Z=1.4"), (zp[0], f"plate bottom Z={zp[0]:.1f}")):
        ax.annotate(lbl, (88, z), fontsize=6.5, va="center")
    ax.set_aspect("equal"); ax.grid(True, lw=0.3, alpha=0.5); ax.margins(0.05); ax.autoscale_view()
    ax.set_title("SIDE view from the USB-C edge: stack-up (vertical case numbers are estimates, see README)", fontsize=9)
    ax.set_xlabel("X (mm)"); ax.set_ylabel("Z (mm)  PCB bottom = 0")

def main():
    fig, axs = plt.subplots(2, 1, figsize=(12, 12), gridspec_kw={"height_ratios": [56, 34]})
    top_view(axs[0]); side_view(axs[1])
    fig.suptitle("Raspberry Pi 4 B + Joy-IT Armor Case BLOCK (RB-AlucaseP4+07) — hole map, mm", fontsize=11)
    fig.tight_layout(); out = HERE / "rpi4b_joyit_armor_case_hole_maps.png"; fig.savefig(out, dpi=130); print("wrote", out)

if __name__ == "__main__":
    main()
