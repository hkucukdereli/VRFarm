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
DEFAULTS_PATH = HERE / ".calib_defaults.json"  # Save/Load Defaults snapshot (all sliders)
MAC_URL = "http://192.168.10.1:4999"           # setup UI — Save POSTs the YAML back here (best-effort archive)

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
PREVIEW_KEYS = ["offset_x", "offset_y", "flip_h", "flip_v", "frame"]

# Defaults — only fill gaps if rig_geometry.yaml is missing a key.
DEFAULT_GEO = {
    "projector": {"resolution": [1920, 1080], "throw_distance_cm": 48,
                  "throw_ratio": 0.8, "optical_axis_elevation_deg": 49,
                  "lens_offset_vertical": 0, "lateral_offset_cm": 0,
                  "horizontal_stretch": 1.0, "vertical_stretch": 2.4},
    "screen": {"parabola_A": 12.7, "parabola_B": 0.04186,
               "altitude_min_deg": -48.6, "altitude_max_deg": 25.3,
               "azimuth_max_deg": 105, "height_cm": 20},
}

GEO = copy.deepcopy(DEFAULT_GEO)   # full geometry dict (loaded from YAML, written back)
PARAMS = {"parabola_A": 12.7, "parabola_B": 0.04186, "throw_ratio": 0.8,
          "horizontal_stretch": 1.0, "vertical_stretch": 2.4,
          "optical_axis_elevation_deg": 49.0, "lens_offset_vertical": 0.0,
          "eye_above_screen_bottom_cm": 14.4,   # driver: sets altitude_min = atan(-h / parabola_A)
          "offset_x": 0, "offset_y": 186, "flip_h": False, "flip_v": True,
          "frame": 2}   # outer-frame thickness: 0-100, each step = 1% (top/bot of H, left/right of W)
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
    # Seed eye-height-above-screen-bottom from the loaded altitude_min; from here the user
    # edits h and it DRIVES altitude_min (see _apply_eye_height).
    try:
        PARAMS["eye_above_screen_bottom_cm"] = round(
            float(-PARAMS["parabola_A"] * np.tan(np.radians(GEO["screen"]["altitude_min_deg"]))), 1)
    except Exception:
        pass
    if PREVIEW_PATH.exists():
        try:
            pv = json.loads(PREVIEW_PATH.read_text())
            for k in PREVIEW_KEYS:
                if k in pv:
                    PARAMS[k] = pv[k]
        except Exception:
            pass


def _apply_eye_height():
    """eye_above_screen_bottom_cm drives altitude_min: the screen bottom (center) sits at
    z = -h below the eye, so altitude_min = atan(-h / parabola_A). Call under LOCK after
    h or parabola_A changes."""
    h = PARAMS.get("eye_above_screen_bottom_cm")
    A = PARAMS.get("parabola_A")
    if h is not None and A:
        GEO.setdefault("screen", {})["altitude_min_deg"] = round(
            float(np.degrees(np.arctan2(-h, A))), 2)


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
    """Merge slider values into GEO, overwrite rig_geometry.yaml; preview -> sidecar.
    Also POSTs the YAML back to the Mac setup UI so it lands (timestamped) in the repo.
    Returns the archived filename on the Mac, or None (local save always succeeds)."""
    with LOCK:
        for slider, (sec, key) in GEO_SLIDERS.items():
            GEO.setdefault(sec, {})[key] = PARAMS[slider]
        geo_out = copy.deepcopy(GEO)
        preview = {k: PARAMS[k] for k in PREVIEW_KEYS}
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
        if r is None or not (np.isfinite(r[0]) and np.isfinite(r[1])):
            return None
        return (int(r[0]), int(r[1]))

    surf = pygame.Surface((W, H))
    # All colors are B/G only (R=0) — the projector's red LED is deactivated, so any red
    # channel simply won't show. Eye line (alt 0 = horizon) and the straight-ahead meridian
    # (az 0) are drawn thick and distinct.
    surf.fill((0, 18, 18))                                      # R-free dark background
    a0 = int(np.floor(almin / 10) * 10)
    for alt_t in range(a0, int(almax) + 1, 10):                 # altitude isolines
        pts = [q for q in (proj_pt(az, alt_t) for az in np.linspace(-90, 90, 80)) if q]
        if len(pts) >= 2:
            if alt_t == 0:                                      # EYE LINE — animal eye level / horizon
                pygame.draw.lines(surf, (0, 255, 0), False, pts, 5)
            else:
                pygame.draw.lines(surf, (0, 190, 190), False, pts, 2)
    for az_t in range(-80, 81, 20):                             # azimuth isolines
        pts = [q for q in (proj_pt(az_t, alt) for alt in np.linspace(almin, almax, 80)) if q]
        if len(pts) >= 2:
            if az_t == 0:                                       # straight-ahead meridian (thick)
                pygame.draw.lines(surf, (0, 200, 255), False, pts, 5)
            else:
                pygame.draw.lines(surf, (0, 110, 235), False, pts, 2)
    # orientation labels — B/G only (red LED off)
    font = pygame.font.SysFont(None, 64)
    almid = (almin + almax) / 2
    for text, az, alt, col in [(f"TOP {almax:.0f}", 0, almax, (0, 255, 130)),
                               (f"BOTTOM {almin:.0f}", 0, almin, (0, 255, 130)),
                               ("LEFT -80", -80, almid, (0, 190, 255)),
                               ("RIGHT +80", 80, almid, (0, 190, 255))]:
        q = proj_pt(az, alt)
        if q:
            s = font.render(text, True, col)
            surf.blit(s, s.get_rect(center=q))
    # azimuth angle of each vertical line, written along the eye line (altitude 0)
    afont = pygame.font.SysFont(None, 40)
    for az_t in range(-80, 81, 20):
        q = proj_pt(az_t, 0)
        if q:
            s = afont.render(f"{az_t}", True, (0, 220, 255))
            surf.blit(s, s.get_rect(center=(q[0], q[1] - 16)))   # just above the eye line
    return surf


def grid_lines(p):
    """Az/alt isolines in projector pixels for the CURRENT params — data for the web
    preview (mirrors render()'s projection but returns polylines, not a pygame surface)."""
    geo = copy.deepcopy(GEO)
    for slider, (sec, key) in GEO_SLIDERS.items():
        geo.setdefault(sec, {})[key] = p[slider]
    A, B = p["parabola_A"], p["parabola_B"]
    almin = geo["screen"]["altitude_min_deg"]
    almax = geo["screen"]["altitude_max_deg"]
    out = {"res": list(geo["projector"]["resolution"]),
           "almin": almin, "almax": almax, "azmin": -90, "azmax": 90,
           "alt_lines": [], "az_lines": []}
    try:
        proj = build_projector(geo)
    except Exception:
        return out

    def pp(az, alt):
        pt = ray_screen_intersection(az, alt, A, B)
        if pt is None:
            return None
        r = screen_point_to_pixel(pt, proj)
        if r is None or not (np.isfinite(r[0]) and np.isfinite(r[1])):
            return None
        return [float(r[0]), float(r[1])]

    a0 = int(np.floor(almin / 10) * 10)
    for alt_t in range(a0, int(almax) + 1, 10):
        pts = [q for q in (pp(az, alt_t) for az in np.linspace(-90, 90, 60)) if q]
        if len(pts) >= 2:
            out["alt_lines"].append({"t": alt_t, "pts": pts})
    for az_t in range(-80, 81, 20):
        pts = [q for q in (pp(az_t, alt) for alt in np.linspace(almin, almax, 60)) if q]
        if len(pts) >= 2:
            out["az_lines"].append({"t": az_t, "pts": pts})
    return out


PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Geo calibration</title><style>
body{font-family:sans-serif;background:#1b1b1b;color:#eee;padding:18px;font-size:15px}
.row{margin:14px 0}label{display:inline-block;width:170px}
input[type=range]{width:44%;vertical-align:middle}output{display:inline-block;width:70px;text-align:right;color:#7cf}
button{padding:9px 18px;margin:12px 8px 0 0;font-size:15px}#msg{margin-left:10px;color:#8f8}
.k{color:#fd6}.note{color:#888;font-size:13px}
#previews{display:flex;gap:18px;margin:8px 0 20px;max-width:1000px}
.pv{flex:1}.pv h4{margin:0 0 5px;font-weight:normal;color:#bbb;font-size:13px}
canvas{width:100%;height:auto;background:#111;border:1px solid #333;border-radius:4px}</style></head><body>
<h3>Geometry calibration (live)</h3>
<p>Tune <span class=k>parabola_B</span> until the cyan horizontal lines are straight/level.
Save overwrites <span class=k>rig_geometry.yaml</span> and archives a timestamped copy on the Mac.</p>
<div class=row><label>Eye above screen bottom</label>
<input type=number id=eyeh step=0.1 style="width:80px" onchange="set('eye_above_screen_bottom_cm',this.value,0)"> cm
<span class=note id=eyenote></span></div>
<div id=previews>
  <div class=pv><h4>TARGET — flat/undistorted (what the mouse should see)</h4><canvas id=cvIdeal width=480 height=270></canvas></div>
  <div class=pv><h4>PROJECTED — warped for the curved screen (what goes IRL)</h4><canvas id=cvWarp width=480 height=270></canvas></div>
</div>
<div id=ctrls></div>
<button onclick=save()>Save rig_geometry.yaml</button>
<button onclick=saveDefaults()>Save defaults</button>
<button onclick=loadDefaults()>Load defaults</button><span id=msg></span>
<p class=note>flip/offset sliders only align this preview (saved to .calib_preview.json), not the warp.
Defaults live on the projector (.calib_defaults.json).</p>
<script>
const S=[['parabola_A',1,30,0.05],['parabola_B',0.001,0.30,0.001],
['throw_ratio',0.4,1.6,0.01],['horizontal_stretch',0.4,4.0,0.05],['vertical_stretch',0.5,4.0,0.05],
['optical_axis_elevation_deg',-30,60,0.5],['lens_offset_vertical',0,1,0.05],
['offset_x',-1000,1000,2],['offset_y',-1000,1000,2],['frame',0,100,1]];
const T=['flip_h','flip_v'];let P={};
const DEC={};for(const[k,,,s]of S)DEC[k]=Math.max(0,-Math.floor(Math.log10(s)+1e-9));
function fmt(k,v){return (+v).toFixed(DEC[k]??2);}
function outLabel(k,v){if(k==='frame')return 'x='+Math.round(v*10.8)+'/1080, y='+Math.round(v*19.2)+'/1920';return fmt(k,v);}
async function load(){P=await(await fetch('/get')).json();draw();refreshPreview();}
function draw(){let h='';for(const[k,a,b,s]of S)h+=`<div class=row><label>${k}</label>
<input type=range min=${a} max=${b} step=${s} value=${P[k]} oninput="set('${k}',this.value,1)">
<output id=o_${k} style="${k==='frame'?'width:170px':''}">${outLabel(k,P[k])}</output></div>`;
for(const k of T)h+=`<div class=row><label>${k}</label>
<input type=checkbox ${P[k]?'checked':''} onchange="set('${k}',this.checked?1:0,0)"></div>`;
document.getElementById('ctrls').innerHTML=h;
document.getElementById('eyeh').value=(+P['eye_above_screen_bottom_cm']).toFixed(1);}
async function set(k,v,num){if(num)document.getElementById('o_'+k).textContent=outLabel(k,v);
await fetch('/set?k='+k+'&v='+v);schedulePreview();}
function flash(t){let m=document.getElementById('msg');m.textContent=t;setTimeout(()=>m.textContent='',2000);}
async function save(){let r=await(await fetch('/save')).json();
flash(r.archived?('saved ✓ → '+r.archived):'saved ✓ (Mac not reached — local only)');}
async function saveDefaults(){await fetch('/save_defaults');flash('defaults saved ✓');}
async function loadDefaults(){P=await(await fetch('/load_defaults')).json();draw();refreshPreview();flash('defaults loaded ✓');}
// ── side-by-side preview: left = ideal flat grid, right = the warp actually projected ──
let previewTimer=null;
function schedulePreview(){clearTimeout(previewTimer);previewTimer=setTimeout(refreshPreview,120);}
function col(t){return t===0?'#ff0':'#fff';}
function poly(ctx,pts,sx,sy){ctx.beginPath();pts.forEach((p,i)=>{const x=p[0]*sx,y=p[1]*sy;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();}
function drawIdeal(d){const c=document.getElementById('cvIdeal'),ctx=c.getContext('2d'),W=c.width,H=c.height;
ctx.fillStyle='#111';ctx.fillRect(0,0,W,H);
const X=a=>(a-d.azmin)/(d.azmax-d.azmin)*W,Y=a=>(1-(a-d.almin)/(d.almax-d.almin))*H;
for(const l of d.alt_lines){ctx.strokeStyle=l.t===0?'#0f0':'#00d2d2';ctx.lineWidth=l.t===0?3:1.5;ctx.beginPath();ctx.moveTo(0,Y(l.t));ctx.lineTo(W,Y(l.t));ctx.stroke();}
for(const l of d.az_lines){ctx.strokeStyle=l.t===0?'#0cf':col(l.t);ctx.lineWidth=l.t===0?3:1.5;ctx.beginPath();ctx.moveTo(X(l.t),0);ctx.lineTo(X(l.t),H);ctx.stroke();}}
function drawWarp(d){const c=document.getElementById('cvWarp'),ctx=c.getContext('2d'),W=c.width,H=c.height;
ctx.fillStyle='#111';ctx.fillRect(0,0,W,H);const sx=W/d.res[0],sy=H/d.res[1];
for(const l of d.alt_lines){ctx.strokeStyle=l.t===0?'#0f0':'#00d2d2';ctx.lineWidth=l.t===0?3:1.5;poly(ctx,l.pts,sx,sy);}
for(const l of d.az_lines){ctx.strokeStyle=l.t===0?'#0cf':col(l.t);ctx.lineWidth=l.t===0?3:1.5;poly(ctx,l.pts,sx,sy);}}
async function refreshPreview(){const d=await(await fetch('/preview')).json();drawIdeal(d);drawWarp(d);
const en=document.getElementById('eyenote');if(en)en.textContent='→ altitude_min = '+d.almin.toFixed(1)+'° (screen bottom)';}
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
            elif k in ("offset_x", "offset_y", "frame"):
                PARAMS[k] = int(float(v))
            elif k in PARAMS:
                PARAMS[k] = float(v)
            if k in ("eye_above_screen_bottom_cm", "parabola_A"):
                _apply_eye_height()          # re-derive altitude_min (screen-bottom position)
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
                        if k in ("flip_h", "flip_v"):
                            PARAMS[k] = bool(v)
                        elif k in ("offset_x", "offset_y"):
                            PARAMS[k] = int(v)
                        elif k in PARAMS:
                            PARAMS[k] = float(v)
                except Exception as e:
                    print("load_defaults failed:", e)
            _apply_eye_height()              # keep altitude_min consistent with restored h
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
        key = tuple(sorted(p.items()))
        if key != last_key:
            last_key = key
            grid = render(p, pygame, SW, SH)
            grid = pygame.transform.flip(grid, p["flip_h"], p["flip_v"])
            composed.fill((0, 0, 0))
            composed.blit(grid, (p["offset_x"], -p["offset_y"]))
        screen.blit(composed, (0, 0))
        # static edge frame — 'frame' slider (0-100), proportional: top/bottom bars are
        # frame% of height, left/right bars frame% of width (R-free blue; red LED off)
        fx = int(round(p["frame"] * SH / 100))   # top/bottom bar thickness (of 1080)
        fy = int(round(p["frame"] * SW / 100))   # left/right bar thickness (of 1920)
        if fx > 0:
            screen.fill((0, 0, 255), (0, 0, SW, fx)); screen.fill((0, 0, 255), (0, SH - fx, SW, fx))
        if fy > 0:
            screen.fill((0, 0, 255), (0, 0, fy, SH)); screen.fill((0, 0, 255), (SW - fy, 0, fy, SH))
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
