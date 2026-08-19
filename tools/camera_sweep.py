#!/usr/bin/env python3
"""
tools/camera_sweep.py

Feasibility sweep for the camera mode / bit-depth / resolution / fps matrix. The sensor CEILING
(SENSOR_MODES in devices/camera.py) says a combo is POSSIBLE; only a real recording says the
Pi 5's SOFTWARE H.264 encoder can SUSTAIN it. This records a short clip of each combo and reports
whether it holds the target fps, so you never discover a dropping combo mid-session.

It drives the production devices.camera.Camera path — including the sensor pin and its
verification — so a pinning bug shows up here, not in a session.


WHEN TO RUN IT
--------------
Re-run any time the answer might have changed:
  * after changing the camera, the lens, or picamera2 / the OS
  * after adding heat load to the rig (enclosure, more devices) — thermal state affects sustain
  * before trusting a NEW (mode, output, fps) combo in a real cohort
  * to re-confirm the encoder ceilings baked into the setup UI (ENC_MAX_FPS) still hold


PREREQUISITES
-------------
  * Run ON THE LEADER PI (needs picamera2), in the `rig` conda env.
  * The mode-pinning devices/camera.py must be deployed to ~/rig/ (Install/Deploy does this).
  * NO session running — the script probes /api/status and refuses if one is.


USAGE
-----
From your workstation over SSH:

    ssh vruser@192.168.10.101 \\
      'source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig && \\
       python ~/rig/tools/camera_sweep.py --secs 8 --fps 30 50 60'

Or in a shell on the Pi:

    source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig
    python ~/rig/tools/camera_sweep.py --secs 8 --fps 30 50 60


EXAMPLES
--------
  # Full matrix at the usual fps points (~30 combos, ~6 min at --secs 8):
  python ~/rig/tools/camera_sweep.py --secs 8 --fps 30 50 60

  # Find the exact edge where a combo starts dropping — bracket it with fps points.
  # (This is how the native-output caps were found: 2028x1520 native is clean at 40, drops at 45.)
  python ~/rig/tools/camera_sweep.py --secs 8 --fps 40 45 50

  # Longer clips for a steadier CPU/fps reading (e.g. if numbers look noisy):
  python ~/rig/tools/camera_sweep.py --secs 20 --fps 50 60

  # Match your real exposure/gain so the CPU numbers reflect production:
  python ~/rig/tools/camera_sweep.py --secs 8 --fps 60 --gain 2 --exposure-ms 10

  # Keep the JSON somewhere you'll find it (also printed as a table):
  python ~/rig/tools/camera_sweep.py --out ~/sweep_$(date +%F).json


READING THE OUTPUT
------------------
      mode bit     output  req | got fps drop  cpu/4   Mbit  GB/hr pin
(2028, 1520)  8  (1014, 760)  60 |   60.04    0    20%   4.00   1.80  OK   <- feasible
(2028, 1520)  8  (2028, 1520)  50 |   39.87   79    85%   3.18   1.43  OK   <- encoder can't keep up

  req      the fps requested for this combo (skipped when above the mode's sensor ceiling)
  got fps  what the sensor timestamps actually delivered. got ~= req AND drop 0  => feasible.
           got << req with drop > 0 => the encoder saturated and dropped frames (a native /
           high-fps case). Half (downscaled) outputs stay feasible to the sensor ceiling.
  drop     frames the encoder dropped over the clip. Anything > 0 = not sustainable at this fps.
  cpu/4    whole-process CPU as a % of ALL 4 cores. ~>=80% = the software encoder is saturating.
  Mbit     real bitrate of the test clip. NOTE: the sweep always records at 4 Mbit/s just to
  GB/hr    measure fps/CPU/drops — these two columns are the TEST bitrate, NOT your session
           bitrate. File size scales linearly with whatever bitrate_mbps you actually set.
  pin      OK  = the sensor mode + bit depth were actually applied (the pin was honoured).
           BAD = the pin silently failed — investigate before trusting that combo.

Full results are also written to --out as JSON (one object per combo) for later analysis.


SAFETY
------
Writes only to /tmp, deletes each clip after measuring, releases the camera between combos, and
touches nothing in the rig config or session data. Aborts immediately if a session is recording.


A SINGLE COMBO, BY HAND
-----------------------
For a one-off check the sweep's --fps covers almost everything, but the minimal direct form is:

    import sys, time, numpy as np; sys.path.insert(0, '/home/vruser/rig')
    from devices.camera import Camera
    c = Camera(); c.init({"sensor_mode":[2028,1520], "bit_depth":8, "resolution":[1014,760],
                          "fps":60, "bitrate_mbps":6, "h264_profile":"main", "gop_s":5.0,
                          "auto_exposure":False, "exposure_ms":10, "gain":2}, {})
    c.start_recording("t", "/tmp/t"); time.sleep(8); print(c.stop_recording())
    ts = np.load("/tmp/t/t/frame_timestamps.npy"); s = ts[:,2]/1e9
    print("fps", (len(ts)-1)/(s[-1]-s[0]))
"""

from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # ~/rig
import numpy as np
from devices.camera import Camera, SENSOR_MODES


def session_active(api="http://127.0.0.1:5080/api/status") -> bool:
    try:
        with urllib.request.urlopen(api, timeout=2) as r:
            s = json.loads(r.read().decode())
        return bool(s.get("process_running") or s.get("camera_recording"))
    except Exception:
        return False   # API down => assume safe to test locally


def measure(mode, bit_depth, out, fps, secs, gain, exposure_ms):
    cfg = {"sensor_mode": list(mode), "bit_depth": bit_depth,
           "resolution": list(out), "fps": fps, "bitrate_mbps": 4,
           "h264_profile": "main", "gop_s": 5.0,
           "auto_exposure": False, "exposure_ms": exposure_ms, "gain": gain}
    c = Camera()
    c.init(cfg, {})
    out_dir = "/tmp/cam_sweep"
    shutil.rmtree(out_dir, ignore_errors=True)
    t_cpu0 = sum(os.times()[:2]); t0 = time.time()
    try:
        c.start_recording("s", out_dir)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    time.sleep(secs)
    res = c.stop_recording()
    cpu = (sum(os.times()[:2]) - t_cpu0) / max(time.time() - t0, 1e-6)
    geom = getattr(c, "_geometry", {}) or {}
    ts_path = f"{out_dir}/s/frame_timestamps.npy"
    vid_path = f"{out_dir}/s/video.h264"
    achieved = drops = mbps = None
    if os.path.exists(ts_path):
        ts = np.load(ts_path)
        sens = ts[:, 2] / 1e9
        good = sens > 0
        if good.sum() > 5:
            s = sens[good]; dur = s[-1] - s[0]
            achieved = (good.sum() - 1) / dur if dur > 0 else None
            d = np.diff(s); med = float(np.median(d))
            drops = int(np.sum(np.round(d / med) - 1)) if med > 0 else None
            if os.path.exists(vid_path) and dur > 0:
                mbps = os.path.getsize(vid_path) * 8 / dur / 1e6
    shutil.rmtree(out_dir, ignore_errors=True)
    return {"ok": True, "achieved_fps": achieved, "drops": drops, "cpu_cores": cpu,
            "mbps": mbps, "gb_per_hr": (mbps * 3600 / 8000) if mbps else None,
            "pinned_mode": geom.get("sensor_mode_output_size"),
            "pinned_depth": geom.get("sensor_mode_bit_depth")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=12, help="record seconds per combo")
    ap.add_argument("--gain", type=float, default=2.0)
    ap.add_argument("--exposure-ms", type=float, default=10.0)
    ap.add_argument("--fps", type=int, nargs="*", default=[30, 50, 60],
                    help="fps points to test (skipped when above a mode's ceiling)")
    ap.add_argument("--out", default="/tmp/camera_sweep_results.json")
    args = ap.parse_args()

    if session_active():
        print("A session is recording — refusing to run. Stop it first.", flush=True)
        sys.exit(1)

    rows = []
    print(f"{'mode':>10} {'bit':>3} {'output':>10} {'req':>4} | "
          f"{'got fps':>7} {'drop':>4} {'cpu/4':>6} {'Mbit':>6} {'GB/hr':>6} {'pin':>3}", flush=True)
    print("-" * 78, flush=True)
    for mode, info in SENSOR_MODES.items():
        for bit in (8, 10, 12):
            ceil = info["max_fps"][bit]
            for out in info["outputs"]:
                for fps in args.fps:
                    if fps > ceil:
                        continue
                    r = measure(mode, bit, out, fps, args.secs, args.gain, args.exposure_ms)
                    if not r.get("ok"):
                        print(f"{str(mode):>10} {bit:>3} {str(out):>10} {fps:>4} | FAILED: {r.get('error')}",
                              flush=True)
                        rows.append({"mode": mode, "bit": bit, "out": out, "fps": fps, **r})
                        continue
                    af = r["achieved_fps"] or 0
                    pin_ok = (tuple(r.get("pinned_mode") or ()) == tuple(mode)
                              and r.get("pinned_depth") == bit)
                    # % of the 4-core Pi for context (cpu_cores is fraction of ONE core here)
                    print(f"{str(mode):>10} {bit:>3} {str(out):>10} {fps:>4} | "
                          f"{af:7.2f} {str(r['drops']):>4} {r['cpu_cores']*100/4:5.0f}% "
                          f"{(r['mbps'] or 0):6.2f} {(r['gb_per_hr'] or 0):6.2f} "
                          f"{'OK' if pin_ok else 'BAD':>3}", flush=True)
                    rows.append({"mode": mode, "bit": bit, "out": out, "fps": fps, **r})
    Path(args.out).write_text(json.dumps(rows, indent=1, default=str))
    print(f"\n[{len(rows)} combos -> {args.out}]", flush=True)


if __name__ == "__main__":
    main()
