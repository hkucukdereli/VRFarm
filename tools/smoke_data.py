#!/usr/bin/env python3
"""
tools/smoke_data.py — the Data tab against a scratch "Pi" on this machine, no hardware.

Builds a scratch rig whose leader is 127.0.0.1 (the controller's SSH helpers run commands
locally for loopback targets), a scratch data tree with two mice x two dates (one session
consolidated, one not, one folder with no h5, plus video files on a separate "SSD" tree),
a scratch data root and scratch settings, then drives /api/data end to end:

  inventory -> all red;  Sync Now one folder -> green + ledger;  purge preview lists only the
  consolidated green one;  purge deletes it and nothing else;  auto-purge sync deletes after
  the verified copy;  Sync & Poweroff preview lists the red folders and the job logs the
  suppressed power-off;  then sessions without video and purge safety:
    T1  a data-only folder (camera unchecked) syncs green, would power off, purges the data tree only
    T2  a video-only folder syncs green and purges the video tree only
    T3  camera_saved: true with no video is "missing": not purgeable, and the rig stays on
    T4  an unreadable subdirectory greys only its own folder and is never purged (skipped as root)
    T5  an unmounted data.video_mount greys the rig and fails its syncs; the Pi refuses purges
  a cancelled copy leaves a resumable partial;  the rsync guard trips on Apple's openrsync.

The scratch settings use a real rsync >= 3.1: the vrfarm env's, else the one on PATH.

    conda activate vrfarm
    python tools/smoke_data.py           # ~60 s; exit code 0 on PASS
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _http(method, url, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def make_h5(path: Path, consolidated: bool, **attrs):
    import h5py
    import numpy as np
    with h5py.File(str(path), "w") as f:
        f.create_dataset("trial_num", data=np.arange(5))
        if consolidated:
            f.attrs["format_version"] = 2
            f.create_group("trials")
        for k, v in attrs.items():
            f.attrs[k] = v


def seed(pi_data: Path, pi_video: Path):
    import numpy as np
    """two mice x two dates; M1 day1: consolidated; M1 day2: unconsolidated (sidecars);
    M2 day1: no h5 at all; every session gets a fake video on the 'SSD' tree."""
    layout = {
        ("M1", "20260901", "M1_20260901_001"): "consolidated",
        ("M1", "20260902", "M1_20260902_001"): "raw",
        ("M2", "20260901", "M2_20260901_001"): "no_h5",
    }
    for (subj, date, sid), kind in layout.items():
        d = pi_data / subj / f"{subj}_{date}" / sid
        d.mkdir(parents=True)
        if kind != "no_h5":
            make_h5(d / f"{sid}.h5", consolidated=(kind == "consolidated"))
        if kind == "raw":
            (d / "metadata.yaml").write_text("session_id: %s\nsubject_id: %s\nrig_name: scratch\n" % (sid, subj))
            (d / "trials.yaml").write_text("- trial: 0\n")
        v = pi_video / subj / f"{subj}_{date}" / sid
        v.mkdir(parents=True)
        (v / "video.mp4").write_bytes(os.urandom(2_000_000))
        np.save(v / "frame_timestamps.npy", np.zeros((10, 3)))
    # a real stimulus archive, like shared/stim_generator.py writes (consolidate folds it into /stimulus)
    np.savez(pi_data / "M1" / "M1_20260902" / "M1_20260902_001" / "stimuli.npz",
             trial_idx=np.arange(5), stim_az_deg=np.full(5, 30.0), duration_s=np.full(5, 2.0),
             iti_durations=np.full(5, 8.0), background_gray=np.float64(0.75), shape=np.str_("square"))


def real_rsync() -> str | None:
    """A real rsync for the scratch settings: the env's (conda-forge), else the one on PATH —
    never Apple's openrsync, which the controller rejects."""
    for cand in (Path(sys.prefix) / "bin" / "rsync", shutil.which("rsync")):
        if cand and Path(cand).exists():
            out = subprocess.run([str(cand), "--version"], capture_output=True, text=True).stdout
            if "openrsync" not in out:
                return str(cand)
    return None


def leader_data(*args) -> dict:
    """Run the Pi-side script directly, as the leader would."""
    r = subprocess.run([PY, "-m", "shared.leader_data", *args], cwd=str(ROOT), capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": None, "error": (r.stderr or r.stdout)[-300:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5056)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="vrfarm_data_smoke_"))
    pi_data, pi_video, root, rigs_dir = scratch / "pi_data", scratch / "pi_video", scratch / "data_root", scratch / "rigs"
    rigs_dir.mkdir()
    seed(pi_data, pi_video)
    (rigs_dir / "scratch.yaml").write_text(f"""name: scratch
data:
  leader_dir: {pi_data}
  video_dir: {pi_video}
network: {{api_port: 5099, event_port: 5571, command_port: 5572, display_port: 5575}}
devices: {{}}
pis:
- {{name: scratch-leader, ip: 127.0.0.1, role: leader, user: {os.environ.get('USER', 'vruser')}, devices: []}}
- {{name: scratch-dlp, ip: 127.0.0.1, role: follower, user: {os.environ.get('USER', 'vruser')}, devices: []}}
slack: {{enabled: false, webhook_url: ''}}
""")
    settings_file = scratch / "controller.yaml"
    rsync = real_rsync()
    # bwlimit: local rsync would otherwise finish a 240 MB copy before the cancel test can fire
    settings_file.write_text(f"ui_port: {args.port}\ndata_root: {root}\nauto_purge: false\nevent_port: 5591\n"
                             "sync: {parallel_rigs: 4, bwlimit_mbps: 200}\n"
                             + (f"rsync_path: {rsync}\n" if rsync else ""))
    env = dict(os.environ, VRFARM_SETTINGS=str(settings_file), PYTHONUNBUFFERED="1")
    logf = open(scratch / "controller.log", "w")
    proc = subprocess.Popen([PY, "controller/app.py", "--port", str(args.port), "--no-browser",
                             "--rigs-dir", str(rigs_dir)], cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{args.port}"
    failures = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            failures.append(msg)

    def wait_job(job_id, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = _http("GET", f"{base}/api/data/jobs/{job_id}")
            if j.get("status") in ("done", "failed", "cancelled"):
                return j
            time.sleep(0.4)
        return _http("GET", f"{base}/api/data/jobs/{job_id}")

    def inv():
        return _http("POST", f"{base}/api/data/inventory", {"rigs": ["scratch"]})["rigs"]["scratch"]

    def statuses():
        return {f["key"]: f["status"] for f in inv()["folders"]}

    def folder(key):
        return next((f for f in inv()["folders"] if f["key"] == key), None)

    def ledger():
        return json.loads((root / ".vrfarm_sync_ledger.json").read_text())["folders"]

    def purge_preview():         # the card is fresh: callers ran inv() just before
        return [f["key"] for f in _http("POST", f"{base}/api/data/purge/preview", {"rigs": ["scratch"]})["rigs"]["scratch"]["folders"]]

    def purge(keys):
        d = _http("POST", f"{base}/api/data/purge", {"items": {"scratch": keys}, "confirm": True})
        if not d.get("ok"):
            return {"status": "rejected", "log": [], "result": {"scratch": {"purged": [], "refused": [], "error": d.get("error")}}}
        return wait_job(d["job"]["id"])

    def sync_now(keys):
        return wait_job(_http("POST", f"{base}/api/data/sync", {"items": {"scratch": keys}})["job"]["id"])

    def sync_poweroff():
        return wait_job(_http("POST", f"{base}/api/data/sync_poweroff", {"rigs": ["scratch"], "confirm": True})["job"]["id"])

    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/api/data/settings", timeout=2).read()
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise SystemExit("controller did not start; see " + str(scratch / "controller.log"))
        s = _http("GET", f"{base}/api/data/settings")
        check(s["data_root"] == str(root), f"scratch data root in use ({s['data_root']})")
        check(s["rsync"]["ok"], f"rsync ok: {s['rsync']}")

        d = _http("POST", f"{base}/api/data/load", {"rig": "scratch"})
        check(d["ok"], "scratch rig loaded")
        card = inv()
        check(card["ssh_ok"] and not card.get("error"), f"inventory via local passthrough ({card.get('error')})")
        check(card["engine_running"] is False, "engine not running")
        keys = {f["key"] for f in card["folders"]}
        check(keys == {"M1/M1_20260901", "M1/M1_20260902", "M2/M2_20260901"}, f"three date folders: {sorted(keys)}")
        check(all(f["status"] == "red" for f in card["folders"]), "everything red before any sync")
        f2 = next(f for f in card["folders"] if f["key"] == "M1/M1_20260902")
        check(f2["n_unconsolidated"] == 1, "M1 day2 flagged unconsolidated")
        check(card["video_tree"] is True and f2["bytes_video"] > 1_900_000, "video tree seen with sizes")

        # Sync Now: the consolidated folder only
        d = _http("POST", f"{base}/api/data/sync", {"items": {"scratch": ["M1/M1_20260901"]}})
        check(d["ok"], f"sync job accepted ({d.get('error')})")
        j = wait_job(d["job"]["id"])
        check(j["status"] == "done" and j["result"]["scratch"]["synced"] == ["M1/M1_20260901"], f"sync job done: {j['status']} {j['result']['scratch']}")
        check((root / "M1/M1_20260901/M1_20260901_001/M1_20260901_001.h5").exists()
              and (root / "M1/M1_20260901/M1_20260901_001/video.mp4").exists(), "h5 + video landed under the data root, subject-first")
        led = json.loads((root / ".vrfarm_sync_ledger.json").read_text())
        check("scratch|M1|20260901" in led["folders"], "ledger entry written")
        st = statuses()
        check(st["M1/M1_20260901"] == "green" and st["M1/M1_20260902"] == "red", f"status after sync: {st}")

        # Purge preview lists only the green consolidated folder; purge deletes just that
        p = _http("POST", f"{base}/api/data/purge/preview", {"rigs": ["scratch"]})["rigs"]["scratch"]
        check([f["key"] for f in p["folders"]] == ["M1/M1_20260901"], f"purge preview: {[f['key'] for f in p['folders']]}")
        d = _http("POST", f"{base}/api/data/purge", {"items": {"scratch": ["M1/M1_20260901"]}, "confirm": True})
        j = wait_job(d["job"]["id"])
        check(j["status"] == "done" and j["result"]["scratch"]["purged"] == ["M1/M1_20260901"], f"purge job: {j['status']} {j['result']['scratch']}")
        check(not (pi_data / "M1/M1_20260901").exists() and not (pi_video / "M1/M1_20260901").exists()
              and (pi_data / "M1/M1_20260902").exists() and (pi_video / "M2/M2_20260901").exists(), "purged both trees, others untouched")
        check((root / "M1/M1_20260901/M1_20260901_001/video.mp4").exists(), "controller copy kept")

        # purge refuses a red folder
        d = _http("POST", f"{base}/api/data/purge", {"items": {"scratch": ["M1/M1_20260902"]}, "confirm": True})
        j = wait_job(d["job"]["id"])
        check((pi_data / "M1/M1_20260902").exists() and not j["result"]["scratch"]["purged"], "purge of an unsynced folder refused")

        # auto-purge: sync the raw folder -> consolidated, copied, verified, deleted on the Pi
        _http("PUT", f"{base}/api/data/settings", {"auto_purge": True})
        d = _http("POST", f"{base}/api/data/sync", {"items": {"scratch": ["M1/M1_20260902"]}})
        j = wait_job(d["job"]["id"])
        res = j["result"]["scratch"]
        check(j["status"] == "done" and res["synced"] == ["M1/M1_20260902"], f"auto-purge sync done: {res}")
        h5 = root / "M1/M1_20260902/M1_20260902_001/M1_20260902_001.h5"
        check(h5.exists() and not (root / "M1/M1_20260902/M1_20260902_001/metadata.yaml").exists(), "raw session consolidated before copy (no sidecars)")
        check(res["purged"] == ["M1/M1_20260902"] and not (pi_data / "M1/M1_20260902").exists(), "auto-purge deleted it on the Pi")
        _http("PUT", f"{base}/api/data/settings", {"auto_purge": False})

        # Sync & Poweroff: preview lists the remaining red folder; job syncs it and suppresses poweroff
        p = _http("POST", f"{base}/api/data/sync_poweroff/preview", {"rigs": ["scratch"]})["rigs"]["scratch"]
        check([f["key"] for f in p["folders"]] == ["M2/M2_20260901"] and p["followers"][0]["ssh_ok"], f"poweroff preview: {[f['key'] for f in p['folders']]}")
        d = _http("POST", f"{base}/api/data/sync_poweroff", {"rigs": ["scratch"], "confirm": True})
        j = wait_job(d["job"]["id"])
        check(j["status"] == "done" and j["result"]["scratch"]["synced"] == ["M2/M2_20260901"]
              and j["result"]["scratch"]["poweroff"] == "skipped", f"sync&poweroff: {j['status']} {j['result']['scratch']}")
        check(any("poweroff suppressed" in l["msg"] for l in j["log"]), "local poweroff suppressed and logged")
        fleet = _http("GET", f"{base}/api/fleet")["rigs"]["scratch"]
        check(fleet["phase"] == "offline", f"rig marked offline ({fleet['phase']})")
        check(statuses() == {"M2/M2_20260901": "green"}, f"only M2 left, green: {statuses()}")

        try:
            # T1 — camera unchecked: a date folder with no video folder at all
            for sid, attrs in (("M4_20260904_001", {"camera_requested": False, "camera_saved": False}),
                               ("M4_20260904_002", {})):                     # 002: no flag, like older sessions
                d4 = pi_data / "M4/M4_20260904" / sid
                d4.mkdir(parents=True)
                make_h5(d4 / f"{sid}.h5", True, **attrs)
            f4 = folder("M4/M4_20260904")
            check(f4 is not None and f4["status"] == "red" and f4["in_data"] and not f4["in_video"] and f4["missing_video"] == [],
                  f"T1 data-only folder: red, data tree only, nothing missing ({f4 and (f4['status'], f4['in_video'], f4['missing_video'])})")
            j = sync_poweroff()
            r = j["result"]["scratch"]
            check(j["status"] == "done" and r["synced"] == ["M4/M4_20260904"] and not r["failed"] and r["poweroff"] == "skipped"
                  and not any("NOT powered off" in l["msg"] for l in j["log"]), f"T1 Sync & Poweroff copies it and goes on to power off: {r}")
            check(folder("M4/M4_20260904")["status"] == "green", "T1 green after the sync")
            check(ledger()["scratch|M4|20260904"].get("present") == {"data": True, "video": False}, "T1 ledger records the data tree only")
            check("M4/M4_20260904" in purge_preview(), "T1 offered for purge")
            j = purge(["M4/M4_20260904"])
            check(j["result"]["scratch"]["purged"] == ["M4/M4_20260904"] and not (pi_data / "M4").exists()
                  and (pi_video / "M2/M2_20260901").exists(), f"T1 purge removed the data tree copy only ({j['result']['scratch']})")

            # T2 — a video-only folder (its data copy already gone)
            v6 = pi_video / "M6/M6_20260906/M6_20260906_001"
            v6.mkdir(parents=True)
            (v6 / "video.mp4").write_bytes(os.urandom(300_000))
            f6 = folder("M6/M6_20260906")
            check(f6 is not None and f6.get("video_only") and f6["status"] == "red", f"T2 video-only folder is red ({f6 and f6['status']})")
            j = sync_now(["M6/M6_20260906"])
            check(j["result"]["scratch"]["synced"] == ["M6/M6_20260906"] and (root / "M6/M6_20260906/M6_20260906_001/video.mp4").exists(),
                  f"T2 synced ({j['result']['scratch']})")
            check(folder("M6/M6_20260906")["status"] == "green" and "M6/M6_20260906" in purge_preview(), "T2 green and offered for purge")
            j = purge(["M6/M6_20260906"])
            check(j["result"]["scratch"]["purged"] == ["M6/M6_20260906"] and not (pi_video / "M6").exists()
                  and (pi_data / "M2/M2_20260901").exists(), f"T2 purge removed the video tree copy only ({j['result']['scratch']})")

            # T3 — video was recorded but is not on the Pi
            import h5py
            import numpy as np
            d5 = pi_data / "M5/M5_20260905/M5_20260905_001"
            d5.mkdir(parents=True)
            make_h5(d5 / "M5_20260905_001.h5", consolidated=False)
            (d5 / "metadata.yaml").write_text("session_id: M5_20260905_001\nsubject_id: M5\nrig_name: scratch\n"
                                              "camera_requested: true\ncamera_saved: true\n")
            (d5 / "trials.yaml").write_text("- trial: 0\n")
            np.savez(d5 / "stimuli.npz", trial_idx=np.arange(5), stim_az_deg=np.full(5, 30.0), duration_s=np.full(5, 2.0),
                     iti_durations=np.full(5, 8.0), background_gray=np.float64(0.75), shape=np.str_("square"))
            f5 = folder("M5/M5_20260905")
            check(f5 is not None and f5["status"] == "missing" and f5["missing_video"] == ["M5_20260905_001"] and f5.get("reason"),
                  f"T3 status missing, with a reason ({f5 and (f5['status'], f5.get('reason'))})")
            check("M5/M5_20260905" not in purge_preview(), "T3 not offered for purge")
            j = sync_poweroff()
            r = j["result"]["scratch"]
            check(r["synced"] == [] and [x["folder"] for x in r["failed"]] == ["M5/M5_20260905"]
                  and "video expected" in r["failed"][0]["error"] and r["poweroff"] == "skipped"
                  and any("NOT powered off" in l["msg"] for l in j["log"]), f"T3 Sync & Poweroff fails it and leaves the rig on: {r}")
            with h5py.File(root / "M5/M5_20260905/M5_20260905_001/M5_20260905_001.h5", "r") as f:
                check(f.attrs.get("format_version") == 2 and bool(f.attrs.get("camera_saved")) is True,
                      "T3 the copied .h5 is consolidated and carries camera_saved")
            j = purge(["M5/M5_20260905"])
            check(not j["result"]["scratch"]["purged"] and [x["folder"] for x in j["result"]["scratch"]["refused"]] == ["M5/M5_20260905"]
                  and d5.exists(), f"T3 a forced purge is refused ({j['result']['scratch']})")

            # T4 — an unreadable subdirectory: localised to its folder, never counted as copied
            if os.geteuid() == 0:
                print("  skip T4 (running as root: directory permissions are not enforced)")
            else:
                d8 = pi_data / "M8/M8_20260908/M8_20260908_001"
                d8.mkdir(parents=True)
                make_h5(d8 / "M8_20260908_001.h5", True, camera_saved=False)
                j = sync_now(["M8/M8_20260908"])
                check(j["result"]["scratch"]["synced"] == ["M8/M8_20260908"], f"T4 synced while readable ({j['result']['scratch']})")
                extra = d8 / "extra"
                extra.mkdir()
                (extra / "late.bin").write_bytes(b"x" * 1000)
                extra.chmod(0)
                try:
                    card = inv()
                    by = {f["key"]: f for f in card["folders"]}
                    check(by["M8/M8_20260908"]["status"] == "grey" and by["M8/M8_20260908"].get("reason"),
                          f"T4 the unreadable folder is grey, with a reason ({by['M8/M8_20260908']['status']}: {by['M8/M8_20260908'].get('reason')})")
                    check(by["M2/M2_20260901"]["status"] == "green", f"T4 localised: M2 is still green ({by['M2/M2_20260901']['status']})")
                    check(bool(card.get("status_errors")), "T4 the tree error is shown on the card")
                    check("M8/M8_20260908" not in purge_preview(), "T4 not offered for purge")
                    j = purge(["M8/M8_20260908"])
                    check(not j["result"]["scratch"]["purged"] and d8.exists(), f"T4 a forced purge is refused ({j['result']['scratch']})")
                finally:
                    extra.chmod(0o755)
                check((extra / "late.bin").exists(), "T4 the unreadable file survived")

            # T5 — the video drive is not mounted (data.video_mount names a plain directory)
            d9 = pi_data / "M9/M9_20260909/M9_20260909_001"
            d9.mkdir(parents=True)
            make_h5(d9 / "M9_20260909_001.h5", True, camera_saved=False)
            not_a_mount = scratch / "ssd"
            not_a_mount.mkdir()
            rig_yaml = rigs_dir / "scratch.yaml"
            plain = rig_yaml.read_text()
            rig_yaml.write_text(plain.replace(f"  video_dir: {pi_video}\n", f"  video_dir: {pi_video}\n  video_mount: {not_a_mount}\n"))
            try:
                check(_http("POST", f"{base}/api/data/load", {"rig": "scratch"})["ok"], "T5 rig re-read with video_mount")
                card = inv()
                check(not any(f["status"] == "green" for f in card["folders"])
                      and any("video tree unavailable" in e for e in card.get("status_errors", [])),
                      f"T5 no folder green while the drive is unmounted ({ {f['key']: f['status'] for f in card['folders']} })")
                j = sync_now(["M9/M9_20260909"])
                r = j["result"]["scratch"]
                check(not r["synced"] and r["failed"] and "video tree unavailable" in r["failed"][0]["error"]
                      and "scratch|M9|20260909" not in ledger(), f"T5 Sync Now fails, no ledger entry ({r})")
                out = leader_data("purge", "--leader-dir", str(pi_data), "--video-dir", str(pi_video), "--video-mount", str(not_a_mount),
                                  "--folder", "M9/M9_20260909", "--verified", "data:M9/M9_20260909")
                check(out.get("ok") is False and "video tree unavailable" in (out.get("error") or "") and d9.exists(),
                      f"T5 the Pi refuses a purge while unmounted ({out.get('error')})")
            finally:
                rig_yaml.write_text(plain)
                _http("POST", f"{base}/api/data/load", {"rig": "scratch"})
            out = leader_data("purge", "--leader-dir", str(pi_data), "--video-dir", str(pi_video),
                              "--folder", "M2/M2_20260901", "--verified", "data:M2/M2_20260901")
            errs = ((out.get("results") or {}).get("M2/M2_20260901") or {}).get("errors") or []
            check(out.get("ok") is False and any("not verified" in e for e in errs)
                  and (pi_data / "M2/M2_20260901").exists() and (pi_video / "M2/M2_20260901").exists(),
                  f"T5 the Pi refuses to purge a folder whose video copy was not verified ({errs})")
        finally:
            for subj in ("M4", "M5", "M8", "M9"):
                shutil.rmtree(pi_data / subj, ignore_errors=True)

        # cancel mid-copy: a big folder, cancel quickly, expect a partial and red status
        big = pi_video / "M3/M3_20260903/M3_20260903_001"
        big.mkdir(parents=True)
        (pi_data / "M3/M3_20260903/M3_20260903_001").mkdir(parents=True)
        make_h5(pi_data / "M3/M3_20260903/M3_20260903_001/M3_20260903_001.h5", True)
        with open(big / "video.mp4", "wb") as f:
            for _ in range(60):
                f.write(os.urandom(4_000_000))
        inv()
        d = _http("POST", f"{base}/api/data/sync", {"items": {"scratch": ["M3/M3_20260903"]}})
        time.sleep(0.6)
        _http("POST", f"{base}/api/data/jobs/{d['job']['id']}/cancel")
        j = wait_job(d["job"]["id"])
        st = statuses().get("M3/M3_20260903")
        check(j["status"] in ("cancelled", "failed") and st == "red", f"cancelled copy leaves the folder red ({j['status']}, {st})")

        # rsync guard: point the settings at a stand-in for Apple's openrsync; GET re-reads the file
        fake = scratch / "openrsync"
        fake.write_text("#!/bin/sh\necho 'openrsync: protocol version 29'\necho 'rsync version 2.6.9 compatible'\n")
        fake.chmod(0o755)
        cfg = yaml.safe_load(settings_file.read_text()) or {}
        cfg["rsync_path"] = str(fake)
        settings_file.write_text(yaml.safe_dump(cfg))
        s = _http("GET", f"{base}/api/data/settings")
        check(s["rsync"]["ok"] is False and s["rsync"]["error"], f"openrsync rejected: {s['rsync']['error']}")
        d = _http("POST", f"{base}/api/data/sync", {"items": {"scratch": ["M2/M2_20260901"]}})
        check(d["ok"] is False, "sync refused while rsync is unusable")
    except Exception as e:
        failures.append(f"exception: {e}")
        print("  EXC  ", e)
    finally:
        if args.keep and not failures:
            print(f"--keep: controller left running on {base}; scratch {scratch}")
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        logf.close()
    if failures:
        print(f"\nFAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        print("logs:", scratch / "controller.log")
        sys.exit(1)
    print("\nPASS")


if __name__ == "__main__":
    main()
