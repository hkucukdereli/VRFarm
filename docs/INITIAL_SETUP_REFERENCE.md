# VRFarm — Environment Setup Reference

**Project:** VRFarm
**Last updated:** 2026-08-11
**Current rig:** cheese (cheddar = Leader, mozzarella = Follower)

This is the first-time **rig bring-up** reference: hardware, network/IPs, OS, conda
envs, package installs, pigpiod, projector startup, systemd service, SSH keys, gotchas.
For the **controller** machine (the Mac/Linux box running the UIs) the authoritative doc
is `docs/CONTROLLER_SETUP.md`; the Mac section here is a short version of it.

Most of the per-Pi package/service work below is automated by the setup UI's **Install**
button (`setup/app.py` -> `api_install_pi`). Do it by hand only when reflashing an SD card
or debugging Install — the manual steps document what Install does under the hood.

---

## Overview

| Machine | Role | Conda env | IP |
|---|---|---|---|
| Controller (Mac/Ubuntu) | UI + data storage | `vrfarm` | 192.168.10.1 |
| cheddar | Leader Pi — imperative trial loop + GPIO devices | `rig` | 192.168.10.101 |
| mozzarella | Follower Pi — pygame display | `rig` | 192.168.10.102 |

Both Pis run **Debian 13 (trixie)**, user `vruser`, conda base at `~/miniforge3`.
The `rig` env Python **must match the system Python** (3.13 on trixie) — see the
conda note under each Pi.

> **Bringing up a new or reflashed Pi?** Start with
> [Onboarding a new Pi onto the switch](#onboarding-a-new-pi-onto-the-switch) (static IP +
> SSH key), then run the setup UI's **Install → Deploy**.

---

## Mac / Controller Setup

Short version; full detail (Ubuntu netplan, firewall, `$VRFARM_DATA_DIR`) is in
`docs/CONTROLLER_SETUP.md`.

### Conda environment

```bash
conda create -n vrfarm python=3.11 -y
conda activate vrfarm
pip install flask requests scipy matplotlib numpy h5py pyyaml
```

`paramiko` and `pyzmq` are no longer needed (replaced by REST API + UDP). `picamera2`
and `pigpio` live on the Pis, not here.

### Project folder

```
~/VRFarm/
├── experiments/*.yaml                 <- task/paradigm configs (stimulus/reward/session/adaptive)
├── rigs/cheese.yaml                    <- rig hardware config (pins, cal, roles, IPs)
├── devices/                            <- device abstraction layer (one file per device)
├── engine/                             <- trial loop: leader.py / follower.py
├── app/app.py                          <- experiment UI (localhost:5000)
├── setup/app.py                        <- rig setup UI (localhost:4999)
├── pi_api/api.py                       <- Pi REST API (deployed to each Pi)
├── shared/                             <- config loaders, stim generator, consolidate
├── display_calibration/                <- projector geometry + warp scripts, start_projector.sh
├── data/                               <- transferred session data (auto-created; $VRFARM_DATA_DIR override)
└── docs/                               <- documentation
```

Session data lands in `$VRFARM_DATA_DIR` (if set) else `~/VRFarm/data`; there is no
controller path in the rig yaml (see `docs/CONTROLLER_SETUP.md` §3b).

### Passwordless SSH (required for the setup UI's deploy/calibrate)

```bash
ssh-keygen -t ed25519          # skip if you already have a key
ssh-copy-id vruser@192.168.10.101   # cheddar
ssh-copy-id vruser@192.168.10.102   # mozzarella

# Test — seeds known_hosts, which the non-interactive setup UI needs
ssh vruser@192.168.10.101 echo "cheddar OK"
ssh vruser@192.168.10.102 echo "mozzarella OK"
```

The experiment-run UI (`app/app.py`) uses no SSH — only the setup UI does (deploy,
warp push, reboot, calibrate). Every new controller must add **its own** key to the Pis.

---

## Cheddar (Leader Pi) Setup

Everything below is what the setup UI **Install** does automatically; do it by hand only
for a fresh SD card. cheddar owns the behavioral devices: lick sensor, reward,
photodiode (Teensy-fed sync pulse), camera, and the running-wheel encoder.

### Conda environment

The `rig` env Python must equal the **system** Python (trixie = 3.13) — the camera
bindings (`python3-libcamera`/`python3-picamera2`) are apt-built for the system Python
and symlinked into the env, so a mismatch breaks `import picamera2`.

```bash
conda create -n rig python=$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")') -y
conda activate rig
```

### Python packages (pip, into the `rig` env)

Base plus the packages for whichever devices this Pi runs (Install picks these per the
rig's device list):

```bash
pip install flask pyyaml numpy h5py       # base + leader (h5py)
pip install smbus2                          # lick_sensor
pip install pigpio scipy                    # reward      (Pi 4 / main;  Pi 5 -> lgpio scipy)
pip install pigpio                          # photodiode  (Pi 4 / main;  Pi 5 -> lgpio)
pip install pillow simplejpeg piexif av     # camera (plus h5py)
```

> **GPIO library is board-dependent.** **Pi 4** (`main`): reward/photodiode use **`pigpio`** +
> the `pigpiod` daemon (below). **Pi 5** (`dev-pi5`): they use **`lgpio`** — **no daemon**, it
> opens `/dev/gpiochip0` (the RP1 40-pin header) directly. Note **`pip install lgpio` builds
> from source and needs `swig` + `python3-dev`** (otherwise it dies with `command 'swig' failed`),
> so on a Pi install it one of two ways:
> - `sudo apt install -y swig python3-dev && pip install lgpio` — build it in the env, **or**
> - `sudo apt install -y python3-lgpio` then symlink it in like the camera bindings:
>   `SITE=$(python3 -c 'import site;print(site.getsitepackages()[0])')` then
>   `ln -sf /usr/lib/python3/dist-packages/lgpio.py /usr/lib/python3/dist-packages/_lgpio*.so "$SITE"/`
>   (rig env Python must == system Python, same rule as the camera bindings).
>
> Verify either way: `python3 -c "import lgpio; h=lgpio.gpiochip_open(0); print('ok',h); lgpio.gpiochip_close(h)"`
> (`gpiodetect` shows `gpiochip0 [pinctrl-rp1]`). See `docs/PI5_LEADER_FEASIBILITY.md`.

### Camera bindings (apt + symlink, only if camera enabled)

Not a pip install — the bindings are apt packages symlinked into the conda env:

```bash
sudo apt-get install -y python3-libcamera python3-picamera2
# Install then symlinks libcamera/picamera2/pykms/pidng/videodev2/prctl into the rig
# env's site-packages. The env Python minor version MUST equal system Python or the
# compiled .so bindings have the wrong ABI and import fails.
```

### pigpiod (reward / photodiode) — Pi 4 only

> **Skip this whole section on a Pi 5** (`dev-pi5`): `lgpio` needs no daemon — it opens
> `/dev/gpiochip0` directly — so there is no `pigpiod` to build or enable. `pip install lgpio`
> is all the GPIO setup a Pi 5 Leader needs.

The setup UI Install runs `sudo apt install pigpio-tools` and enables the `pigpiod`
service. On **trixie the packaged `pigpio` may be unavailable**, in which case build the
daemon from source:

```bash
cd ~
git clone https://github.com/joan2937/pigpio.git
cd pigpio && make && sudo make install       # the Python step fails (distutils gone) — harmless
sudo ldconfig
```

The Python client is `pip install pigpio` (already installed for reward/photodiode above).
Verify:

```bash
sudo pigpiod
python3 -c "import pigpio; pi = pigpio.pi(); print('connected:', pi.connected); pi.stop()"
```

### Auto-start pigpiod on boot

Install enables the service. If you built from source, install the unit yourself
(daemon lives at `/usr/local/bin/pigpiod`; the apt build uses `/usr/bin/pigpiod`):

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

Camera video (H.264) writes to the rig yaml's `data.video_dir`. The default is
`/media/vruser/ssd/video` (an external SSD, because cheddar's SD card is too full to
hold video); the current `rigs/cheese.yaml` points it at `/home/vruser/data` instead.
If you use the SSD:

```bash
sudo mkdir -p /media/vruser/ssd
sudo mount /dev/sda1 /media/vruser/ssd
sudo chown vruser:vruser /media/vruser/ssd
echo "/dev/sda1 /media/vruser/ssd ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
```

### Folder structure on Pi

```
~/rig/                      <- code deployed here (setup UI Deploy)
~/data/<subj>/<subj_date>/<session_id>/   <- HDF5 + sidecars per session (transferred after)
<data.video_dir>/<subj>/<subj_date>/      <- video recordings
```

At **Transfer** the sidecars are folded into one consolidated `<session_id>.h5`
(see `docs/DATA_FORMAT.md`).

---

## Mozzarella (Follower Pi) Setup

mozzarella runs only the pygame display (over the DLP projector). Install handles the
packages, code, and systemd service; the manual steps here are for a fresh card.

### System packages (X11)

```bash
sudo apt install -y libx11-dev libxext-dev libxi-dev \
  xserver-xorg-core libgl1-mesa-dri libglu1-mesa mesa-utils
```

### Conda environment

```bash
conda create -n rig python=$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")') -y
conda activate rig
pip install flask pyyaml numpy pygame
```

PsychoPy is not used — pygame handles all rendering.

### X11 configuration

Must use the modesetting driver for 24-bit visuals and V3D hardware GL.

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

### Projector startup (before each display session)

The DLP projector connects via DPI GPIO. Bring-up is `~/rig/start_projector.sh`
(deployed by the setup UI, or triggered over REST via `/api/init_projector` — the setup
UI's **Init Devices** and the Display card's **Reinit** button both call it). The script:

```bash
# 1. Set GPIO 0-21 to ALT2 (DPI parallel video output)
for i in $(seq 0 21); do pinctrl set $i a2; done
# 2. Enable video buffer (GPIO 25 high)
pinctrl set 25 op dh
# 3. Initialize DLPC3436 for external parallel video input
source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig
cd ~/dlp && python3 init_parallel_mode.py
# 4. Restart X11
pkill -f 'Xorg :0' 2>/dev/null || true; sleep 1
sudo X :0 -ac -s off -dpms > /tmp/xorg.log 2>&1 &
sleep 3
export DISPLAY=:0
```

`start_projector.sh` calls `~/dlp/init_parallel_mode.py`, from the **vendored TI DLPC SDK
in this repo's `dlp/`**, which the setup UI pushes to `~/dlp/` via scp on Deploy (follower
only — `~/dlp` sits outside `~/rig`, so it rides scp, not the REST upload). A reflashed card
gets it back on the next Deploy.

**Two prerequisites**, both handled by **Install → Deploy** — do them before running the script
by hand, or `cd ~/dlp` fails and the DLPC never initializes:
- **`~/dlp/` must exist** — it lands on the first **Deploy** (follower). A fresh card has no
  `~/dlp` until then.
- **I²C must be enabled** — `init_parallel_mode.py` drives the DLPC over `/dev/i2c-*` (raw
  `fcntl.ioctl` in `dlp/linuxi2c.py` — **no `smbus2` needed**). Install runs
  `sudo raspi-config nonint do_i2c 0` for the follower; by hand: enable I²C in `raspi-config`.

### Folder structure on Pi

```
~/rig/                          <- code deployed here (setup UI Deploy)
~/rig/start_projector.sh        <- projector bring-up (deployed)
~/rig/calibration/              <- warp_map.npz, rig_geometry.yaml, calib tools
~/rig/stims/<session_id>/       <- pre-generated stim NPZ (pushed by Leader at deploy)
~/dlp/                          <- DLPC init SDK (vendored, pushed via scp on Deploy)
```

---

## Deploy Code to Pis

Use the rig setup UI:

```bash
conda activate vrfarm
cd ~/VRFarm
python setup/app.py    # opens localhost:4999
```

1. **Load Rig** (`cheese`)
2. **Connect** — checks SSH + REST API on each Pi (`/api/status`)
3. **Install** (first time, via SSH): ensures the `rig` conda env matched to system
   Python, installs the device-specific apt/pip packages, symlinks the camera bindings,
   uploads the code, installs + enables the `vrfarm` systemd service, and on a **Pi 4**
   enables `pigpiod` (a **Pi 5**/`dev-pi5` install uses `lgpio` instead, no daemon).
4. **Deploy** (via REST API): re-uploads the code files and restarts `pi_api` so the new
   code runs; the follower also gets `start_projector.sh`, the calibration tools, and
   `~/dlp/` via scp.
5. **Init Devices** — initializes each enabled device on its Pi (projector + display,
   lick, reward, camera, photodiode, encoder).

Any parameter change in the UI invalidates a deploy (Go grays out in the experiment UI).

### systemd service

Each Pi runs `pi_api/api.py` as the `vrfarm` service (unit `pi_api/vrfarm.service`) on
port 5080, `Restart=always`, launched with the `rig` env Python from `/home/vruser/rig`:

```bash
# Status / restart:
ssh vruser@192.168.10.101 "sudo systemctl status vrfarm"
ssh vruser@192.168.10.101 "sudo systemctl restart vrfarm"
```

The setup UI can restart `pi_api` remotely (`/api/restart`, used automatically after
Deploy) — it self-kills and systemd respawns it with the new code.

---

## Running Experiments

```bash
conda activate vrfarm
cd ~/VRFarm
python app/app.py      # experiment UI, localhost:5000
```

Workflow: Load Rig -> Connect -> Deploy -> Go -> Stop -> Transfer.
An optional `VRFARM_SLACK_WEBHOOK` env var enables Slack start/end/timeout notifications.
See `docs/EXPERIMENT_PROTOCOL.md` for the full protocol.

---

## NTP Clock Sync

All timestamps are `time.time()` Unix seconds and must share a common clock.

### Each Pi — sync to internet NTP

```bash
sudo apt install chrony -y
chronyc tracking
# "System time" should be <1ms offset
```

The Pis reach NTP over their WiFi (`wlan0`); the wired experiment switch has no route out.

---

## Network Layout

```
Gigabit switch (experiment traffic)
├── Controller        192.168.10.1   (wired NIC; any .x except .101/.102)
├── cheddar           192.168.10.101 (eth0 static) — Leader
└── mozzarella        192.168.10.102 (eth0 static) — Follower

Both Pis also on institute WiFi (wlan0) for internet/NTP; WiFi stays the default route.
```

UDP / TCP ports:
- 5571: Leader -> controller (events) — must be open inbound on the controller
- 5572: controller -> Leader (commands; first packet teaches the Leader the return address)
- 5575: Leader -> Follower (display commands)
- 5080: REST API on each Pi (HTTP)

### Onboarding a new Pi onto the switch

The wired switch is a private, **gateway-less** `192.168.10.0/24` island carrying only
experiment traffic (low-latency UDP + REST). Each Pi keeps WiFi (`wlan0`) as its default
route to the internet; eth0 gets a **static** address with **no gateway**, so it never
competes for the default route. Do this once per new or reflashed Pi, before Install/Deploy.

> **Which IP:** a replacement Leader takes cheddar's `192.168.10.101`, a replacement Follower
> takes mozzarella's `192.168.10.102`. Adding an *extra* Pi → next free `.10x` (`.1` is the
> controller). Keep the `pis:` list in `rigs/cheese.yaml` in sync.

1. **Flash + first boot (headless).** Flash Debian 13 (trixie) with Raspberry Pi Imager; in
   its settings set the hostname, user `vruser`, enable SSH, and join the **institute WiFi**
   so the Pi has internet on first boot — the wired switch has none, so WiFi is how you first
   reach the Pi and how Install pulls packages.
2. **Cable eth0 to the gigabit switch** — the same switch as the controller and the other Pi.
3. **First login over WiFi** (the wired side has no IP yet):
   ```bash
   ssh vruser@<hostname>.local          # mDNS; or the wlan0 IP from your router
   ```
4. **Hostname** (skip if you set it at flash time):
   ```bash
   sudo hostnamectl set-hostname cheddar        # or mozzarella / a new name
   ```
5. **Static IP on eth0.** trixie images configure networking one of two ways — check which,
   because it changes *where* you set the address:
   - **NetworkManager** (most images, **including netplan that renders to NM** — you'll see
     `netplan-*` connections in `nmcli` and `/etc/netplan/90-NM-*.yaml` files). Configure
     **through NM, not by hand-editing netplan YAML** — those `90-NM-*` files are *generated
     from* the NM connections, so a hand-written netplan file collides with them. Find the
     wired profile and **modify it in place** (don't delete+add — the reflected profile is the
     one that persists):
     ```bash
     nmcli -f NAME,DEVICE,TYPE connection show    # wired name: netplan-eth0 / "Wired connection 1" / ...
     sudo nmcli connection modify netplan-eth0 \
       ipv4.method manual ipv4.addresses 192.168.10.102/24 \
       ipv4.gateway "" ipv4.dns "" ipv4.never-default yes \
       ipv6.method link-local connection.autoconnect yes
     sudo nmcli connection up netplan-eth0        # use the exact name you found
     ```
     (`.101` = Leader, `.102` = Follower. If there is *no* wired profile at all, use
     `nmcli connection add type ethernet ifname eth0 con-name eth-static …` with the same
     `ipv4.*` settings.) Confirm it persisted: `sudo cat /etc/netplan/90-NM-*.yaml` shows the
     static address instead of `dhcp4: true`.
   - **Hand-written netplan** (plain `/etc/netplan/*.yaml`, **no** `90-NM-*` files): add a
     static eth0 stanza (`dhcp4: false`, `addresses: [192.168.10.10X/24]`, no gateway),
     `sudo chmod 600` the file, `sudo netplan apply`.

   No gateway/DNS on eth0 on purpose — **wlan0 stays the default route** to the internet.

   > **Duplicate-IP trap (swapping Pis one at a time):** if the box you're replacing is still
   > on the switch at the same address, NetworkManager's duplicate-address detection refuses
   > to assign it — the log says `IP address 192.168.10.10X … already in use by host <MAC>`
   > and `ip -br addr show eth0` shows **no IPv4** despite "activated". Power off the old box
   > (or pick a free `.10x`), then re-run `sudo nmcli connection up …`.
6. **Verify from the controller** (over the switch):
   ```bash
   ping -c1 192.168.10.101
   ```
7. **Copy the controller's SSH key** (the setup UI needs passwordless SSH):
   ```bash
   ssh-copy-id vruser@192.168.10.101            # run on the controller
   ssh vruser@192.168.10.101 echo ok            # seeds known_hosts
   ```
8. **Passwordless sudo** — Install runs `sudo apt`/`raspi-config` **non-interactively over SSH**,
   so `vruser` must have NOPASSWD, else Install fails with `sudo: a terminal is required`. Images
   flashed with the Pi Imager user preset usually have it; a hand-created user often doesn't:
   ```bash
   # on the Pi, enter the password this once:
   echo "vruser ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/010_vruser-nopasswd >/dev/null
   sudo chmod 440 /etc/sudoers.d/010_vruser-nopasswd
   sudo visudo -c && sudo -n true && echo "passwordless sudo OK"
   ```
9. **Register + bring up.** Setup UI (`localhost:4999`) → *Add a Pi* (name, ip, role,
   devices), or edit the `pis:` list in `rigs/cheese.yaml`. Then **Connect → Install →
   Deploy**.

> If the WiFi is firewall-gated (the `public` captive portal blocks egress), Install's
> package pulls won't reach the internet — run the controller HTTP proxy and set
> `HTTPS_PROXY` on the Pi (see Troubleshooting), or flash on an open network first.

---

## Dependency Summary

| Package | Controller | Leader (cheddar) | Follower (mozzarella) |
|---|---|---|---|
| Python | 3.11 | match system (3.13 trixie) | match system (3.13 trixie) |
| flask | yes | yes | yes |
| requests | yes | — | — |
| numpy | yes | yes | yes |
| scipy | yes | yes (reward) | — |
| matplotlib | yes | — | — |
| h5py | yes | yes | — |
| pyyaml | yes | yes | yes |
| pygame | — | — | yes |
| smbus2 | — | yes (lick) | — |
| pigpio + pigpiod (**Pi 4** / `main`) | — | yes (reward/photodiode) | — |
| pillow, simplejpeg, piexif, av | — | yes (camera) | — |
| python3-picamera2 + python3-libcamera (apt, symlinked) | — | yes (camera) | — |
| lgpio (**Pi 5** / `dev-pi5`; no daemon, opens gpiochip0) | — | yes (reward/photodiode) | — |
| chrony (apt) | — | yes | yes |
| xserver-xorg / libgl (apt) | — | — | yes |

No longer needed: `paramiko`, `pyzmq`, `psychopy`, `pyglet`, `psychtoolbox`, `libcap-dev`
(camera is apt `python3-picamera2` now, not a pip build).

---

## Troubleshooting Quick Reference

| Problem | Fix |
|---|---|
| SSH connect fails | `ssh-copy-id vruser@<ip>` (and `ssh` once to seed known_hosts) |
| REST API not responding | `sudo systemctl restart vrfarm` on Pi |
| pigpiod not found | `sudo pigpiod`, or `sudo systemctl start pigpiod` |
| `import picamera2` fails | rig env Python must equal system Python (3.13); recreate env, re-run Install |
| pygame display fails | check `DISPLAY=:0`; run `~/rig/start_projector.sh` (or Init Devices) |
| Projector black / no image | run `~/rig/start_projector.sh` (GPIO ALT2 + GPIO25 high + DLPC3436 init) |
| Clock drift between Pis | `sudo systemctl restart chrony` |
| SSD not mounting | check `lsblk`, verify `/etc/fstab` entry |
| Warp map not found | Generate Warp in the setup UI (built on the controller, scp'd to `~/rig/calibration/warp_map.npz`) |
| Pi can't reach the internet to install | Pis are firewall-gated off `public` WiFi; run an HTTP proxy on the controller (`python -m proxy --hostname 192.168.10.1 --port 8899`) and `export HTTPS_PROXY=http://192.168.10.1:8899` on the Pi |
| cheddar SD card full | tiny ~8 GB card; reclaim with `conda clean -a -y` and `pip cache purge` |
| Port 5000 taken on Mac | disable AirPlay Receiver (System Settings -> General -> AirDrop & Handoff) |
| conda not in SSH PATH | `source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig` |
</content>
</invoke>
