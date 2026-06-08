#!/bin/bash
# cal_stop.sh — stop any running display-calibration tool. Run ON mozzarella.
if pkill -f 'calib_geo.py|calib_tool.py|panel_grid.py|validate_calibration_pygame.py'; then
  echo "calibration tool stopped."
else
  echo "(nothing was running)"
fi
