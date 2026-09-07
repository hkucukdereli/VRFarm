#!/usr/bin/env python3
"""Verify the panel dither actually removes the luminance-correction banding.

WHY THIS EXISTS
---------------
The DPI link to the DLPC3436 is RGB666 (dlp/sample_config/config_kms.txt: "RGB666 on GPIO0-21 is
this overlay's DEFAULT format"), so the hardware discards the low 2 bits of every 8-bit value the
renderer writes. The full-field luminance correction C(az) spans about 5:1, which is ~150 distinct
8-bit codes across the screen -- but only ~38 survive to the panel. The result is wide vertical
columns instead of a smooth gradient. devices/display.Display._quantize dithers before that
truncation so the local mean lands on the intended luminance.

This script re-derives what the panel actually receives (`>> 2`) from a real warp map and checks
the dither is doing its job. It imports the shipping code rather than reimplementing it, so it
cannot drift from the renderer. No display, no pygame -- runs anywhere numpy does.

    python display_diagnostics/dither_check.py [warp_map.npz] [--bg 0.75]

Exit 0 = dither is working. Exit 1 = it regressed.
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from devices.display import Display, _build_dither_tile   # noqa: E402


def panel_view(code8: np.ndarray, bits: int) -> np.ndarray:
    """What the panel shows, back in 8-bit units: truncate to `bits`, then rescale."""
    shift = 8 - bits
    return (code8 >> shift).astype(np.float64) * (1 << shift)


def band_stats(panel: np.ndarray, valid: np.ndarray, row: int):
    """Run lengths of constant panel level along one row -- the visible 'columns'."""
    v = panel[row][valid[row]]
    if v.size == 0:
        return 0, 0.0, 0
    edges = np.flatnonzero(np.r_[True, v[1:] != v[:-1], True])
    runs = np.diff(edges)
    return len(runs), float(np.median(runs)), int(runs.max())


def block_mean_error(panel, ideal, valid, k=8):
    """Mean |error| of the k x k local average vs the intended value. This is the number that
    matters: the animal integrates over an area, so what counts is whether the neighbourhood
    average is right, not whether any single pixel is."""
    h, w = (ideal.shape[0] // k) * k, (ideal.shape[1] // k) * k

    def blocks(a):
        return a[:h, :w].reshape(h // k, k, w // k, k).mean(axis=(1, 3))

    mask = blocks(valid.astype(float)) > 0.99
    if not mask.any():
        return float("nan")
    return float(np.abs(blocks(panel) - blocks(ideal))[mask].mean())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("warp", nargs="?", default="display_calibration/warp_map.npz")
    ap.add_argument("--bg", type=float, default=0.75, help="background_gray to test at")
    ap.add_argument("--bits", type=int, default=6, help="panel bits per channel (DPI RGB666 = 6)")
    ap.add_argument("--mode", choices=["empirical", "theoretical", "none"], default="theoretical",
                    help="force lum_correction_mode instead of using the one baked into the warp; "
                         "the banding this checks for only exists when a correction is applied, so "
                         "a warp saved with mode 'none' would otherwise vacuously pass")
    a = ap.parse_args()

    warp = Path(a.warp)
    if not warp.exists():
        print(f"no warp map at {warp}", file=sys.stderr)
        return 2

    # Drive the real device object: load_warp/_build_corr_map/_quantize need no display.
    dev = Display()
    dev.init({"resolution": [1920, 1080], "panel_bits": a.bits}, {"background_gray": a.bg})
    if not dev.load_warp(str(warp)):
        print(f"could not load {warp}", file=sys.stderr)
        return 2
    # _build_corr_map accepts a plain dict as well as an NpzFile, so the mode can be overridden
    # without rewriting the file.
    baked = str(dev._warp["lum_correction_mode"]) if "lum_correction_mode" in dev._warp.files \
        else "<absent>"
    dev._warp = {k: dev._warp[k] for k in dev._warp.files} | {"lum_correction_mode": a.mode}
    dev._corr_map = dev._build_corr_map()
    if dev._corr_map is None:
        print("warp has no luminance data -- nothing to check", file=sys.stderr)
        return 2
    dev._dither = _build_dither_tile(dev._corr_map.shape, dev.panel_bits)

    valid = np.asarray(dev._warp["valid_map"], dtype=bool)
    drive = a.bg * dev._corr_map
    ideal = drive * 255.0
    row = valid.shape[0] // 2

    dithered = panel_view(dev._quantize(drive), a.bits)
    dev._dither = None                                   # same code path, dither disabled
    plain = panel_view(dev._quantize(drive), a.bits)

    corr = dev._corr_map[valid]
    print(f"warp            : {warp}  (baked mode: {baked}, testing as: {a.mode})")
    print(f"panel           : {a.bits} bits/channel  ->  {1 << a.bits} levels")
    print(f"C(az) range     : {corr.min():.4f} .. {corr.max():.4f}  "
          f"({corr.max() / max(corr.min(), 1e-9):.1f}:1)")
    print(f"background_gray : {a.bg}")

    n_p, med_p, max_p = band_stats(plain, valid, row)
    n_d, med_d, max_d = band_stats(dithered, valid, row)
    err_p = block_mean_error(plain, ideal, valid)
    err_d = block_mean_error(dithered, ideal, valid)

    print()
    print(f"{'':16}{'bands':>8}{'median px':>12}{'widest px':>12}{'8x8 mean err':>15}")
    print(f"{'truncate only':16}{n_p:>8}{med_p:>12.0f}{max_p:>12}{err_p:>15.3f}")
    print(f"{'dithered':16}{n_d:>8}{med_d:>12.0f}{max_d:>12}{err_d:>15.3f}")

    ok = True
    if a.bits < 8:
        # The point of the dither: local mean tracks the target instead of sitting a half-step dark.
        if not err_d < err_p / 4:
            print("\nFAIL: dither did not improve local luminance fidelity", file=sys.stderr)
            ok = False
        # ...and the wide flat plateaus break up into noise.
        if not med_d < med_p:
            print("\nFAIL: dither did not narrow the bands", file=sys.stderr)
            ok = False
    else:
        # Nothing is truncated, so dithering must change NOTHING -- never add noise to a panel
        # that can already show every code we write.
        if not np.array_equal(plain, dithered):
            print("\nFAIL: dither altered output on an 8-bit panel", file=sys.stderr)
            ok = False
    # The tile itself must not exist at 8 bits, whatever the warp says.
    if _build_dither_tile((16, 16), 8) is not None:
        print("\nFAIL: 8-bit panel should get no dither tile", file=sys.stderr)
        ok = False

    print("\nOK" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
