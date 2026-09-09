# Multi-rig controller

**Last updated:** 2026-09-08

One controller Mac runs several rigs from one web app on one port. This page explains what
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

## The Data tab

Load a rig or a group. Each rig gets a card with a checkbox (which rigs the buttons act on),
the leader's SSH status, hostname, whether the experiment engine is running, free disk on the
data drive and the video SSD, and the list of **date folders** (`<mouse>/<mouse>_<date>`):

- **green** — an rsync dry run finds nothing left to copy in either tree (data dir and video dir)
- **red** — something is still pending
- grey — the check itself failed (no SSH, rsync missing on the Pi, …)

One **Data root** for all rigs (the field, or Browse…). Sessions land subject-first, exactly as
before: `<data root>/<mouse>/<mouse>_<date>/<session_id>/`. The rig that ran a session is in
its metadata and in the subject index (`<data root>/subjects/<mouse>.json`, written when the
session ends). If two rigs ever hold the same session id, the sync refuses that folder.

**Sync Now** — pick folders from the list (red ones are pre-selected). For each folder the
controller: consolidates any session not yet folded into one `.h5` (on the Pi, idempotent),
copies the data folder and the video folder with `rsync -rlt` over SSH (partial files are
kept in `.rsync-partial` so a cancelled copy resumes), runs the dry run again and only then
marks the folder green and records it in `<data root>/.vrfarm_sync_ledger.json`. Shepherd
logs are mirrored to `<data root>/shepherd_logs/<rig>/`.

**Sync & Poweroff** — first a dialog listing, per rig, what will be copied and which rigs are
skipped (running, unreachable). On Yes: every red folder of every checked rig is synced, then
for each rig where everything succeeded the controller runs `sudo poweroff` on every follower
and then on the leader and waits for port 22 to close. A rig with a failed folder stays on.
Do not cut power until a rig shows **OFF**.

**Purge Data** — a dialog lists the folders that are green in both trees and fully
consolidated; on confirm each is re-checked right before deletion and removed from both trees
on the Pi. Unsynced or unconsolidated folders are never listed. The Pi-side script only
accepts `<mouse>/<mouse>_YYYYMMDD` names inside the two configured data dirs and refuses to run
while the engine is alive.

**Auto purge** (toggle) — Sync Now and Sync & Poweroff delete each folder on the Pi right after
its verified copy. Stored in `controller.yaml`.

A job's progress (overall and per rig), its log and a Cancel button appear under the buttons.
One job runs at a time; inside a job the rigs run in parallel.

## What runs where

| Piece | Where | Installed by |
|---|---|---|
| `rsync` >= 3.1 | controller (`vrfarm` env) | `conda install -n vrfarm -c conda-forge rsync`. Apple's `/usr/bin/rsync` is openrsync and is rejected with a banner in the Data tab. |
| `rsync` | every Pi | the Install step's apt list |
| `shared/leader_data.py` | leader Pis | rides Deploy and Install (`shared/deploy_manifest.py`) |

A rig installed before this change needs a **Deploy** (ships `leader_data.py`) and either a
re-**Install** or `sudo apt install rsync` on the leader. The Data tab says which is missing.

## Settings: `controller.yaml`

Machine-specific, gitignored, created from `controller.example.yaml` on first run: UI port,
the controller's IP (for the diagram), the event port, data root, auto purge, the rsync path,
sync tuning (`parallel_rigs`, `bwlimit_mbps`, `verify_checksum_before_purge`,
`shepherd_logs_keep_days`) and the rig groups. Per-rig settings stay in `rigs/<rig>.yaml`.
`VRFARM_SETTINGS=<path>` points the app at another file (the tests use a scratch one).

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
2. Set the two Pis' static IPs to match, put them on the switch.
3. Setup → Load rig → Pi cards → **Install** on each Pi (first time), then **Deploy**.
4. Setup → **Initialize** and calibrate the display as usual.
5. Network → add the rig to a group if you run it with others.

## Testing without hardware

```bash
python tools/smoke_multirig.py     # two fake rigs (mock Pis) through connect/deploy/go/end/reset
python tools/smoke_data.py         # the Data tab against a scratch data tree on this machine
python tools/capture_ui_shots.py   # Playwright walkthrough of all four tabs -> docs/images/
```
