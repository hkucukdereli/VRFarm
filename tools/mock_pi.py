#!/usr/bin/env python
"""
tools/mock_pi.py — a fake Pi + fake leader on localhost, for exercising the experiment
UI end-to-end WITHOUT real hardware. Built to test the Slack notifications (dev-slack
branch), but it also drives the live plots so it's a general dry-run harness.

Run it alongside the controller, using the `demo` rig:

    export VRFARM_SLACK_WEBHOOK=...     # else notify() is a silent no-op
    python tools/mock_pi.py            # this fake Pi:  HTTP :5080  + UDP :5572
    python app/app.py                  # the real controller (on the MERGED dev-slack branch)

Then in the browser: rig = demo → Load Rig → Deploy → Go. The mock answers every pi_api
call as OK so Deploy/Go succeed, and on the Go "START" command it plays a short scripted
session over UDP (trials → session_end), which:
  • fires ▶️ (session started, from the controller's go())  and  ✅ (session ended, from
    the controller's UDP listener receiving our session_end),
  • populates the trial list + live raster/PSTH.

To hit the other two messages:
  • ⏹️  stopped-no-session_end  →  press Stop mid-run (we then send NO session_end).
  • ⏱️  global timeout          →  MOCK_END=timeout python tools/mock_pi.py

Env: MOCK_N=8  MOCK_PERIOD=1.5  MOCK_END=end|timeout
     API_PORT=5080  EVENT_PORT=5571  CMD_PORT=5572  CTRL_HOST=127.0.0.1
"""
import json
import os
import socket
import threading
import time

import yaml
from flask import Flask, Response, jsonify, request

API_PORT = int(os.environ.get("API_PORT", 5080))
EVENT_PORT = int(os.environ.get("EVENT_PORT", 5571))   # controller listens here
CMD_PORT = int(os.environ.get("CMD_PORT", 5572))       # we listen here for START / STOP
N = int(os.environ.get("MOCK_N", 8))                   # trials in the fake session
PERIOD = float(os.environ.get("MOCK_PERIOD", 1.5))     # seconds per trial
END_MODE = os.environ.get("MOCK_END", "end")           # "end" or "timeout"
CTRL_HOST = os.environ.get("CTRL_HOST", "127.0.0.1")

app = Flask(__name__)
_stop = threading.Event()       # set when the controller sends STOP
_running = threading.Event()    # a session is currently playing

# A fake universal trial table (so the controller/UI get a real N-trial plan — the raster
# y-axis then shows the full planned count).
TRIALS = [{"trial": i, "block": 0, "iti_s": 8.0, "prestim_s": 0.5, "poststim_s": 1.0,
           "duration_s": 2.0, "stim_az": 30 if (i % 3) else 90, "contrast": 0.5, "level": 1}
          for i in range(N)]


# ── fake pi_api: answer OK to everything the controller asks ──

@app.route("/api/generate_stims", methods=["POST"])
def generate_stims():
    sid = (request.get_json(silent=True) or {}).get("session_id", "demo")
    return jsonify({"ok": True, "n_trials": N, "npz_path": f"stims/{sid}/stimuli.npz"})


@app.route("/api/download/<path:p>", methods=["GET"])
def download(p):
    if p.endswith("trials.yaml"):
        return Response(yaml.safe_dump(TRIALS), mimetype="text/yaml")
    return Response(b"", mimetype="application/octet-stream")   # empty NPZ; controller only relays it


@app.route("/api/status", methods=["GET"])
def status():
    return jsonify({"ok": True, "role": "leader", "running": _running.is_set()})


@app.route("/api/logs", methods=["GET"])
def logs():
    return jsonify({"ok": True, "lines": ["[mock-pi] fake leader — no real engine"]})


@app.route("/api/<path:endpoint>", methods=["GET", "POST"])
def catchall(endpoint):
    print(f"[mock-pi] {request.method} /api/{endpoint} -> ok")
    return jsonify({"ok": True, "pid": 4242, "lines": [], "files": []})


# ── UDP fake leader: play a scripted session on START ──

def _send(sock, evt):
    evt.setdefault("t", time.time())
    sock.sendto(json.dumps(evt).encode(), (CTRL_HOST, EVENT_PORT))


def play_session(session_id):
    if _running.is_set():
        return
    _running.set(); _stop.clear()
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        _send(out, {"type": "experiment_start"})
        completed = 0
        for i in range(N):
            if _stop.is_set():
                break
            tr = TRIALS[i]
            az = tr["stim_az"]
            _send(out, {"type": "iti_start", "trial": i, "duration": tr["iti_s"]})
            time.sleep(min(0.4, PERIOD / 4))
            _send(out, {"type": "trial_start", "trial": i})
            t_on = time.time()
            _send(out, {"type": "stim", "on": True, "trial": i, "az": az})
            responded = (i % 3) != 0 and (i % 4) != 0          # a mix of hits/misses
            rt = 0.6 + 0.25 + (i % 5) * 0.13                   # within the response window
            if responded:
                _send(out, {"type": "lick", "t": t_on + rt})
                if i % 2:
                    _send(out, {"type": "lick", "t": t_on + rt + 0.25})
                _send(out, {"type": "reward", "on": True, "pulse_ms": 30, "t": t_on + rt + 0.05})
            _send(out, {"type": "stim", "on": False, "trial": i, "t": t_on + tr["duration_s"]})
            _send(out, {"type": "trial", "trial_num": i, "stim_az": az, "contrast": tr["contrast"],
                        "outcome": "hit" if responded else "miss", "reward_type": "operant",
                        "rt_ms": round(rt * 1000) if responded else None,
                        "resp_licks": (1 if responded else 0), "level": 1, "adaptive_state": 1})
            completed += 1
            slept = 0.0                                        # rest of the trial period, STOP-aware
            while slept < PERIOD and not _stop.is_set():
                time.sleep(0.1); slept += 0.1
        if _stop.is_set():
            print("[mock-pi] STOP received mid-session — NOT sending session_end (tests ⏹️)")
        else:
            if END_MODE == "timeout":
                _send(out, {"type": "global_timeout", "trial": completed,
                            "n_dry": 12, "phases": ["prestim", "stim"]})
                print("[mock-pi] sent global_timeout (tests ⏱️)")
            _send(out, {"type": "session_end", "n_completed": completed, "n_planned": N})
            print(f"[mock-pi] sent session_end {completed}/{N} (tests ✅)")
    finally:
        out.close(); _running.clear()


def cmd_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", CMD_PORT))
    print(f"[mock-pi] listening for START/STOP on UDP :{CMD_PORT}")
    while True:
        data, _ = sock.recvfrom(4096)
        try:
            msg = json.loads(data)
        except Exception:
            continue
        cmd = msg.get("cmd")
        print(f"[mock-pi] cmd received: {cmd}")
        if cmd == "START":
            threading.Thread(target=play_session, args=(msg.get("session_id"),), daemon=True).start()
        elif cmd == "STOP":
            _stop.set()


if __name__ == "__main__":
    threading.Thread(target=cmd_listener, daemon=True).start()
    print(f"[mock-pi] fake pi_api on http://127.0.0.1:{API_PORT}  (use rig=demo)  "
          f"N={N} period={PERIOD}s end={END_MODE}")
    app.run(host="127.0.0.1", port=API_PORT, threaded=True)
