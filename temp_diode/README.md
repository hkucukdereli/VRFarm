# temp_diode — photodiode bring-up (scratch)

Throwaway optical test to confirm the photodiode + oscilloscope before trusting the
sync path in a real session. **pygame**, mirrors `devices/display.py` exactly — same
fullscreen setup and the same 40×40 **red** sync square at **bottom-center**. No UDP,
no engine, no devices framework, writes no data. Delete this folder once the diode is
confirmed.

Runs on **mozzarella** (the follower / display Pi). The photodiode is on **cheddar**
(leader, GPIO24) in the real rig, but this test only exercises the *optical* side —
display → photodiode → your scope. Tape the diode over the bottom-center patch.

## What it does

Cycles `[ 10 s blank ] → [ 5 s stim ] → repeat`. During the stim it draws a center
stimulus square plus the red sync square; during the blank both are background gray.
Every stim ON/OFF edge prints a `time.time()` stamp so you can line the scope trace up
to the software clock.

**Layout** — `--row N`: by default one red square at bottom-center (matches the real
rig). `--row N` instead draws N evenly-spaced red squares edge-to-edge along the bottom
(first flush-left, last flush-right, so the corners are included), numbered on screen and
all pulsing together. Sweep the diode along the row to choose a mounting spot and to check
DLP brightness / color-wheel uniformity across the screen width.

**Timing** — two modes:

| mode | flag | what the patch does |
|---|---|---|
| steady (default) | — | red for the whole 5 s stim → one rising + one falling edge per cycle |
| pulse | `--pulse N` | replicates the real per-frame sync (`follower._show_synced`): patch ON every Nth frame during the stim. At 60 Hz, `--pulse 5` ≈ a 12 Hz, 1-frame-on train |

## Run it

Projector X must be up (`start_projector.sh` on mozzarella; deploy.sh runs it if needed).

From the Mac:

```bash
cd temp_diode
./deploy.sh --row 7              # bottom-row sweep: 7 squares, find the diode spot
./deploy.sh --row 7 --pulse 5    # sweep with the real per-frame cadence
./deploy.sh                      # single bottom-center square (matches the real rig)
./deploy.sh --color white        # compare red vs white edge on the scope
./deploy.sh --blank 8 --stim 4 --cycles 5
```

Or directly on mozzarella:

```bash
SDL_AUDIODRIVER=dummy DISPLAY=:0 ~/miniforge3/envs/rig/bin/python pulse_test.py
```

`ESC` / `q` / `Ctrl-C` quits.

## Options (`pulse_test.py --help`)

`--blank` (10) · `--stim` (5) · `--pulse` (0=steady) · `--row` (0=single) · `--no-labels` ·
`--color` red/white/green/blue · `--sync-size` (40) · `--stim-size` (200) ·
`--contrast` (1.0) · `--bg-gray` (0.0) · `--res` (1920 1080) · `--refresh` (60) ·
`--cycles` (0=forever)

## Color note (read before you measure)

The real sync square is **red** (`display._draw_sync_square` → `(255,0,0)`), drawn over
a mid-gray background `(127,127,127)`. That is a smaller total-flux change than it looks:
red sums to 255 vs gray's 381, so a **broadband photodiode can read the patch as dimmer,
not brighter**, when it turns on — and on a DLP the single red channel is chopped by the
color wheel. If the edge is weak, slow, or inverted on the scope, run `./deploy.sh
--color white` and compare. If white is clearly better, that's a reason to switch the
real `_draw_sync_square` to white (or to a black background behind the patch) — flag it
and we'll change `devices/display.py`.
