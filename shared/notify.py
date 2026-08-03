"""
Slack notifications via incoming webhook.

Set VRFARM_SLACK_WEBHOOK to the webhook URL before launching the app;
if unset, notify() is a silent no-op. Posts are fire-and-forget in a
daemon thread so a slow or offline Slack can never block a Flask route
or the UDP listener, and never crashes a running session.
"""

import os
import threading

import requests

WEBHOOK_URL = os.environ.get("VRFARM_SLACK_WEBHOOK", "").strip()


def notify(text: str):
    """Post text to Slack. Never raises, never blocks the caller."""
    if not WEBHOOK_URL:
        return

    def _post():
        try:
            requests.post(WEBHOOK_URL, json={"text": text}, timeout=5)
        except Exception as e:
            print(f"[notify] Slack post failed: {e}")

    threading.Thread(target=_post, daemon=True).start()
