#!/usr/bin/env bash
# Copy pulse_test.py to mozzarella (follower / display Pi) and run it on the projector.
# Extra args pass straight through to pulse_test.py, e.g.:
#   ./deploy.sh --pulse 5
#   ./deploy.sh --color white --blank 8 --stim 4
#
# Runs in the FOREGROUND so you see the ON/OFF edge timestamps live. ESC/q in the
# window or Ctrl-C here quits. Ensures the projector X server is up first.
set -euo pipefail

PI=vruser@192.168.10.102          # mozzarella = follower (display)
DIR=temp_diode
HERE="$(cd "$(dirname "$0")" && pwd)"

ssh "$PI" "mkdir -p ~/$DIR"
scp "$HERE/pulse_test.py" "$PI:~/$DIR/pulse_test.py"

ssh -t "$PI" "pgrep -x Xorg >/dev/null || bash ~/rig/start_projector.sh; \
  cd ~/$DIR && SDL_AUDIODRIVER=dummy DISPLAY=:0 \
  ~/miniforge3/envs/rig/bin/python pulse_test.py $*"
