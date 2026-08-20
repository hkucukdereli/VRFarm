#!/usr/bin/env python3
"""
engine/display_worker.py

Out-of-process pygame/display worker for the follower Pi's SETUP-time display.

WHY THIS EXISTS. pi_api used to run pygame/SDL on an in-process thread, so an SDL/X crash took the
whole management API down (HTTP "connection refused" until systemd respawned it). This worker moves
that pygame into its own process — mirroring how engine/follower.py already owns the display during a
real session — so a display crash kills only this worker; pi_api stays up, reports it, and respawns
us. The session path (follower.py, UDP :5575) is unchanged; this worker only serves setup commands.

PROTOCOL. pi_api sends one JSON request per UDP datagram to 127.0.0.1:<port> (default 5578):
    {"id": N, "action": "init"|"blank"|"checkers"|"stimulus"|"reload_warp"|"shutdown"|"sync_test"
                        |"sync_burst"|"stop_sync"|"ping", ...}
We reply with one JSON datagram that ECHOES "id" so pi_api can drain stale replies. The action set is
identical to the old pi_api display thread; `stop_sync` (unblock the flash loop) and `ping` (liveness)
are answered inline by the receiver thread so they work even while the pygame thread is mid sync-test.

ALL rendering lives in devices/display.py — this file is just a command loop, like follower.py.
"""
from __future__ import annotations
import argparse
import json
import os
import queue
import signal
import socket
import sys
import threading
from pathlib import Path

# Make devices.display importable (~/rig on the follower; this file is ~/rig/engine/display_worker.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_PORT = 5578


def _install_clean_exit():
    """SIGTERM/SIGINT -> quit pygame then exit, so pi_api's terminate() releases :0 cleanly. SDL is
    told not to install its own signal handlers (devices/display.py), so these fire normally."""
    def _term(*_):
        try:
            import pygame
            pygame.quit()
        finally:
            os._exit(0)
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)


def main():
    ap = argparse.ArgumentParser(description="Follower setup-time display worker (pygame).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    _install_clean_exit()
    from devices.display import Display

    cmd_q: queue.Queue = queue.Queue()      # (cmd, reply_addr) for the pygame (main) thread
    sync_stop = threading.Event()           # out-of-band stop for run_sync_test

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))

    def reply(addr, obj):
        try:
            sock.sendto(json.dumps(obj).encode(), addr)
        except OSError:
            pass

    def receiver():
        """ping/stop_sync answered here (work even while the pygame thread is blocked in a sync
        test); everything else is queued for the single pygame thread."""
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except OSError:
                break
            try:
                cmd = json.loads(data.decode())
            except Exception:
                continue
            action, rid = cmd.get("action"), cmd.get("id")
            if action == "ping":
                reply(addr, {"id": rid, "ok": True, "pong": True})
            elif action == "stop_sync":
                sync_stop.set()
                reply(addr, {"id": rid, "ok": True})
            else:
                cmd_q.put((cmd, addr))

    threading.Thread(target=receiver, daemon=True).start()

    # ---- pygame thread (this thread): every pygame call happens here ----
    dev = None
    warp_path = Path.home() / "rig" / "calibration" / "warp_map.npz"
    print(f"[display_worker] listening on 127.0.0.1:{args.port}", flush=True)
    try:
        while True:
            cmd, addr = cmd_q.get()
            action, rid = cmd.get("action"), cmd.get("id")

            def R(obj):
                obj["id"] = rid
                reply(addr, obj)

            try:
                if action == "init":
                    os.environ["DISPLAY"] = ":0"
                    dev = Display()
                    dev.init(rig_config=cmd.get("rig_config", {}), task_params={})
                    dev.start_display()
                    warp_loaded = dev.load_warp(str(warp_path))
                    msg = "Display initialized"
                    if warp_loaded:
                        msg += " (warp map loaded)"
                    R({"ok": True, "message": msg})
                elif action == "blank":
                    if not dev:
                        R({"ok": False, "error": "Display not initialized"})
                    else:
                        dev.blank_with_gray(cmd.get("gray_value", 0.0))
                        R({"ok": True})
                elif action == "checkers":
                    if not dev:
                        R({"ok": False, "error": "Display not initialized"})
                    else:
                        dev.show_checkers(use_warp=cmd.get("apply_warp", True))
                        R({"ok": True})
                elif action == "stimulus":
                    if not dev:
                        R({"ok": False, "error": "Display not initialized"})
                    else:
                        dev.show_patch_spherical(
                            cmd.get("az_deg", 0.0), cmd.get("alt_deg", 0.0),
                            cmd.get("size_deg", 8.0), cmd.get("corr_contrast", 0.5),
                            cmd.get("bg_gray", 0.0), shape=cmd.get("shape", "square"),
                            apply_lum=cmd.get("apply_lum", True))
                        R({"ok": True})
                elif action == "reload_warp":
                    # Re-read warp_map.npz into the running Display (load_warp caches in memory and
                    # never re-reads on its own), so a freshly Generated warp takes effect w/o reinit.
                    if not dev:
                        R({"ok": True, "reloaded": False, "message": "display not active"})
                    else:
                        loaded = dev.load_warp(str(warp_path))
                        R({"ok": True, "reloaded": bool(loaded),
                           "message": ("warp reloaded" if loaded else "warp_map.npz not found")})
                elif action == "shutdown":
                    if dev:
                        dev.shutdown()
                        dev = None
                    R({"ok": True})
                elif action == "sync_test":
                    if not dev or getattr(dev, "_screen", None) is None:
                        R({"ok": False, "error": "Display not initialized"})
                    else:
                        # Ack BEFORE the (blocking) flash loop so the HTTP start returns immediately;
                        # the loop owns this thread until stop_sync sets the Event out-of-band.
                        sync_stop.clear()
                        if hasattr(dev, "set_sync_layout"):
                            dev.set_sync_layout(cmd.get("sync_corner"), cmd.get("sync_size_px"),
                                                cmd.get("sync_brightness"))
                        R({"ok": True, "message": "sync test running"})
                        try:
                            dev.run_sync_test(int(cmd.get("every_n", 5)), sync_stop)
                        except Exception as e:
                            print("[display_worker] sync_test error:", e, flush=True)
                        # result already sent; do NOT reply again (would desync)
                elif action == "sync_burst":
                    if not dev or getattr(dev, "_screen", None) is None:
                        R({"ok": False, "error": "Display not initialized"})
                    else:
                        # Bounded flash for the photodiode INIT verify: run to completion, then reply
                        # with the emitted flash count (NOT ack-first — the caller needs the number).
                        if hasattr(dev, "set_sync_layout"):
                            dev.set_sync_layout(cmd.get("sync_corner"), cmd.get("sync_size_px"),
                                                cmd.get("sync_brightness"))
                        try:
                            flashes = dev.run_sync_burst(int(cmd.get("every_n", 5)),
                                                         float(cmd.get("duration_s", 1.0)))
                            R({"ok": True, "flashes": int(flashes)})
                        except Exception as e:
                            R({"ok": False, "error": str(e)})
                else:
                    R({"ok": False, "error": f"Unknown action: {action}"})
            except Exception as e:
                R({"ok": False, "error": str(e)})
    finally:
        try:
            import pygame
            pygame.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
