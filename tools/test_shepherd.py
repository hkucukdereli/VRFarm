#!/usr/bin/env python3
"""tools/test_shepherd.py — shepherd's camera encode-fps estimate and alert state, with a scripted
pi_api /api/status and a fake clock. No hardware.
    conda activate vrfarm && python tools/test_shepherd.py
"""
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shepherd"))
import shepherd as S          # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


CFG = {"shepherd": {"interval_s": 1.0, "realert_critical_s": 30,
                    "api_probe": {"enabled": True, "url": "http://127.0.0.1:1/api/status", "timeout_s": 1}},
       "metrics": {"camera_fps": {"enabled": True, "direction": "low", "warn": 45, "critical": 30, "unit": "fps",
                                  "warn_msg": "warn {value}", "critical_msg": "crit {value}", "recover_msg": "rec {value}"}}}

# a fake clock the module sees as `time`, and a scripted urlopen
clock = {"t": 1000.0}


class FakeTime:
    @staticmethod
    def monotonic():
        return clock["t"]
    time = staticmethod(time.time)
    sleep = staticmethod(lambda s: None)


bodies = []


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(url, timeout=None):
    return _Resp(json.dumps(bodies.pop(0)).encode())


S.time = FakeTime
S.urllib.request.urlopen = fake_urlopen

mon = S.Monitor(CFG)


def sample(rec, frames):
    bodies.append({"ok": True, "process_running": True, "camera_recording": rec, "camera_frames": frames})
    clock["t"] += 1.0
    return mon.sample_api().get("camera_fps")


print("— fps estimate")
seq = [(False, None), (True, 0), (True, 25), (True, 75), (True, 125), (False, None), (True, 0), (True, 50), (True, 100),
       (True, 100), (True, 100), (True, 3), (True, 53)]
got = [sample(*s) for s in seq]
want = [None, None, None, 50.0, 50.0, None, None, None, 50.0, 0.0, 0.0, None, 50.0]
check(got == want, f"fps per sample: {got}")
check(mon._prev_frames == (53, clock["t"]), "delta state follows the last recording sample")
sample(False, None)
check(mon._prev_frames is None, "idle sample clears the delta state")
got2 = [sample(True, 10), sample(True, 60)]
check(got2 == [None, 50.0], f"after idle: first sample no fps, second valid (counter already > 0): {got2}")

print("— alert engine")
eng = S.ThresholdEngine(CFG)
a = eng.evaluate("camera_fps", 20)
check(a and a["level"] == "critical", f"20 fps -> critical: {a}")
a = eng.evaluate("camera_fps", 50)
check(a and a["level"] == "ok" and a["message"] == "rec 50", f"back to 50 without a reset -> recovery: {a}")
eng.evaluate("camera_fps", 20)
eng.reset("camera_fps")
a = eng.evaluate("camera_fps", 50)
check(a is None, "after reset(): 50 fps is not a recovery")
a = eng.evaluate("camera_fps", 0.0)
check(a and a["level"] == "critical", "a stall (0 fps) is critical")


class Sink:
    def __init__(self):
        self.sent = []

    def send(self, a):
        self.sent.append(a)


print("— evaluate_all")
eng, sink = S.ThresholdEngine(CFG), Sink()
S.evaluate_all(eng, sink, {"api": {"ok": True, "camera_recording": True, "camera_fps": 20}})
check([a["level"] for a in sink.sent] == ["critical"], f"recording at 20 fps alerts: {[a['message'] for a in sink.sent]}")
S.evaluate_all(eng, sink, {"api": {"ok": True, "camera_recording": True}})            # the two gated samples: no fps
S.evaluate_all(eng, sink, {"api": {"ok": True, "camera_recording": False}})          # recording ended
S.evaluate_all(eng, sink, {"api": {"ok": True, "camera_recording": True, "camera_fps": 50}})
check(len(sink.sent) == 1, f"no alert for the gated samples, none for the idle sample, no 'recovered' at the next recording ({len(sink.sent)} sent)")
S.evaluate_all(eng, sink, {"api": {"ok": True, "camera_recording": True, "camera_fps": 25}})
check(len(sink.sent) == 2 and sink.sent[-1]["level"] == "critical", "a real drop in the next recording still alerts")

print(f"\n{'PASS' if not fails else f'{fails} FAILURE(S)'}")
sys.exit(1 if fails else 0)
