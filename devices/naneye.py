"""
devices/naneye.py

NanEye — the NanEyeM micro-camera behind a "vbridge" CSI bridge, captured with the
CUSTOM-BUILT libcamera CLI (/usr/local/bin/libcamera-vid) as an MJPEG subprocess.
The CLI is the proven path for this sensor (picamera2 compatibility with the custom
libcamera build is unverified), and process isolation means a libcamera crash can't
take pi_api down.

Rig config:
  video: true
  libcamera_bin: /usr/local/bin/libcamera-vid
  resolution: [320, 320]
  fps: 30              # sensor does up to 186; raise after measured_fps proves headroom
  preview_fps: 10
  camera_match: vbridge   # substring expected in --list-cameras output
"""

from __future__ import annotations
import subprocess
from pathlib import Path

from devices.base import DeviceInfo, IOType, register_device
from devices.mjpeg_pipe import PipeCameraBase


@register_device
class NanEye(PipeCameraBase):
    info = DeviceInfo(
        name="naneye",
        label="NanEye",
        io_type=IOType.CSI,
        required_packages=[],
    )

    def init(self, rig_config: dict, task_params: dict):
        super().init(rig_config, task_params)
        self.libcamera_bin = self.cfg.get("libcamera_bin", "/usr/local/bin/libcamera-vid")
        self.resolution = list(self.cfg.get("resolution", [320, 320]))
        self.fps = float(self.cfg.get("fps", 30))
        self.camera_match = self.cfg.get("camera_match", "vbridge")

    def _build_cmd(self):
        w, h = self.resolution
        return [
            self.libcamera_bin, "-t", "0",
            "--width", str(int(w)), "--height", str(int(h)),
            "--framerate", str(int(self.fps)),
            "--codec", "mjpeg", "-n", "-o", "-",
        ]

    def _pkill_pattern(self):
        return "libcamera-vid"

    def check(self) -> dict:
        if not Path(self.libcamera_bin).exists():
            return {"ok": False, "message": f"{self.libcamera_bin} not found"}
        try:
            r = subprocess.run([self.libcamera_bin, "--list-cameras"],
                               capture_output=True, text=True, timeout=10)
            out = (r.stdout or "") + (r.stderr or "")
            if self.camera_match in out:
                return {"ok": True, "message": f"camera '{self.camera_match}' detected"}
            return {"ok": False,
                    "message": f"'{self.camera_match}' not in --list-cameras output"}
        except Exception as e:
            return {"ok": False, "message": f"--list-cameras failed: {e}"}

    @classmethod
    def task_params_schema(cls) -> dict:
        return {
            "fps": {"type": "float", "default": 30, "label": "Capture FPS"},
        }
