"""
validate_calibration_pygame.py

Pygame warp-map validator. (The original validate_calibration.py uses PsychoPy,
which isn't installed on the follower, and indexes the forward map with the inverse
valid_map — so it never ran.)

Draws azimuth/altitude isolines from the INVERSE maps (az_map / alt_map / valid_map,
all H×W = projector resolution — the same arrays display.py renders with) as
sign-change contours (clean 1-px lines, density-independent), plus orientation labels
and a big asymmetric "F".

PROJECTOR FLIP HANDLING: the whole composed frame (lines + text + F) is rendered to an
offscreen surface and flipped as ONE unit via --flip-h / --flip-v, so text flips
coherently with the image. Find the combination that makes the ENTIRE image read
correctly on the physical screen — that combination is the projector's transform, and
the same flip then gets baked into the warp generation so stimulus pixels land right.

Run on mozzarella (projector X must be up — see start_projector.sh):
  DISPLAY=:0 ~/miniforge3/envs/rig/bin/python validate_calibration_pygame.py \
      --warp ~/rig/calibration/warp_map.npz [--flip-h] [--flip-v]

Headless-friendly: --cycle SECONDS rotates patterns, --duration SECONDS self-exits.
Honors SPACE/Q/ESC if a keyboard is attached, and SIGTERM (kill over SSH).
"""
import argparse
import signal
import time
from pathlib import Path

import numpy as np

DEFAULT_WARP = Path(__file__).parent / "warp_map.npz"
PATTERNS = ["grid", "azimuths"]


def _isoline_mask(field, valid, target):
    """Pixels where `field` crosses `target` (sign change to a right/down neighbor)."""
    sign = np.sign(field - target)
    m = np.zeros(field.shape, dtype=bool)
    m[:, :-1] |= sign[:, :-1] != sign[:, 1:]
    m[:-1, :] |= sign[:-1, :] != sign[1:, :]
    return m & valid & np.isfinite(field)


def _build_grid(az_map, alt_map, valid):
    H, W = az_map.shape
    img = np.full((H, W, 3), 64, dtype=np.uint8)
    lo, hi = int(np.floor(np.nanmin(alt_map[valid]))), int(np.ceil(np.nanmax(alt_map[valid])))
    for alt_t in range(lo - lo % 10, hi + 1, 10):
        img[_isoline_mask(alt_map, valid, alt_t)] = (0, 200, 200)   # altitude: cyan
    for az_t in range(-100, 101, 20):
        img[_isoline_mask(az_map, valid, az_t)] = (255, 255, 255)   # azimuth: white
    img[_isoline_mask(az_map, valid, 0)] = (255, 255, 0)            # 0° azimuth: yellow
    return img


def _build_azimuths(az_map, alt_map, valid):
    H, W = az_map.shape
    img = np.full((H, W, 3), 64, dtype=np.uint8)
    colors = {-80: (255, 60, 60), -40: (255, 160, 40), 0: (255, 255, 255),
              40: (255, 160, 40), 80: (255, 60, 60)}
    for az_t, col in colors.items():
        img[_isoline_mask(az_map, valid, az_t)] = col
    return img


def _anchor(az_map, alt_map, ok, az_t, alt_t):
    """Pixel (x, y) whose mapped (az, alt) is nearest the target."""
    d = np.where(ok, (az_map - az_t) ** 2 + (alt_map - alt_t) ** 2, np.inf)
    y, x = np.unravel_index(int(np.argmin(d)), d.shape)
    return int(x), int(y)


def run(warp_path, pattern, cycle, duration, flip_h=False, flip_v=False, side_az=80, up_px=0):
    import pygame

    d = np.load(str(warp_path))
    az_map, alt_map = d["az_map"], d["alt_map"]
    valid = d["valid_map"].astype(bool)
    ok = valid & np.isfinite(az_map) & np.isfinite(alt_map)

    az_lo, az_hi = float(np.nanmin(az_map[ok])), float(np.nanmax(az_map[ok]))
    alt_lo, alt_hi = float(np.nanmin(alt_map[ok])), float(np.nanmax(alt_map[ok]))
    alt_mid = (alt_lo + alt_hi) / 2

    builders = {"grid": _build_grid, "azimuths": _build_azimuths}
    imgs = {name: fn(az_map, alt_map, valid) for name, fn in builders.items()}

    pygame.init()
    pygame.font.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    W, H = screen.get_size()
    font = pygame.font.SysFont(None, 30)
    big = pygame.font.SysFont(None, 60)
    huge = pygame.font.SysFont(None, 200)

    surfs = {name: pygame.surfarray.make_surface(img.transpose(1, 0, 2))
             for name, img in imgs.items()}

    def A(az_t, alt_t):
        return _anchor(az_map, alt_map, ok, az_t, alt_t)

    labels = []
    for az_t in (-80, -40, 0, 40, 80):
        if az_lo - 1 <= az_t <= az_hi + 1:
            labels.append((f"{az_t:+d}", A(az_t, alt_mid), (255, 255, 0), font))
    for alt_t in (-40, -20, 0, 20):
        if alt_lo - 1 <= alt_t <= alt_hi + 1:
            labels.append((f"alt{alt_t}", A(0, alt_t), (0, 230, 230), font))
    sa = min(side_az, abs(az_lo), az_hi)  # keep inside the valid field
    labels += [
        (f"LEFT -{sa:.0f}",  A(-sa, alt_mid), (255, 130, 130), big),
        (f"RIGHT +{sa:.0f}", A(+sa, alt_mid), (255, 130, 130), big),
        (f"TOP alt{alt_hi:.0f}",    A(0, alt_hi), (130, 255, 130), big),
        (f"BOTTOM alt{alt_lo:.0f}", A(0, alt_lo), (130, 255, 130), big),
    ]

    frame = pygame.Surface((W, H))
    pat = pattern if pattern in surfs else "grid"
    running = {"v": True}
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__("v", False))

    clock = pygame.time.Clock()
    t0 = last = time.time()
    while running["v"]:
        # compose the whole frame upright...
        frame.blit(surfs[pat], (0, 0))
        for text, (x, y), color, fnt in labels:
            s = fnt.render(text, True, color)
            frame.blit(s, s.get_rect(center=(x, y)))
        tag = f"SPACE: next   Q/ESC: quit   [{pat}]  flip_h={flip_h} flip_v={flip_v}"
        frame.blit(font.render(tag, True, (160, 160, 160)), (20, H - 32))
        # optional physical vertical shift (applied in upright space so it survives
        # the flip cancellation): positive up_px moves the physical image UP
        disp = frame
        if up_px:
            disp = pygame.Surface((W, H))
            disp.fill((0, 0, 0))
            disp.blit(frame, (0, -up_px))
        # ...then flip the WHOLE frame as one unit (text included)
        screen.blit(pygame.transform.flip(disp, flip_h, flip_v), (0, 0))
        pygame.display.flip()

        now = time.time()
        if cycle and now - last >= cycle:
            names = list(surfs)
            pat = names[(names.index(pat) + 1) % len(names)]
            last = now
        if duration and now - t0 >= duration:
            break

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running["v"] = False
            elif e.type == pygame.KEYDOWN:
                if e.key in (pygame.K_q, pygame.K_ESCAPE):
                    running["v"] = False
                elif e.key == pygame.K_SPACE:
                    names = list(surfs)
                    pat = names[(names.index(pat) + 1) % len(names)]
        clock.tick(20)

    pygame.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Validate warp calibration (pygame)")
    ap.add_argument("--warp", default=str(DEFAULT_WARP))
    ap.add_argument("--pattern", default="grid", choices=PATTERNS)
    ap.add_argument("--cycle", type=float, default=0)
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--flip-h", action="store_true", help="mirror left-right (whole frame)")
    ap.add_argument("--flip-v", action="store_true", help="flip upside-down (whole frame)")
    ap.add_argument("--side-az", type=float, default=80,
                    help="azimuth for the LEFT/RIGHT labels (default 80; 105 clips)")
    ap.add_argument("--up-px", type=int, default=0,
                    help="shift physical image up by N panel px (~48 px/cm; 2cm~=96)")
    a = ap.parse_args()
    run(a.warp, a.pattern, a.cycle, a.duration, a.flip_h, a.flip_v, a.side_az, a.up_px)
