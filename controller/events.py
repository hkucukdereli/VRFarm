"""
controller/events.py

The ONE UDP socket the controller listens on (event_port, default 5571), opened once at
start-up, and the demux that sorts every datagram to a rig: by the sender's IP first (the
ip -> rig map of all loaded rigs), else by the `rig` field the leader stamps on its events
(the only way to tell mock Pis apart in a smoke test, where every sender is 127.0.0.1).
Datagrams nobody claims are counted per IP, not dropped silently.

Also here: the per-rig SSE fan-out (every browser tab of a rig gets every event — the old
single queue split them between tabs) and the shared UDP command socket.
"""
from __future__ import annotations
import json
import queue
import socket
import struct
import sys
import threading
import time

from flask import Response

from controller.registry import registry, RigState, event_is_go, metrics_suffix

# Per-kind Slack throttle for display_health. Upstream is already edge-triggered, but the
# notifier is the boundary where a chatty producer becomes a pager storm. Recoveries are
# never throttled — a "fixed" message that arrives late is worse than one that arrives twice.
_DISPLAY_NOTIFY_THROTTLE_S = 60.0

# Shepherd metrics whose criticals stay OUT of Slack. They still reach the UI log over SSE
# and shepherd's own alerts file. soc_temp_c trips at 70 °C, which a fanless Pi reaches
# routinely under camera encode; the throttle bits (metric "throttled") still page.
_SLACK_MUTED_SHEPHERD_METRICS = {"soc_temp_c"}


def disable_udp_conn_reset(sock):
    """Windows only: stop a prior ICMP 'port unreachable' from poisoning this UDP socket."""
    if sys.platform != "win32":
        return
    try:
        sock.ioctl(socket.SIO_UDP_CONNRESET, struct.pack("I", 0))
    except (AttributeError, OSError):
        pass


# ── command socket (controller -> leader) ──

_cmd_sock = None
_cmd_lock = threading.Lock()


def send_command(ip: str, port: int, msg: dict) -> None:
    """Send a UDP command to a leader. Unbound socket, so one serves every rig. Tolerant of a
    Windows ConnectionResetError left by a previous packet: rebuild and retry once."""
    global _cmd_sock
    payload = json.dumps(msg).encode()
    with _cmd_lock:
        if _cmd_sock is None:
            _cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            disable_udp_conn_reset(_cmd_sock)
        try:
            _cmd_sock.sendto(payload, (ip, port))
        except ConnectionResetError:
            try:
                _cmd_sock.close()
            except OSError:
                pass
            _cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            disable_udp_conn_reset(_cmd_sock)
            _cmd_sock.sendto(payload, (ip, port))


# ── per-event handling (was the body of app/app.py's listener loop) ──

def _display_notify_due(rs: RigState, kind: str) -> bool:
    now = time.time()
    if now - rs.display_notify_t.get(kind, 0.0) < _DISPLAY_NOTIFY_THROTTLE_S:
        return False
    rs.display_notify_t[kind] = now
    return True


def handle_event(rs: RigState, event: dict) -> None:
    rs.last_event_t = time.time()
    typ = event.get("type")
    if typ == "trial":
        rs.trials.append(event)
        # Append GO-trial hit RTs (ms) to the live-RT temp file (matches the histogram;
        # a nogo-trial lick is a false alarm, not a hit).
        if rs.rt_hits_path and event.get("outcome") == "hit" and event_is_go(rs, event):
            rt = event.get("rt_ms")
            if isinstance(rt, (int, float)) and not isinstance(rt, bool) and rt == rt:
                try:
                    with open(rs.rt_hits_path, "a") as f:
                        f.write(f"{rt:.1f}\n")
                except Exception:
                    pass
    elif typ == "shepherd_alert":
        # Health alert from the shepherd monitor on a rig Pi. Flows to the SSE stream for the
        # UI log like any event; a CRITICAL also goes to Slack so it reaches you when the
        # browser is closed, unless its metric is muted.
        if (event.get("level") == "critical"
                and event.get("metric") not in _SLACK_MUTED_SHEPHERD_METRICS):
            rs.notify(f"🔴 {event.get('host', rs.name)} — {event.get('message', 'critical alert')}")
    elif typ == "display_abort":
        # The leader ended the session at a trial boundary because the display loop was broken.
        rs.notify(f"🛑 {rs.name} — EXPERIMENT ABORTED at trial "
                  f"{event.get('trial', '?')}: {event.get('reason', 'display fault')} "
                  f"({rs.session_id}){metrics_suffix(rs, rs.trials)}")
    elif typ == "display_health":
        # Display-stack health from the leader's heartbeat correlator or displayd's own
        # watchdogs. This branch is the ONLY thing that turns a dark projector into something
        # a human learns about, so an alarm pages like a critical shepherd alert.
        a = event.get("alarm") or {}
        kind = a.get("kind", "display_health")
        msg = a.get("msg", kind)
        where = event.get("source", "display")
        if a.get("level") == "alarm":
            if _display_notify_due(rs, kind):
                rs.notify(f"🔴 {rs.name} — display [{where}]: {msg} ({rs.session_id})")
        elif kind.endswith("_ok") or a.get("level") == "event":
            rs.display_notify_t.pop(kind.replace("_ok", "_lost"), None)
            rs.notify(f"🟢 {rs.name} — display [{where}]: {msg} ({rs.session_id})")
    elif typ == "global_timeout":
        rs.notify(f"⏱️ {rs.name} — GLOBAL TIMEOUT: {event.get('n_dry', '?')} dry "
                  f"trials, aborting at trial {event.get('trial', '?')} "
                  f"({rs.session_id}){metrics_suffix(rs, rs.trials)}")
    elif typ == "session_end":
        rs.session_end_seen = True
        # A run cut short by a dark projector must never page as a clean ✅ finish; the
        # display_abort branch already sent the reason.
        if event.get("end_reason") != "display_fault":
            rs.notify(f"✅ {rs.name} — session ended: {event.get('n_completed', '?')}"
                      f"/{event.get('n_planned', '?')} trials ({rs.session_id})"
                      f"{metrics_suffix(rs, rs.trials)}")
        # A natural end MUST tear down server-side (stop the camera, kill the engine); the
        # browser only flips its own phase. Separate thread: teardown does blocking HTTP.
        from controller.experiment import teardown_session   # lazy: avoids an import cycle
        threading.Thread(target=teardown_session, args=(rs, "session_end"), daemon=True).start()
    rs.broadcast(event)


# ── the one listening socket ──

class UdpDemux:
    def __init__(self, port: int):
        self.port = int(port)
        self._sock = None
        self._thread = None
        self._running = False
        self.stats = {"received": 0, "routed": 0, "unknown": 0, "bad_json": 0, "started_t": None}
        self._unknown_log_t: dict[str, float] = {}

    def start(self) -> None:
        if self._running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        disable_udp_conn_reset(sock)
        sock.settimeout(1.0)
        self._sock = sock
        self._running = True
        self.stats["started_t"] = time.time()
        self._thread = threading.Thread(target=self._loop, name="udp-demux", daemon=True)
        self._thread.start()
        print(f"[udp] listening on 0.0.0.0:{self.port} for all rigs", flush=True)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _loop(self) -> None:
        while self._running:
            try:
                data, (ip, _port) = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue          # Windows: a stray ICMP unreachable; keep listening
            except OSError:
                break
            self.stats["received"] += 1
            try:
                event = json.loads(data)
                if not isinstance(event, dict):
                    raise ValueError("not an object")
            except Exception:
                self.stats["bad_json"] += 1
                continue
            try:
                self.dispatch(ip, event)
            except Exception as e:
                print(f"[udp] handler error for {ip}: {e}", flush=True)

    def dispatch(self, ip: str, event: dict) -> None:
        rs = registry.by_ip(ip)
        how = "ip"
        if rs is None:
            claimed = event.get("rig")
            if isinstance(claimed, str) and claimed in registry.rigs:
                rs = registry.rigs[claimed]
                how = "rig_field"
        if rs is None:
            self.stats["unknown"] += 1
            registry.note_unknown(ip, event)
            now = time.time()
            if now - self._unknown_log_t.get(ip, 0.0) > 60.0:
                self._unknown_log_t[ip] = now
                print(f"[udp] datagram from unknown sender {ip} "
                      f"(type={event.get('type')!r}, rig={event.get('rig')!r}) — no loaded rig owns it",
                      flush=True)
            return
        self.stats["routed"] += 1
        event["rig"] = rs.name
        event["src_ip"] = ip
        event["_demux"] = how
        handle_event(rs, event)


demux: UdpDemux | None = None


def start_demux(port: int) -> UdpDemux:
    global demux
    if demux is None:
        demux = UdpDemux(port)
        demux.start()
    return demux


# ── SSE ──

def sse_response(rs: RigState) -> Response:
    """Server-sent events for one rig: a fresh subscriber queue per connection, fan-out from
    RigState.broadcast, keepalive every 5 s, unsubscribed when the browser goes away."""
    def generate():
        q = rs.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=5)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            rs.unsubscribe(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
