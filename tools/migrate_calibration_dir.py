#!/usr/bin/env python3
"""
tools/migrate_calibration_dir.py — give a rig its own calibration folder.

Before multi-rig, display_calibration/ held ONE rig's geometry, warp map and luminance
files. With several rigs each needs its own set, so the controller looks in
display_calibration/<rig>/ first and falls back to the shared folder until this has run.

    python tools/migrate_calibration_dir.py cheddar

Copies (never moves) rig_geometry*.yaml, warp_map.npz, luminance_cal_*.yaml and
luminance_measurements_*.yaml into display_calibration/<rig>/ and recreates the
luminance_cal_latest.yaml pointer there. Safe to re-run; existing files in the target are
left alone unless --overwrite is given.
"""
from __future__ import annotations
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "display_calibration"
PATTERNS = ["rig_geometry*.yaml", "warp_map.npz", "luminance_cal_*.yaml", "luminance_measurements_*.yaml"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rig")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args()
    if not (ROOT / "rigs" / f"{a.rig}.yaml").exists():
        print(f"no rigs/{a.rig}.yaml — is that the rig's filename?")
        return 1
    dst = SRC / a.rig
    dst.mkdir(exist_ok=True)
    copied, skipped = [], []
    latest_target = None
    for pat in PATTERNS:
        for f in sorted(SRC.glob(pat)):
            if f.name == "luminance_cal_latest.yaml":
                if f.is_symlink() or f.exists():
                    latest_target = f.resolve().name
                continue
            out = dst / f.name
            if out.exists() and not a.overwrite:
                skipped.append(f.name)
                continue
            shutil.copy2(f, out)
            copied.append(f.name)
    if latest_target and (dst / latest_target).exists():
        latest = dst / "luminance_cal_latest.yaml"
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(latest_target)
        copied.append(f"luminance_cal_latest.yaml -> {latest_target}")
    print(f"display_calibration/{a.rig}/")
    for c in copied:
        print("  copied ", c)
    for s in skipped:
        print("  kept   ", s, "(already there; --overwrite to replace)")
    if not copied and not skipped:
        print("  nothing to copy — the shared folder has no calibration files")
    print("The controller now reads and writes this rig's calibration files here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
