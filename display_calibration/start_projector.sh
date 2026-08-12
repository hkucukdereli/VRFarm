#!/bin/bash
# start_projector.sh — run before launching task.py
# Sets up DPI GPIO, initializes DLPC3436, starts X11

set -e

# 1. Set GPIO 0-21 to ALT2 (DPI parallel video output)
for i in $(seq 0 21); do pinctrl set $i a2; done

# 2. Enable video buffer (GPIO 25 high)
pinctrl set 25 op dh

# 3. Initialize DLPC3436 for external parallel video input
source ~/miniforge3/etc/profile.d/conda.sh
conda activate rig
cd ~/dlp && python3 init_parallel_mode.py

# 4. Free display :0 — the X server runs as root (started with sudo below), so killing it
#    needs sudo, and its lock must be cleared, or a re-run dies "Server is already active
#    for display 0" (the old plain `pkill -f 'Xorg :0'` matched nothing and never freed :0).
sudo pkill -9 Xorg 2>/dev/null || true
sudo rm -f /tmp/.X0-lock 2>/dev/null || true
sleep 1

# 5. Start X11
sudo X :0 -ac -s off -dpms > /tmp/xorg.log 2>&1 &
sleep 3

export DISPLAY=:0
echo Projector ready. DISPLAY=:0
