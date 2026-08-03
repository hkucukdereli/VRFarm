"""
shared/config.py

Config loaders for the two config files:
  - Rig config (YAML): hardware, pins, calibrations, Pi roles
  - Task config (YAML): stimulus / reward / session / adaptive params (no `states`;
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
    """Load rig config YAML. Returns raw dict."""
    with open(path) as f:
        return yaml.safe_load(f)


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
    stim = task_config.get("stimulus", {})
    contrast = stim.get("contrast", {})
    contrast_values = contrast.get("values", [])

    db["sessions"].append({
        "session_id": session_id,
        "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
        "session_num": session_num,
        "level": task_config.get("reward", {}).get("level", "0"),
        "n_trials_planned": sess.get("n_blocks", 6) * sess.get("block_size", 25),
        "n_trials_completed": n_trials_completed,
        "block_sequence": sess.get("block_sequence", []),
        "stim_size_deg": stim.get("size_deg", 0),
        "contrast_values": contrast_values,
        "notes": notes,
        "timestamp": datetime.now().isoformat(),
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
