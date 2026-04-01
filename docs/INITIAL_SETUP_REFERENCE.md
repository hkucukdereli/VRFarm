# VRFarm — Environment Setup Reference

**Project:** VRFarm  
**Last updated:** 2026-03-30  
**Rig naming:** `<cheese>_stim` (projector Pi) + `<cheese>_control` (GPIO Pi)  
**Current rig:** mozzarella (stim) + cheddar (control)

---

## Overview

| Machine | Role | Conda env | IP |
|---|---|---|---|
| Mac (balthazar) | UI + data storage | `vrfarm` | 192.168.10.1 |
| mozzarella | Stim Pi — PsychoPy + ZMQ | `rig` | 192.168.10.102 |
| cheddar | Control Pi — lick/reward/camera | `rig` | 192.168.10.101 |

---

## Mac Setup

### Conda environment

```bash
conda create -n vrfarm python=3.11
conda activate vrfarm
pip install flask paramiko pyzmq scipy matplotlib numpy h5py pyyaml
```

### Project folder

```
~/VRFarm/
├── rig_setup/
│   └── rig_setup_ui.py          # one-time rig setup UI (localhost:4999)
├── experiment/
│   ├── experiment_ui.py      # experiment UI (localhost:5000)
│   └── config/
│       └── HK001_day07.yaml  # session configs go here
├── shared/
│   └── protocol.py           # config dataclass (copied to Pis automatically)
├── stim/
│   ├── task.py               # trial loop (deployed to stim Pi)
│   └── stim_generator.py     # pre-renders stimuli (deployed to stim Pi)
├── control/
│   └── worker.py             # lick/reward/camera (deployed to control Pi)
├── calibration/
│   ├── rig_geometry.yaml     # screen physical measurements
│   ├── compute_warp_map.py   # generates warp_map.npz
│   ├── display_test_patches.py
│   ├── fit_luminance_correction.py
│   └── validate_calibration.py
├── data/
│   └── subjects/             # HK001.json etc — auto-created
└── rig.json                  # Pi connection info — saved by rig_setup_ui.py
```

### Passwordless SSH (required for auto-deploy)

```bash
ssh-keygen -t ed25519          # skip if you already have a key
ssh-copy-id vruser@192.168.10.101   # cheddar
ssh-copy-id vruser@192.168.10.102   # mozzarella

# Test
ssh vruser@192.168.10.101 echo "cheddar OK"
ssh vruser@192.168.10.102 echo "mozzarella OK"
```

---

## Mozzarella (Stim Pi) Setup

### System packages

```bash
sudo apt install libx11-dev libxext-dev libxi-dev -y
# X11 + GPU (for PsychoPy via modesetting driver):
sudo apt install -y xserver-xorg-core libgl1-mesa-dri libglu1-mesa mesa-utils
```

### X11 configuration

Must use modesetting driver (not fbdev) to get 24-bit visuals and V3D hardware GL.
The raw framebuffer (/dev/fb0) is 16-bit under FKMS+DPI, which breaks PsychoPy.

```bash
sudo tee /etc/X11/xorg.conf > /dev/null << 'EOF'
Section "Device"
    Identifier "drm"
    Driver "modesetting"
EndSection

Section "Screen"
    Identifier "Default Screen"
    Device "drm"
    DefaultDepth 24
    SubSection "Display"
        Depth 24
    EndSubSection
EndSection
EOF
```

### Conda environment

```bash
conda create -n rig python=3.11
conda activate rig
uv pip install pyzmq h5py pyyaml flask numpy scipy
# psychopy without psychtoolbox (avoids X11 build issues):
uv pip install psychopy --no-deps
# pyglet MUST be pinned to 1.5.27 — 2.x fails to create GL context on Pi FKMS
uv pip install 'pyglet==1.5.27' pillow moviepy imageio imageio-ffmpeg pyopengl \
  requests packaging psutil six json_tricks pandas pyserial \
  python-bidi arabic-reshaper freetype-py
```

### Folder structure on Pi

```
~/rig/                  ← Python files deployed here by experiment_ui.py
~/rig/calibration/      ← warp_map.npz and calibration scripts (copy manually once)
~/stims/                ← pre-generated stimuli per session (auto-created)
```

### One-time manual file copy (calibration)

```bash
# From Mac:
scp ~/VRFarm/calibration/rig_geometry.yaml      vruser@192.168.10.102:~/rig/calibration/
scp ~/VRFarm/calibration/compute_warp_map.py    vruser@192.168.10.102:~/rig/calibration/
scp ~/VRFarm/calibration/display_test_patches.py vruser@192.168.10.102:~/rig/calibration/
scp ~/VRFarm/calibration/fit_luminance_correction.py vruser@192.168.10.102:~/rig/calibration/
scp ~/VRFarm/calibration/validate_calibration.py vruser@192.168.10.102:~/rig/calibration/
```

### Projector startup (before each session)

The DLP projector connects via DPI GPIO — must set pin modes and initialize DLPC3436
before X11 can display anything. Run `~/rig/start_projector.sh` or do it manually:

```bash
# 1. Set GPIO 0-21 to ALT2 (DPI parallel video)
for i in $(seq 0 21); do pinctrl set $i a2; done
# 2. Enable video buffer
pinctrl set 25 op dh
# 3. Initialize DLPC3436 for external video input
cd ~/dlp && conda run -n rig python3 init_parallel_mode.py
# 4. Start X11 (must be root)
sudo X :0 -ac > /tmp/xorg.log 2>&1 &
sleep 3
export DISPLAY=:0
```

The script `~/rig/start_projector.sh` does all of the above.

### Generate warp map (once per rig, after calibration files are copied)

```bash
# On mozzarella (after projector startup):
conda activate rig
cd ~/rig/calibration
python compute_warp_map.py
# With validation plot:
DISPLAY=:0 python compute_warp_map.py --validate
```

---

## Cheddar (Control Pi) Setup

### System packages

```bash
sudo apt install libcap-dev -y
```

### Conda environment

```bash
conda create -n rig python=3.11
conda activate rig
uv pip install pyzmq smbus2 pyyaml flask numpy picamera2
```

### pigpio (build from source — no Pi wheel on PyPI)

```bash
cd ~
git clone https://github.com/joan2937/pigpio.git
cd pigpio && make && sudo make install
sudo ldconfig
# Python bindings from PyPI (distutils missing in 3.11, so skip make install for Python):
pip install pigpio
# Verify:
sudo pigpiod
python3 -c "import pigpio; pi = pigpio.pi(); print('connected:', pi.connected); pi.stop()"
```

### Auto-start pigpiod on boot

```bash
# Add to /etc/rc.local before "exit 0":
sudo nano /etc/rc.local
# Add line: /usr/bin/pigpiod
```

Or create a systemd service:

```bash
sudo tee /etc/systemd/system/pigpiod.service > /dev/null << 'EOF'
[Unit]
Description=pigpio daemon
After=network.target

[Service]
ExecStart=/usr/local/bin/pigpiod -l
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable pigpiod
sudo systemctl start pigpiod
```

### SSD mount for video

```bash
# Find device
lsblk

# Format (if new SSD — WARNING: erases data)
sudo mkfs.ext4 /dev/sda1

# Mount
sudo mkdir -p /media/vruser/ssd
sudo mount /dev/sda1 /media/vruser/ssd
sudo chown vruser:vruser /media/vruser/ssd

# Auto-mount on boot
echo "/dev/sda1 /media/vruser/ssd ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab

# Verify
df -h /media/vruser/ssd
```

### Folder structure on Pi

```
~/rig/                      ← Python files deployed here automatically
/media/vruser/ssd/video/    ← video recordings per session
```

---

## NTP Clock Sync (one-time, all machines)

All timestamps (licks, rewards, camera frames, stim onset) must share a
common clock. Mac acts as NTP server, Pis sync to it.

### Mac — enable NTP

```bash
sudo systemsetup -setnetworktimeserver time.apple.com
sudo systemsetup -setusingnetworktime on
```

### Each Pi — sync to Mac

```bash
sudo apt install chrony -y
sudo tee /etc/chrony/sources.d/mac.conf > /dev/null << 'EOF'
server 192.168.10.1 iburst prefer minpoll 4 maxpoll 4
EOF
sudo systemctl restart chrony
# Wait ~30 seconds, then verify offset is <1ms:
chronyc tracking
```

---

## Network Layout

```
Gigabit switch (local — no internet)
├── Mac balthazar     192.168.10.1   (en5 ethernet)
├── mozzarella stim   192.168.10.102 (eth0 static)
└── cheddar control   192.168.10.101 (eth0 static)

All Pis also on institute WiFi for internet access (wlan0).
Experiment traffic stays on ethernet switch only.
```

### Static IP setup on Pis (if not already done)

```bash
sudo nmcli con add type ethernet ifname eth0 con-name eth-static \
  ip4 192.168.10.10X/24        # 101 for cheddar, 102 for mozzarella
sudo nmcli con up eth-static
```

---

## Running VRFarm

### One-time rig setup

```bash
conda activate vrfarm
cd ~/VRFarm
python rig_setup/rig_setup_ui.py
# Opens http://localhost:4999
# Add Pi names/IPs, click "Setup All"
```

### Each session

```bash
conda activate vrfarm
cd ~/VRFarm
python experiment/experiment_ui.py --config experiment/config/HK001_day07.yaml
# Opens http://localhost:5000
```

---

## Dependency Summary

| Package | Mac | Stim Pi | Control Pi |
|---|---|---|---|
| Python | 3.11 | 3.11 | 3.11 |
| flask | ✓ | ✓ | ✓ |
| pyzmq | ✓ | ✓ | ✓ |
| numpy | ✓ | ✓ | ✓ |
| scipy | ✓ | ✓ | — |
| matplotlib | ✓ | — | — |
| h5py | ✓ | ✓ | — |
| pyyaml | ✓ | ✓ | ✓ |
| paramiko | ✓ | — | — |
| psychopy | — | ✓ (--no-deps) | — |
| pyglet | — | ✓ (==1.5.27) | — |
| pandas | — | ✓ | — |
| pyserial | — | ✓ | — |
| python-bidi | — | ✓ | — |
| arabic-reshaper | — | ✓ | — |
| freetype-py | — | ✓ | — |
| six | — | ✓ | — |
| json_tricks | — | ✓ | — |
| pillow | — | ✓ | — |
| moviepy | — | ✓ | — |
| imageio | — | ✓ | — |
| imageio-ffmpeg | — | ✓ | — |
| pyopengl | — | ✓ | — |
| requests | — | ✓ | — |
| packaging | — | ✓ | — |
| psutil | — | ✓ | — |
| smbus2 | — | — | ✓ |
| pigpio | — | — | ✓ (source) |
| picamera2 | — | — | ✓ |
| xserver-xorg-core (apt) | — | ✓ | — |
| libgl1-mesa-dri (apt) | — | ✓ | — |
| libglu1-mesa (apt) | — | ✓ | — |
| mesa-utils (apt) | — | ✓ | — |
| libcap-dev (apt) | — | — | ✓ |
| libx11-dev (apt) | — | ✓ | — |
| libxext-dev (apt) | — | ✓ | — |
| libxi-dev (apt) | — | ✓ | — |
| chrony (apt) | — | ✓ | ✓ |

---

## Troubleshooting Quick Reference

| Problem | Fix |
|---|---|
| SSH connect fails | `ssh-copy-id vruser@<ip>` |
| pigpiod not found | `sudo pigpiod` or check `/usr/local/bin/pigpiod` |
| pigpio Python import fails | `sudo ldconfig` then `pip install pigpio` |
| picamera2 build fails | `sudo apt install libcap-dev -y` |
| psychtoolbox build fails | `uv pip install psychopy --no-deps` + manual deps |
| pyglet GL context fail | Pin `pyglet==1.5.27` (2.x fails under FKMS+DPI) |
| "failed to create drawable" | Use modesetting X driver, not fbdev (see xorg.conf) |
| Clock drift between Pis | `sudo systemctl restart chrony` on each Pi |
| SSD not mounting | Check `lsblk`, verify `/etc/fstab` entry |
| Warp map not found | Run `python compute_warp_map.py` on mozzarella |
| Projector black/no image | Run `~/rig/start_projector.sh` or init GPIO ALT2 + DLP |
