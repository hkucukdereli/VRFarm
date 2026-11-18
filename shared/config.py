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


def save_rig_atomic(config: dict, path: Union[str, Path]):
    """Save a rig YAML by writing a temp file next to it and renaming it into place, so a
    crash mid-write can never leave a half-written (unparseable) rig config behind."""
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".yaml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
                     n_trials_completed: int = 0, notes: str = "", rig: str = ""):
    """Record a completed session in the subject database (rig = which rig ran it)."""
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
    opto = task_config.get("opto") or {}

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
        "rig": rig,
        # optogenetics (task opto: section) — what was scheduled, not what fired (that is in the HDF5)
        "opto_enabled": bool(opto.get("enabled", False)),
        "opto_fraction": float(opto.get("fraction", 0) or 0) if opto.get("enabled", False) else 0.0,
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


def photodiode_init_payload(cfg: dict) -> dict:
    """The /api/init_photodiode payload both UIs send — one builder so the two can't
    drift (the experiment UI once sent only gpio + long-dead glitch/debounce keys).
    The optical-verify target (follower_ip/follower_api_port) is NOT included: the
    setup UI adds it (its init order guarantees the follower display is up first);
    the experiment UI must not (it shuts the setup display worker down at Load Rig
    and runs its own verify later, at leader-engine init, over UDP)."""
    return {
        "gpio": cfg.get("gpio", 24),
        "gpiochip": cfg.get("gpiochip", 0),
        "pulse_every_n_frames": cfg.get("pulse_every_n_frames", 5),
        "serial_port": cfg.get("serial_port"),          # Teensy USB for the amplitude stream
        "v_high": cfg.get("v_high", 3.0),
        "v_low": cfg.get("v_low", 0.3),
        "v_window_s": cfg.get("v_window_s", 10.0),
        "v_realert_s": cfg.get("v_realert_s", 30.0),
        "verify_duration_s": cfg.get("verify_duration_s", 1.0),
        "verify_enabled": cfg.get("verify_enabled", True),
        "sync_corner": cfg.get("sync_corner"),
        "sync_size_px": cfg.get("sync_size_px"),
        "sync_brightness": cfg.get("sync_brightness"),
    }


# ── Optogenetics (devices/opto.py + task opto: section) ──

def opto_init_payload(cfg: dict) -> dict:
    """The /api/init_opto payload both UIs send — one builder so the two can't drift (the
    photodiode_init_payload precedent)."""
    return {
        "pins": cfg.get("pins") or {},
        "gpiochip": cfg.get("gpiochip", 0),
        "max_on_s": cfg.get("max_on_s", 5.0),
    }


def rig_opto_device(rig: dict) -> Union[dict, None]:
    """The rig's usable opto device block, or None. 'Usable' is the ENGINE's own test
    (Leader.init_devices): a devices.opto block of type opto, `enabled` missing-or-true, and listed
    in the leader Pi's devices. The controller shows the Experiment tab's OPTOGENETICS section and
    accepts an opto-enabled task exactly when this is not None, so it refuses when the engine would."""
    devs = (rig or {}).get("devices") or {}
    cfg = devs.get("opto")
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return None
    if str(cfg.get("type", "opto")) != "opto":
        return None
    leader = next((p for p in ((rig or {}).get("pis") or []) if p.get("role") == "leader"), None)
    if not leader or "opto" not in (leader.get("devices") or []):
        return None
    return cfg


# ── IR illuminator (devices/ir_led.py) ──

# the rig-YAML keys devices/ir_led.py reads; pi_api's /api/init_ir_led accepts exactly these
IR_LED_CFG_KEYS = ("i2c_address", "i2c_bus", "dac_vdd", "v_min", "v_max", "curve", "intensity_pct")


def ir_led_init_payload(cfg: dict) -> dict:
    """The /api/init_ir_led payload (Connect, Setup's Initialize and the Experiment tab's camera
    preview all send it) — one builder so they can't drift. Only keys the rig YAML sets are
    passed on; the driver's own defaults cover the rest."""
    cfg = cfg or {}
    out = {k: cfg[k] for k in IR_LED_CFG_KEYS if cfg.get(k) is not None}
    out.setdefault("i2c_address", "0x60")
    return out


def rig_ir_led_device(rig: dict) -> Union[dict, None]:
    """The rig's usable IR illuminator block, or None — the ENGINE's own test again
    (rig_opto_device): a devices.ir_led block of type ir_led, `enabled` missing-or-true, listed in
    the leader Pi's devices. The Experiment tab's camera preview lights the LED exactly when this
    is not None."""
    devs = (rig or {}).get("devices") or {}
    cfg = devs.get("ir_led")
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return None
    if str(cfg.get("type", "ir_led")) != "ir_led":
        return None
    leader = next((p for p in ((rig or {}).get("pis") or []) if p.get("role") == "leader"), None)
    if not leader or "ir_led" not in (leader.get("devices") or []):
        return None
    return cfg


def validate_opto(task: dict, rig: Union[dict, None]) -> tuple:
    """(errors, warnings) for the task's opto: section against the rig. Empty errors = runnable.
    Pure (no Flask, no hardware) — Deploy, the device's init and tools/test_opto.py all use it.
    rig=None skips the rig checks (device presence, pins, cap)."""
    errors, warnings = [], []
    opto = (task or {}).get("opto") or {}
    if not opto.get("enabled", False):
        return errors, warnings
    from devices.opto import opto_spec_errors
    dev = rig_opto_device(rig) if rig is not None else None
    if rig is not None and dev is None:
        errors.append("opto is enabled in the task but this rig has no enabled opto device on its "
                      "leader (Setup tab: add / enable the Optogenetics device)")
    try:
        fraction = float(opto.get("fraction", 0))
    except (TypeError, ValueError):
        fraction = -1.0
    if not 0.0 <= fraction <= 1.0:
        errors.append("opto.fraction must be between 0 and 1")
    try:
        if int(opto.get("min_gap_trials", 0) or 0) < 0:
            errors.append("opto.min_gap_trials must be >= 0")
    except (TypeError, ValueError):
        errors.append("opto.min_gap_trials must be an integer")
    eligible = str(opto.get("eligible", "all") or "all").strip().lower()
    go_rule = str(((task or {}).get("stimulus") or {}).get("go_rule", "all") or "all").lower()
    if eligible not in ("all", "go", "nogo"):
        errors.append("opto.eligible must be all | go | nogo")
    elif eligible == "nogo" and go_rule == "all":
        errors.append("opto.eligible = nogo but stimulus.go_rule = all: every trial is a GO trial")
    pins = (dev or {}).get("pins") if dev else None
    max_on = float((dev or {}).get("max_on_s", 5.0) or 5.0)
    errors += opto_spec_errors(opto, pins, max_on)
    stim = (task or {}).get("stimulus") or {}
    if not stim.get("photodiode_sync_enabled", False):
        warnings.append("opto with photodiode stim sync OFF: the stimulus-onset window is timed "
                        "from the SHOW command plus the rig's latency estimate (±25 ms); turn "
                        "the sync on for ms timing")
    wins = opto.get("windows") or {}
    so, rw = wins.get("stim_onset") or {}, wins.get("response_window") or {}
    if so.get("enabled") and rw.get("enabled"):
        rd = float(((task or {}).get("reward") or {}).get("resp_delay_s", 0.6) or 0)
        a0, a1 = float(so.get("onset_s", 0)), float(so.get("onset_s", 0)) + float(so.get("duration_s", 0))
        b0 = rd + float(rw.get("onset_s", 0))
        b1 = b0 + float(rw.get("duration_s", 0))
        if a0 < b1 and b0 < a1:
            warnings.append("the stim_onset and response_window windows overlap (resp_delay_s "
                            f"{rd:g}): the second one is dropped while the pin is still on")
    return errors, warnings
