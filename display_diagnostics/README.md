# display_diagnostics — projector / photodiode bring-up tools

Standalone optical diagnostics for the **follower / display Pi** (mozzarella). They drive
the projector directly so you can check the display→photodiode→scope path and DLP colour
before trusting the real sync pipeline. **pygame**, mirroring `devices/display.py`'s
fullscreen setup by copy — no UDP, no engine, no devices framework, they write no data.
These are kept (not scratch): re-run them on the next rig bring-up. Sibling of
`display_calibration/` (geometry/warp), which owns the calibration side.

The photodiode itself is a GPIO input on **cheddar** (leader, GPIO24) — `devices/photodiode.py`.
These tools only exercise the *optical* side (display → photodiode → your scope); tape the
diode over the sync patch.

## Tools

| File | What it does |
|------|--------------|
| `sync_square_flash.py` | Flashes the red sync square (matches `display._draw_sync_square`): steady or per-frame pulse, single bottom-centre patch or a bottom-row sweep, colour compare. Photodiode bring-up + mounting-spot sweep. |
| `dlp_color_test.py`    | Pushes full-frame / split solid colours (R/G/B/dark, left-right splits) — DLP colour-wheel / brightness-uniformity check. |
| `run_sync_flash.sh`    | scp `sync_square_flash.py` to mozzarella and run it on `DISPLAY=:0` (ensures the projector X server is up). Args pass straight through. |

## sync_square_flash.py

Cycles `[ 10 s blank ] → [ 5 s stim ] → repeat`. During the stim it draws a centre stimulus
square plus the red sync square; during the blank both are background gray. Every stim ON/OFF
edge prints a `time.time()` stamp so you can line the scope trace up to the software clock.

**Layout** — `--row N`: by default one red square at bottom-center (matches the real rig).
`--row N` instead draws N evenly-spaced red squares edge-to-edge along the bottom (first
flush-left, last flush-right, so the corners are included), numbered on screen and all pulsing
together. Sweep the diode along the row to choose a mounting spot and to check DLP brightness /
color-wheel uniformity across the screen width.

**Timing** — two modes:

| mode | flag | what the patch does |
|---|---|---|
| steady (default) | — | red for the whole 5 s stim → one rising + one falling edge per cycle |
| pulse | `--pulse N` | replicates the real per-frame sync (`follower._show_synced`): patch ON every Nth frame during the stim. At 60 Hz, `--pulse 5` ≈ a 12 Hz, 1-frame-on train |

### Run it

Projector X must be up (`start_projector.sh` on mozzarella; `run_sync_flash.sh` runs it if needed).

From the Mac:

```bash
cd display_diagnostics
./run_sync_flash.sh --row 7              # bottom-row sweep: 7 squares, find the diode spot
./run_sync_flash.sh --row 7 --pulse 5    # sweep with the real per-frame cadence
./run_sync_flash.sh                      # single bottom-center square (matches the real rig)
./run_sync_flash.sh --color white        # compare red vs white edge on the scope
./run_sync_flash.sh --blank 8 --stim 4 --cycles 5
```

Or directly on mozzarella:

```bash
SDL_AUDIODRIVER=dummy DISPLAY=:0 ~/miniforge3/envs/rig/bin/python sync_square_flash.py
```

`ESC` / `q` / `Ctrl-C` quits.

### Options (`sync_square_flash.py --help`)

`--blank` (10) · `--stim` (5) · `--pulse` (0=steady) · `--row` (0=single) · `--no-labels` ·
`--color` red/white/green/blue · `--sync-size` (40) · `--stim-size` (200) ·
`--contrast` (1.0) · `--bg-gray` (0.0) · `--res` (1920 1080) · `--refresh` (60) ·
`--cycles` (0=forever)

## dlp_color_test.py

Loops a pipe-separated colour sequence — each block is `SPEC,...:COUNT`, where SPEC is a full
colour (`red`) or a left/right split (`red/black`) and COUNT is `N` frames or `Ns` seconds.
Default: full R, G, B, dark (1 s each), then half-R, half-G, half-B. Use it to eyeball DLP
colour-wheel behaviour and brightness uniformity.

```bash
SDL_AUDIODRIVER=dummy DISPLAY=:0 ~/miniforge3/envs/rig/bin/python dlp_color_test.py
... dlp_color_test.py --seq "red:1s | red/black:1s"   # full then split
... dlp_color_test.py --cycles 20                     # stop after 20 loops
```

Headless projector, so to stop a backgrounded run: `pkill -9 -f 'dlp_color_test[.]py'`.

## Colour note (background)

The real sync square is **red** (`display._draw_sync_square` → `(255,0,0)`). A broadband
photodiode over a red patch on a mid-gray background sees a *smaller* flux change than it looks
(red sums to 255 vs gray's 381), and on a DLP the single red channel is chopped by the colour
wheel — so the edge can read weak or even inverted. **This was resolved:** `devices/display.py`
now draws the sync pulse **red-on-BLACK** (`display.py` `_show_synced` / the forced pre-warp
patch), which triggers reliably. To reproduce that here, run `sync_square_flash.py --bg-gray -1`
(black background). `--color white` is still handy to sanity-check the scope against a maximal edge.
