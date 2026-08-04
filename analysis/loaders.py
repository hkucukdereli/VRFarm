"""Loaders for VRFarm session data.

As of format_version 2 a transferred session is a single self-contained HDF5:

    /                       root attrs: session_id, subject_id, date, session_num,
                            notes, level, rig_name, timestamp, n_trials_completed,
                            n_trials_planned, saved_devices, skipped_devices,
                            task_config (JSON), format_version
    /trials/                recorded per-trial data (one row per completed trial)
    /stimulus/              pre-generated plan (per-trial + block arrays; scalar attrs)
    /lick/  /reward/  /photodiode/   per-device data (present only if that device was saved)
    /camera/frame_timestamps         Nx3 [frame_idx, wall_clock_s, sensor_ts_ns]

Older sessions (flat datasets at the root, no groups) still load via a fallback.
"""

from pathlib import Path
import json
import numpy as np
import h5py


# ── low-level helpers ────────────────────────────────────────────────────

def _resolve_h5(path):
    path = Path(path)
    if path.is_dir():
        h5s = sorted(path.glob("*.h5"))
        if not h5s:
            raise FileNotFoundError(f"No .h5 file in {path}")
        path = h5s[0]
    return path


def _is_grouped(f):
    """format_version 2 (grouped) vs legacy flat."""
    return f.attrs.get("format_version", 0) >= 2 or "trials" in f


def _read(ds):
    """Read a dataset: vlen strings -> list[str]; vlen numeric -> list[ndarray]; else ndarray."""
    if h5py.check_string_dtype(ds.dtype):
        return [s.decode() if isinstance(s, bytes) else s for s in ds[:]]
    if h5py.check_vlen_dtype(ds.dtype) is not None:
        return [np.asarray(ds[i]) for i in range(len(ds))]
    return ds[:]


def _read_attrs(obj):
    out = {}
    for k, v in obj.attrs.items():
        if isinstance(v, str) and k in ("task_config", "saved_devices", "skipped_devices"):
            try:
                v = json.loads(v)
            except Exception:
                pass
        out[k] = v
    return out


# ── HDF5 (experiment recording) ──────────────────────────────────────────

def load_session(path):
    """Load a session HDF5 into a nested dict.

    Returns {"attrs": {...root metadata...}, "trials": {...}, "stimulus": {...},
    "lick"/"reward"/"photodiode"/"camera": {...}}. Each group dict maps dataset name
    -> array (vlen datasets -> list of arrays; trial_outcome -> list of str); a group's
    own attributes (e.g. /stimulus scalars) are under its "_attrs" key.
    Legacy flat files return everything under "trials".
    """
    path = _resolve_h5(path)
    out = {}
    with h5py.File(path, "r") as f:
        out["attrs"] = _read_attrs(f)
        if _is_grouped(f):
            for gname, node in f.items():
                if isinstance(node, h5py.Group):
                    g = {k: _read(node[k]) for k in node}
                    g["_attrs"] = _read_attrs(node)
                    out[gname] = g
        else:
            out["trials"] = {name: _read(f[name]) for name in f}
    return out


def load_trials(path):
    """Per-trial recorded data as a numpy structured array (one row per trial)."""
    path = _resolve_h5(path)
    with h5py.File(path, "r") as f:
        grp = f["trials"] if "trials" in f else f
        names = [k for k in sorted(grp.keys()) if not k.startswith("_")]
        n = len(grp["trial_num"]) if "trial_num" in grp else 0
        if n == 0:
            return np.array([])
        dtypes, columns = [], []
        for name in names:
            ds = grp[name]
            if h5py.check_vlen_dtype(ds.dtype) is not None and not h5py.check_string_dtype(ds.dtype):
                continue                      # skip per-trial vlen (lick_times etc.)
            if len(ds) != n:
                continue
            if h5py.check_string_dtype(ds.dtype):
                dtypes.append((name, "U16"))
                columns.append([s.decode() if isinstance(s, bytes) else s for s in ds[:]])
            else:
                dtypes.append((name, ds.dtype))
                columns.append(ds[:])
        arr = np.zeros(n, dtype=dtypes)
        for (name, _), col in zip(dtypes, columns):
            arr[name] = col
    return arr


def session_info(path):
    """Summary dict: session_id, n_trials, duration_s, hit_rate, total_rewards_ul, groups."""
    path = _resolve_h5(path)
    info = {}
    with h5py.File(path, "r") as f:
        attrs = _read_attrs(f)
        info["session_id"] = attrs.get("session_id")
        info["groups"] = [k for k in f if isinstance(f[k], h5py.Group)] or list(f.keys())
        grp = f["trials"] if "trials" in f else f
        n = len(grp["trial_num"]) if "trial_num" in grp else 0
        info["n_trials"] = n
        if n == 0:
            return info
        info["duration_s"] = float(grp["outcome_t"][-1] - grp["iti_start_t"][0])
        outcomes = [s.decode() if isinstance(s, bytes) else s for s in grp["trial_outcome"][:]]
        info["hit_rate"] = sum(1 for o in outcomes if o == "hit") / n
        rw = f.get("reward/reward_amount_ul", f.get("reward_amount_ul"))
        if rw is not None:
            info["total_rewards_ul"] = float(np.nansum(rw[:]))
    return info


def response_times(path, ref="onset", within_window=False):
    """Per-trial first-lick latency (s) — the response/reaction time.

    ref="onset"  : measured from the stimulus onset (true_onset_t, the photodiode-corrected
                   onset, with a stim_onset_t fallback where the sync failed).
    ref="window" : measured from when the response window opens (response_window_t).

    within_window=True restricts the qualifying lick to the response window: it must land before
    the window closes (response_window_t + reward.response_window). Because per-trial licks are
    recorded across the WHOLE trial (pre-stim → stim → response window → post-stim), the default
    (False) can return a latency into the post-stim period — a first post-onset lick that occurs
    after the window has closed — so RTs can exceed resp_delay + response_window. Set True to
    exclude those late licks.

    Uses the full per-trial lick_times when the lick device was saved (first lick at/after the
    reference — so pre-stim/anticipatory licks don't count); otherwise falls back to the recorded
    first_lick_t. Returns a length-n_trials array; NaN for trials with no qualifying lick.
    """
    s = load_session(path)
    tr = s.get("trials", {})
    n = len(tr.get("trial_num", []))
    if n == 0:
        return np.array([])
    win_open = np.asarray(tr["response_window_t"], float)
    if ref == "window":
        ref_t = win_open
    else:
        onset = np.asarray(tr["true_onset_t"], float)
        ref_t = np.where(np.isfinite(onset), onset, np.asarray(tr["stim_onset_t"], float))

    hi = np.full(n, np.inf)
    if within_window:
        rw = float(((s.get("attrs", {}).get("task_config") or {}).get("reward", {})
                    ).get("response_window", 1.4))
        hi = win_open + rw   # window close time; licks at/after this are post-stim

    lick_times = s.get("lick", {}).get("lick_times")   # list of per-trial arrays, or None
    first_lick = np.asarray(tr.get("first_lick_t", np.full(n, np.nan)), float)
    out = np.full(n, np.nan)
    for i in range(n):
        r = ref_t[i]
        if not np.isfinite(r):
            continue
        if lick_times is not None:
            a = np.asarray(lick_times[i], float)
            a = a[(a >= r) & (a < hi[i])]
            if a.size:
                out[i] = a[0] - r
        elif np.isfinite(first_lick[i]) and r <= first_lick[i] < hi[i]:
            out[i] = first_lick[i] - r
    return out


# ── stimulus plan + camera timestamps (now inside the h5; .npz/.npy fallback) ──

def load_stims(path):
    """Pre-generated stimulus plan. Reads /stimulus from the session h5 (v2); falls back to a
    legacy stimuli.npz. Returns a dict of arrays plus unpacked scalar params."""
    path = Path(path)
    if path.is_dir():
        h5s = sorted(path.glob("*.h5"))
        if h5s:
            with h5py.File(h5s[0], "r") as f:
                if "stimulus" in f:
                    d = {k: _read(f["stimulus"][k]) for k in f["stimulus"]}
                    d.update(_read_attrs(f["stimulus"]))
                    return d
        path = path / "stimuli.npz"
    data = dict(np.load(path, allow_pickle=True))
    for key in ("n_trials", "background_gray", "global_delay",
                "block_delay_skip_first", "sync_square_every_n"):
        if key in data and data[key].size == 1:
            data[key] = data[key].item()
    return data


def load_video_timestamps(path):
    """Camera frame timestamps as {frame_idx, t (wall clock), t_sensor, avg_fps}.

    Reads /camera/frame_timestamps from the session h5 (v2); falls back to a legacy
    frame_timestamps.npy. Columns: [frame_idx, host wall clock (time.time(), NTP-synced,
    same timebase as all trial events), SensorTimestamp ns on CLOCK_BOOTTIME]. t_sensor maps
    the sensor clock onto the wall clock via the session-median offset (accurate frame spacing,
    no ISP jitter); None if the sensor column is absent.
    """
    path = Path(path)
    arr = None
    if path.is_dir():
        h5s = sorted(path.glob("*.h5"))
        if h5s:
            with h5py.File(h5s[0], "r") as f:
                if "camera/frame_timestamps" in f:
                    arr = f["camera/frame_timestamps"][:]
        if arr is None:
            path = path / "frame_timestamps.npy"
    if arr is None and str(path).endswith(".h5"):
        with h5py.File(path, "r") as f:
            arr = f["camera/frame_timestamps"][:]
    if arr is None:
        arr = np.load(path)

    n = len(arr)
    out = {
        "frame_idx": arr[:, 0].astype(int),
        "t": arr[:, 1],
        "t_sensor": None,
        "avg_fps": float((n - 1) / max(arr[-1, 1] - arr[0, 1], 1e-9)) if n > 1 else float("nan"),
    }
    if arr.ndim == 2 and arr.shape[1] >= 3 and np.any(arr[:, 2] > 0):
        sens_s = arr[:, 2] / 1e9
        out["t_sensor"] = sens_s + np.median(arr[:, 1] - sens_s)
    return out
