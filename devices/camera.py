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
        # H.264 target bitrate (Mbit/s). Grayscale behavior video compresses well, so this can
        # be dropped (e.g. 3) to fit limited storage such as the SD card. See rig config.
        self.bitrate_mbps = float(task_params.get("bitrate_mbps",
                            rig_config.get("bitrate_mbps", 8)))
        # IR exposure/gain (rig-hardware calibration, set live from the setup UI). auto_exposure
        # True => libcamera AEC/AGC; False => fixed ExposureTime + AnalogueGain. Capping the sensor
        # gain is what suppresses the rolling readout banding on the mono IMX296. Read like
        # bitrate_mbps (task_params first, then rig_config); not in task_params_schema.
        self.auto_exposure = bool(task_params.get("auto_exposure",
                             rig_config.get("auto_exposure", True)))
        self.exposure_us = int(task_params.get("exposure_us",
                           rig_config.get("exposure_us", 10000)))
        self.gain = float(task_params.get("gain",
                    rig_config.get("gain", 1.0)))
        self._recording = False
        self._is_preview = True   # False once a real session recording starts (see start_recording)
        self._stream_active = False
        # Set = no mjpeg preview loop is inside capture_array(); stop_recording waits on this
        # before close() so we never close the camera mid-capture (a libcamera corruption path).
        self._stream_idle = threading.Event()
        self._stream_idle.set()
        self._cam = None
        self._frame_log = []
        self._frame_idx = 0
        self._tslog_path = None
        self._video_path = None

    def start_recording(self, session_id: str, output_dir: str):
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FileOutput

        # "preview" = a throwaway live preview (safe to stop anytime); anything else is a real
        # session recording that must not be stopped by a setup-UI action.
        self._is_preview = (session_id == "preview")
        out_dir = Path(output_dir) / session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        self._video_path = out_dir / "video.h264"
        self._tslog_path = out_dir / "frame_timestamps.npy"
        self._frame_log = []
        self._frame_idx = 0

        # Open + configure + start. If ANY step after the sensor is opened fails (e.g. an
        # unsupported res/fps, encoder error, or unwritable output), release the half-opened
        # camera here — otherwise the open Picamera2 lingers (defeating refcount GC via the
        # pre_callback ref cycle) and hangs the next open, unrecoverable via Stop/Reinit.
        try:
            self._cam = Picamera2()
            controls = {"FrameRate": self.fps, "Saturation": 0.0}
            controls.update(self._exposure_controls())
            cfg = self._cam.create_video_configuration(
                main={"size": tuple(self.resolution), "format": "RGB888"},
                controls=controls,
            )
            self._cam.configure(cfg)
            encoder = H264Encoder(bitrate=int(self.bitrate_mbps * 1_000_000))
            output = FileOutput(str(self._video_path))
            self._cam.pre_callback = self._on_frame
            self._recording = True
            self._cam.start_recording(encoder, output)
        except Exception:
            self._recording = False
            if self._cam is not None:
                try:
                    self._cam.pre_callback = None   # break the dev<->_cam reference cycle
                except Exception:
                    pass
                try:
                    self._cam.close()
                except Exception:
                    pass
                self._cam = None
                time.sleep(0.3)   # sensor releases asynchronously; settle before any reopen
            raise

    def _exposure_controls(self) -> dict:
        """libcamera exposure/gain controls for the current settings. Auto => let AEC/AGC run;
        manual => fix ExposureTime + AnalogueGain (low gain suppresses the rolling readout
        banding). Applied both at configure time (start_recording) and live (apply_exposure)."""
        if self.auto_exposure:
            return {"AeEnable": True}
        return {"AeEnable": False,
                "ExposureTime": int(self.exposure_us),
                "AnalogueGain": float(self.gain)}

    def set_live_controls(self, ctrls: dict) -> dict:
        """Apply libcamera controls to the running camera, dropping any keys this libcamera build
        doesn't support (e.g. on trixie). No-op if the camera isn't open. Returns what was applied."""
        if self._cam is None:
            return {}
        try:
            supported = set(self._cam.camera_controls)
        except Exception:
            supported = None
        applied = {k: v for k, v in ctrls.items()
                   if supported is None or k in supported}
        if applied:
            self._cam.set_controls(applied)
        return applied

    def apply_exposure(self, auto_exposure=None, exposure_us=None, gain=None) -> dict:
        """Update exposure settings (any arg left None is unchanged) and push them to the running
        camera. Returns the controls applied (empty if the camera isn't open)."""
        if auto_exposure is not None:
            self.auto_exposure = bool(auto_exposure)
        if exposure_us is not None:
            self.exposure_us = int(exposure_us)
        if gain is not None:
            self.gain = float(gain)
        return self.set_live_controls(self._exposure_controls())

    def _on_frame(self, request):
        if self._recording:
            self._frame_log.append((self._frame_idx, time.time()))
            self._frame_idx += 1

    def stop_recording(self) -> dict:
        if not self._recording:
            # Idempotent: still release any lingering camera handle so the sensor is free for
            # the next Picamera2() (a res/fps change or Reinit). A retained closed Picamera2
            # blocks the next open and hangs the camera.
            if self._cam is not None:
                try:
                    self._cam.close()
                except Exception:
                    pass
                self._cam = None
                time.sleep(0.3)   # let libcamera release the sensor before any reopen
            return {}
        self._recording = False   # stops the mjpeg_stream loop + _on_frame logging
        # Wait for the mjpeg loop to finish any in-flight capture_array() and exit before we
        # close the device — closing mid-capture corrupts libcamera state. Bounded so a wedged
        # stream can't hang stop forever (idle Event is pre-set when no stream is running).
        self._stream_idle.wait(timeout=1.0)
        result = {"frames": len(self._frame_log)}
        if self._frame_log and self._tslog_path:
            arr = np.array(self._frame_log, dtype=np.float64)
            np.save(str(self._tslog_path), arr)
            dur = self._frame_log[-1][1] - self._frame_log[0][1]
            result["avg_fps"] = len(self._frame_log) / max(dur, 1)
            result["video_path"] = str(self._video_path)
            result["timestamps_path"] = str(self._tslog_path)
        # Fully tear down + drop the reference so the sensor is released for the next open.
        try:
            self._cam.stop_recording()
        except Exception:
            pass
        try:
            self._cam.close()
        except Exception:
            pass
        self._cam = None
        time.sleep(0.3)   # let libcamera release the sensor before any reopen
        return result

    def mjpeg_stream(self, every_n: int = 2):
        """MJPEG preview generator for Flask streaming. Runs ONLY while recording; when the
        preview/recording stops (`_recording` -> False, or the camera is released) the generator
        RETURNS so the Flask streaming thread ends and this Camera (and its Picamera2) can be
        garbage-collected/released. The previous `while True` here leaked a spinning thread per
        preview that pinned the old camera and made the next Picamera2() hang on reconfigure."""
        from PIL import Image
        self._stream_idle.clear()   # a capture loop is now active; stop_recording will wait for us
        try:
            n = 0
            while self._recording and self._cam is not None:
                n += 1
                if n % every_n != 0:
                    time.sleep(1 / 15)
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
        finally:
            self._stream_idle.set()   # loop exited; safe for stop_recording to close the camera

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
