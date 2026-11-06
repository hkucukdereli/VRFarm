#!/usr/bin/env python3
"""tools/test_opto.py — checks for the optogenetics stack with NO hardware: devices/opto.py against
a fake lgpio (records every gpio_write with a timestamp; tx_pulse is forbidden), the Deploy-time
trial assignment in shared/stim_generator.py, shared/config.validate_opto, and the HDF5 round trip
through shared/consolidate.py and analysis/loaders.py.

    conda activate vrfarm && python tools/test_opto.py
"""
import math
import statistics
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


# ── fake lgpio, injected before devices.opto is imported (it imports lgpio inside init()) ──

class FakeLgpio(types.ModuleType):
    RISING_EDGE = 1

    def __init__(self):
        super().__init__("lgpio")
        self.writes = []        # (t, gpio, level)
        self.levels = {}
        self.claimed = set()

    def gpiochip_open(self, n):
        return 100 + int(n)

    def gpiochip_close(self, h):
        pass

    def gpio_claim_output(self, h, gpio, level=0, lflags=0):
        self.claimed.add(gpio)
        self.levels[gpio] = level

    def gpio_read(self, h, gpio):
        return self.levels.get(gpio, 0)

    def gpio_write(self, h, gpio, level):
        self.levels[gpio] = level
        self.writes.append((time.time(), gpio, level))

    def gpio_free(self, h, gpio):
        self.claimed.discard(gpio)

    def tx_pulse(self, *a, **k):
        raise AssertionError("tx_pulse must never be called (phase-locked PWM engine)")

    tx_pwm = tx_pulse


fake = FakeLgpio()
sys.modules["lgpio"] = fake

from devices.opto import (OptoStim, pattern_edges, opto_spec_errors, parse_windows,   # noqa: E402
                          SYNC_MIN_LATENCY_S, WINDOW_ANCHORS)
from shared.stim_generator import (build_block_trial_list, generate_stimuli,          # noqa: E402
                                   is_go_trial)
from shared.config import validate_opto, rig_opto_device, opto_init_payload           # noqa: E402

RIG_PINS = {"laser": {"gpio": 17}, "mask": {"gpio": 27}}
RIG_DEV = {"type": "opto", "enabled": True, "pins": RIG_PINS, "gpiochip": 0, "max_on_s": 5.0,
           "show_latency_s": 0.09}
RIG = {"devices": {"opto": RIG_DEV}, "pis": [{"role": "leader", "devices": ["opto"]}]}
LASER, MASK = 17, 27


def task_opto(**kw):
    o = {"enabled": True, "fraction": 0.3, "eligible": "all", "min_gap_trials": 0,
         "windows": {"stim_onset": {"enabled": True, "onset_s": 0.0, "duration_s": 0.2}},
         "pattern": {"mode": "continuous", "pulse_hz": 20, "pulse_width_ms": 5},
         "mask": True, "power_mw": 5, "wavelength_nm": 473}
    o.update(kw)
    return o


def make_dev(opto=None, pins=RIG_PINS, max_on_s=5.0):
    fake.writes.clear()
    dev = OptoStim()
    dev.init({"pins": pins, "gpiochip": 0, "max_on_s": max_on_s, "show_latency_s": 0.09},
             opto if opto is not None else task_opto())
    return dev


def writes(gpio, level=None, since=0.0):
    return [(t, g, l) for (t, g, l) in fake.writes if g == gpio and t >= since and (level is None or l == level)]


def ms(a, b):
    return (b - a) * 1000.0


# ── 1. pattern math ──
print("pattern_edges")
e = pattern_edges({"mode": "continuous"}, 100.0, 1.5)
check(e == [(100.0, 1), (101.5, 0)], "continuous = one ON and one OFF edge")
e = pattern_edges({"mode": "pulsed", "pulse_hz": 20, "pulse_width_ms": 5}, 100.0, 1.0)
check(len(e) == 40 and e[0] == (100.0, 1) and abs(e[1][0] - 100.005) < 1e-9
      and abs(e[38][0] - 100.95) < 1e-9, "pulsed 20 Hz x 5 ms x 1 s = 20 pulses on a fixed grid")

# ── 2. spec validation ──
print("opto_spec_errors / validate_opto")
check(opto_spec_errors(task_opto(), RIG_PINS, 5.0) == [], "good spec passes")
check(any("max_on_s" in m for m in opto_spec_errors(
    task_opto(windows={"stim_onset": {"enabled": True, "onset_s": 0, "duration_s": 6}}), RIG_PINS, 5.0)),
      "duration above the rig cap is refused")
check(any("pulse_width_ms" in m for m in opto_spec_errors(
    task_opto(pattern={"mode": "pulsed", "pulse_hz": 20, "pulse_width_ms": 60}), RIG_PINS, 5.0)),
      "pulse wider than the period is refused")
check(any("pulse_width_ms" in m for m in opto_spec_errors(
    task_opto(pattern={"mode": "pulsed", "pulse_hz": 20, "pulse_width_ms": 1}), RIG_PINS, 5.0)),
      "pulse below the 2 ms floor is refused")
check(any("pulse_hz" in m for m in opto_spec_errors(
    task_opto(pattern={"mode": "pulsed", "pulse_hz": 200, "pulse_width_ms": 2}), RIG_PINS, 5.0)),
      "pulse_hz above 100 is refused")
check(any("negative onset" in m for m in opto_spec_errors(
    task_opto(windows={"reward": {"enabled": True, "onset_s": -0.1, "duration_s": 0.5}}), RIG_PINS, 5.0)),
      "a negative onset on a non-stimulus anchor is refused")
check(opto_spec_errors(task_opto(windows={"stim_onset": {"enabled": True, "onset_s": -0.5, "duration_s": 1}}),
                       RIG_PINS, 5.0) == [], "a 0.5 s stimulus lead is fine")
check(any("capped" in m for m in opto_spec_errors(
    task_opto(windows={"stim_onset": {"enabled": True, "onset_s": -2, "duration_s": 1}}), RIG_PINS, 5.0)),
      "a 2 s lead is refused")
check(any("mask pin" in m for m in opto_spec_errors(task_opto(), {"laser": {"gpio": 17}}, 5.0)),
      "mask requested without a mask pin is refused")
check(any("no window" in m for m in opto_spec_errors(task_opto(windows={}), RIG_PINS, 5.0)),
      "no enabled window is refused")
check(len(parse_windows(task_opto(windows={a: {"enabled": True, "onset_s": 0, "duration_s": 1}
                                            for a in WINDOW_ANCHORS}))) == 5, "all five anchors parse")

task = {"stimulus": {"go_rule": "left", "photodiode_sync_enabled": True},
        "reward": {"resp_delay_s": 0.6}, "opto": task_opto()}
errs, warns = validate_opto(task, RIG)
check(errs == [] and warns == [], f"validate_opto: good task on a good rig -> no errors/warnings {errs} {warns}")
check(validate_opto({"opto": {"enabled": False}}, None) == ([], []), "disabled opto validates trivially")
check(validate_opto(task, {"devices": {}, "pis": []})[0], "rig without the device -> error")
rig_off = {"devices": {"opto": dict(RIG_DEV, enabled=False)}, "pis": RIG["pis"]}
check(validate_opto(task, rig_off)[0], "disabled device -> error")
rig_unassigned = {"devices": {"opto": RIG_DEV}, "pis": [{"role": "leader", "devices": []}]}
check(validate_opto(task, rig_unassigned)[0], "device not on the leader -> error")
check(rig_opto_device(RIG) is RIG_DEV and rig_opto_device(rig_off) is None
      and rig_opto_device(rig_unassigned) is None, "rig_opto_device mirrors the engine's test")
t2 = dict(task, opto=task_opto(eligible="nogo"), stimulus={"go_rule": "all", "photodiode_sync_enabled": True})
check(any("nogo" in m for m in validate_opto(t2, RIG)[0]), "nogo with go_rule all -> error")
t3 = dict(task, stimulus={"go_rule": "left", "photodiode_sync_enabled": False})
check(any("sync" in m for m in validate_opto(t3, RIG)[1]), "sync off -> warning")
t4 = dict(task, opto=task_opto(windows={"stim_onset": {"enabled": True, "onset_s": 0, "duration_s": 1.5},
                                        "response_window": {"enabled": True, "onset_s": 0, "duration_s": 1}}))
check(any("overlap" in m for m in validate_opto(t4, RIG)[1]), "stim/response-window overlap -> warning")
check(any("fraction" in m for m in validate_opto(dict(task, opto=task_opto(fraction=1.5)), RIG)[0]),
      "fraction > 1 -> error")
check(opto_init_payload(RIG_DEV) == {"pins": RIG_PINS, "gpiochip": 0, "max_on_s": 5.0},
      "opto_init_payload carries pins / gpiochip / cap")

# ── 3. device timing against the fake lgpio ──
print("device: init / immediate window / OFF edge")
dev = make_dev()
check(fake.levels.get(LASER) == 0 and fake.levels.get(MASK) == 0 and LASER in fake.claimed,
      "init claims both pins output-LOW")
check(dev.check()["ok"] and "laser GPIO17" in dev.check()["message"], "check() reports the pins")
check(dev.active and dev.windows[0]["anchor"] == "stim_onset", "active with the stim window")
evts = []
dev.start_stream(evts.append)
dev.arm_trial(3, opto_on=True)
dev.fire_at("iti_start", time.time())            # no iti window: nothing
check(not fake.writes, "an anchor without a window writes nothing")
t0 = time.time()
dev.fire_at("trial_start", t0)
dev.arm_edge(t0, t0 + 0.12)
dev.on_sync_edge(t0 + 0.01)                       # too early: a heartbeat sub-pulse, not the stim
check(not writes(LASER, 1), "an edge < 45 ms after SHOW is ignored")
t_edge = time.time()
dev.on_sync_edge(t_edge)                          # (fake: since_t + 45 ms already elapsed? no) …
time.sleep(0.06)
dev.on_sync_edge(time.time())                     # now a valid edge (> 45 ms after since_t)
t_on = time.time()
on = writes(LASER, 1)
check(len(on) == 1 and ms(t_on, on[0][0]) > -2, f"the valid edge writes the laser ON on the caller's thread ({ms(on[0][0], t_on):.2f} ms before return)" if on else "laser ON missing")
check(len(writes(MASK, 1)) == 1, "the mask fires with it")
time.sleep(0.35)
off = writes(LASER, 0, since=t_on - 0.01)
check(len(off) == 1 and abs(ms(on[0][0], off[0][0]) - 200) < 6,
      f"OFF edge at +200 ms (measured {ms(on[0][0], off[0][0]) if off else float('nan'):.1f} ms)")
check(len(writes(LASER, 1)) == 1, "the fallback did not fire a second time after the edge")
rec = dev.finish_trial()
check(rec["opto_on"] == 1 and rec["opto_mask_on"] == 1 and rec["opto_n_windows"] == 1
      and rec["opto_window_anchor"] == "stim_onset:edge" and abs(rec["opto_onset_t"] - on[0][0]) < 1e-6
      and len(rec["opto_window_t"]) == 2, f"finish_trial record {rec}")
check([e["on"] for e in evts if e["pin"] == "laser"] == [True, False], "laser ON/OFF events emitted once each")

print("device: fallback when no edge arrives")
fake.writes.clear()
dev.arm_trial(4, opto_on=True)
t0 = time.time()
dev.fire_at("trial_start", t0)
dev.arm_edge(t0, t0 + 0.08)
time.sleep(0.4)
on = writes(LASER, 1)
check(len(on) == 1 and abs(ms(t0, on[0][0]) - 80) < 6, f"fallback fires once at the predicted onset ({ms(t0, on[0][0]) if on else float('nan'):.1f} ms)")
rec = dev.finish_trial()
check(rec["opto_window_anchor"] == "stim_onset:fallback", "record says fallback")

print("device: lead window, scheduled windows, pin-busy drop, reward guard")
dev.close()
dev = make_dev(task_opto(windows={
    "stim_onset": {"enabled": True, "onset_s": -0.1, "duration_s": 0.3},
    "response_window": {"enabled": True, "onset_s": 0.05, "duration_s": 0.1},
    "reward": {"enabled": True, "onset_s": 0.0, "duration_s": 0.05}}, mask=False))
check(dev.latency_estimate() == 0.09, "latency estimate = rig seed before 3 confirmed onsets")
for lat in (0.10, 0.08, 0.095, 0.02):
    dev.note_true_onset(0.0, lat)
check(abs(dev.latency_estimate() - 0.095) < 1e-9, "running median of confirmed onsets (<40 ms ignored)")
dev.arm_trial(5, opto_on=True)
check(abs(dev.lead_s() - 0.1) < 1e-9, "lead_s = 0.1 for this trial")
dev.fire_at("reward", time.time())
check(not fake.writes, "a reward before trial_start fires nothing")
t0 = time.time()
dev.fire_at("trial_start", t0)
dev.fire_stim_lead(t0 + 0.15)                     # predicted onset 150 ms away -> ON at +50 ms
time.sleep(0.12)
on = writes(LASER, 1)
check(len(on) == 1 and abs(ms(t0, on[0][0]) - 50) < 6, f"lead window scheduled at pred - 0.1 ({ms(t0, on[0][0]) if on else float('nan'):.1f} ms)")
dev.fire_at("response_window", time.time())      # laser still ON (0.3 s window) -> dropped
time.sleep(0.1)
check(len(writes(LASER, 1)) == 1, "a window while the pin is still ON is dropped, not queued")
dev.fire_at("reward", time.time())               # also while ON -> dropped
dev.fire_at("reward", time.time())               # second reward: ignored anyway
time.sleep(0.3)
rec = dev.finish_trial()
check(rec["opto_n_windows"] == 1 and rec["opto_n_dropped"] == 2, f"record: 1 window, 2 dropped ({rec['opto_n_windows']}, {rec['opto_n_dropped']})")
check(fake.levels[LASER] == 0, "laser LOW at the end")

print("device: pulsed pattern, mask-only trial, cancel")
dev.close()
dev = make_dev(task_opto(pattern={"mode": "pulsed", "pulse_hz": 20, "pulse_width_ms": 5},
                         windows={"trial_start": {"enabled": True, "onset_s": 0, "duration_s": 1.0}}))
dev.arm_trial(6, opto_on=True)
t0 = time.time()
dev.fire_at("trial_start", t0)
time.sleep(1.15)
on = writes(LASER, 1)
off = writes(LASER, 0)
check(len(on) == 20 and len(off) == 20, f"20 pulses ({len(on)} on / {len(off)} off)")
if len(on) == 20:
    starts = [ms(t0, t) for t, _, _ in on]
    widths = [ms(a[0], b[0]) for a, b in zip(on, off)]
    periods = [b - a for a, b in zip(starts, starts[1:])]
    drift = starts[-1] - 19 * 50.0
    check(abs(statistics.mean(periods) - 50) < 1 and max(abs(p - 50) for p in periods) < 6,
          f"period 50 ms (mean {statistics.mean(periods):.2f}, worst {max(abs(p - 50) for p in periods):.1f} ms off)")
    check(all(2 <= w < 11 for w in widths), f"widths 5 ms (min {min(widths):.1f} max {max(widths):.1f})")
    check(abs(drift) < 6, f"no drift: pulse 20 at {starts[-1]:.1f} ms vs 950 ({drift:+.1f} ms)")
check(len(writes(MASK, 1)) == 20, "mask pulses alongside")
dev.finish_trial()
fake.writes.clear()
dev.arm_trial(7, opto_on=False)                   # not an opto trial: mask only
t0 = time.time()
dev.fire_at("trial_start", t0)
time.sleep(0.3)
check(not writes(LASER) and len(writes(MASK, 1)) >= 5, "non-opto trial: mask fires, laser silent")
dev.cancel()
t_cancel = time.time()
time.sleep(0.3)
check(fake.levels[MASK] == 0 and not writes(MASK, 1, since=t_cancel + 0.002), "cancel: pins LOW, nothing more scheduled")
rec = dev.finish_trial()
check(rec["opto_on"] == 0 and rec["opto_mask_on"] == 1 and math.isnan(rec["opto_onset_t"])
      and rec["opto_window_t"] == [], "mask-only record: opto_on 0, NaN onset, no laser windows")
dev.close()
check(fake.levels[LASER] == 0 and LASER not in fake.claimed, "close drives LOW and frees the pins")

print("device: inert when the task has opto off; init refusals")
dev = make_dev({"enabled": False})
dev.arm_trial(0, opto_on=True)
dev.fire_at("trial_start", time.time())
check(not fake.writes and not dev.active and dev.lead_s() == 0.0, "task opto off: nothing fires")
dev.close()
try:
    make_dev(task_opto(windows={"stim_onset": {"enabled": True, "onset_s": 0, "duration_s": 9}}))
    check(False, "init must refuse a window above max_on_s")
except RuntimeError as e:
    check("cannot init" in str(e) and "max_on_s" in str(e), f"init refuses: {e}")
try:
    make_dev(task_opto(), pins={"mask": {"gpio": 27}})
    check(False, "init must refuse a rig without a laser pin")
except RuntimeError as e:
    check("laser pin" in str(e), f"init refuses: {e}")

# ── 4. Deploy-time assignment ──
print("stim_generator: opto assignment")
check(is_go_trial(-40, "left") and not is_go_trial(80, "left") and is_go_trial(80, "right")
      and is_go_trial(80, "all") and is_go_trial(0, "all"), "is_go_trial rule")


def gen_task(**opto):
    return {"stimulus": {"size_deg": 8, "shape": "square", "duration_s": 2, "altitude_deg": 10,
                         "go_rule": "left", "background_gray": 0.5,
                         "contrast": {"values": [1.0, 0.5], "proportions": [1, 1]}},
            "reward": {"level": 1}, "session": {"n_blocks": 3, "block_size": 20,
                                                 "block_sequence": [80, -40], "iti": 1},
            "opto": task_opto(**opto)}


def counts(trials):
    by_block = {}
    for t in trials:
        by_block.setdefault(t["block_num"], {}).setdefault(t["contrast"], [0, 0])
        by_block[t["block_num"]][t["contrast"]][0] += 1
        by_block[t["block_num"]][t["contrast"]][1] += int(t["opto"])
    return by_block


ok_total = ok_strat = ok_gap = ok_elig = True
for seed in range(60):
    tr = build_block_trial_list(gen_task(fraction=0.25, min_gap_trials=2), seed=seed)
    n_opto = sum(t["opto"] for t in tr)
    ok_total &= (n_opto == 15)
    for b, g in counts(tr).items():
        ok_strat &= all(n_on in (2, 3) for _, (n, n_on) in g.items()) and sum(v[1] for v in g.values()) == 5
    pos = [i for i, t in enumerate(tr) if t["opto"]]
    ok_gap &= all(b - a > 2 for a, b in zip(pos, pos[1:]))
check(ok_total, "fraction 0.25 x 60 trials -> 15 opto trials for every seed")
check(ok_strat, "per block: 5 opto trials, 2-3 per contrast (stratified)")
check(ok_gap, "min_gap_trials 2 honoured (also across block boundaries)")
for seed in range(30):
    tr = build_block_trial_list(gen_task(fraction=0.5, eligible="go"), seed=seed)
    ok_elig &= all((not t["opto"]) or t["az_deg"] < 0 for t in tr) and sum(t["opto"] for t in tr) == 10
check(ok_elig, "eligible go: only az < 0 trials lit, 50 % of the one go block (az -40) = 10")
tr = build_block_trial_list(gen_task(fraction=0.5, eligible="nogo"), seed=1)
check(all((not t["opto"]) or t["az_deg"] > 0 for t in tr), "eligible nogo: only az > 0 trials lit")
tr = build_block_trial_list(gen_task(fraction=1.0), seed=2)
check(all(t["opto"] for t in tr), "fraction 1 lights every trial")
tr = build_block_trial_list(gen_task(fraction=0.0), seed=2)
check(not any(t["opto"] for t in tr), "fraction 0 lights none")
tr = build_block_trial_list(gen_task(enabled=False, fraction=0.5), seed=2)
check(not any(t["opto"] for t in tr), "opto disabled lights none")
tk = gen_task(fraction=0.25, min_gap_trials=1)
tk["session"]["randomize_blocks"] = True
tr = build_block_trial_list(tk, seed=3)
pos = [i for i, t in enumerate(tr) if t["opto"]]
check(sum(t["opto"] for t in tr) == 15 and all(b - a > 1 for a, b in zip(pos, pos[1:])),
      "randomize_blocks: assigned on the shuffled list as one block, gap kept")
a = build_block_trial_list(gen_task(fraction=0.25), seed=7)
b = build_block_trial_list(gen_task(fraction=0.25), seed=7)
check([t["opto"] for t in a] == [t["opto"] for t in b], "seeded draws are reproducible")

with tempfile.TemporaryDirectory() as d:
    res = generate_stimuli(gen_task(fraction=0.25), None, d, seed=5)
    npz = np.load(Path(d) / "stimuli.npz")
    import yaml
    table = yaml.safe_load((Path(d) / "trials.yaml").read_text())
    check("opto_on" in npz and npz["opto_on"].dtype == np.int8 and len(npz["opto_on"]) == 60
          and int(npz["opto_on"].sum()) == 15, "stimuli.npz carries opto_on (int8, length n)")
    check(all("opto" in row for row in table) and sum(row["opto"] for row in table) == 15
          and [bool(x) for x in npz["opto_on"]] == [row["opto"] for row in table],
          "trials.yaml carries the same opto flags")

# ── 5. HDF5 round trip: the engine's create/resize idiom, consolidate, loaders ──
print("hdf5 / consolidate / loaders")
import h5py
dev = make_dev()
with h5py.File("opto-test.h5", "w", driver="core", backing_store=False) as f:
    for name, spec in dev.hdf5_datasets().items():
        if isinstance(spec, dict):
            f.create_dataset(name, shape=(0,), maxshape=(3,), **spec)
        else:
            f.create_dataset(name, shape=(0,), maxshape=(3,), dtype=spec)
    recs = [{"opto_on": 1, "opto_mask_on": 1, "opto_onset_t": 100.5, "opto_n_windows": 2,
             "opto_n_dropped": 0, "opto_window_t": [100.5, 101.0, 102.0, 102.5],
             "opto_window_anchor": "stim_onset:edge,reward:anchor"},
            {"opto_on": 0, "opto_mask_on": 1}]
    for i, rec in enumerate(recs):
        for name, value in dev.hdf5_trial_data(rec).items():
            f[name].resize(i + 1, axis=0)
            f[name][i] = value
    check(list(f["opto_on"][:]) == [1, 0] and math.isnan(f["opto_onset_t"][1])
          and f["opto_onset_t"][0] == 100.5 and list(f["opto_window_t"][0]) == [100.5, 101.0, 102.0, 102.5]
          and len(f["opto_window_t"][1]) == 0, "per-trial datasets written through the engine idiom")
    s = f["opto_window_anchor"][0]
    s = s.decode() if isinstance(s, bytes) else s
    check(s == "stim_onset:edge,reward:anchor", "vlen str row round-trips")
dev.close()

from shared import consolidate as cons
check(all(cons._GROUP.get(n) == "opto" for n in dev.hdf5_datasets()), "consolidate maps every opto dataset to /opto")
check(cons._NPZ_DATASET.get("opto_on") == "opto_planned", "consolidate keeps the planned opto_on as /stimulus/opto_planned")

# a whole session dir, the way the engine + Deploy leave it, through consolidate and the loader
import yaml
from analysis.loaders import Session
with tempfile.TemporaryDirectory() as d:
    sd = Path(d) / "T1_20261106_001"
    sd.mkdir()
    n = 3
    with h5py.File(sd / "T1_20261106_001.h5", "w") as f:
        core = {"trial_num": ("i4", [0, 1, 2]), "stim_onset_t": ("f8", [10.0, 20.0, 30.0]),
                "true_onset_t": ("f8", [10.09, 20.09, 30.09]),
                "response_window_t": ("f8", [10.7, 20.7, 30.7])}
        for name, (dt, vals) in core.items():
            ds = f.create_dataset(name, shape=(0,), maxshape=(n,), dtype=dt)
            for i, v in enumerate(vals):
                ds.resize(i + 1, axis=0); ds[i] = v
        f.create_dataset("trial_outcome", shape=(0,), maxshape=(n,), dtype=h5py.vlen_dtype(str))
        for i, v in enumerate(["hit", "miss", "hit"]):
            f["trial_outcome"].resize(i + 1, axis=0); f["trial_outcome"][i] = v
        for name, spec in dev.hdf5_datasets().items():
            if isinstance(spec, dict):
                f.create_dataset(name, shape=(0,), maxshape=(n,), **spec)
            else:
                f.create_dataset(name, shape=(0,), maxshape=(n,), dtype=spec)
        recs = [{"opto_on": 1, "opto_mask_on": 1, "opto_onset_t": 9.99, "opto_n_windows": 2,
                 "opto_n_dropped": 0, "opto_window_t": [9.99, 11.49, 10.75, 11.25],
                 "opto_window_anchor": "stim_onset:pred,response_window:anchor"},
                {"opto_on": 0, "opto_mask_on": 1},
                {"opto_on": 1, "opto_mask_on": 1, "opto_onset_t": 30.091, "opto_n_windows": 1,
                 "opto_n_dropped": 1, "opto_window_t": [30.091, 31.591],
                 "opto_window_anchor": "stim_onset:edge"}]
        for i, rec in enumerate(recs):
            for name, value in dev.hdf5_trial_data(rec).items():
                f[name].resize(i + 1, axis=0); f[name][i] = value
    (sd / "metadata.yaml").write_text(yaml.dump({
        "session_id": "T1_20261106_001", "subject_id": "T1", "n_trials_completed": 3,
        "task_config": {"opto": task_opto(), "stimulus": {"go_rule": "left"}},
        "saved_devices": ["opto"], "end_reason": "completed"}))
    np.savez(sd / "stimuli.npz", opto_on=np.array([1, 0, 1], np.int8), n_trials=np.array([3]),
             iti_durations=np.array([1, 1, 1, 1], np.float32), duration_s=np.array([2, 2, 2], np.float32),
             prestim_durations=np.array([0, 0, 0], np.float32))
    (sd / "trials.yaml").write_text("[]")
    res = cons.consolidate_session(sd, remux_video=False)
    with h5py.File(sd / "T1_20261106_001.h5", "r") as f:
        check("opto" in f and "opto_window_t" in f["opto"] and "opto_planned" in f["stimulus"]
              and "opto_on" not in f["trials"], "consolidated file has /opto/* and /stimulus/opto_planned")
    s = Session(sd)
    tr = s.trials
    check(list(tr["opto"]) == [True, False, True] and list(tr["opto_mask"]) == [True] * 3
          and list(tr["opto_planned"]) == [1, 0, 1], "trials: opto / opto_mask / opto_planned columns")
    check(abs(tr["opto_lat_ms"][0] - (-100.0)) < 1e-6 and math.isnan(tr["opto_lat_ms"][1])
          and abs(tr["opto_lat_ms"][2] - 1.0) < 1e-6, f"opto_lat_ms = laser onset - true onset ({list(tr['opto_lat_ms'].round(3))})")
    check(list(tr["opto_anchors"]) == ["stim_onset:pred,response_window:anchor", "", "stim_onset:edge"],
          "opto_anchors column")
    ow = s.opto_windows
    check(len(ow) == 3 and list(ow["anchor"]) == ["stim_onset:pred", "response_window:anchor", "stim_onset:edge"]
          and abs(ow["t_rel_onset_s"][0] - (-0.1)) < 1e-6 and list(ow["trial_num"]) == [0, 0, 2],
          f"opto_windows long table ({len(ow)} rows)")
    check(s.meta["task_config"]["opto"]["power_mw"] == 5, "task_config.opto (power_mw etc.) in the root attrs")
dev.close()

print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
