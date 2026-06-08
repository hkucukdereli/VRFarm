"""
devices/camera.py

Camera recording via picamera2 (CSI).
H264 video + per-frame timestamps saved locally.
MJPEG preview stream for UI livestream.
"""

from __future__ import annotations
import io
import threading
import time
from pathlib import Path

import numpy as np

from .base import Device, DeviceInfo, IOType, register_device


@register_device
class Camera(Device):
    info = DeviceInfo("camera", "Camera", IOType.CSI, ["picamera2"])

    @classmethod
    def task_params_schema(cls):
        return {
            "resolution": {
                "type": "list", "default": [1280, 720],
                "label": "Resolution",
                "options": [[640, 480], [1280, 720], [1920, 1080]],
            },
            "fps": {
                "type": "int", "default": 50,
                "label": "FPS", "min": 10, "max": 120,
            },
        }

    def init(self, rig_config: dict, task_params: dict):
        self.resolution = task_params.get("resolution",
                          rig_config.get("resolution", [1280, 720]))
        self.fps = task_params.get("fps",
                   rig_config.get("fps", 50))
        self._recording = False
        self._stream_active = False
        self._cam = None
        self._frame_log = []
        self._frame_idx = 0
        self._tslog_path = None
        self._video_path = None

    def start_recording(self, session_id: str, output_dir: str):
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FileOutput

        out_dir = Path(output_dir) / session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        self._video_path = out_dir / "video.h264"
        self._tslog_path = out_dir / "frame_timestamps.npy"
        self._frame_log = []
        self._frame_idx = 0

        self._cam = Picamera2()
        cfg = self._cam.create_video_configuration(
            main={"size": tuple(self.resolution), "format": "RGB888"},
            controls={"FrameRate": self.fps, "Saturation": 0.0},
        )
        self._cam.configure(cfg)
        encoder = H264Encoder(bitrate=8_000_000)  # lower bitrate for grayscale
        output = FileOutput(str(self._video_path))
        self._cam.pre_callback = self._on_frame
        self._recording = True
        self._cam.start_recording(encoder, output)

    def _on_frame(self, request):
        if self._recording:
            self._frame_log.append((self._frame_idx, time.time()))
            self._frame_idx += 1

    def stop_recording(self) -> dict:
        if not self._recording:
            return {}
        self._recording = False
        try:
            self._cam.stop_recording()
            self._cam.close()
        except Exception:
            pass
        result = {"frames": len(self._frame_log)}
        if self._frame_log and self._tslog_path:
            arr = np.array(self._frame_log, dtype=np.float64)
            np.save(str(self._tslog_path), arr)
            dur = self._frame_log[-1][1] - self._frame_log[0][1]
            result["avg_fps"] = len(self._frame_log) / max(dur, 1)
            result["video_path"] = str(self._video_path)
            result["timestamps_path"] = str(self._tslog_path)
        return result

    def mjpeg_stream(self, every_n: int = 2):
        """MJPEG preview generator for Flask streaming."""
        from PIL import Image
        n = 0
        while True:
            n += 1
            if n % every_n != 0:
                time.sleep(1 / 15)
                continue
            if self._cam is None or not self._recording:
                time.sleep(0.1)
                continue
            try:
                arr = self._cam.capture_array()
                img = Image.fromarray(arr).convert("L")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=50)
                frame = buf.getvalue()
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + frame + b"\r\n")
            except Exception:
                time.sleep(0.1)
            time.sleep(1 / 15)

    def check(self) -> dict:
        try:
            from picamera2 import Picamera2
            cams = Picamera2.global_camera_info()
            if cams:
                return {"ok": True, "message": f"{len(cams)} camera(s) found"}
            return {"ok": False, "message": "No cameras detected"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def start_stream(self, callback):
        """Periodic frame count updates."""
        self._stream_active = True

        def report():
            while self._stream_active:
                if self._recording:
                    callback({"event": "camera_frame_count",
                              "frames": self._frame_idx})
                time.sleep(1.0)

        threading.Thread(target=report, daemon=True).start()

    def stop_stream(self):
        self._stream_active = False

    def local_files(self) -> list[str]:
        """Files to transfer to Mac after session."""
        files = []
        if self._video_path and Path(self._video_path).exists():
            files.append(str(self._video_path))
        if self._tslog_path and Path(self._tslog_path).exists():
            files.append(str(self._tslog_path))
        return files

    def close(self):
        self.stop_stream()
        self.stop_recording()
