"""
controller/sync.py

The Data tab's engine. Talks to leader Pis over SSH only:

  inventory   — run shared/leader_data.py on the leader (which date folders sit in which tree,
                consolidated or not, recorded video that is missing, is the engine running, disk),
                then an rsync DRY RUN per tree to give each folder a status: green (nothing left
                to copy AND the copy's content was checked against the Pi's files as they are
                now), red (something pending, or the content not checked), missing (a session's
                recorded video is not on the Pi) or grey (the check itself can't be trusted).
  sync        — per date folder: consolidate what needs it, refuse cross-rig session-id
                collisions, rsync the trees that hold the folder into the one data root with live
                progress, verify against a fresh inventory and a second dry run, write the ledger,
                check the content (below), and (auto-purge) purge it.
  purge       — delete a folder on the Pi tree by tree, each copy content-checked and re-verified
                clean moments before; the Pi refuses any tree the controller did not name as verified.
  poweroff    — `sudo poweroff` on every follower, then the leader, and wait for port 22 to close.

Copying uses -rlt (files, links, times), NOT -a: owner/group can never match between the Pi
user and the controller user, so an -a dry run would never come back clean.

No nonzero rsync exit counts as clean: exit 23 is also what an unreadable subdirectory gives, and
its files would otherwise look copied. Which trees a folder is in (a session recorded with the
camera unchecked has no video folder) comes from the inventory, never from rsync's error text.

Content check (leader_data protocol 3, 2026-11). rsync calls a file copied when size and mtime
agree, and its sender reads through the leader's page cache — which on the Pi 5 kernel 6.18.34
handed out wrong bytes once memory had filled (morbier, pecorino; found 2026-11-12). A copy made
then holds wrong bytes and still passes every dry run, `-c` included. So after the copy the leader
hashes each file from a direct read of its disk (`leader_data hash`, O_DIRECT), the controller
hashes its own copy the same way, and a copy that differs is fetched again through a direct read
(`leader_data read`) — the copy it replaces is kept under <data root>/.vrfarm_replaced/. The
ledger entry then carries `verified` with the leader's fingerprint of the folder (names, sizes,
mtimes); a folder is green only while the inventory still reports that fingerprint, and only a
green folder can be purged.
"""
from __future__ import annotations
import collections
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from controller import settings
from controller.registry import registry, RigState
from controller.ssh import target as ssh_target, is_local, ssh_result, ssh_ok, ssh_popen, q
from controller.jobs import Job
from shared import leader_data

ROOT = settings.ROOT
FOLDER_RE = re.compile(r"^([A-Za-z0-9-]+)/\1_(\d{8})(?:/|$)")
FOLDER_KEY_RE = re.compile(r"^([A-Za-z0-9-]+)/\1_(\d{8})$")      # a whole folder key, nothing after it
LEDGER_NAME = ".vrfarm_sync_ledger.json"
LEADER_DATA_PROTO = 3       # the shared/leader_data.py protocol this controller needs on a leader
DEPLOY_NEEDED = "leader_data.py on the leader is out of date — Deploy this rig"
SSH_E = "ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new"
_PROGRESS_RE = re.compile(r"^\s*([\d,]+)\s+(\d{1,3})%\s+(\S+/s)\s+(\d+:\d+:\d+)")
FILE_LEVEL_RC = (23, 24)    # rsync: some files failed / vanished — per file, not the whole link
# Temp files of a remux / .h5 fold still being written on the Pi are never archive content: not
# copied, and never what makes a folder red.
TEMP_EXCLUDES = ["--exclude=*.part", "--exclude=*.consolidating"]
SEVERITY = {"green": 0, "red": 1, "missing": 2, "grey": 3}
_HASH_PROGRESS_RE = re.compile(r"^P (\d+) (\d+)\s*$")
CONTENT_STALL_S = 300       # a `hash` on the leader that stays silent this long is given up on
REPLACED_DIR = ".vrfarm_replaced"       # under the data root: the copies a content check replaced
CONTENT_UNCHECKED = "copied, but its content has not been checked against the Pi yet — Sync Now checks it"
CONTENT_STALE = "its files on the Pi changed since the content check — Sync Now checks them again"
CONTENT_NO_FP = "its files on the Pi could not be listed for the content check"


class _Cancelled(Exception):
    """Raised inside a read loop when the job is cancelled."""


# ── leader access ──

def leader_cmd(rs: RigState, *args) -> tuple[str, str]:
    """(ssh target, command line) that runs shared/leader_data.py on this rig's leader. A
    loopback leader (smoke tests) runs the repo copy with this interpreter instead."""
    leader = rs.leader()
    tgt = ssh_target(leader)
    argstr = " ".join(q(a) for a in args)
    if is_local(tgt):
        cmd = f"cd {q(ROOT)} && exec {q(sys.executable)} -m shared.leader_data {argstr}"
    else:
        cmd = ("source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig && cd ~/rig && "
               f"python -m shared.leader_data {argstr}")
    return tgt, cmd


def run_leader_data(rs: RigState, *args, timeout=300) -> dict:
    tgt, cmd = leader_cmd(rs, *args)
    try:
        r = ssh_result(tgt, cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"leader_data {args[0] if args else ''} timed out after {timeout} s"}
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


def leader_data_args(rs: RigState) -> list[str]:
    """The tree arguments every inventory / consolidate / purge call carries. The rig YAML's
    optional `data.video_mount` names the mountpoint an external video drive must be mounted on;
    without it video_dir is a plain directory."""
    leader_dir, video_dir = data_dirs(rs)
    mount = (rs.config.get("data") or {}).get("video_mount") or None
    return (["--leader-dir", leader_dir] + (["--video-dir", video_dir] if video_dir else [])
            + (["--video-mount", mount] if mount else []))


def proto_outdated(inv: dict) -> bool:
    """Is the leader's leader_data.py older than this controller needs (protocol 3: `hash`, `read`
    and the inventory's `fp`)? Replies from before protocol 2 carry no `proto` at all."""
    try:
        return int(inv.get("proto") or 1) < LEADER_DATA_PROTO
    except (TypeError, ValueError):
        return True


def scoped_inventory(rs: RigState, folders) -> dict:
    """A fresh inventory of just these date folders."""
    return run_leader_data(rs, "inventory", *leader_data_args(rs),
                           *[a for f in folders for a in ("--folder", f)])


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


def _dry_run(rs: RigState, tree: str, only: str | None = None) -> tuple[int | None, set, str | None]:
    """rsync dry run of a tree on the leader (or of one date folder in it) against the data root.
    Returns (exit code, or None when rsync could not run; pending folder keys; error or None).
    It compares names, sizes and mtimes only — content is content_check's job.

    One folder is picked with filter rules on the tree root instead of pointing rsync at the
    folder: nothing has to be created under the data root first, and an unreadable sibling folder
    can't fail the run."""
    if only is not None and not FOLDER_KEY_RE.match(only):
        return None, set(), f"not a date folder: {only!r}"
    root = settings.data_root()
    root.mkdir(parents=True, exist_ok=True)
    args = rsync_base() + ["-rltn", "--itemize-changes", "--out-format=%i|%n",
                           "--modify-window=1", "--exclude=.rsync-partial/", "--exclude=.DS_Store"] + TEMP_EXCLUDES
    if only is None:
        args.append("--exclude=/lost+found/")
    else:
        args += [f"--include=/{only.split('/')[0]}/", f"--include=/{only}/***", "--exclude=*"]
    args += [remote_path(rs, tree), str(root) + "/"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return None, set(), str(e)
    if r.returncode != 0:
        return r.returncode, set(), (r.stderr.strip() or f"rsync exit {r.returncode}")[-400:]
    pending = set()
    for line in r.stdout.splitlines():
        if "|" not in line:
            continue
        code, rel = line.split("|", 1)
        if not (code.startswith(">f") or code.startswith("cd") or code.startswith("<f")):
            continue
        key = _bucket(rel.strip().rstrip("/"))
        if key and (only is None or key == only):
            # a bare `cd` of the date dir itself counts too (folder missing locally)
            pending.add(key)
    return 0, pending, None


def pending_folders(rs: RigState, tree: str, only: str | None = None) -> tuple[set, str | None]:
    """Date folders under `tree` (a path on the leader) with anything left to copy into the
    data root, from a strict rsync dry run. Returns (set of folder keys, error or None)."""
    _rc, pending, err = _dry_run(rs, tree, only)
    return pending, err


def tree_status(rs: RigState, tree: str, keys: list) -> tuple[dict, str | None]:
    """{key: None (clean) | "pending" | "error: <why>"} for date folders of one tree, plus the
    tree's error when its bulk dry run failed. A per-file failure (exit 23/24 — say, one unreadable
    subdirectory) is localised: each folder then gets its own strict dry run, so only the folders
    that really fail go grey. Any other failure (SSH, rsync itself) greys the whole tree."""
    rc, pend, err = _dry_run(rs, tree)
    if err is None:
        return {k: ("pending" if k in pend else None) for k in keys}, None
    if rc not in FILE_LEVEL_RC:
        return {k: f"error: {err}" for k in keys}, err

    def one(k):
        _rc, p, e = _dry_run(rs, tree, only=k)
        return k, (f"error: {e}" if e else ("pending" if k in p else None))

    with ThreadPoolExecutor(max_workers=4) as ex:
        return dict(ex.map(one, keys)), err


def folder_status(rs: RigState, inv: dict) -> dict:
    """Status of every folder in the inventory, the worst finding winning (grey > missing > red >
    green): {status: {key: ...}, reasons: {key: why or None}, errors: [...], proto_outdated}.

      green    every tree that holds the folder has nothing left to copy, the content of the copy
               was checked against the Pi's files as they are now (content_state), and no session
               that recorded camera_saved: true lacks its video. A folder with no video copy is
               green when its sessions were not saving video (or predate the flag).
      red      something is still pending, or the copy is complete by size and time but its
               content is not checked (those keys are also listed in `unchecked`)
      missing  a session's recorded video is not on the Pi
      grey     can't be trusted: leader_data.py predates protocol 3, a tree is unavailable, the
               folder's dry run failed, or its files on the Pi could not be listed
    """
    folders = inv.get("folders", [])
    status = {f["key"]: "green" for f in folders}
    reasons = {k: None for k in status}
    errors = []
    unchecked = []

    def mark(keys, st, why):
        for k in keys:
            if k in status and SEVERITY[st] > SEVERITY[status[k]]:
                status[k], reasons[k] = st, why

    def result(outdated=False):
        return {"status": status, "reasons": reasons, "errors": errors, "proto_outdated": outdated,
                "unchecked": unchecked}

    if proto_outdated(inv):
        errors.append(DEPLOY_NEEDED)
        mark(list(status), "grey", DEPLOY_NEEDED)
        return result(outdated=True)
    droot, vroot = inv.get("data_root") or {}, inv.get("video_root") or {}
    if not droot.get("available"):
        errors.append(f"data tree unavailable: {droot.get('reason')}")
    if vroot.get("separate") and not vroot.get("available"):
        errors.append(f"video tree unavailable: {vroot.get('reason')}")
    if errors:
        mark(list(status), "grey", "; ".join(errors))
        return result()
    leader_dir, video_dir = data_dirs(rs)
    trees = [("data", leader_dir, [f["key"] for f in folders if f.get("in_data")])]
    if vroot.get("separate") and video_dir:
        trees.append(("video", video_dir, [f["key"] for f in folders if f.get("in_video")]))
    for name, tree, keys in trees:
        if not keys:
            continue
        res, err = tree_status(rs, tree, keys)
        if err:
            errors.append(f"{name} tree: {err}")
        for k, v in res.items():
            if v == "pending":
                mark([k], "red", f"not fully copied yet ({name} tree)")
            elif v:
                mark([k], "grey", f"{name} tree: {v[len('error: '):]}")
    ledger = ledger_load().get("folders", {})
    for f in folders:
        why = content_state(ledger.get(ledger_key(rs.name, f["key"])), f)
        if why == CONTENT_NO_FP:
            mark([f["key"]], "grey", why)
        elif why:
            if status[f["key"]] == "green":
                unchecked.append(f["key"])
            mark([f["key"]], "red", why)
    for f in folders:
        if f.get("missing_video"):
            mark([f["key"]], "missing", "video recorded but not on the Pi: " + ", ".join(f["missing_video"]))
    return result()


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
    inv = run_leader_data(rs, "inventory", *leader_data_args(rs))
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
        entry["reason"] = st["reasons"].get(f["key"])
        lk = ledger.get(ledger_key(rs.name, f["key"]))
        entry["last_synced"] = lk.get("last_synced") if lk else None
        entry["purged_at"] = lk.get("purged_at") if lk else None
        entry["content_unchecked"] = f["key"] in st["unchecked"]
        entry["content_checked"] = (((lk or {}).get("verified") or {}).get("t")
                                    if content_state(lk, f) is None else None)
        folders.append(entry)
    card.update({"hostname": inv.get("hostname"), "engine_running": inv.get("engine_running"),
                 "disk_data": inv.get("disk_data"), "disk_video": inv.get("disk_video"),
                 "folders": folders, "status_errors": st["errors"], "video_tree": inv.get("video_tree"),
                 "trees": {"data": inv.get("data_root"), "video": inv.get("video_root")},
                 "proto_outdated": st["proto_outdated"]})
    rs.data.update({"inventory": inv, "inventory_t": time.time(), "status": st["status"],
                    "engine_running": inv.get("engine_running"), "proto_outdated": st["proto_outdated"],
                    "disk_free_gb": (inv.get("disk_data") or {}).get("free_gb"), "card": card})
    return card


def cached_card(rs: RigState, max_age_s: float = 600) -> dict:
    c = rs.data.get("card")
    if c and (time.time() - (rs.data.get("inventory_t") or 0)) < max_age_s:
        return c
    return inventory(rs)


# ── collision guard ──

def collision(rs: RigState, folder: str, finfo: dict | None = None) -> str | None:
    """A session id in this folder that already exists under the data root from ANOTHER rig.
    `finfo` is the folder's inventory entry; without it the rig's cached inventory is used."""
    f = finfo
    if f is None:
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


# ── content check ──

def content_state(entry: dict | None, f: dict) -> str | None:
    """Why a folder's copy can't be called content-checked, or None when it can: the ledger entry
    holds a passed check (`verified`) whose fingerprint of every tree the folder is in is the one
    the inventory reports now — the Pi's files are still the ones that were hashed against the copy.
    `f` is the folder's inventory entry."""
    fp = f.get("fp")
    if not isinstance(fp, dict) or not fp or any(v is None for v in fp.values()):
        return CONTENT_NO_FP
    v = (entry or {}).get("verified")
    vfp = v.get("fp") if isinstance(v, dict) else None
    if not isinstance(vfp, dict):
        return CONTENT_UNCHECKED
    if any(vfp.get(t) != h for t, h in fp.items()):
        return CONTENT_STALE
    return None


def _leader_hash(job: Job, rs: RigState, folder: str) -> dict:
    """Run `leader_data hash` for one date folder: its reply, or {"ok": False, "error": ...}. The
    leader reads every file off its disk, so this takes as long as the folder is big. Its
    `P <done> <total>` lines on stderr drive the progress text and a stall timer, and the process
    is registered with the job, so Cancel ends it."""
    tgt, cmd = leader_cmd(rs, "hash", *leader_data_args(rs), "--folder", folder)
    try:
        p = ssh_popen(tgt, cmd)
    except OSError as e:
        return {"ok": False, "error": f"could not start it: {e}"}
    job.procs[rs.name] = p
    out, err = [], collections.deque(maxlen=20)
    last = [time.monotonic()]

    def read_err():
        for line in p.stderr:
            last[0] = time.monotonic()
            m = _HASH_PROGRESS_RE.match(line)
            if m:
                pct = min(100, 100 * int(m.group(1)) // max(1, int(m.group(2))))
                job.set_progress(rs.name, current=f"{folder} — checking content on the Pi ({pct} %)",
                                 rate=None, eta_s=None)
            elif line.strip():
                err.append(line.strip())

    threads = [threading.Thread(target=read_err, daemon=True),
               threading.Thread(target=lambda: out.append(p.stdout.read()), daemon=True)]
    for t in threads:
        t.start()
    stalled = False
    try:
        while p.poll() is None:
            stalled = time.monotonic() - last[0] > CONTENT_STALL_S
            if stalled or job.cancel.is_set():
                p.kill()
                break
            time.sleep(0.2)
        p.wait()
        for t in threads:
            t.join(timeout=10)
    finally:
        job.procs.pop(rs.name, None)
    if job.cancel.is_set():
        return {"ok": False, "error": "cancelled"}
    if stalled:
        return {"ok": False, "error": f"no sign of life from the leader for {CONTENT_STALL_S} s"}
    text = (out[0] if out else "").strip()
    try:
        d = json.loads(text.splitlines()[-1])
    except Exception:
        return {"ok": False, "error": " | ".join(err)[-400:] or text[-400:] or f"exit {p.returncode}"}
    return d if isinstance(d, dict) else {"ok": False, "error": "unexpected reply"}


def _copy_differs(path: Path, size: int, digest: str, tick) -> tuple[str | None, bool]:
    """(how the copy at `path` differs from a file of this size and SHA-256 — None when it does
    not; whether it was read direct). The copy is read the way the leader reads its own files
    (leader_data.file_digest: O_DIRECT where the filesystem allows), so the hash is of what is on
    this disk, not of what the page cache kept from the copy. An unreadable copy raises OSError."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "missing under the data root", True
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file under the data root", True
    if st.st_size != size:
        return f"{st.st_size} bytes under the data root, {size} on the Pi", True
    got, n, direct = leader_data.file_digest(path, tick=tick)
    if n != size or got != digest:
        return "its content differs from the Pi's", direct
    return None, direct


def _fetch_file(job: Job, rs: RigState, tree: str, folder: str, rel: str, dest: Path,
                size: int, mtime_ns: int, digest: str) -> str | None:
    """Replace one file under the data root with a fresh copy that the leader reads from its disk
    with O_DIRECT (`leader_data read`). The stream is hashed as it arrives and is put in place only
    when it has the size and the SHA-256 the leader reported from its first direct read — two direct
    reads that agree. It gets the Pi's mtime (so rsync still sees it as copied) and the copy it
    replaces is kept under <data root>/.vrfarm_replaced/<folder>/. Returns None, or why nothing was
    replaced ("cancelled" when the job was)."""
    tgt, cmd = leader_cmd(rs, "read", *leader_data_args(rs), "--tree", tree, "--folder", folder, "--file", rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fetch.", suffix=".part", dir=str(dest.parent))
    p = None
    try:
        p = ssh_popen(tgt, cmd, text=False)
        job.procs[rs.name] = p
        err = []
        err_t = threading.Thread(target=lambda: err.extend(p.stderr.read().decode(errors="replace").splitlines()[-20:]),
                                 daemon=True)
        err_t.start()
        h, n = hashlib.new(leader_data.HASH_ALGO), 0
        with os.fdopen(fd, "wb") as f:
            fd = None
            while True:
                chunk = p.stdout.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
                n += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        rc = p.wait()
        err_t.join(timeout=5)
        if job.cancel.is_set():
            return "cancelled"
        if rc != 0:
            return f"the leader could not send it (exit {rc}): {' | '.join(err)[-300:]}"
        if n != size or h.hexdigest() != digest:
            return "two direct reads of it on the Pi do not agree — nothing replaced"
        try:
            old = os.lstat(dest)
        except FileNotFoundError:
            old = None
        os.chmod(tmp, stat.S_IMODE(old.st_mode) if (old and stat.S_ISREG(old.st_mode)) else 0o644)
        os.utime(tmp, ns=(mtime_ns, mtime_ns))
        if old is not None:
            kept = settings.data_root() / REPLACED_DIR / folder / f"{rel}.{time.strftime('%Y%m%d-%H%M%S')}"
            kept.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(dest, kept)
            except OSError:
                os.replace(dest, kept)
        os.replace(tmp, dest)
        tmp = None
        return None
    except OSError as e:
        return f"could not replace it: {e}"
    finally:
        job.procs.pop(rs.name, None)
        if fd is not None:
            os.close(fd)
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)
        if p is not None and p.poll() is None:
            p.kill()
            p.wait()


def content_check(job: Job, rs: RigState, folder: str, finfo: dict,
                  repair: bool = True) -> tuple[dict | None, str | None, bool]:
    """Compare the content of every file of a date folder on the leader with its copy under the
    data root. Returns (record for the ledger, None, False) when all of it matches, else
    (None, why, differs) — `differs` is True only when a copy really holds other bytes, False when
    the check could not be made (leader unreachable, cancelled, the folder changed meanwhile).

    This is the one check of the bytes themselves; everything rsync says rests on size and mtime
    and on reads through the leader's page cache (module docstring). The leader hashes each file
    from a direct read of its disk, the controller hashes its copy from a direct read of its own.
    With repair=True a copy that differs is fetched again (_fetch_file) and re-read from disk; with
    repair=False (the look before a purge) the first difference ends the check.

    `finfo` is the folder's inventory entry. Its `fp` must be the fingerprint of what the leader
    then hashes, so a folder that changes between the inventory and the hash is never called
    checked — and that same `fp` is what the record carries for content_state."""
    t0 = time.time()
    fp = finfo.get("fp")
    if not isinstance(fp, dict) or not fp or any(v is None for v in fp.values()):
        return None, CONTENT_NO_FP, False
    reply = _leader_hash(job, rs, folder)
    if job.cancel.is_set():
        return None, "cancelled", False
    fr = (reply.get("folders") or {}).get(folder)
    if not isinstance(fr, dict):
        return None, f"the leader could not hash it: {reply.get('error') or 'no reply for this folder'}", False
    if not fr.get("ok"):
        return None, "the leader could not hash it: " + ("; ".join(fr.get("errors") or [])[-300:] or "unknown error"), False
    if reply.get("algo") != leader_data.HASH_ALGO:
        return None, f"the leader hashes with {reply.get('algo')}, this controller with {leader_data.HASH_ALGO} — Deploy this rig", False
    trees = fr.get("trees") or {}
    if {t: v.get("fp") for t, v in trees.items()} != fp:
        return None, "its files on the Pi changed during the content check — sync it again", False

    local = settings.data_root() / folder
    total = sum(int(v.get("bytes") or 0) for v in trees.values())
    done, last = 0, [0.0]

    def tick(n):
        nonlocal done
        if job.cancel.is_set():
            raise _Cancelled()
        done += n
        now = time.monotonic()
        if now - last[0] >= 0.5:
            last[0] = now
            job.set_progress(rs.name, current=f"{folder} — checking content here ({min(100, 100 * done // max(1, total))} %)",
                             rate=None, eta_s=None)

    n_files, n_bytes, repaired, direct_here, seen = 0, 0, [], True, set()
    try:
        for tree in ("data", "video"):
            t = trees.get(tree)
            if t is None:
                continue
            for rel, target in t.get("links") or []:
                try:
                    same = os.readlink(local / rel) == target
                except OSError:
                    same = False
                if not same:
                    return None, f"{rel}: the link under the data root is not the Pi's", True
            for rel, size, mtime_ns, digest in t.get("files") or []:
                if rel in seen:
                    return None, f"{rel} is in both the data and the video tree on the Pi", False
                seen.add(rel)
                why, direct = _copy_differs(local / rel, size, digest, tick)
                if why and not repair:
                    return None, f"{rel}: {why}", True
                if why:
                    job.say(rs.name, f"{folder}: {rel}: {why} — fetching it again with a direct read", "warning")
                    err = _fetch_file(job, rs, tree, folder, rel, local / rel, size, mtime_ns, digest)
                    if job.cancel.is_set():
                        return None, "cancelled", False
                    if err:
                        return None, f"{rel}: {why}; {err}", True
                    why, direct = _copy_differs(local / rel, size, digest, tick)
                    if why:
                        return None, f"{rel}: still wrong after fetching it again ({why})", True
                    repaired.append(rel)
                    job.say(rs.name, f"{folder}: {rel}: replaced with the Pi's bytes; the copy it replaced is kept "
                                     f"under {REPLACED_DIR}/{folder}/", "warning")
                direct_here = direct_here and direct
                n_files += 1
                n_bytes += size
    except _Cancelled:
        return None, "cancelled", False
    except OSError as e:
        return None, f"could not read the copy under the data root: {e}", False
    finally:
        job.set_progress(rs.name, current=folder)
    rec = {"t": time.time(), "algo": leader_data.HASH_ALGO, "fp": dict(fp), "files": n_files, "bytes": n_bytes,
           "direct": {"pi": all(bool(v.get("direct")) for v in trees.values()), "controller": direct_here},
           "seconds": round(time.time() - t0, 1)}
    if repaired:
        rec["repaired"] = repaired
    return rec, None, False


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
                           "--info=progress2", "--no-inc-recursive", "--stats", "--exclude=.DS_Store"] + TEMP_EXCLUDES
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
    """Consolidate, guard, copy the trees that hold the folder, verify, ledger, content check,
    (auto-)purge. Decisions come from fresh inventories of this one folder, not from the card.
    True on success."""
    leader_dir, video_dir = data_dirs(rs)
    res = job.result[rs.name]

    def fail(msg):
        job.say(rs.name, f"{folder}: {msg}", "error")
        res["failed"].append({"folder": folder, "error": msg})
        return False

    def look():
        """(inventory, this folder's entry, why the sync can't go on — or None)"""
        inv = scoped_inventory(rs, [folder])
        droot = inv.get("data_root") or {}
        if not inv.get("ok"):
            return inv, None, f"inventory failed: {inv.get('error')}"
        if proto_outdated(inv):
            return inv, None, DEPLOY_NEEDED
        if not droot.get("available"):
            return inv, None, f"data tree unavailable: {droot.get('reason')}"
        f = next((x for x in inv.get("folders", []) if x["key"] == folder), None)
        return inv, f, (None if f else "no longer on the Pi")

    # 1. what is on the Pi right now
    pre, finfo, why = look()
    if why:
        return fail(why)
    vroot = pre.get("video_root") or {}

    # 2. consolidate anything not yet at format v2 and remux raw video (both idempotent, on the Pi) —
    #    but not while the video tree can't be trusted: the .h5 would be written without /camera and
    #    never folded again. The remux lives here since 2026-11 (it used to run at the engine's exit,
    #    where the 10 s stop escalation cut it short): a fresh session holds a raw video.h264 until
    #    its first sync, and a leader deployed before then reports no n_raw_video at all.
    n = finfo.get("n_unconsolidated", 0)
    nv = finfo.get("n_raw_video", 0)
    if "n_raw_video" not in finfo and any("video.mp4.part" in (s.get("video_files") or [])
                                          for s in finfo.get("sessions", [])):
        # the one case where an old leader_data.py would make us copy a multi-GB raw + .part pair
        return fail("a stranded video.mp4.part is on the leader and its leader_data.py predates the "
                    "video remux step — Deploy this rig first")
    if (n or nv) and vroot.get("separate") and not vroot.get("available"):
        job.say(rs.name, f"{folder}: {n + nv} session(s) left unconsolidated — the video tree is unavailable "
                         f"({vroot.get('reason')})", "warning")
    elif n or nv:
        what = ([f"consolidating {n} session(s)"] if n else []) + ([f"remuxing video for {nv} session(s)"] if nv else [])
        job.say(rs.name, f"{folder}: {', '.join(what)}")
        c = run_leader_data(rs, "consolidate", *leader_data_args(rs), "--folder", folder, timeout=1800)
        if not c.get("ok"):
            bad = [f"{k}: {v.get('error')}" for k, v in (c.get("results") or {}).items() if not v.get("ok")]
            job.say(rs.name, f"{folder}: consolidate had errors: {c.get('error') or '; '.join(bad)} — "
                             f"copying anyway; the folder will NOT be purged", "warning")
        for sid, r in sorted((c.get("results") or {}).items()):
            v = (r or {}).get("video") or {}
            name = sid.rsplit("/", 1)[-1]
            if v.get("adopted"):
                job.say(rs.name, f"{name}: adopted the complete video.mp4.part as video.mp4 ({v.get('frames')} frames)")
            elif v.get("remuxed"):
                job.say(rs.name, f"{name}: video.h264 -> video.mp4 ({v.get('frames')} frames)")
            elif v.get("pruned"):
                job.say(rs.name, f"{name}: removed leftovers next to a verified video.mp4")
            elif v.get("error"):
                job.say(rs.name, f"{name}: video: {v['error']}", "warning")
            if v.get("warning"):
                job.say(rs.name, f"{name}: video: {v['warning']}", "warning")
        pre, finfo, why = look()            # sidecars are gone, video is remuxed: re-read
        if why:
            return fail(why)
    if job.cancel.is_set():
        return False

    # 3. cross-rig collision guard
    why = collision(rs, folder, finfo)
    if why:
        job.say(rs.name, f"{folder}: REFUSED — {why}", "error")
        res["failed"].append({"folder": folder, "error": why})
        return False

    # 4. copy the trees that hold this folder — a session recorded without video has no video folder
    trees = []
    if finfo.get("in_data"):
        trees.append(("data", leader_dir, int(finfo.get("bytes_data") or 0)))
    if finfo.get("in_video") and video_dir:
        trees.append(("video", video_dir, int(finfo.get("bytes_video") or 0)))
    offset = bytes_before
    for _name, tree, nbytes in trees:
        if job.cancel.is_set():
            return False
        job.say(rs.name, f"{folder}: copying from {tree}")
        rc, err = _rsync_folder(job, rs, tree, folder, nbytes, offset, rig_total)
        offset += nbytes
        if rc != 0:
            return fail(f"rsync exit {rc} on {tree}: {err.strip().splitlines()[-1] if err.strip() else ''}")
    copied = sum(nbytes for _name, _tree, nbytes in trees)
    job.set_progress(rs.name, bytes_done=bytes_before + copied,
                     pct=round(100.0 * (bytes_before + copied) / max(1, rig_total), 1))

    # 5. verify against a fresh inventory: same trees, every copy clean, no recorded video missing
    post, pinfo, why = look()
    if why:
        return fail(f"after the copy: {why}")
    pv = post.get("video_root") or {}
    if pv.get("separate") and not pv.get("available"):
        return fail(f"video tree unavailable ({pv.get('reason')}) — data copied, but whether video "
                    f"belongs with it can't be checked")
    if (bool(pinfo.get("in_data")), bool(pinfo.get("in_video"))) != (bool(finfo.get("in_data")), bool(finfo.get("in_video"))):
        return fail("its trees changed during the copy — sync it again")
    for name, tree, _n in trees:
        pend, err = pending_folders(rs, tree, only=folder)
        if err or pend:
            return fail(f"verification failed on the {name} tree: {err or 'still pending after the copy'}")
    if pinfo.get("missing_video"):
        return fail(f"video expected for {', '.join(pinfo['missing_video'])} but not on the Pi; data copied")

    # 6. ledger: the copy is complete by size and time. Written before the content check, so the
    #    collision guard knows these sessions whatever the check then says.
    sessions = [s["id"] for s in pinfo.get("sessions", [])]
    nbytes = int(pinfo.get("bytes_data") or 0) + int(pinfo.get("bytes_video") or 0)
    now = time.time()

    def _upd(d):
        e = d["folders"].setdefault(ledger_key(rs.name, folder), {
            "rig": rs.name, "subject": folder.split("/")[0], "date": folder.split("_")[-1],
            "first_synced": now, "purged_at": None})
        e.update({"sessions": sorted(set((e.get("sessions") or []) + sessions)), "bytes": nbytes,
                  "last_synced": now, "last_verified": now,
                  "trees": {"data": leader_dir, "video": video_dir},
                  "present": {"data": bool(pinfo.get("in_data")), "video": bool(pinfo.get("in_video"))}})
    ledger_update(_upd)
    rs.data["last_sync_t"] = now

    # 7. content: the bytes themselves, each side read straight from its disk. A copy that differs
    #    is fetched again; the folder is green only with a passed check in the ledger.
    job.say(rs.name, f"{folder}: checking content against the Pi")
    rec, why, differs = content_check(job, rs, folder, pinfo, repair=True)
    if differs:
        _forget_content_check(rs, folder)
    if job.cancel.is_set():
        return False
    if why:
        return fail(f"content check: {why}")
    if rec.get("repaired"):
        for name, tree, _n in trees:
            pend, err = pending_folders(rs, tree, only=folder)
            if err or pend:
                return fail(f"verification failed on the {name} tree after fetching files again: "
                            f"{err or 'still pending'}")

    def _checked(d):
        e = d["folders"].get(ledger_key(rs.name, folder))
        if e is not None:
            e["verified"] = rec
            e["last_verified"] = rec["t"]
            if rec.get("repaired"):
                e.setdefault("repairs", []).append({"t": rec["t"], "files": rec["repaired"]})
    ledger_update(_checked)
    rs.data.setdefault("status", {})[folder] = "green"
    res["synced"].append(folder)
    job.say(rs.name, f"{folder}: synced and verified ({' + '.join(t[0] for t in trees)}, {nbytes / 1e6:.1f} MB; "
                     f"content of {rec['files']} file(s) checked in {rec['seconds']:.0f} s"
                     + (f", {len(rec['repaired'])} fetched again" if rec.get("repaired") else "") + ")")
    unstarted = [s["id"] for s in pinfo.get("sessions", [])
                 if s.get("camera_requested") is True and s.get("camera_saved") is False]
    if unstarted:
        job.say(rs.name, f"{folder}: note — the camera was checked but never started recording for "
                         f"{', '.join(unstarted)}; no video was expected", "warning")

    # 8. auto-purge: the full purge path, re-verified from scratch — except the hashing, done a
    #    moment ago
    if job.params.get("auto_purge"):
        purge_folders(job, rs, [folder], checked=(folder,))
    return True


def _forget_content_check(rs: RigState, folder: str):
    """A copy turned out to hold other bytes than the Pi's file: drop the folder's passed check
    from the ledger, so it shows red until a Sync Now has fetched the file again."""
    def _clear(d):
        e = d["folders"].get(ledger_key(rs.name, folder))
        if e is not None:
            e.pop("verified", None)
    ledger_update(_clear)
    rs.data.get("status", {}).pop(folder, None)
    rs.data["inventory_t"] = 0


def purge_folders(job: Job, rs: RigState, folders: list, checked=()) -> list:
    """Delete date folders on the Pi, tree by tree, and only copies that re-verified moments ago.
    Everything is decided from a fresh inventory, never from the card the page showed: the ledger
    must hold a passed content check of the folder as it is on the Pi now (content_state), each
    tree holding it must pass a strict dry run, and — when `verify_checksum_before_purge` is on —
    both sides are hashed once more (content_check without repair; skipped for the folders in
    `checked`, which the caller content-checked a moment ago). The Pi is told exactly which trees
    were verified and refuses the rest. A cancelled purge deletes nothing more. Returns the purged
    folder keys; refusals are logged and collected in the job result's `refused`."""
    res = job.result[rs.name]
    refused = res.setdefault("refused", [])

    def refuse(keys, why, level="warning"):
        for k in keys:
            job.say(rs.name, f"{k}: NOT purged — {why}", level)
            refused.append({"folder": k, "reason": why})

    folders = list(dict.fromkeys(folders))
    bad = [f for f in folders if not (isinstance(f, str) and FOLDER_KEY_RE.match(f))]
    refuse(bad, "not a <subject>/<subject>_<date> folder name", "error")
    folders = [f for f in folders if f not in bad]
    if not folders:
        return []
    inv = scoped_inventory(rs, folders)
    droot, vroot = inv.get("data_root") or {}, inv.get("video_root") or {}
    if not inv.get("ok"):
        why = f"inventory failed: {inv.get('error')}"
    elif proto_outdated(inv):
        why = DEPLOY_NEEDED
    elif inv.get("engine_running"):
        why = "the experiment engine is running on the leader"
    elif not droot.get("available"):
        why = f"data tree unavailable: {droot.get('reason')}"
    elif vroot.get("separate") and not vroot.get("available"):
        why = f"video tree unavailable: {vroot.get('reason')}"
    else:
        why = None
    if why:
        refuse(folders, why, "error")
        return []
    leader_dir, video_dir = data_dirs(rs)
    checksum = bool((settings.load().get("sync") or {}).get("verify_checksum_before_purge"))
    on_pi = {f["key"]: f for f in inv.get("folders", [])}
    ledger = ledger_load().get("folders", {})
    verified = {}
    for folder in folders:
        if job.cancel.is_set():
            break
        f = on_pi.get(folder)
        if f is None:
            job.say(rs.name, f"{folder}: not on the Pi — nothing to purge")
            continue
        if f.get("n_unconsolidated"):
            refuse([folder], f"{f['n_unconsolidated']} unconsolidated session(s) — sync it first")
            continue
        if f.get("missing_video"):
            refuse([folder], "video recorded but not on the Pi: " + ", ".join(f["missing_video"]))
            continue
        why = content_state(ledger.get(ledger_key(rs.name, folder)), f)
        if why:
            refuse([folder], why)
            continue
        trees = ([("data", leader_dir)] if f.get("in_data") else []) + \
                ([("video", video_dir)] if (f.get("in_video") and video_dir) else [])
        why = None
        for name, tree in trees:
            _rc, pend, err = _dry_run(rs, tree, only=folder)
            if err or pend:
                why = f"{name} tree: {err or 'not fully copied to the data root'}"
                break
        if not why and checksum and trees and folder not in checked:
            # the bytes once more, right before deleting: rsync's own -c would hash what the Pi's
            # page cache hands out, which is the read this check exists to distrust
            job.say(rs.name, f"{folder}: checking content against the Pi before deleting")
            rec, cwhy, differs = content_check(job, rs, folder, f, repair=False)
            if job.cancel.is_set():
                break
            if differs:
                _forget_content_check(rs, folder)
            if cwhy:
                why = f"content check before deleting: {cwhy}" + (" — Sync Now fetches it again" if differs else "")
            else:
                def _stamp(d, k=ledger_key(rs.name, folder), rec=rec):
                    e = d["folders"].get(k)
                    if e is not None:
                        e["verified"] = rec
                        e["last_verified"] = rec["t"]
                ledger_update(_stamp)
        if why:
            refuse([folder], why)
        elif trees:
            verified[folder] = [name for name, _tree in trees]
    if job.cancel.is_set():
        job.say(rs.name, "cancelled — nothing more is purged", "warning")
        return []
    if not verified:
        return []
    args = [a for k in verified for a in ("--folder", k)]
    args += [a for k, ts in verified.items() for t in ts for a in ("--verified", f"{t}:{k}")]
    out = run_leader_data(rs, "purge", *leader_data_args(rs), *args, timeout=900)
    if not out.get("results"):
        refuse(list(verified), f"the Pi refused: {out.get('error')}", "error")
        return []
    purged = []
    now = time.time()
    for folder, r in out["results"].items():
        if not r.get("ok"):
            refuse([folder], "the Pi refused: " + ("; ".join(r.get("errors") or []) or "unknown error"), "error")
            continue
        purged.append(folder)
        removed = r.get("trees_removed") or []
        job.say(rs.name, f"{folder}: purged on the Pi — {' + '.join(removed)} ({r.get('bytes_freed', 0) / 1e6:.1f} MB freed)")

        def _mark(d, k=ledger_key(rs.name, folder), t=removed):
            d["folders"].setdefault(k, {"rig": rs.name}).update({"purged_at": now, "purged_trees": t})
        ledger_update(_mark)
        rs.data.get("status", {}).pop(folder, None)
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
            if card.get("proto_outdated"):
                job.say(rs.name, f"NOT synced: {DEPLOY_NEEDED}", "error")
                job.set_progress(rs.name, state="failed")
                if job.params.get("poweroff"):
                    job.result[rs.name]["poweroff"] = "skipped"
                    job.say(rs.name, "NOT powered off: nothing could be synced", "warning")
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
            purged = purge_folders(job, rs, list(items.get(rs.name) or []))
            job.set_progress(rs.name, state="done", pct=100)
            n_refused = len(job.result[rs.name].get("refused") or [])
            job.say(rs.name, f"purged {len(purged)} folder(s)" + (f", refused {n_refused}" if n_refused else ""))
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
