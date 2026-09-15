# Multi-rig controller

**Last updated:** 2026-09-08

One controller (the lab's is `fystyk`, an Ubuntu box) runs several rigs from one web app on one port. This page explains what
changed from the two old apps (`app/` experiment UI and `setup/` setup UI), how the four
tabs work, and what the Data tab does to your files.

```bash
conda activate vrfarm
python controller/app.py            # http://localhost:5000  (--port 5055 if macOS AirPlay owns 5000)
```

## The four tabs

- **Network** — the rigs and their Pis. Add a rig (the next free IP pair is suggested:
  .101/.102, .103/.104, …), edit Pi names / IPs / roles / users / devices, rename or delete a
  rig (the YAML moves to `rigs/.trash/`), check every Pi over SSH and the REST API, and make
  **groups** ("super rigs"): a group loads all its rigs at once from the Load menu of Setup and
  Experiment and from the Data tab. The architecture diagram is drawn from the rig YAMLs.
- **Setup** — one sub-tab per loaded rig, each the familiar setup page: Pi cards (Check /
  Install / Deploy / Restart API / Reboot / Shutdown), the device catalog and device cards,
  calibration, Slack and Monitor toggles, Save Rig. Pi identity is edited in Network, not here.
- **Experiment** — one sub-tab per loaded rig, each the familiar experiment page. "Load Rig"
  became **Connect** (still a button: connecting stops leftover processes and re-initializes
  devices on the Pis). Transfer is gone; **New session** returns an ended rig to Connected.
  The Save checkboxes (Camera, Lick, Reward, Photodiode, Run) stay.
- **Data** — syncing, purging and powering off, over SSH only (see below).

Hidden sub-tabs keep running (trial counters keep updating) but pause their camera stream; the
badge on each sub-tab shows the phase, trial count and hit rate. Closing a rig in both Setup
and Experiment unloads it from the controller.

## Do rigs need different ports? No.

The ports on the Pis (5080, 5572, 5573, 5575, 5581, 5582) are opened on each Pi separately, so
four rigs with the same numbers never clash. On the controller only one port is opened, UDP 5571,
once at start-up. Every datagram is sorted by the sender's IP to the rig whose Pi has that IP;
as a backup every leader event also carries a `rig` field (that is how the mock Pis, all at
127.0.0.1, are told apart in the tests). The only thing that must be unique per rig is the IP
pair. Datagrams nobody owns are counted and shown at the bottom of the Network diagram.

## Addresses and switch capacity

Everything shares one flat subnet, `192.168.10.0/24`, with no VLANs:

| Range | Use |
|---|---|
| `.1` | the controller (`controller_ip` in `controller.yaml`; new pairs are derived from its /24) |
| `.101`–`.250` | rig IP pairs from **Add rig**: leaders `.101, .103 … .249`, follower = leader + 1 |
| `.251`–`.254` | infrastructure, never suggested — the switch's web UI is **`.254`** |

The suggester only checks the rig YAMLs, so nothing that isn't a rig may use `.101`–`.250`.

The switch is a **Zyxel XGS1210-12**: 8×1G + 2×2.5G RJ45, 2×10G SFP+. The controller sits on one
SFP+ port over a DAC; the other is reserved. That leaves **10 RJ45 ports, i.e. 5 rigs of two Pis**.
For more, chain a second switch on the spare SFP+ port — 10G between switches, so the uplink
doesn't become the bottleneck — and give it `.253`.

Why the controller link is 10G: the Data tab syncs up to `parallel_rigs` leaders at once, each at
its Pi's 1G. On a 1G controller port those syncs would saturate the one link that every *running*
rig's UDP also crosses. The per-rig guard skips only the rig being synced, not its neighbours.

## The Data tab

Load a rig or a group. Each rig gets a card with a checkbox (which rigs the buttons act on),
the leader's SSH status, hostname, whether the experiment engine is running, free disk on the
data drive and the video SSD, and the list of **date folders** (`<mouse>/<mouse>_<date>`):

- **green** — every tree that holds the folder (data dir, video dir) has nothing left to copy in a
  strict rsync dry run, and no session that recorded video is missing it
- **red** — something is still pending
- **missing** (red, "video missing") — a session recorded `camera_saved: true` but its video is not
  on the Pi. Never purged, and Sync & Poweroff leaves the rig on
- **grey** — the check can't be trusted: no SSH, a dry run failed (hover the folder for why), the
  video drive is not mounted (`data.video_mount`), or the leader needs a Deploy

No nonzero rsync exit counts as clean — exit 23 is also what an unreadable subdirectory gives. A
folder labelled **no video** is normal: the camera was unchecked for those sessions (see
[Sessions without video](#sessions-without-video)).

One **Data root** for all rigs (the field, or Browse…). Sessions land subject-first, exactly as
before: `<data root>/<mouse>/<mouse>_<date>/<session_id>/`. The rig that ran a session is in
its metadata and in the subject index (`<data root>/subjects/<mouse>.json`, written when the
session ends). If two rigs ever hold the same session id, the sync refuses that folder.

**Sync Now** — pick folders from the list (everything not green is pre-selected). For each folder
the controller takes a fresh inventory of it on the Pi, consolidates any session not yet folded
into one `.h5` (on the Pi, idempotent), and copies **whichever trees hold the folder** with
`rsync -rlt` over SSH (partial files are kept in `.rsync-partial` so a cancelled copy resumes).
It then re-inventories and runs the dry run again. Only if the folder's trees are unchanged, every
copy is clean and no recorded video is missing does it mark the folder green and record it in
`<data root>/.vrfarm_sync_ledger.json` (with `present: {data, video}`). Shepherd logs are mirrored
to `<data root>/shepherd_logs/<rig>/`.

**Sync & Poweroff** — first a dialog listing, per rig, what will be copied, which rigs are skipped
(running, unreachable, Deploy needed) and which folders can't be verified. On Yes: every folder
that is not green on every checked rig is synced, then for each rig where everything verified the
controller runs `sudo poweroff` on every follower and then on the leader and waits for port 22 to
close. A rig with a failed, missing or grey folder stays on. Do not cut power until a rig shows
**OFF**.

**Purge Data** — a dialog lists the folders that are green and fully consolidated, video-only
folders included. On confirm the controller takes a fresh inventory and re-runs the strict dry run
for every tree that holds each folder (plus a checksum pass if `verify_checksum_before_purge` is
on), then names exactly the copies that passed to the Pi (`--verified data:<folder>`,
`--verified video:<folder>`). The Pi-side script deletes only those. It refuses a whole folder if a
copy sits in a tree that was not verified or a verified copy has vanished, and refuses the whole
call if the engine is alive, a tree is unavailable, or no `--verified` was sent (an older
controller). It only accepts `<mouse>/<mouse>_YYYYMMDD` names inside the configured dirs.

**Auto purge** (toggle) — Sync Now and Sync & Poweroff purge each folder on the Pi right after its
verified copy, through the same re-verification as Purge Data. Stored in `controller.yaml`.

A job's progress (overall and per rig), its log and a Cancel button appear under the buttons.
One job runs at a time; inside a job the rigs run in parallel.

### Sessions without video

Leaving **Camera** unchecked at GO means no video folder is created for that session, so a date
folder may exist only in the data tree. The Data tab copies and purges tree by tree, so such a
folder syncs green and purges normally; a video folder whose data copy is already gone works the
same way.

To tell "not recorded" from "lost", GO sends the leader `camera_requested` (Camera checked) and
`camera_saved` (the camera Pi confirmed recording started) in the START message. The engine writes
them to `metadata.yaml`, and consolidation carries them into the `.h5` root attrs. A session with
`camera_saved: true` and no video on the leader is **missing**. Sessions recorded before this carry
neither flag and count as unknown, never as missing. A camera that was checked but never started
recording counts as not saved (GO already warned), and the sync log notes it.

If `video_dir` is on its own drive, name its mountpoint in the rig YAML:

```yaml
data:
  video_dir: /media/vruser/ssd/video
  video_mount: /media/vruser/ssd
```

While that path is not a mountpoint the video tree is **unavailable**: every folder of the rig is
grey, syncs fail after copying the data, consolidation waits, and nothing is purged. Without
`video_mount`, an unmounted drive would look like an empty video folder. cheddar keeps its video on
the boot NVMe (`/home/vruser/video`) and needs no `video_mount`.

The leader side is `shared/leader_data.py` protocol 2, which ships with **Deploy**. Until a rig is
deployed, its card says **Deploy needed** and sync and purge are refused.

## What runs where

| Piece | Where | Installed by |
|---|---|---|
| `rsync` >= 3.1 | controller | `conda install -n vrfarm -c conda-forge rsync` — `rsync_path: null` looks only in the env. On Linux, `rsync_path: /usr/bin/rsync` in `controller/configs/controller.yaml` works too (fystyk's env has no rsync, so it needs one of the two). On macOS `/usr/bin/rsync` is Apple's openrsync and is rejected with a banner in the Data tab. |
| `rsync` | every Pi | the Install step's apt list |
| `shared/leader_data.py` | leader Pis | rides Deploy and Install (`shared/deploy_manifest.py`) |

A rig installed before this change needs a **Deploy** (ships `leader_data.py`) and either a
re-**Install** or `sudo apt install rsync` on the leader. The Data tab says which is missing.

## Settings: `controller/configs/controller.yaml`

Machine-specific, gitignored, created on first run from `controller.example.yaml` in the same
folder: UI port, the controller's IP (for the diagram), the event port, data root, auto purge, the
rsync path, sync tuning (`parallel_rigs`, `bwlimit_mbps`, `verify_checksum_before_purge`,
`shepherd_logs_keep_days`) and the rig groups. Per-rig settings stay in `rigs/<rig>.yaml`.
`VRFARM_SETTINGS=<path>` points the app at another file (the tests use a scratch one).

The file used to sit at the repo root. A `controller.yaml` still there is moved into
`controller/configs/` the next time the controller starts; if one already exists there, that one
wins and the old file is ignored (the log says so).

## Calibration files per rig

Geometry, warp map and luminance files are looked up in `display_calibration/<rig>/` when that
folder exists, else in the shared `display_calibration/` (the pre-multi-rig location). Give a
rig its own folder once:

```bash
python tools/migrate_calibration_dir.py cheddar
```

The geometry tool on the display Pi reports a saved geometry back to
`/api/rigs/<rig>/setup/receive_geometry`; the Setup tab passes that address to `cal_start.sh`
as `CAL_MAC_URL`.

## Adding a rig

1. Network → **Add rig**: name it, accept the suggested IP pair, keep the default devices.
2. Set the two Pis' static IPs to match, put them on two free RJ45 ports of the switch. Not while
   another rig is mid-session: recabling the switch once stalled a live Pi port for minutes.
3. Setup → Load rig → Pi cards → **Install** on each Pi (first time), then **Deploy**.
4. Setup → **Initialize** and calibrate the display as usual.
5. Network → add the rig to a group if you run it with others.

## Testing without hardware

```bash
python tools/smoke_multirig.py     # two fake rigs (mock Pis) through connect/deploy/go/end/reset
python tools/smoke_data.py         # the Data tab against a scratch data tree on this machine
python tools/capture_ui_shots.py   # Playwright walkthrough of all four tabs -> docs/images/
```
