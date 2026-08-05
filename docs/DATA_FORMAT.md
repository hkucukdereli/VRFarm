# VRFarm session data format

**`format_version: 2`** — a transferred session is **one self-contained HDF5 file** plus the raw video.

```
<dest>/<subject>/<subject>_<date>/<session_id>/
├── <session_id>.h5     # everything: trials, stimulus plan, per-device data, camera timestamps, metadata
└── video.h264          # raw H.264 elementary stream (only if the camera was saved)
```

`session_id = <subject>_<YYYYMMDD>_<NNN>` (e.g. `mouse1_20260803_001`). `<dest>` is the Browse override, else `$VRFARM_DATA_DIR`, else `~/VRFarm/data`.

---

## How the file is built (at Transfer)

During a run the Leader writes a **flat** `.h5` (one dataset per field at the root), and the camera/stim generator drop sidecars alongside it:

| sidecar | written by |
|---|---|
| `metadata.yaml` | leader engine |
| `stimuli.npz` | stim generator |
| `trials.yaml` | stim generator (human-readable copy of the NPZ) |
| `frame_timestamps.npy` | camera (on the SSD, next to `video.h264`) |

When you hit **Transfer**, the controller POSTs `/api/consolidate/<session_id>` to the Leader. The always-on `pi_api` service runs [`shared/consolidate.py`](../shared/consolidate.py), which:

1. Reorganizes the flat datasets into groups (`/trials`, `/lick`, `/reward`, `/photodiode`).
2. Folds `metadata.yaml` into **root attributes**.
3. Folds `stimuli.npz` into **`/stimulus`**, dropping fields already recorded in `/trials` (no redundancy).
4. Folds `frame_timestamps.npy` into **`/camera/frame_timestamps`**.
5. Deletes all four sidecars (`trials.yaml` is redundant with the NPZ).

It's **idempotent** (a file already at `format_version 2` is left alone) and **atomic** (writes a temp file, then swaps). Only `<session>.h5` + `video.h264` are then pulled to the controller.

---

## File structure

```
<session_id>.h5
├─ (root attrs)          session metadata + full task_config
├─ /trials/              recorded, one row per completed trial
├─ /stimulus/            pre-generated plan (per-trial arrays + scalar group-attrs)
├─ /lick/                (only if lick_sensor was saved)
├─ /reward/              (only if reward was saved)
├─ /photodiode/          (only if photodiode was saved)
└─ /camera/              (only if the camera was saved)
```

### Root attributes — session metadata
| attr | notes |
|---|---|
| `session_id`, `subject_id`, `date`, `session_num`, `notes` | session identity |
| `level`, `rig_name`, `timestamp` | paradigm level, rig, Unix time of finalize |
| `n_trials_completed`, `n_trials_planned` | trial counts |
| `saved_devices`, `skipped_devices` | which devices' detailed data was recorded |
| `task_config` | **the entire task YAML as a JSON string** — full paradigm reconstruction |
| `format_version` | `2` |

### `/trials/` — recorded per-trial data (length = `n_trials_completed`)
| dataset | dtype | meaning |
|---|---|---|
| `trial_num`, `block_num` | i4 | indices |
| `trial_outcome` | str | `"hit"` / `"miss"` |
| `level_effective` | f4 | level actually run (adaptive 2.5 → the drawn 2 or 3) |
| `adaptive_state` | f4 | P(L3) for adaptive 2.5, else 1 |
| `stim_az_deg` | f4 | stimulus azimuth (as run) |
| `contrast` | f4 | raw value in the configured metric (what the UI showed) |
| `corr_contrast` | f4 | normalized headroom fraction actually rendered |
| `iti_duration_s` | f4 | **actual** ITI before this trial |
| `iti_start_t`, `stim_onset_t`, `true_onset_t`, `response_window_t`, `outcome_t`, `first_lick_t` | f8 | event times (Unix s, NTP-synced) |
| `display_latency_s` | f8 | `true_onset_t − stim_onset_t` (photodiode-measured) |
| `sync_ok` | i1 | 1 if the online photodiode onset-sync landed that trial |

`true_onset_t` is the display onset corrected by the photodiode; `stim_onset_t` is when the Leader sent SHOW.

### `/stimulus/` — the pre-generated plan (de-duplicated vs `/trials`)
Per-trial arrays (length = `n_trials_planned`), plus block arrays:
| dataset | meaning |
|---|---|
| `stim_alt_deg` | stimulus altitude/elevation |
| `px_x`, `px_y`, `px_size` | on-screen pixel position + size |
| `duration_s` | planned visual stimulus duration |
| `prestim_s`, `poststim_s` | planned pre/post-stim periods |
| `iti_planned_s` | planned ITIs (length n+1: leading + between + trailing) |
| `block_delays`, `block_start_indices` | per-block delay + first-trial index |

Scalar params as **group attributes**: `background_gray`, `shape`, `global_delay`, `block_delay_skip_first`, `sync_square_every_n`.

> Dropped as redundant with `/trials` (recorded): `trial_idx`, `block_num`, `stim_az_deg`, `contrast`, `corr_contrast`, `bg_gray`, `n_trials`.

### `/lick/`, `/reward/`, `/photodiode/` — per device (present only if saved)
| group | datasets |
|---|---|
| `/lick/` | `lick_times` (vlen f8, in-trial), `iti_lick_times` (vlen f8), `iti_lick_count` (i4) |
| `/reward/` | `reward_t` (f8), `reward_amount_ul` (f4, commanded µL), `reward_pavlovian` (i1, 1 = Pav-delay rescue) |
| `/photodiode/` | `sync_pulses` (vlen f8 — rising-edge times) |

Unchecked (skipped) devices → their group is simply **absent** (see `skipped_devices`). Core `/trials` data is always written.

### `/camera/frame_timestamps` — video frame timing (present only if saved)
`N×3` float64, with a `columns` attribute:
| column | meaning |
|---|---|
| `frame_idx` | index into the `video.h264` frames |
| `wall_clock_s` | host wall clock at frame callback (`time.time()`, NTP-synced — **same timebase as all `/trials` events**) |
| `sensor_ts_ns` | hardware `SensorTimestamp` (start of readout) on `CLOCK_BOOTTIME`, ns |

---

## Loading (`analysis/loaders.py`)

```python
from analysis import loaders

S = loaders.load_session(session_dir)      # nested dict by group
S["attrs"]["task_config"]                  # full paradigm (parsed from JSON)
S["trials"]["trial_outcome"]               # ['hit', 'miss', ...]
S["lick"]["lick_times"][k]                 # licks in trial k
S["stimulus"]["_attrs"]["background_gray"] # scalar group attrs under "_attrs"

trials = loaders.load_trials(session_dir)  # numpy structured array (scalar /trials cols)
info   = loaders.session_info(session_dir) # n_trials, duration_s, hit_rate, total_rewards_ul
vt     = loaders.load_video_timestamps(session_dir)  # {frame_idx, t (wall), t_sensor, avg_fps}
stim   = loaders.load_stims(session_dir)   # the /stimulus plan
```

All loaders accept a session directory or the `.h5` path, read from the single file, and fall back to legacy `.npz`/`.npy`/flat-HDF5 for pre-v2 sessions.

---

## Not in the session folder
- **Subject index** `subjects/<subject>.json` (controller-side, at the default data dir) — a running log across the subject's sessions (session_id, date, level, trial counts, block sequence, stim size, contrast values, notes). Written once per transfer.
