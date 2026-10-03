"""
controller/calib_paths.py — where each rig's display calibration lives on the controller.

    display_calibration/                 scripts shared by every rig
    display_calibration/_template/        one template per file kind (geometry/, intensity/)
    display_calibration/<rig>/
        geometry/rig_geometry_<YYYYmmdd_HHMM>.yaml          one file per calibration, never edited
        intensity/intensity_cal_<stamp>.yaml                 measured: light along azimuth +
                                                             light vs drive at chosen azimuths
        intensity/intensity_cal_<geostamp>_theoretical.yaml  mock from the geometry (default)
        intensity/intensity_cal_none.yaml                    mock: no correction
        warp_map.npz                                         generated; carries the light model

Which geometry a rig uses is `devices.display.geometry_file` in its rig YAML (a file name in
geometry/); which intensity calibration, `devices.display.intensity_calibration`. Dated files
are records: an edited geometry is saved as a NEW dated file.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from controller import settings

TOOLS_DIR = settings.ROOT / "display_calibration"
DEFAULT_GEOMETRY = TOOLS_DIR / "_template" / "geometry" / "rig_geometry_template.yaml"
GEO_GLOB = "rig_geometry_*.yaml"


def rig_dir(rig: str) -> Path:
    d = TOOLS_DIR / rig
    (d / "geometry").mkdir(parents=True, exist_ok=True)
    (d / "intensity").mkdir(parents=True, exist_ok=True)
    return d


def geometry_dir(rig: str) -> Path:
    return rig_dir(rig) / "geometry"


def intensity_dir(rig: str) -> Path:
    return rig_dir(rig) / "intensity"


def warp_path(rig: str) -> Path:
    return rig_dir(rig) / "warp_map.npz"


def stamp_now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M")


def new_geometry_name(rig: str) -> str:
    """rig_geometry_<now>.yaml, with a _2, _3 ... suffix if that minute is already taken."""
    gdir = geometry_dir(rig)
    base = f"rig_geometry_{stamp_now()}"
    name, k = f"{base}.yaml", 2
    while (gdir / name).exists():
        name, k = f"{base}_{k}.yaml", k + 1
    return name


def geometry_files(rig: str) -> list[str]:
    """Dated geometry files, oldest first. Seeds the default geometry into an empty folder so a
    new rig always has something to load and calibrate from."""
    gdir = geometry_dir(rig)
    files = sorted(f.name for f in gdir.glob(GEO_GLOB))
    if not files and DEFAULT_GEOMETRY.exists():
        name = new_geometry_name(rig)
        text = "".join(l for l in DEFAULT_GEOMETRY.read_text().splitlines(True) if not l.startswith("#"))
        (gdir / name).write_text(f"# {rig}: seeded from _template/geometry/rig_geometry_template.yaml on "
                                 f"{datetime.now():%Y-%m-%d}; not calibrated yet.\n" + text)
        files = [name]
    return files


def current_geometry(rig: str, rig_config: dict | None) -> str | None:
    """The geometry file this rig uses: its rig YAML's display.geometry_file when that file
    exists in geometry/, else the newest dated file."""
    files = geometry_files(rig)
    want = (((rig_config or {}).get("devices") or {}).get("display") or {}).get("geometry_file")
    if want and Path(want).name in files:
        return Path(want).name
    return files[-1] if files else None


def resolve_geometry(rig: str, rig_config: dict | None, name: str | None) -> Path | None:
    """Path of geometry `name` (basename only) in this rig's folder, or of the current one."""
    name = Path(name).name if name else current_geometry(rig, rig_config)
    return (geometry_dir(rig) / name) if name else None
