#!/usr/bin/env python
"""
tools/mock_pi.py — a fake Pi + fake leader on localhost, for exercising the experiment
UI end-to-end WITHOUT real hardware.

Run it alongside the controller, using the `demo` rig:

    python tools/mock_pi.py            # this fake Pi:  HTTP :5080  + UDP :5572
    python app/app.py --no-browser     # the real controller

Then in the browser: rig = demo → Load Rig → Deploy → Go. The mock answers every pi_api
call as OK so Deploy/Go succeed (its /api/logs contains the literal "Waiting for START
command..." line the controller's readiness gate greps for), and on the Go "START" command
it plays a scripted 4-phase session (ITI → pre-stim → stim → post-stim) over UDP.

Env: MOCK_N=6  MOCK_ITI=2.0  MOCK_PRE=0.5  MOCK_STIM=1.0  MOCK_POST=0.5
     API_PORT=5080  EVENT_PORT=5571  CMD_PORT=5572  CTRL_HOST=127.0.0.1
"""
import json
import os
import socket
import threading
import time

from flask import Flask, jsonify, request

API_PORT = int(os.environ.get("API_PORT", 5080))
EVENT_PORT = int(os.environ.get("EVENT_PORT", 5571))   # controller listens here
CMD_PORT = int(os.environ.get("CMD_PORT", 5572))       # we listen here for START / STOP
N = int(os.environ.get("MOCK_N", 6))                   # trials in the fake session
ITI_S = float(os.environ.get("MOCK_ITI", 2.0))
PRE_S = float(os.environ.get("MOCK_PRE", 0.5))
STIM_S = float(os.environ.get("MOCK_STIM", 1.0))
POST_S = float(os.environ.get("MOCK_POST", 0.5))
CTRL_HOST = os.environ.get("CTRL_HOST", "127.0.0.1")

app = Flask(__name__)
_stop = threading.Event()       # set when the controller sends STOP
_running = threading.Event()    # a session is currently playing
_started = threading.Event()    # /api/start was called (the fake engine "process" is up)


# ── fake pi_api: answer OK to everything the controller asks ──

@app.route("/api/status", methods=["GET"])
def status():
    return jsonify({"ok": True, "hostname": "mock-pi",
                    "process_running": _started.is_set(),
                    "cameras": {}, "camera_recording": False, "camera_frames": None,
                    "disk_free_gb": 42.0, "disk_total_gb": 64.0})


@app.route("/api/logs", methods=["GET"])
def logs():
    # The controller's Go readiness gate greps for the literal "Waiting for START" line.
    lines = ["[mock-pi] fake leader — no real engine"]
    if _started.is_set():
        lines.append("Waiting for START command...")
    return jsonify({"ok": True, "lines": lines})


@app.route("/api/start", methods=["POST"])
def start():
    _started.set()
    print("[mock-pi] /api/start -> fake engine 'running'")
    return jsonify({"ok": True, "pid": 4242})


@app.route("/api/stop", methods=["POST"])
def stop():
    _started.clear()
    _stop.set()
    return jsonify({"ok": True})


@app.route("/api/<path:endpoint>", methods=["GET", "POST"])
def catchall(endpoint):
    print(f"[mock-pi] {request.method} /api/{endpoint} -> ok")
    return jsonify({"ok": True, "pid": 4242, "lines": [], "files": [], "sizes": {},
                    "events": [], "message": "mock"})


# ── UDP fake leader: play a scripted 4-phase session on START ──

def _send(sock, evt):
    evt.setdefault("t", time.time())
    sock.sendto(json.dumps(evt).encode(), (CTRL_HOST, EVENT_PORT))


def _sleep_stopaware(dur):
    """Sleep `dur` seconds, returning False early if a STOP arrived."""
    slept = 0.0
    while slept < dur and not _stop.is_set():
        time.sleep(0.05)
        slept += 0.05
    return not _stop.is_set()


def play_session(session_id):
    if _running.is_set():
        return
    _running.set()
    _stop.clear()
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        _send(out, {"type": "experiment_start"})
        completed = 0
        for i in range(N):
            if _stop.is_set():
                break
            t0 = time.time()
            _send(out, {"type": "trial_start", "trial": i, "t": t0, "iti": ITI_S})
            if not _sleep_stopaware(ITI_S + PRE_S):
                break
            stim_on_t = time.time()
            _send(out, {"type": "stim", "on": True, "trial": i, "t": stim_on_t})
            if not _sleep_stopaware(STIM_S):
                break
            _send(out, {"type": "stim", "on": False, "trial": i})
            if not _sleep_stopaware(POST_S):
                break
            t1 = time.time()
            _send(out, {"type": "trial", "trial_num": i, "t": t1,
                        "stim_on_t": stim_on_t, "duration_s": round(t1 - t0, 3)})
            completed += 1

        if _stop.is_set():
            print("[mock-pi] STOP received mid-session — NOT sending session_end (tests ⏹️)")
        else:
            _send(out, {"type": "session_end", "n_completed": completed, "n_planned": N})
            print(f"[mock-pi] sent session_end {completed}/{N}")
    finally:
        out.close()
        _running.clear()
        _started.clear()


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
          f"N={N} iti={ITI_S}s pre={PRE_S}s stim={STIM_S}s post={POST_S}s")
    app.run(host="127.0.0.1", port=API_PORT, threaded=True)
