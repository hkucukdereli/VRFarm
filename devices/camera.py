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


# Experiment live-view presets: downsample ONLY the live preview during a session (fps + a scale of
# the recording resolution + JPEG quality). The SAVED video always uses the full recording profile.
PRESETS = {
    "high": {"fps": 25, "scale": 0.5, "quality": 75},
    "med":  {"fps": 15, "scale": 0.25, "quality": 60},
    "low":  {"fps": 10, "scale": 0.25, "quality": 45},
}


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
        # Experiment live-view preset (high/med/low) — downsamples ONLY the live preview during a
        # session; the recorded video always uses the full resolution/fps/bitrate above.
        self.live_preset = str(task_params.get("live_preset",
                           rig_config.get("live_preset", "med")))
        self._recording = False
        self._is_preview = True   # False once a real session recording starts (see start_recording)
        self._encoding = False    # True while the H264 encoder is running (recording, not preview-only)
        self._live = False        # True while the camera is running (preview OR recording)
        self._preview_source = "main"       # capture_array stream feeding the mjpeg preview
        self._preview_fps = int(self.fps)   # preview pacing target (faithful for setup, throttled for recording)
        self._preview_quality = 80          # preview JPEG quality
        self._lores_size = None             # (w,h) of the lores stream when previewing from it
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

        preset = PRESETS.get(self.live_preset, PRESETS["med"])
        lores_size = self._scaled_size(preset["scale"])
        # Open + configure + start. If ANY step after the sensor is opened fails (e.g. an
        # unsupported res/fps, encoder error, or unwritable output), release the half-opened
        # camera here — otherwise the open Picamera2 lingers (defeating refcount GC via the
        # pre_callback ref cycle) and hangs the next open, unrecoverable via Stop/Reinit.
        try:
            self._cam = Picamera2()
            controls = {"FrameRate": self.fps, "Saturation": 0.0}
            controls.update(self._exposure_controls())
            # Record the full 'main' stream to H264; preview from a small, decoupled 'lores' stream
            # so capture_array() can't starve the encoder (the N1 buffer-starvation freeze). The
            # preview runs at the live preset while the saved video stays full resolution/fps.
            cfg = self._cam.create_video_configuration(
                main={"size": tuple(self.resolution), "format": "RGB888"},
                lores={"size": lores_size, "format": "YUV420"},
                controls=controls,
            )
            self._cam.configure(cfg)
            encoder = H264Encoder(bitrate=int(self.bitrate_mbps * 1_000_000))
            output = FileOutput(str(self._video_path))
            self._cam.pre_callback = self._on_frame
            self._recording = True
            self._cam.start_recording(encoder, output)
            self._preview_source = "lores"
            self._preview_fps = preset["fps"]
            self._preview_quality = preset["quality"]
            # Crop the Y-plane with the size libcamera ACTUALLY configured (the ISP may adjust the
            # requested lores size), so the preview never tears; fall back to the requested size.
            try:
                self._lores_size = tuple(self._cam.camera_configuration()["lores"]["size"])
            except Exception:
                self._lores_size = lores_size
            self._encoding = True
            self._live = True
        except Exception:
            self._recording = False
            self._live = False
            self._encoding = False
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

    def start_preview(self, downsample=False):
        """Preview-only live view: configure + start the camera with NO encoder and write no file.
        downsample=False (setup 'Live') runs at the real recording res/fps — faithful. downsample=True
        (experiment 'Live') runs at the live preset (smaller res, lower fps) to keep the load light.
        Freeze-safe either way: with no H264 encoder competing on the stream, capture_array('main')
        can run without the buffer-starvation stall that happens when preview fights the encoder."""
        from picamera2 import Picamera2
        if downsample:
            preset = PRESETS.get(self.live_preset, PRESETS["med"])
            size = self._scaled_size(preset["scale"])
            fps = preset["fps"]
            quality = preset["quality"]
        else:
            size = tuple(self.resolution)
            fps = int(self.fps)   # faithful: the real capture fps, not throttled
            quality = 85
        self._is_preview = True
        self._video_path = None
        self._tslog_path = None
        self._frame_log = []
        self._frame_idx = 0
        try:
            self._cam = Picamera2()
            controls = {"FrameRate": fps, "Saturation": 0.0}
            controls.update(self._exposure_controls())
            cfg = self._cam.create_video_configuration(
                main={"size": size, "format": "RGB888"},
                controls=controls,
            )
            self._cam.configure(cfg)
            self._cam.start()          # no encoder -> nothing is written
            self._preview_source = "main"
            self._preview_fps = fps
            self._preview_quality = quality
            self._lores_size = None
            self._encoding = False
            self._recording = False
            self._live = True
        except Exception:
            self._live = False
            if self._cam is not None:
                try:
                    self._cam.close()
                except Exception:
                    pass
                self._cam = None
                time.sleep(0.3)
            raise

    def _scaled_size(self, scale):
        """A (w,h) scaled from the recording resolution, clamped even and >=64 — the lores preview
        stream size (recording) and the downsampled preview-only size (experiment Live)."""
        mw, mh = self.resolution
        lw = max(64, (int(mw * scale) // 2) * 2)
        lh = max(64, (int(mh * scale) // 2) * 2)
        return (lw, lh)

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

    def _release_camera(self):
        """Stop the preview loop + encoder and release the sensor. Idempotent; safe whether
        recording, previewing, or already stopped. Waits for any in-flight capture_array() to
        finish (via _stream_idle) before closing — closing mid-capture corrupts libcamera state —
        then settles so the next Picamera2() open won't hang. A retained/closed handle blocks the
        next open, so we always drop the reference."""
        self._live = False        # stops the mjpeg_stream loop
        self._recording = False   # stops _on_frame logging
        self._stream_idle.wait(timeout=1.0)   # bounded; pre-set when no stream is running
        if self._cam is not None:
            try:
                if self._encoding:
                    self._cam.stop_recording()
                else:
                    self._cam.stop()
            except Exception:
                pass
            try:
                self._cam.close()
            except Exception:
                pass
            self._cam = None
            time.sleep(0.3)   # let libcamera release the sensor before any reopen
        self._encoding = False

    def stop_recording(self) -> dict:
        result = {}
        was = self._recording
        self._recording = False   # stop _on_frame appends before snapshotting the timestamps
        if was:
            result = {"frames": len(self._frame_log)}
            if self._frame_log and self._tslog_path:
                arr = np.array(self._frame_log, dtype=np.float64)
                np.save(str(self._tslog_path), arr)
                dur = self._frame_log[-1][1] - self._frame_log[0][1]
                result["avg_fps"] = len(self._frame_log) / max(dur, 1)
                result["video_path"] = str(self._video_path)
                result["timestamps_path"] = str(self._tslog_path)
        self._release_camera()
        return result

    def stop_preview(self) -> dict:
        """Stop a preview-only (setup Live) session and release the camera."""
        self._release_camera()
        return {}

    def mjpeg_stream(self):
        """MJPEG preview generator for Flask. Runs while the camera is live (preview-only OR
        recording) and RETURNS when it stops, so the Flask thread ends and the Picamera2 is
        released (the old `while True` leaked a spinning thread that pinned the camera and hung the
        next reconfigure). Paces to `_preview_fps`: the setup preview runs at the real capture fps;
        the recording preview is throttled so capture_array() can't starve the H264 encoder. Reads
        `_preview_source` ('main', or 'lores' once the experiment stream lands)."""
        from PIL import Image
        self._stream_idle.clear()   # a capture loop is now active; teardown will wait for us
        try:
            source = self._preview_source
            lores_size = self._lores_size
            quality = self._preview_quality
            period = 1.0 / max(1, self._preview_fps)
            while self._live and self._cam is not None:
                t0 = time.time()
                try:
                    arr = self._cam.capture_array(source)
                    if source == "lores" and lores_size is not None:
                        lw, lh = lores_size
                        img = Image.fromarray(arr[:lh, :lw], mode="L")   # Y plane = free grayscale
                    else:
                        img = Image.fromarray(arr).convert("L")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=quality)
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + buf.getvalue() + b"\r\n")
                except Exception:
                    time.sleep(0.05)
                dt = time.time() - t0
                if dt < period:
                    time.sleep(period - dt)
        finally:
            self._stream_idle.set()   # loop exited; safe for teardown to close the camera

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
        # Route through stop_recording (which delegates to _release_camera): a generic close() on a
        # still-live recording — e.g. the release_devices abort/reset path — must still flush
        # frame_timestamps.npy, not just finalize the video. Idempotent for preview/already-stopped.
        self.stop_recording()
