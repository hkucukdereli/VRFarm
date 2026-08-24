#!/bin/bash
# cal_stop.sh — stop any running display-calibration tool and give the display back to
# displayd. Run ON the display Pi.
#
# The /resume is the important half: the calibration tool did its own modeset and left the
# DLPC source wherever it liked, so displayd re-walks the whole ladder. Without this the
# projector stays dark after calibration and the next session fails its SYNC_TEST.
DISPLAYD=http://127.0.0.1:5581

if pkill -f 'calib_geo.py|panel_grid.py|validate_calibration_pygame.py'; then
  echo "calibration tool stopped."
else
  echo "(nothing was running)"
fi
sleep 1   # let the tool drop DRM master before displayd tries to take it

if curl -s -m 3 "$DISPLAYD/status" | grep -q '"state"'; then
  echo "returning the display to displayd (re-init)..."
  if curl -s -m 60 -X POST "$DISPLAYD/resume" | grep -q '"state": *"RENDERER_UP"'; then
    echo "displayd RENDERER_UP."
  else
    echo "WARNING: displayd did not reach RENDERER_UP — check 'curl $DISPLAYD/status'."
  fi
fi
