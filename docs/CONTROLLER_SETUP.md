# Controller Setup — new machine

How to bring up the VRFarm **controller** (the Mac/Linux box running `app/app.py` and
`setup/app.py`) on a fresh machine and get it talking to the two rig Pis. Linux/Ubuntu is
the reference here; macOS is the same minus the netplan step, Windows notes at the bottom.

The controller reaches the Pis entirely over the network — HTTP (REST) + UDP for the
experiment loop, plus SSH/SCP for the setup UI's deploy/calibrate actions. There is nothing
machine-specific baked into the rig config; any host on the rig subnet can drive the rig.

---

## 0. What the controller needs

| | |
|---|---|
| A wired NIC on the **experiment switch** | static IP on `192.168.10.0/24` |
| A second NIC (WiFi) | internet + NTP; stays the default route |
| Python env `vrfarm` | flask requests scipy matplotlib numpy h5py pyyaml |
| Passwordless SSH to both Pis | only for the setup UI (deploy/warp/calibrate) |

The Pis are static: **cheddar `192.168.10.101`** (Leader), **mozzarella `192.168.10.102`**
(Follower), both user `vruser`.

---

## 1. Network — static IP on the wired NIC

The controller IP does **not** have to be a specific value: the Leader learns it from the
source address of the first UDP command and replies there, and the event listener binds
`0.0.0.0:5571`. **Any `192.168.10.x` works** except `.101`/`.102`. `192.168.10.1` is the
zero-config choice (it matches the one hardcoded reference, `MAC_URL` in
`display_calibration/calib_geo.py`, used by the optional calibration-archive POST).

> Before claiming `.1`, make sure no other machine (an old Mac/Windows controller) is holding
> it on the switch, or duplicate-address detection will reject it. Pick a free `.x` otherwise.

### Ubuntu (netplan + NetworkManager)

If `nmcli connection show` lists your wired connection as `netplan-<iface>`, netplan owns it —
configure it in netplan (an `nmcli` edit can be overwritten on the next apply). Add a
**separate, additive** drop-in (does not touch WiFi):

```bash
sudo tee /etc/netplan/99-vrfarm-rig.yaml >/dev/null <<'EOF'
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    enp0s31f6:                 # <-- your wired interface (see: nmcli device status)
      dhcp4: false
      dhcp6: false
      addresses:
        - 192.168.10.1/24
      # no gateway4 / routes on purpose -> this NIC never becomes the default route;
      # WiFi stays the internet path
EOF
sudo chmod 600 /etc/netplan/99-vrfarm-rig.yaml
sudo netplan try            # 120s auto-revert safety net; press Enter to keep
```

### macOS

System Settings -> Network -> the Ethernet/adapter -> Details -> TCP/IP -> Configure IPv4
**Manually**, IP `192.168.10.1`, mask `255.255.255.0`, **router blank**.

### Verify (any OS)

```bash
ip -4 addr show <iface>       # expect inet 192.168.10.1/24   (macOS: ifconfig)
ip route                      # default route must be via WiFi/internet, NOT the wired NIC
ping -c3 192.168.10.101       # cheddar
ping -c3 192.168.10.102       # mozzarella
```

### Firewall

The Leader pushes UDP events to the controller on **5571** — that inbound port must be open.
- Linux: `ufw` is usually inactive (nothing to do). If active:
  `sudo ufw allow from 192.168.10.0/24 to any port 5571 proto udp`
- macOS: the application-layer firewall prompts to allow `python`; allow it.

---

## 2. Passwordless SSH to the Pis

Only the **setup UI** needs this (deploy code, push warp maps, reboot, calibrate). The
experiment-run UI uses no SSH. Each new controller must add **its own** key to the Pis —
they only trust the keys already installed.

```bash
# 1. Reuse an existing key or make one (no passphrase = simplest for the app's non-interactive ssh)
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519

# 2. Install it on each Pi (prompts for the vruser password once; sets perms + known_hosts)
ssh-copy-id vruser@192.168.10.101
ssh-copy-id vruser@192.168.10.102

# 3. Verify — both print "ok" with NO password prompt
ssh vruser@192.168.10.101 "echo ok"
ssh vruser@192.168.10.102 "echo ok"
```

Notes:
- `_ssh`/`_scp` in `setup/app.py` run non-interactively (no TTY), so the key must log in
  without a prompt **and** each Pi's host key must already be in `~/.ssh/known_hosts` — the
  interactive `ssh` in step 3 seeds it.
- If your key has a **passphrase**: on a Linux/macOS desktop the login keyring's ssh-agent
  auto-unlocks it per session, so the setup UI's `ssh` calls still work. If not, either use a
  passphrase-less key for the Pis or `ssh-add` the key before launching the setup UI.

---

## 3. Python environment

```bash
conda create -n vrfarm python=3.11 -y
conda activate vrfarm
pip install flask requests scipy matplotlib numpy h5py pyyaml
```

Controller-only deps — `picamera2`/`pigpio` live on the Pis, not here. The setup UI builds
the warp map with `sys.executable`, so it uses whatever `python` is running the UI (this env).

---

## 4. Launch and validate

```bash
conda activate vrfarm
python setup/app.py       # setup UI      -> http://localhost:4999
python app/app.py         # experiment UI -> http://localhost:5000
```

1. Setup UI -> Load Rig -> **Connect**: both Pis answer on REST 5080.
2. **Deploy** to push current engine/device code to the Pis.
3. Experiment UI -> Connect -> Deploy -> run a short **5-trial** session; live events
   appearing in the dashboard proves inbound UDP 5571 and the Leader's reply-to-sender work.

---

## Ports reference

| Port | Proto | Direction | Purpose |
|------|-------|-----------|---------|
| 5080 | TCP | controller -> both Pis | REST API (deploy, config, start/stop, data, camera) |
| 5572 | UDP | controller -> Leader | START/STOP/REWARD (first packet teaches the Leader the return address) |
| 5571 | UDP | Leader -> controller **(inbound)** | trial/lick/reward/stim/sync events |
| 5575 | UDP | Leader -> Follower | SHOW/QUIT (Pi-to-Pi; not the controller) |
| 22 | TCP | controller -> both Pis | SSH/SCP, setup UI only |
| 5000 / 4999 | TCP | localhost | experiment / setup Flask UIs |

---

## OS notes

- **macOS**: port 5000 clashes with AirPlay Receiver — disable it in System Settings ->
  General -> AirDrop & Handoff, or the experiment UI won't bind.
- **Windows**: viable but needs the extra fixes on the `windows-port` branch (UDP
  `SIO_UDP_CONNRESET` guard, UTF-8 file encoding, LF `.gitattributes`, a PowerShell folder
  picker) plus a Windows-Firewall inbound rule for UDP 5571 on **all** profiles. See that
  branch's commits if returning to Windows.
