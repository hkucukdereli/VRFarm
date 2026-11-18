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
  .101/.102, .103/.104, …), edit Pi names / IPs / roles / users / devices, pick the rig's colour
  (a drop-down of nine generated colours, `controller/palette.py`; Add rig takes a free one at
  random, stored as `color:` in the rig YAML and painted on the rig's tabs in Setup and
  Experiment), rename or delete a
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
  strict rsync dry run, no session that recorded video is missing it, and the **content** of every
  file was compared with the Pi's and the Pi's files have not changed since
  (see [The content check](#the-content-check))
- **red** — something is still pending: files left to copy, or a complete copy whose content is not
  checked yet. The card then adds *content not checked*; Sync Now runs the check, and copies nothing
  again unless a file differs
- **missing** (red, "video missing") — a session recorded `camera_saved: true` but its video is not
  on the Pi. Never purged, and Sync & Poweroff leaves the rig on
- **grey** — the check can't be trusted: no SSH, a dry run failed (hover the folder for why), the
  video drive is not mounted (`data.video_mount`), the leader needs a Deploy, or the folder's files
  could not be listed for the content check

No nonzero rsync exit counts as clean — exit 23 is also what an unreadable subdirectory gives. A
folder labelled **no video** is normal: the camera was unchecked for those sessions (see
[Sessions without video](#sessions-without-video)).

One **Data root** for all rigs (the field, or Browse…). Sessions land subject-first, exactly as
before: `<data root>/<mouse>/<mouse>_<date>/<session_id>/`. The rig that ran a session is in
its metadata and in the subject index (`<data root>/subjects/<mouse>.json`, written when the
session ends). If two rigs ever hold the same session id, the sync refuses that folder.

**Sync Now** — pick folders from the list (everything not green is pre-selected). For each folder
the controller takes a fresh inventory of it on the Pi, consolidates any session not yet folded
into one `.h5` and remuxes any raw `video.h264` into `video.mp4` (both on the Pi, idempotent; a
complete leftover `video.mp4.part` is adopted, otherwise ffmpeg takes ~10-30 s per long session —
the card shows *N video to remux*), and copies **whichever trees hold the folder** with
`rsync -rlt` over SSH (partial files are kept in `.rsync-partial` so a cancelled copy resumes).
It then re-inventories and runs the dry run again. Only if the folder's trees are unchanged, every
copy is clean and no recorded video is missing does it record the folder in
`<data root>/.vrfarm_sync_ledger.json` (with `present: {data, video}`). Last comes the
[content check](#the-content-check): every file is hashed on the Pi and under the data root, a copy
that differs is fetched again, and only a passed check (`verified` in the ledger entry) makes the
folder green. Shepherd logs are mirrored to `<data root>/shepherd_logs/<rig>/`.

**Sync & Poweroff** — first a dialog listing, per rig, what will be copied, which rigs are skipped
(running, unreachable, Deploy needed) and which folders can't be verified. A folder marked
*content check only* is already copied; the sync only compares its content. On Yes: every folder
that is not green on every checked rig is synced, then for each rig where everything verified the
controller runs `sudo poweroff` on every follower and then on the leader and waits for port 22 to
close. A rig with a failed, missing or grey folder stays on. Do not cut power until a rig shows
**OFF**.

**Purge Data** — a dialog lists the folders that are green and fully consolidated, video-only
folders included. On confirm the controller takes a fresh inventory, refuses any folder whose
content check is missing or no longer matches the files on the Pi, and re-runs the strict dry run
for every tree that holds each folder. With `verify_checksum_before_purge` on it then hashes every
file on both sides once more; a difference stops the purge of that folder and turns it red, and the
next Sync Now fetches the file again. Then it names exactly the copies that passed to the Pi
(`--verified data:<folder>`, `--verified video:<folder>`). The Pi-side script deletes only those.
It refuses a whole folder if a copy sits in a tree that was not verified or a verified copy has
vanished, and refuses the whole call if the engine is alive, a tree is unavailable, or no
`--verified` was sent (an older controller). It only accepts `<mouse>/<mouse>_YYYYMMDD` names inside
the configured dirs.

**Auto purge** (toggle) — Sync Now and Sync & Poweroff purge each folder on the Pi right after its
verified copy, through the same re-verification as Purge Data (minus the second hashing: the sync
checked the content a moment before). Stored in `controller.yaml`.

A job's progress (overall and per rig), its log and a Cancel button appear under the buttons.
One job runs at a time; inside a job the rigs run in parallel.

### The content check

rsync calls a file copied when its size and modification time agree, and it copies whatever the
Pi's kernel hands it when it reads the file. Neither says that the bytes under the data root are the
bytes on the Pi's disk — and for a while they were not always. On the Raspberry Pi 5 kernel
`6.18.34+rpt-rpi-2712`, reads through the page cache returned wrong bytes once memory had filled
(morbier and pecorino leaders, found 2026-11-12). The disk itself was right and so was every read
with `O_DIRECT`; `6.18.39+rpt-rpi-2712` does not show it (see [The Pi 5 kernel](#the-pi-5-kernel)).
A copy made through such a read holds wrong bytes and still passes every rsync check, `--checksum`
included, because that hashes what the same cache hands out.

So every sync ends by comparing the bytes themselves:

1. The leader hashes (SHA-256) every file of the folder that rsync copies, each read with
   `O_DIRECT`, that is from the disk and not from the page cache (`leader_data hash`).
2. The controller hashes its own copy the same way and compares size and hash, file by file.
3. A copy that differs is fetched again: the leader streams the file from a second direct read
   (`leader_data read`), and the controller puts it in place only if it has the size and hash of
   the first direct read, gives it the Pi's modification time, and reads it back from its own disk.
   The copy it replaced is kept as
   `<data root>/.vrfarm_replaced/<mouse>/<mouse>_<date>/<session>/<file>.<YYYYmmdd-HHMMSS>`, and the
   job log and the ledger entry (`repairs`) name the file.
4. The ledger entry gets `verified`: the time, the number of files and bytes, whether each side
   read direct, the files fetched again, and the leader's **fingerprint** of the folder — one hash
   per tree over the names, sizes and modification times (in nanoseconds) of its files.

The inventory reports that fingerprint on every refresh, and a folder is green only while it equals
the one in the ledger. A file that changes on the Pi after the check therefore turns the folder red
again (*its files on the Pi changed since the content check*), including a change of the
modification time that rsync's one-second window (`--modify-window=1`) does not see. Purge Data
refuses a folder in that state.

What it does not cover:

- It compares the Pi's disk with the controller's disk. Bytes that were already wrong when they were
  written on the Pi are copied faithfully and match.
- It checks the copy as it was at that moment. A copy under the data root that is altered later with
  the same size and time still shows green; `verify_checksum_before_purge: true` catches it, because
  Purge Data then hashes both sides once more before the source is deleted.
- A folder that was purged before the check existed (2026-11-13) has no source left to compare with.
- A file the check replaced has the size and time of the copy it replaced. Anything that mirrors
  the data root by size and time (the nightly upload to the group drive does) will not pick it up:
  copy the files named under `.vrfarm_replaced/` there again by hand.

A folder synced before 2026-11-13 has no `verified` in its ledger entry: it shows red with *content
not checked* until a Sync Now (or Sync & Poweroff) has checked it. The check reads every file once
on each side, so it adds time in proportion to the folder's size; the hash alone runs at about
1.4 GB/s on a Pi 5 and 0.5 GB/s on the controller. Tests: `tools/test_leader_hash.py` (the pieces)
and cases T8 to T10 of `tools/smoke_data.py`.

### The Pi 5 kernel

Every leader must run a kernel without the read fault. morbier and pecorino were moved from
`6.18.34+rpt-rpi-2712` to `6.18.39+rpt-rpi-2712`, the kernel cheddar already ran, on 2026-11-12:

```bash
sudo apt-get install --no-install-recommends linux-image-6.18.39+rpt-rpi-2712 linux-image-6.18.39+rpt-rpi-v8
sudo reboot
```

The read test in `~/pi_read_test/` on the controller (`readtest.py`: hash a few GB of files through
the page cache and again with direct reads, with memory held so the cache has to recycle) gave 75 of
102 files wrong on morbier and 196 of 1600 on pecorino on the old kernel, 186 of 1600 on pecorino
after a plain reboot of the old kernel, and 0 in three runs each on the new one. The followers
(Pi 4) showed no wrong read on either kernel, one run each.

- A Pi set up from the same image comes with the old kernel: run the read test on every new leader
  before its first session. `uname -r` shows the running kernel.
- The kernel meta packages on the Pis still name 6.18.34, and a plain `apt upgrade` would install a
  newer kernel (6.18.60 was the candidate on 2026-11-12) that has not been through the read test.
  Run the test after any kernel change.
- Back to the old kernel: `sudo apt remove linux-image-6.18.39+rpt-rpi-2712
  linux-image-6.18.39+rpt-rpi-v8`, then `sudo dpkg-reconfigure linux-image-6.18.34+rpt-rpi-2712
  linux-image-6.18.34+rpt-rpi-v8`, reboot. morbier and pecorino also keep the boot files of
  6.18.34 in `~/boot_firmware_backup_6.18.34/`.

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

The leader side is `shared/leader_data.py` protocol 3 (2026-11-13: the `hash` and `read` commands
and the `fp` fingerprint in the inventory), which ships with **Deploy**. Until a rig is deployed
with it, its card says **Deploy needed**, all its folders are grey, and sync and purge are refused.

## What runs where

| Piece | Where | Installed by |
|---|---|---|
| `rsync` >= 3.1 | controller | `conda install -n vrfarm -c conda-forge rsync` — `rsync_path: null` looks only in the env. On Linux, `rsync_path: /usr/bin/rsync` in `controller/configs/controller.yaml` works too (fystyk's env has no rsync, so it needs one of the two). On macOS `/usr/bin/rsync` is Apple's openrsync and is rejected with a banner in the Data tab. |
| `rsync` | every Pi | the Install step's apt list |
| `shared/leader_data.py` | leader Pis | rides Deploy and Install (`shared/deploy_manifest.py`) |

A rig installed before this change needs a **Deploy** (ships `leader_data.py`) and either a
re-**Install** or `sudo apt install rsync` on the leader. The Data tab says which is missing.

## Settings: `controller/configs/controller.yaml`

Machine-specific, gitignored, created on first run from `controller_template.yaml` in the same
folder: UI port, the controller's IP (for the diagram), the event port, data root, auto purge, the
rsync path, sync tuning (`parallel_rigs`, `bwlimit_mbps`, `verify_checksum_before_purge`,
`shepherd_logs_keep_days`) and the rig groups. Per-rig settings stay in `rigs/<rig>.yaml`.
`VRFARM_SETTINGS=<path>` points the app at another file (the tests use a scratch one).
`verify_checksum_before_purge: true` makes Purge Data hash every file on the Pi and under the data
root once more before it deletes (see [The content check](#the-content-check)); every sync checks
content whatever this is set to.

The file used to sit at the repo root. A `controller.yaml` still there is moved into
`controller/configs/` the next time the controller starts; if one already exists there, that one
wins and the old file is ignored (the log says so).

## Calibration files per rig

Each rig has `display_calibration/<rig>/geometry/` (dated geometry files), `intensity/`
(intensity calibrations: measured `intensity_cal_*` plus theoretical and none mocks) and its `warp_map.npz`; see
[display_calibration/README.md](../display_calibration/README.md#where-the-files-live). The
folders are created the first time the Setup tab touches a rig; a rig without geometry is seeded
from `display_calibration/_template/geometry/rig_geometry_template.yaml`.

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
python tools/test_leader_hash.py   # the content check piece by piece (direct reads, hash, fetch)
python tools/capture_ui_shots.py   # Playwright walkthrough of all four tabs -> docs/images/
```
