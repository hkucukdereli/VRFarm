"""
shared/config.py

Config loaders for the two config files:
  - Rig config (YAML): hardware, device configs, Pi roles
  - Task config (YAML): session (trial timing) + per-device params (no `states`;
    the trial engine is the imperative loop in engine/leader.py)

Also: subject database (session history tracking).
"""

from __future__ import annotations
import json
import yaml
from datetime import date, datetime
from pathlib import Path
from typing import Union


# ── Loaders ──

def load_rig(path: Union[str, Path]) -> dict:
    """Load rig config YAML. Returns raw dict.

    The FILENAME is the rig's identity. Everything that picks a rig does so by filename (the
    experiment/setup UIs list `rigs/*.yaml` by stem), so the in-file `name:` is only a label —
    and a rig yaml copied to a new filename silently keeps the OLD label. That is how Slack kept
    announcing "cheese" for sessions run on `cheddar.yaml`, and how `rig_name` in the archived
    HDF5s came out wrong.

    So: the filename stem always wins. A disagreeing `name:` is overridden and reported rather
    than silently honoured, because a silent mismatch is exactly the failure mode above.
    """
    path = Path(path)
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    stem = path.stem
    declared = str(cfg.get("name") or "").strip()
    if declared != stem:
        if declared:
            print(f"[config] rig '{path.name}' declares name: {declared!r}, which does not match "
                  f"its filename — using {stem!r}. Fix the `name:` field to silence this.",
                  flush=True)
        cfg["name"] = stem
    return cfg


def load_task(path: Union[str, Path]) -> dict:
    """Load task config YAML. Returns raw dict."""
    with open(path) as f:
        return yaml.safe_load(f)


def save_task(config: dict, path: Union[str, Path]):
    """Save task config back to YAML (after UI edits)."""
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def save_rig(config: dict, path: Union[str, Path]):
    """Save rig config back to YAML."""
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


# ── Helpers ──

def get_leader_pi(rig: dict) -> dict:
    """Return the Pi dict with role='leader'."""
    for pi in rig["pis"]:
        if pi["role"] == "leader":
            return pi
    raise ValueError("No leader Pi in rig config")


def get_follower_pis(rig: dict) -> list[dict]:
    """Return list of Pi dicts with role='follower'."""
    return [pi for pi in rig["pis"] if pi["role"] == "follower"]


def make_session_id(subject_id: str, date_str: str, session_num: int) -> str:
    return f"{subject_id}_{date_str}_{session_num:03d}"


# ── Subject database ──

def next_session_num(subject_id: str, today: str, db_dir: Union[str, Path]) -> int:
    """Get next session number for a subject on a given date."""
    db_file = Path(db_dir) / f"{subject_id}.json"
    if not db_file.exists():
        return 1
    with open(db_file) as f:
        db = json.load(f)
    same_day = [s for s in db.get("sessions", [])
                if s["date"].replace("-", "") == today]
    return len(same_day) + 1


def register_session(subject_id: str, session_id: str, session_num: int,
                     date_str: str, task_config: dict,
                     db_dir: Union[str, Path],
                     n_trials_completed: int = 0, notes: str = ""):
    """Record a completed session in the subject database."""
    db_dir = Path(db_dir)
    db_dir.mkdir(parents=True, exist_ok=True)
    db_file = db_dir / f"{subject_id}.json"

    if db_file.exists():
        with open(db_file) as f:
            db = json.load(f)
    else:
        db = {"subject_id": subject_id,
              "created": date.today().isoformat(),
              "sessions": []}

    sess = task_config.get("session", {})

    db["sessions"].append({
        "session_id": session_id,
        "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
        "session_num": session_num,
        "n_trials_planned": sess.get("n_trials", 0),
        "n_trials_completed": n_trials_completed,
        "notes": notes,
        "timestamp": datetime.now().isoformat(),
        # Full task config, so the paradigm details are queryable without schema assumptions.
        "task_config": task_config,
    })

    with open(db_file, "w") as f:
        json.dump(db, f, indent=2)


def get_subject_history(subject_id: str, db_dir: Union[str, Path]) -> list:
    """Get session history for a subject."""
    db_file = Path(db_dir) / f"{subject_id}.json"
    if not db_file.exists():
        return []
    with open(db_file) as f:
        return json.load(f).get("sessions", [])
