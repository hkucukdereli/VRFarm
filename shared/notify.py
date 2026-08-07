"""
Slack notifications via incoming webhook.

The webhook can be set two ways (configure() wins):
  - configure(url): runtime override, e.g. app/app.py pushes it from the rig
    config's `slack:` block at Load Rig. Read at call time, so it works even
    though this module is imported before the rig is loaded.
  - VRFARM_SLACK_WEBHOOK env var: legacy `export ...` before launch (fallback).
If neither yields a URL, notify() is a silent no-op. Posts are fire-and-forget in
a daemon thread so a slow or offline Slack can never block a Flask route or the UDP
listener, and never crashes a running session.
"""

import os
import threading

import requests

# Runtime override set by configure(); None = fall back to the env var.
_override = None


def configure(url):
    """Set the webhook at runtime. A URL string overrides the env var; '' disables notify()
    outright; None clears the override so the VRFARM_SLACK_WEBHOOK env var applies again."""
    global _override
    _override = None if url is None else url.strip()


def _webhook() -> str:
    return _override if _override is not None else os.environ.get("VRFARM_SLACK_WEBHOOK", "").strip()


def notify(text: str):
    """Post text to Slack. Never raises, never blocks the caller. No-op if no webhook is set."""
    url = _webhook()
    if not url:
        return

    def _post():
        try:
            requests.post(url, json={"text": text}, timeout=5)
        except Exception as e:
            print(f"[notify] Slack post failed: {e}")

    threading.Thread(target=_post, daemon=True).start()
