"""
temp_diode/frame_sequence.py  (pygame)

Manually push a sequence of FULL-FRAME (or split) solid colors to the projector on
mozzarella (the FOLLOWER / display Pi). Standalone: no UDP, no engine, no devices
framework, writes no data. Same pygame fullscreen setup as devices/display.py /
pulse_test.py (FULLSCREEN|DOUBLEBUF|HWSURFACE, vsync=1).

Sequence is a pipe-separated list of blocks; each block is "SPEC,SPEC,...:COUNT".
Each SPEC is one of:
    color          -> fill the whole screen (e.g. red)
    colorL/colorR  -> LEFT half colorL, RIGHT half colorR (e.g. red/black)
Multiple comma-separated SPECs in a block cycle one-per-frame.
COUNT is either:
    N    -> hold for N display frames
    Ns   -> hold for N seconds of wall-clock time (e.g. 1s, 0.5s) -- rate-independent
The whole sequence loops until you quit.

Default sequence (what was asked for):
    full RED 1s, GREEN 1s, BLUE 1s, DARK 1s,
    then half-RED 1s, half-GREEN 1s, half-BLUE 1s   (color left half, dark right half)

Run on mozzarella (projector X must be up -- see start_projector.sh):
    SDL_AUDIODRIVER=dummy DISPLAY=:0 ~/miniforge3/envs/rig/bin/python frame_sequence.py
    ... frame_sequence.py --seq "red:1s | red/black:1s"   # full then split
    ... frame_sequence.py --seq "red,green,blue:20"       # cycle per frame (20 frames)
    ... frame_sequence.py --cycles 20                     # stop after 20 loops
    ... frame_sequence.py --verbose                       # timestamp every frame

Block onsets print a time.time() stamp (first 2 loops, then quiet). ESC / q / Ctrl-C
quits. The projector Pi is headless, so to stop a backgrounded run:
    pkill -9 -f 'frame_sequence[.]py'
"""

import argparse
import time

COLORS = {
    "red":     (255, 0, 0),
    "green":   (0, 255, 0),
    "blue":    (0, 0, 255),
    "white":   (255, 255, 255),
    "black":   (0, 0, 0),
    "dark":    (0, 0, 0),
    "cyan":    (0, 255, 255),
    "magenta": (255, 0, 255),
    "yellow":  (255, 255, 0),
    "gray":    (127, 127, 127),
}


def parse_seq(spec):
    """'red:1s | red/black:1s' -> [(['red'], 1.0, 's'), (['red/black'], 1.0, 's')].
    Count 'Ns' = hold N seconds (wall-clock); bare 'N' = hold N frames.
    A SPEC 'A/B' = left half A, right half B; a plain color fills the screen."""
    blocks = []
    for part in spec.split("|"):
        part = part.strip()
        if not part:
            continue
        specs_str, sep, count_str = part.rpartition(":")
        if not sep:
            raise SystemExit(f"bad block {part!r} (expected 'colors:N' or 'colors:Ns')")
        specs = [s.strip().lower() for s in specs_str.split(",") if s.strip()]
        for s in specs:
            for half in s.split("/"):
                if half not in COLORS:
                    raise SystemExit(f"unknown color {half!r} in {s!r}; known: {sorted(COLORS)}")
        count_str = count_str.strip().lower()
        if count_str.endswith("s"):
            unit, val = "s", float(count_str[:-1])
        else:
            unit, val = "f", int(count_str)
        if not specs or val <= 0:
            raise SystemExit(f"bad block {part!r}")
        blocks.append((specs, val, unit))
    if not blocks:
        raise SystemExit("empty sequence")
    return blocks


def draw_spec(screen, spec, w, h):
    """Render one SPEC: plain color fills the screen; 'A/B' splits left/right."""
    if "/" in spec:
        left, right = spec.split("/", 1)
        screen.fill(COLORS[left], (0, 0, w // 2, h))
        screen.fill(COLORS[right], (w // 2, 0, w - w // 2, h))
    else:
        screen.fill(COLORS[spec])


def main():
    ap = argparse.ArgumentParser(
        description="Push full-frame / split solid colors to the projector (per-frame or per-second holds).")
    ap.add_argument("--seq",
                    default=("red:1s | green:1s | blue:1s | black:1s | "
                             "red/black:1s | green/black:1s | blue/black:1s"),
                    help="pipe-separated blocks 'SPEC,...:N|Ns'; SPEC = color or colorL/colorR")
    ap.add_argument("--res", type=int, nargs=2, default=[1920, 1080], metavar=("W", "H"))
    ap.add_argument("--refresh", type=float, default=60.0,
                    help="frame cap Hz when vsync is unavailable")
    ap.add_argument("--cycles", type=int, default=0,
                    help="stop after N loops of the whole sequence (0 = until quit)")
    ap.add_argument("--verbose", action="store_true",
                    help="print a timestamp for EVERY frame (noisy)")
    args = ap.parse_args()

    blocks = parse_seq(args.seq)
    w, h = args.res

    import pygame
    pygame.init()
    flags = pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE
    try:
        screen = pygame.display.set_mode((w, h), flags, vsync=1)
        vsync = True
    except (pygame.error, TypeError):
        screen = pygame.display.set_mode((w, h), flags)
        vsync = False
    pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    print(f"window {w}x{h}  "
          f"vsync={'on' if vsync else 'OFF (capped to %g Hz)' % args.refresh}")
    for bi, (specs, val, unit) in enumerate(blocks):
        dur = f"{val:g} s" if unit == "s" else f"{int(val)} frames"
        print(f"  block {bi}: {dur}, {specs}")
    print(f"{'loop until quit' if args.cycles == 0 else str(args.cycles) + ' loops'}")
    print("ESC / q / Ctrl-C to quit  (headless projector -> pkill -9 -f 'frame_sequence[.]py')\n")

    running = True
    loops = 0
    try:
        while running:
            t_loop = time.time()
            nframes = 0
            for bi, (specs, val, unit) in enumerate(blocks):
                t_block = time.time()
                fi = 0
                while running:
                    spec = specs[fi % len(specs)]
                    draw_spec(screen, spec, w, h)
                    pygame.display.flip()          # vsync=1: blocks until the next refresh
                    if not vsync:
                        clock.tick(args.refresh)
                    if fi == 0 and loops < 2:
                        print(f"{time.time():.6f}  loop{loops} block{bi} start ({spec})")
                    if args.verbose:
                        print(f"{time.time():.6f}  loop{loops} block{bi} f{fi} {spec}")
                    fi += 1
                    nframes += 1
                    for e in pygame.event.get():
                        if e.type == pygame.QUIT:
                            running = False
                        elif e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_q):
                            running = False
                    if not running:
                        break
                    done = (time.time() - t_block >= val) if unit == "s" else (fi >= val)
                    if done:
                        break
                if not running:
                    break
            loops += 1
            dt = time.time() - t_loop
            if running and dt > 0 and loops <= 5:
                print(f"{time.time():.6f}  loop{loops - 1}: {nframes} frames in "
                      f"{dt * 1000:.0f} ms ({nframes / dt:.1f} fps)")
            if loops == 5 and not args.verbose and args.cycles == 0:
                print("  (steady; looping silently -- pkill -9 -f 'frame_sequence[.]py' to stop)")
            if args.cycles and loops >= args.cycles:
                running = False
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()
    print(f"\ndone: {loops} loop(s)")


if __name__ == "__main__":
    main()
