"""
shared/deploy_manifest.py

The single source of truth for which code files get deployed to a Pi's ~/rig.
Consumed by BOTH the setup UI (Install over scp, Deploy over REST) and the
experiment UI (Deploy step 0) — keep it that way; two diverging copies of this
list is how deploys used to break.
"""

from __future__ import annotations

# (local path relative to the repo root, remote path relative to ~/rig)
_COMMON = [
    ("shared/__init__.py", "shared/__init__.py"),
    ("shared/config.py", "shared/config.py"),
    ("shared/consolidate.py", "shared/consolidate.py"),
    ("shared/mjpeg_relay.py", "shared/mjpeg_relay.py"),
    ("devices/__init__.py", "devices/__init__.py"),
    ("devices/base.py", "devices/base.py"),
    ("pi_api/api.py", "pi_api/api.py"),
]

_LEADER = [
    ("engine/__init__.py", "engine/__init__.py"),
    ("engine/leader.py", "engine/leader.py"),
    ("shepherd/shepherd.py", "shepherd/shepherd.py"),
]


def deploy_files(role: str) -> list:
    """(local, remote) file pairs to ship to a Pi of the given role."""
    files = list(_COMMON)
    if role == "leader":
        files += _LEADER
    return files
