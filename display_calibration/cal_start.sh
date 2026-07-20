#!/bin/bash
# cal_start.sh — start a display-calibration tool. Run ON mozzarella.
#   bash ~/rig/calibration/cal_start.sh           # geometry tool (default, web on :5091)
#   bash ~/rig/calibration/cal_start.sh geo       # live geometry sliders (parabola, stretch, offsets)
#   bash ~/rig/calibration/cal_start.sh panel     # raw numbered panel grid (no web UI)
#   bash ~/rig/calibration/cal_start.sh warp      # warp-grid validator (no web UI)
TOOL="${1:-geo}"
PY=~/miniforge3/envs/rig/bin/python
CAL=~/rig/calibration

# Make sure the projector + X are up (needed after a reboot)
if ! pgrep -x Xorg >/dev/null; then
  echo "X not running — initializing projector (DLP + X)..."
  bash ~/rig/start_projector.sh
fi

# Stop anything already on the projector
pkill -f 'calib_geo.py|panel_grid.py|validate_calibration_pygame.py' 2>/dev/null
sleep 1

case "$TOOL" in
  geo)    SCRIPT=calib_geo.py;   ARGS="--port 5091";                              URL="http://192.168.10.102:5091" ;;
  panel)  SCRIPT=panel_grid.py;  ARGS="--spacing 100";                            URL="(no web UI — read the grid)" ;;
  warp)   SCRIPT=validate_calibration_pygame.py; ARGS="--warp $CAL/warp_map.npz --pattern grid"; URL="(no web UI)" ;;
  *) echo "unknown tool '$TOOL' (use: geo | panel | warp)"; exit 1 ;;
esac

# Launch detached so it survives closing this SSH session.
# Redirect the WHOLE detached subtree's stdio (outside the bash -c) so it releases the
# SSH channel — otherwise `ssh ... cal_start.sh` never returns (hangs the caller/UI).
setsid bash -c "SDL_AUDIODRIVER=dummy DISPLAY=:0 $PY $CAL/$SCRIPT $ARGS" < /dev/null > /tmp/cal_$TOOL.log 2>&1 &
sleep 3
if pgrep -f "$SCRIPT" >/dev/null; then
  echo "Started '$TOOL' on the projector.  Control: $URL"
else
  echo "FAILED to start '$TOOL' — log:"; tail -6 /tmp/cal_$TOOL.log
fi
echo "Stop with:  bash ~/rig/calibration/cal_stop.sh"
