"""
panel_grid.py

Projects a plain NUMBERED grid over the ENTIRE projector panel (NOT warped — this is
raw framebuffer pixels) so you can read off, with a ruler on the physical screen, which
gridlines fall where. That gives a direct panel-pixel <-> physical-screen-position
correspondence, from which the projector geometry can be solved backwards.

Numbering: columns 0,1,2,... left->right across the top; rows 0,1,2,... top->bottom down
the left. Numbered lines every `--spacing` px (default 100; bold every 5th). The whole
frame is flipped as one unit (--flip-v on by default to cancel the projector's vertical
inversion) so the numbers read upright with 0 at the physical top-left.

Run on mozzarella:
  DISPLAY=:0 ~/miniforge3/envs/rig/bin/python panel_grid.py [--spacing 100] [--flip-h]

Headless-friendly: --duration N self-exits; SIGTERM/Q/ESC quit.
"""
import argparse
import signal
import time


def run(spacing, flip_h, flip_v, duration):
    import pygame
    pygame.init()
    pygame.font.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    W, H = screen.get_size()
    big = pygame.font.SysFont(None, 70)   # large numbers, readable from the rig

    frame = pygame.Surface((W, H))
    frame.fill((0, 0, 0))

    # thin lines every `spacing` (gray); BOLD + big-numbered every 5th.
    # Labels are REPEATED at several positions along each bold line so they stay
    # readable even when the panel edges overflow off the physical screen.
    ncol = W // spacing
    nrow = H // spacing
    y_positions = [int(f * H) for f in (0.04, 0.27, 0.50, 0.73, 0.93)]
    x_positions = [int(f * W) for f in (0.02, 0.27, 0.50, 0.73, 0.95)]
    for i in range(ncol + 1):
        x = i * spacing
        bold = (i % 5 == 0)
        pygame.draw.line(frame, (255, 255, 0) if bold else (70, 70, 70),
                         (x, 0), (x, H), 4 if bold else 1)
        if bold:
            lbl = big.render(str(i), True, (255, 255, 0))
            for yy in y_positions:
                frame.blit(lbl, (x + 5, yy))
    for j in range(nrow + 1):
        y = j * spacing
        bold = (j % 5 == 0)
        pygame.draw.line(frame, (0, 220, 220) if bold else (70, 70, 70),
                         (0, y), (W, y), 4 if bold else 1)
        if bold:
            lbl = big.render(str(j), True, (0, 220, 220))
            for xx in x_positions:
                frame.blit(lbl, (xx, y + 5))

    # mark the panel center for reference
    pygame.draw.circle(frame, (255, 0, 255), (W // 2, H // 2), 10)

    running = {"v": True}
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__("v", False))
    out = pygame.transform.flip(frame, flip_h, flip_v)

    clock = pygame.time.Clock()
    t0 = time.time()
    while running["v"]:
        screen.blit(out, (0, 0))
        pygame.display.flip()
        if duration and time.time() - t0 >= duration:
            break
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and
                                         e.key in (pygame.K_q, pygame.K_ESCAPE)):
                running["v"] = False
        clock.tick(10)
    pygame.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Numbered panel grid (raw framebuffer)")
    ap.add_argument("--spacing", type=int, default=100, help="px between numbered lines")
    ap.add_argument("--flip-h", action="store_true")
    ap.add_argument("--flip-v", dest="flip_v", action="store_true", default=True)
    ap.add_argument("--no-flip-v", dest="flip_v", action="store_false")
    ap.add_argument("--duration", type=float, default=0)
    a = ap.parse_args()
    run(a.spacing, a.flip_h, a.flip_v, a.duration)
