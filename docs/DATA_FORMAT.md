# VRFarm session data format

**`format_version: 2`** — a transferred session is **one self-contained HDF5 file** plus the raw video.

```
<dest>/<subject>/<subject>_<date>/<session_id>/
├── <session_id>.h5     # everything: trials, running wheel, stimulus plan, per-device data, camera timestamps, metadata
└── video.h264          # raw H.264 elementary stream (only if the camera was saved)
```

`session_id = <subject>_<YYYYMMDD>_<NNN>` (e.g. `mouse1_20260803_001`). `<dest>` is the Browse override, else `$VRFARM_DATA_DIR`, else `~/VRFarm/data`.

---

## How the file is built (at session end)

During a run the Leader writes a **flat** `.h5` (one dataset per field at the root), and the camera/stim generator drop sidecars alongside it:

| sidecar | written by |
|---|---|
| `metadata.yaml` | leader engine (in `_finalize_session`) |
| `stimuli.npz` | stim generator |
| `trials.yaml` | stim generator (human-readable copy of the NPZ) |
| `frame_timestamps.npy` | camera (on the SSD, next to `video.h264`) |

**Consolidation runs at session end, not at Transfer.** The moment the session stops or finishes, the Leader engine's `shutdown()` calls `_consolidate_at_exit()` — after every device is closed and its sidecar flushed (the camera's `frame_timestamps.npy` included) — which runs [`shared/consolidate.py`](../shared/consolidate.py) on the just-closed `.h5`. So the archive is already self-contained before anything is transferred. **Transfer re-POSTs `/api/consolidate/<session_id>` as a no-op safety net**, so a session whose exit consolidation didn't run (crash, hard stop) still gets consolidated when pulled.

`consolidate.py`:

1. Reorganizes the flat root datasets into groups (`/trials`, `/lick`, `/reward`, `/photodiode`). Any dataset not in its map — **including the running-wheel arrays** — falls through to `/trials`.
2. Folds `metadata.yaml` into **root attributes**.
3. Folds `stimuli.npz` into **`/stimulus`**, dropping fields already recorded in `/trials` (no redundancy).
4. Folds `frame_timestamps.npy` into **`/camera/frame_timestamps`**.
5. Deletes all four sidecars (`trials.yaml` is redundant with the NPZ).

It's **idempotent** (a file already at `format_version 2`, or that already has a `/trials` group, is left alone) and **atomic** (writes a temp file, then swaps). Only `<session>.h5` + `video.h264` are then pulled to the controller.

---

## File structure

```
<session_id>.h5
├─ (root attrs)          session metadata + full task_config
├─ /trials/              recorded per-trial columns + the continuous running-wheel stream
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
| `trial_run_distance_cm` | f4 | distance the running wheel turned during this trial (cm) |

`true_onset_t` is the display onset corrected by the photodiode; `stim_onset_t` is when the Leader sent SHOW.

#### Running-wheel stream — also under `/trials/`, but length = **number of encoder samples**, not per-trial
The AS5600 wheel encoder is a *continuous* device: it streams for the whole session, so these arrays are far longer than the per-trial columns above (tens of thousands of samples). They share the `/trials` group only because the engine writes them as root datasets and they fall through `consolidate.py`'s group map. They are **not** part of the per-trial `Session.trials` table (different length) — read them from `Session.running` (see Loading) or h5py directly.

| dataset | dtype | meaning |
|---|---|---|
| `running_t` | f8 | sample time (Unix s, NTP-synced — same timebase as `/trials` events) |
| `running_counts` | i8 | **cumulative signed raw encoder counts — the ground truth.** 4096 counts/rev (12-bit AS5600); rotations = `counts / 4096` |
| `running_distance_cm` | f4 | cumulative distance = rotations × wheel circumference (π · `wheel_diameter_cm`) |
| `running_speed_cms` | f4 | instantaneous speed = d(distance)/dt |

Because `running_counts` is the raw ground truth, **speed and distance can be fully recomputed offline** — even with a corrected wheel diameter.

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

Unchecked (skipped) devices → their group is simply **absent** (see `skipped_devices`). Core `/trials` data is always written. The running-wheel **encoder has no group of its own** — its per-trial and continuous data live under `/trials` (above).

### `/camera/frame_timestamps` — video frame timing (present only if saved)
`N×3` float64, with a `columns` attribute:
| column | meaning |
|---|---|
| `frame_idx` | index into the `video.h264` frames |
| `wall_clock_s` | host wall clock at frame callback (`time.time()`, NTP-synced — **same timebase as all `/trials` events**) |
| `sensor_ts_ns` | hardware `SensorTimestamp` (start of readout) on `CLOCK_BOOTTIME`, ns |

---

## Loading (`analysis/loaders.py`)

Two classes turn the file into tidy pandas DataFrames: **`Session`** (one file) and **`Dataset`** (many, concatenated). Every frame carries `session_id` + `subject_id` columns, so single- and multi-session frames are interchangeable.

```python
from analysis.loaders import Session, Dataset

s = Session(session_dir)          # a session dir OR the .h5 path
s.trials      # one row / completed trial — the analysis-ready wide table (see below)
s.running     # one row / wheel-encoder sample (continuous stream; + rotations column)
s.licks       # one row / lick   (long form: in-trial + ITI, times rel. to onset/window)
s.pulses      # one row / photodiode sync pulse (long form, times rel. to onset)
s.stimulus    # one row / planned trial
s.camera      # one row / recorded video frame (empty if the camera wasn't saved)
s.meta        # dict of root attrs; s.meta["task_config"] is the full paradigm
s.stim_attrs  # scalar /stimulus attrs + block arrays (block_delays, iti_planned_s, …)

ds = Dataset(".../data/HK1")      # dir (recursive *.h5), glob, list of paths, or Session(s)
ds.trials                          # every session's trials stacked, tagged by session_id
ds.add(".../HK1_20260811_001.h5")  # append another session (chainable)
ds.meta                            # one row per session
```

**`s.trials` columns** = the recorded `/trials` per-trial cols, plus merged per-trial device data, planned `/stimulus`, and derived ready-to-plot columns:
- **merged:** `reward_t`, `reward_ul`, `pavlovian`, `n_licks`, `n_iti_licks`, `iti_lick_count`, `n_pulses`, `first_pulse_t`, `trial_run_distance_cm`, and planned `stim_alt_deg`, `px_x/px_y/px_size`, `duration_s`, `prestim_s`, `poststim_s`.
- **derived:** `latency_ms`, `rt_ms` (first lick − response-window open), `reward_lat_ms` (reward − true onset), `first_pulse_lat_ms`, `hit` (bool).

The continuous running-wheel arrays are **not** in `s.trials` (different length) — they're in `s.running`. **Only format_version 2 is supported** (no legacy `.npz`/flat-HDF5 fallback).

---

## Not in the session folder
- **Subject index** `subjects/<subject>.json` (controller-side, at the default data dir) — a running log across the subject's sessions (session_id, date, level, trial counts, block sequence, stim size, contrast values, notes). Written once per transfer.
