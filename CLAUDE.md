# VRFarm — Claude Code Handoff

This document is for Claude Code to pick up the VRFarm project.
Read this entire file before touching anything.

---

## What is VRFarm

A behavioral neuroscience experiment system for mice. Two Raspberry Pis per rig
(Leader + Follower), controlled from the Controller via Flask web UIs. Paradigms are
tuned via task YAML (stimulus/reward/session/adaptive params); the trial engine is the
imperative loop in `engine/leader.py`. Devices are pluggable. Named after cheese.

**Current rig:** `cheddar` — Leader `cheddar` (192.168.10.101, **Pi 5**),
Follower `cheddar-dlp` (192.168.10.102, **Pi 4**, drives the projector).
Live rig config is `rigs/cheddar.yaml` — gitignored (it carries the Slack webhook), so the live
config has no copy in git; `rigs/_template.yaml` is the tracked template.

**Controller:** `fystyk` (192.168.10.1), **Ubuntu 26.04 LTS** (Linux, not a Mac), env `vrfarm`
(Python 3.11) at `~/miniforge3/envs/vrfarm`. Rig link is the 10G SFP+ card (netplan `rig0`, matched by MAC) — see Network.
ONE web app runs everything: `python controller/app.py` → http://localhost:5000 with
four tabs, Network / Setup / Experiment / Data. Controller-wide settings (data root, auto purge, rig
groups) live in `controller/configs/controller.yaml` (gitignored; template `controller.example.yaml`
in the same folder). Full guide: `docs/MULTI_RIG.md`.
**Both Pis:** Debian 13 (trixie), conda env `rig`, user `vruser`. The `rig` env Python
**must match the system Python** (3.13 on trixie) — the camera bindings
(`python3-libcamera`/`python3-picamera2`) are apt-built for the system Python and symlinked
into the env, so a version mismatch breaks `import picamera2`. Create with
`conda create -n rig python=$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')`.

> **The display does NOT run in the conda env.** The projector runs under full KMS, and the
> conda env's SDL has **no kmsdrm backend** — it silently falls back to a null driver that
> renders nothing at all. Everything that opens the display (`displayd/renderer.py`,
> `display_calibration/calib_geo.py`) runs on the **system `/usr/bin/python3`**.

---

## Project structure

```
~/VRFarm/                              <- project root on the Controller
├── rigs/
│   ├── cheddar.yaml                   <- rig hardware config (pins, cal, ports, roles) — YAML, one per rig
│   └── _template.yaml                 <- new-rig template (Network tab -> Add rig); '_' files are not rigs
├── experiments/
│   └── *.yaml                         <- task/paradigm configs (stimulus/reward/session/adaptive)
├── devices/                           <- one file per device type (base.py = Device, DEVICE_REGISTRY)
│   ├── lick_sensor.py  reward.py  camera.py  photodiode.py  encoder.py  display.py
├── engine/
│   └── leader.py                      <- Leader Pi main process (imperative trial loop)
├── displayd/                          <- KMS display daemon (owns the projector)
│   ├── displayd.py                    <- control daemon: lifecycle, L1 poll, supervisor, REST
│   ├── renderer.py                    <- renderer child, sole DRM master (system python3)
│   ├── dlpc.py                        <- defensive DLPC3436 I2C wrapper
│   ├── PROTOCOL.md                    <- THE interface contract — read before changing anything
│   └── displayd.service
├── controller/                        <- THE controller UI, localhost:5000 (replaced app/ and setup/)
│   ├── app.py                         <- shell (4 tabs) + global routes; opens the ONE UDP socket (5571)
│   ├── registry.py  events.py         <- a RigState per loaded rig; UDP demux by sender IP; SSE fan-out
│   ├── experiment.py  setup.py        <- /api/rigs/<rig>/...  and  /api/rigs/<rig>/setup/...
│   ├── network.py  data.py            <- rigs/Pis/groups CRUD; Data tab (SSH/rsync sync, purge, poweroff)
│   ├── sync.py  jobs.py  ssh.py       <- Data-tab engine, background jobs, ssh/scp helpers
│   ├── configs/                       <- controller.yaml (controller-wide settings, gitignored) + controller.example.yaml
│   └── templates/ static/             <- shell.html + one page per tab (per-rig pages run in iframes)
├── pi_api/api.py                      <- Flask REST API on each Pi, port 5080
├── shared/
│   ├── config.py  stim_generator.py  notify.py
│   ├── leader_data.py                 <- Pi-side inventory / consolidate / purge for the Data tab (over SSH)
│   └── deploy_manifest.py             <- single deploy file list (Install and Deploy)
├── display_calibration/               <- geometry calibration (kmsdrm; calib_geo.py + cal_start/stop.sh)
├── shepherd/                          <- Pi-side health monitor
└── data/subjects/                     <- session history JSONs
```

**On the Leader (.101):** `~/rig/` (code), `~/data/<subject>/<subject_date>/<session_id>/`
(HDF5; sidecars are folded into the .h5 by the consolidate step), video on the SSD.
**On the Follower (.102):** `~/rig/` (code incl. `displayd/`), `~/rig/calibration/`,
`~/rig/stims/<session_id>/stimuli.npz`.

---

## Network

```
Zyxel XGS1210-12 switch (experiment traffic) — web UI http://192.168.10.254
├── port 11    10G SFP+ (DAC)  Controller  192.168.10.1    rig0 (MAC-matched; enp4s0 today)
├── ports 1-2  1G RJ45         Leader      192.168.10.101  (eth0 static)
│                              Follower    192.168.10.102  (eth0 static)
└── port 12    10G SFP+        spare — reserved for a 10G link to a second switch
```
Both Pis are also on institute WiFi for internet/NTP. Passwordless SSH + sudo from the
Controller to both Pis.

**Address plan (192.168.10.0/24):** `.1` Controller · `.101`–`.250` rig IP pairs, handed out by
the Network tab (`controller/network.py` `api_suggest_ips`: leaders `.101, .103 … .249`, follower =
leader + 1) · `.251`–`.254` infrastructure, never allocated — `.254` is the switch. The allocator
only checks rig YAMLs, so nothing else may sit in `.101`–`.250`.

**Controller NIC.** The rig link is an Intel 82599ES single-port 10G SFP+ card (Argus ST-7211,
in-kernel `ixgbe` — Intel's vendor driver pack is NOT needed; its out-of-tree ixgbe
5.16.5 only shims kernels up to 5.11, this one is 7.0), DAC to switch port 11. Any passive/active DAC is accepted; third-party *optics* would need
`ixgbe allow_unsupported_sfp=1`. Config: `/etc/netplan/99-vrfarm-rig.yaml`, netplan id `rig0`
**matched by MAC** (`c4:62:37:0c:16:68`), static `192.168.10.1/24`, **no gateway** (WiFi stays the
default route). **Never key that stanza by interface name:** `enpXsY` follows the card's PCI slot,
so it changed on its own (`enp6s0` → `enp4s0` after a reboot on 2026-09-15), the stanza then matched
nothing, and every rig was unreachable while the card and its 10G link were healthy. The onboard `enp0s31f6` is
unused: its link flapped at 100 Mbps after the card install and its cable is out. Why 10G: the
Data tab syncs up to `parallel_rigs` leaders at once; on a 1G controller port that saturates the
one link every running rig's UDP also needs (`_rig_guard` only protects the rig being synced).

**Switch.** Management is static `192.168.10.254/24`, DHCP off, gateway `0.0.0.0` (factory default
is `192.168.1.3` — after a reset, reach it with a temporary `sudo ip addr add 192.168.1.200/24 dev
enp4s0`, the rig NIC's current kernel name from `ip -br addr`). All ports untagged VLAN 1. **Loop Prevention on — keep it** (it guards every rig once a
second switch is chained); Broadcast Storm Control off. Capacity: 10 RJ45 ports = 5 rigs of two
Pis; beyond that, chain a second switch on port 12. The password is not recorded here.

| Port | Direction | Purpose |
|------|-----------|---------|
| 5000 | browser → Controller | the one controller UI (Network / Setup / Experiment / Data) |
| 22 | Controller → Pi | SSH: Install, calibration hand-off, and the Data tab (rsync sync, purge, poweroff) |
| 5080 | Controller → Pi | pi_api REST (deploy, start/stop, device init, camera) |
| 5571 | Leader → Controller | events (trial, lick, reward, sync, display_health, display_abort) from EVERY rig — one socket, sorted by sender IP (+ the `rig` field each event carries) |
| 5572 | Controller → Leader | commands (START, STOP, REWARD) |
| 5575 | Leader → displayd | session channel (SHOW, LOAD_STIMS, HB_FLASH, SYNC_TEST) |
| 5573 | displayd → Leader | acks: stim_onset, hb_flash (flip times), display_health |
| 5581 | localhost only | displayd control REST (/status /bringup /standby /resume /render /lease) |
| 5582 | Leader → displayd | photodiode ingest + hb_verdict (drives `optics` in /status) |
| 5091 | browser → Follower | calib_geo sliders, only while calibrating |
| 80 | browser → switch | Zyxel XGS1210-12 web UI at 192.168.10.254 |

---

## Display architecture (KMS)

The projector is a TI **DLPDLCR230NPEVM** (DLPC3436 controller, DLP230NP DMD, XPR-4 pixel
shift). Video is **DPI parallel RGB666 on GPIO0-21 — not HDMI**. The DLPC is configured over
a bit-banged software I2C bus (GPIO23=SDA, GPIO22=SCL) at 8-bit address 0x36.

Full KMS (`vc4-kms-v3d` + `vc4-kms-dpi-generic`), 1920×1080 @ **57.46 Hz**, 125 MHz pixel
clock. There is **no X server anywhere** on the rig.

> **pygame needs the `SCALED` (or `OPENGL`) flag for `vsync=1` to be honored.** Without it
> `vsync=1` is silently ignored — this was the root cause of years of unlocked flips
> (44 fps against a 57.46 Hz scanout). See `devices/display.py`.

**displayd** owns the display: a control daemon plus a renderer child that is the sole DRM
master. Two DRM masters is a black screen, so there is deliberately **no second path** to
the framebuffer — pi_api's display endpoints forward to displayd or return 503.

Health layers:
- **L1** — DLPC I2C poll at 1 Hz (LED/DMD/sequencer status; fault bits latch and clear-on-read).
  Live input-loss detector is `ActuatorWatchdogTimerTimeout`, *not* AutoFraming.
- **L2** — renderer flip watchdog + startup self-test (backend, fps, flip-block).
- **L3** — photodiode heartbeat: the leader flashes the sync square on **its own clock** every
  5 s between stimuli and correlates the pulse against the flip time displayd reports back.
  Counts alone are not enough (mains flicker ~100 Hz and ground-fault hum ~50 Hz both fake them).
- **L4** — camera arbiter. Not built.

A lost heartbeat is localized from the Teensy's 1 Hz `B <floor> <ceil>` idle line, which reports
the analog front end independently of pulse detection: `sensor_dead` (B absent), `ttl_dead`
(light seen, no TTL edge), `no_light` (front end alive, genuinely dark), `no_telemetry`.
`no_light` does **not** claim the projector failed — a covered diode is equally dark; only L4
could separate those. Alarms reach Slack via the Controller and, if `session.abort_on_display_fault`
is set (default true), end the session at a **trial boundary** with `end_reason: display_fault`.

---

## Config system

| What | Where | Example |
|------|-------|---------|
| Pins, I2C addresses, calibration tables, roles, IPs, ports | rig YAML | `gpio: 16`, `refresh_hz: 57.46` |
| Paradigm / trial params, adaptive rules | task YAML | `stimulus.duration_s`, `reward.amount_ul` |
| Subject, date, session # | runtime (UI) | set in the experiment UI |

## Device abstraction

Adding a device = one file in `devices/`, subclassing `Device` from `devices/base.py`:
`info`, `init()`, `check()`, `task_params_schema()`, `hdf5_datasets()`/`hdf5_trial_data()`,
`start_stream()`/`stop_stream()`, optional `needs_calibration`/`calibrate()`.
`@register_device` adds it to `DEVICE_REGISTRY`.

---

## Experiment workflow

Setup → Connect → **Deploy** → Go → Ended → New session; the data is pulled later from the **Data** tab.
Deploy uploads code (list lives in `shared/deploy_manifest.py`), generates stimuli on the
Leader, pushes the NPZ to the Follower, and renders thumbnails. Any parameter change
invalidates the deploy and greys out Go.

Several rigs run side by side: each loaded rig is a sub-tab of Setup and Experiment, and a rig
**group** (Network tab) loads several at once. Rigs do NOT need different ports: the controller
opens ONE UDP socket and sorts datagrams by the sender's IP (falling back to the `rig` field every
leader event carries). Pi identity (name / IP / role / user) is edited in the Network tab.

**Data tab** (SSH only, talks to leaders through `shared/leader_data.py` protocol 2, which ships with
Deploy): a date folder `<mouse>/<mouse>_<date>` is **green** when every tree that holds it (data dir,
video dir) has nothing left to copy in a strict rsync dry run, **red** when something is pending,
**missing** when a session recorded `camera_saved: true` but its video is not on the Pi, **grey**
when the check can't be trusted (a failed dry run, an unmounted `data.video_mount`, a leader that
needs a Deploy). No nonzero rsync exit counts as clean. A folder with no video folder is normal —
the camera was unchecked (GO sends `camera_requested`/`camera_saved` in START; the engine records
them). **Sync Now** copies chosen folders (consolidate on the Pi first, copy only the trees that
hold the folder, verify against a fresh inventory plus a second dry run, ledger at
`<data root>/.vrfarm_sync_ledger.json`); **Sync & Poweroff** copies everything not green, then
`sudo poweroff`s every Pi of each rig whose folders all verified; **Purge Data** deletes green +
consolidated folders on the Pi, each tree's copy re-verified right before and named to the Pi with
`--verified`, which refuses anything else; **Auto purge** runs that same purge after each verified
sync. Data lands subject-first in one tree for all rigs: `<data root>/<mouse>/<mouse>_<date>/<session_id>/`.

---

## Packages

**Controller** (`conda activate vrfarm`): `flask requests scipy matplotlib numpy h5py pyyaml`, plus
a real `rsync` >= 3.1 for the Data tab. `rsync_path: null` resolves to **the env's rsync only**
(`controller/settings.py` `rsync_path()` → `sys.prefix/bin/rsync`, no PATH fallback), and the
`vrfarm` env on fystyk has none — so either `conda install -n vrfarm -c conda-forge rsync` or set
`rsync_path: /usr/bin/rsync` in `controller/configs/controller.yaml` (Ubuntu's is real rsync,
3.4.1). Only on macOS is `/usr/bin/rsync` Apple's openrsync, which the Data tab rejects.
**Leader** (`rig` env): `flask pyyaml numpy scipy h5py smbus2 pigpio lgpio pyserial` + `picamera2`;
`rsync` from apt on every Pi (the Install step adds it).
**Follower**: `rig` env for pi_api; **system python3** for displayd/renderer and calib_geo
(`python3-pygame`, `python3-numpy`, `python3-yaml` from apt — no Flask, and calib_geo's web
UI is stdlib `http.server` for exactly that reason).

pigpiod is built from source (`/usr/local/bin/pigpiod`, unit at
`/etc/systemd/system/pigpiod.service`, enabled) — the apt package is gone on trixie.

```bash
conda activate vrfarm
python controller/app.py            # the controller UI, localhost:5000 (on macOS: --port 5055 if AirPlay squats 5000)
python tools/smoke_multirig.py      # two fake rigs end to end (mock Pis)
python tools/smoke_data.py          # the Data tab against a scratch data tree
```
Slack comes from the rig YAML's `slack:` block (`enabled` + `webhook_url`).

---

## Known issues / gotchas

- `conda` is not in PATH for non-interactive SSH — use
  `source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig`
- **Never run the display from the conda env** (no kmsdrm backend; silent null driver).
- **Never start X on the display Pi.** It takes DRM master from displayd and blacks the rig.
  Geometry calibration hands over properly instead: `cal_start.sh` POSTs `:5581/standby`,
  `cal_stop.sh` POSTs `/resume`. The setup UI refuses any X path while displayd is alive.
- `pkill -f PATTERN` **self-matches** any wrapper whose command line mentions the literal —
  always issue the kill separately from commands that name the file. (The Data tab's
  engine-running check therefore scans `/proc` from inside Python, `shared/leader_data.py`.)
- **Every new package or Pi-side file goes through Install / Deploy**: apt packages in the Install
  step (`controller/setup.py`), Pi-side files in `shared/deploy_manifest.py`, controller packages
  in the list above. A leader installed before the multi-rig work needs a Deploy (ships
  `shared/leader_data.py`) and a re-Install or `sudo apt install rsync`.
- **shepherd's "pi_api not responding" has a measured grace period.** Deploy and Restart API time
  each pi_api restart (`controller/pi_restart.py`) and write the outage ×1.5 (5–60 s) to
  `~/rig/shepherd/api_grace.json` on the leader through `/api/shepherd_grace`; shepherd alerts only
  once pi_api has been away that long, but at once if a session was running. Deploy also restarts
  shepherd, which a `shepherd.py` change otherwise never reached. The Pi's `config.yaml` is seeded
  once (`cp -n`), so new shepherd settings need code defaults.
- Install sets every Pi's time zone to `Europe/Vienna` (`timedatectl`); before that the leader ran on
  Europe/London, so its log times read an hour behind the follower's. Recorded data is Unix time.
- Calibration files are per rig once `display_calibration/<rig>/` exists
  (`python tools/migrate_calibration_dir.py cheddar`); until then the shared folder is used.
- **One serial reader.** The Teensy's `/dev/ttyACM0` gives its bytes to exactly one process;
  a second reader silently steals them. Release pi_api's devices before bench tools.
- The Teensy sync output is **pin 16** (scope-verified), analog in is **A1**.
- Leader is on a small SD card — reclaim with `conda clean -a -y` / `pip cache purge`.
- Pis are firewall-gated off the institute WiFi. To install packages, run a proxy on the
  Controller (`python -m proxy --hostname 192.168.10.1 --port 8899`) and set
  `HTTPS_PROXY=http://192.168.10.1:8899` on the Pi.
- **Never `netplan apply` or `netplan try` on fystyk.** Both restart NetworkManager, disconnect
  WiFi and flush addresses (observed: WiFi dropped ~4.5 s and rejoined a different SSID), and
  `try`'s auto-revert leaves the edited YAML on disk. Apply rig-link changes surgically — only files
  are rewritten, WiFi is never touched: `sudo netplan generate && sudo nmcli connection reload &&
  sudo nmcli connection up netplan-rig0`.
- **NetworkManager auto-creates a DHCP `Wired connection N`** for any ethernet port it has no
  profile for, bound to that port with autoconnect on. Delete it (`sudo nmcli connection delete
  "Wired connection N"`) *before* the new rig NIC gets a link, or it starts DHCP there; NM then
  remembers the MAC and doesn't recreate it. The installer's `00-installer-config.yaml` still names
  `enp0s31f6` (MAC match + `set-name`, no address) — leave it: that profile is what stops NM
  auto-DHCPing the unused onboard port.
- **Don't recable the switch during a session.** When the DAC first went in, the Follower's link
  dropped and the switch stopped delivering to the Leader's port for several minutes with its link
  up — no trace on either host, and not reproduced by a later DAC replug (~2 s recovery) or a switch
  power cycle (~35 s).
- A barrel-jack pull on the EVM has been observed to take out DLPC I2C as well as the light
  (L1 goes unreachable). Suite A documented the opposite — logic back-feeding through the Pi
  header while the room went dark — so do not assume either signature.

---

## Style / conventions

- UDP datagrams for all real-time Pi communication; REST (Flask) for management; SSE to browsers
- systemd for Pi process lifecycle (`vrfarm.service`, `displayd.service`, `pigpiod.service`)
- HDF5 for trial data, written incrementally per trial on the Leader
- All timestamps `time.time()` Unix seconds, NTP-synced
- Trial engine: imperative loop in `engine/leader.py`, tuned by task-YAML params
- `displayd/PROTOCOL.md` is the interface contract — update it in the same commit as the code
