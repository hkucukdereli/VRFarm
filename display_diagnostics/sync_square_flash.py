"""
display_diagnostics/sync_square_flash.py  (pygame)

Standalone photodiode bring-up test. Runs on mozzarella (the FOLLOWER / display Pi).

Mirrors devices/display.py so the scope sees what the real task produces: same pygame
fullscreen setup (FULLSCREEN|DOUBLEBUF|HWSURFACE, vsync) and the same 40x40 RED sync
square at bottom-center that display._draw_sync_square() draws. No UDP, no engine, no
devices framework, writes no data — it just drives the projector so you can tape the
photodiode over a patch and read the edges on an oscilloscope.

Sync-square layout:
  default      : one 40x40 red square at bottom-center (matches the real rig exactly).
  --row N      : N evenly-spaced red squares edge-to-edge along the bottom, all pulsing
                 together, numbered on screen. Sweep the diode along the row to choose a
                 mounting spot and check DLP brightness/color-wheel uniformity across X.

Timing modes:
  steady (default): patch(es) RED for the whole stim, background-gray during the blank
                    -> one rising edge at onset, one falling edge at offset.
  pulse (--pulse N): replicates the real per-frame sync (follower._show_synced):
                    vsync-locked, patch ON every Nth frame during the stim.

Run on mozzarella (projector X must be up — see start_projector.sh), or use run_sync_flash.sh:
  SDL_AUDIODRIVER=dummy DISPLAY=:0 ~/miniforge3/envs/rig/bin/python sync_square_flash.py
  ...sync_square_flash.py --row 7              # bottom-row position sweep
  ...sync_square_flash.py --row 7 --pulse 5    # sweep with the real per-frame cadence
  ...sync_square_flash.py --color white        # compare red vs white edge on the scope

Each stim ON/OFF edge prints a time.time() stamp. Ctrl-C / ESC / q quits.

Luminance note: the real patch is RED (255,0,0) on mid-gray (127,127,127). That is a
smaller total-flux change than it looks (red sums to 255 vs gray's 381), so a broadband
diode can read "on" as DIMMER, not brighter — and on a DLP the red channel is chopped by
the color wheel. If an edge looks weak/inverted, try --color white and compare. That
comparison is the point of this test.
"""

import argparse
import time


COLORS = {
    "red":   (255, 0, 0),
    "white": (255, 255, 255),
    "green": (0, 255, 0),
    "blue":  (0, 0, 255),
}


def gray_to_rgb(value: float) -> int:
    """PsychoPy-style [-1,1] gray -> 8-bit, matching devices/display.py."""
    lin = (value + 1) / 2
    return max(0, min(255, int(lin * 255)))


def build_sync_rects(pygame, w: int, h: int, size: int, row: int):
    """Return a list of (pygame.Rect, label) for the sync square(s).
    row<=0 -> single bottom-center square; row>=1 -> N squares edge-to-edge along the
    bottom, first flush-left, last flush-right (so corners are included)."""
    y = h - size
    if row <= 0:
        return [(pygame.Rect(w // 2 - size // 2, y, size, size), "")]
    if row == 1:
        return [(pygame.Rect(w // 2 - size // 2, y, size, size), "0")]
    rects = []
    for i in range(row):
        x = round(i * (w - size) / (row - 1))
        rects.append((pygame.Rect(x, y, size, size), str(i)))
    return rects


def main():
    ap = argparse.ArgumentParser(description="Flash red sync square(s) for photodiode testing (pygame).")
    ap.add_argument("--blank", type=float, default=10.0, help="blank duration per cycle (s)")
    ap.add_argument("--stim",  type=float, default=5.0,  help="stim duration per cycle (s)")
    ap.add_argument("--pulse", type=int, default=0,
                    help="0 = steady (on whole stim); N>0 = patch on every Nth frame")
    ap.add_argument("--row", type=int, default=0,
                    help="0 = single bottom-center square; N = row of N squares along the bottom")
    ap.add_argument("--color", default="red", choices=list(COLORS),
                    help="sync square ON color (default red, matching display.py)")
    ap.add_argument("--no-red", action="store_true",
                    help="render bg+stim from green+blue only (no red) so a red-filtered "
                         "diode sees ONLY the red sync square (mice are red-blind)")
    ap.add_argument("--sync-size", type=int, default=40, help="sync square size (px), matches display.py")
    ap.add_argument("--no-labels", action="store_true", help="don't draw index numbers in row mode")
    ap.add_argument("--stim-size", type=int, default=200, help="center stimulus square size (px)")
    ap.add_argument("--contrast", type=float, default=1.0, help="stim corrected contrast 0..1")
    ap.add_argument("--bg-gray", type=float, default=0.0, help="background gray -1..1 (0 = mid-gray)")
    ap.add_argument("--res", type=int, nargs=2, default=[1920, 1080], metavar=("W", "H"))
    ap.add_argument("--refresh", type=float, default=60.0, help="refresh Hz (frame cap when vsync absent)")
    ap.add_argument("--cycles", type=int, default=0, help="stop after N blank+stim cycles (0 = until quit)")
    args = ap.parse_args()

    import pygame

    pygame.init()
    flags = pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE
    try:
        screen = pygame.display.set_mode(tuple(args.res), flags, vsync=1)
        vsync = True
    except (pygame.error, TypeError):
        screen = pygame.display.set_mode(tuple(args.res), flags)
        vsync = False
    pygame.mouse.set_visible(False)

    w, h = args.res
    bg_rgb = gray_to_rgb(args.bg_gray)
    # stim square value, same formula as display.show_rect()
    bg_lin = (args.bg_gray + 1) / 2
    stim_lin = bg_lin + args.contrast * (1 - bg_lin)
    s = max(0, min(255, int(stim_lin * 255)))
    if args.no_red:
        # Green+blue only: the green channel carries the mouse-relevant luminance
        # (M-cone), red is left at 0 so a red-filtered diode sees only the sync square.
        bg = (0, bg_rgb, bg_rgb)
        stim_color = (0, s, s)
        label_rgb = (0, 255, 255)   # cyan labels — keep red off-screen except the patch
    else:
        bg = (bg_rgb, bg_rgb, bg_rgb)
        stim_color = (s, s, s)
        label_rgb = (255, 255, 255)
    sync_on = COLORS[args.color]

    S = args.sync_size
    sync_rects = build_sync_rects(pygame, w, h, S, args.row)
    ss = args.stim_size
    stim_rect = pygame.Rect(w // 2 - ss // 2, h // 2 - ss // 2, ss, ss)

    # Pre-render index labels (white, just above each square) for row mode
    labels = []
    if args.row > 0 and not args.no_labels:
        try:
            font = pygame.font.SysFont(None, 28)
            for rect, lab in sync_rects:
                surf = font.render(lab, True, label_rgb)
                labels.append((surf, (rect.centerx - surf.get_width() // 2,
                                      rect.top - surf.get_height() - 4)))
        except Exception as e:
            print(f"(labels disabled: {e})")

    mode = f"pulse every {args.pulse} frame(s)" if args.pulse > 0 else "steady (on whole stim)"
    layout = (f"row of {len(sync_rects)} squares along the bottom"
              if args.row > 0 else "single square bottom-center")
    print(f"window {w}x{h}  vsync={'on' if vsync else 'OFF (capped to %g Hz)' % args.refresh}")
    print(f"mode: {mode} | {args.blank:g}s blank / {args.stim:g}s stim")
    print(f"sync: {args.color} {S}px | {layout}")
    if args.no_red:
        print("no-red: bg+stim are green+blue only; red reserved for the sync square "
              "(use a RED-FILTERED photodiode)")
    for rect, lab in sync_rects:
        tag = f"#{lab} " if lab else ""
        print(f"   {tag}x_center={rect.centerx:>5}  topleft={rect.topleft}")
    print("Tape/sweep the diode over a square. ESC / q / Ctrl-C to quit.\n")

    period = args.blank + args.stim
    clock = pygame.time.Clock()
    t0 = time.time()
    prev_stim = None
    fi = 0          # frames since stim onset (for pulse mode)
    cycles = 0
    running = True

    try:
        while running:
            now = time.time()
            in_stim = ((now - t0) % period) >= args.blank

            if in_stim and args.pulse > 0:
                patch = (fi % args.pulse == 0)
            else:
                patch = in_stim

            screen.fill(bg)
            if in_stim:
                screen.fill(stim_color, stim_rect)
            color = sync_on if patch else bg
            for rect, _ in sync_rects:
                screen.fill(color, rect)
            for surf, pos in labels:
                screen.blit(surf, pos)
            pygame.display.flip()

            fi = fi + 1 if in_stim else 0

            if in_stim != prev_stim:
                print(f"{now:.6f}  stim {'ON ' if in_stim else 'OFF'}")
                if prev_stim is True and in_stim is False:
                    cycles += 1
                    if args.cycles and cycles >= args.cycles:
                        running = False
                prev_stim = in_stim

            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    running = False
                elif e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False

            clock.tick(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()


if __name__ == "__main__":
    main()
