"""
display_calibration/intensity_cal.py — the rig's intensity calibration files.

    display_calibration/<rig>/intensity/
        intensity_cal_<YYYYmmdd_HHMM>.yaml               measured (Setup → INTENSITY → Measure)
        intensity_cal_<geostamp>_theoretical.yaml        mock: geometry gain, straight response
        intensity_cal_none.yaml                          mock: flat, drive = brightness

One file holds everything (format: _template/intensity/intensity_cal_template.yaml): one level
column per azimuth the user picked — light vs drive 1.0 .. 0.0 there — at one altitude for the
whole file. Two columns are the minimum, three or more the useful case. shared/intensity_model.py
turns the columns into the per-level light table the warp map carries to the renderer. The rig
uses the file named by its rig YAML's devices.display.intensity_calibration (Setup → INTENSITY →
Apply). An `azimuth_sweep` key from the earlier format is ignored.
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


def save_measured(cal_dir, alt_deg, level_sweeps, size_deg=None, method=None, unit="lux",
                  note=None) -> tuple[Path, dict]:
    """Validate (fit) and write intensity_cal_<now>.yaml from the level columns
    [{az_deg, measurements: [{level, reading}]}]. Returns (path, fit report)."""
    cal_dir = Path(cal_dir); cal_dir.mkdir(parents=True, exist_ok=True)
    cal = {
        "kind": "intensity", "version": 1, "source": "measured",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "method": method or "manual", "unit": unit or "lux",
        "alt_deg": float(alt_deg),
        "patch": {"shape": "square", "size_deg": float(size_deg or 15), "bg_gray": 0,
                  "apply_lum": False},
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
                      "# One column per azimuth: light vs drive 1.0 .. 0.0 (0.0 = black floor), one altitude.\n")
    return path, fit_report(cal, params)


def write_theoretical(cal_dir, az, gain, geometry_name, alt_deg=0.0) -> Path:
    """intensity_cal_<geostamp>_theoretical.yaml: the geometry model's gain along azimuth as
    two-level columns (drive 0 -> 0, drive 1 -> gain), no floor — the old theoretical
    correction, in the new format."""
    cal_dir = Path(cal_dir); cal_dir.mkdir(parents=True, exist_ok=True)
    m = re.search(r"(\d{8}_\d{4})", Path(geometry_name).stem)
    stamp = m.group(1) if m else datetime.now().strftime("%Y%m%d_%H%M")
    cal = mock_cal("theoretical", list(az), list(gain), alt_deg=alt_deg,
                   geometry_file=Path(geometry_name).name,
                   timestamp=datetime.now().isoformat(timespec="seconds"))
    return _write(cal_dir / f"intensity_cal_{stamp}_theoretical.yaml", cal,
                  "# MOCK (theoretical): one two-level column per azimuth, drive 1 = cos(incidence) gain\n"
                  "# from the geometry file below, drive 0 = 0 (no floor, straight response). Not measured.\n")


def write_none(cal_dir) -> Path:
    """intensity_cal_none.yaml: flat light, straight response — brightness is the drive.
    Rewritten when missing or no longer readable (an earlier format)."""
    cal_dir = Path(cal_dir); cal_dir.mkdir(parents=True, exist_ok=True)
    path = cal_dir / NONE_NAME
    if not path.exists() or not _fits(path):
        _write(path, mock_cal("none"), "# MOCK (none): no correction — brightness is the drive.\n")
    return path


def _fits(path: Path) -> bool:
    try:
        fit(yaml.safe_load(path.read_text()) or {})
        return True
    except Exception:
        return False


def upgrade_mocks(cal_dir) -> list[str]:
    """Rewrite mock files (source theoretical/none) left in the earlier format — gain along azimuth
    in `azimuth_sweep` plus one level column — as two-level columns, keeping their azimuth gain,
    altitude, geometry_file and timestamp. Measured files are never touched (their azimuth column
    cannot be turned into level columns; one with fewer than two columns stays invalid, and says
    why). Returns the names rewritten."""
    done = []
    for f in Path(cal_dir).glob(GLOB):
        try:
            cal = yaml.safe_load(f.read_text()) or {}
        except Exception:
            continue
        if cal.get("source") not in ("theoretical", "none") or not cal.get("azimuth_sweep") or _fits(f):
            continue
        sweep = sorted((float(r["az_deg"]), float(r["reading"])) for r in cal["azimuth_sweep"])
        extra = {k: cal[k] for k in ("geometry_file", "timestamp") if k in cal}
        new = mock_cal(cal["source"], [a for a, _ in sweep], [g for _, g in sweep],
                       alt_deg=float(cal.get("alt_deg") or 0.0), **extra)
        if cal["source"] == "none":
            new = mock_cal("none")
        _write(f, new, f"# MOCK ({cal['source']}): rewritten from the earlier single-column format; "
                       "two levels per azimuth column (drive 0 = 0, drive 1 = gain). Not measured.\n")
        done.append(f.name)
    return done


def list_cals(cal_dir) -> list[dict]:
    """Every intensity_cal_*.yaml, newest first: name, source, alt, column azimuths, uniform range,
    or the reason it cannot be used."""
    out = []
    for f in Path(cal_dir).glob(GLOB):
        e = {"name": f.name, "mtime": f.stat().st_mtime}
        try:
            cal = yaml.safe_load(f.read_text()) or {}
            params = fit(cal)
            e.update(source=cal.get("source", "measured"), alt_deg=cal.get("alt_deg"),
                     unit=params["unit"], lo=params["lo"], hi=params["hi"],
                     columns=[float(a) for a in params["cols_az"]], az_cal=params["az_cal"],
                     levels=int(len(params["levels"])))
        except (CalibrationError, Exception) as ex:   # noqa: B014 — report, never raise
            e["error"] = str(ex)
        out.append(e)
    order = {"measured": 0, "theoretical": 1, "none": 2}
    return sorted(out, key=lambda d: (order.get(d.get("source"), 0), -d["mtime"]))
