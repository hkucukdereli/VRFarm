"""
shared/deploy_manifest.py

The single source of truth for which code files get deployed to a Pi's ~/rig.
Consumed by BOTH the setup UI (Install over scp, Deploy over REST) and the
experiment UI (Deploy step 0) — keep it that way: the two lists used to live
separately in setup/app.py and app/app.py and had already diverged (the
experiment UI never shipped the calibration tools), which is how a Pi ends
up running mixed-generation display code.
"""

from __future__ import annotations

# (local path relative to the repo root, remote path relative to ~/rig)
_COMMON = [
    # Shared
    ("shared/__init__.py", "shared/__init__.py"),
    ("shared/config.py", "shared/config.py"),
    ("shared/stim_generator.py", "shared/stim_generator.py"),
    ("shared/consolidate.py", "shared/consolidate.py"),   # data consolidation at Transfer
    # Devices
    ("devices/__init__.py", "devices/__init__.py"),
    ("devices/base.py", "devices/base.py"),
    ("devices/reward.py", "devices/reward.py"),
    ("devices/reward_calibration.py", "devices/reward_calibration.py"),
    ("devices/lick_sensor.py", "devices/lick_sensor.py"),
    ("devices/camera.py", "devices/camera.py"),
    ("devices/photodiode.py", "devices/photodiode.py"),
    ("devices/display.py", "devices/display.py"),
    ("devices/calibration_probe.py", "devices/calibration_probe.py"),
    ("devices/encoder.py", "devices/encoder.py"),
    # Pi API
    ("pi_api/api.py", "pi_api/api.py"),
]

_LEADER = [
    ("engine/__init__.py", "engine/__init__.py"),
    ("engine/leader.py", "engine/leader.py"),
    # shepherd health monitor code. Its config.yaml is NOT here on purpose — it is
    # seeded once at Install (cp -n) so thresholds edited on the Pi survive a re-Deploy.
    ("shepherd/shepherd.py", "shepherd/shepherd.py"),
    # camera feasibility sweep (run by hand on the Pi to validate mode/fps/CPU combos)
    ("tools/camera_sweep.py", "tools/camera_sweep.py"),
]

_FOLLOWER = [
    ("engine/__init__.py", "engine/__init__.py"),
    # Phase 4: engine/follower.py, engine/display_worker.py and start_projector.sh are gone.
    # displayd owns the display; there is no X server and no second process to draw with.
    # The calibration tools below now run on kmsdrm (cal_start.sh asks displayd to stand
    # aside first), so they no longer need a projector bring-up script.
    ("display_calibration/vsync_probe.py", "calibration/vsync_probe.py"),
    ("display_calibration/calib_geo.py", "calibration/calib_geo.py"),
    ("display_calibration/cal_start.sh", "calibration/cal_start.sh"),
    ("display_calibration/cal_stop.sh", "calibration/cal_stop.sh"),
    ("display_calibration/panel_grid.py", "calibration/panel_grid.py"),
    ("display_calibration/validate_calibration_pygame.py",
     "calibration/validate_calibration_pygame.py"),
    # displayd (phase 2): KMS display daemon + its renderer child + DLPC wrapper.
    # Runs on the SYSTEM python3, not the conda env (kmsdrm SDL). PROTOCOL.md
    # rides along so the deployed interface contract is inspectable on the Pi.
    # The unit file is NOT here — displayd.service is installed to /etc/systemd
    # by the setup UI's Install step (static, like the dlp/ SDK push).
    ("displayd/displayd.py", "displayd/displayd.py"),
    ("displayd/renderer.py", "displayd/renderer.py"),
    ("displayd/dlpc.py", "displayd/dlpc.py"),
    ("displayd/PROTOCOL.md", "displayd/PROTOCOL.md"),
]


def deploy_files(role: str) -> list:
    """(local, remote) file pairs to ship to a Pi of the given role."""
    files = list(_COMMON)
    if role == "leader":
        files += _LEADER
    elif role == "follower":
        files += _FOLLOWER
    return files
