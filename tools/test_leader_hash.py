#!/usr/bin/env python3
"""tools/test_leader_hash.py — the Data tab's content check, piece by piece: the direct reads, file
list, fingerprint and `hash` / `read` commands of shared/leader_data.py, and what controller/sync.py
makes of them (content_state; content_check on a wrong, short, missing or stale copy; the fetch that
replaces a file only when two direct reads agree). The "Pi" is this machine — the controller's ssh
helpers run a 127.0.0.1 leader locally. No hardware, no controller app.

The scratch tree goes under ~/.cache when there is one, so the reads hit a real disk with O_DIRECT
(a temp dir on tmpfs has no direct I/O, or only a pretend one); the first line says which it was.
    conda activate vrfarm && python tools/test_leader_hash.py
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import h5py

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PY = sys.executable

_cache = Path.home() / ".cache"
TMP = Path(tempfile.mkdtemp(prefix="vrfarm_hash_test_", dir=str(_cache) if _cache.is_dir() else None))
DATA_ROOT = TMP / "data_root"
(TMP / "controller.yaml").write_text(f"data_root: {DATA_ROOT}\n")
os.environ["VRFARM_SETTINGS"] = str(TMP / "controller.yaml")       # read when controller.settings is imported
from shared import leader_data as L                                 # noqa: E402
from controller import sync as S                                    # noqa: E402
from controller.jobs import Job                                     # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


def sha(b):
    return hashlib.sha256(b).hexdigest()


def cli(*args):
    """The Pi-side script as a process: (exit code, stdout bytes, stderr text)."""
    r = subprocess.run([PY, "-m", "shared.leader_data", *args], cwd=str(ROOT), capture_output=True)
    return r.returncode, r.stdout, r.stderr.decode(errors="replace")


def reply(out) -> dict:
    try:
        return json.loads((out.decode() if isinstance(out, bytes) else out).strip().splitlines()[-1])
    except Exception:
        return {}


def in_process(argv):
    """leader_data.main() in this process (so a test can patch it): (exit code, reply, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = L.main(argv)
    return rc, reply(out.getvalue()), err.getvalue()


def quiet(fn, *a, **kw):
    """Job.say prints every log line; keep the test output to the checks."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


def corrupt(path, at=0, n=64):
    """Other bytes at one place — same size, same mtime: what a bad read leaves in a copy."""
    st = path.stat()
    with open(path, "r+b") as f:
        f.seek(at)
        b = f.read(n)
        f.seek(at)
        f.write(bytes(x ^ 0xFF for x in b))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


# ── direct reads ──
try:
    os.close(os.open(TMP / "controller.yaml", os.O_RDONLY | os.O_DIRECT))
    HAS_DIRECT = True
except OSError:
    HAS_DIRECT = False
print(f"scratch: {TMP} — O_DIRECT {'available' if HAS_DIRECT else 'NOT available here: every read falls back to a buffered one'}")
print("direct reads")
B = L.READ_BUF
rd = TMP / "reads"
rd.mkdir()
flags, wrong = [], []
for size in (0, 1, 4095, 4096, 4097, B - 1, B, B + 1, 2 * B + 513):
    data = os.urandom(size)
    (rd / f"f{size}").write_bytes(data)
    ticks = []
    digest, n, direct = L.file_digest(rd / f"f{size}", tick=ticks.append)
    flags.append(direct)
    if digest != sha(data) or n != size or sum(ticks) != size:
        wrong.append(size)
check(not wrong, f"file_digest equals hashlib's SHA-256 at every buffer edge, 0 .. {2 * B + 513} bytes (wrong: {wrong})")
check(set(flags) == {HAS_DIRECT}, f"every read says whether it was direct ({set(flags)})")
p = rd / f"f{B + 1}"
d2, _n, direct2 = L.file_digest(p, direct=False)
check(d2 == sha(p.read_bytes()) and direct2 is False, "direct=False reads buffered, gives the same digest and says so")
fresh = rd / "fresh"
a, b = os.urandom(300_000), os.urandom(300_000)
fresh.write_bytes(a)
da = L.file_digest(fresh)[0]
with open(fresh, "r+b") as f:
    f.write(b)
db = L.file_digest(fresh)[0]
check(da == sha(a) and db == sha(b), "a file written a moment ago (still dirty in the page cache) hashes to its new bytes")


class Boom(Exception):
    pass


def boom(_chunk):
    raise Boom()


try:
    L.read_file(p, boom)
    raised = False
except Boom:
    raised = True
check(raised, "an exception in the sink comes out of read_file as itself")
try:
    L.file_digest(rd / "nope")
    raised = False
except OSError:
    raised = True
check(raised, "a missing file raises OSError")

# ── the scratch Pi: one date folder in two trees, with everything rsync leaves out ──
print("file list and fingerprint")
KEY, SID = "M1/M1_20260901", "M1_20260901_001"
pi_data, pi_video = TMP / "pi_data", TMP / "pi_video"
ARGS = ("--leader-dir", str(pi_data), "--video-dir", str(pi_video))
d, v = pi_data / KEY / SID, pi_video / KEY / SID
(d / "plots").mkdir(parents=True)
v.mkdir(parents=True)
with h5py.File(str(d / f"{SID}.h5"), "w") as f:
    f.create_dataset("trial_num", data=list(range(5)))
    f.attrs["format_version"] = 2
    f.attrs["camera_saved"] = True
    f.create_group("trials")
(d / "plots" / "a.png").write_bytes(os.urandom(5000))
(d / "empty.txt").write_bytes(b"")
(v / "video.mp4").write_bytes(os.urandom(2 * B + 513))
(v / "frame_timestamps.npy").write_bytes(os.urandom(B))
os.symlink(f"{SID}.h5", d / "latest.h5")                    # rsync -l copies a link as a link
os.symlink("plots", d / "plots_link")
(d / ".DS_Store").write_bytes(b"x")
(d / "plots" / ".DS_Store").write_bytes(b"x")
(d / f"{SID}.h5.consolidating").write_bytes(b"x" * 10)
(v / "video.mp4.part").write_bytes(b"x" * 10)
(d / "old.part").mkdir()
(d / "old.part" / "inside.bin").write_bytes(b"x")
(d / ".rsync-partial").mkdir()
(d / ".rsync-partial" / "half.h5").write_bytes(b"x")
os.mkfifo(d / "pipe")
TREE_ROOT = {"data": pi_data, "video": pi_video}
WANT = {"data": {f"{SID}/{SID}.h5", f"{SID}/plots/a.png", f"{SID}/empty.txt"},
        "video": {f"{SID}/video.mp4", f"{SID}/frame_timestamps.npy"}}
LINKS = {f"{SID}/latest.h5": f"{SID}.h5", f"{SID}/plots_link": "plots"}
TOTAL = sum((TREE_ROOT[t] / KEY / rel).stat().st_size for t in WANT for rel in WANT[t])

files, links, errors = L._archive_files(pi_data / KEY)
check({f[0] for f in files} == WANT["data"] and dict(links) == LINKS and not errors,
      f"regular files and links only — temp files, .DS_Store, the partial dir and a FIFO are left out ({sorted(f[0] for f in files)})")
rsync = shutil.which("rsync")
if not rsync:
    print("  skip the comparison with rsync (no rsync on PATH)")
else:
    dst = TMP / "rsync_dst"
    dst.mkdir()
    r = subprocess.run([rsync, "-rlt", "--partial", "--partial-dir=.rsync-partial", "--modify-window=1",
                        "--exclude=.DS_Store", *S.TEMP_EXCLUDES, str(pi_data / KEY) + "/", str(dst) + "/"],
                       capture_output=True, text=True)
    got_f, got_l = set(), {}
    for base, dirs, names in os.walk(dst):
        for name in names + dirs:
            pth = Path(base) / name
            if pth.is_symlink():
                got_l[str(pth.relative_to(dst))] = os.readlink(pth)
            elif pth.is_file():
                got_f.add(str(pth.relative_to(dst)))
    check(r.returncode == 0 and got_f == WANT["data"] and got_l == LINKS,
          f"the controller's rsync copies exactly that set (exit {r.returncode}; {len(got_f)} files, {len(got_l)} links)")

fp0 = L._fingerprint(files, links)
i = next(k for k, f in enumerate(files) if f[0].endswith(".h5"))
rel, size, mt = files[i]
variants = {"a size": files[:i] + [(rel, size + 1, mt)] + files[i + 1:],
            "an mtime, by 1 ns": files[:i] + [(rel, size, mt + 1)] + files[i + 1:],
            "a name": files[:i] + [(rel + "x", size, mt)] + files[i + 1:],
            "a file less": files[:i] + files[i + 1:]}
same = [k for k, fl in variants.items() if L._fingerprint(fl, links) == fp0]
check(len(fp0) == 64 and L._tree_fp(pi_data / KEY) == fp0 and not same
      and L._fingerprint(files, [(links[0][0], "elsewhere")] + links[1:]) != fp0,
      f"the fingerprint changes with a size, an mtime, a name, a missing file, a link target (unchanged by: {same})")
h5p = d / f"{SID}.h5"
st = h5p.stat()
os.utime(h5p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
fp1 = L._tree_fp(pi_data / KEY)
os.utime(h5p, ns=(st.st_atime_ns, st.st_mtime_ns))
(d / "late.part").write_bytes(b"x")
fp2 = L._tree_fp(pi_data / KEY)
(d / "late.part").unlink()
check(fp1 != fp0 and fp2 == fp0 and L._tree_fp(pi_data / KEY) == fp0,
      "on disk: an mtime 1 s later (inside rsync's --modify-window) is another fingerprint, a temp file is not")
if os.geteuid() == 0:
    print("  skip the unreadable-directory cases (running as root)")
else:
    (d / "plots").chmod(0)
    try:
        _f, _l, errs = L._archive_files(pi_data / KEY)
        check(bool(errs) and L._tree_fp(pi_data / KEY) is None, f"a directory that cannot be listed: no fingerprint ({errs})")
    finally:
        (d / "plots").chmod(0o755)

# ── leader_data hash ──
print("hash")
rc, out, err = cli("inventory", *ARGS)
inv = reply(out)
finfo = next((f for f in inv.get("folders", []) if f["key"] == KEY), {})
check(inv.get("proto") == 3 and (finfo.get("fp") or {}).get("data") == fp0 and set(finfo["fp"]) == {"data", "video"},
      f"inventory (protocol {inv.get('proto')}) carries each tree's fingerprint")
rc, out, err = cli("hash", *ARGS, "--folder", KEY)
h = reply(out)
fr = (h.get("folders") or {}).get(KEY) or {}
trees = fr.get("trees") or {}
check(rc == 0 and h.get("ok") and h.get("algo") == "sha256" and fr.get("ok")
      and {t: x.get("fp") for t, x in trees.items()} == finfo.get("fp"), "ok, sha256, the same fingerprints as the inventory")
want_h = {t: {rel: sha((TREE_ROOT[t] / KEY / rel).read_bytes()) for rel in WANT[t]} for t in WANT}
got_h = {t: {f[0]: (f[3] if len(f) == 4 else None) for f in x["files"]} for t, x in trees.items()}
check(got_h == want_h, "one SHA-256 per copied file, equal to hashlib's, and nothing else listed")
check(all(f[1] == (TREE_ROOT[t] / KEY / f[0]).stat().st_size and f[2] == (TREE_ROOT[t] / KEY / f[0]).stat().st_mtime_ns
          for t, x in trees.items() for f in x["files"])
      and sum(x["bytes"] for x in trees.values()) == TOTAL and {ln[0]: ln[1] for ln in trees["data"]["links"]} == LINKS
      and all(x["direct"] is HAS_DIRECT for x in trees.values()),
      f"each file with its size and mtime_ns, the links, the byte total ({TOTAL}) and direct={HAS_DIRECT}")
plines = [ln for ln in err.splitlines() if ln.startswith("P ")]
check(bool(plines) and plines[0] == f"P 0 {TOTAL}" and plines[-1] == f"P {TOTAL} {TOTAL}",
      f"progress lines on stderr, from 0 to the total ({plines[:1]} .. {plines[-1:]})")
rc, out, _e = cli("hash", "--leader-dir", str(pi_data), "--folder", KEY)
one = reply(out)
check(set(((one.get("folders") or {}).get(KEY) or {}).get("trees") or {}) == {"data"}
      and set(next(f for f in reply(cli("inventory", "--leader-dir", str(pi_data))[1])["folders"] if f["key"] == KEY)["fp"]) == {"data"},
      "a rig with one tree: the data tree only, in the inventory and in the hash")
rc, out, _e = cli("hash", *ARGS)
check(rc == 2 and reply(out).get("ok") is False, "no --folder: refused")
rc, out, _e = cli("hash", *ARGS, "--folder", "../M1", "--folder", "M9/M9_20260909", "--folder", KEY)
many = reply(out).get("folders") or {}
check(many.get("../M1", {}).get("ok") is False and many.get("M9/M9_20260909", {}).get("trees") == {} and many.get(KEY, {}).get("ok") is True,
      "a name that is no date folder fails alone; a folder that is not there has no trees; the good one is hashed")
if os.geteuid() != 0:
    KEY2 = "M2/M2_20260902"
    d2 = pi_data / KEY2 / "M2_20260902_001"
    d2.mkdir(parents=True)
    (d2 / "locked.bin").write_bytes(b"x" * 100)
    (d2 / "locked.bin").chmod(0)
    rc, out, _e = cli("hash", *ARGS, "--folder", KEY2, "--folder", KEY)
    two = reply(out)
    f2 = (two.get("folders") or {}).get(KEY2) or {}
    check(rc == 0 and two.get("ok") is False and f2.get("ok") is False and any("locked.bin" in e for e in f2.get("errors", []))
          and two["folders"][KEY]["ok"] is True, f"a file that cannot be read fails its own folder only ({f2.get('errors')})")
    shutil.rmtree(pi_data / "M2")

png = d / "plots" / "a.png"
st_png = png.stat()
real_digest = L.file_digest


def touching_digest(path, direct=True, tick=None):
    res = real_digest(path, direct=direct, tick=tick)
    if str(path).endswith("a.png"):
        os.utime(path, ns=(st_png.st_atime_ns, st_png.st_mtime_ns + 1))
    return res


L.file_digest = touching_digest
try:
    rc, h2, _e = in_process(["hash", *ARGS, "--folder", KEY])
finally:
    L.file_digest = real_digest
    os.utime(png, ns=(st_png.st_atime_ns, st_png.st_mtime_ns))
fr2 = (h2.get("folders") or {}).get(KEY) or {}
check(fr2.get("ok") is False and any("a.png: changed while it was read" in e for e in fr2.get("errors", []))
      and [len(f) for f in fr2["trees"]["data"]["files"] if f[0].endswith("a.png")] == [3],
      "a file that changes while it is read fails its folder and gets no digest")
real_engine = L.engine_running
L.engine_running = lambda: True
try:
    rc_h, h3, _e = in_process(["hash", *ARGS, "--folder", KEY])
    rc_r, _r, err_r = in_process(["read", *ARGS, "--tree", "data", "--folder", KEY, "--file", f"{SID}/{SID}.h5"])
finally:
    L.engine_running = real_engine
check(rc_h == 2 and h3.get("ok") is False and rc_r == 3 and err_r.startswith("read: "), "hash and read are refused while the engine runs")

# ── leader_data read ──
print("read")
bad_reads = []
for tree, rel in (("data", f"{SID}/{SID}.h5"), ("video", f"{SID}/video.mp4"), ("data", f"{SID}/empty.txt"), ("data", f"{SID}/plots/a.png")):
    rc, out, err = cli("read", *ARGS, "--tree", tree, "--folder", KEY, "--file", rel)
    if rc != 0 or out != (TREE_ROOT[tree] / KEY / rel).read_bytes() or sha(out) != want_h[tree][rel]:
        bad_reads.append(rel)
check(not bad_reads, f"stdout is the file's bytes and nothing else — an h5, a video over two buffers, an empty file, a nested one (wrong: {bad_reads})")
outside = TMP / "outside.bin"
outside.write_bytes(b"not session data")
KEY3 = "M3/M3_20260903"
d3 = pi_data / KEY3
(d3 / "sub").mkdir(parents=True)
(d3 / "ok.bin").write_bytes(b"ok")
(d3 / "sub" / "in.bin").write_bytes(b"in")
os.symlink("ok.bin", d3 / "link_to_file")
os.symlink(str(outside), d3 / "link_out")
os.symlink("sub", d3 / "dirlink")
refusals = {
    "a path that climbs out": ["--tree", "data", "--folder", KEY3, "--file", "../../../outside.bin"],
    "an absolute path": ["--tree", "data", "--folder", KEY3, "--file", str(outside)],
    "an empty path component": ["--tree", "data", "--folder", KEY3, "--file", "sub//in.bin"],
    "a dot component": ["--tree", "data", "--folder", KEY3, "--file", "./ok.bin"],
    "an empty --file": ["--tree", "data", "--folder", KEY3, "--file", ""],
    "a link to a file": ["--tree", "data", "--folder", KEY3, "--file", "link_to_file"],
    "a link that leaves the folder": ["--tree", "data", "--folder", KEY3, "--file", "link_out"],
    "a file reached through a linked directory": ["--tree", "data", "--folder", KEY3, "--file", "dirlink/in.bin"],
    "a directory": ["--tree", "data", "--folder", KEY3, "--file", "sub"],
    "a missing file": ["--tree", "data", "--folder", KEY3, "--file", "nope.bin"],
    "a folder that is no date folder": ["--tree", "data", "--folder", "../..", "--file", "ok.bin"],
    "two folders": ["--tree", "data", "--folder", KEY3, "--folder", KEY, "--file", "ok.bin"],
}
not_refused = []
for name, extra in refusals.items():
    rc, out, err = cli("read", *ARGS, *extra)
    if rc != 3 or out != b"" or not err.startswith("read: "):
        not_refused.append(f"{name} (exit {rc}, {len(out)} bytes)")
rc, out, err = cli("read", "--leader-dir", str(pi_data), "--tree", "video", "--folder", KEY3, "--file", "ok.bin")
if rc != 3 or out != b"" or not err.startswith("read: "):
    not_refused.append(f"the video tree of a rig without one (exit {rc})")
check(not not_refused, f"{len(refusals) + 1} requests refused with exit 3 and an empty stdout (not refused: {not_refused})")
rc, out, _e = cli("read", *ARGS, "--tree", "data", "--folder", KEY3, "--file", "sub/in.bin")
check(rc == 0 and out == b"in", "the plain file next to them is served")
shutil.rmtree(pi_data / "M3")

# ── the controller's side ──
print("controller: content_state")
CU, CS, CN = S.CONTENT_UNCHECKED, S.CONTENT_STALE, S.CONTENT_NO_FP
A64, B64, C64 = "a" * 64, "b" * 64, "c" * 64
fpd = {"data": A64, "video": B64}
cases = [("no fingerprint in the inventory (an old leader)", {"verified": {"fp": fpd}}, {}, CN),
         ("a tree that could not be listed", {"verified": {"fp": fpd}}, {"fp": {"data": A64, "video": None}}, CN),
         ("no ledger entry", None, {"fp": fpd}, CU),
         ("an entry without a passed check", {"last_synced": 1.0}, {"fp": fpd}, CU),
         ("a check of other files", {"verified": {"fp": {"data": A64, "video": C64}}}, {"fp": fpd}, CS),
         ("a check that did not cover a tree", {"verified": {"fp": {"data": A64}}}, {"fp": fpd}, CS),
         ("a check of exactly these files", {"verified": {"fp": dict(fpd)}}, {"fp": fpd}, None),
         ("a checked tree that has left the Pi since", {"verified": {"fp": dict(fpd)}}, {"fp": {"data": A64}}, None)]
off = [name for name, entry, f, want in cases if S.content_state(entry, f) != want]
check(not off, f"{len(cases)} cases, each with the expected verdict (off: {off})")

print("controller: content_check")
rs = types.SimpleNamespace(name="scratch", data={},
                           config={"data": {"leader_dir": str(pi_data), "video_dir": str(pi_video)}},
                           leader=lambda: {"name": "scratch-leader", "ip": "127.0.0.1", "user": os.environ.get("USER", "vruser")})


def new_job():
    return Job("sync", ["scratch"])


def fresh_finfo():
    return next(f for f in reply(cli("inventory", *ARGS, "--folder", KEY)[1])["folders"] if f["key"] == KEY)


def pi_file(rel):
    return TREE_ROOT["data" if rel in WANT["data"] else "video"] / KEY / rel


def copy_to_root():
    """What rsync leaves under the data root: both trees merged into one folder, mtimes kept."""
    shutil.rmtree(DATA_ROOT / KEY, ignore_errors=True)
    for t in WANT:
        for rel in WANT[t]:
            (DATA_ROOT / KEY / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(TREE_ROOT[t] / KEY / rel, DATA_ROOT / KEY / rel)
    for rel, target in LINKS.items():
        os.symlink(target, DATA_ROOT / KEY / rel)


def same_as_pi():
    return all((DATA_ROOT / KEY / rel).read_bytes() == pi_file(rel).read_bytes() for t in WANT for rel in WANT[t])


def kept_copies(name):
    return sorted((DATA_ROOT / S.REPLACED_DIR / KEY / SID).glob(name + ".*"))


def leftovers():
    return [str(x) for x in (DATA_ROOT / KEY).rglob(".fetch.*")]


copy_to_root()
finfo = fresh_finfo()
rec, why, differs = quiet(S.content_check, new_job(), rs, KEY, finfo)
check(why is None and differs is False and rec is not None and rec["files"] == 5 and rec["bytes"] == TOTAL and rec["fp"] == finfo["fp"]
      and rec["algo"] == "sha256" and "repaired" not in rec and rec["direct"] == {"pi": HAS_DIRECT, "controller": HAS_DIRECT},
      f"a good copy passes: 5 files, {TOTAL} bytes, the inventory's fingerprint in the record ({why})")

vid = DATA_ROOT / KEY / SID / "video.mp4"
corrupt(vid, B + 7)
wrong_bytes = vid.read_bytes()
rec, why, differs = quiet(S.content_check, new_job(), rs, KEY, finfo, repair=False)
check(rec is None and differs is True and why == f"{SID}/video.mp4: its content differs from the Pi's"
      and vid.read_bytes() == wrong_bytes and not (DATA_ROOT / S.REPLACED_DIR).exists(),
      f"repair=False: 64 wrong bytes in an {vid.stat().st_size >> 20} MB copy of the right size and mtime are found and named, nothing is touched")
job = new_job()
rec, why, differs = quiet(S.content_check, job, rs, KEY, finfo)
kept = kept_copies("video.mp4")
check(why is None and rec["repaired"] == [f"{SID}/video.mp4"] and same_as_pi()
      and vid.stat().st_mtime_ns == (v / "video.mp4").stat().st_mtime_ns, f"repair=True: the copy is replaced with the Pi's bytes and keeps the Pi's mtime ({why})")
check(len(kept) == 1 and kept[0].read_bytes() == wrong_bytes and not leftovers() and not job.procs
      and len([x for x in job.log if x["level"] == "warning"]) == 2,
      f"the replaced copy is kept under {S.REPLACED_DIR}/, two warnings in the job log, no temp file or process left")

(DATA_ROOT / KEY / SID / "plots" / "a.png").unlink()
h5c = DATA_ROOT / KEY / SID / f"{SID}.h5"
st = h5c.stat()
h5c.write_bytes(h5c.read_bytes()[:-1])
os.utime(h5c, ns=(st.st_atime_ns, st.st_mtime_ns))
rec, why, differs = quiet(S.content_check, new_job(), rs, KEY, finfo)
check(why is None and sorted(rec["repaired"]) == sorted([f"{SID}/plots/a.png", f"{SID}/{SID}.h5"]) and same_as_pi()
      and len(kept_copies(f"{SID}.h5")) == 1 and not (DATA_ROOT / S.REPLACED_DIR / KEY / SID / "plots").exists(),
      f"a missing copy and a copy one byte short are fetched again; only the short one had something to keep ({why})")

lk = DATA_ROOT / KEY / SID / "latest.h5"
lk.unlink()
os.symlink("other.h5", lk)
rec, why, differs = quiet(S.content_check, new_job(), rs, KEY, finfo)
lk.unlink()
os.symlink(LINKS[f"{SID}/latest.h5"], lk)
check(rec is None and differs is True and "latest.h5: the link under the data root is not the Pi's" in (why or ""), f"a link with another target fails the check ({why})")

st = h5p.stat()
os.utime(h5p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
rec, why, differs = quiet(S.content_check, new_job(), rs, KEY, finfo)
os.utime(h5p, ns=(st.st_atime_ns, st.st_mtime_ns))
check(rec is None and differs is False and "changed during the content check" in (why or ""),
      f"files that changed on the Pi after the inventory are never called checked ({why})")

(v / "empty.txt").write_bytes(b"")
rec, why, differs = quiet(S.content_check, new_job(), rs, KEY, fresh_finfo())
(v / "empty.txt").unlink()
check(rec is None and differs is False and "is in both the data and the video tree" in (why or ""), f"one name in both trees is refused ({why})")

job = new_job()
job.cancel.set()
rec, why, differs = quiet(S.content_check, job, rs, KEY, finfo)
check((rec, why, differs) == (None, "cancelled", False) and not job.procs, "a cancelled job checks nothing and says so")

corrupt(vid, 5)
wrong_bytes = vid.read_bytes()
real_cmd = S.leader_cmd


def lying_leader_cmd(rs_, *args):
    if args and args[0] == "read":                           # the right size, other bytes
        return "x@127.0.0.1", f"head -c {len(wrong_bytes)} /dev/zero"
    return real_cmd(rs_, *args)


S.leader_cmd = lying_leader_cmd
try:
    rec, why, differs = quiet(S.content_check, new_job(), rs, KEY, finfo)
finally:
    S.leader_cmd = real_cmd
check(rec is None and differs is True and "two direct reads of it on the Pi do not agree — nothing replaced" in (why or "")
      and vid.read_bytes() == wrong_bytes and len(kept_copies("video.mp4")) == 1 and not leftovers(),
      f"a second read that does not match the first replaces nothing ({why})")
job = new_job()
err = quiet(S._fetch_file, job, rs, "data", KEY, f"{SID}/nope.bin", DATA_ROOT / KEY / SID / "nope.bin", 1, 0, "0" * 64)
check((err or "").startswith("the leader could not send it (exit 3)") and not (DATA_ROOT / KEY / SID / "nope.bin").exists()
      and not leftovers() and not job.procs, f"a read the leader refuses creates nothing ({err})")
rec, why, differs = quiet(S.content_check, new_job(), rs, KEY, finfo)
check(why is None and rec["repaired"] == [f"{SID}/video.mp4"] and same_as_pi() and len(kept_copies("video.mp4")) == 2,
      "and with the real leader back the same copy is repaired")

lkey = S.ledger_key("scratch", KEY)
S.ledger_update(lambda led: led["folders"].update({lkey: {"rig": "scratch", "last_synced": 1.0, "verified": rec}}))
rs.data.update({"status": {KEY: "green"}, "inventory_t": 123.0})
check(S.content_state(S.ledger_load()["folders"][lkey], finfo) is None, "with that record in the ledger the folder counts as checked")
S._forget_content_check(rs, KEY)
entry = S.ledger_load()["folders"][lkey]
check("verified" not in entry and entry.get("last_synced") == 1.0 and KEY not in rs.data["status"] and rs.data["inventory_t"] == 0
      and S.content_state(entry, finfo) == CU, "forgetting a check drops only the record: the folder is unchecked again")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'PASS' if not fails else f'{fails} FAILURE(S)'}")
sys.exit(1 if fails else 0)
