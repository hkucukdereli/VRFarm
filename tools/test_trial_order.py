#!/usr/bin/env python3
"""tools/test_trial_order.py — checks for the Deploy-time trial plan (shared/stim_generator.py
build_block_trial_list), no hardware: the blocked order (block_sequence cycled, contrasts shuffled
inside each block) and the Randomize blocks order (one shuffled list, balanced over location x
contrast), the largest-remainder split behind it, and the Deploy log's count line.

    conda activate vrfarm && python tools/test_trial_order.py
"""
import contextlib
import io
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.stim_generator import (_apportion, build_block_trial_list, generate_stimuli,   # noqa: E402
                                   plan_summary, switch_trial_rule)

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


def task(n_blocks=3, block_size=50, seq=(80, -80), values=(3, 0.4, 0.15), props=(0.34, 0.33, 0.33),
         randomize=True, **stim):
    """The shape of experiments/attention_training_L2_ASD110.yaml by default."""
    return {
        "session": {"n_blocks": n_blocks, "block_size": block_size, "block_sequence": list(seq),
                    "randomize_blocks": randomize, "iti": [3, 6], "block_delay": 0.2},
        "stimulus": {"contrast": {"values": list(values), "proportions": list(props)},
                     "altitude_deg": 10.0, "go_rule": "all", "size_deg": 20, "duration_s": 1.0,
                     "background_gray": 0.3, **stim},
    }


def by_az(trials):
    return Counter(t["az_deg"] for t in trials)


def by_contrast(trials):
    return Counter(t["contrast"] for t in trials)


def cells(trials):
    return Counter((t["az_deg"], t["contrast"]) for t in trials)


def longest_run(trials, key="az_deg"):
    best = run = 0
    prev = object()
    for t in trials:
        run = run + 1 if t[key] == prev else 1
        prev = t[key]
        best = max(best, run)
    return best


# ── the split: largest remainder ──
print("apportion (largest remainder)")
rng = np.random.default_rng(0)
seen = Counter(tuple(_apportion(150, [0.34, 0.33, 0.33], rng)) for _ in range(400))
check(set(seen) == {(51, 50, 49), (51, 49, 50)},
      f"150 trials at 34/33/33: 51 + a 50 and a 49, never the old 54/48/48 ({dict(seen)})")
check(min(seen.values()) > 120, "the tie between the two 33s is drawn at random (both orders, about evenly)")
check(_apportion(150, [34, 33, 33], rng) in ([51, 50, 49], [51, 49, 50]), "proportions on any scale")
check(_apportion(100, [0.5, 0.25, 0.25], rng) == [50, 25, 25], "exact shares stay exact")
check(_apportion(10, [0.1, 0.2, 0.7], rng) == [1, 2, 7], "float dust (0.7 * 10 = 6.999...) does not cost a trial")
check(_apportion(150, [1, 0, 1], rng) == [75, 0, 75], "proportion 0 gets no trial")
check(_apportion(0, [1, 2], rng) == [0, 0] and _apportion(7, [3], rng) == [7], "no trials / one entry")
ok = True
for _ in range(2000):
    k = int(rng.integers(1, 7))
    w = rng.random(k) + 0.01
    n = int(rng.integers(0, 400))
    s = _apportion(n, w, rng)
    exact = n * w / w.sum()
    ok &= sum(s) == n and all(np.floor(e - 1e-6) <= x <= np.ceil(e + 1e-6) for x, e in zip(s, exact))
check(ok, "2000 random splits: shares add up to n, each is its exact value rounded down or up")
firsts = Counter(int(np.argmax(_apportion(4, [1, 1, 1], rng))) for _ in range(900))
check(len(firsts) == 3 and min(firsts.values()) > 200,
      f"4 trials over 3 equal entries: the spare trial goes to each entry in turn ({dict(firsts)})")

# ── Randomize blocks: one shuffled list, balanced over location x contrast ──
print("Randomize blocks: balance")
N_SEEDS = 300
ok_n = ok_az = ok_con = ok_cell = ok_blk = ok_idx = ok_keys = True
con_seen = set()
for seed in range(N_SEEDS):
    tr = build_block_trial_list(task(), seed=seed)
    ok_n &= len(tr) == 150
    ok_az &= by_az(tr) == {80.0: 75, -80.0: 75}
    cc = by_contrast(tr)
    con_seen.add((cc[3.0], cc[0.4], cc[0.15]))
    ok_con &= (cc[3.0], cc[0.4], cc[0.15]) in ((51, 50, 49), (51, 49, 50))
    ce = cells(tr)
    ok_cell &= all(cc[c] // 2 <= ce[(az, c)] <= -(-cc[c] // 2) for az in (80.0, -80.0) for c in cc)
    ok_blk &= all(t["block_num"] == 0 for t in tr)
    ok_idx &= [t["trial_idx"] for t in tr] == list(range(150))
    ok_keys &= all(set(t) == {"trial_idx", "block_num", "az_deg", "alt_deg", "contrast", "opto"}
                   and t["alt_deg"] == 10.0 and t["opto"] is False for t in tr)
check(ok_n and ok_az, f"3 x 50 over [80, -80] (ASD110's task): 75 trials per side in all {N_SEEDS} plans "
                      "(the old shuffle gave 100 / 50)")
check(ok_con and len(con_seen) == 2, f"contrasts 51/50/49 or 51/49/50 of 150 ({sorted(con_seen)})")
check(ok_cell, "every side x contrast cell holds half of that contrast, give or take one trial")
check(ok_blk and ok_idx and ok_keys, "one block (block_num 0), trial_idx 0..149, same trial keys as before")

ok = True
for seed in range(100):
    tr = build_block_trial_list(task(n_blocks=150, block_size=1, values=(0.25, 0.15, 0.125)), seed=seed)
    cc = by_contrast(tr)
    ok &= by_az(tr) == {80.0: 75, -80.0: 75} and sorted(cc.values()) == [49, 50, 51] and cc[0.25] == 51
check(ok, "150 x 1 (attention_random_L25's shape): 75 per side and all three contrasts — block size "
          "no longer matters (the blocked rounding left only 12.5 %)")

ok_tot = ok_side = ok_cell = True
for seed in range(200):
    tr = build_block_trial_list(task(n_blocks=6, block_size=25, seq=(-80, 45, -45, 80)), seed=seed)
    az, cc, ce = by_az(tr), by_contrast(tr), cells(tr)
    ok_tot &= sorted(az.values()) == [37, 37, 38, 38]
    ok_side &= az[-80.0] + az[-45.0] == 75 and az[45.0] + az[80.0] == 75
    ok_cell &= all(cc[c] // 4 <= ce[(a, c)] <= -(-cc[c] // 4) for a in az for c in cc)
check(ok_tot, "6 x 25 over 4 locations: 37 or 38 trials each (blocked: 50 / 50 / 25 / 25)")
check(ok_side, "the spare trials go to neighbours in the sequence: [-80, 45, -45, 80] stays 75 left / 75 right")
check(ok_cell, "every location x contrast cell within one trial of a quarter of its contrast")

ok = True
for seed in range(100):
    tr = build_block_trial_list(task(seq=(80, 80, -80)), seed=seed)
    az, cc, ce = by_az(tr), by_contrast(tr), cells(tr)
    ok &= az == {80.0: 100, -80.0: 50}
    ok &= all(cc[c] // 3 <= ce[(-80.0, c)] <= -(-cc[c] // 3) for c in cc)
check(ok, "a location listed twice gets twice the trials: [80, 80, -80] -> 100 / 50, contrasts split 2:1")

ok = True
extra = Counter()
for seed in range(300):
    tr = build_block_trial_list(task(n_blocks=7, block_size=7), seed=seed)
    az = by_az(tr)
    ok &= len(tr) == 49 and sorted(az.values()) == [24, 25]
    extra[max(az, key=az.get)] += 1
check(ok and min(extra.values()) > 100,
      f"49 trials over 2 locations: 24 / 25, the spare trial on either side from plan to plan ({dict(extra)})")

tr = build_block_trial_list(task(props=(1, 0, 1)), seed=1)
check(by_contrast(tr) == {3.0: 75, 0.15: 75} and by_az(tr) == {80.0: 75, -80.0: 75},
      "proportion 0: that contrast is not shown (a blocked task shows one per block)")
tr = build_block_trial_list(task(seq=(80,), values=(1.0,), props=(1,)), seed=1)
check(len(tr) == 150 and by_az(tr) == {80.0: 150} and by_contrast(tr) == {1.0: 150}, "one location, one contrast")
tr = build_block_trial_list(task(n_blocks=1, block_size=2, seq=(-80, 45, -45, 80)), seed=1)
check(len(tr) == 2 and len(by_az(tr)) == 2, "fewer trials than locations: no location twice")

print("Randomize blocks: order")
a = build_block_trial_list(task(), seed=11)
b = build_block_trial_list(task(), seed=11)
c = build_block_trial_list(task(), seed=12)
check(a == b, "a seeded draw is reproducible")
check([t["az_deg"] for t in a] != [t["az_deg"] for t in c]
      and [t["contrast"] for t in a] != [t["contrast"] for t in c], "another seed, another order")
N = 2000
first_az = Counter()
first_con = Counter()
half = []
runs = []
for seed in range(N):
    tr = build_block_trial_list(task(), seed=seed)
    first_az[tr[0]["az_deg"]] += 1
    first_con[tr[0]["contrast"]] += 1
    half.append(sum(t["az_deg"] > 0 for t in tr[:75]))
    runs.append(longest_run(tr))
check(abs(first_az[80.0] / N - 0.5) < 0.08, f"trial 0 is +80 in {first_az[80.0] / N:.1%} of {N} plans (no side leads)")
check(all(abs(first_con[c] / N - 1 / 3) < 0.08 for c in (3.0, 0.4, 0.15)),
      "trial 0 takes each contrast about a third of the time")
check(abs(np.mean(half) - 37.5) < 0.6 and 2.0 < np.std(half) < 4.5,
      f"first half of the session: {np.mean(half):.1f} +- {np.std(half):.1f} trials at +80 (a plain shuffle of "
      "75 / 75, no blocks left in it)")
check(max(runs) < 40, f"longest run at one side: median {int(np.median(runs))}, worst {max(runs)} in {N} plans "
                      "(not limited by the generator)")

t_sw = task(switch_trial_contrast="lowest")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    firsts = Counter(build_block_trial_list(t_sw, seed=s)[0]["contrast"] for s in range(200))
check(switch_trial_rule(t_sw) is None and len(firsts) == 3 and "switch_trial_contrast ignored" in buf.getvalue(),
      "1st trial of block is off in this mode (warned, first trial not pinned)")

t_op = task()
t_op["opto"] = {"enabled": True, "fraction": 0.2, "min_gap_trials": 1, "eligible": "all",
                "windows": [{"anchor": "stim_onset", "offset_s": 0.0, "duration_s": 0.5}]}
ok = True
for seed in range(50):
    tr = build_block_trial_list(t_op, seed=seed)
    pos = [i for i, t in enumerate(tr) if t["opto"]]
    ok &= len(pos) == 30 and all(q - p > 1 for p, q in zip(pos, pos[1:])) and by_az(tr) == {80.0: 75, -80.0: 75}
check(ok, "opto on the balanced list: 20 % of 150 = 30 lit, gap kept")

# ── blocked order (Randomize blocks off): unchanged ──
print("blocked order")
ok_loc = ok_cnt = ok_idx = True
for seed in range(50):
    tr = build_block_trial_list(task(n_blocks=6, block_size=50, seq=(-80, 45, -45, 80), randomize=False), seed=seed)
    ok_idx &= len(tr) == 300 and [t["trial_idx"] for t in tr] == list(range(300))
    for b in range(6):
        blk = tr[b * 50:(b + 1) * 50]
        ok_loc &= all(t["block_num"] == b and t["az_deg"] == (-80, 45, -45, 80)[b % 4] for t in blk)
        cc = by_contrast(blk)
        ok_cnt &= (cc[3.0], cc[0.4], cc[0.15]) == (18, 16, 16)
check(ok_loc and ok_idx, "one location per block, block_sequence cycled in the listed order (not shuffled)")
check(ok_cnt, "per-block contrast counts from the proportions: 18 / 16 / 16 of 50 at 34/33/33")
tr = build_block_trial_list(task(n_blocks=6, block_size=50, seq=(-80, 45, -45, 80), randomize=False), seed=0)
check(by_az(tr) == {-80.0: 100, 45.0: 100, -45.0: 50, 80.0: 50},
      "6 blocks over 4 locations: the first two get two blocks each (100 / 100 / 50 / 50)")
orders = {tuple(t["contrast"] for t in build_block_trial_list(task(randomize=False), seed=s)[:50]) for s in range(20)}
check(len(orders) == 20, "contrasts are shuffled inside the block")
ok = True
for rule, want in (("lowest", 0.15), ("highest", 3.0)):
    for seed in range(30):
        tr = build_block_trial_list(task(randomize=False, switch_trial_contrast=rule), seed=seed)
        ok &= all(tr[b * 50]["contrast"] == want for b in range(3))
        ok &= all(sorted(by_contrast(tr[b * 50:(b + 1) * 50]).values()) == [16, 16, 18] for b in range(3))
check(ok, "1st trial of block: lowest / highest contrast heads every block, counts unchanged")

# ── the count line and the generated files ──
print("plan summary + generate_stimuli")
with tempfile.TemporaryDirectory() as d:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        arr = generate_stimuli(task(), None, d, seed=5)
    out = buf.getvalue()
    table = yaml.safe_load((Path(d) / "trials.yaml").read_text())
line = plan_summary(table)
check(int(arr["n_trials"][0]) == 150 and set(arr["block_num"].tolist()) == {0}
      and len(arr["block_delays"]) == 1 and arr["block_start_indices"].tolist() == [0],
      "generate_stimuli: 150 trials in one block, one block delay")
check(Counter(arr["stim_az_deg"].tolist()) == {80.0: 75, -80.0: 75}
      and [r["az"] for r in table] == arr["stim_az_deg"].tolist(), "stimuli.npz and trials.yaml carry the balanced plan")
want = cells(build_block_trial_list(task(), seed=5))
parts = " · ".join(f"{az:g}°: 75 ({'/'.join(str(want[(float(az), c)]) for c in (3.0, 0.4, 0.15))})" for az in (-80, 80))
check(line == f"Trials per location (contrast 300/40/15 %): {parts}", f"count line: {line}")
check(("  " + line) in out.splitlines(), "the generator prints the same line on the Pi")
with tempfile.TemporaryDirectory() as d:
    with contextlib.redirect_stdout(io.StringIO()):
        generate_stimuli(task(n_blocks=6, block_size=25, seq=(-80, 45, -45, 80), values=(0.25,), props=(1,),
                              randomize=False), None, d, seed=5)
    table = yaml.safe_load((Path(d) / "trials.yaml").read_text())
check(plan_summary(table) == "Trials per location: -80°: 50 · -45°: 25 · 45°: 50 · 80°: 25",
      f"one contrast, blocked 6 x 25 over 4 locations: {plan_summary(table)}")
check(plan_summary([]) == "" and plan_summary(None) == "" and plan_summary([None, "x", {"az": "left"}, {"az": 80}]) == "",
      "no readable rows: empty string, no exception")
rows = [{"az": 80.0, "contrast": float(np.float32(0.125))}, {"az": 80, "contrast": 0.125},
        {"az": -22.5, "contrast": float(np.float32(0.15))}, None]
check(plan_summary(rows) == "Trials per location (contrast 15/12.5 %): -22.5°: 1 (1/0) · 80°: 2 (0/2)",
      f"float32 contrasts and unreadable rows: {plan_summary(rows)}")

print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
