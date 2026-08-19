"""
devices/camera.py

Camera recording via picamera2 (CSI).
H264 video + per-frame timestamps saved locally.
MJPEG preview stream for UI livestream.

Sensor on the cheddar rig: **IMX477** (Raspberry Pi HQ camera) — a 4056x3040 COLOUR (Bayer,
RGGB) ROLLING-shutter sensor, mounted Rotation 180. Not the mono global-shutter IMX296 that
earlier comments here claimed. Grayscale is produced by the ISP, not by the sensor: `Saturation:
0.0` in the controls below plus taking the Y plane of the YUV420 output. That is format-driven,
so the code is correct on either sensor.

All offered resolutions select sensor mode 2028x1080 @ 12-bit (max 62.81 fps, crop_limits
(0,440,4056,2160)) — full sensor width, vertically cropped to 71% of the array, 2x2 binned, then
ISP-downscaled. Two ways that silently changes the FIELD OF VIEW rather than the resolution, so
both are guarded: a 4:3 output size selects the 1332x990 mode (a hard centre crop losing ~35% in
both axes), and fps above 62 does the same — hence the 16:9-only option list and the fps cap.

`colour_space` is pinned to Rec709 rather than left to picamera2, which would otherwise choose it
from the output size (picamera2.py: `Smpte170m` if width < 1280 OR height < 720, else `Rec709`).
Since grayscale here IS the Y plane, that default would change the luma weighting — and so the
recorded pixel values — when the resolution crosses 1280x720. Pinning keeps every session
comparable regardless of resolution.

Rolling shutter means rows are exposed progressively over the frame readout (>=15.9 ms in this
mode), so a frame is not an instant. Stimulus timing does not depend on this — that comes from
the photodiode sync channel — but per-frame video timing is only accurate to the readout skew.
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

# IMX477 sensor readout modes we expose. `max_fps` is the sensor's DECLARED ceiling per raw bit
# depth, read from Picamera2.sensor_modes on the actual rig — the sensor can deliver these; whether
# the Pi 5's SOFTWARE encoder can sustain a given (mode, depth, output, fps) is a separate
# feasibility question answered by tools/camera_sweep.py. Kept in sync with CAM_MODES in
# setup/templates/setup.html.
SENSOR_MODES = {
    (2028, 1080): {"label": "fast (16:9)",  "fov": "100% x 71%",
                   "outputs": [(2028, 1080), (1014, 540)],   # native, half
                   "max_fps": {8: 92.27, 10: 74.74, 12: 62.81}},
    (2028, 1520): {"label": "hires (4:3)",  "fov": "100% x 100%",
                   "outputs": [(2028, 1520), (1014, 760)],
                   "max_fps": {8: 66.38, 10: 53.77, 12: 45.19}},
}

# Native-output ENCODER ceilings, MEASURED on the Pi 5 (tools/camera_sweep.py): the software H.264
# encoder saturates and drops frames above these when emitting the FULL-RESOLUTION stream — native
# 2028x1080 is clean at 50 fps and drops at 55; native 2028x1520 is clean at 40 and drops at 45.
# Half/downscaled outputs have NO such limit (verified clean to 66 fps @ 25% CPU). The setup UI
# caps fps to min(sensor, encoder); this table backs a soft warning for a hand-edited rig yaml.
ENCODER_MAX_FPS_NATIVE = {(2028, 1080): 50, (2028, 1520): 40}


@register_device
class Camera(Device):
    info = DeviceInfo("camera", "Camera", IOType.CSI, ["picamera2"])

    @classmethod
    def task_params_schema(cls):
        return {
            # Output size. Since sensor_mode is now pinned (see init), the valid outputs are the
            # native + half sizes of the chosen mode: 2028x1080 mode -> 2028x1080 or 1014x540;
            # 2028x1520 mode -> 2028x1520 or 1014x760. The setup UI restricts the choice per mode.
            "resolution": {
                "type": "list", "default": [1014, 540],
                "label": "Resolution",
                "options": [[1014, 540], [2028, 1080], [1014, 760], [2028, 1520]],
            },
            # Max is the highest sensor ceiling across the exposed combos (2028x1080 @ 8-bit =
            # 92 fps). The real per-(mode, bit_depth) ceiling is smaller and is enforced by the
            # setup UI; libcamera clamps anything above the mode's ceiling, but then the encoder is
            # still told the requested fps and the file lands under target and mis-tagged — so DON'T
            # over-request. See SENSOR_MODES.
            "fps": {
                "type": "int", "default": 50,
                "label": "FPS", "min": 10, "max": 92,
            },
        }

    def init(self, rig_config: dict, task_params: dict):
        self.resolution = task_params.get("resolution",
                          rig_config.get("resolution", [1280, 720]))
        self.fps = task_params.get("fps",
                   rig_config.get("fps", 50))
        # Sensor mode + raw bit depth. Pinning these makes a session's FIELD OF VIEW and readout
        # depth DETERMINISTIC instead of inferred from the output size (which is how a 4:3 request
        # used to fall onto a hidden centre-crop mode). sensor_mode = the mode's output_size, e.g.
        # [2028,1080] (fast/16:9) or [2028,1520] (hires/full-sensor 4:3). bit_depth 8/10/12 sets the
        # fps ceiling and the raw quantization (8-bit is plenty for well-lit motion — see the guide).
        # Both optional: absent => legacy behaviour (no pin, picamera2's default 12-bit).
        sm = task_params.get("sensor_mode", rig_config.get("sensor_mode"))
        self.sensor_mode = tuple(sm) if sm else None
        bd = task_params.get("bit_depth", rig_config.get("bit_depth"))
        self.bit_depth = int(bd) if bd else None
        # H.264 target bitrate (Mbit/s). With h264_profile "main" (CABAC), 4 Mbit/s at 720p50
        # measures better than 8 Mbit/s did on baseline — see the encoder settings below.
        self.bitrate_mbps = float(task_params.get("bitrate_mbps",
                            rig_config.get("bitrate_mbps", 4)))
        # H.264 profile. picamera2 defaults to no profile, which leaves libav on the "ultrafast"
        # preset => BASELINE: no CABAC, no B-frames, and roughly HALF the compression efficiency.
        # Naming a non-baseline profile moves it to "superfast" + CABAC. Measured on real
        # behaviour video (720p50, head-fixed mouse): 2 Mbit/s main (SSIM 0.9765) beats 4 Mbit/s
        # baseline (0.9717), and 8 Mbit/s main matches what 12 Mbit/s baseline used to give.
        # Cost is CPU only, and the Pi 5 has room: 157 fps encode = 3.1x realtime at 720p50.
        # "" / "none" / "auto" => let the encoder choose (i.e. baseline).
        prof = str(task_params.get("h264_profile",
                   rig_config.get("h264_profile", "main")) or "").strip()
        self.h264_profile = None if prof.lower() in ("", "none", "auto") else prof
        # Keyframe (IDR) interval in SECONDS — converted to frames against the real fps, so it
        # stays 5 s whatever the fps. picamera2's default iperiod=30 means an IDR every 30 FRAMES,
        # i.e. every 0.6 s at 50 fps: 3,024 IDRs in a 30 min session at ~126 KB each. Seeking in a
        # raw .h264 is keyframe-granular, so this trades seek granularity for size.
        self.gop_s = float(task_params.get("gop_s", rig_config.get("gop_s", 5.0)))
        # IR exposure/gain (rig-hardware calibration, set live from the setup UI). auto_exposure
        # True => libcamera AEC/AGC; False => fixed ExposureTime + AnalogueGain. Capping the sensor
        # gain is what suppresses the rolling readout banding on the IMX477. Read like
        # bitrate_mbps (task_params first, then rig_config); not in task_params_schema.
        self.auto_exposure = bool(task_params.get("auto_exposure",
                             rig_config.get("auto_exposure", True)))
        self.exposure_ms = float(task_params.get("exposure_ms",
                           rig_config.get("exposure_ms", 10.0)))
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
        # Held ONLY around a single capture_array() call. _release_camera acquires it before
        # close() so the sensor is never torn down while a capture is executing — the finer guard
        # that plugs the residual race left by the _stream_idle timeout (the mid-capture wedge).
        self._capture_lock = threading.Lock()
        self._cam = None
        self._frame_log = []
        self._frame_idx = 0
        self._tslog_path = None
        self._video_path = None
        self._geom_path = None
        self._geometry = {}

    def start_recording(self, session_id: str, output_dir: str):
        from picamera2 import Picamera2
        from libcamera import ColorSpace
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FileOutput

        # "preview" = a throwaway live preview (safe to stop anytime); anything else is a real
        # session recording that must not be stopped by a setup-UI action.
        self._is_preview = (session_id == "preview")
        # Soft warning for a native-output combo the software encoder can't sustain (the UI caps
        # this, but a hand-edited yaml could exceed it). Warn, don't block — a dropped-frame video
        # is still a video, and frame_timestamps stays truthful.
        if self.sensor_mode and tuple(self.resolution) == tuple(self.sensor_mode):
            cap = ENCODER_MAX_FPS_NATIVE.get(tuple(self.sensor_mode))
            if cap and self.fps > cap:
                print(f"[camera] WARNING: native {tuple(self.resolution)} @ {self.fps} fps exceeds "
                      f"the measured encoder ceiling ({cap} fps) — frames WILL drop. Use a half "
                      f"output or fps <= {cap}.", flush=True)
        out_dir = Path(output_dir) / session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        self._video_path = out_dir / "video.h264"
        self._tslog_path = out_dir / "frame_timestamps.npy"
        self._geom_path = out_dir / "camera_metadata.json"
        self._geometry = {}
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
                main={"size": tuple(self.resolution), "format": "YUV420"},
                lores={"size": lores_size, "format": "YUV420"},
                controls=controls,
                colour_space=ColorSpace.Rec709(),
                **self._sensor_config_kwargs(self._cam),   # pin mode + bit depth (if configured)
            )
            self._cam.configure(cfg)
            self._verify_sensor(self._cam)   # raise if the pin wasn't honoured (never record the wrong FoV)
            # framerate= is NOT cosmetic: on the Pi 5 there is no hardware encoder, so this is
            # libav/x264 in software, and x264 derives its per-frame bit budget from this value.
            # Left at picamera2's default of 30 while the sensor runs at 50, every recording came
            # out fps/30 too large (50 fps => 1.67x: a 12 Mbit/s session landed at 19.85 Mbit/s,
            # 10.3 GB for 69 min) AND was tagged 30 fps in the SPS, so it played back at 0.6x
            # speed. Both bugs are this one argument. Frame timestamps were always correct.
            # Kwarg names match on the Pi 4 hardware H264Encoder too, so this stays portable.
            encoder = H264Encoder(bitrate=int(self.bitrate_mbps * 1_000_000),
                                  framerate=self.fps,
                                  iperiod=max(1, int(round(self.fps * self.gop_s))),
                                  profile=self.h264_profile)
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
            self._geometry = self._capture_geometry(encoder)
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
        BOTH modes preview from a small YUV420 `lores` stream (its Y-plane taken directly as
        grayscale) — the SAME light path the recording preview uses. capture_array('main') at full
        resolution can't be JPEG-encoded in Python fast enough and comes back blank/stalled, so
        the preview never reads `main`. downsample=False (setup 'Live') uses a
        larger half-res lores for focus/framing at a capped fps; downsample=True (experiment 'Live')
        uses the live preset."""
        from picamera2 import Picamera2
        from libcamera import ColorSpace
        if downsample:
            preset = PRESETS.get(self.live_preset, PRESETS["med"])
            lores_size = self._scaled_size(preset["scale"])
            fps = preset["fps"]
            quality = preset["quality"]
        else:
            lores_size = self._scaled_size(0.5)   # setup: half-res preview — detailed but light
            fps = min(int(self.fps), 25)          # capped: Python JPEG can't sustain 50 fps
            quality = 80
        self._is_preview = True
        self._video_path = None
        self._tslog_path = None
        self._frame_log = []
        self._frame_idx = 0
        try:
            self._cam = Picamera2()
            controls = {"FrameRate": fps, "Saturation": 0.0}
            controls.update(self._exposure_controls())
            # main (full-res, unread here) + lores YUV420 — mirrors start_recording's stream setup
            # minus the encoder; the preview captures `lores`, never the heavy `main`.
            cfg = self._cam.create_video_configuration(
                main={"size": tuple(self.resolution), "format": "YUV420"},
                lores={"size": lores_size, "format": "YUV420"},
                controls=controls,
                colour_space=ColorSpace.Rec709(),
                **self._sensor_config_kwargs(self._cam),   # pin mode + bit depth (if configured)
            )
            self._cam.configure(cfg)
            self._verify_sensor(self._cam)   # raise if the pin wasn't honoured
            self._cam.start()          # no encoder -> nothing is written
            self._preview_source = "lores"
            self._preview_fps = fps
            self._preview_quality = quality
            # Use the size libcamera ACTUALLY configured (the ISP may adjust it) so the Y-plane
            # crop matches and the preview never tears; fall back to the requested size.
            try:
                self._lores_size = tuple(self._cam.camera_configuration()["lores"]["size"])
            except Exception:
                self._lores_size = lores_size
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

    @staticmethod
    def _jsonable(v):
        """Coerce a libcamera/numpy value into something json.dump can write."""
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if isinstance(v, (list, tuple)):
            return [Camera._jsonable(x) for x in v]
        if isinstance(v, dict):
            return {str(k): Camera._jsonable(x) for k, x in v.items()}
        for attr in ("tolist", "item"):
            try:
                return getattr(v, attr)()
            except Exception:
                pass
        return str(v)

    def _capture_geometry(self, encoder=None) -> dict:
        """Snapshot everything that determines WHICH PART OF THE SCENE each pixel is, and at what
        scale — sensor mode, ScalerCrop, output size, binning, orientation, colour space — plus the
        exposure and encoder settings that produced this file.

        None of it is recoverable from video.h264 afterwards: two recordings can be byte-identical
        in format and cover completely different fields of view because the ScalerCrop differed.
        Written next to frame_timestamps.npy and folded into the .h5 /camera attrs by
        shared/consolidate.py, so a session is self-describing.

        Never raises: a metadata problem must not abort a recording. Missing keys are simply absent.
        """
        g = {}

        def put(key, fn):
            try:
                g[key] = self._jsonable(fn())
            except Exception:
                pass

        cam = self._cam
        if cam is not None:
            props = getattr(cam, "camera_properties", {}) or {}
            for k in ("Model", "PixelArraySize", "PixelArrayActiveAreas", "Rotation",
                      "ColorFilterArrangement", "ScalerCropMaximum", "UnitCellSize"):
                if k in props:
                    put(f"sensor_{k}", lambda k=k: props[k])
            cfg = {}
            put("_cfg", lambda: cfg.update(cam.camera_configuration()) or None)
            g.pop("_cfg", None)
            for name in ("main", "lores", "raw"):
                if cfg.get(name):
                    put(f"{name}_size", lambda n=name: cfg[n].get("size"))
                    put(f"{name}_format", lambda n=name: cfg[n].get("format"))
            if cfg.get("sensor"):
                put("sensor_mode_output_size", lambda: cfg["sensor"].get("output_size"))
                put("sensor_mode_bit_depth", lambda: cfg["sensor"].get("bit_depth"))
            put("colour_space", lambda: cfg.get("colour_space"))

        # Requested (as opposed to negotiated) settings, straight from the rig config.
        g["requested_resolution"] = list(self.resolution)
        g["requested_fps"] = self.fps
        if self.sensor_mode:
            g["requested_sensor_mode"] = list(self.sensor_mode)
        if self.bit_depth:
            g["requested_bit_depth"] = self.bit_depth
        g["auto_exposure"] = bool(self.auto_exposure)
        g["exposure_ms"] = float(self.exposure_ms)
        g["gain"] = float(self.gain)

        if encoder is not None:
            g["encoder_class"] = type(encoder).__name__
            for attr, key in (("bitrate", "encoder_bitrate_bps"),
                              ("framerate", "encoder_framerate"),
                              ("iperiod", "encoder_iperiod_frames"),
                              ("profile", "encoder_profile"),
                              ("repeat", "encoder_repeat_headers")):
                put(key, lambda a=attr: getattr(encoder, a))
        g["gop_s"] = float(self.gop_s)
        return g

    def _scaled_size(self, scale):
        """A (w,h) scaled from the recording resolution, clamped even and >=64 — the lores preview
        stream size (recording) and the downsampled preview-only size (experiment Live)."""
        mw, mh = self.resolution
        lw = max(64, (int(mw * scale) // 2) * 2)
        lh = max(64, (int(mh * scale) // 2) * 2)
        return (lw, lh)

    def _sensor_config_kwargs(self, cam) -> dict:
        """Extra create_video_configuration kwargs that PIN the sensor mode + raw bit depth.
        Empty dict = don't pin (legacy: libcamera infers the mode from the output size). Prefers
        picamera2's modern `sensor=` API; falls back to `raw=` with a Bayer format that encodes the
        bit depth on older builds. Which one the installed picamera2 takes is confirmed by the
        signature check, so this works across versions without a live probe."""
        if not (self.sensor_mode or self.bit_depth):
            return {}
        import inspect
        try:
            params = inspect.signature(cam.create_video_configuration).parameters
        except (TypeError, ValueError):
            params = {}
        if "sensor" in params:
            spec = {}
            if self.sensor_mode:
                spec["output_size"] = tuple(self.sensor_mode)
            if self.bit_depth:
                spec["bit_depth"] = int(self.bit_depth)
            return {"sensor": spec}
        # Older picamera2: pin via the raw stream. Size selects the mode; SRGGBn selects the depth.
        if self.sensor_mode:
            fmt = {8: "SRGGB8", 10: "SRGGB10_CSI2P", 12: "SRGGB12_CSI2P"}.get(
                self.bit_depth or 12, "SRGGB12_CSI2P")
            return {"raw": {"size": tuple(self.sensor_mode), "format": fmt}}
        return {}

    def _verify_sensor(self, cam):
        """Fail LOUDLY if a requested pin was not honoured. Silently recording the wrong field of
        view or depth is exactly the failure the pin exists to prevent, so a mismatch raises rather
        than falling back — the caller's try/except then releases the camera cleanly."""
        if not (self.sensor_mode or self.bit_depth):
            return
        try:
            sensor = cam.camera_configuration().get("sensor", {})
        except Exception:
            return
        got_size = tuple(sensor.get("output_size", ()) or ())
        got_depth = sensor.get("bit_depth")
        if self.sensor_mode and got_size and got_size != tuple(self.sensor_mode):
            raise RuntimeError(f"sensor-mode pin failed: requested {tuple(self.sensor_mode)}, "
                               f"got {got_size}")
        if self.bit_depth and got_depth is not None and int(got_depth) != int(self.bit_depth):
            raise RuntimeError(f"bit-depth pin failed: requested {self.bit_depth}-bit, "
                               f"got {got_depth}-bit")

    def _exposure_controls(self) -> dict:
        """libcamera exposure/gain controls for the current settings. Auto => let AEC/AGC run;
        manual => fix ExposureTime + AnalogueGain (low gain suppresses the rolling readout
        banding). Applied both at configure time (start_recording) and live (apply_exposure)."""
        if self.auto_exposure:
            return {"AeEnable": True}
        return {"AeEnable": False,
                "ExposureTime": int(self.exposure_ms * 1000),   # ms -> µs (libcamera ExposureTime is µs)
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

    def apply_exposure(self, auto_exposure=None, exposure_ms=None, gain=None) -> dict:
        """Update exposure settings (any arg left None is unchanged) and push them to the running
        camera. Returns the controls applied (empty if the camera isn't open)."""
        if auto_exposure is not None:
            self.auto_exposure = bool(auto_exposure)
        if exposure_ms is not None:
            self.exposure_ms = float(exposure_ms)
        if gain is not None:
            self.gain = float(gain)
        return self.set_live_controls(self._exposure_controls())

    def _on_frame(self, request):
        if self._recording:
            # frame_timestamps.npy columns: [frame_idx, host wall clock, SensorTimestamp].
            # Wall clock = time.time() (NTP-synced — aligns frames with HDF5 events, but
            # includes ~tens of ms of ISP pipeline latency + jitter). SensorTimestamp =
            # hardware stamp at start of frame readout in ns on CLOCK_BOOTTIME — accurate
            # frame spacing, no pipeline jitter; map to wall time via
            # offset = median(wall - sensor/1e9) over the session.
            try:
                md = request.get_metadata()
                sens_ns = md.get("SensorTimestamp", 0)
                # The APPLIED crop, once, from the first frame. libcamera may adjust the requested
                # rect (ISP minimums, aspect matching), so only the request metadata is the truth
                # about what field of view this file actually shows.
                if self._frame_idx == 0 and "ScalerCrop" in md:
                    self._geometry["scaler_crop_applied"] = self._jsonable(md["ScalerCrop"])
                    for k in ("ExposureTime", "AnalogueGain", "DigitalGain",
                              "FrameDuration", "ColourGains"):
                        if k in md:
                            self._geometry[f"applied_{k}"] = self._jsonable(md[k])
            except Exception:
                sens_ns = 0
            self._frame_log.append((self._frame_idx, time.time(), sens_ns))
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
            # Acquire the capture lock so we never close() while capture_array() is executing
            # (the libcamera-corruption / next-open-hang path). Bounded: if a capture is truly
            # wedged inside capture_array() the lock never frees, so force-close after the timeout
            # (a driver reset is then the only clean recovery — see /api/camera_reset).
            got = self._capture_lock.acquire(timeout=2.0)
            if not got:
                print("[camera] _release_camera: capture_lock timeout; force-closing", flush=True)
            try:
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
            finally:
                if got:
                    self._capture_lock.release()
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
                # measured_fps is the only frame rate that is always true: the SPS tag depends on
                # what the encoder was told, and the config is only a request.
                self._geometry["measured_fps"] = result["avg_fps"]
                self._geometry["frame_count"] = len(self._frame_log)
            # Sidecar, even if no frames were logged — knowing the geometry of a failed or empty
            # recording is still worth more than knowing nothing. Never let it break the stop.
            if self._geom_path and self._geometry:
                try:
                    import json
                    Path(self._geom_path).write_text(json.dumps(self._geometry, indent=1))
                    result["geometry_path"] = str(self._geom_path)
                except Exception as e:
                    print(f"[camera] could not write {self._geom_path}: {e}", flush=True)
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
                    # Capture under the lock so _release_camera can't close() mid-capture.
                    # Re-check state inside — teardown may have flipped _live/_cam while we waited.
                    with self._capture_lock:
                        if not self._live or self._cam is None:
                            break
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
        if self._geom_path and Path(self._geom_path).exists():
            files.append(str(self._geom_path))
        return files

    def close(self):
        self.stop_stream()
        # Route through stop_recording (which delegates to _release_camera): a generic close() on a
        # still-live recording — e.g. the release_devices abort/reset path — must still flush
        # frame_timestamps.npy, not just finalize the video. Idempotent for preview/already-stopped.
        self.stop_recording()
