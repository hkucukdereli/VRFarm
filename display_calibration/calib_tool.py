"""
calib_tool.py — interactive display-fit calibration.

Shows the warp-grid (azimuth/altitude isolines from warp_map.npz) on the projector and
applies a LIVE affine transform you tune from a web slider panel in your browser. Since
the projector has no keystone and the parabola in the warp map already handles the screen
curvature, the remaining fit is a pure affine: independent H/V scale, X/Y offset, flips.

  scale_x : horizontal magnification   (≈ throw_ratio)
  scale_y : vertical magnification      (≈ vertical_stretch)
  offset_x: shift left/right (+ = right, physical)
  offset_y: shift up/down    (+ = up, physical)
  flip_h / flip_v : projector orientation

Run on mozzarella (projector X must be up):
  DISPLAY=:0 ~/miniforge3/envs/rig/bin/python calib_tool.py --warp ~/rig/calibration/warp_map.npz

Then on your Mac open:  http://192.168.10.102:5090
Drag sliders -> projector updates live. Click "Save" to write fit_params.json; those
numbers then get baked into rig_geometry.yaml / the warp.
SIGTERM / Q / ESC quit.
"""
import argparse
import json
import signal
import threading
from pathlib import Path

import numpy as np

PARAMS = {"scale_x": 1.0, "scale_y": 1.0, "offset_x": 0, "offset_y": 0,
          "flip_h": False, "flip_v": True}
LOCK = threading.Lock()
SAVE_PATH = Path.home() / "rig" / "calibration" / "fit_params.json"


def build_grid_surface(warp_path, pygame):
    """Render az/alt isolines from the warp map's inverse maps into a Surface."""
    d = np.load(str(warp_path))
    az, alt = d["az_map"], d["alt_map"]
    valid = d["valid_map"].astype(bool)
    H, W = az.shape

    def iso(field, t):
        s = np.sign(field - t)
        m = np.zeros(field.shape, dtype=bool)
        m[:, :-1] |= s[:, :-1] != s[:, 1:]
        m[:-1, :] |= s[:-1, :] != s[1:, :]
        return m & valid & np.isfinite(field)

    img = np.full((H, W, 3), 40, dtype=np.uint8)
    lo, hi = int(np.floor(np.nanmin(alt[valid]))), int(np.ceil(np.nanmax(alt[valid])))
    for a in range(lo - lo % 10, hi + 1, 10):
        img[iso(alt, a)] = (0, 200, 200)      # altitude isolines (cyan)
    for z in range(-100, 101, 20):
        img[iso(az, z)] = (255, 255, 255)     # azimuth isolines (white)
    img[iso(az, 0)] = (255, 255, 0)           # 0 az (yellow)
    return pygame.surfarray.make_surface(img.transpose(1, 0, 2)), W, H


PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Display fit</title><style>
body{font-family:sans-serif;background:#1c1c1c;color:#eee;padding:18px;font-size:15px}
.row{margin:16px 0}label{display:inline-block;width:90px}
input[type=range]{width:55%;vertical-align:middle}output{display:inline-block;width:64px;text-align:right;color:#7cf}
button{padding:9px 18px;margin-top:12px;font-size:15px}#msg{margin-left:10px;color:#8f8}
</style></head><body>
<h3>Display fit calibration</h3><div id=ctrls></div>
<button onclick=save()>Save fit_params.json</button><span id=msg></span>
<script>
const S=[['scale_x',0.3,2.5,0.005],['scale_y',0.3,4.0,0.005],
         ['offset_x',-900,900,1],['offset_y',-900,900,1]];
const T=['flip_h','flip_v'];let P={};
async function load(){P=await(await fetch('/get')).json();draw();}
function draw(){let h='';for(const[k,a,b,s]of S)h+=`<div class=row><label>${k}</label>
<input type=range min=${a} max=${b} step=${s} value=${P[k]} oninput="set('${k}',this.value,1)">
<output id=o_${k}>${(+P[k]).toFixed(s<1?2:0)}</output></div>`;
for(const k of T)h+=`<div class=row><label>${k}</label>
<input type=checkbox ${P[k]?'checked':''} onchange="set('${k}',this.checked?1:0,0)"></div>`;
document.getElementById('ctrls').innerHTML=h;}
async function set(k,v,num){if(num){document.getElementById('o_'+k).textContent=(+v).toFixed(v<10&&v>-10?2:0);}
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


def run(warp_path, port):
    import pygame
    # restore previously-saved fit so a restart keeps your tuning
    try:
        if SAVE_PATH.exists():
            saved = json.loads(SAVE_PATH.read_text())
            with LOCK:
                for k in list(PARAMS):
                    if k in saved:
                        PARAMS[k] = saved[k]
            print("loaded saved fit:", saved)
    except Exception as e:
        print("no saved fit:", e)
    threading.Thread(target=start_web, args=(port,), daemon=True).start()

    pygame.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    SW, SH = screen.get_size()
    grid, GW, GH = build_grid_surface(warp_path, pygame)

    running = {"v": True}
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__("v", False))

    clock = pygame.time.Clock()
    last_key = None
    composed = pygame.Surface((SW, SH))
    while running["v"]:
        with LOCK:
            p = dict(PARAMS)
        key = (p["scale_x"], p["scale_y"], p["offset_x"], p["offset_y"],
               p["flip_h"], p["flip_v"])
        if key != last_key:
            last_key = key
            flipped = pygame.transform.flip(grid, p["flip_h"], p["flip_v"])
            sw = max(1, min(8000, int(GW * p["scale_x"])))
            sh = max(1, min(8000, int(GH * p["scale_y"])))
            scaled = pygame.transform.smoothscale(flipped, (sw, sh))
            composed.fill((0, 0, 0))
            x = (SW - sw) // 2 + p["offset_x"]
            y = (SH - sh) // 2 - p["offset_y"]   # +offset_y = up (physical)
            composed.blit(scaled, (x, y))
        screen.blit(composed, (0, 0))
        pygame.display.flip()
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and
                                         e.key in (pygame.K_q, pygame.K_ESCAPE)):
                running["v"] = False
        clock.tick(20)
    pygame.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Interactive display-fit calibration")
    ap.add_argument("--warp", default=str(Path.home() / "rig" / "calibration" / "warp_map.npz"))
    ap.add_argument("--port", type=int, default=5090)
    a = ap.parse_args()
    run(a.warp, a.port)
