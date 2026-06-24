"""
calib_geo.py — LIVE geometry calibration with curvature.

Draws the azimuth/altitude isolines by forward-projecting sampled angles THROUGH the
projector+screen geometry on every change — so every geometry parameter, including the
screen CURVATURE (parabola_A / parabola_B), is a live slider. Fast because it only
projects a few hundred points per line (no 2M-pixel warp regen).

Adjust from a web slider panel on your Mac; watch the projector update live:
  parabola_A : screen depth (eye->vertex, cm)
  parabola_B : screen CURVATURE (bigger = more curved) <- the one you want
  throw_ratio / horizontal_stretch / vertical_stretch : image scale (anamorphic)
  optical_axis_elevation_deg / lens_offset_vertical   : projector geometry
  offset_x/y, flip_h/flip_v : preview alignment only (see below)

THE DELIVERABLE IS rig_geometry.yaml. The geometry sliders edit it directly — clicking
Save OVERWRITES rig_geometry.yaml (next to this script) with the tuned values, preserving
every other section (mouse, luminance_reference, altitude/azimuth limits, ...). The setup
UI then loads the same file into its editable boxes and feeds it to compute_warp_map.py.

The flip_h/flip_v/offset_x/offset_y sliders ONLY orient this live PREVIEW so it matches
the physical projector — nothing in the experiment render path reads them, so they are
NOT written into rig_geometry.yaml. They persist to a small sidecar (.calib_preview.json)
so a restart keeps your preview orientation.

Run on mozzarella (projector X must be up):
  DISPLAY=:0 ~/miniforge3/envs/rig/bin/python calib_geo.py
Then open http://192.168.10.102:5091 on your Mac. Tune parabola_B until the cyan
horizontal lines are straight/level on the curved screen, then click Save.
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
PREVIEW_PATH = HERE / ".calib_preview.json"    # preview-only flips/offsets

# Geometry sliders -> (yaml section, key). Written back into rig_geometry.yaml on Save.
GEO_SLIDERS = {
    "parabola_A": ("screen", "parabola_A"),
    "parabola_B": ("screen", "parabola_B"),
    "throw_ratio": ("projector", "throw_ratio"),
    "horizontal_stretch": ("projector", "horizontal_stretch"),
    "vertical_stretch": ("projector", "vertical_stretch"),
    "optical_axis_elevation_deg": ("projector", "optical_axis_elevation_deg"),
    "lens_offset_vertical": ("projector", "lens_offset_vertical"),
}
PREVIEW_KEYS = ["offset_x", "offset_y", "flip_h", "flip_v"]

# Defaults — only fill gaps if rig_geometry.yaml is missing a key.
DEFAULT_GEO = {
    "projector": {"resolution": [1920, 1080], "throw_distance_cm": 48,
                  "throw_ratio": 0.8, "optical_axis_elevation_deg": 49,
                  "lens_offset_vertical": 0, "lateral_offset_cm": 0,
                  "horizontal_stretch": 1.0, "vertical_stretch": 2.4},
    "screen": {"parabola_A": 12.7, "parabola_B": 0.04186,
               "altitude_min_deg": -48.6, "altitude_max_deg": 25.3,
               "azimuth_max_deg": 105, "height_cm": 20},
    "mouse": {"eye_height_above_ball_cm": 2.54, "ball_diameter_cm": 20.32},
}

GEO = copy.deepcopy(DEFAULT_GEO)   # full geometry dict (loaded from YAML, written back)
PARAMS = {"parabola_A": 12.7, "parabola_B": 0.04186, "throw_ratio": 0.8,
          "horizontal_stretch": 1.0, "vertical_stretch": 2.4,
          "optical_axis_elevation_deg": 49.0, "lens_offset_vertical": 0.0,
          "offset_x": 0, "offset_y": 186, "flip_h": False, "flip_v": True}
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
    """Load rig_geometry.yaml into GEO and seed the geometry sliders from it.
    Preview flips/offsets load from the sidecar."""
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
    if PREVIEW_PATH.exists():
        try:
            pv = json.loads(PREVIEW_PATH.read_text())
            for k in PREVIEW_KEYS:
                if k in pv:
                    PARAMS[k] = pv[k]
        except Exception:
            pass


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
    """Merge slider values into GEO, overwrite rig_geometry.yaml; preview -> sidecar."""
    with LOCK:
        for slider, (sec, key) in GEO_SLIDERS.items():
            GEO.setdefault(sec, {})[key] = PARAMS[slider]
        geo_out = copy.deepcopy(GEO)
        preview = {k: PARAMS[k] for k in PREVIEW_KEYS}
    GEO_PATH.write_text(_dump_geo(geo_out))
    PREVIEW_PATH.write_text(json.dumps(preview, indent=2))
    print(f"saved {GEO_PATH}")


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


def render(p, pygame, W, H):
    geo = copy.deepcopy(GEO)
    for slider, (sec, key) in GEO_SLIDERS.items():
        geo.setdefault(sec, {})[key] = p[slider]
    A, B = p["parabola_A"], p["parabola_B"]
    almin = geo["screen"]["altitude_min_deg"]
    almax = geo["screen"]["altitude_max_deg"]
    try:
        proj = build_projector(geo)
    except Exception:
        return pygame.Surface((W, H))

    def proj_pt(az, alt):
        pt = ray_screen_intersection(az, alt, A, B)
        if pt is None:
            return None
        r = screen_point_to_pixel(pt, proj)
        return None if r is None else (int(r[0]), int(r[1]))

    surf = pygame.Surface((W, H))
    surf.fill((28, 28, 28))
    a0 = int(np.floor(almin / 10) * 10)
    for alt_t in range(a0, int(almax) + 1, 10):                 # altitude isolines (cyan)
        pts = [q for q in (proj_pt(az, alt_t) for az in np.linspace(-90, 90, 80)) if q]
        if len(pts) >= 2:
            pygame.draw.lines(surf, (0, 210, 210), False, pts, 2)
    for az_t in range(-80, 81, 20):                             # azimuth isolines (white/yellow)
        pts = [q for q in (proj_pt(az_t, alt) for alt in np.linspace(almin, almax, 80)) if q]
        if len(pts) >= 2:
            pygame.draw.lines(surf, (255, 255, 0) if az_t == 0 else (255, 255, 255),
                              False, pts, 3 if az_t == 0 else 2)
    # orientation labels (anchored to angles; flip with the grid so they read upright)
    font = pygame.font.SysFont(None, 64)
    almid = (almin + almax) / 2
    for text, az, alt, col in [(f"TOP {almax:.0f}", 0, almax, (130, 255, 130)),
                               (f"BOTTOM {almin:.0f}", 0, almin, (130, 255, 130)),
                               ("LEFT -80", -80, almid, (255, 150, 150)),
                               ("RIGHT +80", 80, almid, (255, 150, 150))]:
        q = proj_pt(az, alt)
        if q:
            s = font.render(text, True, col)
            surf.blit(s, s.get_rect(center=q))
    return surf


PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Geo calibration</title><style>
body{font-family:sans-serif;background:#1b1b1b;color:#eee;padding:18px;font-size:15px}
.row{margin:14px 0}label{display:inline-block;width:170px}
input[type=range]{width:44%;vertical-align:middle}output{display:inline-block;width:70px;text-align:right;color:#7cf}
button{padding:9px 18px;margin-top:12px;font-size:15px}#msg{margin-left:10px;color:#8f8}
.k{color:#fd6}.note{color:#888;font-size:13px}</style></head><body>
<h3>Geometry calibration (live)</h3>
<p>Tune <span class=k>parabola_B</span> until the cyan horizontal lines are straight/level.
Save overwrites <span class=k>rig_geometry.yaml</span>.</p>
<div id=ctrls></div>
<button onclick=save()>Save rig_geometry.yaml</button><span id=msg></span>
<p class=note>flip/offset sliders only align this preview (saved to .calib_preview.json), not the warp.</p>
<script>
const S=[['parabola_A',-16,16,0.05],['parabola_B',-0.30,0.30,0.001],
['throw_ratio',0.4,1.6,0.01],['horizontal_stretch',0.4,4.0,0.05],['vertical_stretch',0.5,4.0,0.05],
['optical_axis_elevation_deg',-30,60,0.5],['lens_offset_vertical',0,1,0.05],
['offset_x',-1000,1000,2],['offset_y',-1000,1000,2]];
const T=['flip_h','flip_v'];let P={};
async function load(){P=await(await fetch('/get')).json();draw();}
function draw(){let h='';for(const[k,a,b,s]of S)h+=`<div class=row><label>${k}</label>
<input type=range min=${a} max=${b} step=${s} value=${P[k]} oninput="set('${k}',this.value,1)">
<output id=o_${k}>${(+P[k]).toFixed(s<0.01?3:(s<1?2:0))}</output></div>`;
for(const k of T)h+=`<div class=row><label>${k}</label>
<input type=checkbox ${P[k]?'checked':''} onchange="set('${k}',this.checked?1:0,0)"></div>`;
document.getElementById('ctrls').innerHTML=h;}
async function set(k,v,num){if(num)document.getElementById('o_'+k).textContent=(+v).toFixed(Math.abs(v)<1?3:(Math.abs(v)<10?2:0));
await fetch('/set?k='+k+'&v='+v);}
async function save(){await fetch('/save');let m=document.getElementById('msg');m.textContent='saved ✓';
setTimeout(()=>m.textContent='',1500);}
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
            elif k in ("offset_x", "offset_y"):
                PARAMS[k] = int(float(v))
            elif k in PARAMS:
                PARAMS[k] = float(v)
        return "ok"

    @app.route("/save")
    def save():
        save_geometry()
        return "saved"

    app.run(host="0.0.0.0", port=port, threaded=True)


def run(port, geo_path=None):
    import pygame
    global GEO_PATH, PREVIEW_PATH
    if geo_path:
        GEO_PATH = Path(geo_path).expanduser().resolve()
        PREVIEW_PATH = GEO_PATH.parent / ".calib_preview.json"  # keep the sidecar paired
    load_geometry()
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
        key = tuple(sorted(p.items()))
        if key != last_key:
            last_key = key
            grid = render(p, pygame, SW, SH)
            grid = pygame.transform.flip(grid, p["flip_h"], p["flip_v"])
            composed.fill((0, 0, 0))
            composed.blit(grid, (p["offset_x"], -p["offset_y"]))
        screen.blit(composed, (0, 0))
        pygame.display.flip()
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and
                                         e.key in (pygame.K_q, pygame.K_ESCAPE)):
                running["v"] = False
        clock.tick(20)
    pygame.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Live geometry calibration")
    ap.add_argument("--port", type=int, default=5091)
    ap.add_argument("--geo", default=None,
                    help="path to rig_geometry.yaml (default: next to this script)")
    a = ap.parse_args()
    run(a.port, a.geo)
