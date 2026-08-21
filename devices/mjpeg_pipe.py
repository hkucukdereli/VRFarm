"""
devices/mjpeg_pipe.py

Shared machinery for subprocess-based MJPEG cameras (worldcam, naneye):

  MJPEGPipeSource — spawn a capture command whose stdout is a raw MJPEG byte
  stream, split it into JPEG frames in a reader thread, and hand each frame to
  a callback. Process isolation is the point: a capture-stack crash kills the
  subprocess, not pi_api.

  PipeCameraBase — a Device base class implementing the camera contract the
  rest of the system expects (start_preview / start_recording / stop_recording /
  mjpeg_stream + the _recording/_is_preview/_frame_idx attributes pi_api's
  status introspection reads). Subclasses supply the capture command + check().

Recording layout (per session): <output_dir>/<session_id>/<device name>/
  video.mjpeg            raw concatenated JPEG frames (container-copied to .avi
                         at consolidation — no transcode on the Pi)
  frame_timestamps.npy   Nx3 float64 [frame_idx, wall_t, 0.0] (no sensor stamp;
                         timestamps are pipe-arrival — see ts_source in metadata)
  camera_metadata.json   cmd, requested/measured fps, frame count, provenance

This module registers no device itself.
"""

from __future__ import annotations
import json
import subprocess
import threading
import time
from pathlib import Path

from devices.base import Device

_SOI = b"\xff\xd8"   # JPEG start-of-image
_EOI = b"\xff\xd9"   # JPEG end-of-image
_MAX_BUF = 8 * 1024 * 1024   # runaway-buffer cap (no EOI ever seen)


class MJPEGPipeSource:
    """Run `cmd`, split its stdout into JPEG frames, call on_frame(jpeg_bytes, t)."""

    def __init__(self, cmd, pkill_pattern=None):
        self.cmd = [str(c) for c in cmd]
        self.pkill_pattern = pkill_pattern
        self._proc = None
        self._reader = None
        self._stderr_reader = None
        self._stop = threading.Event()
        self._stderr_tail = []

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def stderr_tail(self):
        return "\n".join(self._stderr_tail[-10:])

    def start(self, on_frame):
        # Reap any orphan from a prior crash that could still hold the camera.
        if self.pkill_pattern:
            subprocess.run(["pkill", "-f", self.pkill_pattern],
                           capture_output=True, check=False)
            time.sleep(0.2)
        self._stop.clear()
        self._stderr_tail = []
        self._proc = subprocess.Popen(
            self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

        def _drain_stderr():
            for line in self._proc.stderr:
                try:
                    self._stderr_tail.append(line.decode(errors="replace").rstrip())
                except Exception:
                    pass
                if len(self._stderr_tail) > 50:
                    self._stderr_tail.pop(0)

        def _read():
            # Naive SOI/EOI scanning is safe for these sources: v4l2/libcamera MJPEG
            # output is baseline JPEG with no embedded-thumbnail false positives.
            buf = bytearray()
            stdout = self._proc.stdout
            while not self._stop.is_set():
                chunk = stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    soi = buf.find(_SOI)
                    if soi < 0:
                        # keep a possible half-received SOI marker at the tail
                        # (a chunk boundary can split \xff|\xd8)
                        if buf.endswith(b"\xff"):
                            del buf[:-1]
                        else:
                            del buf[:]
                        break
                    eoi = buf.find(_EOI, soi + 2)
                    if eoi < 0:
                        if soi > 0:
                            del buf[:soi]
                        if len(buf) > _MAX_BUF:   # corrupt stream — resync
                            del buf[:]
                        break
                    frame = bytes(buf[soi:eoi + 2])
                    del buf[:eoi + 2]
                    try:
                        on_frame(frame, time.time())
                    except Exception:
                        pass

        self._stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
        self._stderr_reader.start()
        self._reader = threading.Thread(target=_read, daemon=True)
        self._reader.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=timeout)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
                except Exception:
                    pass
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        self._proc = None
        self._reader = None


class PipeCameraBase(Device):
    """Camera-contract Device driven by an MJPEGPipeSource. Subclasses implement
    _build_cmd() and check(); init() must call super().init(...) first."""

    def init(self, rig_config: dict, task_params: dict):
        self.cfg = dict(rig_config or {})
        self.cfg.update(task_params or {})
        self.preview_fps = float(self.cfg.get("preview_fps", 10))
        self._source = None
        self._recording = False       # a pipeline is running (preview OR record)
        self._is_preview = True       # False only for a real session recording
        self._frame_idx = 0
        self._latest = None
        self._lock = threading.Lock()
        self._file = None
        self._ts = []                 # [(frame_idx, wall_t, 0.0), ...]
        self._out_dir = None
        self._t_start = None

    # subclass API -----------------------------------------------------------

    def _build_cmd(self):
        raise NotImplementedError

    def _pkill_pattern(self):
        return None

    # capture ----------------------------------------------------------------

    def _on_frame(self, jpeg, t):
        with self._lock:
            self._latest = jpeg
            if self._file is not None:
                self._file.write(jpeg)
                self._ts.append((self._frame_idx, t, 0.0))
            self._frame_idx += 1

    def _start_source(self):
        src = MJPEGPipeSource(self._build_cmd(), self._pkill_pattern())
        src.start(self._on_frame)
        # Fail loudly if the pipeline dies immediately (bad device node, busy camera):
        # give it a moment, then surface the subprocess's stderr as the error.
        time.sleep(0.8)
        if not src.running and self._frame_idx == 0:
            err = src.stderr_tail() or "capture process exited"
            src.stop()
            raise RuntimeError(f"{self.info.name} capture failed to start: {err[-300:]}")
        self._source = src

    def start_preview(self, downsample=False):
        # downsample is part of the shared camera contract; the preview is paced by
        # preview_fps in mjpeg_stream() instead, so it's accepted and ignored here.
        self._file = None
        self._ts = []
        self._frame_idx = 0
        self._t_start = time.time()
        self._recording = True
        self._is_preview = True
        self._start_source()

    def start_recording(self, session_id: str, output_dir: str):
        out = Path(output_dir) / session_id / self.info.name
        out.mkdir(parents=True, exist_ok=True)
        self._out_dir = out
        self._file = open(out / "video.mjpeg", "wb")
        self._ts = []
        self._frame_idx = 0
        self._t_start = time.time()
        self._recording = True
        self._is_preview = False
        try:
            self._start_source()
        except Exception:
            f, self._file = self._file, None
            try:
                f.close()
            except Exception:
                pass
            self._recording = False
            raise

    def stop_recording(self):
        """Stop the pipeline and flush the sidecars (no-op for a preview)."""
        if self._source is not None:
            self._source.stop()
            self._source = None
        t_end = time.time()
        result = {"frames": self._frame_idx}
        with self._lock:
            f, self._file = self._file, None
            ts, self._ts = self._ts, []
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
            try:
                import numpy as np
                np.save(str(self._out_dir / "frame_timestamps.npy"),
                        np.asarray(ts, dtype=np.float64).reshape(-1, 3))
                dur = max(1e-6, t_end - (self._t_start or t_end))
                meta = {
                    "device": self.info.name,
                    "cmd": " ".join(self._build_cmd()),
                    "resolution": self.cfg.get("resolution"),
                    "fps": self.cfg.get("fps"),
                    "measured_fps": round(len(ts) / dur, 2),
                    "frame_count": len(ts),
                    "duration_s": round(dur, 2),
                    "ts_source": "pipe_arrival",
                    "video_format": "mjpeg",
                }
                (self._out_dir / "camera_metadata.json").write_text(
                    json.dumps(meta, indent=2))
                result.update(meta)
            except Exception as e:
                result["sidecar_error"] = str(e)
        self._recording = False
        return result

    def close(self):
        self.stop_recording()
        self._latest = None

    # streaming --------------------------------------------------------------

    def mjpeg_stream(self):
        """Multipart MJPEG generator: latest frame, paced at preview_fps."""
        period = 1.0 / max(1.0, self.preview_fps)
        last = None
        while self._recording or self._latest is not None:
            with self._lock:
                frame = self._latest
            if frame is not None and frame is not last:
                last = frame
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + frame + b"\r\n")
            if not self._recording:
                break
            time.sleep(period)
