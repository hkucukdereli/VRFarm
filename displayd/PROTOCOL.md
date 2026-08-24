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

Every down-op carries a monotonic "id"; results echo it (displayd correlates by
id, falling back to action name for older renderers).
down: {"op":"mode","lease":"idle"|"setup"|"session","config":{...display device
       config: rig devices.display merged with the photodiode sync_* keys, exactly
       engine/follower.py's merge — arrives on EVERY mode op, apply idempotently}}
      {"op":"load_stims","path":"/home/vruser/rig/stims/<sid>/stimuli.npz"}
      {"op":"show","seq":42,"trial":7}
      {"op":"render","action":"blank"|"checkers"|"stimulus"|"reload_warp"|
       "sync_test"|"stop_sync"|"sync_burst", ...params identical to the old
       display_worker actions...}   # session BG/BLANK add "use_session_bg":true
       # -> the device's plain blank() (NPZ background gray), never black
      {"op":"heartbeat_flash"}          # one red sync-square frame, then restore
      {"op":"quit"}
up:   {"ev":"ready","driver":"KMSDRM","fps":57.46,"flip_ms":13.5}   # after self-test
      {"ev":"flip","t":<wall>,"flip_ms":13.5,"n":<flips represented>}  # sampled
      # every 0.25 s (the old 1/60 s throttle never fired against a 17.4 ms frame)
      {"ev":"onset","seq":42,"trial":7,"t":<wall>,"flip_ms":13.5}
      {"ev":"flash","t_cmd":<wall>}                  # heartbeat flash flip time
      {"ev":"result","op":"sync_burst","id":<echo>,"flashes":12,"flash_times":[...]}
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
                  "dlpc":{...latest sweep, latched:{bit: since_t} (fault bits only,
                    retained 300 s from last assertion), read_errors:{name:msg}...},
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

# ── Phase 3 additions ──────────────────────────────────────────────────────────

## Daemon-mode detection (pi_api)

pi_api decides daemon vs legacy per request by probing GET 127.0.0.1:5581/status
(timeout 0.5 s). Alive -> daemon mode. No config flag, no state: enabling/stopping
displayd.service IS the switch, and old controllers keep working through the shim.

## pi_api forwarding (daemon mode)

- /api/init_projector      -> POST :5581/bringup (never start_projector.sh/X)
- /api/init_display        -> GET :5581/status (renderer is always up; returns ok
                              when state==RENDERER_UP, plus the status blob)
- /api/shutdown_display    -> POST :5581/release (lease release; daemon never exits)
- /api/blank_display, /api/test_checkers, /api/test_stimulus, /api/reload_warp,
  /api/photodiode_test_start|stop, /api/photodiode_sync_burst
                           -> POST :5581/render with the mapped action + params
                              (identical param names; sync burst returns flashes
                              + flash_times)
- /api/start {script:"follower", args:[..., "--stims", PATH]}  (the SHIM)
                           -> do NOT spawn a process. Send LOAD_STIMS {path:PATH}
                              to 127.0.0.1:5575, return {ok, daemon:true}. The
                              leader's SYNC_TEST retry loop remains the readiness
                              barrier. /api/stop with the daemon alive sends
                              nothing (QUIT is ignored anyway) and reports ok.
- /api/restart             -> 409 {"error":"session lease active"} when :5581
                              /status.lease.mode == "session" (unless force:true).
Legacy paths stay intact when the daemon is down (phase 4 deletes them).

## Leader-clocked L3 heartbeats (engine/leader.py)

- Scheduler: when the photodiode device exists AND task stimulus.photodiode_sync_enabled
  is true, every hb_period_s (default 5.0, task/device override "heartbeat_period_s")
  while NO stim window is active: record t_cmd = time.time(), send
  {"cmd":"HB_FLASH","seq":k} to every follower :5575. displayd forwards
  heartbeat_flash to the renderer and reports {"type":"hb_flash","seq":k,"t_flip":...}
  back on ack_port; the leader stores t_flip (fallback t_cmd + 0.05 if no report).
- When photodiode_sync_enabled is FALSE: no heartbeats, and one
  {"type":"display_health","alarm":{"kind":"optics_watch_off",...}} event is
  published at session start — the named degraded mode, never silence.
- Correlator (leader-side, single clock): photodiode pulse times arrive from the
  existing GPIO callback (already leader-clock). Cluster pulses: gap > 30 ms starts
  a new cluster (one flash = 2-3 sub-pulses at 8.7 ms). A heartbeat is CONFIRMED if
  a cluster onset lands in [t_flip - 0.020, t_flip + 0.070]. 3 consecutive
  unconfirmed heartbeats -> publish {"type":"display_health","alarm":{"kind":
  "heartbeat_lost",...}} + Slack notify (via the controller's existing event->
  notify path); recovery event after 2 consecutive confirmed. Stim windows: the
  existing per-trial sync path (sync_queue first-pulse) is unchanged and doubles
  as the in-stim heartbeat; the scheduler pauses from SHOW until stim_off.
- Verdict feedback (pd port :5582, leader -> displayd): every judged heartbeat is
  reported as {"type":"hb_verdict","seq":k,"confirmed":bool,"t":...}. The daemon owns
  the display but cannot see its own light, so this is the ONLY way it learns whether
  its output reaches the diode. displayd streaks these with the same thresholds the
  leader uses (2 confirmed -> optics "ok", 3 unconfirmed -> optics "lost") and resets
  to "unverified" whenever the renderer respawns, since a new process's optics are
  unproven. Fire-and-forget: the leader's own alarm is the authority. Reported in
  /status.optics ONLY — ST_OPTICS_OK remains unentered until phase 4 revisits the
  state ladder, so this does not alter bringup or recovery control flow.
- Display-fault abort (engine/leader.py, session.abort_on_display_fault, default
  true; needs photodiode sync): evaluated at the END of a trial, never mid-cue.
  Aborts when abort_after_bad_trials (default 3) consecutive stimuli record
  sync_ok == 0, or when the heartbeat alarm stands AND this trial's stimulus was
  unconfirmed. A standing alarm alone is deliberately insufficient — a confirmed
  stimulus proves light is arriving now. Publishes {"type":"display_abort","trial",
  "cause","reason","n_bad"} (controller pages Slack immediately), sets metadata
  end_reason: "display_fault" plus an "abort" block, and session_end carries
  end_reason so an aborted run never reports as a clean finish.
- Loss localization: heartbeat_lost carries "cause" from the Teensy's 1 Hz
  "B <floor> <ceil>" idle line, which reports the analog front end independently of
  pulse detection — no_telemetry / sensor_dead (B absent >3 s) / ttl_dead (light seen,
  no TTL edge) / no_light (front end alive, genuinely dark). no_light does NOT claim
  the projector failed: a covered or misaimed diode is equally dark from here, and
  only L4 can separate those.
- Per-trial record: sync_ok becomes tri-state int: 1 confirmed, 0 failed (sync
  enabled, no pulse), -1 unavailable (sync disabled). New per-trial field
  onset_source: "photodiode" | "ack" | "command" — the best available onset
  anchor (photodiode pulse > displayd stim_onset ack > SHOW send time); the
  chosen onset keeps feeding true_onset_t exactly as today.
