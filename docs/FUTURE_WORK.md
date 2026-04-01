# VRFarm — Future Work & Deferred Items

Items deferred during initial setup. Revisit when relevant.

---

## Infrastructure

### conda not in PATH for non-interactive SSH sessions
**Priority:** Done — patched in rig_setup_ui.py  
**Context:** `conda run -n rig` fails via SSH because conda is not initialised
in non-interactive shells. Fixed by sourcing conda profile explicitly:
`source ~/miniforge3/etc/profile.d/conda.sh && conda activate rig && ...`  
**Status: resolved ✓ 2026-03-30**

### Mac NTP serving over local ethernet
**Priority:** Low  
**Context:** Both Pis sync to public internet NTP at ~6µs accuracy. Relative
drift <2ms — sufficient for current experiments. Mac NTP serving was attempted
but chrony has no Homebrew service definition and launchd setup was unstable.  
**When to revisit:** If photodiode sync data reveals a systematic cross-machine
timestamp offset, or if internet access on the Pis is ever removed.  
**What's needed:**
- Create working launchd plist for chronyd on Mac
- Config: `/opt/homebrew/etc/chrony.conf` with `allow 192.168.10.0/24`
- Plist: `/Library/LaunchDaemons/org.chrony.chronyd.plist`
- Then point both Pis to `192.168.10.1` via `/etc/chrony/sources.d/mac.conf`

### Switch to Linux control machine
**Priority:** Low  
**Context:** Mac works fine for everything except NTP serving, which requires
launchd wrangling. A Linux machine would make NTP, service management, and
future automation simpler.  
**When to revisit:** If replacing the Mac anyway, or when scaling to 4+ rigs.

---

## Experiment System

### Multi-rig support (4 rigs)
**Priority:** Medium — plan is 4 rigs total  
**Context:** Architecture already supports it. Single-rig flow needs to be
validated first.  
**What's needed:**
- `experiment_ui.py` refactor: grid layout with one panel per rig
- `rigs.yaml` listing all rigs and their Pi IPs
- "GO ALL" button to start all rigs simultaneously
- Per-rig data subdirectories
- `task.py` and `worker.py` unchanged — already rig-agnostic

### Latency measurement and validation
**Priority:** High — do after first successful end-to-end session  
**Context:** Need to characterise actual timing accuracy of the full stack
before trusting behavioral data.  
**What to measure:**
- Stim onset jitter (PsychoPy flip timestamp vs photodiode pulse)
- Lick detection latency (MPR121 polling at 200Hz → ~5ms resolution)
- Reward delivery latency (ZMQ round trip + solenoid response)
- Camera frame timestamp accuracy vs photodiode

### Photodiode hardware integration
**Priority:** Medium  
**Context:** Code is already written and tested in software. Just needs
hardware: photodiode + voltage divider wired to GPIO24 on cheddar, taped
to sync patch corner of projector screen.  
**Config:** Set `use_photodiode: true` and `photodiode_gpio: 24` in YAML.

### Running wheel encoder
**Priority:** Low — future addition  
**Context:** Mentioned as a possible future tool on cheddar. Architecture
supports adding new hardware tools without changing task.py.  
**What's needed:** Quadrature encoder on cheddar GPIO, new worker thread,
new ZMQ event type `ENCODER`, new HDF5 dataset.

### Luminance calibration (empirical)
**Priority:** Medium — do before serious behavioral experiments  
**Context:** Warp map currently uses theoretical cosine falloff estimate.
Needs photometer measurements at multiple azimuths to fit empirical
correction curve.  
**Steps:** `display_test_patches.py` → measure with photometer →
`fit_luminance_correction.py`

---

## Data & Analysis

### Online trial monitoring / early stopping
**Priority:** Low  
**Context:** Currently the UI shows hit rate and trial count but has no
logic to auto-stop if performance drops below threshold or animal stops
licking. Useful for welfare and data quality.

### Data integrity check script
**Priority:** Low  
**Context:** After transfer, verify HDF5 trial count matches expected,
video frame count is consistent, no NaN timestamps.

---

## Notes
- Add items here as they come up during testing
- Date each entry when it's resolved and move to a RESOLVED section below

---

## Resolved

_(none yet)_
