# Control Pi (cheddar) — Notes

**Pi:** cheddar, 192.168.10.101, user `vruser`  
**Role:** Lick detection, reward delivery, camera, photodiode sync

---

## Files deployed to cheddar

```
~/rig/
  worker.py      ← main hardware control loop
  protocol.py    ← shared config dataclass (copied from Mac)
  config.yaml    ← session config (deployed per session)
```

## Hardware

### Lick Sensor — MPR121 Capacitive Touch
- **IC:** MPR121, I2C address 0x5A
- **Electrode:** #4 (configurable in rig JSON)
- **Polling rate:** 200 Hz (in lick_poll thread)
- **Library:** smbus2 (direct I2C register reads)
- **Connection:** I2C bus, lick spout connected to electrode 4
- Touch is detected by reading register 0x00/0x01 and checking bitmask

### Reward Solenoid
- **GPIO:** 18 (configurable in rig JSON `reward.pins.main.gpio`)
- **Control:** pigpio daemon (`pigpiod` must be running)
- **Calibration:** Pulse duration (ms) → volume (ul) curve in rig JSON
- **Delivery:** Threaded pulse (GPIO high → sleep → GPIO low)

### Camera — picamera2
- **Resolution:** 1280x720 (configurable)
- **FPS:** 50 (configurable)
- **Recording format:** H264 to file
- **Preview:** Flask MJPEG stream on port 5001
- **Frame timestamps:** Saved as numpy array alongside video
- **Video storage:** `/media/vruser/ssd/video/{session_id}/`

### Photodiode TTL Input
- **GPIO:** 24 (configurable in rig JSON)
- **Detection:** pigpio RISING_EDGE callback
- **Precision:** Hardware timestamps from pigpio (microsecond resolution)
- **Events:** Sends `SYNC_PULSE` with precise timestamp and pulse index to stim Pi
- **Purpose:** Frame sync validation between projector output and stim timing

## ZMQ Sockets (worker.py)

| Socket | Port | Direction | Purpose |
|--------|------|-----------|---------|
| DEALER | → stim:5570 | Sends to mozzarella | READY, LICK, REWARD_DONE, SYNC_PULSE |
| PULL   | 5572 | Mac pushes commands | REWARD, START_CAMERA, STOP_CAMERA |

## pigpio Setup

```bash
# Built from source (~/pigpio/), C library only
# Python bindings installed via pip:
pip install pigpio

# Daemon must be running:
sudo pigpiod

# Verify:
pigs t   # returns tick count if running
```

Note: `sudo make install` for pigpio fails on the Python step (distutils missing
in Python 3.11) — this is harmless, the C library installs fine.

## Known Issues

- `MONITOR_LICKS` and `MONITOR_PHOTODIODE` commands from experiment_ui.py
  are not yet handled in worker.py — needs implementation to enable
  hardware check buttons in the UI
- Camera `mjpeg_stream()` has a parameter mismatch — Flask route passes
  `preview_res` but the method signature doesn't accept it
- `lick_poll()` exception handling is too broad — could mask I2C bus hangs
- SSD must be mounted at `/media/vruser/ssd/` before camera recording
