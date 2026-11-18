#!/usr/bin/env python3
"""
tools/test_ir_led.py — the IR illuminator (devices/ir_led.py) end to end, without a Pi or a board.

A fake `smbus2` with one MCP4725 at 0x60 on I2C-1 stands in for the IRDriver board. Its registers
live in a file every process shares, so the test sees what the "LED" does whoever drives it: the
driver in this process, a real pi_api, a real engine/leader.py started through pi_api's
/api/start, and a scratch controller — all on loopback, on ports picked at run time.

  1. driver      EEPROM-to-0 at init, the intensity -> DAC code table, verified off, the failure
                 paths (board absent, DAC stuck, EEPROM busy / stuck), stream events, the session
                 scalars, safe_state
  2. config      the init payload, the "does this rig have one" test, templates, the deploy list
  3. pi_api      init / monitor / stop / device_set / release, the safe-state passes at start-up
                 and on SIGTERM, and what they do not cover
  4. engine      real sessions: LED on at START and off at the end, pi_api's routes refused
                 meanwhile, SIGKILL and STOP mid-session, a board that vanishes, the /ir_led group
  5. controller  Connect, the Setup card's calls, the camera preview lighting the LED

    conda activate vrfarm
    python tools/test_ir_led.py            # ~30 s; exit code 0 when everything passed

Options: --quick (parts 1-2 only, a few seconds), --keep (leave the scratch folder and its logs).
Nothing here touches a rig, the real controller or its settings. No hardware is involved either:
a pass says the software is consistent, not that a board works (docs/IR_LED.md "Status").
Run it on its own, not next to tools/smoke_data.py: part 4 starts a real engine process, and that
test's "is an engine running" check (shared/leader_data.py scans /proc) would see it and fail.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PY = sys.executable
RIG = "irledtest"

# The board. Registers in chip.json (written by whoever does a bus transaction), fault switches in
# flags.json (written only by this test, so a switch can't be lost to a concurrent register
# write), every transaction as one JSON line in bus.log with the pid that made it.
FAKE_SMBUS2 = r'''"""Fake smbus2 for tools/test_ir_led.py: one MCP4725 at 0x60 on bus 1, state in files."""
import json
import os
import time
from pathlib import Path

_DIR = Path(os.environ["FAKE_MCP4725_DIR"])
_STATE, _FLAGS, _LOG = _DIR / "chip.json", _DIR / "flags.json", _DIR / "bus.log"
_FACTORY = {"dac": 2048, "eeprom": 2048, "pd": 0, "eeprom_pd": 0, "busy_until": 0.0}


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(default)


def _save(st):
    tmp = _STATE.with_name(f"chip.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, _STATE)


def _log(**rec):
    with open(_LOG, "a") as f:
        f.write(json.dumps({"t": time.time(), "pid": os.getpid(), **rec}) + "\n")


class _Msg:
    def __init__(self, addr, n):
        self.addr, self.n, self.buf = addr, n, []

    def __iter__(self):
        return iter(self.buf)


class i2c_msg:
    @staticmethod
    def read(addr, n):
        return _Msg(addr, n)


class SMBus:
    def __init__(self, bus):
        self.bus_no = int(bus)
        _log(op="open", bus=self.bus_no)

    def _chip(self, addr):
        st, fl = _read(_STATE, _FACTORY), _read(_FLAGS, {})
        if fl.get("absent") or self.bus_no != 1 or addr != 0x60:
            _log(op="nack", bus=self.bus_no, addr=addr)
            raise OSError(121, "Remote I/O error")
        return st, fl

    def write_i2c_block_data(self, addr, cmd, data):
        st, fl = self._chip(addr)
        code = ((data[0] << 4) | (data[1] >> 4)) & 0xFFF
        pd = (cmd >> 1) & 0x3
        if time.time() < st.get("busy_until", 0):     # EEPROM write in progress: command ignored
            _log(op="ignored", bus=self.bus_no, addr=addr, cmd=cmd, code=code)
            return
        _log(op="write", bus=self.bus_no, addr=addr, cmd=cmd, code=code)
        if fl.get("stuck"):                           # ACKs, but the DAC register keeps its value
            return
        st["dac"], st["pd"] = code, pd
        if cmd & 0xE0 == 0x60:                        # DAC + EEPROM
            if not fl.get("eeprom_stuck"):
                st["eeprom"], st["eeprom_pd"] = code, pd
            if fl.get("busy_s"):
                st["busy_until"] = time.time() + float(fl["busy_s"])
        _save(st)

    def i2c_rdwr(self, msg):
        st, fl = self._chip(msg.addr)
        busy = time.time() < st.get("busy_until", 0)
        if busy and fl.get("busy_reads", "nack") == "nack":
            _log(op="nack", bus=self.bus_no, addr=msg.addr)
            raise OSError(121, "Remote I/O error")
        _log(op="read", bus=self.bus_no, addr=msg.addr)
        msg.buf = [(0 if busy else 0x80) | (st["pd"] << 1), (st["dac"] >> 4) & 0xFF,
                   (st["dac"] << 4) & 0xF0, (st["eeprom_pd"] << 5) | ((st["eeprom"] >> 8) & 0x0F),
                   st["eeprom"] & 0xFF][:msg.n]

    def close(self):
        _log(op="close", bus=self.bus_no)
'''

# intensity (% of the LDD's 0.5..2.5 V window) -> 12-bit DAC code at a 3.3 V reference. Written
# out, not computed with the driver's formula.
CODES = {0: 620, 30: 1365, 35: 1489, 40: 1613, 50: 1861, 60: 2110, 70: 2358, 80: 2606, 90: 2854,
         100: 3102}
CURVE = [[0, 0.6], [50, 1.2], [100, 2.4]]
CURVE_CODES = {0: 745, 25: 1117, 50: 1489, 75: 2234, 100: 2978}
SCALARS = ("ir_led_intensity_pct", "ir_led_dac_volts", "ir_led_on", "ir_led_on_t", "ir_led_off_t")

fails = 0
T: Path = None          # scratch folder
CHIP: Path = None       # the fake board's files


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails += 1
    return bool(cond)


# ── the fake board, seen from the test ──

def chip() -> dict:
    try:
        return json.loads((CHIP / "chip.json").read_text())
    except (OSError, ValueError):
        return {"dac": 2048, "eeprom": 2048, "pd": 0, "eeprom_pd": 0, "busy_until": 0.0}


def set_chip(**kw):
    """Set registers by hand (what a previous owner left behind). Only while nothing else writes."""
    st = chip()
    st.update(kw)
    tmp = CHIP / "chip.test.tmp"
    tmp.write_text(json.dumps(st))
    os.replace(tmp, CHIP / "chip.json")


def fresh_chip():
    """A factory-new MCP4725: DAC and EEPROM at mid-scale (the LED would light at ~60 %)."""
    set_chip(dac=2048, eeprom=2048, pd=0, eeprom_pd=0, busy_until=0.0)


def set_flags(**kw):
    """Fault switches; set_flags() clears them all. absent: every transaction NACKs. stuck: DAC
    writes ACK but change nothing. eeprom_stuck: the EEPROM keeps its value. busy_s: seconds the
    chip is busy after an EEPROM write; busy_reads 'nack' (default) or 'rdy0'."""
    tmp = CHIP / "flags.test.tmp"
    tmp.write_text(json.dumps(kw))
    os.replace(tmp, CHIP / "flags.json")


def bus_log() -> list:
    try:
        return [json.loads(l) for l in (CHIP / "bus.log").read_text().splitlines() if l.strip()]
    except OSError:
        return []


def log_mark() -> int:
    return len(bus_log())


def since(mark: int) -> list:
    return bus_log()[mark:]


def writes(recs, pid=None) -> list:
    return [r for r in recs if r["op"] == "write" and (pid is None or r["pid"] == pid)]


def wait_for(cond, timeout=10.0, step=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


def raises(exc, fn, *a, **kw):
    """The message of the `exc` that fn raises, or None if it raises nothing / something else."""
    try:
        fn(*a, **kw)
    except exc as e:
        return str(e) or type(e).__name__
    except Exception:
        return None
    return None


# ── http / processes ──

def http(method, url, body=None, timeout=30):
    """(status, json) — error statuses too, so a 409 can be checked like a 200."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300].decode("utf-8", "replace")}


def http_text(url, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def wait_http(url, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(0.2)
    return False


def free_port(kind=socket.SOCK_DGRAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def child_env(**extra) -> dict:
    env = dict(os.environ, PYTHONPATH=str(T / "fake"), FAKE_MCP4725_DIR=str(CHIP),
               PYTHONUNBUFFERED="1", **extra)
    env.pop("VRFARM_SLACK_WEBHOOK", None)     # nothing here may reach a real Slack channel
    return env


class PiApi:
    """A real pi_api/api.py on 127.0.0.1 whose home is the scratch folder: ~/rig holds links to
    this repo's engine/, devices/ and shared/, and ~/miniforge3/envs/rig/bin/python (what
    /api/start runs the engine with) is a link to this interpreter."""

    def __init__(self, port):
        self.port, self.base, self.proc, self.n = port, f"http://127.0.0.1:{port}", None, 0
        home = T / "home"
        (home / "rig").mkdir(parents=True)
        for d in ("engine", "devices", "shared"):
            (home / "rig" / d).symlink_to(ROOT / d)
        (home / "miniforge3/envs/rig/bin").mkdir(parents=True)
        (home / "miniforge3/envs/rig/bin/python").symlink_to(PY)
        self.safe_file = home / "rig" / "safe_state.json"

    def start(self) -> bool:
        self.n += 1
        self.logpath = T / f"pi_api_{self.n}.log"
        self._logf = open(self.logpath, "w")
        self.proc = subprocess.Popen(
            [PY, str(ROOT / "pi_api" / "api.py"), "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(T), env=child_env(HOME=str(T / "home")), stdout=self._logf,
            stderr=subprocess.STDOUT)
        return wait_http(self.base + "/api/status")

    def log(self) -> str:
        return self.logpath.read_text()

    def post(self, path, body=None, timeout=30):
        return http("POST", self.base + path, body if body is not None else {}, timeout)

    def get(self, path, timeout=30):
        return http("GET", self.base + path, None, timeout)

    def engine_log(self) -> list:
        return self.get("/api/logs?n=500")[1].get("lines", [])

    def engine_running(self) -> bool:
        return bool(self.get("/api/status")[1].get("process_running"))

    def wait_exit(self, timeout=10) -> bool:
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def stop(self):
        """SIGTERM (what systemd sends), SIGKILL if that is ignored."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            if not self.wait_exit(10):
                self.proc.kill()
                self.proc.wait(timeout=5)
        if getattr(self, "_logf", None):
            self._logf.close()


class Events:
    """The controller's side of the event channel: collects the engine's UDP datagrams."""

    def __init__(self, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(0.2)
        self.items, self._stop = [], threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop.is_set():
            try:
                d, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.items.append(json.loads(d))
            except ValueError:
                pass

    def mark(self) -> int:
        return len(self.items)

    def since(self, m) -> list:
        return list(self.items[m:])

    def wait(self, m, pred, timeout=30):
        return wait_for(lambda: next((e for e in self.items[m:] if pred(e)), None), timeout)

    def close(self):
        self._stop.set()
        self.sock.close()


# ── 1. the driver ──

def test_driver():
    print("1. driver (devices/ir_led.py) on the fake MCP4725")
    import numpy as np
    import smbus2
    check(Path(smbus2.__file__).parent == T / "fake", "smbus2 is the fake one")
    from devices.base import DEVICE_REGISTRY
    from devices.ir_led import IRLed, _all_off_at_exit

    cfg = {"type": "ir_led", "enabled": True, "i2c_address": "0x60", "i2c_bus": 1}

    def opened(rig=None, task=None) -> "IRLed":
        d = IRLed()
        d.init(dict(cfg, **(rig or {})), task or {})
        return d

    def quiet(fn, *a, **kw):
        """fn's result and what it printed."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            res = fn(*a, **kw)
        return res, out.getvalue()

    def eeprom_writes(recs):
        return [r for r in writes(recs) if r["cmd"] & 0xE0 == 0x60]

    # construction and registry
    m = log_mark()
    d = IRLed()
    check(since(m) == [], "IRLed() touches no hardware")
    check(DEVICE_REGISTRY.get("ir_led") is IRLed and IRLed.info.name == "ir_led"
          and IRLed.info.label == "IR Illuminator", "registered as 'ir_led' (IR Illuminator)")
    sch, live = IRLed.task_params_schema(), IRLed.live_params()
    check(sch["intensity_pct"]["default"] == 50.0 and sch["on_during_session"]["default"] is True,
          "task params: intensity_pct 50, on_during_session true")
    check(set(live) == {"intensity_pct"} and (live["intensity_pct"]["min"], live["intensity_pct"]["max"],
                                             live["intensity_pct"]["step"]) == (0, 100, 1),
          "one live parameter: intensity_pct 0..100 step 1")

    # a factory-new chip: EEPROM to 0 once, DAC to 0, LED off
    fresh_chip()
    m = log_mark()
    _, out = quiet(d.init, cfg, {})
    st, ee = chip(), eeprom_writes(since(m))
    check(st["dac"] == 0 and st["eeprom"] == 0 and not d.is_on,
          "init on a factory-new chip (2048 / 2048): DAC 0, EEPROM 0, LED off")
    check(len(ee) == 1 and ee[0]["code"] == 0, "exactly one EEPROM write, of 0")
    check("EEPROM power-up value set to 0 (was 2048)" in out, "and it says so: " + out.strip())
    d.close()
    m = log_mark()
    d = opened()
    check(eeprom_writes(since(m)) == [], "the next init writes no EEPROM")
    c = d.check()
    check(c["ok"] and "EEPROM 0" in c["message"] and ", off," in c["message"], "check(): " + c["message"])

    # on / the code table / off
    d.on()
    check(chip()["dac"] == 1861 and d.is_on, "on() at the default 50 % -> DAC 1861")
    check(d.status() == {"on": True, "intensity_pct": 50.0, "dac_code": 1861, "dac_volts": 1.5,
                         "i2c_address": "0x60"}, f"status(): {d.status()}")
    got = {}
    for pct in CODES:
        d.set_intensity(pct)
        got[pct] = chip()["dac"]
    check(got == CODES, f"intensity -> DAC code: {got}")
    check(CODES[0] > 0, "0 % is the dimmest LIT level (0.5 V), not off")
    d.set_intensity(250)
    hi = chip()["dac"]
    d.set_intensity(-5)
    check((hi, chip()["dac"]) == (3102, 620), "out-of-range intensities clamp to 100 % / 0 %")
    d.off()
    check(chip()["dac"] == 0 and not d.is_on, "off() -> DAC 0")
    d.set_intensity(80)
    check(chip()["dac"] == 0, "set_intensity while off does not light the LED")
    d.on()
    check(chip()["dac"] == 2606, "the next on() uses it (80 % -> 2606)")
    d.close()
    check(chip()["dac"] == 0 and d.bus is None, "close() switches off and releases the bus")

    # stream events and the session scalars
    d, ev = opened(), []
    d.start_stream(ev.append)
    d.set_intensity(70)
    d.stop_stream()
    check([(e["event"], e["on"], e["dac_code"]) for e in ev]
          == [("ir_led", True, 1861), ("ir_led", True, 2358), ("ir_led", False, 0)],
          f"stream: on (1861), change (2358), off (0): {[(e['on'], e['dac_code']) for e in ev]}")
    check(all("error" not in e and "t" in e for e in ev), "no `error` key on a clean run")
    d.set_intensity(60)
    check(len(ev) == 3 and d._callback is None, "nothing is announced after stop_stream")
    h = d.hdf5_session_data()
    check(tuple(sorted(h)) == tuple(sorted(SCALARS)) and all(v.shape == () for v in h.values()),
          "session scalars: " + ", ".join(sorted(h)))
    check(h["ir_led_on"] == 1 and h["ir_led_on"].dtype == np.int8
          and h["ir_led_on_t"].dtype == np.float64 and h["ir_led_on_t"] <= h["ir_led_off_t"]
          and h["ir_led_intensity_pct"].dtype == np.float32 and h["ir_led_intensity_pct"] == 60
          and abs(float(h["ir_led_dac_volts"]) - 1.7) < 0.002,
          "on = 1, on_t <= off_t, intensity and volts as last set (60 %, 1.70 V)")
    d.close()

    d, ev = opened(task={"on_during_session": False}), []
    m = log_mark()
    d.start_stream(ev.append)
    d.stop_stream()
    h = d.hdf5_session_data()
    check(all(r["code"] == 0 for r in writes(since(m))) and [e["on"] for e in ev] == [False, False],
          "on_during_session false: the LED never lights, both events say off")
    check(h["ir_led_on"] == 0 and math.isnan(h["ir_led_on_t"]) and math.isnan(h["ir_led_off_t"]),
          "its scalars: on = 0, on_t / off_t NaN")
    d.close()

    seen = []
    for rig, task in (({"intensity_pct": 30}, {"intensity_pct": 80}), ({"intensity_pct": 30}, {}), ({}, {})):
        d = opened(rig, task)
        seen.append(d.intensity_pct)
        d.close()
    check(seen == [80.0, 30.0, 50.0], f"intensity: task override > rig value > 50 ({seen})")

    # the Setup card's slider
    d = opened()
    s = d.set_live("intensity_pct", 40)
    check(s["message"] == "40 % = 1.30 V, LED off (Live lights it)" and chip()["dac"] == 0,
          "set_live while off: " + s["message"])
    d.on()
    s = d.set_live("intensity_pct", "70")
    check(s["message"] == "70 % = 1.90 V, LED ON" and chip()["dac"] == 2358 and s["on"] is True,
          "set_live while on: " + s["message"])
    check("no live parameter 'gain'" in (raises(ValueError, d.set_live, "gain", 1) or ""),
          "set_live refuses any other parameter")
    d.close()

    # safe_state: LED off without an instance
    set_chip(dac=1234, eeprom=5)
    m = log_mark()
    IRLed.safe_state(cfg)
    recs = since(m)
    check([r["op"] for r in recs] == ["open", "write", "close"] and recs[1]["cmd"] == 0x40
          and recs[1]["code"] == 0 and chip()["dac"] == 0,
          "safe_state: open, one volatile write of 0, close")
    check(chip()["eeprom"] == 5, "safe_state leaves the EEPROM alone")
    set_chip(dac=77, eeprom=0)
    IRLed.safe_state({})
    check(chip()["dac"] == 0, "safe_state({}) uses 0x60 on I2C-1")
    set_flags(absent=True)
    m = log_mark()
    check(raises(OSError, IRLed.safe_state, cfg) is not None
          and [r["op"] for r in since(m)] == ["open", "nack", "close"],
          "safe_state with no board raises OSError and still closes the bus")

    # no board / wrong address / wrong bus
    d = IRLed()
    msg = raises(RuntimeError, d.init, cfg, {}) or ""
    check("MCP4725 at 0x60 on I2C-1" in msg and d.bus is None, "init with no board: " + msg)
    check(raises(Exception, d.off) is None and "not initialised" in (raises(RuntimeError, d.on) or "")
          and d.check()["ok"] is False, "after that: off() is quiet, on() raises, check() fails")
    set_flags()
    check("MCP4725 at 0x61 on I2C-1" in (raises(RuntimeError, IRLed().init, dict(cfg, i2c_address="0x61"), {}) or "")
          and "MCP4725 at 0x60 on I2C-3" in (raises(RuntimeError, IRLed().init, dict(cfg, i2c_bus=3), {}) or ""),
          "a wrong address or bus fails the same way, naming both")
    d = opened({"i2c_address": 96})
    check(d.status()["i2c_address"] == "0x60", "the address may be an integer (96)")

    # a DAC that does not take the value
    set_flags(stuck=True)
    m = log_mark()
    msg = raises(RuntimeError, d.on) or ""
    check("DAC read-back 0 (pd 0) != written 1861" in msg and len(writes(since(m))) == 2 and not d.is_on,
          "on() with a stuck DAC: two writes, then " + msg)
    set_flags()

    # switching off fails while lit: the LED may still be on, and the driver says so
    ev = []
    d.start_stream(ev.append)
    set_flags(absent=True)
    check(raises(OSError, d.off) is not None and d.is_on, "off() with the board gone raises; is_on stays true")
    msg = raises(OSError, d.stop_stream)
    check(msg is not None and ev[-1]["on"] is True and "Remote I/O" in ev[-1].get("error", "")
          and d._callback is None, f"stop_stream raises too; its event: on = true, error = {ev[-1].get('error')}")
    set_flags(stuck=True)
    check("DAC read-back 1861" in (raises(RuntimeError, d.off) or "") and d.is_on,
          "off() with a stuck DAC: read-back error, is_on stays true")
    set_flags()
    d.off()
    check(chip()["dac"] == 0 and not d.is_on and d._off_t is not None, "once the board answers again off() works")

    # an LED that never lit has nothing to report at the stop
    ev = []
    set_flags(absent=True)
    lit = raises(OSError, d.start_stream, ev.append)
    stop = raises(OSError, d.stop_stream)
    check(lit is not None and stop is not None and len(ev) == 1 and ev[0]["on"] is False and "error" not in ev[0],
          "failed switch-on, then failed stop: one plain 'off' event, no second error")
    set_flags()

    # close() never raises; safe_state cleans up after it
    d.on()
    set_flags(absent=True)
    _, out = quiet(d.close)
    check("WARNING: could not switch the LED off on close" in out and d.bus is None and chip()["dac"] == 1861,
          "close() with the board gone: a WARNING, no exception (the DAC still holds 1861)")
    set_flags()
    IRLed.safe_state(cfg)
    check(chip()["dac"] == 0, "safe_state then switches it off")

    # EEPROM write timing
    for mode, label in (("nack", "NACKs"), ("rdy0", "answers RDY = 0")):
        fresh_chip()
        set_flags(busy_s=0.05, busy_reads=mode)
        m = log_mark()
        (d, out) = quiet(opened)
        nacks = len([r for r in since(m) if r["op"] == "nack"])
        check(chip()["eeprom"] == 0 and chip()["dac"] == 0 and (nacks > 0) == (mode == "nack"),
              f"a chip that {label} for 50 ms after the EEPROM write is waited for")
        d.close()
    fresh_chip()
    set_flags(busy_s=2.0, busy_reads="rdy0")
    t0 = time.time()
    msg = raises(RuntimeError, IRLed().init, cfg, {}) or ""
    check("EEPROM write to 0 failed" in msg and 0.4 < time.time() - t0 < 1.5,
          f"an EEPROM write that never finishes fails init after 0.5 s: {msg}")
    fresh_chip()
    set_flags(busy_s=2.0)
    check("MCP4725 at 0x60" in (raises(RuntimeError, IRLed().init, cfg, {}) or ""), "same when it NACKs throughout")
    fresh_chip()
    set_flags(eeprom_stuck=True)
    msg = raises(RuntimeError, IRLed().init, cfg, {}) or ""
    check("EEPROM write to 0 failed (reads 2048" in msg, "an EEPROM that keeps its value fails init: " + msg)
    set_flags()
    set_chip(dac=0, eeprom=0, eeprom_pd=2, pd=0, busy_until=0.0)
    m = log_mark()
    (d, _) = quiet(opened)
    check(len(eeprom_writes(since(m))) == 1 and chip()["eeprom_pd"] == 0,
          "power-down bits in the EEPROM count as 'not 0' and are rewritten")
    d.close()
    set_chip(pd=1)
    m = log_mark()
    d = opened()
    check(eeprom_writes(since(m)) == [] and chip()["pd"] == 0,
          "power-down bits in the DAC register are cleared by init's volatile write")
    d.close()

    # a measured curve instead of the linear map
    d = opened({"curve": CURVE})
    d.on()
    got = {}
    for pct in CURVE_CODES:
        d.set_intensity(pct)
        got[pct] = chip()["dac"]
    check(got == CURVE_CODES, f"curve {CURVE}: {got}")
    d.close()
    m = log_mark()
    bad = [{"curve": [[0, 1.0]]}, {"curve": [[0, 1.5], [100, 1.0]]}, {"curve": [[0, 0.2], [100, 2.0]]},
           {"curve": [[0, 0.6], [100, 3.6]]}, {"v_min": 2.5, "v_max": 2.5}, {"v_max": 3.4}]
    check(all(raises(ValueError, IRLed().init, dict(cfg, **b), {}) for b in bad) and since(m) == [],
          "bad curve / voltage window: ValueError before the bus is opened")

    # interpreter exit
    d = opened()
    d.on()
    _all_off_at_exit()
    check(chip()["dac"] == 0 and not d.is_on, "the atexit hook switches a lit LED off")
    d.on()
    set_flags(absent=True)
    check(raises(Exception, _all_off_at_exit) is None, "and never raises")
    set_flags()
    d.close()
    check(chip()["dac"] == 0, "driver part ends with the LED off")


# ── 2. config, templates, deploy list ──

def test_config():
    print("2. config, templates, deploy list")
    import yaml
    from devices.base import Device
    from shared.config import IR_LED_CFG_KEYS, ir_led_init_payload, rig_ir_led_device
    from shared.consolidate import _GROUP
    from shared.deploy_manifest import deploy_files

    check(ir_led_init_payload(None) == {"i2c_address": "0x60"} == ir_led_init_payload({}),
          "init payload of an empty block: just the default address")
    check(ir_led_init_payload({"type": "ir_led", "enabled": True, "i2c_address": "0x61", "i2c_bus": 1,
                               "intensity_pct": 35, "curve": None, "label": "x"})
          == {"i2c_address": "0x61", "i2c_bus": 1, "intensity_pct": 35},
          "init payload: only the driver's keys that are set")
    m = re.search(r"^_IR_LED_CFG_KEYS = (\(.*\))$", (ROOT / "pi_api" / "api.py").read_text(), re.M)
    check(bool(m) and ast.literal_eval(m.group(1)) == IR_LED_CFG_KEYS,
          "pi_api accepts exactly shared.config.IR_LED_CFG_KEYS")

    def rig(block, leader=("ir_led",), follower=()):
        return {"pis": [{"name": "l", "role": "leader", "devices": list(leader)},
                        {"name": "f", "role": "follower", "devices": list(follower)}],
                "devices": {"ir_led": block} if block is not None else {}}

    blk = {"type": "ir_led", "i2c_address": "0x60"}
    check(rig_ir_led_device(rig(blk)) is blk and rig_ir_led_device(rig(dict(blk, enabled=True))) is not None,
          "a rig has an IR LED: block present, enabled missing or true, on the leader")
    check(all(rig_ir_led_device(r) is None for r in (
        rig(dict(blk, enabled=False)), rig(blk, leader=()), rig(blk, leader=(), follower=("ir_led",)),
        rig(dict(blk, type="opto")), rig(None), {}, None)),
          "not when disabled, unassigned, on the follower, of another type, or absent")

    tpl = yaml.safe_load((ROOT / "rigs" / "_template.yaml").read_text())
    tblk = (tpl.get("devices") or {}).get("ir_led") or {}
    check(tblk.get("type") == "ir_led" and tblk.get("enabled") is False
          and not any("ir_led" in (p.get("devices") or []) for p in tpl.get("pis") or [])
          and rig_ir_led_device(tpl) is None,
          "rigs/_template.yaml: ir_led block present, disabled, given to no Pi")
    check(ir_led_init_payload(tblk) == {"i2c_address": "0x60", "i2c_bus": 1, "intensity_pct": 50},
          "its values: 0x60, I2C-1, 50 %")
    task = yaml.safe_load((ROOT / "experiments" / "_template.yaml").read_text())
    check("ir_led" not in (task.get("devices") or {}),
          "experiments/_template.yaml: the task override is a commented example only")

    shipped = {str(remote) for _, remote in deploy_files("leader")}
    need = ["devices/ir_led.py", "devices/base.py", "pi_api/api.py", "engine/leader.py",
            "shared/config.py", "shared/consolidate.py"]
    miss = [n for n in need if not any(s.endswith(n) for s in shipped)]
    check(not miss, "Deploy ships the driver and everything it hooks into" + (f" — missing {miss}" if miss else ""))
    check(Device.live_params() == {} and Device.safe_state({}) is None,
          "other devices: no live parameters, no safe state")
    check(all(_GROUP.get(n) == "ir_led" for n in SCALARS), "consolidate files the five scalars under /ir_led")


# ── 3. pi_api ──

def test_pi_api(api: PiApi):
    print("3. pi_api (a real pi_api/api.py on loopback)")
    payload = {"i2c_address": "0x60", "i2c_bus": 1, "intensity_pct": 50}

    def lit(body=None):
        return api.post("/api/monitor_ir_led", body)

    # before anything was initialized on this "Pi"
    set_chip(dac=1234, eeprom=0)
    check(api.start(), "pi_api started")
    check(chip()["dac"] == 1234 and "safe state" not in api.log(),
          "limit: a pi_api that never opened the LED has nothing remembered, its start-up leaves the DAC alone")
    set_chip(dac=0)
    m = log_mark()
    c1, r1 = api.post("/api/stop_monitor_ir_led")
    c2, r2 = lit()
    c3, r3 = api.post("/api/device_set", {"name": "ir_led", "param": "intensity_pct", "value": 10})
    check(c1 == 200 and r1.get("ok") and r1.get("on") is False and "not initialized" in r1.get("message", ""),
          "stop before init: ok, nothing was lit")
    check(c2 == 400 and "not initialized" in r2.get("error", "") and c3 == 400
          and "not initialized" in r3.get("error", "") and since(m) == [],
          "Live and the slider before init: 400 'not initialized', no bus traffic")

    # init, Live, slider, stop
    fresh_chip()
    c, r = api.post("/api/init_ir_led", payload)
    check(c == 200 and r.get("ok") and "EEPROM 0" in r.get("message", "") and chip()["dac"] == 0
          and chip()["eeprom"] == 0, "init: " + str(r.get("message") or r.get("error")))
    try:
        remembered = json.loads(api.safe_file.read_text())
    except (OSError, ValueError):
        remembered = None
    check(remembered == {"ir_led": {"type": "ir_led", "config": payload}},
          "~/rig/safe_state.json remembers the device and its config")
    c, r = lit()
    check(c == 200 and r.get("on") is True and r.get("dac_code") == 1861 and chip()["dac"] == 1861,
          "Live: LED on at the rig's 50 % (1861)")
    c, r = lit({"intensity_pct": 40})
    check(c == 200 and chip()["dac"] == 1613, "Live with an intensity: 40 % (1613)")
    c, r = api.post("/api/device_set", {"name": "ir_led", "param": "intensity_pct", "value": 70})
    check(c == 200 and r.get("state", {}).get("message") == "70 % = 1.90 V, LED ON" and chip()["dac"] == 2358,
          "slider while lit: " + str(r.get("state", {}).get("message")))
    c1, r1 = api.post("/api/device_set", {"name": "ir_led", "param": "v_max", "value": 3})
    c2, r2 = api.post("/api/device_set", {"name": "lamp", "param": "intensity_pct", "value": 3})
    check(c1 == 400 and "no live parameter 'v_max'" in r1.get("error", "") and c2 == 400 and chip()["dac"] == 2358,
          "slider: only the device's live parameters, only initialized devices")
    c, r = api.post("/api/stop_monitor_ir_led")
    check(c == 200 and r.get("on") is False and chip()["dac"] == 0, "Stop: LED off")
    c, r = api.post("/api/device_set", {"name": "ir_led", "param": "intensity_pct", "value": 30})
    check(c == 200 and "LED off" in r.get("state", {}).get("message", "") and chip()["dac"] == 0,
          "slider while off: stored, not lit")
    c, r = lit()
    check(c == 200 and chip()["dac"] == 1365, "the next Live uses it (30 % -> 1365)")

    # release (GO), preview without Connect, re-init while lit
    c, r = api.post("/api/release_devices")
    c3, r3 = api.post("/api/device_set", {"name": "ir_led", "param": "intensity_pct", "value": 10})
    check(c == 200 and chip()["dac"] == 0 and c3 == 400, "release_devices while lit: LED off, device gone")
    c, r = lit(payload)
    check(c == 200 and r.get("on") is True and chip()["dac"] == 1861,
          "Live with the rig config opens the device on demand and lights it")
    c, r = api.post("/api/init_ir_led", payload)
    check(c == 200 and chip()["dac"] == 0, "init while lit: LED off")

    # faults
    set_flags(absent=True)
    c, r = api.post("/api/init_ir_led", payload)
    c2, r2 = lit(payload)
    check(c == 500 and "MCP4725 at 0x60 on I2C-1" in r.get("error", "") and c2 == 500
          and "MCP4725 at 0x60" in r2.get("error", ""), "no board: init and Live answer 500, " + str(r.get("error")))
    set_flags()
    api.post("/api/init_ir_led", payload)
    set_flags(stuck=True)
    c, r = lit()
    check(c == 500 and "DAC read-back" in r.get("error", ""), "stuck DAC: Live answers 500, " + str(r.get("error")))
    set_flags()
    c, r = api.post("/api/stop_monitor_ir_led")
    check(c == 200 and chip()["dac"] == 0, "and Stop still works afterwards")

    # the controller's preview helper against this pi_api
    from controller.experiment import _ir_led_preview
    rig = {"pis": [{"name": "t-leader", "ip": "127.0.0.1", "role": "leader", "devices": ["ir_led"]}],
           "devices": {"ir_led": {"type": "ir_led", "enabled": True, "i2c_address": "0x60", "i2c_bus": 1,
                                  "intensity_pct": 60}}}
    rs = types.SimpleNamespace(config=rig, phase="connected", pis=rig["pis"], api_port=api.port)
    e1 = _ir_led_preview(rs, True)
    d1 = chip()["dac"]
    e2 = _ir_led_preview(rs, False)
    check(e1 is None and d1 == 2110 and e2 is None and chip()["dac"] == 0,
          "controller preview helper: on at the rig's 60 % (2110), off again")
    m = log_mark()
    rs.phase = "running"
    e1, e2 = _ir_led_preview(rs, True), _ir_led_preview(rs, False)
    rs.phase = "connected"
    plain = types.SimpleNamespace(config={"pis": rig["pis"], "devices": {}}, phase="connected",
                                  pis=rig["pis"], api_port=api.port)
    check(e1 is None and e2 is None and _ir_led_preview(plain, True) is None and since(m) == [],
          "it does nothing during a session or on a rig without the device")
    set_flags(absent=True)
    e1 = _ir_led_preview(rs, True)
    set_flags()
    check(isinstance(e1, str) and "Remote I/O" in e1, f"and returns the Pi's error when the LED fails: {e1}")
    api.post("/api/stop_monitor_ir_led")

    # pi_api stops / starts: the safe-state passes
    lit()
    check(chip()["dac"] == 2110, "(lit again at 60 %)")
    c, r = api.post("/api/restart")
    check(c == 200 and api.wait_exit(8) and chip()["dac"] == 0
          and "ir_led: safe state applied (pi_api stopping)" in api.log(),
          "Restart API (SIGTERM) while lit: LED off before pi_api goes")
    set_chip(dac=1861)
    check(api.start() and chip()["dac"] == 0 and "ir_led: safe state applied (pi_api start-up)" in api.log(),
          "pi_api start-up finds the DAC at 1861 (as after a warm reboot): LED off")
    lit(payload)
    api.proc.kill()
    api.wait_exit(5)
    time.sleep(0.3)
    check(chip()["dac"] == 1861, "limit: SIGKILL of pi_api itself leaves the LED lit ...")
    check(api.start() and chip()["dac"] == 0, "... until the next start")

    # an engine started by hand (not pi_api's child) is left alone
    fdir = T / "foreign" / "engine"
    fdir.mkdir(parents=True)
    (fdir / "leader.py").write_text("import time\ntime.sleep(120)\n")
    foreign = subprocess.Popen([PY, str(fdir / "leader.py")])
    try:
        lit(payload)
        time.sleep(0.3)
        api.stop()
        check(chip()["dac"] == 1861 and "pi_api stopping" not in api.log(),
              "with an engine/leader.py running that pi_api did not start, pi_api's SIGTERM pass is skipped")
        check(api.start() and chip()["dac"] == 1861 and "safe state" not in api.log(),
              "and so is the start-up pass")
    finally:
        foreign.kill()
        foreign.wait(timeout=5)
    c, r = api.post("/api/init_ir_led", payload)
    check(c == 200 and chip()["dac"] == 0, "pi_api part ends with the LED off")


# ── 4. the engine ──

def test_engine(api: PiApi):
    print("4. engine (real engine/leader.py sessions, started through pi_api's /api/start)")
    import h5py
    import yaml
    from analysis.loaders import Session
    from controller.events import handle_event

    payload = {"i2c_address": "0x60", "i2c_bus": 1, "intensity_pct": 50}
    edir = T / "engine"
    edir.mkdir()
    net = {k: free_port() for k in ("command_port", "event_port", "ack_port", "display_port")}
    rig_file = edir / f"{RIG}.yaml"
    rig_file.write_text(yaml.safe_dump({
        "name": RIG, "network": net,
        "pis": [{"name": "t-leader", "ip": "127.0.0.1", "role": "leader", "user": "vruser",
                 "devices": ["ir_led"]}],
        "devices": {"ir_led": dict(payload, type="ir_led", enabled=True)},
        "data": {"leader_dir": str(T / "data")}}))
    ev = Events(net["event_port"])
    cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def task_file(tag, n_trials, ir=None) -> Path:
        task = {"stimulus": {"size_deg": 8, "shape": "square", "duration_s": 0.3, "altitude_deg": 10,
                             "go_rule": "left", "background_gray": 0.5,
                             "contrast": {"values": [1.0], "proportions": [1]}},
                "reward": {"level": 1, "resp_delay_s": 0.05, "response_window": 0.1},
                "session": {"n_blocks": 1, "block_size": n_trials, "block_sequence": [80], "iti": 0.3}}
        if ir is not None:
            task["devices"] = {"ir_led": ir}
        p = edir / f"task_{tag}.yaml"
        p.write_text(yaml.safe_dump(task))
        return p

    def start(num, task, prep=True):
        """Engine up to 'Waiting for START' (or its exit). Returns its pid."""
        if prep:                              # what Connect and GO leave behind on the Pi
            api.post("/api/init_ir_led", payload)
            api.post("/api/release_devices")
        c, r = api.post("/api/start", {"script": "leader", "args": [
            "--rig", str(rig_file), "--task", str(task), "--subject", "T1", "--date", "20261118",
            "--session-num", str(num)]})
        wait_for(lambda: any("Waiting for START" in l for l in api.engine_log())
                 or not api.engine_running(), 40, 0.2)
        return r.get("pid")

    def go():
        cmd.sendto(json.dumps({"cmd": "START"}).encode(), ("127.0.0.1", net["command_port"]))

    def ended(timeout=60):
        return bool(wait_for(lambda: not api.engine_running(), timeout, 0.2))

    def h5_scalars(num) -> dict:
        sid = f"T1_20261118_{num:03d}"
        with h5py.File(T / "data" / "T1" / "T1_20261118" / sid / f"{sid}.h5", "r") as f:
            return {k: v[()].item() for k, v in f["ir_led"].items()}

    def ir_events(evs):
        return [e for e in evs if e.get("type") == "ir_led"]

    def notices(event) -> list:
        """What the controller sends to Slack for one event."""
        out = []
        rs = types.SimpleNamespace(name=RIG, session_id="T1_20261118_000", notify=out.append,
                                   broadcast=lambda e: None, trials=[], rt_hits_path=None,
                                   last_event_t=0.0)
        handle_event(rs, event)
        return out

    try:
        # A — a whole session, task override 80 %
        m_ev, m_bus = ev.mark(), log_mark()
        pid = start(1, task_file("a", 4, {"intensity_pct": 80}))
        check(bool(pid) and api.engine_running() and chip()["dac"] == 0,
              "engine initialized the LED and waits for START with it off")
        c1, r1 = api.post("/api/init_ir_led", payload)
        c2, r2 = api.post("/api/monitor_ir_led", payload)
        check(c1 == 409 and c2 == 409 and "engine owns the IR LED" in r1.get("error", "") and chip()["dac"] == 0,
              "pi_api refuses init and Live while its engine is alive (409)")
        go()
        on_ev = ev.wait(m_ev, lambda e: e.get("type") == "ir_led", 20) or {}
        check(on_ev.get("on") is True and on_ev.get("intensity_pct") == 80 and on_ev.get("dac_code") == 2606
              and wait_for(lambda: chip()["dac"] == 2606, 3),
              "START: LED on at the task's 80 % (2606), announced to the controller")
        c1, _ = api.post("/api/init_ir_led", payload)
        c2, _ = api.post("/api/monitor_ir_led", payload)
        c3, r3 = api.post("/api/stop_monitor_ir_led")
        c4, _ = api.post("/api/device_set", {"name": "ir_led", "param": "intensity_pct", "value": 5})
        check(c1 == 409 and c2 == 409 and c3 == 200 and c4 == 400 and chip()["dac"] == 2606 and api.engine_running(),
              "during the session no pi_api route changes the LED")
        check(ended(60), "the engine ends the session and exits by itself")
        evs = ev.since(m_ev)
        kinds = [e.get("type") for e in evs]
        ir = ir_events(evs)
        end = next((e for e in evs if e.get("type") == "session_end"), {})
        check(len(ir) == 2 and ir[0]["on"] is True and ir[1]["on"] is False and not any("error" in e for e in ir)
              and "experiment_start" in kinds and "session_end" in kinds
              and kinds.index("ir_led") < kinds.index("experiment_start")
              and len(kinds) - 1 - kinds[::-1].index("ir_led") < kinds.index("session_end"),
              "events: ir_led on before experiment_start, ir_led off before session_end")
        check(end.get("n_completed") == 4 and end.get("n_planned") == 4 and all(e.get("rig") == RIG for e in evs),
              "4 of 4 trials ran; every event carries the rig name")
        lit = [r for r in writes(since(m_bus)) if r["code"] != 0]
        check(bool(lit) and all(r["pid"] == pid and r["code"] == 2606 for r in lit) and chip()["dac"] == 0,
              "only the engine lit the LED, and it is off after the session")
        check(wait_for(lambda: "ir_led: safe state applied (engine exited with code 0)" in api.log(), 5),
              "pi_api's watcher applied the safe state when the engine exited")
        sc = h5_scalars(1)
        check(sorted(sc) == sorted(SCALARS) and sc["ir_led_intensity_pct"] == 80 and sc["ir_led_on"] == 1
              and abs(sc["ir_led_dac_volts"] - 2.1) < 0.005
              and abs(sc["ir_led_on_t"] - on_ev.get("t", 0)) < 0.01 and sc["ir_led_on_t"] < sc["ir_led_off_t"],
              "h5 /ir_led: 80 %, 2.10 V, on, on_t < off_t")
        s = Session(T / "data" / "T1" / "T1_20261118" / "T1_20261118_001" / "T1_20261118_001.h5")
        check(s.ir_led.get("intensity_pct") == 80 and s.ir_led.get("on") == 1
              and set(s.ir_led) == {"intensity_pct", "dac_volts", "on", "on_t", "off_t"}
              and len(s.trials) == 4 and not [c for c in s.trials.columns if "ir_led" in c],
              "analysis loader: Session.ir_led, and no ir_led column in the trials table")
        check(notices(ir[0]) == [] and notices(ir[1]) == [], "controller: on / off are log lines, no Slack")

        # B — the engine is SIGKILLed while the LED is lit
        long_task = task_file("b", 40)
        m_bus = log_mark()
        pid = start(2, long_task)
        go()
        check(wait_for(lambda: chip()["dac"] == 1861, 20), "rig default: LED on at 50 % (1861)")
        n_safe = api.log().count("safe state applied (engine exited")
        os.kill(pid, signal.SIGKILL)
        off = wait_for(lambda: chip()["dac"] == 0, 5)
        last = (writes(since(m_bus)) or [{}])[-1]
        check(off and last.get("pid") == api.proc.pid and last.get("code") == 0
              and wait_for(lambda: "safe state applied (engine exited with code -9)" in api.log(), 3)
              and api.log().count("safe state applied (engine exited") == n_safe + 1,
              "engine SIGKILLed mid-session: pi_api switches the LED off")
        c, r = api.post("/api/init_ir_led", payload)
        check(c == 200, "and gives the device back to Setup (init answers 200 again)")

        # B2 — Stop (pi_api /api/stop = SIGTERM) mid-session
        m_ev, m_bus = ev.mark(), log_mark()
        pid = start(3, long_task)
        go()
        wait_for(lambda: chip()["dac"] == 1861, 20)
        ev.wait(m_ev, lambda e: e.get("type") == "trial", 20)
        c, r = api.post("/api/stop", timeout=40)
        mine = [r["code"] for r in writes(since(m_bus), pid)]
        ir = ir_events(ev.since(m_ev))
        check(c == 200 and not api.engine_running() and chip()["dac"] == 0
              and 1861 in mine and set(mine[mine.index(1861) + 1:]) == {0}
              and len(ir) == 2 and ir[1]["on"] is False and "error" not in ir[1],
              "Stop mid-session: the engine itself switches the LED off and says so")

        # C — the board stops answering between engine start and START
        m_ev = ev.mark()
        n_fail = api.log().count("safe state FAILED")
        pid = start(4, task_file("c", 2))
        set_flags(absent=True)
        go()
        done = ended(60)
        wait_for(lambda: api.log().count("safe state FAILED") > n_fail, 5)
        failed_pass = api.log().count("safe state FAILED") > n_fail
        set_flags()
        evs = ev.since(m_ev)
        ir = ir_events(evs)
        end = next((e for e in evs if e.get("type") == "session_end"), {})
        check(done and end.get("n_completed") == 2 and end.get("n_planned") == 2,
              "board lost before START: the session still runs to its end (2 of 2)")
        check(len(ir) == 2 and ir[0]["on"] is False and "Remote I/O" in ir[0].get("error", "")
              and ir[1]["on"] is False and "error" not in ir[1],
              "one ir_led event with the error, then a plain 'off' at the end")
        check(any("WARNING: IR LED did not switch on" in l for l in api.engine_log()) and failed_pass,
              "engine log has the WARNING; pi_api logs that its safe state FAILED")
        sc = h5_scalars(4)
        check(sc["ir_led_on"] == 0 and math.isnan(sc["ir_led_on_t"]) and math.isnan(sc["ir_led_off_t"]),
              "h5 /ir_led: on 0, no on / off time")
        n1, n2 = (notices(ir[0]), notices(ir[1])) if len(ir) == 2 else ([], ["?"])
        check(len(n1) == 1 and "IR LED did not switch on" in n1[0] and "will be dark" in n1[0] and n2 == [],
              "controller: one Slack notice 'did not switch on ... video will be dark'")

        # C2 — the board stops answering mid-session, the LED stays lit
        m_ev = ev.mark()
        pid = start(5, task_file("c2", 3))
        go()
        wait_for(lambda: chip()["dac"] == 1861, 20)
        set_flags(absent=True)
        done = ended(60)
        time.sleep(0.6)                       # the watcher's pass (it fails too)
        still = chip()["dac"]
        set_flags()
        ir = ir_events(ev.since(m_ev))
        check(done and len(ir) == 2 and ir[1]["on"] is True and "Remote I/O" in ir[1].get("error", "")
              and still == 1861, "board lost mid-session: the stop event says 'still on' with the error")
        sc = h5_scalars(5)
        check(sc["ir_led_on"] == 1 and not math.isnan(sc["ir_led_on_t"]) and math.isnan(sc["ir_led_off_t"]),
              "h5 /ir_led: on 1, on_t set, no off time")
        n2 = notices(ir[1]) if len(ir) == 2 else []
        check(len(n2) == 1 and "IR LED did not switch off" in n2[0] and "may still be lit" in n2[0],
              "controller: one Slack notice 'did not switch off ... may still be lit'")
        c, r = api.post("/api/init_ir_led", payload)
        check(c == 200 and chip()["dac"] == 0, "the next Connect / Initialize switches it off")

        # D — a task that keeps the LED off
        m_ev, m_bus = ev.mark(), log_mark()
        pid = start(6, task_file("d", 2, {"on_during_session": False}))
        go()
        done = ended(60)
        ir = ir_events(ev.since(m_ev))
        check(done and not [r for r in writes(since(m_bus)) if r["code"] != 0]
              and bool(ir) and not any(e["on"] or "error" in e for e in ir) and h5_scalars(6)["ir_led_on"] == 0,
              "task `on_during_session: false`: never lit, no error, h5 on 0")

        # E — no board when the engine starts
        set_flags(absent=True)
        pid = start(7, task_file("e", 2), prep=False)
        done = ended(40)
        wait_for(lambda: len(re.findall(r"engine exited with code (-?\d+)", api.log())) >= 7, 5)
        codes = re.findall(r"engine exited with code (-?\d+)", api.log())
        set_flags()
        check(done and any("MCP4725 at 0x60 on I2C-1" in l for l in api.engine_log())
              and bool(codes) and codes[-1] not in ("0", "-9", "-15"),
              "no board at engine start: the engine exits with an error that names the chip "
              f"(exit code {codes[-1] if codes else '?'})")
    finally:
        if api.engine_running():
            api.post("/api/stop", timeout=40)
        set_flags()
        ev.close()
        cmd.close()
    c, r = api.post("/api/init_ir_led", payload)
    check(c == 200 and chip()["dac"] == 0, "engine part ends with the LED off")


# ── 5. the controller ──

def test_controller(api: PiApi):
    print("5. controller (a scratch controller/app.py; the leader is the pi_api above)")
    import yaml
    rdir = T / "rigs"
    rdir.mkdir()
    ui_port = free_port(socket.SOCK_STREAM)
    (T / "ctl.yaml").write_text(f"ui_port: {ui_port}\nevent_port: {free_port()}\n")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.2", api.port))
        cam_ip = "127.0.0.2"                  # a second loopback address: the camera's mock Pi
    except OSError:
        cam_ip = None                         # (macOS has only 127.0.0.1)
    pis = [{"name": "t-leader", "ip": "127.0.0.1", "role": "leader", "user": "vruser", "devices": ["ir_led"]}]
    devs = {"ir_led": {"type": "ir_led", "enabled": True, "i2c_address": "0x60", "i2c_bus": 1,
                       "intensity_pct": 60}}
    if cam_ip:
        pis.append({"name": "t-cam", "ip": cam_ip, "role": "follower", "user": "vruser", "devices": ["camera"]})
        devs["camera"] = {"type": "camera", "enabled": True}
    (rdir / f"{RIG}.yaml").write_text(yaml.safe_dump({
        "name": RIG, "network": {"api_port": api.port, "command_port": free_port(), "event_port": free_port()},
        "pis": pis, "devices": devs, "data": {"leader_dir": str(T / "data")}}))

    procs, logs = [], []

    def spawn(name, argv, **env):
        logs.append(open(T / f"{name}.log", "w"))
        e = dict(os.environ, PYTHONUNBUFFERED="1", **env)
        e.pop("VRFARM_SLACK_WEBHOOK", None)
        procs.append(subprocess.Popen(argv, cwd=str(ROOT), env=e, stdout=logs[-1], stderr=subprocess.STDOUT))

    base = f"http://127.0.0.1:{ui_port}"

    def rig(path, body=None, timeout=60):
        return http("POST", f"{base}/api/rigs/{RIG}{path}", body if body is not None else {}, timeout)

    def proxy(endpoint, payload=None, ip="127.0.0.1"):
        return rig("/setup/proxy", {"ip": ip, "endpoint": endpoint, "method": "POST",
                                    "payload": payload or {}, "timeout": 10})

    try:
        if cam_ip:
            spawn("mock_cam", [PY, "tools/mock_pi.py"], API_HOST=cam_ip, API_PORT=str(api.port),
                  CMD_PORT=str(free_port()), EVENT_PORT=str(free_port()), MOCK_RIG=RIG)
        spawn("controller", [PY, "controller/app.py", "--port", str(ui_port), "--no-browser",
                             "--rigs-dir", str(rdir)],
              VRFARM_SETTINGS=str(T / "ctl.yaml"), VRFARM_DATA_DIR=str(T / "ctl_data"))
        up = wait_http(base + "/api/rigs") and (not cam_ip or wait_http(f"http://{cam_ip}:{api.port}/api/status"))
        if not check(bool(up), "scratch controller" + (" and the camera's mock Pi" if cam_ip else "") + " started"):
            return
        c, r = http("GET", base + "/api/rigs")
        check(RIG in {x.get("name") for x in r.get("rigs", [])}, f"rig '{RIG}' listed")

        # Connect
        api.post("/api/release_devices")
        fresh_chip()
        c, r = rig("/load_rig")
        lead = (r.get("pi_results") or {}).get("t-leader") or {}
        check(c == 200 and r.get("all_pis_ok") is True and lead.get("ok") is True
              and chip()["dac"] == 0 and chip()["eeprom"] == 0,
              "Connect on a factory-new board: LED off, EEPROM 0, all Pis ok")
        set_flags(absent=True)
        c, r = rig("/load_rig")
        lead = (r.get("pi_results") or {}).get("t-leader") or {}
        set_flags()
        check(r.get("all_pis_ok") is False and "MCP4725 at 0x60" in str(lead.get("error")),
              "Connect without the board: the leader is flagged, " + str(lead.get("error")))

        # Setup tab: Initialize, Reinit, the card's Live / slider / Stop
        c, r = rig("/setup/init_devices")
        res = (r.get("results") or {}).get("ir_led") or {}
        check(c == 200 and res.get("ok") is True and "EEPROM 0" in str(res.get("message")),
              "Setup Initialize: " + str(res.get("message") or r))
        c, r = rig("/setup/reinit_device", {"device": "ir_led"})
        check(c == 200 and r.get("ok") is True and "EEPROM 0" in str(r.get("message")), "Setup Reinit")
        c, r = proxy("/api/monitor_ir_led", {"intensity_pct": 35})
        d1 = chip()["dac"]
        c2, r2 = proxy("/api/device_set", {"name": "ir_led", "param": "intensity_pct", "value": 90})
        d2 = chip()["dac"]
        c3, r3 = proxy("/api/stop_monitor_ir_led")
        check(r.get("ok") and d1 == 1489 and r2.get("ok") and d2 == 2854 and r3.get("ok") and chip()["dac"] == 0,
              "Setup card through the controller: Live 35 % (1489), slider 90 % (2854), Stop (0)")
        c, r = proxy("/api/monitor_ir_led", {"intensity_pct": 35}, ip="127.0.0.9")
        check(c == 403 and chip()["dac"] == 0, "the proxy refuses an address that is not a Pi of this rig")
        c, r = http("GET", base + "/api/device_schemas")
        sch = (r.get("schemas") or r).get("ir_led") or {}
        check(sch.get("label") == "IR Illuminator" and "intensity_pct" in (sch.get("live_params") or {}),
              "device catalog: ir_led with its live parameter")
        c1, t1 = http_text(f"{base}/setup/{RIG}")
        c2, t2 = http_text(f"{base}/experiment/{RIG}")
        check(c1 == 200 and "/api/monitor_ir_led" in t1 and "renderIrLedControls" in t1
              and c2 == 200 and "ir_led_error" in t2 and "case 'ir_led'" in t2,
              "the Setup and Experiment pages render with the IR LED code")

        # Experiment tab: the camera preview lights the LED
        if not cam_ip:
            print("  skip the camera preview checks (no second loopback address on this machine)")
            return
        c, r = rig("/camera_start")
        check(c == 200 and r.get("ok") and "ir_led_error" not in r and chip()["dac"] == 2110,
              "camera preview on: LED on at the rig's 60 % (2110)")
        c, r = rig("/camera_stop")
        check(c == 200 and r.get("ok") and "ir_led_error" not in r and chip()["dac"] == 0,
              "camera preview off: LED off")
        set_flags(absent=True)
        c, r = rig("/camera_start")
        set_flags()
        check(c == 200 and r.get("ok") and "Remote I/O" in str(r.get("ir_led_error")),
              "a failing LED does not block the preview; the answer carries ir_led_error")
        rig("/camera_stop")
        api.stop()
        api.start()
        c, r = rig("/camera_start")
        check(c == 200 and "ir_led_error" not in r and chip()["dac"] == 2110,
              "after a pi_api restart (device gone) the preview opens the LED again and lights it")
        c, r = rig("/camera_stop")
        check(chip()["dac"] == 0, "and switches it off")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in logs:
            f.close()


def main():
    global T, CHIP
    ap = argparse.ArgumentParser(description="The IR illuminator end to end against a fake MCP4725.")
    ap.add_argument("--quick", action="store_true", help="driver and config checks only (no processes)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder (logs, bus.log, h5)")
    args = ap.parse_args()

    T = Path(tempfile.mkdtemp(prefix="vrfarm_ir_led_")).resolve()
    CHIP = T / "chip"
    CHIP.mkdir()
    (T / "fake").mkdir()
    (T / "fake" / "smbus2.py").write_text(FAKE_SMBUS2)
    os.environ["FAKE_MCP4725_DIR"] = str(CHIP)
    sys.path.insert(0, str(T / "fake"))
    # Scratch settings before any controller import: never the real controller.yaml, never Slack
    (T / "controller.yaml").write_text(f"ui_port: {free_port(socket.SOCK_STREAM)}\nevent_port: {free_port()}\n")
    os.environ["VRFARM_SETTINGS"] = str(T / "controller.yaml")
    os.environ["VRFARM_DATA_DIR"] = str(T / "ctl_data")
    os.environ.pop("VRFARM_SLACK_WEBHOOK", None)
    set_flags()
    fresh_chip()
    rig_logs = ROOT / "logs" / RIG
    had_rig_logs = rig_logs.exists()
    print(f"scratch folder: {T}")

    api = None
    try:
        test_driver()
        test_config()
        if not args.quick:
            api = PiApi(free_port(socket.SOCK_STREAM))
            test_pi_api(api)
            test_engine(api)
            test_controller(api)
        # whoever wrote to the chip in this run, in whatever process
        w = writes(bus_log())
        bad = [r for r in w if r["cmd"] not in (0x40, 0x60) or (r["cmd"] == 0x60 and r["code"] != 0)]
        print("the whole run")
        check(bool(w) and not bad, f"{len(w)} DAC writes from {len({r['pid'] for r in w})} processes: "
                                   "the EEPROM was only ever written with 0, never a power-down mode")
    finally:
        if api is not None:
            api.stop()
        if not had_rig_logs and rig_logs.exists():
            shutil.rmtree(rig_logs, ignore_errors=True)        # the scratch controller's per-rig log
        if args.keep:
            print(f"kept: {T}")
        else:
            shutil.rmtree(T, ignore_errors=True)
    print("ALL PASSED" if not fails else f"FAILURES: {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
