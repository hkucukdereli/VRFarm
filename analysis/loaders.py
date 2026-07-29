"""Loaders for VRFarm experiment data files (HDF5 + NPZ)."""

from pathlib import Path
import numpy as np
import h5py


# ── NPZ (stimulus parameters) ──────────────────────────────────────────


def load_stims(path):
    """Load a stimuli.npz file.

    Parameters
    ----------
    path : str or Path
        Path to the .npz file, or a session directory containing stimuli.npz.

    Returns
    -------
    dict
        All arrays from the NPZ, with scalar wrappers unpacked.
        Scalars stored as 1-element arrays (n_trials, background_gray,
        global_delay, block_delay_skip_first, sync_square_every_n)
        are returned as plain Python values.
    """
    path = Path(path)
    if path.is_dir():
        path = path / "stimuli.npz"
    data = dict(np.load(path, allow_pickle=True))

    # Unpack 1-element arrays to scalars
    for key in ("n_trials", "background_gray", "global_delay",
                "block_delay_skip_first", "sync_square_every_n"):
        if key in data and data[key].size == 1:
            data[key] = data[key].item()

    return data


# ── Video frame timestamps ─────────────────────────────────────────────


def load_video_timestamps(path):
    """Load a session's frame_timestamps.npy (written by devices/camera.py).

    Parameters
    ----------
    path : str or Path
        Path to frame_timestamps.npy, or a session directory containing it.

    Returns
    -------
    dict
        frame_idx : (n,) int array — index into the H.264 video frames.
        t : (n,) float64 — host wall clock at frame callback (time.time(),
            NTP-synced; same timebase as all HDF5 events). Includes ISP
            pipeline latency (~tens of ms, roughly constant) + jitter.
        t_sensor : (n,) float64 or None — hardware SensorTimestamp (start of
            frame readout) mapped onto the wall clock via the session-median
            offset: accurate frame spacing, no pipeline jitter. None for
            sessions recorded before the 3-column format.
        avg_fps : float — from first-to-last wall-clock spacing.
    """
    path = Path(path)
    if path.is_dir():
        path = path / "frame_timestamps.npy"
    arr = np.load(path)
    n = len(arr)
    out = {
        "frame_idx": arr[:, 0].astype(int),
        "t": arr[:, 1],
        "t_sensor": None,
        "avg_fps": float((n - 1) / max(arr[-1, 1] - arr[0, 1], 1e-9))
                   if n > 1 else float("nan"),
    }
    # 3-column format: [idx, wall, SensorTimestamp ns on CLOCK_BOOTTIME]
    if arr.ndim == 2 and arr.shape[1] >= 3 and np.any(arr[:, 2] > 0):
        sens_s = arr[:, 2] / 1e9
        offset = np.median(arr[:, 1] - sens_s)   # boot→wall anchor
        out["t_sensor"] = sens_s + offset
    return out


# ── HDF5 (experiment recording) ────────────────────────────────────────


# Datasets that store one variable-length array per trial
_VLEN_DATASETS = {"lick_times", "iti_lick_times", "sync_pulses"}

# Datasets that are session-level (not per-trial)
_SESSION_DATASETS = {"_iti_durations_planned"}


def load_session(path):
    """Load a session HDF5 file into a dict of arrays.

    Parameters
    ----------
    path : str or Path
        Path to the .h5 file, or a session directory containing one .h5.

    Returns
    -------
    dict
        Keys are dataset names, values are numpy arrays.
        Variable-length datasets (lick_times, iti_lick_times, sync_pulses)
        are returned as Python lists of arrays.
        trial_outcome is returned as a list of strings.
        Session-level datasets (prefixed with _) are included as-is.
    """
    path = _resolve_h5(path)
    out = {}
    with h5py.File(path, "r") as f:
        for name in f:
            ds = f[name]
            if name in _VLEN_DATASETS:
                out[name] = [np.array(ds[i]) for i in range(len(ds))]
            elif ds.dtype.kind == "O":  # vlen strings (trial_outcome)
                out[name] = [s.decode() if isinstance(s, bytes) else s
                             for s in ds[:]]
            else:
                out[name] = ds[:]
    return out


def load_trials(path):
    """Load per-trial scalar data from a session HDF5 as a structured array.

    Parameters
    ----------
    path : str or Path
        Path to the .h5 file, or a session directory containing one .h5.

    Returns
    -------
    numpy structured array
        One row per trial. Columns are all scalar (non-vlen) per-trial
        datasets. trial_outcome is stored as U8 (unicode string).
    """
    path = _resolve_h5(path)
    with h5py.File(path, "r") as f:
        n = _n_trials(f)
        if n == 0:
            return np.array([])

        # Collect scalar per-trial datasets
        names = []
        dtypes = []
        columns = []
        for name in sorted(f.keys()):
            if name in _VLEN_DATASETS or name in _SESSION_DATASETS:
                continue
            ds = f[name]
            if len(ds) != n:
                continue
            if ds.dtype.kind == "O":
                names.append(name)
                dtypes.append((name, "U16"))
                columns.append([s.decode() if isinstance(s, bytes) else s
                                for s in ds[:]])
            else:
                names.append(name)
                dtypes.append((name, ds.dtype))
                columns.append(ds[:])

        arr = np.zeros(n, dtype=dtypes)
        for name, col in zip(names, columns):
            arr[name] = col
    return arr


def session_info(path):
    """Return a summary dict for a session HDF5 file.

    Keys: n_trials, datasets (list of names), duration_s,
    hit_rate, total_rewards_ul.
    """
    path = _resolve_h5(path)
    info = {}
    with h5py.File(path, "r") as f:
        info["datasets"] = list(f.keys())
        n = _n_trials(f)
        info["n_trials"] = n
        if n == 0:
            return info

        t0 = f["iti_start_t"][0]
        t1 = f["outcome_t"][-1]
        info["duration_s"] = float(t1 - t0)

        outcomes = [s.decode() if isinstance(s, bytes) else s
                    for s in f["trial_outcome"][:]]
        info["hit_rate"] = sum(1 for o in outcomes if o == "hit") / n

        if "reward_amount_ul" in f:
            info["total_rewards_ul"] = float(np.nansum(f["reward_amount_ul"][:]))

    return info


# ── Helpers ─────────────────────────────────────────────────────────────


def _resolve_h5(path):
    path = Path(path)
    if path.is_dir():
        h5s = list(path.glob("*.h5"))
        if not h5s:
            raise FileNotFoundError(f"No .h5 file in {path}")
        path = h5s[0]
    return path


def _n_trials(f):
    """Number of trials in an open HDF5 file."""
    if "trial_num" in f:
        return len(f["trial_num"])
    return 0
