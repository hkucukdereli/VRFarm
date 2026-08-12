#!/usr/bin/env python3
"""
vsync_probe.py — is the follower's pygame display actually vsync-locked to the projector?

Run on the FOLLOWER, after start_projector.sh, in the rig conda env:

    source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig
    DISPLAY=:0 python3 ~/rig/calibration/vsync_probe.py             # the real (vsync-requested) path
    DISPLAY=:0 python3 ~/rig/calibration/vsync_probe.py --no-vsync  # baseline for comparison

Why this exists: devices/display.py sets self._vsync = True whenever set_mode(vsync=1) doesn't
RAISE — but SDL returning a surface does NOT prove vsync is active. On the FKMS/DPI + Xorg-
modesetting stack the request can succeed yet never lock, so the per-frame sync loop free-runs.
This probe replicates display.py's exact set_mode (FULLSCREEN|DOUBLEBUF|HWSURFACE, vsync=1) and
MEASURES the truth: real fps, whether flip() actually blocks on the refresh, and dropped frames.

Two independent tells:
  - flip() block time: with real vsync, pygame.display.flip() blocks until the next vblank
    (a few ms, up to one refresh period); free-running, it returns in microseconds.
  - frame Δt / fps: locked -> steady at the DPI refresh (~58 Hz here); free-running -> hundreds
    of fps and/or irregular (the bursty pattern the session .h5 hinted at).
"""

from __future__ import annotations
import argparse
import os
import statistics
import time

# Match display.py: keep SDL from stealing SIGINT/SIGTERM. Must precede pygame.init().
os.environ["SDL_HINT_NO_SIGNAL_HANDLERS"] = "1"


def _pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, int(round(p / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def _histogram(dt_ms, nominal_ms):
    """Coarse Δt histogram (in units of the nominal refresh period) to expose multimodality /
    bursts — a clean lock is one tight bar at ~1.0x; free-running/bursty spreads out."""
    edges = [0.0, 0.25, 0.5, 0.75, 1.25, 1.75, 2.5, 3.5, 1e9]
    labels = ["<0.25x", "0.25-0.5x", "0.5-0.75x", "0.75-1.25x", "1.25-1.75x",
              "1.75-2.5x", "2.5-3.5x", ">3.5x"]
    counts = [0] * len(labels)
    for d in dt_ms:
        r = d / nominal_ms
        for i in range(len(labels)):
            if edges[i] <= r < edges[i + 1]:
                counts[i] += 1
                break
    n = max(1, len(dt_ms))
    print("  Δt histogram (multiples of one refresh period; a clean lock = one bar at 0.75-1.25x):")
    for lab, c in zip(labels, counts):
        bar = "#" * int(round(40 * c / n))
        print(f"    {lab:>10}: {c:5d} {bar}")


def main():
    ap = argparse.ArgumentParser(description="Measure follower display vsync/fps/dropped frames.")
    ap.add_argument("--seconds", type=float, default=6.0, help="measurement duration (s)")
    ap.add_argument("--size", default="1920x1080", help="WxH; match the projector mode")
    ap.add_argument("--expect-hz", type=float, default=58.0,
                    help="nominal DPI refresh (config_dlp.txt hdmi_timings is 58 Hz)")
    ap.add_argument("--no-vsync", action="store_true",
                    help="request NO vsync (baseline: shows what free-running looks like)")
    args = ap.parse_args()
    w, h = (int(x) for x in args.size.lower().split("x"))

    import pygame
    pygame.init()
    flags = pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE
    vsync_req = 0 if args.no_vsync else 1
    set_mode_ok = None
    try:
        screen = pygame.display.set_mode((w, h), flags, vsync=vsync_req)
        set_mode_ok = True   # this is exactly what makes display.py believe _vsync = True
    except (pygame.error, TypeError) as e:
        screen = pygame.display.set_mode((w, h), flags)
        set_mode_ok = False
        print(f"set_mode(vsync={vsync_req}) fell back to no-vsync: {e}")
    pygame.mouse.set_visible(False)

    try:
        driver = pygame.display.get_driver()
    except Exception:
        driver = "?"
    try:
        drv_hz = pygame.display.get_current_refresh_rate()   # pygame 2.2+
    except Exception:
        drv_hz = None

    print("=" * 68)
    print(f"driver={driver}  vsync_requested={vsync_req}  set_mode_succeeded={set_mode_ok}  "
          f"driver_reports_hz={drv_hz}  size={w}x{h}")
    print(f"(display.py would set self._vsync = {set_mode_ok} here — the point is to check if that's true)")

    # Warm up: the first few flips after a modeset are atypical.
    for _ in range(12):
        screen.fill((0, 0, 0))
        pygame.display.flip()

    nominal = 1000.0 / args.expect_hz
    flip_ms = []   # how long flip() itself blocks -> the direct vsync tell
    dt_ms = []     # frame-to-frame loop period -> fps + drops
    t_prev = time.perf_counter()
    t_end = t_prev + args.seconds
    i = 0
    while time.perf_counter() < t_end:
        # Full-screen fill every frame (a realistic stimulus workload), alternating so each
        # present is genuinely different and can't be coalesced.
        screen.fill((0, 0, 0) if (i & 1) else (0, 24, 24))
        t0 = time.perf_counter()
        pygame.display.flip()
        t1 = time.perf_counter()
        flip_ms.append((t1 - t0) * 1e3)
        dt_ms.append((t1 - t_prev) * 1e3)
        t_prev = t1
        i += 1
        pygame.event.pump()
    pygame.quit()

    n = len(dt_ms)
    if n == 0:
        print("no frames measured"); return
    elapsed = sum(dt_ms) / 1e3
    fps = n / elapsed if elapsed else 0.0
    dt_sorted = sorted(dt_ms)
    flip_sorted = sorted(flip_ms)
    med_flip = statistics.median(flip_ms)
    drops = sum(1 for d in dt_ms if d > 1.5 * nominal)

    print("-" * 68)
    print(f"frames={n}  elapsed={elapsed:.2f}s  MEASURED fps={fps:.1f}   (expected ~{args.expect_hz:.0f} if locked)")
    print(f"frame Δt ms : median={statistics.median(dt_ms):6.2f}  mean={statistics.mean(dt_ms):6.2f}  "
          f"min={min(dt_ms):6.2f}  max={max(dt_ms):7.2f}  p99={_pct(dt_sorted,99):6.2f}  "
          f"std={statistics.pstdev(dt_ms):5.2f}")
    print(f"flip() ms   : median={med_flip:6.3f}  mean={statistics.mean(flip_ms):6.3f}  "
          f"max={max(flip_ms):7.2f}   (locked -> blocks a few ms up to ~{nominal:.1f}; free -> ~0)")
    print(f"dropped     : {drops}/{n} frames Δt > 1.5x refresh ({100.0*drops/n:.1f}%)")
    _histogram(dt_ms, nominal)

    # Verdict from the two independent signals.
    flip_blocks = med_flip > 0.30 * nominal
    fps_near = abs(fps - args.expect_hz) < 0.20 * args.expect_hz
    print("-" * 68)
    if flip_blocks and fps_near:
        verdict = f"VSYNC-LOCKED at ~{fps:.0f} Hz (flip() blocks, fps == refresh)."
    elif (not flip_blocks) and fps > 1.6 * args.expect_hz:
        verdict = (f"FREE-RUNNING at ~{fps:.0f} fps — flip() does NOT block, fps >> refresh. "
                   f"display.py's _vsync={set_mode_ok} is WRONG; the per-frame sync loop is unpaced.")
    else:
        verdict = (f"IRREGULAR ~{fps:.0f} fps — neither cleanly locked nor free (see Δt spread / "
                   f"histogram). flip_blocks={flip_blocks}, fps_near_refresh={fps_near}.")
    print("VERDICT:", verdict)
    print("=" * 68)


if __name__ == "__main__":
    main()
