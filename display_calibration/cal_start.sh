#!/bin/bash
# cal_start.sh — start a display-calibration tool. Run ON the display Pi.
#   bash ~/rig/calibration/cal_start.sh           # geometry tool (default, web on :5091)
#   bash ~/rig/calibration/cal_start.sh geo       # live geometry sliders (parabola, stretch, offsets)
#   bash ~/rig/calibration/cal_start.sh panel     # raw numbered panel grid (no web UI)
#   bash ~/rig/calibration/cal_start.sh warp      # warp-grid validator (no web UI)
#
# KMS, no X. Two things follow from that and both are handled here rather than by the
# caller, so running this by hand over SSH behaves the same as the setup UI's button:
#
#   1. displayd owns DRM. It must step aside (POST :5581/standby) or the tool cannot get
#      a modeset — and if the supervisor respawned the renderer alongside the tool, two DRM
#      masters would black the projector. cal_stop.sh calls /resume to give it back.
#   2. SYSTEM python3 only. The conda `rig` env's SDL has no kmsdrm backend and would fall
#      back to a null driver: the tool would appear to run, print nothing wrong, and
#      display absolutely nothing.
TOOL="${1:-geo}"
PY=/usr/bin/python3
CAL=~/rig/calibration
DISPLAYD=http://127.0.0.1:5581

case "$TOOL" in
  geo)    SCRIPT=calib_geo.py;   ARGS="--port 5091";                              URL="http://192.168.10.102:5091" ;;
  panel)  SCRIPT=panel_grid.py;  ARGS="--spacing 100";                            URL="(no web UI — read the grid)" ;;
  warp)   SCRIPT=validate_calibration_pygame.py; ARGS="--warp $CAL/warp_map.npz --pattern grid"; URL="(no web UI)" ;;
  *) echo "unknown tool '$TOOL' (use: geo | panel | warp)"; exit 1 ;;
esac

# Stop anything already on the projector
pkill -f 'calib_geo.py|panel_grid.py|validate_calibration_pygame.py' 2>/dev/null
sleep 1

# Ask displayd to release the display. No daemon (curl fails / nothing listening) is fine:
# then nothing else holds DRM and the tool can take it directly.
if curl -s -m 3 "$DISPLAYD/status" | grep -q '"state"'; then
  echo "displayd is running — requesting standby (releasing DRM)..."
  if ! curl -s -m 20 -X POST "$DISPLAYD/standby" | grep -q '"state": *"STANDBY"'; then
    echo "FAILED: displayd would not go to standby — refusing to fight it for DRM master."
    exit 1
  fi
  echo "displayd in STANDBY."
fi

# Launch detached so it survives closing this SSH session.
# Redirect the WHOLE detached subtree's stdio (outside the bash -c) so it releases the
# SSH channel — otherwise `ssh ... cal_start.sh` never returns (hangs the caller/UI).
setsid bash -c "SDL_AUDIODRIVER=dummy $PY $CAL/$SCRIPT $ARGS" < /dev/null > /tmp/cal_$TOOL.log 2>&1 &
sleep 3
if pgrep -f "$SCRIPT" >/dev/null; then
  echo "Started '$TOOL' on the projector.  Control: $URL"
else
  echo "FAILED to start '$TOOL' — log:"; tail -6 /tmp/cal_$TOOL.log
  # Do not leave the rig dark because the tool refused to start.
  curl -s -m 30 -X POST "$DISPLAYD/resume" >/dev/null 2>&1
fi
echo "Stop with:  bash ~/rig/calibration/cal_stop.sh"
