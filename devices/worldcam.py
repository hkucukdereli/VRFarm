"""
devices/worldcam.py

WorldCam — a USB UVC camera (LogiLink / MacroSilicon MS210x video grabber) captured
via an ffmpeg subprocess in MJPEG **stream copy** mode: the grabber already emits
JPEG frames, so there is zero transcode CPU on the Pi and the stored frames are the
sensor's own bytes. The same frames feed the live preview (no re-encode).

Rig config:
  video: true                 <- marks it camera-like for the UIs / Go recording
  video_device: /dev/video1   <- prefer a stable /dev/v4l/by-id/... path
  resolution: [1280, 720]
  fps: 30
  preview_fps: 10
"""

from __future__ import annotations
import shutil
import subprocess
from pathlib import Path

from devices.base import DeviceInfo, IOType, register_device
from devices.mjpeg_pipe import PipeCameraBase


@register_device
class WorldCam(PipeCameraBase):
    info = DeviceInfo(
        name="worldcam",
        label="World Camera",
        io_type=IOType.USB,
        required_packages=[],
    )

    def init(self, rig_config: dict, task_params: dict):
        super().init(rig_config, task_params)
        self.video_device = self.cfg.get("video_device", "/dev/video1")
        self.resolution = list(self.cfg.get("resolution", [1280, 720]))
        self.fps = float(self.cfg.get("fps", 30))

    def _build_cmd(self):
        w, h = self.resolution
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", f"{int(w)}x{int(h)}",
            "-framerate", str(int(self.fps)),
            "-i", self.video_device,
            "-c:v", "copy", "-f", "mjpeg", "pipe:1",
        ]

    def check(self) -> dict:
        if not Path(self.video_device).exists():
            return {"ok": False, "message": f"{self.video_device} not found"}
        if shutil.which("ffmpeg") is None:
            return {"ok": False, "message": "ffmpeg not installed"}
        msg = f"{self.video_device} present"
        # Best effort: confirm the node actually offers MJPEG (v4l-utils optional).
        try:
            r = subprocess.run(["v4l2-ctl", "-d", self.video_device, "--list-formats"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and "MJPG" not in r.stdout:
                return {"ok": False,
                        "message": f"{self.video_device} has no MJPG format"}
            if r.returncode == 0:
                msg += ", MJPG ok"
        except Exception:
            pass
        return {"ok": True, "message": msg}

    @classmethod
    def task_params_schema(cls) -> dict:
        return {
            "fps": {"type": "float", "default": 30, "label": "Capture FPS"},
        }
