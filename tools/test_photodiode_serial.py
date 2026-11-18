#!/usr/bin/env python3
"""tools/test_photodiode_serial.py — checks for the Teensy serial side of devices/photodiode.py, no
hardware: a fake lgpio, a fake pyserial and a small model of the sync firmware v2_3
(teensy/photodiode_sync_v2_3: "?" -> I line, "S <NAME> <value>" in a DEBUG build, D / E / P / B
lines). Covers the line parser, what the device reports about the firmware, sending parameters,
the Debug window's ring (one time axis for the Teensy's samples and this Pi's GPIO edges, across
a micros() rollover and a Teensy restart) and the reopening of a dropped port.

    conda activate vrfarm && python tools/test_photodiode_serial.py
"""
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


# ── fake lgpio (devices.photodiode imports it inside init()) ──
class _Cb:
    def __init__(self, owner, func):
        self.owner, self.func = owner, func
        owner.cbs.append(self)

    def cancel(self):
        if self in self.owner.cbs:
            self.owner.cbs.remove(self)


class FakeLgpio(types.ModuleType):
    BOTH_EDGES, RISING_EDGE, SET_PULL_DOWN = 3, 1, 32

    def __init__(self):
        super().__init__("lgpio")
        self.cbs = []

    def gpiochip_open(self, n):
        return 1

    def gpio_claim_alert(self, *a):
        pass

    def gpio_set_debounce_micros(self, *a):
        pass

    def callback(self, chip, gpio, edge, func):
        return _Cb(self, func)

    def gpio_read(self, chip, gpio):
        return 0

    def gpio_free(self, *a):
        pass

    def gpiochip_close(self, *a):
        pass

    def edge(self, wall_t, level=1):
        """A GPIO edge that happened at wall time wall_t, with the kernel's monotonic stamp."""
        tick = time.monotonic_ns() - int((time.time() - wall_t) * 1e9)
        for cb in list(self.cbs):
            cb.func(1, 0, level, tick)


lg = FakeLgpio()
sys.modules["lgpio"] = lg


# ── fake pyserial + firmware model ──
class Teensy:
    """What photodiode_sync_v2_3 does on its serial port, at wall-clock pace. `version=None` is a
    firmware before v2_3: no answer to anything, `legacy` picks what it streams."""

    def __init__(self, debug=True, version="v2_3", start_us=0, legacy=None):
        self.debug, self.version, self.legacy = debug, version, legacy
        self.params = {"MIN_PULSE_V": 0.30, "START_FRAC": 0.60, "END_FRAC": 0.30,
                       "STEADY_US": 150, "HOLDOFF_US": 30000}
        self.port = None
        self.cmd = b""
        self.boot(start_us)
        self.run = False
        self.th = None
        self.flashes = True
        self.announce = True           # E lines (off: the GPIO edges have nothing to pair with)
        self.accept_wall = []          # wall time of every accepted pulse (= the GPIO edge)

    def boot(self, start_us=0):
        self.t_boot = time.time()
        self.start_us = start_us
        self.t_sim = self.t_boot       # generated up to here
        self.next_flash = self.t_boot + 0.05
        self.next_b = self.t_boot + 1.0

    def micros(self, wall):
        return (int(round((wall - self.t_boot) * 1e6)) + self.start_us) & 0xFFFFFFFF

    def send(self, text):
        if self.port is not None:
            self.port.deliver(text.encode())

    def info(self):
        p = self.params
        return (f"I {self.version} DEBUG={int(self.debug)} MIN_PULSE_V={p['MIN_PULSE_V']:.3f} "
                f"START_FRAC={p['START_FRAC']:.3f} END_FRAC={p['END_FRAC']:.3f} "
                f"STEADY_US={p['STEADY_US']} HOLDOFF_US={p['HOLDOFF_US']}\n")

    def host_wrote(self, data):
        if self.version is None:
            return
        self.cmd += data
        while b"\n" in self.cmd:
            line, self.cmd = self.cmd.split(b"\n", 1)
            parts = line.decode().split()
            if parts == ["?"]:
                self.send(self.info())
            elif parts and parts[0] == "S":
                if not self.debug:
                    self.send("X S needs a DEBUG build\n")
                elif len(parts) != 3:
                    self.send("X usage: S <NAME> <value>\n")
                elif parts[1] not in self.params:
                    self.send(f"X unknown name {parts[1]}\n")
                else:
                    kind = type(self.params[parts[1]])
                    self.params[parts[1]] = kind(float(parts[2]))
                    self.send(self.info())

    # the streaming part: 87 ms flash period, 1.2 ms pulses of 1.0 V on a 0.02 V floor
    def start(self):
        self.run = True
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()

    def stop(self):
        self.run = False
        if self.th:
            self.th.join()

    def _loop(self):
        while self.run:
            time.sleep(0.005)
            self.step(time.time())

    def step(self, now):
        out = []
        t = self.t_sim
        while not self.flashes and self.next_flash < t:       # dark: no flashes to catch up on later
            self.next_flash += 0.087
        while t < now:
            in_flash = self.flashes and self.next_flash <= t < self.next_flash + 0.0012
            v = 1.0 if in_flash else 0.02
            if self.version is None:
                if self.legacy == "debug":
                    out.append(f"{v:.3f} 0.600 0.300 0\n")
            elif self.debug:
                out.append(f"D {self.micros(t)} {v:.3f} 0.600 0.300\n")
            if self.flashes and t >= self.next_flash + 0.0012:
                acc = self.next_flash + 0.00015          # steady for STEADY_US, then OUT goes high
                if self.version is not None:
                    if self.debug and self.announce:
                        out.append(f"E {self.micros(acc)}\n")
                    out.append("P 0.980 1.000 0.020 1200\n")
                self.accept_wall.append(acc)
                lg.edge(acc + 0.00003)                   # the Pi sees the edge 30 us later
                self.next_flash += 0.087
            if t >= self.next_b:
                self.next_b += 1.0
                if self.version is not None or self.legacy == "prod":
                    out.append("B 0.020 0.021\n")
            t += 0.0002
        self.t_sim = t
        if out:
            self.send("".join(out))


class FakeSerial:
    teensy = None            # what the next opened port talks to
    fail_open = False
    opened = []

    def __init__(self, port, baud, timeout=None, write_timeout=None):
        if FakeSerial.fail_open:
            raise OSError(2, f"could not open port {port}")
        self.timeout = timeout
        self.rx = bytearray()
        self.cv = threading.Condition()
        self.dead = False
        self.closed = False
        self.written = b""
        FakeSerial.opened.append(self)
        if FakeSerial.teensy is not None:
            FakeSerial.teensy.port = self

    def deliver(self, data):
        with self.cv:
            self.rx += data
            self.cv.notify_all()

    @property
    def in_waiting(self):
        if self.dead:
            raise OSError(5, "Input/output error")
        return len(self.rx)

    def read(self, n):
        with self.cv:
            if not self.rx and not self.dead:
                self.cv.wait(self.timeout)
            if self.dead:
                raise OSError(5, "Input/output error")
            data = bytes(self.rx[:n])
            del self.rx[:n]
            return data

    def write(self, data):
        self.written += data
        if FakeSerial.teensy is not None and FakeSerial.teensy.port is self:
            FakeSerial.teensy.host_wrote(data)

    def cancel_read(self):
        with self.cv:
            self.cv.notify_all()

    def close(self):
        self.closed = True
        self.dead = True
        self.cancel_read()


serial_mod = types.ModuleType("serial")
serial_mod.Serial = FakeSerial
sys.modules["serial"] = serial_mod

from devices import photodiode as pd_mod                      # noqa: E402
from devices.photodiode import Photodiode, TEENSY_PARAMS      # noqa: E402


def device(teensy, **cfg):
    FakeSerial.teensy = teensy
    d = Photodiode()
    d.init({"gpio": 24, "serial_port": "/dev/ttyFAKE", **cfg}, {})
    return d


# ── 1. the sketches and the device agree on the tunables ──
print("1. sketches")
import re                                                     # noqa: E402
sketches = sorted(ROOT.glob("teensy/**/photodiode_sync_v2_3.ino"))
check(len(sketches) >= 1, f"{len(sketches)} photodiode_sync_v2_3 sketch(es) in teensy/")
for sk in sketches:
    text = sk.read_text()
    found = {n for n in TEENSY_PARAMS
             if re.search(rf"^\s*const\s+(float|uint32_t)\s+{n}\s*=\s*[^;]+;", text, re.M)}
    check(found == set(TEENSY_PARAMS) and re.search(r"^#define\s+DEBUG\s+0\b", text, re.M)
          and '#define FW_VERSION "v2_3"' in text,
          f"{sk.relative_to(ROOT)}: the five tunables, DEBUG 0, FW_VERSION v2_3")

# ── 2. production build: I line, P / B telemetry, S refused ──
print("2. production build")
t = Teensy(debug=False)
d = device(t)
check(wait_for(lambda: d.fw_info()["known"]), "the device asks '?' when the port opens and gets the I line")
fw = d.fw_info()
check(fw["version"] == "v2_3" and fw["debug"] is False and fw["serial_open"],
      f"fw: version {fw.get('version')}, debug {fw.get('debug')}")
check(fw["params"] == t.params and isinstance(fw["params"]["STEADY_US"], int),
      f"fw params as the sketch has them: {fw['params']}")
t.send("P 0.612 0.655 0.012 1180\nB 0.011 0.014\n")
check(wait_for(lambda: d.get_v_status().get("last_baseline") is not None), "P and B lines arrive")
st = d.get_v_status()
check(st["available"] and st["last_pulse"]["median_V"] == 0.612 and st["last_pulse"]["width_us"] == 1180
      and st["last_baseline"]["floor_V"] == 0.011 and st["v"] == 0.612,
      "get_v_status: last pulse, baseline, latest V")
check(st["fw"]["known"] and st["fw"]["debug"] is False, "get_v_status carries the firmware info")
r = d.teensy_set({"MIN_PULSE_V": 0.2})
check(not r["ok"] and "production build" in r["error"], f"teensy_set refused: {r['error']}")
check(b"S " not in FakeSerial.opened[-1].written, "nothing was sent to a production build")
d._serial_write(b"S MIN_PULSE_V 0.2\n")                         # what the Teensy itself answers
check(wait_for(lambda: len(d._x_lines) == 1) and d._x_lines[0][1] == "S needs a DEBUG build",
      "an X line (refused command) is kept")
d.debug_start()
t.send("D 1000 0.5 0.6 0.3\n")
time.sleep(0.1)
r = d.debug_read()
check(r["ok"] and r["fw"]["debug"] is False and r["t"] == [0.0] and r["on"],
      "debug_read on a production build: on, firmware says no DEBUG")
d.close()
check(FakeSerial.opened[-1].closed and not d._serial_thread.is_alive() and not lg.cbs,
      "close(): port closed, reader thread gone, no GPIO callback left")

# ── 3. parser details ──
print("3. parser")
t = Teensy(debug=True)
d = device(t)
wait_for(lambda: d.fw_info()["known"])
now = time.time()
d._parse_lines([b"D 100 0.5 0.6 0.3", b"E 150", b"garbage", b"", b"P 1 2", b"I v2_3", b"D x y",
                b"B 0.01", b"7.5", b"D 300 0.5 0.6 0.3\r"], now)
check(d._dbg_n == 0 and d._dbg_acc_n == 0, "D / E lines are not stored while the Debug window is off")
check(d.fw_info()["stream"], "… but a D stream is noticed")
check(d._last_pulse is None and d._last_baseline is None and len(d._v) == 0,
      "short / torn lines and an out-of-range bare number change nothing")
d._parse_lines([b"0.42"], now)
check(len(d._v) == 1 and d._v[-1][1] == 0.42, "a bare number is a legacy V sample")
d._parse_lines([b"I v2_3 DEBUG=1 MIN_PULSE_V=0.123 START_FRAC=0.700 END_FRAC=0.200 STEADY_US=99 "
                b"HOLDOFF_US=1234"], now)
fw = d.fw_info()
check(fw["params"] == {"MIN_PULSE_V": 0.123, "START_FRAC": 0.7, "END_FRAC": 0.2, "STEADY_US": 99,
                       "HOLDOFF_US": 1234} and fw["debug"], f"I line parsed: {fw['params']}")
d.close()

# ── 4. sending parameters to a DEBUG build ──
print("4. teensy_set")
t = Teensy(debug=True)
d = device(t)
wait_for(lambda: d.fw_info()["known"])
r = d.teensy_set({"MIN_PULSE_V": 0.25, "STEADY_US": 200})
check(r["ok"] and r["fw"]["params"]["MIN_PULSE_V"] == 0.25 and r["fw"]["params"]["STEADY_US"] == 200,
      f"two values sent and confirmed: {r.get('sent')}")
check(t.params["MIN_PULSE_V"] == 0.25 and t.params["STEADY_US"] == 200, "the Teensy runs them")
check(b"S MIN_PULSE_V 0.250\n" in FakeSerial.opened[-1].written
      and b"S STEADY_US 200\n" in FakeSerial.opened[-1].written, "wire format: S <NAME> <value>")
r = d.teensy_set({"START_FRAC": "0.5", "END_FRAC": 0.2, "HOLDOFF_US": 40000.0})
check(r["ok"] and t.params["HOLDOFF_US"] == 40000 and t.params["START_FRAC"] == 0.5,
      "numbers given as text / float are taken")
before = dict(t.params)
for bad, why in (({"END_FRAC": 0.5}, "END_FRAC must be below START_FRAC"),
                 ({"END_FRAC": 0.9, "START_FRAC": 0.8}, "END_FRAC must be below START_FRAC"),
                 ({"MIN_PULSE_V": 4}, "outside"), ({"STEADY_US": -1}, "outside"),
                 ({"START_FRAC": 0}, "outside"), ({"OUT_PULSE_US": 100}, "not a tunable"),
                 ({"MIN_PULSE_V": "abc"}, "not a number"), ({}, "no parameters")):
    r = d.teensy_set(bad)
    check(not r["ok"] and why in r["error"] and t.params == before, f"refused {bad}: {r['error']}")
d.close()

# ── 5. the Debug window's ring, at wall-clock pace across a micros() rollover ──
print("5. debug ring")
t = Teensy(debug=True, start_us=(1 << 32) - 400_000)      # micros() rolls over 0.4 s in
d = device(t)
wait_for(lambda: d.fw_info()["known"])
t.boot(t.start_us)
t.start()
time.sleep(0.3)                                           # stream runs, window closed
check(d._dbg_n == 0, "nothing is stored before debug_start")
fw = d.debug_start()
check(fw["known"] and fw["debug"], "debug_start returns the firmware info")
T, V, E, A, syncs = [], [], [], [], []
cur = {"since": 0, "edges_since": 0, "accepts_since": 0, "epoch": None}
epochs = set()
t_end = time.time() + 1.6
while time.time() < t_end:
    time.sleep(0.05)
    r = d.debug_read(**cur)
    cur = {"since": r["seq"], "edges_since": r["eseq"], "accepts_since": r["aseq"], "epoch": r["epoch"]}
    epochs.add(r["epoch"])
    T += r["t"]; V += r["v"]; A += r["accepts"]                # noqa: E702
    E += [(e, r["sync"]) for e in r["edges"]]
    syncs.append(r["sync"])
rate = len(T) / (T[-1] - T[0])
check(len(epochs) == 1, "one epoch: the rollover of micros() did not clear the ring")
check(4500 < rate < 5500 and abs(T[0]) < 1e-9, f"samples: {len(T)} in {T[-1] - T[0]:.2f} s ({rate:.0f}/s), axis starts at 0")
check(all(b > a for a, b in zip(T, T[1:])) and max(b - a for a, b in zip(T, T[1:])) < 0.0003,
      "time axis strictly increasing and gap-free across the rollover (no sample twice, none lost)")
check(15 <= len(A) <= 20 and len(A) - 1 <= len(E) <= len(A) and len(set(e for e, _ in E)) == len(E),
      f"{len(A)} accepted pulses (E lines), {len(E)} GPIO edges, each once (an edge is handed out 60 ms late)")
hi = [sum(1 for tt, vv in zip(T, V) if a <= tt < a + 0.0008 and vv > 0.9) for a in A]
check(min(hi) >= 3, "the V trace is high right after every accepted pulse (same axis)")
fine = [e - min(A, key=lambda a: abs(a - e)) for e, how in E if how == "gpio"]
coarse = [e - min(A, key=lambda a: abs(a - e)) for e, how in E if how == "serial"]
check(len(fine) >= len(E) - 3 and all(abs(x) < 20e-6 for x in fine),
      f"GPIO edges on the Teensy axis: {len(fine)} placed from matched pulses, each on its accept "
      f"within 20 us (worst {max(abs(x) for x in fine) * 1e6:.0f} us)")
check(all(abs(x) < 0.004 for x in coarse),
      f"{len(coarse)} placed from serial arrival times before the first match, within 4 ms")
check(syncs[-1] == "gpio" and r["accepts_1s"] in (11, 12) and r["edges_1s"] in (11, 12),
      f"rates: {r['accepts_1s']} accepts and {r['edges_1s']} GPIO edges in the last second")
r2 = d.debug_read(**cur)
check(len(r2["t"]) < 400, "a second read returns only what is new")
r3 = d.debug_read()
check(len(r3["t"]) > 5000 and r3["t"][0] < 0.5 and len(r3["accepts"]) >= 15,
      "a read without cursors returns the ring from its start")
r4 = d.debug_read(since=10 ** 9, epoch=r["epoch"] + 77)
check(len(r4["t"]) > 5000, "a stale epoch resets the cursors")

# no flashes: no edges; an edge with no E line near it is still drawn
t.flashes = False
time.sleep(0.2)
r = d.debug_read(**cur)
cur = {"since": r["seq"], "edges_since": r["eseq"], "accepts_since": r["aseq"], "epoch": r["epoch"]}
lg.edge(time.time() - 0.02)
lg.edge(time.time() - 0.01, level=0)                      # falling edge: ignored
time.sleep(0.1)
r = d.debug_read(**cur)
check(len(r["edges"]) == 1 and not r["accepts"], "a GPIO edge the Teensy did not announce is drawn; falling edges are not")
check(abs((r["t"][-1] - r["edges"][0]) - 0.12) < 0.03, "… at the right place on the axis")

# no E lines to pair with: the edges are placed from the serial arrival times alone
t.flashes, t.announce = True, False
d.debug_start()
time.sleep(0.7)
r = d.debug_read()
rises = [tt for (tt, vv), v0 in zip(list(zip(r["t"], r["v"]))[1:], r["v"]) if vv > 0.9 and v0 < 0.5]
err = [e - min(rises, key=lambda x: abs(x - e)) for e in r["edges"]]
check(r["sync"] == "serial" and len(err) >= 5 and not r["accepts"],
      f"without E lines: {len(err)} edges placed from serial arrival times alone")
check(all(-0.002 < x < 0.0006 for x in err),
      f"… each within -2 .. +0.6 ms of the rise in the V trace (worst {max(err, key=abs) * 1e3:+.2f} ms)")
t.flashes, t.announce = False, True
time.sleep(0.15)
r = d.debug_read()

# Teensy restart: its clock starts again -> new epoch, axis from 0
ep = r["epoch"]
t.stop()
t.boot(0)
t.flashes = True
t.start()
time.sleep(0.4)
r = d.debug_read(since=r["seq"], edges_since=r["eseq"], accepts_since=r["aseq"], epoch=ep)
check(r["epoch"] != ep and r["t"] and r["t"][0] == 0.0 and r["t"][-1] < 0.6,
      "a Teensy restart (time runs backwards) starts a new epoch with a fresh axis")

# port drop (a firmware upload): reopened by the reader thread
port = FakeSerial.opened[-1]
n_open = len(FakeSerial.opened)
FakeSerial.fail_open = True
port.dead = True
port.cancel_read()
check(wait_for(lambda: d._serial is None and port.closed), "a dropped port is closed at once")
st = d.get_v_status()
check(st["available"] and not st["fw"]["serial_open"] and st["fw"]["known"] is False,
      "… the telemetry stays 'available' (stale), firmware unknown")
r = d.teensy_set({"MIN_PULSE_V": 0.2})
check(not r["ok"] and "not open" in r["error"], f"teensy_set while the port is away: {r['error']}")
t.debug = False                                           # the upload brought the production build
FakeSerial.fail_open = False
check(wait_for(lambda: d.fw_info()["known"], 4.0) and len(FakeSerial.opened) == n_open + 1,
      "the port is reopened and the firmware asked again")
check(d.fw_info()["debug"] is False, "the device now reports the production build")
r = d.debug_read()
check(r["epoch"] != ep and r["fw"]["debug"] is False, "the ring was cleared at the reconnect")

# the Debug window went away without debug_stop
old_idle = pd_mod.DEBUG_IDLE_S
pd_mod.DEBUG_IDLE_S = 0.3
check(wait_for(lambda: not d._dbg_on and not lg.cbs, 4.0), "the capture stops by itself when nobody reads it")
pd_mod.DEBUG_IDLE_S = old_idle
t.stop()
d.close()

# ── 6. firmware before v2_3, and no port at all ──
print("6. older firmware / no port")
t = Teensy(version=None, legacy="prod")
d = device(t)
t.start()
check(wait_for(lambda: d.fw_info()["version"] is not None, 3.0), "B lines without an I line are recognised")
fw = d.fw_info()
check(fw["known"] is False and fw["debug"] is False and fw["version"] == "older than v2_3",
      f"guess: {fw['version']}, production")
r = d.teensy_set({"MIN_PULSE_V": 0.2})
check(not r["ok"] and "older than v2_3" in r["error"], f"teensy_set: {r['error']}")
check(FakeSerial.opened[-1].written.count(b"?") <= 4, "'?' is asked a few times, not forever")
t.stop()
d.close()

t = Teensy(version=None, legacy="debug")
d = device(t)
t.start()
check(wait_for(lambda: d.fw_info()["debug"] is True, 3.0) and d.fw_info()["known"] is False,
      "a bare-number stream is recognised as an old DEBUG build")
check(d.get_v_status()["n"] > 0, "… and still feeds the V series")
t.stop()
d.close()

FakeSerial.fail_open = True
d = device(Teensy())
st = d.get_v_status()
check(st == {"available": False}, "a port that never opened: telemetry not available")
check(d._serial_thread is not None and d._serial_thread.is_alive(), "… but the reader keeps trying")
FakeSerial.fail_open = False
check(wait_for(lambda: d.get_v_status()["available"], 4.0), "… and takes the port once it is there")
d.close()

d = Photodiode()
d.init({"gpio": 24}, {})
check(d.get_v_status() == {"available": False} and d._serial_thread is None
      and d.fw_info()["serial_open"] is False, "no serial_port in the rig: no reader, no telemetry")
check(d.debug_start()["known"] is False and d.debug_read()["t"] == [], "debug_start / debug_read still answer")
d.close()

print("\nFAILED: %d" % fails if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
