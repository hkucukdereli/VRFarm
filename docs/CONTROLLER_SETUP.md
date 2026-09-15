# Controller Setup — new machine

How to bring up the VRFarm **controller** (the Mac/Linux box running `controller/app.py`)
on a fresh machine and get it talking to the two rig Pis. Linux/Ubuntu is
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

The Pis are static: **Leader `192.168.10.101`**, **Follower `192.168.10.102`**,
both user `vruser`.

---

## 1. Network — static IP on the wired NIC

The address is not baked into the Pis — the Leader learns it from the source of the first UDP
command and replies there, and the event listener binds `0.0.0.0:5571` — but **`controller_ip` in
`controller/configs/controller.yaml` must match it**: the Network tab derives new rig IP pairs from
its /24 (`controller/network.py`) and the Setup tab builds the geometry-callback URL from it
(`controller/setup.py`). `192.168.10.1` is the default. Stay clear of the rest of the address
plan: **`.101`–`.250` are rig IP pairs** (the Network tab hands them out) and **`.251`–`.254` are
infrastructure** (the switch's web UI is `.254`).

> Before claiming `.1`, make sure no other machine (an old Mac/Windows controller) is holding
> it on the switch, or duplicate-address detection will reject it. Pick a free `.x` otherwise.

### Ubuntu (netplan + NetworkManager)

If `nmcli connection show` lists your wired connection as `netplan-<iface>`, netplan owns it —
configure it in netplan (an `nmcli` edit can be overwritten on the next apply).

First, if NetworkManager has auto-created a DHCP profile bound to the rig NIC (`Wired connection N`
in `nmcli connection show`, `connection.interface-name` = your NIC), delete it **before the NIC gets
a link**, or NM starts DHCP on it. NM then remembers the MAC and won't recreate it:

```bash
sudo nmcli connection delete "Wired connection 1"    # only the one bound to your rig NIC
```

Then add a **separate, additive** drop-in:

```bash
sudo tee /etc/netplan/99-vrfarm-rig.yaml >/dev/null <<'EOF'
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    rig0:                      # just an id — the match below is what binds the card
      match:
        macaddress: "c4:62:37:0c:16:68"   # <-- your rig NIC's MAC (ip -br link)
      dhcp4: false
      dhcp6: false
      addresses:
        - 192.168.10.1/24
      # no gateway4 / routes on purpose -> this NIC never becomes the default route;
      # WiFi stays the internet path
EOF
sudo chmod 600 /etc/netplan/99-vrfarm-rig.yaml
```

Apply it **without** `netplan apply` or `netplan try`. Both restart NetworkManager, disconnect WiFi
and flush addresses — on fystyk WiFi dropped for ~4.5 s and rejoined a *different* SSID — and a
reverted `try` still leaves the edited YAML on disk. This rewrites only the generated files and
never touches WiFi:

```bash
sudo netplan generate
sudo nmcli connection reload
sudo nmcli connection up netplan-rig0
```

**Match the card by MAC, never by interface name.** `enpXsY` follows the card's PCI slot, so it
changes with no edit on your side: on fystyk the 10G card came back as `enp4s0` after a reboot (it
had been `enp6s0`), the name-keyed stanza matched nothing, and every rig went unreachable while the
card and its 10G link were perfectly healthy. The signature is `ip -br addr` showing the rig NIC up
with no address. A `set-name:` is not the fix — netplan then binds the NM profile to that name and
the rename only lands at the next boot, so the apply fails meanwhile.

**Moving the rig link to another NIC** (what fystyk did when a 10G card replaced the onboard port):
unplug the old NIC's cable first so the two can never both hold `.1`, back up, point `match:` at the
new card's MAC, and review the *merged* config before applying — the installer's own netplan file
may define the old NIC as well:

```bash
sudo cp -a /etc/netplan /root/netplan-backup
ip -br link                     # the new card's MAC
sudo sed -i 's/<old-mac>/<new-mac>/' /etc/netplan/99-vrfarm-rig.yaml
sudo netplan get ethernets      # the rig stanza carries the address; the old NIC must have none
```

Keep the `ethernets` argument: a bare `netplan get` also prints the WiFi passwords. If the old NIC
still appears with only a `match:`/`set-name:` (installer-written, no address), leave it — that
profile stops NM auto-creating a DHCP connection for the unused port. Roll back with
`sudo cp -a /root/netplan-backup/. /etc/netplan/` followed by the three apply commands above.

**fystyk's rig NIC** is an Intel 82599ES 10G SFP+ card (Argus ST-7211), in-kernel `ixgbe` — no
vendor driver pack — with a passive DAC into the switch's SFP+ port. Any DAC is accepted; third-party
*optics* need `options ixgbe allow_unsupported_sfp=1`. A 10G controller link matters with several
rigs: the Data tab's parallel syncs would otherwise saturate the one link every running rig's UDP
also uses.

### macOS

System Settings -> Network -> the Ethernet/adapter -> Details -> TCP/IP -> Configure IPv4
**Manually**, IP `192.168.10.1`, mask `255.255.255.0`, **router blank**.

### Verify (any OS)

```bash
ip -4 addr show <iface>       # expect inet 192.168.10.1/24   (macOS: ifconfig)
ip route                      # default route must be via WiFi/internet, NOT the wired NIC
ping -c3 192.168.10.101       # leader
ping -c3 192.168.10.102       # follower
```

### Firewall

The Leader pushes UDP events to the controller on **5571** — that inbound port must be open.
- Linux: `ufw` is usually inactive (nothing to do). If active:
  `sudo ufw allow from 192.168.10.0/24 to any port 5571 proto udp`
- macOS: the application-layer firewall prompts to allow `python`; allow it.

---

## 2. Passwordless SSH to the Pis

The **Setup** tab needs this (Install, push warp maps, reboot, calibration hand-off) and so does the
**Data** tab, which syncs, purges and powers off over SSH only. Running an experiment uses no SSH.
Each new controller must add **its own** key to the Pis — they only trust the keys already
installed.

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
- the SSH helpers in `controller/ssh.py` run non-interactively (no TTY), so the key must log in
  without a prompt **and** each Pi's host key must already be in `~/.ssh/known_hosts` — the
  interactive `ssh` in step 3 seeds it.
- If your key has a **passphrase**: on a Linux/macOS desktop the login keyring's ssh-agent
  auto-unlocks it per session, so the controller's `ssh` calls still work. If not, either use a
  passphrase-less key for the Pis or `ssh-add` the key before launching `controller/app.py`.

---

## 3. Python environment

```bash
conda create -n vrfarm python=3.11 -y
conda activate vrfarm
pip install flask requests scipy matplotlib numpy h5py pyyaml
```

Controller-only deps — `picamera2`/`lgpio` live on the Pis, not here. The setup UI builds
the warp map with `sys.executable`, so it uses whatever `python` is running the UI (this env).

---

## 3b. Data directory (machine-specific — NOT in the rig yaml)

Synced session data and the subject database live in a **controller-local** root, the same
tree for every rig, resolved at runtime:

0. `data_root` in `controller.yaml` — the Data tab's **Data root** field / Browse… writes it, else
1. `$VRFARM_DATA_DIR` if set (e.g. point it at a big SSD mount), else
2. `~/VRFarm/data` — the same convention on every OS
   (`/Users/<user>` on macOS, `/home/<user>` on Linux, `C:\Users\<user>` on Windows).

Nothing to configure for a default setup — the directory is created on first sync.
To use a dedicated drive, export the variable before launching the UI (and persist it in
`~/.bashrc` / `~/.zshrc`):

```bash
export VRFARM_DATA_DIR=/mnt/ssd/vrfarm-data
```

The rig yaml deliberately has **no** controller path (`data.mac_dir` was removed: a
machine-specific absolute path in a shared config broke every other controller — the
original Linux symptom was `Transfer failed: cannot create /Users/... permission denied`).
The yaml's `data.leader_dir` / `data.video_dir` are Pi-side paths and stay.
The Data tab's **Data root** field is the normal way to point at a drive; it is saved in
`controller.yaml` and wins over the environment variable.

---

## 4. Launch and validate

The Data tab copies with `rsync`, which must be a real rsync (3.1 or newer). With `rsync_path: null`
in `controller.yaml` the controller looks **only in the `vrfarm` env** (`sys.prefix/bin/rsync`, no
PATH fallback — `controller/settings.py`), and a fresh env has none. Either install it there:

```bash
conda install -n vrfarm -c conda-forge rsync
```

or, **on Linux**, point at the system one, which is real rsync (fystyk: 3.4.1):

```yaml
rsync_path: /usr/bin/rsync          # controller/configs/controller.yaml
```

On **macOS** only the first option works: `/usr/bin/rsync` there is Apple's openrsync, which the Data
tab rejects. The Data tab shows a banner naming whichever problem it finds.

```bash
conda activate vrfarm
python controller/app.py  # -> http://localhost:5000 (Network / Setup / Experiment / Data)
```

1. **Network** tab -> **Check all**: every Pi should show a green dot. **Setup** tab -> Load rig:
   the page checks the Pis again; then **Initialize** to bring the devices up.
2. **Deploy** to push current engine/device code to the Pis.
3. Experiment UI -> **Load Rig** -> **Load Experiment** -> **Deploy** -> run a short
   **5-trial** session; live events appearing in the dashboard proves inbound UDP 5571 and
   the Leader's reply-to-sender work.

Both UIs are walked through with screenshots in [SETUP_UI.md](SETUP_UI.md) and
[EXPERIMENT_UI.md](EXPERIMENT_UI.md). To validate the controller with no rig attached, run
`python tools/mock_pi.py` and use the `demo` rig.

---

## Ports reference

| Port | Proto | Direction | Purpose |
|------|-------|-----------|---------|
| 5080 | TCP | controller -> both Pis | REST API (deploy, config, start/stop, data, camera) |
| 5572 | UDP | controller -> Leader | START/STOP/REWARD (first packet teaches the Leader the return address) |
| 5571 | UDP | Leader -> controller **(inbound)** | trial/lick/reward/stim/sync events |
| 5575 | UDP | Leader -> Follower | SHOW/QUIT (Pi-to-Pi; not the controller) |
| 22 | TCP | controller -> both Pis | SSH/SCP: Setup tab (Install, calibration) and Data tab (rsync, purge, poweroff) |
| 5000 | TCP | localhost | the one controller UI (Network / Setup / Experiment / Data) |
| 80 | TCP | controller -> switch | Zyxel XGS1210-12 web UI at `192.168.10.254` |

---

## OS notes

- **macOS**: port 5000 clashes with AirPlay Receiver — disable it in System Settings ->
  General -> AirDrop & Handoff, or the experiment UI won't bind.
- **Windows**: viable but needs the extra fixes on the `windows-port` branch (UDP
  `SIO_UDP_CONNRESET` guard, UTF-8 file encoding, LF `.gitattributes`, a PowerShell folder
  picker) plus a Windows-Firewall inbound rule for UDP 5571 on **all** profiles. See that
  branch's commits if returning to Windows.
