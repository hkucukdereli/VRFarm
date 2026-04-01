"""
cheddar/worker.py

Hardware worker — runs on cheddar.
Handles:
  - MPR121 lick detection (I2C)
  - Solenoid reward delivery (GPIO)
  - Camera recording (picamera2, optional)
  - Photodiode TTL input for stimulus frame sync (GPIO, optional)

Start via SSH from mac/setup.py, or manually:
  conda activate autopilot
  python worker.py --config config.yaml
"""

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import zmq

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from protocol import load_config


# ── MPR121 ─────────────────────────────────────────────────────────────────────

class MPR121:
    def __init__(self, i2c_address: int, electrode: int, bus: int = 1):
        import smbus2
        self.bus       = smbus2.SMBus(bus)
        self.addr      = i2c_address
        self.electrode = electrode
        self._init()

    def _init(self):
        addr = self.addr
        self.bus.write_byte_data(addr, 0x80, 0x63)
        time.sleep(0.02)
        for i in range(12):
            self.bus.write_byte_data(addr, 0x41 + i * 2, 12)
            self.bus.write_byte_data(addr, 0x42 + i * 2,  6)
        self.bus.write_byte_data(addr, 0x5C, 0x10)
        self.bus.write_byte_data(addr, 0x5D, 0x20)
        self.bus.write_byte_data(addr, 0x5E, 0x8F)

    def is_touched(self) -> bool:
        lo = self.bus.read_byte_data(self.addr, 0x00)
        hi = self.bus.read_byte_data(self.addr, 0x01)
        return bool(((hi << 8) | lo) & (1 << self.electrode))

    def close(self):
        self.bus.close()


# ── Solenoid ───────────────────────────────────────────────────────────────────

class Solenoid:
    def __init__(self, gpio_pin: int):
        import pigpio
        self.pin = gpio_pin
        self.pi  = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("pigpiod not running — sudo pigpiod")
        self.pi.set_mode(gpio_pin, pigpio.OUTPUT)
        self.pi.write(gpio_pin, 0)

    def pulse(self, duration_ms: float):
        t = threading.Thread(target=self._pulse_thread,
                             args=(duration_ms,), daemon=True)
        t.start()

    def _pulse_thread(self, duration_ms: float):
        self.pi.write(self.pin, 1)
        time.sleep(duration_ms / 1000.0)
        self.pi.write(self.pin, 0)

    def close(self):
        self.pi.write(self.pin, 0)
        self.pi.stop()


# ── Photodiode TTL input ───────────────────────────────────────────────────────

class Photodiode:
    """
    Monitors a GPIO pin for TTL rising edges from a photodiode.

    The photodiode is taped to a sync patch drawn in a fixed corner of the
    projector image. mozzarella draws this patch on the first stimulus frame
    and every N frames thereafter (set by photodiode_pulse_every_n_frames in config).

    Each rising edge timestamps a stimulus frame with sub-millisecond accuracy
    using pigpio hardware timestamps (microsecond resolution).

    Wiring:
      Photodiode signal → GPIO pin (photodiode_gpio in config)
      Signal must be 3.3V TTL — use a voltage divider if source is 5V.

    Frame reconstruction:
      pulse_idx=1 → stim frame 1 onset
      pulse_idx=2 → stim frame (1 + N) onset
      pulse_idx=3 → stim frame (1 + 2N) onset
      Between pulses, frame times are interpolated at the known frame rate.
    """

    def __init__(self, gpio_pin: int):
        import pigpio
        self.pin = gpio_pin
        self.pi  = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("pigpiod not running")
        self.pi.set_mode(gpio_pin, pigpio.INPUT)
        self.pi.set_pull_up_down(gpio_pin, pigpio.PUD_DOWN)
        self._cb        = None
        self._send_fn   = None
        self._active    = False
        self._pulse_idx = 0
        # Reference tick for wraparound-safe tick_us differences
        self._ref_tick  = None
        self._ref_time  = None

    def start(self, send_fn):
        import pigpio
        self._send_fn   = send_fn
        self._active    = True
        self._pulse_idx = 0
        self._ref_tick  = None
        self._cb = self.pi.callback(self.pin, pigpio.RISING_EDGE,
                                    self._on_edge)
        print(f"  Photodiode: monitoring GPIO{self.pin}")

    def reset_trial(self):
        """Call at START_TRIAL to reset pulse counter."""
        self._pulse_idx = 0
        self._ref_tick  = None
        self._ref_time  = None

    def stop(self):
        self._active = False
        if self._cb:
            self._cb.cancel()
            self._cb = None

    def _on_edge(self, gpio, level, tick):
        """pigpio callback — microsecond hardware timestamp."""
        if not self._active:
            return
        # Capture wall time for first pulse, use tick differences for rest
        wall_t = time.time()
        if self._ref_tick is None:
            self._ref_tick = tick
            self._ref_time = wall_t
        # tick is uint32 microseconds, wraps at 2^32 (~71 minutes)
        tick_diff_us = (tick - self._ref_tick) & 0xFFFFFFFF
        t_precise = self._ref_time + tick_diff_us / 1e6
        self._pulse_idx += 1
        if self._send_fn:
            self._send_fn({
                'event':     'SYNC_PULSE',
                't':         t_precise,
                'pulse_idx': self._pulse_idx,
            })

    def close(self):
        self.stop()
        self.pi.stop()


# ── Camera ─────────────────────────────────────────────────────────────────────

class Camera:
    def __init__(self):
        self._recording  = False
        self._cam        = None
        self._frame_log  = []
        self._frame_idx  = 0
        self._tslog_path = None

    def start(self, session_id: str, video_dir: str,
              resolution=(1280, 720), fps=50):
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FileOutput

        out_dir = Path(video_dir) / session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path         = out_dir / 'video.h264'
        self._tslog_path = out_dir / 'frame_timestamps.npy'
        self._frame_log  = []
        self._frame_idx  = 0

        self._cam = Picamera2()
        cfg = self._cam.create_video_configuration(
            main={'size': tuple(resolution), 'format': 'RGB888'},
            controls={'FrameRate': fps},
        )
        self._cam.configure(cfg)
        self._encoder   = H264Encoder(bitrate=15_000_000)
        self._output    = FileOutput(str(out_path))
        self._cam.pre_callback = self._on_frame
        self._recording = True
        self._cam.start_recording(self._encoder, self._output)
        print(f"  Camera: recording → {out_path} "
              f"({resolution[0]}x{resolution[1]} @ {fps}fps)")

    def _on_frame(self, request):
        if self._recording:
            self._frame_log.append((self._frame_idx, time.time()))
            self._frame_idx += 1

    def stop(self):
        if not self._recording:
            return
        self._recording = False
        try:
            self._cam.stop_recording()
            self._cam.close()
        except Exception as e:
            print(f"Camera stop error: {e}")
        if self._frame_log and self._tslog_path:
            arr = np.array(self._frame_log, dtype=np.float64)
            np.save(str(self._tslog_path), arr)
            n   = len(self._frame_log)
            dur = self._frame_log[-1][1] - self._frame_log[0][1]
            print(f"  Camera: {n} frames, {n/max(dur,1):.1f} fps avg → {self._tslog_path}")

    def mjpeg_stream(self, every_n=2):
        """
        Lightweight MJPEG preview using the existing camera instance.
        Captures JPEG from the recording stream every `every_n` frames.
        640x360 effective output via JPEG compression, ~7fps to browser.
        """
        import io
        n = 0
        while True:
            n += 1
            if n % every_n != 0:
                time.sleep(1 / 15)
                continue
            if self._cam is None or not self._recording:
                # Camera not started yet — send a blank placeholder
                time.sleep(0.1)
                continue
            try:
                buf = io.BytesIO()
                # Capture a still from the running camera (non-blocking)
                self._cam.capture_file(buf, format='jpeg',
                                       quality=50, signal_function=None)
                buf.seek(0)
                frame = buf.read()
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
                       + frame + b'\r\n')
            except Exception:
                time.sleep(0.1)
            time.sleep(1 / 15)

    def close(self):
        self.stop()


# ── Flask preview server ───────────────────────────────────────────────────────

def start_camera_flask(camera: Camera, port: int = 5001):
    try:
        from flask import Flask, Response
        app = Flask('camera_preview')

        @app.route('/camera')
        def video_feed():
            return Response(
                camera.mjpeg_stream(every_n=2),
                mimetype='multipart/x-mixed-replace; boundary=frame')

        threading.Thread(
            target=lambda: app.run(host='0.0.0.0', port=port,
                                   debug=False, threaded=True,
                                   use_reloader=False),
            daemon=True
        ).start()
        print(f"  Camera preview: http://0.0.0.0:{port}/camera "
              f"(640x360, ~7fps)")
    except Exception as e:
        print(f"  Camera Flask error: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)

    print("\n=== cheddar worker ===")
    print(f"Session: {cfg.session_id}")
    print("Initialising hardware...")

    # Lick sensor
    lick_sensor = MPR121(i2c_address=cfg.lick.i2c_address,
                         electrode=cfg.lick.electrode)
    print(f"  MPR121: electrode {cfg.lick.electrode} @ 0x{cfg.lick.i2c_address:02X}")

    # Solenoids
    solenoids = {}
    for name, pin_cfg in cfg.reward.pins.items():
        solenoids[name] = Solenoid(gpio_pin=pin_cfg.gpio)
        print(f"  Solenoid '{name}': GPIO{pin_cfg.gpio} ({pin_cfg.label})")

    # Camera (optional)
    camera = None
    if cfg.hardware.use_camera:
        camera = Camera()
        start_camera_flask(camera, port=5001)
    else:
        print("  Camera: disabled")

    # Photodiode (optional)
    photodiode = None
    if getattr(cfg.hardware, 'use_photodiode', False):
        pd_gpio = getattr(cfg.hardware, 'photodiode_gpio', 24)
        try:
            photodiode = Photodiode(gpio_pin=pd_gpio)
            print(f"  Photodiode: GPIO{pd_gpio} (enabled)")
        except Exception as e:
            print(f"  Photodiode: FAILED ({e})")
            photodiode = None
    else:
        print("  Photodiode: disabled")

    # ZMQ
    ctx    = zmq.Context()
    dealer = ctx.socket(zmq.DEALER)
    dealer.connect(
        f"tcp://{cfg.network.stim_ip}:{cfg.network.worker_port}")
    print(f"\nZMQ: → mozzarella:{cfg.network.worker_port}")

    # Direct command socket — Mac sends REWARD/etc without going via mozzarella
    cmd_pull = ctx.socket(zmq.PULL)
    cmd_pull.bind(f"tcp://*:{cfg.network.control_port}")
    print(f"ZMQ: command PULL on :{cfg.network.control_port}")

    # Monitor PUB socket — Mac subscribes for direct lick/photodiode events
    monitor_pub = ctx.socket(zmq.PUB)
    monitor_port = cfg.network.control_port + 1  # 5573
    monitor_pub.bind(f"tcp://*:{monitor_port}")
    print(f"ZMQ: monitor PUB on :{monitor_port}")

    monitoring_licks = False
    monitoring_photodiode = False

    send_lock = threading.Lock()
    def send(msg: dict):
        with send_lock:
            dealer.send_json(msg)
            # Also publish to monitor PUB if monitoring is active
            evt = msg.get('event')
            if monitoring_licks and evt == 'LICK':
                monitor_pub.send_json(msg)
            elif monitoring_photodiode and evt == 'SYNC_PULSE':
                monitor_pub.send_json(msg)
            elif evt in ('CHECK_LICK_OK', 'CHECK_LICK_FAIL',
                         'CHECK_CAMERA_OK', 'CHECK_CAMERA_FAIL'):
                monitor_pub.send_json(msg)

    # Start photodiode before announcing ready
    if photodiode:
        photodiode.start(send_fn=send)

    # Announce ready
    send({'event': 'READY', 'version': '1.0',
          'has_camera':     camera is not None,
          'has_photodiode': photodiode is not None})
    print("Sent READY to mozzarella\n")

    # Lick polling thread (200 Hz)
    stop_event  = threading.Event()
    was_touched = False

    def lick_poll():
        nonlocal was_touched
        while not stop_event.is_set():
            try:
                touched = lick_sensor.is_touched()
                if touched and not was_touched:
                    send({'event': 'LICK', 't': time.time()})
                was_touched = touched
            except Exception as e:
                print(f"Lick error: {e}")
            time.sleep(0.005)

    threading.Thread(target=lick_poll, daemon=True).start()
    print("Lick polling: 200 Hz")
    print("Waiting for commands...\n")

    # Command loop
    poller = zmq.Poller()
    poller.register(dealer, zmq.POLLIN)
    poller.register(cmd_pull, zmq.POLLIN)

    try:
        while True:
            socks = dict(poller.poll(timeout=200))
            if dealer in socks:
                msg = dealer.recv_json()
            elif cmd_pull in socks:
                msg = cmd_pull.recv_json()
            else:
                continue
            cmd = msg.get('cmd')

            if cmd == 'REWARD':
                pin    = msg.get('pin', 18)
                dur_ms = msg.get('duration_ms', 20)
                sol    = next((s for s in solenoids.values()
                               if s.pin == pin), None)
                if sol:
                    sol.pulse(dur_ms)
                    send({'event': 'REWARD_DONE', 't': time.time()})
                    print(f"  REWARD: GPIO{pin} {dur_ms:.1f}ms")
                else:
                    print(f"  No solenoid on GPIO{pin}")

            elif cmd == 'START_CAMERA':
                if camera:
                    camera.start(
                        session_id = msg.get('session_id', cfg.session_id),
                        video_dir  = msg.get('video_dir', cfg.data.video_dir),
                        resolution = msg.get('resolution',
                                             cfg.hardware.camera_resolution),
                        fps        = msg.get('fps', cfg.hardware.camera_fps),
                    )
                    send({'event': 'CAMERA_STARTED'})

            elif cmd == 'STOP_CAMERA':
                if camera:
                    camera.stop()
                    send({'event': 'CAMERA_STOPPED'})

            elif cmd == 'START_TRIAL':
                if photodiode:
                    photodiode.reset_trial()

            elif cmd == 'CHECK_LICK':
                try:
                    touched = lick_sensor.is_touched()
                    send({'event': 'CHECK_LICK_OK',
                          'touched': touched})
                    print(f"  CHECK_LICK: OK (touched={touched})")
                except Exception as e:
                    send({'event': 'CHECK_LICK_FAIL',
                          'error': str(e)})
                    print(f"  CHECK_LICK: FAIL ({e})")

            elif cmd == 'CHECK_CAMERA':
                ok = False
                error = ''
                if camera is None:
                    error = 'Camera not initialized (use_camera is false)'
                else:
                    try:
                        from picamera2 import Picamera2
                        # Just verify picamera2 can list cameras
                        cam_list = Picamera2.global_camera_info()
                        if cam_list:
                            ok = True
                        else:
                            error = 'No cameras detected'
                    except Exception as e:
                        error = str(e)
                if ok:
                    send({'event': 'CHECK_CAMERA_OK'})
                    print(f"  CHECK_CAMERA: OK")
                else:
                    send({'event': 'CHECK_CAMERA_FAIL',
                          'error': error})
                    print(f"  CHECK_CAMERA: FAIL ({error})")

            elif cmd == 'MONITOR_LICKS':
                monitoring_licks = msg.get('enable', False)
                print(f"  Lick monitoring: {'ON' if monitoring_licks else 'OFF'}")

            elif cmd == 'MONITOR_PHOTODIODE':
                monitoring_photodiode = msg.get('enable', False)
                print(f"  Photodiode monitoring: {'ON' if monitoring_photodiode else 'OFF'}")

            elif cmd == 'STOP':
                print("STOP received")
                break

    except KeyboardInterrupt:
        print("\nKeyboard interrupt")
    finally:
        stop_event.set()
        if photodiode:
            photodiode.close()
        if camera:
            camera.close()
        for sol in solenoids.values():
            sol.close()
        lick_sensor.close()
        monitor_pub.close()
        dealer.close()
        ctx.term()
        print("Worker shutdown complete")


if __name__ == '__main__':
    main()
