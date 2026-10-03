"""
display_calibration/intensity_cal.py — the rig's intensity calibration files.

    display_calibration/<rig>/intensity/
        intensity_cal_<YYYYmmdd_HHMM>.yaml               measured (Setup → INTENSITY → Measure)
        intensity_cal_<geostamp>_theoretical.yaml        mock: geometry gain, straight response
        intensity_cal_none.yaml                          mock: flat, drive = brightness

One file holds everything (format: _template/intensity/intensity_cal_template.yaml): column 1 =
light at full drive along azimuth; one or more level columns = light vs drive at an azimuth the
user picked; one altitude for the whole file. shared/intensity_model.py turns it into the model
the warp map carries to the renderer. The rig uses the file named by its rig YAML's
devices.display.intensity_calibration (Setup → INTENSITY → Apply).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.intensity_model import (CalibrationError, fit, fit_report, mock_cal,   # noqa: E402
                                    IntensityModel)

GLOB = "intensity_cal_*.yaml"
NONE_NAME = "intensity_cal_none.yaml"


def _safe(name: str) -> str:
    n = Path(str(name)).name
    if not (n.startswith("intensity_cal_") and n.endswith(".yaml")):
        raise ValueError(f"not an intensity calibration file: {name}")
    return n


def load(name: str, cal_dir) -> dict:
    """The calibration dict (with `name` set). Raises FileNotFoundError / ValueError / CalibrationError."""
    path = Path(cal_dir) / _safe(name)
    if not path.exists():
        raise FileNotFoundError(f"no {path.name} in {cal_dir}")
    cal = yaml.safe_load(path.read_text()) or {}
    cal["name"] = path.name
    fit(cal)                                    # validate now, with the file's own reason
    return cal


def model(name: str, cal_dir) -> IntensityModel:
    return IntensityModel.from_cal(load(name, cal_dir))


def _write(path: Path, cal: dict, header: str) -> Path:
    body = {k: v for k, v in cal.items() if k != "name"}
    path.write_text(header + yaml.dump(body, default_flow_style=False, sort_keys=False))
    return path


def save_measured(cal_dir, alt_deg, azimuth_sweep, level_sweeps, size_deg=None, method=None,
                  unit="lux", note=None) -> tuple[Path, dict]:
    """Validate (fit) and write intensity_cal_<now>.yaml. Returns (path, fit report)."""
    cal_dir = Path(cal_dir); cal_dir.mkdir(parents=True, exist_ok=True)
    cal = {
        "kind": "intensity", "version": 1, "source": "measured",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "method": method or "manual", "unit": unit or "lux",
        "alt_deg": float(alt_deg),
        "patch": {"shape": "square", "size_deg": float(size_deg or 15), "bg_gray": 0,
                  "apply_lum": False},
        "azimuth_sweep": [{"az_deg": float(r["az_deg"]), "reading": float(r["reading"])}
                          for r in azimuth_sweep if r.get("reading") not in (None, "")],
        "level_sweeps": [{"az_deg": float(s["az_deg"]),
                          "measurements": sorted(
                              ({"level": float(m["level"]), "reading": float(m["reading"])}
                               for m in s.get("measurements", []) if m.get("reading") not in (None, "")),
                              key=lambda m: -m["level"])}
                         for s in level_sweeps],
    }
    if note:
        cal["note"] = note
    params = fit(cal)                           # raises CalibrationError with the reason
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path, k = cal_dir / f"intensity_cal_{stamp}.yaml", 2
    while path.exists():
        path, k = cal_dir / f"intensity_cal_{stamp}_{k}.yaml", k + 1
    _write(path, cal, "# Measured intensity calibration (Setup → Display → INTENSITY → Measure).\n"
                      "# Column 1 = light at drive 1.0 along azimuth; level columns = light vs drive.\n")
    return path, fit_report(cal, params)


def write_theoretical(cal_dir, az, gain, geometry_name, alt_deg=0.0) -> Path:
    """intensity_cal_<geostamp>_theoretical.yaml: the geometry model's gain along azimuth, a
    straight response, no floor — the old theoretical correction, in the new format."""
    cal_dir = Path(cal_dir); cal_dir.mkdir(parents=True, exist_ok=True)
    m = re.search(r"(\d{8}_\d{4})", Path(geometry_name).stem)
    stamp = m.group(1) if m else datetime.now().strftime("%Y%m%d_%H%M")
    cal = mock_cal("theoretical", list(az), list(gain), alt_deg=alt_deg,
                   geometry_file=Path(geometry_name).name,
                   timestamp=datetime.now().isoformat(timespec="seconds"))
    return _write(cal_dir / f"intensity_cal_{stamp}_theoretical.yaml", cal,
                  "# MOCK (theoretical): column 1 = cos(incidence) gain from the geometry file below;\n"
                  "# drive 0 = 0, drive 1 = 1 (no floor, straight response). Not measured.\n")


def write_none(cal_dir) -> Path:
    """intensity_cal_none.yaml: flat light, straight response — brightness is the drive."""
    cal_dir = Path(cal_dir); cal_dir.mkdir(parents=True, exist_ok=True)
    path = cal_dir / NONE_NAME
    if not path.exists():
        _write(path, mock_cal("none"), "# MOCK (none): no correction — brightness is the drive.\n")
    return path


def list_cals(cal_dir) -> list[dict]:
    """Every intensity_cal_*.yaml, newest first: name, source, alt, columns, uniform range, or the
    reason it cannot be used."""
    out = []
    for f in Path(cal_dir).glob(GLOB):
        e = {"name": f.name, "mtime": f.stat().st_mtime}
        try:
            cal = yaml.safe_load(f.read_text()) or {}
            params = fit(cal)
            e.update(source=cal.get("source", "measured"), alt_deg=cal.get("alt_deg"),
                     unit=params["unit"], lo=params["lo"], hi=params["hi"],
                     columns=[float(a) for a in params["sweep_az"]], az_cal=params["az_cal"])
        except (CalibrationError, Exception) as ex:   # noqa: B014 — report, never raise
            e["error"] = str(ex)
        out.append(e)
    order = {"measured": 0, "theoretical": 1, "none": 2}
    return sorted(out, key=lambda d: (order.get(d.get("source"), 0), -d["mtime"]))
