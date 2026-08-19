# shepherd — VRFarm rig health monitor

A tiny, **out-of-process** watchdog that runs on a rig Pi and watches the things
that can silently ruin a session: SoC temperature and throttling, disk space,
CPU/memory, the camera's live encode rate, and whether `pi_api` is still answering.

## Why a separate process

The Pi 5 has **no hardware H.264 encoder and no fan**, so a long session software-
encodes on the CPU and heats up. When it throttles, the encoder slows, **drops
frames, and corrupts the frame timing** the whole camera pipeline exists to
preserve — and none of that is visible in the preview or in the data until you
analyse it. shepherd catches it live.

A monitor living *inside* `pi_api` would freeze at exactly the moment you most
want its data (a thermal stall, a wedged process). shepherd runs in its own
process, so a stall or crash in `pi_api` can't take the watchdog down with it —
and shepherd's API probe is precisely what **detects** that stall.

It is deliberately minimal: each cycle is a few `/proc` and `/sys` reads plus one
short HTTP probe. Sampling cost is far below anything it watches. It **does not
stream telemetry**, does not touch the HDF5, and writes only two plain files.

## What it watches

| Metric | Source | Default warn / critical |
|---|---|---|
| SoC temperature | `/sys/class/thermal` | 50 °C / 70 °C |
| Throttle / under-voltage flags | `vcgencmd get_throttled` | soft-limit / throttling-now |
| Disk used % (video mount) | `statvfs` | 80% / 90% |
| Disk free GB | `statvfs` | 40 / 15 GB |
| Total + per-core CPU | `/proc/stat` | 85% / 95% |
| Memory used % | `/proc/meminfo` | 85% / 95% |
| Disk write rate | `/proc/diskstats` | off by default |
| Camera encode fps (while recording) | `pi_api /api/status` | 45 / 30 fps |
| `pi_api` responsive | HTTP probe | critical if down |

Every threshold, direction, and **message** is editable in [`config.yaml`](config.yaml).

## Two alert levels

- **warning** → orange in the experiment-UI log
- **critical** → red in the UI log, **and** a Slack message (so it reaches you with
  the browser closed)

Alerts are **edge-triggered** — fired when a metric changes level, not every
second — so the log isn't spammed. A still-critical condition re-alerts every
`realert_critical_s` (default 30 s). A return to normal logs a recovery line.

Delivery: a `shepherd_alert` UDP packet to the controller's event port
(`network.event_port`, default 5571). The controller already forwards every event
to the UI over SSE; `app/app.py` adds the Slack hop for criticals and
`experiment.html` colours the line by level. If no session UI is live the packet is
simply dropped (UDP is best-effort) — criticals still reach Slack.

## Outputs (both under `output_dir`, default `~/shepherd_logs/`)

- `shepherd_<timestamp>.jsonl` — one JSON object per sample (all metrics). A plain,
  separate file; **nothing goes into the session `.h5`.**
- `shepherd_<timestamp>.alerts.log` — human-readable warning/critical/recovery lines.

## Running it

**Installed automatically on the leader.** The setup-UI **Install** deploys
`shepherd.py`, seeds `config.yaml` (see below), and installs + enables + starts the
`shepherd` systemd service. It then runs continuously, learning session state from
the API probe — you do **not** start/stop it per session. Watch it with:
```bash
journalctl -u shepherd -f
sudo systemctl status shepherd
```

**Config is seeded once, then owned by the Pi.** Install copies `config.yaml` with
`cp -n` (no-clobber), so thresholds/messages you edit on the leader survive a
re-Install or Deploy. To change them:
```bash
nano ~/rig/shepherd/config.yaml         # on the leader
sudo systemctl restart shepherd         # shepherd reads the file once at startup
```
(The repo copy is the default/template. A `shepherd.py` *code* change rides Deploy
but needs a `systemctl restart shepherd`, or a re-Install, to take effect.)

**Manually (for a quick look or a test), on any machine:**

**Manually (for a quick look or a test):**
```bash
python3 shepherd/shepherd.py --config shepherd/config.yaml
python3 shepherd/shepherd.py --config shepherd/config.yaml --once   # one sample, then exit
```

## Configuring

Edit [`config.yaml`](config.yaml) and restart shepherd (it reads the file once at
startup). Each metric block:
```yaml
soc_temp_c:
  enabled: true
  direction: high            # 'high' fires when value rises; 'low' when it falls
  warn: 50
  critical: 70
  warn_msg:     "SoC temperature {value}°C — running warm; check ventilation"
  critical_msg: "SoC temperature {value}°C — CRITICAL: thermal throttle imminent"
  recover_msg:  "SoC temperature back to normal ({value}°C)"
```
Message placeholders: `{value}`, `{warn}`, `{critical}`, `{unit}`, `{mount}`
(and `{error}` for `api_health`).

Point the alerts at your controller:
```yaml
shepherd:
  alerts:
    udp: { enabled: true, host: 192.168.10.1, port: 5571 }
```

## Dependencies

stdlib + PyYAML only. The API probe uses `urllib` (stdlib), not `requests`. Runs in
the `rig` conda env with nothing extra to install.

## Notes / limits

- `vcgencmd` and the thermal zones are Pi-only; off a Pi those metrics are simply
  absent (shepherd degrades gracefully, never errors).
- Live encode fps needs `pi_api /api/status` to expose `camera_frames` (added
  alongside this monitor). Older API builds just omit that one alert.
- The disk write-rate meter sums whole-disk devices matching `disk_devices`; it's a
  coarse "is something hammering the disk" signal, off by default.
