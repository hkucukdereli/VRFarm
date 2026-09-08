"""
controller/sync.py

The Data tab's engine. Talks to leader Pis over SSH only:

  inventory   — run shared/leader_data.py on the leader (what is there, consolidated or not,
                is the engine running, disk), then an rsync DRY RUN per tree to decide which
                date folders are green (nothing left to copy) or red (something pending).
  sync        — per date folder: consolidate what needs it, refuse cross-rig session-id
                collisions, rsync data tree + video tree into the one data root with live
                progress, verify with a second dry run, write the ledger, and (auto-purge)
                delete the folder on the Pi.
  purge       — delete green + consolidated date folders on the Pi (re-verified right before).
  poweroff    — `sudo poweroff` on every follower, then the leader, and wait for port 22 to close.

Copying uses -rlt (files, links, times), NOT -a: owner/group can never match between the Pi
user and the controller user, so an -a dry run would never come back clean.
"""
from __future__ import annotations
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from controller import settings
from controller.registry import registry, RigState
from controller.ssh import target as ssh_target, is_local, ssh_result, ssh_ok, q
from controller.jobs import Job

ROOT = settings.ROOT
FOLDER_RE = re.compile(r"^([A-Za-z0-9-]+)/\1_(\d{8})(?:/|$)")
LEDGER_NAME = ".vrfarm_sync_ledger.json"
SSH_E = "ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new"
_PROGRESS_RE = re.compile(r"^\s*([\d,]+)\s+(\d{1,3})%\s+(\S+/s)\s+(\d+:\d+:\d+)")


# ── leader access ──

def leader_cmd(rs: RigState, *args) -> tuple[str, str]:
    """(ssh target, command line) that runs shared/leader_data.py on this rig's leader. A
    loopback leader (smoke tests) runs the repo copy with this interpreter instead."""
    leader = rs.leader()
    tgt = ssh_target(leader)
    argstr = " ".join(q(a) for a in args)
    if is_local(tgt):
        cmd = f"cd {q(ROOT)} && {q(sys.executable)} -m shared.leader_data {argstr}"
    else:
        cmd = ("source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig && cd ~/rig && "
               f"python -m shared.leader_data {argstr}")
    return tgt, cmd


def run_leader_data(rs: RigState, *args, timeout=300) -> dict:
    tgt, cmd = leader_cmd(rs, *args)
    r = ssh_result(tgt, cmd, timeout=timeout)
    out = (r.stdout or "").strip()
    line = out.splitlines()[-1] if out else ""
    try:
        d = json.loads(line)
    except Exception:
        err = (r.stderr or "").strip()[-600:] or out[-600:] or f"exit {r.returncode}"
        if "No module named" in err or "leader_data" in err:
            err += " — deploy the latest code to this leader (shared/leader_data.py rides Deploy)"
        if "rsync" in err and "not found" in err:
            err += " — apt install rsync (a re-Install adds it)"
        return {"ok": False, "error": err, "rc": r.returncode}
    if r.returncode != 0 and d.get("ok") is None:
        d["ok"] = False
    return d


def data_dirs(rs: RigState) -> tuple[str, str | None]:
    d = rs.config.get("data") or {}
    leader_dir = d.get("leader_dir") or "/home/vruser/data"
    video_dir = d.get("video_dir") or None
    return leader_dir, video_dir


def remote_path(rs: RigState, path: str) -> str:
    """'user@ip:/path/' for rsync, or a plain local path for a loopback leader."""
    tgt = ssh_target(rs.leader())
    p = path.rstrip("/") + "/"
    return p if is_local(tgt) else f"{tgt}:{p}"


def rsync_bin() -> str:
    return str(settings.rsync_path())


def rsync_base(extra_ssh=True) -> list[str]:
    args = [rsync_bin()]
    if extra_ssh:
        args += ["-e", SSH_E]
    return args


# ── ledger ──

_ledger_lock = threading.Lock()


def ledger_path() -> Path:
    return settings.data_root() / LEDGER_NAME


def ledger_load() -> dict:
    p = ledger_path()
    if not p.exists():
        return {"version": 1, "folders": {}}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"version": 1, "folders": {}}


def ledger_update(fn) -> dict:
    with _ledger_lock:
        d = ledger_load()
        fn(d)
        p = ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ledger.", suffix=".json", dir=str(p.parent))
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, p)
        return d


def ledger_key(rig: str, folder: str) -> str:
    subject, date = folder.split("/")[0], folder.split("_")[-1]
    return f"{rig}|{subject}|{date}"


# ── status (dry runs) ──

def _bucket(rel: str):
    m = FOLDER_RE.match(rel)
    return f"{m.group(1)}/{m.group(1)}_{m.group(2)}" if m else None


def pending_folders(rs: RigState, tree: str, only: str | None = None) -> tuple[set, str | None]:
    """Date folders under `tree` (a path on the leader) with anything left to copy into the
    data root, from an rsync dry run. Returns (set of folder keys, error or None)."""
    root = settings.data_root()
    root.mkdir(parents=True, exist_ok=True)
    src = remote_path(rs, tree if only is None else f"{tree.rstrip('/')}/{only}")
    dst = str(root) + ("/" if only is None else f"/{only}/")
    if only is not None:
        Path(dst).mkdir(parents=True, exist_ok=True)
    args = rsync_base() + ["-rltn", "--itemize-changes", "--out-format=%i|%n", "--modify-window=1",
                           "--exclude=.rsync-partial/", "--exclude=.DS_Store", src, dst]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return set(), str(e)
    if r.returncode not in (0, 23, 24):
        return set(), (r.stderr.strip() or f"rsync exit {r.returncode}")[-400:]
    pending = set()
    for line in r.stdout.splitlines():
        if "|" not in line:
            continue
        code, rel = line.split("|", 1)
        if not (code.startswith(">f") or code.startswith("cd") or code.startswith("<f")):
            continue
        rel = rel.strip().rstrip("/")
        key = _bucket(rel if only is None else f"{only}/{rel}")
        if key and (only is None or key == only):
            # a bare `cd` of the subject or date dir itself counts too (folder missing locally)
            pending.add(key)
    return pending, None


def folder_status(rs: RigState, inv: dict) -> dict:
    """{folder_key: green|red|grey} for every folder in the inventory."""
    leader_dir, video_dir = data_dirs(rs)
    status = {f["key"]: "green" for f in inv.get("folders", [])}
    errs = []
    pend, err = pending_folders(rs, leader_dir)
    if err:
        errs.append(f"data tree: {err}")
        for k in status:
            status[k] = "grey"
    else:
        for k in pend:
            if k in status:
                status[k] = "red"
    if inv.get("video_tree") and video_dir:
        pend, err = pending_folders(rs, video_dir)
        if err:
            errs.append(f"video tree: {err}")
            for k in status:
                if status[k] == "green":
                    status[k] = "grey"
        else:
            for k in pend:
                if k in status and status[k] != "grey":
                    status[k] = "red"
    return {"status": status, "errors": errs}


def inventory(rs: RigState) -> dict:
    """Card contents for one rig: leader SSH, engine state, disk, folders with status."""
    leader = rs.leader()
    tgt = ssh_target(leader)
    card = {"name": rs.name, "leader": {"name": leader["name"], "ip": leader["ip"]},
            "phase": rs.phase, "busy": rs.busy_kind, "ssh_ok": False, "error": None,
            "hostname": None, "engine_running": None, "disk_data": None, "disk_video": None,
            "folders": [], "last_sync_t": rs.data.get("last_sync_t"), "t": time.time()}
    if not ssh_ok(tgt):
        card["error"] = f"no SSH to {tgt}"
        rs.data.update({"inventory": None, "status": {}, "engine_running": None})
        return card
    card["ssh_ok"] = True
    leader_dir, video_dir = data_dirs(rs)
    inv = run_leader_data(rs, "inventory", "--leader-dir", leader_dir,
                          *(["--video-dir", video_dir] if video_dir else []))
    if not inv.get("ok"):
        card["error"] = inv.get("error", "inventory failed")
        rs.data.update({"inventory": None, "status": {}})
        return card
    st = folder_status(rs, inv)
    ledger = ledger_load().get("folders", {})
    folders = []
    for f in inv["folders"]:
        entry = dict(f)
        entry["bytes"] = (f.get("bytes_data") or 0) + (f.get("bytes_video") or 0)
        entry["status"] = st["status"].get(f["key"], "grey")
        lk = ledger.get(ledger_key(rs.name, f["key"]))
        entry["last_synced"] = lk.get("last_synced") if lk else None
        entry["purged_at"] = lk.get("purged_at") if lk else None
        folders.append(entry)
    card.update({"hostname": inv.get("hostname"), "engine_running": inv.get("engine_running"),
                 "disk_data": inv.get("disk_data"), "disk_video": inv.get("disk_video"),
                 "folders": folders, "status_errors": st["errors"], "video_tree": inv.get("video_tree")})
    rs.data.update({"inventory": inv, "inventory_t": time.time(), "status": st["status"],
                    "engine_running": inv.get("engine_running"),
                    "disk_free_gb": (inv.get("disk_data") or {}).get("free_gb"), "card": card})
    return card


def cached_card(rs: RigState, max_age_s: float = 600) -> dict:
    c = rs.data.get("card")
    if c and (time.time() - (rs.data.get("inventory_t") or 0)) < max_age_s:
        return c
    return inventory(rs)


# ── collision guard ──

def collision(rs: RigState, folder: str) -> str | None:
    """A session id in this folder that already exists under the data root from ANOTHER rig."""
    inv = rs.data.get("inventory") or {}
    f = next((x for x in inv.get("folders", []) if x["key"] == folder), None)
    if not f:
        return None
    ledger = ledger_load().get("folders", {})
    root = settings.data_root()
    for s in f.get("sessions", []):
        local = root / folder / s["id"]
        if not local.exists():
            continue
        for k, v in ledger.items():
            if v.get("rig") != rs.name and s["id"] in (v.get("sessions") or []):
                return f"session {s['id']} already synced from rig '{v.get('rig')}'"
    return None


# ── copying ──

def _rsync_folder(job: Job, rs: RigState, tree: str, folder: str, bytes_total: int,
                  bytes_before: int, rig_total: int) -> tuple[int, str]:
    """rsync one date folder of one tree with live progress. Returns (exit code, stderr tail)."""
    root = settings.data_root()
    src = remote_path(rs, f"{tree.rstrip('/')}/{folder}")
    dst = root / folder
    dst.mkdir(parents=True, exist_ok=True)
    cfg = settings.load().get("sync") or {}
    args = rsync_base() + ["-rlt", "--partial", "--partial-dir=.rsync-partial", "--modify-window=1",
                           "--info=progress2", "--no-inc-recursive", "--stats", "--exclude=.DS_Store"]
    bw = int(cfg.get("bwlimit_mbps") or 0)
    if bw > 0:
        args.append(f"--bwlimit={bw * 125}")     # rsync takes KiB/s-ish units (1 Mbit/s ~ 125 KB/s)
    args += [src, str(dst) + "/"]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    job.procs[rs.name] = p
    err_buf = []
    stderr_t = threading.Thread(target=lambda: err_buf.extend(p.stderr.read().splitlines()[-30:]), daemon=True)
    stderr_t.start()
    buf = ""
    while True:
        ch = p.stdout.read(1)
        if not ch:
            break
        if ch in ("\r", "\n"):
            m = _PROGRESS_RE.match(buf)
            if m:
                pct = int(m.group(2))
                done_here = int(bytes_total * pct / 100)
                job.set_progress(rs.name, pct=round(100.0 * (bytes_before + done_here) / max(1, rig_total), 1),
                                 bytes_done=bytes_before + done_here, rate=m.group(3), eta_s=m.group(4),
                                 current=folder)
            buf = ""
        else:
            buf += ch
    p.wait()
    stderr_t.join(timeout=5)
    job.procs.pop(rs.name, None)
    return p.returncode, "\n".join(err_buf)[-800:]


def sync_folder(job: Job, rs: RigState, folder: str, bytes_before: int, rig_total: int) -> bool:
    """Consolidate, guard, copy both trees, verify, ledger, (auto-)purge. True on success."""
    leader_dir, video_dir = data_dirs(rs)
    inv = rs.data.get("inventory") or {}
    finfo = next((x for x in inv.get("folders", []) if x["key"] == folder), {})
    res = job.result[rs.name]
    bd, bv = int(finfo.get("bytes_data") or 0), int(finfo.get("bytes_video") or 0)

    # 1. consolidate anything not yet at format v2 (idempotent)
    unconsolidated = finfo.get("n_unconsolidated", 0)
    if unconsolidated:
        job.say(rs.name, f"{folder}: consolidating {unconsolidated} session(s)")
        c = run_leader_data(rs, "consolidate", "--leader-dir", leader_dir,
                            *(["--video-dir", video_dir] if video_dir else []), "--folder", folder, timeout=1800)
        bad = [f"{k}: {v.get('error')}" for k, v in (c.get("results") or {}).items() if not v.get("ok")]
        if not c.get("ok"):
            job.say(rs.name, f"{folder}: consolidate had errors: {c.get('error') or '; '.join(bad)} — "
                             f"copying anyway; the folder will NOT be purged", "warning")
            finfo["unconsolidated_after"] = True
        else:
            finfo["n_unconsolidated"] = 0
            # re-read sizes: sidecars are gone, video is remuxed
            inv2 = run_leader_data(rs, "inventory", "--leader-dir", leader_dir,
                                   *(["--video-dir", video_dir] if video_dir else []))
            if inv2.get("ok"):
                rs.data["inventory"] = inv2
                f2 = next((x for x in inv2.get("folders", []) if x["key"] == folder), None)
                if f2:
                    finfo.update(f2)
                    bd, bv = int(f2.get("bytes_data") or 0), int(f2.get("bytes_video") or 0)
    if job.cancel.is_set():
        return False

    # 2. cross-rig collision guard
    why = collision(rs, folder)
    if why:
        job.say(rs.name, f"{folder}: REFUSED — {why}", "error")
        res["failed"].append({"folder": folder, "error": why})
        return False

    # 3. copy: data tree, then video tree
    trees = [(leader_dir, bd)]
    if inv.get("video_tree") and video_dir:
        trees.append((video_dir, bv))
    offset = bytes_before
    for tree, nbytes in trees:
        if job.cancel.is_set():
            return False
        job.say(rs.name, f"{folder}: copying from {tree}")
        rc, err = _rsync_folder(job, rs, tree, folder, nbytes, offset, rig_total)
        offset += nbytes
        if rc != 0:
            msg = f"{folder}: rsync exit {rc} on {tree}: {err.strip().splitlines()[-1] if err.strip() else ''}"
            job.say(rs.name, msg, "error")
            res["failed"].append({"folder": folder, "rc": rc, "error": err[-300:]})
            return False
    job.set_progress(rs.name, bytes_done=bytes_before + bd + bv,
                     pct=round(100.0 * (bytes_before + bd + bv) / max(1, rig_total), 1))

    # 4. verify: a clean dry run of the folder in both trees
    for tree, _n in trees:
        pend, err = pending_folders(rs, tree, only=folder)
        if err or pend:
            job.say(rs.name, f"{folder}: verification failed on {tree}: {err or 'still pending'}", "error")
            res["failed"].append({"folder": folder, "error": f"verify: {err or 'pending after copy'}"})
            return False

    # 5. ledger
    sessions = [s["id"] for s in finfo.get("sessions", [])]
    now = time.time()

    def _upd(d):
        e = d["folders"].setdefault(ledger_key(rs.name, folder), {
            "rig": rs.name, "subject": folder.split("/")[0], "date": folder.split("_")[-1],
            "first_synced": now, "purged_at": None})
        e.update({"sessions": sorted(set((e.get("sessions") or []) + sessions)), "bytes": bd + bv,
                  "last_synced": now, "last_verified": now,
                  "trees": {"data": leader_dir, "video": video_dir}})
    ledger_update(_upd)
    rs.data["status"][folder] = "green"
    rs.data["last_sync_t"] = now
    res["synced"].append(folder)
    job.say(rs.name, f"{folder}: synced and verified ({(bd + bv) / 1e6:.1f} MB)")

    # 6. auto-purge
    if job.params.get("auto_purge"):
        if finfo.get("unconsolidated_after") or finfo.get("n_unconsolidated"):
            job.say(rs.name, f"{folder}: auto-purge skipped (unconsolidated session)", "warning")
        else:
            purge_folders(job, rs, [folder], recheck=False)
    return True


def purge_folders(job: Job, rs: RigState, folders: list, recheck: bool = True) -> list:
    """Delete date folders on the Pi (both trees). With recheck, every folder is dry-run
    verified clean right before deletion; a checksum pass when the setting asks for it."""
    leader_dir, video_dir = data_dirs(rs)
    inv = rs.data.get("inventory") or {}
    res = job.result[rs.name]
    cfg = settings.load().get("sync") or {}
    todo = []
    for folder in folders:
        if job.cancel.is_set():
            break
        if recheck:
            ok = True
            for tree in [leader_dir] + ([video_dir] if (inv.get("video_tree") and video_dir) else []):
                pend, err = pending_folders(rs, tree, only=folder)
                if err or pend:
                    job.say(rs.name, f"{folder}: NOT purged — {err or 'unsynced changes on the Pi'}", "warning")
                    ok = False
                    break
                if cfg.get("verify_checksum_before_purge"):
                    src = remote_path(rs, f"{tree.rstrip('/')}/{folder}")
                    dst = str(settings.data_root() / folder) + "/"
                    r = subprocess.run(rsync_base() + ["-rltnc", "--itemize-changes", "--out-format=%i|%n",
                                                       "--exclude=.rsync-partial/", src, dst],
                                       capture_output=True, text=True, timeout=7200)
                    if any(l.split("|")[0].startswith(">f") for l in r.stdout.splitlines() if "|" in l):
                        job.say(rs.name, f"{folder}: NOT purged — checksum mismatch", "error")
                        ok = False
                        break
            if not ok:
                continue
        todo.append(folder)
    if not todo:
        return []
    out = run_leader_data(rs, "purge", "--leader-dir", leader_dir,
                          *(["--video-dir", video_dir] if video_dir else []),
                          *[a for f in todo for a in ("--folder", f)], timeout=900)
    if not out.get("ok") and not out.get("results"):
        job.say(rs.name, f"purge failed: {out.get('error')}", "error")
        return []
    purged = []
    now = time.time()
    for folder, r in (out.get("results") or {}).items():
        if r.get("ok"):
            purged.append(folder)
            job.say(rs.name, f"{folder}: purged on the Pi ({r.get('bytes_freed', 0) / 1e6:.1f} MB freed)")
            ledger_update(lambda d, k=ledger_key(rs.name, folder): d["folders"].setdefault(k, {"rig": rs.name}).update({"purged_at": now}))
            rs.data.get("status", {}).pop(folder, None)
        else:
            job.say(rs.name, f"{folder}: purge error: {r.get('errors')}", "error")
    res["purged"].extend(purged)
    rs.data["inventory_t"] = 0        # force a fresh inventory next time
    return purged


# ── power off ──

def _port_open(ip: str, port: int = 22, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def poweroff_rig(job: Job, rs: RigState) -> str:
    """Followers first, then the leader. Returns off | sent | skipped | failed."""
    pis = rs.followers() + [rs.leader()]
    tgt_leader = ssh_target(rs.leader())
    if is_local(tgt_leader):
        job.say(rs.name, "(local leader) poweroff suppressed — this is a test rig", "warning")
        rs.phase = "offline"
        return "skipped"
    sent = []
    for pi in pis:
        tgt = ssh_target(pi)
        r = ssh_result(tgt, "sudo poweroff", timeout=15)
        err = (r.stderr or "").lower()
        if r.returncode == 0 or (r.returncode == 255 and "sudo" not in err and "password" not in err):
            sent.append(pi)
            job.say(rs.name, f"poweroff sent to {pi['name']} ({pi['ip']})")
        else:
            job.say(rs.name, f"poweroff FAILED on {pi['name']}: {(r.stderr or '').strip()[-200:]}", "error")
            return "failed"
    deadline = time.time() + 90
    alive = {pi["ip"] for pi in sent}
    while alive and time.time() < deadline:
        time.sleep(3)
        alive = {ip for ip in alive if _port_open(ip)}
    rs.phase = "offline"
    for pi in pis:
        rs.pi_status[pi["name"]] = {"ok": False, "ssh": False, "api": False, "t": time.time(), "off": True}
    if alive:
        job.say(rs.name, f"poweroff sent but {', '.join(alive)} still answer on port 22 after 90 s", "warning")
        return "sent"
    job.say(rs.name, "all Pis are OFF — safe to cut power")
    return "off"


# ── job bodies ──

def _rig_guard(job: Job, rs: RigState) -> bool:
    if rs.phase == "running":
        job.say(rs.name, "skipped: a session is running", "warning")
        return False
    if rs.data.get("engine_running"):
        job.say(rs.name, "skipped: the experiment engine is running on the leader", "warning")
        return False
    if not rs.try_busy(job.kind):
        job.say(rs.name, f"skipped: rig is busy ({rs.busy_kind})", "warning")
        return False
    return True


def _run_rigs(job: Job, per_rig):
    cfg = settings.load().get("sync") or {}
    sem = threading.Semaphore(max(1, int(cfg.get("parallel_rigs") or 4)))
    threads = []

    def wrap(name):
        with sem:
            rs = registry.rigs.get(name)
            if rs is None:
                job.say(name, "rig not loaded", "error")
                job.set_progress(name, state="failed")
                return
            try:
                per_rig(rs)
            except Exception as e:
                job.say(name, f"error: {e}", "error")
                job.set_progress(name, state="failed")

    for name in job.rigs:
        t = threading.Thread(target=wrap, args=(name,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


def job_sync(job: Job):
    """params: {items: {rig: [folder,...]}, auto_purge, poweroff}"""
    items = job.params.get("items") or {}

    def per_rig(rs: RigState):
        folders = list(items.get(rs.name) or [])
        if not _rig_guard(job, rs):
            job.set_progress(rs.name, state="skipped")
            return
        try:
            card = cached_card(rs)
            if not card.get("ssh_ok") or card.get("error"):
                job.say(rs.name, f"skipped: {card.get('error')}", "error")
                job.set_progress(rs.name, state="failed")
                return
            if job.params.get("all_unsynced"):
                folders = [f["key"] for f in card["folders"] if f["status"] != "green"]
            sizes = {f["key"]: f.get("bytes", 0) for f in card["folders"]}
            total = sum(sizes.get(f, 0) for f in folders)
            job.set_progress(rs.name, state="running", bytes_total=total, bytes_done=0, pct=0)
            if not folders:
                job.say(rs.name, "nothing to sync")
            done = 0
            ok_all = True
            for folder in folders:
                if job.cancel.is_set():
                    ok_all = False
                    break
                ok = sync_folder(job, rs, folder, done, total)
                ok_all = ok_all and ok
                done += sizes.get(folder, 0)
            # shepherd logs: best effort, tiny
            if not job.cancel.is_set():
                _mirror_shepherd_logs(job, rs)
            job.set_progress(rs.name, state=("cancelled" if job.cancel.is_set() else "done" if ok_all else "failed"),
                             pct=(100 if ok_all and not job.cancel.is_set() else job.progress[rs.name].get("pct", 0)))
            if job.params.get("poweroff"):
                if ok_all and not job.cancel.is_set():
                    job.result[rs.name]["poweroff"] = poweroff_rig(job, rs)
                else:
                    job.result[rs.name]["poweroff"] = "skipped"
                    job.say(rs.name, "NOT powered off: a folder failed to sync (fix it and retry)", "warning")
        finally:
            rs.release_busy()
            rs.data["inventory_t"] = 0

    _run_rigs(job, per_rig)


def job_purge(job: Job):
    """params: {items: {rig: [folder,...]}}"""
    items = job.params.get("items") or {}

    def per_rig(rs: RigState):
        if not _rig_guard(job, rs):
            job.set_progress(rs.name, state="skipped")
            return
        try:
            job.set_progress(rs.name, state="running")
            purged = purge_folders(job, rs, list(items.get(rs.name) or []), recheck=True)
            job.set_progress(rs.name, state="done", pct=100)
            job.say(rs.name, f"purged {len(purged)} folder(s)")
        finally:
            rs.release_busy()

    _run_rigs(job, per_rig)


def _mirror_shepherd_logs(job: Job, rs: RigState):
    tgt = ssh_target(rs.leader())
    src = ("~/shepherd_logs/" if not is_local(tgt) else str(Path.home() / "shepherd_logs") + "/")
    if is_local(tgt) and not Path(src).is_dir():
        return
    dst = settings.data_root() / "shepherd_logs" / rs.name
    dst.mkdir(parents=True, exist_ok=True)
    args = rsync_base() + ["-rlt", "--exclude=.DS_Store", (src if is_local(tgt) else f"{tgt}:{src}"), str(dst) + "/"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        days = int((settings.load().get("sync") or {}).get("shepherd_logs_keep_days") or 7)
        run_leader_data(rs, "prune-logs", "--days", str(days), timeout=60)
    elif "No such file" not in (r.stderr or ""):
        job.say(rs.name, f"shepherd logs not mirrored: {(r.stderr or '').strip()[-160:]}", "warning")
