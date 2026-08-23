# displayd — interfaces and contracts (phase 2)

The display daemon for the KMS display Pi. Two processes: `displayd.py` (control,
health, DLPC, supervision — system python3, stdlib+PyYAML only) and its child
`renderer.py` (sole DRM master — system python3 + python3-pygame/numpy). Design
rationale and failure playbook: the displayd design spec (bench 2026-08-22/23).
Everything here targets the follower's SYSTEM python (3.13 on trixie) — the conda
env's SDL has no kmsdrm and must never run either process.

## Files

- `displayd/displayd.py`   — daemon entry: `python3 displayd.py --rig <yaml>`
- `displayd/renderer.py`   — child entry (spawned by displayd only): inherits fd 3 =
  its end of an AF_UNIX SOCK_DGRAM socketpair
- `displayd/dlpc.py`       — DLPC3436 I2C wrapper (imports the vendored SDK from
  `~/dlp`; sys.path append `~/dlp` + `~/dlp/api`)
- `displayd/displayd.service` — systemd unit, installed to /etc/systemd/system,
  shipped DISABLED (phase 3 enables)
- Deployed under `~/rig/displayd/`; `devices/display.py` (already in `~/rig`) is
  imported by renderer.py for drawing (sys.path insert `~/rig`).

## Ports (network block of the rig yaml; defaults in code)

- `display_port` 5575/udp — leader engine session channel (bind 0.0.0.0)
- `ack_port` 5573/udp — displayd -> leader (ACK/ONSET/display_health); leader binds
- `displayd_ctl_port` 5581/tcp — control REST, 127.0.0.1 ONLY
- `displayd_pd_port` 5582/udp — leader photodiode -> displayd pulse ingest (phase 2:
  bind + buffer; the correlator consumes it in phase 3)

## Lifecycle state machine (states exactly as reported in /status)

BOOT -> CONFIG_OK -> DLPC_ALIVE -> VIDEO_UP -> DLPC_LOCKED -> CURTAIN_DOWN
     -> RENDERER_UP -> OPTICS_OK ; FAULT(reason) from any transition failure.
Repair states: REINIT_DLPC (auto on L1 source-revert), MODESET_KICK, RENDERER_RESTART.

- CONFIG_OK: /sys/class/drm/card*-DPI-1/status == "connected" and modes contain
  1920x1080. (No config.txt hash check — a plain presence check; the critique's note.)
- DLPC_ALIVE: one ShortStatus read succeeds. Reported as "logic powered", NEVER
  "projector on" (back-feed).
- VIDEO_UP: `pinctrl set 25 op dh` (idempotent).
- DLPC_LOCKED: run the init sequence (dlpc.init_parallel()), readback SourceSelect ==
  external parallel, then sleep 2.5 s (light settle).
- CURTAIN_DOWN: curtain readback == off.
- RENDERER_UP: renderer child spawned; its startup contract passed (see below);
  first flip report received.
- OPTICS_OK: phase 2 = parked with `optics: "unverified"` unless a manual
  /render sync_burst + external confirmation is recorded; the L3 correlator
  arrives in phase 3. The state machine must expose this honestly in /status.
- MODESET_KICK: stop renderer -> `timeout 2 modetest -M vc4 -s DPI-1:1920x1080`
  -> respawn renderer. NEVER unbind vc4_dpi (kernel oops).

## Renderer startup contract (hard-fail; each clause is a bench-caught bug)

1. `SDL_HINT_NO_SIGNAL_HANDLERS=1`, `SDL_VIDEODRIVER=kmsdrm`, no DISPLAY.
2. `pygame.display.set_mode((1920,1080), FULLSCREEN|SCALED, vsync=1)`.
3. Assert `pygame.display.get_driver() == "KMSDRM"` (null-backend fallback is silent).
4. 30-frame self-test: measured fps within 57.46±1.5 AND mean flip-block >= 8 ms.
5. On any failure: send `{"ev":"fatal","reason":"NULL_BACKEND|FPS_OUT_OF_RANGE|
   SDL_ERROR","detail":...}` up the socketpair and exit(3). displayd surfaces the
   reason verbatim and does not blind-retry more than once.

## displayd <-> renderer socketpair (JSON per datagram, fd 3 in the child)

down: {"op":"mode","lease":"idle"|"setup"|"session"}
      {"op":"load_stims","path":"/home/vruser/rig/stims/<sid>/stimuli.npz"}
      {"op":"show","seq":42,"trial":7}
      {"op":"render","action":"blank"|"checkers"|"stimulus"|"reload_warp"|
       "sync_test"|"stop_sync"|"sync_burst", ...params identical to the old
       display_worker actions...}
      {"op":"heartbeat_flash"}          # one red sync-square frame, then restore
      {"op":"quit"}
up:   {"ev":"ready","driver":"KMSDRM","fps":57.46,"flip_ms":13.5}   # after self-test
      {"ev":"flip","t":<wall>,"flip_ms":13.5}       # per flip, throttle to <= 60/s
      {"ev":"onset","seq":42,"trial":7,"t":<wall>,"flip_ms":13.5}
      {"ev":"flash","t_cmd":<wall>}                  # heartbeat flash flip time
      {"ev":"result","op":"sync_burst","flashes":12,"flash_times":[...]}
      {"ev":"fatal","reason":...,"detail":...}
      {"ev":"log","line":"..."}

## Session channel (UDP :5575) — leader engine compatible

Phase 2 keeps follower.py's message names so the existing leader works unchanged:
SHOW {trial}, BG, BLANK, LOAD_STIMS {path}, SYNC_TEST {...}, QUIT (ignored with a
log — the daemon never exits), plus new: PING -> PONG {state}. Replies/acks go to
`ack_port`: on SHOW's first flip send {"type":"stim_onset","trial":N,"t":...} —
the same shape follower.py sends today. Seq-numbered SESSION_BEGIN arrives with
the phase-3 leader; tolerate unknown cmds with a log line.

## Control REST (:5581, localhost; stdlib ThreadingHTTPServer)

GET  /status  -> {"state":..., "since":..., "optics":"unverified"|"ok",
                  "dlpc":{...latest sweep, latched:{bit: since_t}...},
                  "renderer":{"pid":..., "fps":..., "flip_ms_p99":..., "restarts":N},
                  "lease":{"mode":"idle|setup|session|external","holder":...},
                  "alarms":[...last 20...]}
POST /bringup -> walk CONFIG_OK..RENDERER_UP; body {} ; returns /status shape
POST /lease   -> {"mode":"setup"|"external"} ; POST /release
POST /render  -> forward body to renderer {"op":"render",...}; returns its result
POST /recover -> {"action":"modeset_kick"|"dlpc_reinit"|"renderer_restart"}
GET  /health/history?n=100 -> alarm/event ring

## L1 poller (thread in displayd)

1 Hz sweep via dlpc.sweep() (the bench 18-read set, ~8.6 ms). Continuous from
daemon start; keeps {live_snapshot, latched_since{bit:t}} — first read after an
event is history (latch semantics). Detections -> alarms + auto-transitions:
SourceSelect != parallel -> REINIT_DLPC; ActuatorWatchdogTimerTimeout==1 ->
alarm "input signal lost". NAK -> alarm "DLPC unreachable" (logic power lost —
rare; back-feed). All alarms: append to ring + send {"type":"display_health",...}
to leader ack_port + print one journald line.

## dlpc.py API

class Dlpc: open()/close(); sweep() -> {"ok":bool, "reads":{name:{...}},
"errors":{name:msg}} (defensive: NameError == no-response; never trust
Summary.Successful on writes); init_parallel() (the init_parallel_mode.py
command sequence in-process, curtain try/finally); read_source() / read_curtain()
/ read_short() convenience. A single threading.Lock serializes ALL bus access.

## displayd.service

[Unit] Description=VRFarm display daemon; After=network-online.target
[Service] User=vruser; ExecStart=/usr/bin/python3 /home/vruser/rig/displayd/displayd.py --rig /home/vruser/rig/rigs/<name>.yaml
Restart=always; RestartSec=2; WatchdogSec=15; Type=notify (sd_notify via a ~10-line
AF_UNIX datagram helper — no python3-systemd dep). Journald gets stdout/stderr.
Shipped disabled: Install writes the unit + `systemctl daemon-reload`, no enable.

## Install additions (setup/app.py, follower role)

- apt: python3-pygame python3-numpy python3-yaml libdrm-tests (+ apt-mark hold
  libsdl2-2.0-0 python3-pygame)
- write /etc/systemd/system/displayd.service (scp + sudo mv), daemon-reload
- KMS config: copy dlp/sample_config/config_kms.txt to /boot/firmware/ as
  config_kms_candidate.txt ONLY (never activate config.txt in phase 2)
- deploy manifest: displayd/*.py join the follower list

## Non-goals in phase 2 (phase 3+)

Leader-side HB_FLASH scheduler + correlator; seq-numbered SESSION_BEGIN protocol;
pi_api forwarding of the old endpoint names to :5581; /api/start translation shim;
deletion of follower.py/display_worker.py; enabling the service.
