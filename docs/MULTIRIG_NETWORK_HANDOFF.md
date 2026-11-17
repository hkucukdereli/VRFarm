# Handoff: set up the multi-rig network on the Linux controller

**Written:** 2026-09-11, from the Mac that developed the `multirig` branch.
**For:** the Claude agent working on the Linux PC that now holds the SFP+ 10G network card,
the switch and the SFP+ cable. Hakan (they/them) owns both machines.

Your job is to make that Linux PC the VRFarm **controller**: the machine that runs the web UI,
talks to every rig's two Raspberry Pis over a private wired network, and receives the
experiment events. Up to four rigs will hang off the switch. Nothing on the Pis has to change
for this; the work is on the PC.

Read `CLAUDE.md` in the repo as soon as you have it. It is the project handbook and it wins over
this page wherever they disagree. Then read `docs/MULTI_RIG.md` (what the four-tab UI does) and
`docs/CONTROLLER_SETUP.md` (a fresh-controller guide that already covers Ubuntu). This page is
the order of work plus the things that are specific to this move.

---

## 1. The picture

```
Linux PC (the controller)
 ├─ institute NIC ........ internet, NTP, the default route.  Leave it alone.
 └─ SFP+ 10G NIC ......... 192.168.10.1/24, no gateway, no DNS, no DHCP
        │  SFP+ cable (DAC)
        ▼
     switch (rig traffic only)
        ├─ 192.168.10.101  cheddar       leader   (Pi 5, runs the trial engine, cameras, GPIO)
        ├─ 192.168.10.102  cheddar-dlp   follower (Pi 4, drives the projector)
        ├─ .103 / .104     rig 2 leader / follower   (not built yet)
        ├─ .105 / .106     rig 3
        └─ .107 / .108     rig 4
```

- **Leader** and **follower** are the two Pis of one rig. The leader runs the experiment and
  sends events to the controller; the follower only draws the stimulus on the projector.
- The Pis are 1 GbE. The 10G link is only the uplink between the PC and the switch.
- Every Pi is also on the institute WiFi for internet and clock sync (NTP). The wired
  network never routes to the internet and must not become anyone's default route.
- The controller's address is **192.168.10.1** and that is not negotiable: the health monitor
  on each leader (`shepherd`) has it baked into its config as the place to send alarms, and
  the calibration tool on the follower posts its results to it.

---

## 2. Decisions already made (do not re-open them)

- The controller sits at `192.168.10.1`; rigs use the IP pairs above.
- Rigs do **not** get different port numbers. The controller opens one UDP socket (5571) and
  sorts incoming datagrams by the sender's IP. Only the IP pair has to be unique per rig.
- Synced data goes into one tree for all rigs, mouse first:
  `<data root>/<mouse>/<mouse>_<date>/<session_id>/`.
- The Data tab talks to leaders over SSH only (no Pi web API).
- All of this lives on git branch **`multirig`**. It is not merged into `main`. Do not merge it;
  that is Hakan's call after the live rig has been exercised.

---

## 3. Get the code and the files git does not carry

```bash
git clone git@github.com:hkucukdereli/VRFarm.git ~/VRFarm     # or cd into an existing checkout
cd ~/VRFarm
git fetch origin
git checkout multirig
```

If `origin/multirig` does not exist yet, stop and ask Hakan to push it from the Mac
(`git push -u origin multirig`). Do not rebuild the work on `main`.

Three kinds of files are deliberately **not** in git and must exist on this machine:

| File | What it is | Where to get it |
|---|---|---|
| `rigs/cheddar.yaml` | the live rig's config; carries the Slack webhook, so it is gitignored | copy it from the old controller if one exists there. Otherwise `cp rigs/cheese.yaml rigs/cheddar.yaml` and put the real webhook in its `slack:` block. Never commit it. |
| `controller.yaml` | controller-wide settings | `cp controller.example.yaml controller.yaml`, then edit (section 8) |
| `display_calibration/luminance_*.yaml` | projector brightness measurements | copy from the old controller if they exist; if not, the Setup tab can re-measure later |

Note that `rigs/cheese.yaml` is the tracked twin of the live config and `rigs/demo*.yaml`
are fakes for tests. The filename is the rig's identity, so the live rig is whatever
`rigs/cheddar.yaml` says.

---

## 4. Cabling and link

1. SFP+ cable between the PC's SFP+ cage and the switch's SFP+ port. Pis into the ordinary
   RJ45 ports. Put nothing else on this switch.
2. If the switch is managed, leave it at factory defaults: one VLAN, the SFP+ port enabled and
   on auto or 10G. No VLANs, no port isolation.
3. On the PC, find the card and confirm the link:

```bash
ip -br link                       # the new interface, e.g. enp5s0f0
lspci | grep -i ethernet
sudo ethtool <iface>              # want: Speed: 10000Mb/s  Link detected: yes
sudo ethtool -m <iface>           # the cable's module info (vendor, part)
```

If the link stays down:
- `dmesg | grep -iE 'sfp|ixgbe|mlx|i40e|unsupported'`. Intel 10G cards (`ixgbe` driver) refuse
  cables they do not recognise. Fix: `echo 'options ixgbe allow_unsupported_sfp=1' | sudo tee
  /etc/modprobe.d/ixgbe.conf`, then `sudo modprobe -r ixgbe && sudo modprobe ixgbe` (or reboot).
- Some switches also reject unbranded cables; check the switch's own log or port status page.
- A DAC does not auto-negotiate. If the driver picked the wrong speed: `sudo ethtool -s <iface>
  speed 10000 duplex full autoneg off`.

---

## 5. Static address on the SFP+ interface

First find out what manages the interface (`nmcli device status`, or `networkctl` if the
machine uses systemd-networkd). Two common cases:

**NetworkManager (most desktops):**

```bash
sudo nmcli con add type ethernet ifname <iface> con-name vrfarm-rigs \
    ipv4.method manual ipv4.addresses 192.168.10.1/24 \
    ipv4.never-default yes ipv6.method disabled connection.autoconnect yes
sudo nmcli con up vrfarm-rigs
```

**Netplan (Ubuntu server, or a desktop whose wired connection shows as `netplan-<iface>`):**
use the drop-in in `docs/CONTROLLER_SETUP.md` section 1 and `sudo netplan try`.

Either way the rules are: manual IPv4 `192.168.10.1/24`, **no gateway**, no DNS, IPv6 off,
MTU left at 1500 (the Pis are 1500; do not turn on jumbo frames). Do not run a DHCP server on
this network and do not enable IP forwarding or NAT; the Pis get internet over WiFi.

If the old controller is still plugged into the switch holding `.1`, unplug it first, or the
address will be rejected as a duplicate.

Verify:

```bash
ip -4 addr show <iface>           # inet 192.168.10.1/24
ip route                          # "default via ..." must name the institute NIC, not <iface>
ping -c3 192.168.10.101           # cheddar leader
ping -c3 192.168.10.102           # cheddar follower
timedatectl                       # System clock synchronized: yes (every timestamp is Unix time)
```

---

## 6. Firewall

The web app binds all interfaces and has **no login**. Keep it off the institute side.

| Port | Proto | Direction | Why | Rule |
|---|---|---|---|---|
| 5571 | UDP | Pis → PC | every leader's events and the health monitor's alarms | allow from 192.168.10.0/24 |
| 5000 | TCP | Pis → PC and localhost | the web UI; the follower's calibration tool posts geometry here | allow from 192.168.10.0/24; do not open it on the institute NIC |
| 8899 | TCP | Pis → PC | optional package proxy for the Pis (section 11) | allow from 192.168.10.0/24 when needed |
| 22, 5080, 5572, 5091 | TCP/UDP | PC → Pis | SSH, Pi REST API, commands, calibration sliders | outbound, normally unrestricted |

With `ufw`:

```bash
sudo ufw allow from 192.168.10.0/24 to any port 5571 proto udp
sudo ufw allow from 192.168.10.0/24 to any port 5000 proto tcp
sudo ufw status verbose
```

With `firewalld`, put `<iface>` in a zone of its own and add the same two ports as rich rules.
If no firewall is active, nothing to do, but then the UI is reachable from the institute
network on port 5000; tell Hakan.

---

## 7. Passwordless SSH to every Pi

The Setup tab (install, deploy, reboot, calibration) and the Data tab (sync, purge, power off)
use `ssh`, `scp` and `rsync` non-interactively. Each controller adds **its own** key to the Pis.

```bash
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519      # a key without passphrase is simplest
ssh-copy-id vruser@192.168.10.101
ssh-copy-id vruser@192.168.10.102
ssh vruser@192.168.10.101 "echo ok"                     # must print ok with no prompt
ssh vruser@192.168.10.102 "echo ok"
ssh vruser@192.168.10.101 "sudo -n true && echo sudo-ok"   # passwordless sudo is already set up on the Pis
```

The interactive `ssh` calls also seed `~/.ssh/known_hosts`, which the app needs. If a Pi has
been re-imaged since, `ssh-keygen -R <ip>` first. If your key has a passphrase, the app only
works while an ssh-agent holds it unlocked.

The Pis are Debian 13 (trixie), user `vruser`, conda env `rig` under `~/miniforge3`. Their
side is already set up for cheddar; `docs/INITIAL_SETUP_REFERENCE.md` covers a fresh Pi.

---

## 8. Python environment, rsync, settings

```bash
conda create -n vrfarm python=3.11 -y
conda activate vrfarm
pip install flask requests scipy matplotlib numpy h5py pyyaml
```

**rsync.** The Data tab copies with rsync and refuses anything older than 3.1 or Apple's
openrsync. The app looks for `<vrfarm env>/bin/rsync` unless `rsync_path` is set in
`controller.yaml`. On Linux either works:

```bash
conda install -n vrfarm -c conda-forge rsync          # option A: into the env (what the Mac does)
sudo apt install rsync                                # option B: distro rsync, then set rsync_path: /usr/bin/rsync
```

**controller.yaml** (copied from the example in section 3). Set:

```yaml
ui_port: 5000                 # no AirPlay on Linux, 5000 is free
controller_ip: 192.168.10.1
data_root: /path/on/the/big/drive/vrfarm-data   # one tree for all rigs; created on first sync
rsync_path: /usr/bin/rsync    # only if you chose option B above
```

Leave `groups` empty; groups are made in the Network tab.

Known Linux gap: the Data tab's **Browse…** button opens a macOS folder picker and does
nothing here. Type the path into the Data root field and save it instead. (If you want to fix
it, the route is `browse_folder` in `controller/data.py`; `zenity --file-selection --directory`
is the obvious Linux equivalent.)

---

## 9. Launch and prove the network works

```bash
conda activate vrfarm
python controller/app.py                 # opens http://localhost:5000; add --no-browser on a headless box
```

Do not use `--debug` for real work: it forks a reloader and only the child may own the UDP socket.

Checks, in order:

1. `ss -ulnp | grep 5571` shows python listening on `0.0.0.0:5571`.
2. **Network** tab → **Check all**. Both cheddar Pis get a green dot (SSH "ok" plus a reply
   from the Pi's REST API on 5080). A red dot on SSH means section 7; red on API means the
   Pi's `vrfarm.service` is down or the switch is not passing traffic.
3. Sanity without hardware, any time: `python tools/smoke_multirig.py` (two fake rigs, checks
   that events are sorted to the right rig) and `python tools/smoke_data.py` (the Data tab
   against a scratch folder). Both must pass on this machine; they use no Pi.

---

## 10. Bring cheddar onto the new controller

The branch changed a few Pi-side files that have never been deployed to the real rig. Do this
once, in this order, with nobody running an experiment:

1. **Setup** tab → Load `cheddar` → on each Pi card: **Deploy**, then **Restart API**. This
   ships the leader's data script (`shared/leader_data.py`), the rig tag on every event, the
   pi_api rig-file fix and the calibration-tool changes.
2. The leader needs `rsync`. Either re-run **Install** on the leader card (it is in the apt list
   now) or `ssh vruser@192.168.10.101 'sudo apt install -y rsync'`.
3. `python tools/migrate_calibration_dir.py cheddar` moves the projector geometry and warp
   files into `display_calibration/cheddar/`, one folder per rig from now on.
4. The live checklist:
   - Experiment tab → Connect → Load a task → Deploy → a short session (5 trials) to its
     natural end. Live events in the dashboard prove inbound UDP 5571 works. Slack should post
     if the webhook is set.
   - Data tab → load cheddar. The engine shows stopped; today's date folder is red.
   - **Sync Now** on it. It turns green; the files appear under the data root; the ledger
     `<data root>/.vrfarm_sync_ledger.json` has an entry.
   - **Purge Data**: the preview lists exactly that folder. Purge it.
   - Run a second short session, then at the end of the day **Sync & Poweroff** on cheddar
     alone. Both Pis show OFF and the Experiment badge shows offline. Cold boot the Pis;
     Network → Check all is green again.

Record what happened at each step for Hakan (section 13).

---

## 11. Adding rigs 2 to 4

Each rig is a new pair of Pis built per `docs/INITIAL_SETUP_REFERENCE.md`: Debian 13, user
`vruser`, WiFi for internet, static `eth0` at the next free pair, passwordless SSH and sudo. The
follower additionally needs the projector wiring described in `CLAUDE.md` (DPI parallel video
on the GPIO header, I2C to the projector controller); the Install step writes its boot config.

On the controller: **Network** tab → **Add rig** suggests the next IP pair and the names
`<rig>` and `<rig>-dlp` (rigs are named after cheeses). Then **Setup** → the new rig →
**Install** on both Pis (packages, conda env, systemd units, the health monitor with
`192.168.10.1` as its alarm target) → **Deploy** → **Restart API**. Groups ("super rigs") are
made in the Network tab and load several rigs at once in Setup, Experiment and Data.

The Pis cannot reach the internet from the institute WiFi for package installs. Run a proxy on
the PC (`pip install proxy.py`, then `python -m proxy --hostname 192.168.10.1 --port 8899`) and
set `HTTPS_PROXY=http://192.168.10.1:8899` on the Pi while installing.

---

## 12. Things that break rigs (from CLAUDE.md, repeated because they are expensive)

- Never start an X server on a follower and never run the display from the conda env. The
  display daemon is the only owner of the screen; a second owner is a black projector.
- `pkill -f PATTERN` matches the shell that ran it. Kill from a separate command.
- The Teensy serial port has exactly one reader. Stop the Pi's API devices before any bench tool.
- Never commit `rigs/cheddar.yaml` (Slack webhook) or `controller.yaml`.
- Do not change port numbers, and do not give the rig-side NIC a gateway.
- Do not merge `multirig` into `main`.

---

## 13. What to report back to Hakan

- The interface name and the exact network config you left in place (nmcli connection or
  netplan file), and whether the SFP+ cable needed a driver option.
- Firewall rules added, and whether port 5000 is reachable from the institute network.
- Whether `rigs/cheddar.yaml` and the luminance files were found or re-created.
- Which rsync option you took and the `rsync --version` line.
- The outcome of each step in section 10, including anything red, with the app's log
  (`logs/cheddar/app.log`) for failures.
- Anything you changed in the code, as a commit on `multirig`, never on `main`.
