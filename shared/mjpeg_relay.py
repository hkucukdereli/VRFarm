"""
shared/mjpeg_relay.py

Reconnecting MJPEG relay for the controller UIs. Both Flask apps (app/app.py, setup/app.py) proxy the
Pi's /api/camera_stream so the browser <img> stays same-origin. A one-shot proxy turned every transient
Pi hiccup — pi_api restart at Deploy, the preview->record swap at Go, a brief reconfigure, or the <img>
connecting before the preview is up (Pi returns 400 "Camera not streaming") — into a silent HTTP-200
blank the browser couldn't tell from success, so the feed never came back. This relay reconnects across
those gaps and only forwards a live 200 stream, so the preview rides through hiccups and resumes on its
own instead of freezing/blanking.
"""

from __future__ import annotations
import time

import requests


def relay(url, connect_timeout=3.05, read_timeout=20.0, idle_giveup=20.0, retry_s=0.4):
    """Yield MJPEG chunks from `url`, reconnecting across transient failures.

    - Split (connect, read) timeout: a short connect timeout, a generous read timeout so normal
      inter-frame gaps never trip it (the old flat timeout=5 killed the stream on any >5 s gap).
    - Non-200 upstream (camera not streaming yet) -> retry instead of forwarding the error body as if
      it were image bytes; this covers the <img>-before-preview ordering race.
    - Reconnect on a clean upstream end (Deploy/Go teardown) or a transient connection/read error.
    - Give up only after `idle_giveup` s with no frame at all, so a genuinely dead camera ends the
      stream (letting the client's manual reset / re-Live take over) rather than spinning forever.
    - Return on GeneratorExit (browser <img> disconnected) so we don't leak the upstream connection.
    """
    last_frame = time.time()
    while True:
        try:
            r = requests.get(url, stream=True, timeout=(connect_timeout, read_timeout))
            if r.status_code != 200:
                r.close()
                if time.time() - last_frame > idle_giveup:
                    return
                time.sleep(retry_s)
                continue
            for chunk in r.iter_content(chunk_size=4096):
                if chunk:
                    last_frame = time.time()
                    yield chunk
            # Upstream ended cleanly (camera stopped/reconfigured) — reconnect to the fresh stream.
            if time.time() - last_frame > idle_giveup:
                return
            time.sleep(0.3)
        except GeneratorExit:
            return   # browser <img> disconnected (Stop, page unload, re-point)
        except Exception:
            # Connect/read timeout or connection error — transient; reconnect until idle_giveup.
            if time.time() - last_frame > idle_giveup:
                return
            time.sleep(retry_s)
