"""
analysis/loaders.py

Pandas IO for VRFarm v2 session files (one self-contained ``<session_id>.h5`` per
session — see docs/DATA_FORMAT.md).

The data lives at several granularities, so it loads as a small family of tidy
DataFrames rather than one table:

    Session(path)            one .h5 -> DataFrames, each tagged with session_id/subject_id
      .trials      one row / completed trial  (the analysis-ready wide table:
                   /trials + reward + lick/pulse summaries + planned stimulus + derived cols)
      .running     one row / wheel-encoder sample   (continuous, ~tens of thousands of rows)
      .licks       one row / lick                   (long form, in-trial + ITI)
      .pulses      one row / photodiode sync pulse   (long form)
      .stimulus    one row / planned trial
      .camera      one row / recorded video frame
      .meta        dict of root attributes (task_config parsed from JSON)

    Dataset(source)          many sessions -> the same tables, concatenated
      ds = Dataset("data/HK1")            # dir (recursive *.h5), glob, list, or Session(s)
      ds.trials                            # every session stacked, + session_id column
      ds.add("HK1_20260811_001.h5")        # append another session

Only format_version 2 is supported (consolidation is automatic at session end).
"""
from __future__ import annotations

import glob
import json
from functools import cached_property
from pathlib import Path

import h5py
import numpy as np
import pandas as pd

__all__ = ["Session", "Dataset"]

# AS5600 is a 12-bit absolute encoder: 4096 counts per wheel revolution (the ground
# truth in /trials/running_counts). rotations = counts / COUNTS_PER_REV.
COUNTS_PER_REV = 4096

# Per-trial columns pulled from the planned /stimulus group into the wide trials table
# (sliced to the trials that actually ran). Everything else in /stimulus stays in .stimulus.
_STIM_INTO_TRIALS = ("stim_alt_deg", "px_x", "px_y", "px_size",
                     "duration_s", "prestim_s", "poststim_s")

# Root attrs stored as JSON strings by consolidate.py -> parse back to Python objects.
_JSON_ATTRS = ("task_config", "saved_devices", "skipped_devices")


# ── helpers ──────────────────────────────────────────────────────────────────

def _decode(v):
    """bytes -> str, numpy scalar -> python scalar; pass everything else through."""
    if isinstance(v, bytes):
        return v.decode()
    if isinstance(v, np.generic):
        return v.item()
    return v


def _vlen_rows(ds):
    """A vlen HDF5 dataset -> list of 1-D numpy arrays (one per trial)."""
    return [np.asarray(ds[i]) for i in range(len(ds))]


def _session_h5(path) -> Path:
    """Resolve a session dir or a .h5 path to the .h5 file."""
    p = Path(path)
    if p.is_dir():
        h5s = sorted(p.glob("*.h5"))
        if not h5s:
            raise FileNotFoundError(f"No .h5 file in {p}")
        return h5s[0]
    return p


# ── one session ──────────────────────────────────────────────────────────────

class Session:
    """One consolidated ``<session_id>.h5``. Tables are built once, lazily, and cached."""

    def __init__(self, path):
        self.h5_path = _session_h5(path)

    def __repr__(self):
        return f"Session({self.session_id!r}, n_trials={self.n_trials})"

    # identity -----------------------------------------------------------------
    @property
    def session_id(self) -> str:
        return str(self.meta.get("session_id", self.h5_path.stem))

    @property
    def subject_id(self) -> str:
        return str(self.meta.get("subject_id", ""))

    @property
    def n_trials(self) -> int:
        return len(self._tables["trials"])

    # tables (public) ----------------------------------------------------------
    @property
    def trials(self) -> pd.DataFrame:
        return self._tables["trials"]

    @property
    def running(self) -> pd.DataFrame:
        return self._tables["running"]

    @property
    def licks(self) -> pd.DataFrame:
        return self._tables["licks"]

    @property
    def pulses(self) -> pd.DataFrame:
        return self._tables["pulses"]

    @property
    def stimulus(self) -> pd.DataFrame:
        return self._tables["stimulus"]

    @property
    def camera(self) -> pd.DataFrame:
        return self._tables["camera"]

    @cached_property
    def meta(self) -> dict:
        with h5py.File(self.h5_path, "r") as f:
            out = {}
            for k, v in f.attrs.items():
                v = _decode(v)
                if k in _JSON_ATTRS and isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except (ValueError, TypeError):
                        pass
                out[k] = v
        return out

    @cached_property
    def stim_attrs(self) -> dict:
        """Scalar /stimulus group attrs + its non-per-trial datasets (block_delays, …)."""
        return self._tables["_stim_attrs"]

    # build --------------------------------------------------------------------
    def _ident(self, df: pd.DataFrame) -> pd.DataFrame:
        """Prepend session_id / subject_id so tables from different sessions stack."""
        if len(df):
            df.insert(0, "subject_id", self.subject_id)
            df.insert(0, "session_id", self.session_id)
        return df

    @cached_property
    def _tables(self) -> dict:
        with h5py.File(self.h5_path, "r") as f:
            grp = f["trials"] if "trials" in f else f
            n = len(grp["trial_num"]) if "trial_num" in grp else 0

            # /trials -> per-trial scalar cols (len == n) vs continuous stream (len != n)
            per_trial, running = {}, {}
            for name, ds in grp.items():
                if name.startswith("_"):
                    continue
                is_str = h5py.check_string_dtype(ds.dtype) is not None
                is_vlen = h5py.check_vlen_dtype(ds.dtype) is not None
                if is_vlen and not is_str:
                    continue
                if len(ds) == n:
                    per_trial[name] = ([_decode(s) for s in ds[:]] if is_str else ds[:])
                elif name.startswith("running_"):
                    running[name] = ds[:]

            df = pd.DataFrame(per_trial)

            # per-trial event times other tables need (may be absent)
            onset = np.asarray(per_trial.get("true_onset_t", np.full(n, np.nan)), float)
            win = np.asarray(per_trial.get("response_window_t", np.full(n, np.nan)), float)
            sw = np.asarray(per_trial.get("stim_onset_t", np.full(n, np.nan)), float)
            tnums = np.asarray(per_trial.get("trial_num", np.arange(n)))

            # merge /reward
            if "reward" in f:
                r = f["reward"]
                if "reward_t" in r:
                    df["reward_t"] = r["reward_t"][:]
                if "reward_amount_ul" in r:
                    df["reward_ul"] = r["reward_amount_ul"][:]
                if "reward_pavlovian" in r:
                    df["pavlovian"] = r["reward_pavlovian"][:].astype(bool)

            # merge /lick summaries + collect long-form rows
            lick_rows = []
            if "lick" in f:
                lk = f["lick"]
                if "iti_lick_count" in lk:
                    df["iti_lick_count"] = lk["iti_lick_count"][:]
                lt = _vlen_rows(lk["lick_times"]) if "lick_times" in lk else [np.array([])] * n
                it = _vlen_rows(lk["iti_lick_times"]) if "iti_lick_times" in lk else [np.array([])] * n
                df["n_licks"] = [len(lt[i]) for i in range(n)]
                df["n_iti_licks"] = [len(it[i]) for i in range(n)]
                for i in range(n):
                    for t in lt[i]:
                        lick_rows.append((tnums[i], "trial", float(t),
                                          float(t) - onset[i], float(t) - win[i]))
                    for t in it[i]:
                        lick_rows.append((tnums[i], "iti", float(t),
                                          float(t) - onset[i], float(t) - win[i]))

            # merge /photodiode summary + collect long-form rows
            pulse_rows = []
            if "photodiode" in f and "sync_pulses" in f["photodiode"]:
                sp = _vlen_rows(f["photodiode"]["sync_pulses"])
                df["n_pulses"] = [len(sp[i]) for i in range(n)]
                df["first_pulse_t"] = [float(sp[i][0]) if len(sp[i]) else np.nan
                                       for i in range(n)]
                for i in range(n):
                    for k, t in enumerate(sp[i]):
                        pulse_rows.append((tnums[i], k, float(t),
                                           (float(t) - sw[i]) * 1e3))

            # merge planned /stimulus (per-trial cols, sliced to the trials that ran)
            stim_df, stim_attrs = pd.DataFrame(), {}
            if "stimulus" in f:
                sg = f["stimulus"]
                for name in _STIM_INTO_TRIALS:
                    if name in sg and len(sg[name]) >= n:
                        df[name] = sg[name][:n]
                stim_df, stim_attrs = self._read_stimulus(sg)

            # derived, human-readable columns
            if "display_latency_s" in df:
                df["latency_ms"] = df["display_latency_s"] * 1e3
            if {"first_lick_t", "response_window_t"} <= set(df.columns):
                df["rt_ms"] = (df["first_lick_t"] - df["response_window_t"]) * 1e3
            if {"reward_t", "true_onset_t"} <= set(df.columns):
                rt = df["reward_t"].where(df["reward_t"] > 0)
                df["reward_lat_ms"] = (rt - df["true_onset_t"]) * 1e3
            if {"first_pulse_t", "stim_onset_t"} <= set(df.columns):
                df["first_pulse_lat_ms"] = (df["first_pulse_t"] - df["stim_onset_t"]) * 1e3
            if "trial_outcome" in df:
                df["hit"] = df["trial_outcome"] == "hit"

            # running-wheel stream
            run_df = pd.DataFrame({k: running[k] for k in sorted(running)})
            if "running_counts" in run_df:
                run_df["rotations"] = run_df["running_counts"] / COUNTS_PER_REV

            # licks / pulses long form
            licks = pd.DataFrame(lick_rows, columns=[
                "trial_num", "phase", "t", "t_rel_onset_s", "t_rel_window_s"])
            pulses = pd.DataFrame(pulse_rows, columns=[
                "trial_num", "pulse_idx", "t", "t_rel_onset_ms"])

            camera = self._read_camera(f)

        return {
            "trials": self._ident(df),
            "running": self._ident(run_df),
            "licks": self._ident(licks),
            "pulses": self._ident(pulses),
            "stimulus": self._ident(stim_df),
            "camera": self._ident(camera),
            "_stim_attrs": stim_attrs,
        }

    @staticmethod
    def _read_stimulus(sg):
        """Return (per-planned-trial DataFrame, attrs dict). Per-trial arrays share the
        modal length; scalar group-attrs and shorter arrays (block_*) go to the dict."""
        lengths = [len(ds) for ds in sg.values()]
        n_plan = max(set(lengths), key=lengths.count) if lengths else 0
        cols, attrs = {}, {}
        for name, ds in sg.items():
            if len(ds) == n_plan:
                cols[name] = ds[:]
            else:
                attrs[name] = ds[:]                      # block_delays, block_start_indices, …
        for k, v in sg.attrs.items():
            attrs[k] = _decode(v)
        df = pd.DataFrame(cols)
        if len(df):
            df.insert(0, "trial_idx", np.arange(n_plan))
        return df, attrs

    @staticmethod
    def _read_camera(f):
        if "camera" not in f or "frame_timestamps" not in f["camera"]:
            return pd.DataFrame()
        ds = f["camera"]["frame_timestamps"]
        arr = ds[:]
        cols = [_decode(c) for c in ds.attrs.get(
            "columns", ["frame_idx", "wall_clock_s", "sensor_ts_ns"])]
        return pd.DataFrame(arr, columns=cols[:arr.shape[1]])


# ── many sessions ────────────────────────────────────────────────────────────

def _iter_sessions(source):
    """Yield Session objects from a Session, a path/glob/dir, or an iterable of those."""
    if source is None:
        return
    if isinstance(source, Session):
        yield source
    elif isinstance(source, (str, Path)):
        s = str(source)
        if any(c in s for c in "*?[") and not Path(s).exists():
            for m in sorted(glob.glob(s, recursive=True)):
                yield Session(m)
        elif Path(s).is_dir():
            for h in sorted(Path(s).rglob("*.h5")):
                yield Session(h)
        else:
            yield Session(s)
    else:
        for item in source:
            yield from _iter_sessions(item)


class Dataset:
    """A collection of Sessions whose tables concatenate on access, each row tagged with
    its session_id. Build from a dir / glob / list, and grow it with ``.add()``."""

    def __init__(self, source=None):
        self.sessions: list[Session] = []
        if source is not None:
            self.add(source)

    def __repr__(self):
        return f"Dataset({len(self.sessions)} sessions, {len(self.trials)} trials)"

    def __len__(self):
        return len(self.sessions)

    def __iter__(self):
        return iter(self.sessions)

    def __getitem__(self, i):
        return self.sessions[i]

    def add(self, source) -> "Dataset":
        """Append one or more sessions (dir, glob, path, Session, or an iterable). Chainable."""
        self.sessions.extend(_iter_sessions(source))
        return self

    def _concat(self, attr: str) -> pd.DataFrame:
        frames = [getattr(s, attr) for s in self.sessions]
        frames = [df for df in frames if df is not None and len(df)]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    @property
    def trials(self) -> pd.DataFrame:
        return self._concat("trials")

    @property
    def running(self) -> pd.DataFrame:
        return self._concat("running")

    @property
    def licks(self) -> pd.DataFrame:
        return self._concat("licks")

    @property
    def pulses(self) -> pd.DataFrame:
        return self._concat("pulses")

    @property
    def stimulus(self) -> pd.DataFrame:
        return self._concat("stimulus")

    @property
    def camera(self) -> pd.DataFrame:
        return self._concat("camera")

    @property
    def meta(self) -> pd.DataFrame:
        """One row per session (task_config dropped — reach it via Session.meta)."""
        rows = [{k: v for k, v in s.meta.items() if k != "task_config"}
                for s in self.sessions]
        return pd.DataFrame(rows)
