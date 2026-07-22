"""
calib_geo.py — LANDMARK-registration geometry calibration.

Instead of eyeballing abstract stretch factors, you register the projected grid to
physical reality and the tool back-solves the geometry:

  1. frame_x / frame_y_top / frame_y_bottom : slide the thin cyan boundary lines onto
     where the projector light actually meets the physical screen. frame_x is a single
     symmetric left/right inset; the top and bottom insets are independent (the screen is
     symmetric in azimuth but not in altitude). The enclosed rectangle = USABLE PIXELS.
  2. offset_x / offset_y : move the GREEN 0° cross (vertical = az 0 meridian,
     horizontal = alt 0 "azimuth line" / eye level) onto physical straight-ahead. That
     intersection is the coordinate origin.
  3. azimuth_height (cm) : measure IRL from the horizontal green line to the screen
     bottom and type it in. With screen height_cm and parabola_A it DERIVES the visual
     field: altitude_min = atan(-h/A), altitude_max = atan((height-h)/A).
  4. az90_x : drag the GREEN ±90° azimuth lines onto the physical 90° marks. The tool
     rescales horizontal_stretch so the model projects ±90° exactly there, and rescales
     vertical_stretch so the altitude range fills the usable area — no more eyeballing.
  5. parabola_B : tune only the mid-field CURVATURE against the cyan 30° grid; the
     ±90° and altitude endpoints stay anchored.

Grid is drawn every 30°. Green = 0° vertical, 0° horizontal, ±90° verticals. Cyan =
every other line and the usable-area frame. All colors are B/G only (R=0) — the
projector's red LED is deactivated.

THE DELIVERABLE IS rig_geometry.yaml. Save overwrites it (next to this script) with the
solved geometry (incl. horizontal/vertical_stretch), the derived altitudes, the
azimuth_height, and a `calibration:` block (flip/offset/frame/az90 landmarks), dropping
the now-redundant azimuth_max_deg. compute_warp_map.py builds warp_map.npz from it.

Run on mozzarella (projector X must be up):
  DISPLAY=:0 ~/miniforge3/envs/rig/bin/python calib_geo.py
Then open http://192.168.10.102:5091 on your Mac.
"""
import argparse
import copy
import json
import signal
import threading
from pathlib import Path

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
GEO_PATH = HERE / "rig_geometry.yaml"          # OVERWRITTEN on Save (the deliverable)
PREVIEW_PATH = HERE / ".calib_preview.json"    # landmark/orientation sidecar (fast restart)
DEFAULTS_PATH = HERE / ".calib_defaults.json"  # Save/Load Defaults snapshot (all sliders)
MAC_URL = "http://192.168.10.1:4999"           # setup UI — Save POSTs the YAML back here (best-effort archive)

RES_W, RES_H = 1920, 1080                       # projector panel (also read from GEO on load)

# Geometry sliders -> (yaml section, key). Written back into rig_geometry.yaml on Save.
# horizontal_stretch / vertical_stretch are SOLVED from the landmarks, not hand-tuned.
GEO_SLIDERS = {
    "parabola_A": ("screen", "parabola_A"),
    "parabola_B": ("screen", "parabola_B"),
    "throw_ratio": ("projector", "throw_ratio"),
    "horizontal_stretch": ("projector", "horizontal_stretch"),
    "vertical_stretch": ("projector", "vertical_stretch"),
    "optical_axis_elevation_deg": ("projector", "optical_axis_elevation_deg"),
    "lens_offset_vertical": ("projector", "lens_offset_vertical"),
}
# Landmark / orientation controls. Written to rig_geometry.yaml `calibration:` AND the
# sidecar. These DO reach the deployed warp (usable area + orientation), unlike before.
DISPLAY_KEYS = ["offset_x", "offset_y", "flip_h", "flip_v", "frame_x",
                "frame_y_top", "frame_y_bottom", "az90_x"]

# Changing any of these re-anchors the solved stretches to the landmarks.
SOLVE_TRIGGERS = {"az90_x", "offset_x", "frame_y_top", "frame_y_bottom", "azimuth_height",
                  "parabola_A", "parabola_B", "throw_ratio", "optical_axis_elevation_deg",
                  "lens_offset_vertical"}

# Defaults — only fill gaps if rig_geometry.yaml is missing a key.
DEFAULT_GEO = {
    "projector": {"resolution": [1920, 1080], "throw_distance_cm": 48,
                  "throw_ratio": 1.2, "optical_axis_elevation_deg": -3,
                  "lens_offset_vertical": 1.0, "lateral_offset_cm": 0,
                  "horizontal_stretch": 0.85, "vertical_stretch": 3.35},
    "screen": {"parabola_A": 12.7, "parabola_B": 0.044,
               "azimuth_height": 14.4, "height_cm": 20,
               "altitude_min_deg": -49.75, "altitude_max_deg": 25.3},
}

GEO = copy.deepcopy(DEFAULT_GEO)   # full geometry dict (loaded from YAML, written back)
PARAMS = {"parabola_A": 12.7, "parabola_B": 0.044, "throw_ratio": 1.2,
          "horizontal_stretch": 0.85, "vertical_stretch": 3.35,
          "optical_axis_elevation_deg": -3.0, "lens_offset_vertical": 1.0,
          "azimuth_height": 14.4,   # cm, eye/az-line above screen bottom -> drives altitudes
          "offset_x": 0, "offset_y": 0, "flip_h": False, "flip_v": True,
          "frame_x": 40,                  # usable-area inset (px), symmetric L/R
          "frame_y_top": 40, "frame_y_bottom": 40,   # usable-area insets (px), independent T/B
          "az90_x": 1720}                 # display column of the +90° azimuth landmark line
LOCK = threading.Lock()


# ── load / save rig_geometry.yaml ──
def _deep_merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_geometry():
    """Load rig_geometry.yaml into GEO and seed the sliders from it. Landmark/orientation
    controls load from the `calibration:` block (or the sidecar as a fallback)."""
    global RES_W, RES_H
    if GEO_PATH.exists():
        try:
            loaded = yaml.safe_load(GEO_PATH.read_text()) or {}
            _deep_merge(GEO, loaded)
            for slider, (sec, key) in GEO_SLIDERS.items():
                if sec in GEO and key in GEO[sec]:
                    PARAMS[slider] = GEO[sec][key]
            print(f"loaded geometry from {GEO_PATH}")
        except Exception as e:
            print("geometry load failed (using defaults):", e)
    else:
        print(f"{GEO_PATH.name} not found — starting from defaults")
    try:
        RES_W, RES_H = (int(x) for x in GEO["projector"]["resolution"])
    except Exception:
        pass
    # azimuth_height: prefer the stored value; else reconstruct from altitude_min (migration
    # from the old eye_above_screen_bottom_cm). From here it DRIVES the altitudes.
    sc = GEO.get("screen", {})
    if "azimuth_height" in sc:
        PARAMS["azimuth_height"] = float(sc["azimuth_height"])
    elif "eye_above_screen_bottom_cm" in sc:
        PARAMS["azimuth_height"] = float(sc["eye_above_screen_bottom_cm"])
    else:
        try:
            PARAMS["azimuth_height"] = round(
                float(-PARAMS["parabola_A"] * np.tan(np.radians(sc["altitude_min_deg"]))), 1)
        except Exception:
            pass
    # landmark/orientation controls: calibration block first, then sidecar override.
    sources = [GEO.get("calibration", {})]
    if PREVIEW_PATH.exists():
        try:
            sources.append(json.loads(PREVIEW_PATH.read_text()))
        except Exception:
            pass
    for src in sources:
        for k in DISPLAY_KEYS:
            if k in src:
                PARAMS[k] = src[k]
        # migrate a legacy symmetric frame_y -> independent top/bottom insets
        if "frame_y" in src and "frame_y_top" not in src and "frame_y_bottom" not in src:
            PARAMS["frame_y_top"] = int(src["frame_y"])
            PARAMS["frame_y_bottom"] = int(src["frame_y"])
    _coerce_types()
    _apply_visual_field()
    _solve_stretches()


def _coerce_types():
    for k in ("flip_h", "flip_v"):
        PARAMS[k] = bool(PARAMS[k])
    for k in ("offset_x", "offset_y", "frame_x", "frame_y_top", "frame_y_bottom", "az90_x"):
        PARAMS[k] = int(round(float(PARAMS[k])))


def _apply_visual_field():
    """azimuth_height (h) + screen height (hc) + parabola_A (A) DERIVE the visual field:
    the eye/az-line sits h above the screen bottom at depth A, so
      altitude_min = atan(-h / A)          (screen bottom, below eye)
      altitude_max = atan((hc - h) / A)    (screen top, above eye)
    Call under LOCK after h, height_cm, or parabola_A change."""
    h = PARAMS.get("azimuth_height")
    A = PARAMS.get("parabola_A")
    hc = GEO.get("screen", {}).get("height_cm", 20)
    if h is not None and A:
        s = GEO.setdefault("screen", {})
        s["altitude_min_deg"] = round(float(np.degrees(np.arctan2(-h, A))), 2)
        s["altitude_max_deg"] = round(float(np.degrees(np.arctan2(hc - h, A))), 2)


def _params_to_geo(p=None):
    """Full geometry dict from GEO + the live slider values (for the ray trace)."""
    p = p if p is not None else PARAMS
    geo = copy.deepcopy(GEO)
    for slider, (sec, key) in GEO_SLIDERS.items():
        geo.setdefault(sec, {})[key] = p[slider]
    return geo


class _Flow(list):
    """List that yaml.dump renders inline: [a, b, c]."""


yaml.add_representer(
    _Flow, lambda d, data: d.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True))


def _dump_geo(geo):
    g = copy.deepcopy(geo)
    if "projector" in g and "resolution" in g["projector"]:
        g["projector"]["resolution"] = _Flow(g["projector"]["resolution"])
    if "luminance_reference" in g:
        g["luminance_reference"] = [_Flow(p) for p in g["luminance_reference"]]
    return yaml.dump(g, default_flow_style=False, sort_keys=True)


def save_geometry():
    """Merge solved geometry + derived altitudes + azimuth_height + landmark record into
    GEO, overwrite rig_geometry.yaml (dropping azimuth_max_deg), mirror to the sidecar,
    and POST to the Mac for a timestamped archive. Returns the archived filename or None."""
    with LOCK:
        for slider, (sec, key) in GEO_SLIDERS.items():
            GEO.setdefault(sec, {})[key] = PARAMS[slider]
        GEO.setdefault("screen", {})["azimuth_height"] = PARAMS["azimuth_height"]
        _apply_visual_field()
        GEO["screen"].pop("azimuth_max_deg", None)            # extent now from ±90 landmark + frame
        GEO["screen"].pop("eye_above_screen_bottom_cm", None)  # renamed -> azimuth_height
        GEO["calibration"] = {k: PARAMS[k] for k in DISPLAY_KEYS}
        geo_out = copy.deepcopy(GEO)
        preview = {k: PARAMS[k] for k in DISPLAY_KEYS}
    text = _dump_geo(geo_out)
    GEO_PATH.write_text(text)
    PREVIEW_PATH.write_text(json.dumps(preview, indent=2))
    print(f"saved {GEO_PATH}")
    return _post_to_mac(text)


def _post_to_mac(yaml_text):
    """Best-effort push of the saved geometry to the Mac setup UI for timestamped
    archiving. Never raises — the local save has already happened."""
    import urllib.request
    try:
        req = urllib.request.Request(
            MAC_URL + "/api/receive_geometry",
            data=json.dumps({"yaml": yaml_text}).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=4).read().decode())
        print(f"archived on Mac as {resp.get('archived')}")
        return resp.get("archived")
    except Exception as e:
        print(f"[warn] could not archive to Mac ({MAC_URL}): {e}")
        return None


# ── geometry (embedded verbatim from compute_warp_map.py; numpy-only) ──
def ray_screen_intersection(az_deg, alt_deg, A, B):
    az = np.radians(az_deg)
    alt = np.radians(alt_deg)
    dx = np.sin(az)
    dy = np.cos(az)
    if abs(dx) < 1e-10:
        if dy <= 0:
            return None
        r_horiz = A / dy
    else:
        a_coef = B * dx * dx
        if a_coef <= 1e-12:          # B <= 0 -> degenerate (flat/invalid screen), no intersection
            return None
        disc = dy ** 2 - 4 * a_coef * (-A)
        if disc < 0:
            return None
        r_horiz = (-dy + np.sqrt(disc)) / (2 * a_coef)
        if r_horiz <= 0:
            r_horiz = (-dy - np.sqrt(disc)) / (2 * a_coef)
        if r_horiz <= 0:
            return None
    x_s = r_horiz * dx
    y_s = r_horiz * dy
    z_s = (r_horiz / np.cos(alt)) * np.sin(alt)
    return (x_s, y_s, z_s)


def build_projector(geo):
    p = geo["projector"]
    res_w, res_h = p["resolution"]
    throw_dist = p["throw_distance_cm"]
    ax_el = np.radians(p["optical_axis_elevation_deg"])
    lat_offset = p.get("lateral_offset_cm", 0.0)
    base = throw_dist / p["throw_ratio"]
    img_w = base / p.get("horizontal_stretch", 1.0)        # larger = wider
    img_h = base * (res_h / res_w) / p.get("vertical_stretch", 1.0)  # larger = taller
    A = geo["screen"]["parabola_A"]
    alt_c = np.radians((geo["screen"]["altitude_min_deg"] +
                        geo["screen"]["altitude_max_deg"]) / 2)
    sc = ray_screen_intersection(0.0, np.degrees(alt_c), A, geo["screen"]["parabola_B"])
    if sc is None:
        raise ValueError("no screen center")
    fwd = np.array([lat_offset, np.cos(ax_el), -np.sin(ax_el)])
    fwd = fwd / np.linalg.norm(fwd)
    right = np.cross(fwd, np.array([0, 0, 1.0])); right /= np.linalg.norm(right)
    up = np.cross(right, fwd); up /= np.linalg.norm(up)
    proj_pos = np.array(sc) - throw_dist * fwd
    return {"pos": proj_pos, "right": right, "up": up, "forward": fwd,
            "img_w": img_w, "img_h": img_h, "res": (res_w, res_h),
            "throw": throw_dist, "lens_offset_v": p.get("lens_offset_vertical", 0.0)}


def screen_point_to_pixel(pt, proj):
    v = np.array(pt) - proj["pos"]
    depth = np.dot(v, proj["forward"])
    if depth <= 0:
        return None
    x_img = np.dot(v, proj["right"]) / depth * proj["throw"]
    y_img = np.dot(v, proj["up"]) / depth * proj["throw"]
    res_w, res_h = proj["res"]
    ov = proj.get("lens_offset_v", 0.0)
    px = (x_img / proj["img_w"] + 0.5) * res_w
    py = (1.0 - (y_img / proj["img_h"] + 0.5 - ov * 0.5)) * res_h
    return (px, py)


# ── landmark solver ──
def _model_pixel(az, alt, geo, proj):
    pt = ray_screen_intersection(az, alt, geo["screen"]["parabola_A"],
                                 geo["screen"]["parabola_B"])
    if pt is None:
        return None
    r = screen_point_to_pixel(pt, proj)
    if r is None or not (np.isfinite(r[0]) and np.isfinite(r[1])):
        return None
    return r


def _solve_stretches():
    """Re-anchor horizontal/vertical_stretch to the registered landmarks (call under LOCK).

    The projected span scales EXACTLY linearly with each stretch (img_w ∝ 1/hstretch,
    img_h ∝ 1/vstretch, and az=0 stays at the panel center column), so a single
    proportional rescale hits the target with no iteration:
      - horizontal: model |px(±90)-px(0)| span -> the marked ±90 half-span in display px.
      - vertical:   model |py(alt_max)-py(alt_min)| span -> the usable height
                    (res_h - frame_y_top - frame_y_bottom).
    """
    geo = _params_to_geo()
    try:
        proj = build_projector(geo)
    except Exception:
        return
    res_w, res_h = geo["projector"]["resolution"]
    almin = geo["screen"]["altitude_min_deg"]
    almax = geo["screen"]["altitude_max_deg"]

    # Horizontal: anchor at the eye line (alt=0), where the origin cross and the ±90°
    # landmark are read. (The ±90° column varies slightly with altitude because the tilted
    # optical axis adds perspective, so the anchor altitude must be fixed.)
    p0 = _model_pixel(0.0, 0.0, geo, proj)
    p90 = _model_pixel(90.0, 0.0, geo, proj)
    if p0 and p90:
        cur = abs(p90[0] - p0[0])
        tgt = abs(PARAMS["az90_x"] - (res_w / 2 + PARAMS["offset_x"]))
        if cur > 1.0 and tgt > 1.0:
            PARAMS["horizontal_stretch"] = float(
                np.clip(PARAMS["horizontal_stretch"] * tgt / cur, 0.05, 6.0))

    ptop = _model_pixel(0.0, almax, geo, proj)
    pbot = _model_pixel(0.0, almin, geo, proj)
    if ptop and pbot:
        cur = abs(pbot[1] - ptop[1])
        tgt = abs(res_h - PARAMS["frame_y_top"] - PARAMS["frame_y_bottom"])
        if cur > 1.0 and tgt > 1.0:
            PARAMS["vertical_stretch"] = float(
                np.clip(PARAMS["vertical_stretch"] * tgt / cur, 0.1, 6.0))


# ── colors (B/G only; red LED off) ──
BG_COL = (0, 18, 18)
GREEN = (0, 255, 0)       # 0° vertical, 0° horizontal, ±90° verticals
CYAN = (0, 220, 220)      # every other 30° line
FRAME_COL = (0, 255, 255)  # usable-area boundary


def _az_color(az_t):
    return GREEN if az_t in (0, 90, -90) else CYAN


def render(p, pygame, W, H):
    geo = _params_to_geo(p)
    A, B = p["parabola_A"], p["parabola_B"]
    almin = geo["screen"]["altitude_min_deg"]
    almax = geo["screen"]["altitude_max_deg"]
    try:
        proj = build_projector(geo)
    except Exception:
        return pygame.Surface((W, H))

    def proj_pt(az, alt):
        r = _model_pixel(az, alt, geo, proj)
        return (int(r[0]), int(r[1])) if r else None

    surf = pygame.Surface((W, H))
    surf.fill(BG_COL)
    # altitude isolines every 30° (plus the 0° eye line)
    a0 = int(np.floor(almin / 30) * 30)
    alts = sorted(set(list(range(a0, int(almax) + 30, 30)) + [0]))
    for alt_t in alts:
        if alt_t < almin - 0.5 or alt_t > almax + 0.5:
            continue
        pts = [q for q in (proj_pt(az, alt_t) for az in np.linspace(-90, 90, 80)) if q]
        if len(pts) >= 2:
            green = (alt_t == 0)
            pygame.draw.lines(surf, GREEN if green else CYAN, False, pts, 4 if green else 2)
    # azimuth isolines every 30° (0° meridian and ±90° drawn green/thick)
    for az_t in range(-90, 91, 30):
        pts = [q for q in (proj_pt(az_t, alt) for alt in np.linspace(almin, almax, 80)) if q]
        if len(pts) >= 2:
            green = az_t in (0, 90, -90)
            pygame.draw.lines(surf, _az_color(az_t), False, pts, 4 if green else 2)
    # azimuth angle numbers along the eye line (altitude 0)
    afont = pygame.font.SysFont(None, 40)
    for az_t in range(-90, 91, 30):
        q = proj_pt(az_t, 0)
        if q:
            s = afont.render(f"{az_t}", True, (0, 220, 255))
            surf.blit(s, s.get_rect(center=(q[0], q[1] - 16)))
    return surf


def grid_lines(p):
    """Az/alt 30° isolines in projector pixels for the web preview (mirrors render())."""
    geo = _params_to_geo(p)
    A, B = p["parabola_A"], p["parabola_B"]
    almin = geo["screen"]["altitude_min_deg"]
    almax = geo["screen"]["altitude_max_deg"]
    res_w, res_h = geo["projector"]["resolution"]
    out = {"res": [res_w, res_h], "almin": almin, "almax": almax,
           "azmin": -90, "azmax": 90,
           "frame_x": p["frame_x"], "frame_y_top": p["frame_y_top"],
           "frame_y_bottom": p["frame_y_bottom"],
           "hstretch": round(p["horizontal_stretch"], 4),
           "vstretch": round(p["vertical_stretch"], 4),
           "alt_lines": [], "az_lines": []}
    try:
        proj = build_projector(geo)
    except Exception:
        return out

    def pp(az, alt):
        r = _model_pixel(az, alt, geo, proj)
        return [float(r[0]), float(r[1])] if r else None

    a0 = int(np.floor(almin / 30) * 30)
    for alt_t in sorted(set(list(range(a0, int(almax) + 30, 30)) + [0])):
        if alt_t < almin - 0.5 or alt_t > almax + 0.5:
            continue
        pts = [q for q in (pp(az, alt_t) for az in np.linspace(-90, 90, 60)) if q]
        if len(pts) >= 2:
            out["alt_lines"].append({"t": alt_t, "pts": pts})
    for az_t in range(-90, 91, 30):
        pts = [q for q in (pp(az_t, alt) for alt in np.linspace(almin, almax, 60)) if q]
        if len(pts) >= 2:
            out["az_lines"].append({"t": az_t, "pts": pts})
    return out


PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Geo calibration</title><style>
body{font-family:sans-serif;background:#1b1b1b;color:#eee;padding:18px;font-size:15px}
.row{margin:12px 0}label{display:inline-block;width:180px}
input[type=range]{width:42%;vertical-align:middle}output{display:inline-block;min-width:70px;text-align:right;color:#7cf}
button{padding:9px 18px;margin:12px 8px 0 0;font-size:15px}#msg{margin-left:10px;color:#8f8}
.k{color:#fd6}.note{color:#888;font-size:13px}.solved{color:#6f6}
#previews{display:flex;gap:18px;margin:8px 0 20px;max-width:1000px}
.pv{flex:1}.pv h4{margin:0 0 5px;font-weight:normal;color:#bbb;font-size:13px}
canvas{width:100%;height:auto;background:#111;border:1px solid #333;border-radius:4px}
h4.sec{margin:18px 0 2px;color:#9cf;font-weight:normal;border-bottom:1px solid #333;padding-bottom:3px}</style></head><body>
<h3>Geometry calibration — landmark registration</h3>
<p>Register the grid to the real screen: set the <span class=k>frame</span> to the usable
area, place the green <span class=k>0° cross</span> with offsets, type the measured
<span class=k>azimuth_height</span>, then drag <span class=k>az90_x</span> so the green ±90°
lines sit on the physical 90° marks. Stretches are solved for you. Tune
<span class=k>parabola_B</span> for mid-field shape. Save writes <span class=k>rig_geometry.yaml</span>.</p>
<div class=row><label>azimuth_height</label>
<input type=number id=azh step=0.1 style="width:80px" onchange="set('azimuth_height',this.value,0)"> cm
<span class=note id=azhnote></span></div>
<div id=previews>
  <div class=pv><h4>TARGET — flat/undistorted (what the mouse should see)</h4><canvas id=cvIdeal width=480 height=270></canvas></div>
  <div class=pv><h4>PROJECTED — warped for the curved screen (what goes IRL)</h4><canvas id=cvWarp width=480 height=270></canvas></div>
</div>
<div id=ctrls></div>
<button onclick=save()>Save rig_geometry.yaml</button>
<button onclick=saveDefaults()>Save defaults</button>
<button onclick=loadDefaults()>Load defaults</button><span id=msg></span>
<p class=note>horizontal_stretch / vertical_stretch are <span class=solved>solved</span> from the ±90°
and frame landmarks. flip/offset/frame/az90 persist to .calib_preview.json and the yaml
`calibration:` block. Defaults live on the projector (.calib_defaults.json).</p>
<script>
const RES=[1920,1080];
// [key, min, max, step, section-label]
const S=[
['parabola_A',1,30,0.05,'Screen (measured)'],['parabola_B',0.001,0.30,0.001,''],
['throw_ratio',0.4,1.6,0.01,'Projector (measured)'],
['optical_axis_elevation_deg',-30,60,0.5,''],['lens_offset_vertical',0,1,0.05,''],
['horizontal_stretch',0.05,6.0,0.01,'Solved from landmarks'],['vertical_stretch',0.1,6.0,0.01,''],
['offset_x',-1000,1000,1,'Landmarks — origin cross'],['offset_y',-1000,1000,1,''],
['az90_x',0,1920,1,'Landmark — ±90° lines'],
['frame_x',0,600,1,'Usable area (px insets)'],
['frame_y_top',0,540,1,''],['frame_y_bottom',0,540,1,'']];
const T=['flip_h','flip_v'];let P={};
const DEC={};for(const r of S)DEC[r[0]]=Math.max(0,-Math.floor(Math.log10(r[3])+1e-9));
function fmt(k,v){return (+v).toFixed(DEC[k]??2);}
function outLabel(k,v){v=+v;
  if(k==='frame_x')return v+'px  usable x: '+v+'..'+(RES[0]-v)+' ('+(RES[0]-2*v)+')';
  if(k==='frame_y_top')return v+'px  top edge y='+v+'  (usable h '+(RES[1]-v-(+P.frame_y_bottom||0))+')';
  if(k==='frame_y_bottom')return v+'px  bottom edge y='+(RES[1]-v)+'  (usable h '+(RES[1]-(+P.frame_y_top||0)-v)+')';
  if(k==='az90_x')return 'col '+v;
  return fmt(k,v);}
const SOLVED=new Set(['horizontal_stretch','vertical_stretch']);
async function load(){P=await(await fetch('/get')).json();draw();refreshPreview();}
function draw(){let h='';for(const[k,a,b,s,sec] of S){
  if(sec)h+=`<h4 class=sec>${sec}</h4>`;
  const ro=SOLVED.has(k)?' disabled':'';
  h+=`<div class=row><label ${SOLVED.has(k)?'class=solved':''}>${k}</label>
<input type=range min=${a} max=${b} step=${s} value=${P[k]}${ro} oninput="set('${k}',this.value,1)">
<output id=o_${k} style="${(k==='frame_x'||k==='frame_y_top'||k==='frame_y_bottom')?'min-width:230px':''}">${outLabel(k,P[k])}</output></div>`;}
for(const k of T)h+=`<div class=row><label>${k}</label>
<input type=checkbox ${P[k]?'checked':''} onchange="set('${k}',this.checked?1:0,0)"></div>`;
document.getElementById('ctrls').innerHTML=h;
document.getElementById('azh').value=(+P['azimuth_height']).toFixed(1);}
async function set(k,v,num){if(num){P[k]=+v;document.getElementById('o_'+k).textContent=outLabel(k,v);
  if(k==='frame_y_top'||k==='frame_y_bottom'){const o=(k==='frame_y_top')?'frame_y_bottom':'frame_y_top';
    const e=document.getElementById('o_'+o);if(e)e.textContent=outLabel(o,P[o]);}}
await fetch('/set?k='+k+'&v='+v);schedulePreview();}
function flash(t){let m=document.getElementById('msg');m.textContent=t;setTimeout(()=>m.textContent='',2500);}
async function save(){let r=await(await fetch('/save')).json();
flash(r.archived?('saved ✓ → '+r.archived):'saved ✓ (Mac not reached — local only)');}
async function saveDefaults(){await fetch('/save_defaults');flash('defaults saved ✓');}
async function loadDefaults(){P=await(await fetch('/load_defaults')).json();draw();refreshPreview();flash('defaults loaded ✓');}
// ── side-by-side preview: left = ideal flat grid, right = the warp actually projected ──
let previewTimer=null;
function schedulePreview(){clearTimeout(previewTimer);previewTimer=setTimeout(refreshPreview,120);}
function azcol(t){return (t===0||t===90||t===-90)?'#0f0':'#0dd';}
function altcol(t){return t===0?'#0f0':'#0dd';}
function poly(ctx,pts,sx,sy){ctx.beginPath();pts.forEach((p,i)=>{const x=p[0]*sx,y=p[1]*sy;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();}
function drawIdeal(d){const c=document.getElementById('cvIdeal'),ctx=c.getContext('2d'),W=c.width,H=c.height;
ctx.fillStyle='#111';ctx.fillRect(0,0,W,H);
const X=a=>(a-d.azmin)/(d.azmax-d.azmin)*W,Y=a=>(1-(a-d.almin)/(d.almax-d.almin))*H;
for(const l of d.alt_lines){ctx.strokeStyle=altcol(l.t);ctx.lineWidth=l.t===0?3:1.5;ctx.beginPath();ctx.moveTo(0,Y(l.t));ctx.lineTo(W,Y(l.t));ctx.stroke();}
for(const l of d.az_lines){ctx.strokeStyle=azcol(l.t);ctx.lineWidth=(l.t===0||Math.abs(l.t)===90)?3:1.5;ctx.beginPath();ctx.moveTo(X(l.t),0);ctx.lineTo(X(l.t),H);ctx.stroke();}}
function drawWarp(d){const c=document.getElementById('cvWarp'),ctx=c.getContext('2d'),W=c.width,H=c.height;
ctx.fillStyle='#111';ctx.fillRect(0,0,W,H);const sx=W/d.res[0],sy=H/d.res[1];
for(const l of d.alt_lines){ctx.strokeStyle=altcol(l.t);ctx.lineWidth=l.t===0?3:1.5;poly(ctx,l.pts,sx,sy);}
for(const l of d.az_lines){ctx.strokeStyle=azcol(l.t);ctx.lineWidth=(l.t===0||Math.abs(l.t)===90)?3:1.5;poly(ctx,l.pts,sx,sy);}
// usable-area frame
ctx.strokeStyle='#0ff';ctx.lineWidth=1.5;
ctx.strokeRect(d.frame_x*sx,d.frame_y_top*sy,(d.res[0]-2*d.frame_x)*sx,(d.res[1]-d.frame_y_top-d.frame_y_bottom)*sy);}
async function refreshPreview(){const d=await(await fetch('/preview')).json();drawIdeal(d);drawWarp(d);
// reflect solved stretches + derived altitude range
P.horizontal_stretch=d.hstretch;P.vertical_stretch=d.vstretch;
const oh=document.getElementById('o_horizontal_stretch');if(oh)oh.textContent=fmt('horizontal_stretch',d.hstretch);
const ov=document.getElementById('o_vertical_stretch');if(ov)ov.textContent=fmt('vertical_stretch',d.vstretch);
const rh=document.querySelector('input[oninput*="horizontal_stretch"]');if(rh)rh.value=d.hstretch;
const rv=document.querySelector('input[oninput*="vertical_stretch"]');if(rv)rv.value=d.vstretch;
const en=document.getElementById('azhnote');if(en)en.textContent='→ altitude '+d.almin.toFixed(1)+'° … '+d.almax.toFixed(1)+'° (bottom … top)';}
load();</script></body></html>"""


def start_web(port):
    from flask import Flask, request
    app = Flask(__name__)

    @app.route("/")
    def idx():
        return PAGE

    @app.route("/get")
    def get():
        with LOCK:
            return json.dumps(PARAMS)

    @app.route("/set")
    def setp():
        k, v = request.args["k"], request.args["v"]
        with LOCK:
            if k in ("flip_h", "flip_v"):
                PARAMS[k] = (v == "1")
            elif k in ("offset_x", "offset_y", "frame_x", "frame_y_top",
                       "frame_y_bottom", "az90_x"):
                PARAMS[k] = int(float(v))
            elif k in PARAMS:
                PARAMS[k] = float(v)
            if k in ("azimuth_height", "parabola_A"):
                _apply_visual_field()
            if k in SOLVE_TRIGGERS:
                _solve_stretches()
        return "ok"

    @app.route("/save")
    def save():
        archived = save_geometry()
        return json.dumps({"ok": True, "archived": archived})

    @app.route("/preview")
    def preview():
        with LOCK:
            p = dict(PARAMS)
        return json.dumps(grid_lines(p))

    @app.route("/save_defaults")
    def save_defaults():
        with LOCK:
            DEFAULTS_PATH.write_text(json.dumps(PARAMS, indent=2))
        print(f"saved defaults {DEFAULTS_PATH}")
        return "ok"

    @app.route("/load_defaults")
    def load_defaults():
        with LOCK:
            if DEFAULTS_PATH.exists():
                try:
                    d = json.loads(DEFAULTS_PATH.read_text())
                    for k, v in d.items():
                        if k in PARAMS:
                            PARAMS[k] = v
                    _coerce_types()
                except Exception as e:
                    print("load_defaults failed:", e)
            _apply_visual_field()
            _solve_stretches()
            return json.dumps(PARAMS)

    app.run(host="0.0.0.0", port=port, threaded=True)


def run(port, geo_path=None):
    import pygame
    global GEO_PATH, PREVIEW_PATH
    if geo_path:
        GEO_PATH = Path(geo_path).expanduser().resolve()
        PREVIEW_PATH = GEO_PATH.parent / ".calib_preview.json"  # keep the sidecar paired
    load_geometry()
    if not DEFAULTS_PATH.exists():          # seed defaults from the loaded (known-good) values
        DEFAULTS_PATH.write_text(json.dumps(PARAMS, indent=2))
    threading.Thread(target=start_web, args=(port,), daemon=True).start()

    pygame.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    SW, SH = screen.get_size()
    running = {"v": True}
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__("v", False))
    clock = pygame.time.Clock()
    last_key = None
    composed = pygame.Surface((SW, SH))
    while running["v"]:
        with LOCK:
            p = dict(PARAMS)
        key = tuple(sorted((k, str(v)) for k, v in p.items()))
        if key != last_key:
            last_key = key
            grid = render(p, pygame, SW, SH)
            grid = pygame.transform.flip(grid, p["flip_h"], p["flip_v"])
            composed.fill((0, 0, 0))
            composed.blit(grid, (p["offset_x"], -p["offset_y"]))
            # thin cyan usable-area frame lines (x symmetric; y independent top/bottom)
            fx = int(p["frame_x"])
            fyt, fyb = int(p["frame_y_top"]), int(p["frame_y_bottom"])
            for x in (fx, SW - fx):
                pygame.draw.line(composed, FRAME_COL, (x, 0), (x, SH), 2)
            for y in (fyt, SH - fyb):
                pygame.draw.line(composed, FRAME_COL, (0, y), (SW, y), 2)
        screen.blit(composed, (0, 0))
        pygame.display.flip()
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and
                                         e.key in (pygame.K_q, pygame.K_ESCAPE)):
                running["v"] = False
        clock.tick(20)
    pygame.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Landmark geometry calibration")
    ap.add_argument("--port", type=int, default=5091)
    ap.add_argument("--geo", default=None,
                    help="path to rig_geometry.yaml (default: next to this script)")
    a = ap.parse_args()
    run(a.port, a.geo)
