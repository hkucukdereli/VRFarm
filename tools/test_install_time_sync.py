#!/usr/bin/env python3
"""tools/test_install_time_sync.py — the Setup tab's Install and the Pi clocks, no Pi: Install puts
chrony on every Pi (apt) and logs what chrony says (controller/setup.py `_time_sync_step`). ssh and
scp are replaced by recorders, so no command leaves this machine.

    conda activate vrfarm && python tools/test_install_time_sync.py
"""
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, g                                    # noqa: E402
from controller import setup                                  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


# `systemctl is-active chrony` followed by `chronyc -n tracking`, as cheddar's leader printed them.
SYNCED = """active
Reference ID    : 4E297495 (78.41.116.149)
Stratum         : 3
Ref time (UTC)  : Thu Nov 19 22:12:29 2026
System time     : 0.000046210 seconds slow of NTP time
Last offset     : +0.000011267 seconds
RMS offset      : 0.000166396 seconds
Frequency       : 1.198 ppm fast
Residual freq   : +0.000 ppm
Skew            : 0.044 ppm
Root delay      : 0.009215287 seconds
Root dispersion : 0.005495615 seconds
Update interval : 1034.6 seconds
Leap status     : Normal
"""
NO_SERVER = """active
Reference ID    : 00000000 ()
Stratum         : 0
Ref time (UTC)  : Thu Jan 01 00:00:00 1970
System time     : 0.000000000 seconds fast of NTP time
Leap status     : Not synchronised
"""
STOPPED = "inactive\n506 Cannot talk to daemon\n"

calls = []          # every command the code under test wanted to run on the Pi
answer = {"time": SYNCED}


def fake_ssh(tgt, cmd, timeout=60, check=True):
    calls.append(cmd)
    return "3.13\n3.13\n" if "sys.version_info" in cmd else ""


def fake_scp(*a, **kw):
    calls.append("scp " + " ".join(map(str, a)))


def fake_ssh_result(tgt, cmd, timeout=60):
    calls.append(cmd)
    if isinstance(answer["time"], Exception):
        raise answer["time"]
    out, err = answer["time"] if isinstance(answer["time"], tuple) else (answer["time"], "")
    return subprocess.CompletedProcess([], 0 if out else 255, out, err)


setup.ssh, setup.scp, setup.ssh_result = fake_ssh, fake_scp, fake_ssh_result

print("the log line")
line = setup._time_sync_step("vruser@10.0.0.1")
check(line == "Time sync: chrony, server 78.41.116.149, clock 0.05 ms slow", f"synchronised: {line!r}")
check("enable --now chrony" in calls[-1] and "waitsync" in calls[-1] and "tracking" in calls[-1],
      "one command: start chrony if it is down, wait for a server, read tracking")
answer["time"] = NO_SERVER
line = setup._time_sync_step("vruser@10.0.0.1")
check(line.startswith("WARN") and "no time server" in line, f"no server yet: {line!r}")
answer["time"] = STOPPED
line = setup._time_sync_step("vruser@10.0.0.1")
check(line.startswith("WARN") and "not running (inactive)" in line, f"daemon down: {line!r}")
answer["time"] = ("", "ssh: connect to host 10.0.0.1 port 22: No route to host")
line = setup._time_sync_step("vruser@10.0.0.1")
check(line.startswith("WARN: time sync not checked") and "No route to host" in line, f"no answer: {line!r}")
answer["time"] = subprocess.TimeoutExpired("ssh", 60)
line = setup._time_sync_step("vruser@10.0.0.1")
check(line.startswith("WARN: time sync not checked"), f"ssh timeout: {line!r}")

print("Install")
app = Flask(__name__)
rs = SimpleNamespace(logger=logging.getLogger("test_install"), pis=[], name="test")


def install(role, devices, time_answer):
    calls.clear()
    answer["time"] = time_answer
    with app.test_request_context(json={"ip": "10.0.0.1", "user": "vruser", "role": role, "devices": devices}):
        g.rs = rs
        return setup.api_install_pi().get_json()


res = install("leader", ["lick_sensor", "reward"], SYNCED)
check(res["ok"], "leader Install runs through")
apt = [c for c in calls if "apt-get install" in c]
check(len(apt) == 1 and " chrony" in apt[0] and " rsync" in apt[0], "apt installs chrony next to rsync")
i = next((k for k, s in enumerate(res["steps"]) if s.startswith("Installed system packages")), -1)
check(i >= 0 and "chrony" in res["steps"][i] and res["steps"][i + 1].startswith("Time sync: chrony"),
      "the time sync line follows the apt step")
check(calls.index(apt[0]) < next(k for k, c in enumerate(calls) if "chronyc" in c),
      "chrony is asked only after apt has installed it")
res = install("leader", [], NO_SERVER)
check(res["ok"] and any(s.startswith("WARN: chrony runs but has no time server") for s in res["steps"]),
      "a Pi without a time server is a warning, the Install still completes")
check(any(" chrony" in c for c in calls if "apt-get install" in c), "chrony does not depend on the device list")

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
