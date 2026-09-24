"""
controller/pi_restart.py

Restart pi_api on a rig's Pis and time how long each one was unreachable.

Deploy and Restart API restart pi_api so freshly uploaded code runs, and pi_api answers nothing
for a few seconds while systemd respawns it. shepherd, the leader's health monitor, probes pi_api
every cycle and paged "pi_api not responding" for exactly that window. So every restart is timed
here, and each leader's shepherd is told the result: its api_health grace period becomes the
measured outage x GRACE_FACTOR (pi_api's /api/shepherd_grace writes shepherd/api_grace.json, which
shepherd re-reads). A restart that keeps pi_api away 4 s means shepherd waits 6 s before alerting.
"""
from __future__ import annotations
import threading
import time

import requests

GRACE_FACTOR = 1.5      # headroom, so a slightly slower restart next time still fits
GRACE_MIN_S = 5.0       # a lucky fast restart must not leave a hair trigger
GRACE_MAX_S = 60.0      # a slow one must not hide a real outage for long
POLL_S = 0.2
GONE_WITHIN_S = 4.0     # /api/restart exits 0.5 s after it answers (forced at 2.5 s)


def grace_for(down_s: float) -> float:
    """shepherd's api_health grace period for a restart that kept pi_api away down_s seconds."""
    return round(min(GRACE_MAX_S, max(GRACE_MIN_S, down_s * GRACE_FACTOR)), 1)


def _post(ip: str, port: int, path: str, body: dict | None = None,
          timeout: float = 5.0) -> tuple[int | None, dict]:
    """(HTTP status, or None when nothing answered; the JSON body, or {})."""
    try:
        r = requests.post(f"http://{ip}:{port}{path}", json=body or {}, timeout=timeout)
    except Exception as e:
        return None, {"error": str(e)}
    try:
        return r.status_code, (r.json() if r.content else {})
    except ValueError:
        return r.status_code, {}


def _answers(ip: str, port: int) -> bool:
    try:
        return requests.get(f"http://{ip}:{port}/api/status", timeout=1.0).status_code == 200
    except Exception:
        return False


def restart_pi_api(ip: str, port: int, wait_s: float = 45.0) -> dict:
    """POST /api/restart, then watch pi_api go away and come back.

    Returns {ok, down_s, error}. down_s runs from the last answer before the outage to the first
    answer after it, so it errs long; it is None when pi_api never stopped answering. ok is False
    when the restart was refused (a session lease), or pi_api was still away after wait_s."""
    code, body = _post(ip, port, "/api/restart")
    if code is not None and code != 200:
        return {"ok": False, "down_s": None, "error": body.get("error") or f"restart refused (HTTP {code})"}
    start = last_up = time.monotonic()
    down_at = None
    while time.monotonic() - start < wait_s:
        up = _answers(ip, port)
        now = time.monotonic()
        if down_at is None:
            if not up:
                down_at = last_up
            elif now - start > GONE_WITHIN_S:
                if code is None:
                    return {"ok": False, "down_s": None,
                            "error": f"restart request failed ({body.get('error')}); pi_api kept running"}
                return {"ok": True, "down_s": None, "error": None}
            else:
                last_up = now
        elif up:
            return {"ok": True, "down_s": round(now - down_at, 1), "error": None}
        time.sleep(POLL_S)
    return {"ok": False, "down_s": None, "error": f"pi_api did not come back within {wait_s:.0f} s"}


def restart_shepherd(ip: str, port: int) -> dict:
    """Restart shepherd on that Pi so a freshly deployed shepherd.py runs.
    Returns {ok, skipped, missing, error}: skipped says why nothing was restarted (shepherd not
    running there); missing means that pi_api predates /api/restart_shepherd."""
    code, body = _post(ip, port, "/api/restart_shepherd", timeout=45.0)
    if code == 404:
        return {"ok": False, "skipped": None, "missing": True, "error": "pi_api predates /api/restart_shepherd"}
    if code == 200 and body.get("ok"):
        return {"ok": True, "skipped": body.get("skipped"), "missing": False, "error": None}
    return {"ok": False, "skipped": None, "missing": False,
            "error": body.get("error") or (f"HTTP {code}" if code else "no answer")}


def set_shepherd_grace(ip: str, port: int, down_s: float) -> dict:
    """Give shepherd on that Pi its api_health grace period for a restart that took down_s.
    Returns {ok, grace_s, error}."""
    grace_s = grace_for(down_s)
    code, body = _post(ip, port, "/api/shepherd_grace", {"down_s": down_s, "grace_s": grace_s})
    if code == 200 and body.get("ok"):
        return {"ok": True, "grace_s": grace_s, "error": None}
    return {"ok": False, "grace_s": grace_s,
            "error": body.get("error") or (f"HTTP {code}" if code else "no answer")}


def _shepherd_line(pi: dict, r: dict) -> str:
    if r["ok"]:
        return (f"shepherd is not running on {pi['name']} — left as is" if r["skipped"]
                else f"Restarted shepherd on {pi['name']} (runs the deployed shepherd.py)")
    return f"WARNING: shepherd restart failed on {pi['name']}: {r['error']}"


def restart_timed(pis: list, port: int, reload_shepherd: bool = False) -> tuple[dict, list]:
    """Restart pi_api on these Pis (all at once), time each outage, and hand it to the leaders'
    shepherd as its grace period. reload_shepherd (Deploy) also restarts shepherd on the leaders
    BEFORE pi_api, so the shepherd.py just uploaded is what watches the restart; a leader whose
    pi_api is too old for that gets it right after the new pi_api is up.

    pis: [{name, ip, role}]. Returns ({pi name: restart_pi_api result}, log lines)."""
    steps, retry = [], []
    leaders = [pi for pi in pis if pi.get("role") == "leader"]
    for pi in (leaders if reload_shepherd else []):
        r = restart_shepherd(pi["ip"], port)
        if r["missing"]:
            retry.append(pi)
        else:
            steps.append(_shepherd_line(pi, r))

    results = {}

    def one(pi):
        results[pi["name"]] = restart_pi_api(pi["ip"], port)

    threads = [threading.Thread(target=one, args=(pi,), daemon=True) for pi in pis]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for pi in pis:
        res = results[pi["name"]]
        if not res["ok"]:
            steps.append(f"WARNING: pi_api on {pi['name']}: {res['error']}")
            continue
        steps.append(f"pi_api on {pi['name']} was down {res['down_s']} s" if res["down_s"] is not None
                     else f"pi_api on {pi['name']} restarted (outage not measured)")
        if pi in retry:
            steps.append(_shepherd_line(pi, restart_shepherd(pi["ip"], port)))
        if pi in leaders and res["down_s"] is not None:
            gr = set_shepherd_grace(pi["ip"], port, res["down_s"])
            steps.append(f"shepherd on {pi['name']} now gives pi_api {gr['grace_s']} s to come back before alerting"
                         if gr["ok"] else
                         f"WARNING: shepherd grace period not updated on {pi['name']}: {gr['error']}")
    return results, steps
