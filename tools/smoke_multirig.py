#!/usr/bin/env python3
"""
tools/smoke_multirig.py — two fake rigs on one machine, end to end, no Pi needed.

Starts two tools/mock_pi.py (rigs `demo` and `demo2` from rigs/, on different API and
command ports, both sending events to the controller's ONE UDP port) and controller/app.py,
then drives both rigs through connect -> task -> session -> deploy -> go and checks that:

  * each rig's live event stream carries only its own trials (demux by the `rig` field,
    since every mock sender is 127.0.0.1),
  * both sessions end on their own and the fleet view shows both `ended`,
  * `reset` returns both rigs to `connected`,
  * a datagram nobody owns is counted under unknown senders instead of vanishing.

    conda activate vrfarm
    python tools/smoke_multirig.py            # ~30 s; exit code 0 on PASS

Options: --port 5055 (controller UI port), --n 6 (trials per rig), --keep (leave the
processes running afterwards so you can open the page).
"""
from __future__ import annotations
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
EVENT_PORT = 5571

RIGS = [  # name, api port, command port  (must match rigs/demo.yaml and rigs/demo2.yaml)
    ("demo", 5080, 5572),
    ("demo2", 5081, 5582),
]


def _http(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class Ctl:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path, **kw):
        return _http("GET", self.base + path, **kw)

    def post(self, path, body=None, **kw):
        try:
            return _http("POST", self.base + path, body or {}, **kw)
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.code}: {e.read()[:200]!r}"}

    def rig(self, name, path, body=None, **kw):
        return self.post(f"/api/rigs/{name}{path}", body, **kw)


def wait_http(url, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def sse_reader(url, sink: list, stop: threading.Event):
    """Collect SSE events from `url` into `sink` until `stop` is set."""
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            while not stop.is_set():
                line = r.readline()
                if not line:
                    break
                if line.startswith(b"data:"):
                    try:
                        sink.append(json.loads(line[5:].strip()))
                    except Exception:
                        pass
    except Exception as e:
        if not stop.is_set():
            sink.append({"type": "_sse_error", "error": str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5055)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="vrfarm_smoke_"))
    env = dict(os.environ, VRFARM_DATA_DIR=str(scratch / "data"), PYTHONUNBUFFERED="1")
    logs = {}
    procs = []

    def spawn(name, argv, extra_env=None):
        e = dict(env, **(extra_env or {}))
        logs[name] = open(scratch / f"{name}.log", "w")
        p = subprocess.Popen(argv, cwd=str(ROOT), env=e, stdout=logs[name], stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    failures = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            failures.append(msg)

    try:
        for name, api_port, cmd_port in RIGS:
            spawn(f"mock_{name}", [PY, "tools/mock_pi.py"],
                  {"MOCK_RIG": name, "API_PORT": str(api_port), "CMD_PORT": str(cmd_port),
                   "EVENT_PORT": str(EVENT_PORT), "MOCK_N": str(args.n),
                   "MOCK_ITI": "0.4", "MOCK_TRIAL_S": "0.3"})
        spawn("controller", [PY, "controller/app.py", "--port", str(args.port), "--no-browser"])

        ctl = Ctl(args.port)
        assert wait_http(ctl.base + "/api/rigs"), "controller did not come up"
        for name, api_port, _ in RIGS:
            assert wait_http(f"http://127.0.0.1:{api_port}/api/status"), f"mock {name} did not come up"
        print(f"controller on {ctl.base}, mocks up, scratch {scratch}")

        rigs = ctl.get("/api/rigs")["rigs"]
        names = {r["name"] for r in rigs}
        check({"demo", "demo2"} <= names, f"rigs listed: {sorted(names)}")

        tasks = sorted(p.stem for p in (ROOT / "experiments").glob("*.yaml"))
        assert tasks, "no experiments/*.yaml to load"
        today = date.today().strftime("%Y%m%d")

        # connect -> task -> session -> deploy, per rig
        for i, (name, _, _) in enumerate(RIGS, start=1):
            print(f"[{name}] connect / task / session / deploy")
            r = ctl.rig(name, "/load_rig")
            check(r.get("all_pis_ok") is True, f"{name}: connect all_pis_ok ({r.get('error')})")
            r = ctl.rig(name, "/load_task", {"task": tasks[0]})
            check("task" in r, f"{name}: task {tasks[0]} loaded")
            r = ctl.rig(name, "/update_session",
                        {"subject_id": f"MOCK{i}", "date": today, "session_num": 1, "notes": "smoke"})
            check(r.get("session_id") == f"MOCK{i}_{today}_001", f"{name}: session id {r.get('session_id')}")
            r = ctl.rig(name, "/deploy", timeout=180)
            check(r.get("ok") is True, f"{name}: deploy ({r.get('error')})")

        fleet = ctl.get("/api/fleet")["rigs"]
        check(all(fleet[n]["phase"] == "deployed" for n, _, _ in RIGS), "fleet shows both deployed")

        # live streams first, then GO on both
        stop = threading.Event()
        streams = {}
        threads = []
        for name, _, _ in RIGS:
            streams[name] = []
            t = threading.Thread(target=sse_reader, args=(f"{ctl.base}/api/rigs/{name}/events", streams[name], stop),
                                 daemon=True)
            t.start()
            threads.append(t)
        time.sleep(0.5)
        for name, _, _ in RIGS:
            r = ctl.rig(name, "/go", {"save": {}, "estimate_ms": 0}, timeout=90)
            check(r.get("ok") is True, f"{name}: go ({r.get('error')})")
        fleet = ctl.get("/api/fleet")["rigs"]
        check(all(fleet[n]["phase"] == "running" for n, _, _ in RIGS), "fleet shows both running")

        # wait for both natural ends
        deadline = time.time() + args.n * 1.2 + 25
        while time.time() < deadline:
            fleet = ctl.get("/api/fleet")["rigs"]
            if all(fleet[n]["phase"] == "ended" for n, _, _ in RIGS):
                break
            time.sleep(0.5)
        fleet = ctl.get("/api/fleet")["rigs"]
        check(all(fleet[n]["phase"] == "ended" for n, _, _ in RIGS),
              "both sessions ended by themselves " + str({n: fleet[n]["phase"] for n, _, _ in RIGS}))
        time.sleep(1.0)
        stop.set()

        for name, _, _ in RIGS:
            evs = streams[name]
            trials = [e for e in evs if e.get("type") == "trial"]
            foreign = [e for e in evs if e.get("rig") not in (name, None)]
            check(len(trials) == args.n, f"{name}: {len(trials)} trial events on its stream (expected {args.n})")
            check(not foreign, f"{name}: no events from another rig on its stream ({len(foreign)} foreign)")
            check(all(e.get("_demux") == "rig_field" for e in trials),
                  f"{name}: routed by the rig field (mocks share 127.0.0.1)")
            check(fleet[name]["n_trials"] == args.n, f"{name}: fleet n_trials={fleet[name]['n_trials']}")
            check(any(e.get("type") == "session_end" for e in evs), f"{name}: session_end seen")

        # reset -> connected
        for name, _, _ in RIGS:
            r = ctl.rig(name, "/reset")
            check(r.get("ok") and r.get("phase") == "connected", f"{name}: reset -> {r.get('phase')}")

        # unknown sender: a datagram claiming a rig nobody loaded
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(json.dumps({"type": "trial", "rig": "nobody", "t": time.time()}).encode(),
                 ("127.0.0.1", EVENT_PORT))
        s.close()
        time.sleep(0.5)
        unk = ctl.get("/api/fleet")["unknown_senders"]
        check("127.0.0.1" in unk and unk["127.0.0.1"]["count"] >= 1,
              f"unknown sender counted: {unk}")

        # subject index written at session end, with the rig name
        subj = scratch / "data" / "subjects" / f"MOCK1.json"
        ok = subj.exists() and json.loads(subj.read_text())["sessions"][-1].get("rig") == "demo"
        check(ok, f"subject index written at session end with rig name ({subj})")

    except Exception as e:
        failures.append(f"exception: {e}")
        print("  EXC  ", e)
    finally:
        if args.keep and not failures:
            print(f"--keep: processes left running (controller {ctl.base}); logs in {scratch}")
        else:
            for p in procs:
                try:
                    p.terminate()
                except Exception:
                    pass
            for p in procs:
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()
        for f in logs.values():
            f.close()

    if failures:
        print(f"\nFAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        print(f"logs: {scratch}")
        sys.exit(1)
    print("\nPASS")


if __name__ == "__main__":
    main()
