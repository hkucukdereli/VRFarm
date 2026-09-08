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
  suppressed power-off;  a cancelled copy leaves a resumable partial;  the rsync guard trips
  when the rsync path points at Apple's openrsync.

    conda activate vrfarm
    python tools/smoke_data.py           # ~30 s; exit code 0 on PASS
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

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


def make_h5(path: Path, consolidated: bool):
    import h5py
    import numpy as np
    with h5py.File(str(path), "w") as f:
        f.create_dataset("trial_num", data=np.arange(5))
        if consolidated:
            f.attrs["format_version"] = 2
            f.create_group("trials")


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
    # bwlimit: local rsync would otherwise finish a 240 MB copy before the cancel test can fire
    settings_file.write_text(f"ui_port: {args.port}\ndata_root: {root}\nauto_purge: false\nevent_port: 5591\n"
                             "sync: {parallel_rigs: 4, bwlimit_mbps: 200}\n")
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

        # rsync guard: point the settings at Apple's openrsync; GET re-reads the file
        settings_file.write_text(settings_file.read_text() + "rsync_path: /usr/bin/rsync\n")
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
