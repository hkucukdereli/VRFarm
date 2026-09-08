#!/usr/bin/env python3
"""Render what the projector ACTUALLY shows, with and without the panel dither.

The renderer writes 8-bit codes; the RGB666 DPI link discards the low 2 bits before the DLPC ever
sees them. These images reproduce that truncation, so what you are looking at is the panel's view
of the luminance-corrected field -- not the framebuffer's.

Outputs (into --out, default display_diagnostics/preview/):
  dither_before_after.png  native-resolution frames, truncate-only above, dithered below
  dither_detail.png        1:1 crop of the worst banding, magnified with NEAREST (no resampling)
  dither_profile.png       luminance profile through the mid row: ideal vs panel vs local mean

    python display_diagnostics/dither_preview.py [warp_map.npz] [--bg 0.75] [--mode theoretical]

NOTE ON VIEWING: view dither_before_after.png at 100%. Any viewer that downscales will average the
dither away (making "after" look perfect) while leaving the banding in "before" -- which flatters
the fix. dither_detail.png is the honest close-up.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from devices.display import Display, _build_dither_tile   # noqa: E402

LABEL_H = 44


def _label_bar(width, text, fg=235, bg=18):
    """A thin caption strip drawn with PIL's default bitmap font (no font files, no fallbacks)."""
    from PIL import ImageDraw
    img = Image.new("RGB", (width, LABEL_H), (bg, bg, bg))
    d = ImageDraw.Draw(img)
    d.text((16, LABEL_H // 2 - 6), text, fill=(fg, fg, fg))
    return img


def _to_rgb(panel_code8):
    """Green+blue only, R=0 -- the same channels the stimulus uses (red is the sync square's)."""
    h, w = panel_code8.shape
    out = np.zeros((h, w, 3), dtype=np.uint8)
    out[..., 1] = panel_code8
    out[..., 2] = panel_code8
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("warp", nargs="?", default="display_calibration/warp_map.npz")
    ap.add_argument("--bg", type=float, default=0.75)
    ap.add_argument("--bits", type=int, default=6)
    ap.add_argument("--mode", choices=["empirical", "theoretical", "none"], default="theoretical")
    ap.add_argument("--out", default="display_diagnostics/preview")
    ap.add_argument("--zoom", type=int, default=3, help="magnification for the detail crop")
    a = ap.parse_args()

    warp = Path(a.warp)
    if not warp.exists():
        print(f"no warp map at {warp}", file=sys.stderr)
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    dev = Display()
    dev.init({"resolution": [1920, 1080], "panel_bits": a.bits}, {"background_gray": a.bg})
    dev.load_warp(str(warp))
    dev._warp = {k: dev._warp[k] for k in dev._warp.files} | {"lum_correction_mode": a.mode}
    dev._corr_map = dev._build_corr_map()
    dev._dither = _build_dither_tile(dev._corr_map.shape, dev.panel_bits)

    valid = np.asarray(dev._warp["valid_map"], dtype=bool)
    drive = a.bg * dev._corr_map
    shift = 8 - a.bits

    dithered = (dev._quantize(drive) >> shift) << shift      # what the panel shows, dithered
    dev._dither = None
    plain = (dev._quantize(drive) >> shift) << shift          # ...and without
    for img in (dithered, plain):
        img[~valid] = 0

    # ── full frames, native resolution, no resampling ────────────────────────────────
    w = plain.shape[1]
    parts = [_label_bar(w, f"BEFORE  truncate only  -  {a.bits}-bit panel, mode={a.mode}, bg={a.bg}"),
             Image.fromarray(_to_rgb(plain)),
             _label_bar(w, "AFTER  8x8 ordered dither before truncation"),
             Image.fromarray(_to_rgb(dithered))]
    total_h = sum(p.height for p in parts)
    sheet = Image.new("RGB", (w, total_h))
    y = 0
    for p in parts:
        sheet.paste(p, (0, y))
        y += p.height
    sheet.save(out / "dither_before_after.png")

    # ── detail crop: centre on the widest band in the mid row ────────────────────────
    row = plain.shape[0] // 2
    v = plain[row]
    m = valid[row]
    idx = np.flatnonzero(m)
    vv = v[m]
    edges = np.flatnonzero(np.r_[True, vv[1:] != vv[:-1], True])
    runs = np.diff(edges)
    widest = int(np.argmax(runs))
    cx = int(idx[edges[widest] + runs[widest] // 2])
    cw, ch = 480, 140
    x0 = max(0, min(plain.shape[1] - cw, cx - cw // 2))
    y0 = max(0, min(plain.shape[0] - ch, row - ch // 2))
    sl = (slice(y0, y0 + ch), slice(x0, x0 + cw))

    def _stretch(a2d):
        """Rescale this crop's own code range to 0..255. A 4-code step is nearly invisible in an
        sRGB PNG, so the plain crop understates what the projector shows (its gamma and the eye's
        dark adaptation both amplify the step). This is a VISUALISATION of the level structure --
        not a simulation of the percept."""
        lo, hi = float(a2d.min()), float(a2d.max())
        if hi <= lo:
            return np.zeros_like(a2d)
        return np.clip((a2d.astype(float) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)

    def _tile(arr, label):
        return [_label_bar(cw * a.zoom, label),
                Image.fromarray(_to_rgb(arr)).resize((cw * a.zoom, ch * a.zoom), Image.NEAREST)]

    crops = (_tile(plain[sl], f"BEFORE  x{a.zoom}  (widest band here: {runs[widest]} px)")
             + _tile(dithered[sl], f"AFTER  x{a.zoom}  same {cw}x{ch} px region")
             + _tile(_stretch(plain[sl]), "BEFORE, contrast-stretched  -  visualisation of the "
                                          "level structure, not the percept")
             + _tile(_stretch(dithered[sl]), "AFTER, contrast-stretched  -  same stretch applied"))
    th = sum(c.height for c in crops)
    det = Image.new("RGB", (cw * a.zoom, th))
    y = 0
    for c in crops:
        det.paste(c, (0, y))
        y += c.height
    det.save(out / "dither_detail.png")

    # ── profile: ideal vs what the panel shows vs the local mean the animal integrates ──
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = idx
    ideal = (drive * 255.0)[row][m]
    k = 8
    kern = np.ones(k) / k
    fig, ax = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
    ax[0].plot(x, ideal, lw=1.2, color="0.25", label="ideal (what the code computes)")
    ax[0].plot(x, v[m], lw=0.9, color="#c1440e", label="panel, truncate only")
    ax[0].set_ylabel("8-bit code"); ax[0].legend(loc="upper center", fontsize=9)
    ax[0].set_title(f"Luminance-corrected background, mid row  —  {a.bits}-bit panel, "
                    f"mode={a.mode}, bg={a.bg}")
    ax[1].plot(x, ideal, lw=1.2, color="0.25", label="ideal")
    # Trim k//2 from each end: the boxcar runs off the valid region there and the dip it produces
    # is an artefact of this plot, not of the dither.
    lm = np.convolve(dithered[row][m].astype(float), kern, "same")
    e = k // 2
    ax[1].plot(x[e:-e], lm[e:-e], lw=0.9, color="#1b7a43",
               label="panel, dithered (8 px local mean)")
    ax[1].set_ylabel("8-bit code"); ax[1].set_xlabel("screen x (px)")
    ax[1].legend(loc="upper center", fontsize=9)
    for b in ax:
        b.grid(alpha=0.25, lw=0.5)
    fig.tight_layout()
    fig.savefig(out / "dither_profile.png", dpi=110)

    print(f"wrote {out}/dither_before_after.png  ({sheet.width}x{sheet.height})")
    print(f"wrote {out}/dither_detail.png        (crop at x={x0}..{x0+cw}, y={y0}..{y0+ch})")
    print(f"wrote {out}/dither_profile.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
