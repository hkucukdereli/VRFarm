"""
calib_geo.py — LIVE geometry calibration with curvature.

Unlike calib_tool.py (affine-only on a fixed warp map), this draws the azimuth/altitude
isolines by forward-projecting sampled angles THROUGH the projector+screen geometry on
every change — so every geometry parameter, including the screen CURVATURE
(parabola_A / parabola_B), is a live slider. Fast because it only projects a few hundred
points per line (no 2M-pixel warp regen).

Adjust from a web slider panel on your Mac; watch the projector update live:
  parabola_A : screen depth (eye->vertex, cm)
  parabola_B : screen CURVATURE (bigger = more curved) <- the one you want
  throw_ratio: horizontal scale
  vertical_stretch: vertical scale (anamorphic)
  offset_x/y : fine position (+y = up)
  flip_h/flip_v : orientation

Run on mozzarella:
  DISPLAY=:0 ~/miniforge3/envs/rig/bin/python calib_geo.py
Then open http://192.168.10.102:5091 on your Mac. Click Save -> geo_fit.json.
"""
import argparse
import copy
import json
import signal
import threading
from pathlib import Path

import numpy as np

SAVE_PATH = Path.home() / "rig" / "calibration" / "geo_fit.json"

# Base geometry (non-slider params). Slider params override these each render.
BASE_GEO = {
    "projector": {"resolution": [1920, 1080], "throw_distance_cm": 48,
                  "throw_ratio": 0.8, "optical_axis_elevation_deg": 49,
                  "lens_offset_vertical": 0, "lateral_offset_cm": 0,
                  "vertical_stretch": 2.4},
    "screen": {"parabola_A": 12.7, "parabola_B": 0.04186,
               "altitude_min_deg": -48.6, "altitude_max_deg": 25.3,
               "azimuth_max_deg": 105},
}

PARAMS = {"parabola_A": 12.7, "parabola_B": 0.04186, "throw_ratio": 0.8,
          "horizontal_stretch": 1.0, "vertical_stretch": 2.4,
          "optical_axis_elevation_deg": 49.0, "lens_offset_vertical": 0.0,
          "offset_x": 0, "offset_y": 186, "flip_h": False, "flip_v": True}
LOCK = threading.Lock()


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
    geo = copy.deepcopy(BASE_GEO)
    geo["screen"]["parabola_A"] = p["parabola_A"]
    geo["screen"]["parabola_B"] = p["parabola_B"]
    geo["projector"]["throw_ratio"] = p["throw_ratio"]
    geo["projector"]["horizontal_stretch"] = p["horizontal_stretch"]
    geo["projector"]["vertical_stretch"] = p["vertical_stretch"]
    geo["projector"]["optical_axis_elevation_deg"] = p["optical_axis_elevation_deg"]
    geo["projector"]["lens_offset_vertical"] = p["lens_offset_vertical"]
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
.row{margin:14px 0}label{display:inline-block;width:140px}
input[type=range]{width:48%;vertical-align:middle}output{display:inline-block;width:70px;text-align:right;color:#7cf}
button{padding:9px 18px;margin-top:12px;font-size:15px}#msg{margin-left:10px;color:#8f8}
.k{color:#fd6}</style></head><body>
<h3>Geometry calibration (live)</h3>
<p>Tune <span class=k>parabola_B</span> until the cyan horizontal lines are straight/level.</p>
<div id=ctrls></div>
<button onclick=save()>Save geo_fit.json</button><span id=msg></span>
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
        with LOCK:
            SAVE_PATH.write_text(json.dumps(PARAMS, indent=2))
        return "saved"

    app.run(host="0.0.0.0", port=port, threaded=True)


def run(port):
    import pygame
    try:
        if SAVE_PATH.exists():
            saved = json.loads(SAVE_PATH.read_text())
            with LOCK:
                for k in list(PARAMS):
                    if k in saved:
                        PARAMS[k] = saved[k]
            print("loaded saved geo fit:", saved)
    except Exception as e:
        print("no saved fit:", e)
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
    a = ap.parse_args()
    run(a.port)
