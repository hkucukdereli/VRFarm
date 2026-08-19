#!/usr/bin/env bash
# Copy sync_square_flash.py to mozzarella (follower / display Pi) and run it on the projector.
# Extra args pass straight through to sync_square_flash.py, e.g.:
#   ./run_sync_flash.sh --pulse 5
#   ./run_sync_flash.sh --color white --blank 8 --stim 4
#
# Runs in the FOREGROUND so you see the ON/OFF edge timestamps live. ESC/q in the
# window or Ctrl-C here quits. Ensures the projector X server is up first.
set -euo pipefail

PI=vruser@192.168.10.102          # mozzarella = follower (display)
DIR=display_diagnostics
HERE="$(cd "$(dirname "$0")" && pwd)"

ssh "$PI" "mkdir -p ~/$DIR"
scp "$HERE/sync_square_flash.py" "$PI:~/$DIR/sync_square_flash.py"

ssh -t "$PI" "pgrep -x Xorg >/dev/null || bash ~/rig/start_projector.sh; \
  cd ~/$DIR && SDL_AUDIODRIVER=dummy DISPLAY=:0 \
  ~/miniforge3/envs/rig/bin/python sync_square_flash.py $*"
