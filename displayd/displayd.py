"""
displayd/displayd.py

Control daemon for the KMS display Pi (phase 2). Owns DLPC health (the 1 Hz L1
poller), supervises the renderer child (the sole DRM master), serves the
leader-facing session channel (UDP :5575, follower.py-compatible), buffers
photodiode pulses (UDP :5582 — the correlator that consumes them is phase 3),
and exposes a localhost-only control REST (:5581).

The interface contract is displayd/PROTOCOL.md — message shapes, ports and file
names are fixed there. This process runs on the follower's SYSTEM python3
(trixie = 3.13) and is stdlib + PyYAML ONLY: pygame/numpy live in renderer.py,
and the conda env's SDL has no kmsdrm so it must never run either process.

Run: /usr/bin/python3 displayd.py --rig /home/vruser/rig/rigs/<name>.yaml
"""

import argparse
import glob
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

# dlpc.py lives beside us; the deployed ~/rig/displayd/ dir is not a package,
# so put our own dir on sys.path and import it flat. dlpc itself appends ~/dlp
# and ~/dlp/api for the vendored TI SDK. Import defensively: if the SDK is not
# on this Pi yet the daemon must still come up and report FAULT via /status
# instead of crash-looping under systemd Restart=always.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import dlpc as dlpc_mod
    _DLPC_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — any import failure means "no DLPC access"
    dlpc_mod = None
    _DLPC_IMPORT_ERROR = str(e)


# ── Defaults (overridden by the rig yaml network: block) ──

DEFAULT_DISPLAY_PORT = 5575      # leader engine session channel (bind 0.0.0.0)
DEFAULT_ACK_PORT = 5573          # displayd -> leader (ACK/ONSET/display_health)
DEFAULT_CTL_PORT = 5581          # control REST, 127.0.0.1 ONLY
DEFAULT_PD_PORT = 5582           # leader photodiode -> displayd pulse ingest
# Optics verdict streaks — deliberately the same thresholds as the leader's HB_OK_N / HB_LOST_N
# so the daemon's reported optics and the leader's heartbeat alarm never disagree.
OPTICS_OK_N = 2
OPTICS_LOST_N = 3

# Lifecycle states exactly as reported in /status (PROTOCOL.md).
ST_BOOT = "BOOT"
ST_CONFIG_OK = "CONFIG_OK"
ST_DLPC_ALIVE = "DLPC_ALIVE"
ST_VIDEO_UP = "VIDEO_UP"
ST_DLPC_LOCKED = "DLPC_LOCKED"
ST_CURTAIN_DOWN = "CURTAIN_DOWN"
ST_RENDERER_UP = "RENDERER_UP"
ST_OPTICS_OK = "OPTICS_OK"
ST_FAULT = "FAULT"
ST_REINIT_DLPC = "REINIT_DLPC"
ST_MODESET_KICK = "MODESET_KICK"
ST_RENDERER_RESTART = "RENDERER_RESTART"

# States in which the DLPC lock was established — only then does an L1 source
# readback != parallel mean "reverted" (during bringup it just means "not yet").
_LOCKED_STATES = (ST_DLPC_LOCKED, ST_CURTAIN_DOWN, ST_RENDERER_UP, ST_OPTICS_OK)

# The identity read in dlpc._READS: if it NAKs on the wire the part is not
# answering at all. NOTE dlpc.py caches the boot-constant reads (_STATIC_READS)
# after open(), so a cached identity can appear in sweep()["reads"] with the bus
# dead — hence _live_reads() below, which only counts reads that answered THIS
# sweep. Individual other reads failing is partial data, not a power loss.
_IDENTITY_READ = "ControllerDeviceId"


def _live_reads(reads):
    """Names in a sweep's reads that were actually fetched over the bus this
    sweep (i.e. minus anything dlpc.py may serve from its boot-constant cache)."""
    cached = frozenset(getattr(dlpc_mod, "_STATIC_READS", ()) or ())
    return set(reads) - cached


# How long a latched DLPC fault bit stays in /status.dlpc.latched after it was
# last seen asserted. DLPC status bits clear on read, so a bit is history the
# moment we read it — the retention window IS the report (see _l1_sweep_once).
LATCH_RETENTION_S = 300.0


def log(msg):
    """One line to stdout, flushed — journald is the log sink (no log files)."""
    print(msg, flush=True)


def sd_notify(msg):
    """~10-line sd_notify: datagram to $NOTIFY_SOCKET (Type=notify + WatchdogSec
    without a python3-systemd dep). Silently a no-op outside systemd."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):                 # abstract-namespace socket
        path = "\0" + path[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(msg.encode(), path)
        finally:
            s.close()
    except OSError:
        pass


def _source_is_parallel(val):
    """True if a dlpc source readback means 'external parallel'. Handles both
    read_source()'s {"ok","source":"ExternalParallelPort",...} and the sweep's
    nested SourceSelect read ({"source":{"name":...,"value":...}}) — unwrap
    dicts by the obvious keys, then match the SDK enum name (or the raw
    register value, 0 = external parallel port on DLPC343x)."""
    while isinstance(val, dict):
        for k in ("source", "source_select", "name", "value"):
            if k in val:
                val = val[k]
                break
        else:
            return False
    if isinstance(val, str):
        return "parallel" in val.lower()
    if isinstance(val, bool):
        return False
    if isinstance(val, int):
        return val == 0
    return False


def _curtain_is_off(val):
    """True if a dlpc curtain readback means the curtain is OFF (image visible).
    Same defensive shape-handling as _source_is_parallel."""
    if isinstance(val, dict):
        for k in ("curtain", "enable", "enabled", "on", "value"):
            if k in val:
                val = val[k]
                break
    if isinstance(val, str):
        return val.strip().lower() in ("off", "0", "false", "disabled", "none")
    return val in (0, False)


class Displayd:
    def __init__(self, rig_config, rig_path):
        self.rig = rig_config
        self.rig_path = rig_path

        net = rig_config.get("network") or {}
        self.display_port = int(net.get("display_port", DEFAULT_DISPLAY_PORT))
        self.ack_port = int(net.get("ack_port", DEFAULT_ACK_PORT))
        self.ctl_port = int(net.get("displayd_ctl_port", DEFAULT_CTL_PORT))
        self.pd_port = int(net.get("displayd_pd_port", DEFAULT_PD_PORT))

        devices = rig_config.get("devices") or {}
        disp_cfg = devices.get("display") or {}
        pd_cfg = devices.get("photodiode") or {}
        # Panel mode for the CONFIG_OK check + the modetest kick (PROTOCOL pins
        # 1920x1080; the rig display block may narrow it for a future panel).
        res = disp_cfg.get("resolution") or [1920, 1080]
        self.mode_str = "%dx%d" % (int(res[0]), int(res[1]))
        # Sync-square layout defaults: authored on the photodiode card, drawn by
        # the display — SYNC_TEST params override these per message.
        self.sync_layout = {k: pd_cfg[k] for k in
                            ("sync_corner", "sync_size_px", "sync_brightness")
                            if k in pd_cfg}
        self.display_cfg = dict(disp_cfg)   # rig devices.display verbatim

        # Leader address for onset acks + display_health alarms (ack_port — the
        # leader binds it; event_port is bound on the CONTROLLER, see follower.py).
        self._leader_addr = None
        for pi in rig_config.get("pis") or []:
            if pi.get("role") == "leader":
                self._leader_addr = (pi["ip"], self.ack_port)
                break
        self._ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # ── State machine ──
        self.state = ST_BOOT
        self.state_since = time.time()
        self.fault_reason = None
        self.optics = "unverified"       # "ok"/"lost" once the leader's L3 correlator reports
                                         # (hb_verdict on the pd port); never claim "ok" without
                                         # that external confirmation — the daemon cannot see
                                         # its own light. NOTE: this reports optics, it does not
                                         # drive the state ladder; ST_OPTICS_OK stays unentered
                                         # until the lifecycle is revisited in phase 4.
        self._optics_ok_streak = 0
        self._optics_lost_streak = 0
        self._bringup_lock = threading.RLock()   # serializes bringup/recover/auto-repair
        self._stop = threading.Event()

        # ── Alarm/event ring (last 200; /status shows the last 20 alarms) ──
        self._ring = deque(maxlen=200)
        self._ring_lock = threading.Lock()

        # ── DLPC / L1 poller ──
        self.dlpc = None                 # dlpc.Dlpc once opened (poller opens lazily)
        self._dlpc_open_lock = threading.Lock()   # only ONE Dlpc may ever be built
        self._dlpc_snapshot = None       # latest sweep() result verbatim
        self._dlpc_snapshot_t = None
        self._latched = {}               # bit -> first-seen t (latch semantics)
        self._latched_last = {}          # bit -> last-seen-asserted t (retention)
        self._dlpc_unreachable = False   # NAK edge detector (alarm once per outage)
        self._read_err_names = frozenset()   # reads that failed in the last sweep
        self._read_err_t = 0.0               # throttle for the dlpc_read_errors event
        self._last_auto_reinit = 0.0     # throttle auto REINIT_DLPC (no repair loops)

        # ── Renderer supervision ──
        self._renderer_lock = threading.RLock()
        self._renderer_proc = None
        self._renderer_sock = None       # our end of the socketpair (child has fd 3)
        self._renderer_gen = 0           # bumped per (re)spawn; stale readers exit
        self._renderer_should_run = False
        self._renderer_started_t = 0.0
        self._renderer_fatal = None      # verbatim reason from the fatal up-event
        self._renderer_ready = threading.Event()
        self._renderer_first_flip = threading.Event()
        self._renderer_info = {}         # driver/fps/flip_ms from the ready ev
        self._renderer_restarts = 0      # total respawns (reported in /status)
        self._renderer_fail_streak = 0   # policy: 1 = blind retry, 2 = modeset kick, 3 = FAULT
        self._flip_ts = deque(maxlen=120)     # recent flip wall-times -> fps estimate
        self._flip_ms = deque(maxlen=1024)    # recent flip-block durations -> p99
        self._last_flash = None          # heartbeat-flash t_cmd (phase-3 correlator input)
        self._hb_pending = deque(maxlen=16)   # HB_FLASH seqs awaiting their flash ev
        self._last_sync_burst = None     # last sync_burst result ev (manual optics check)
        self._onset_dedup = None         # (t, flip_ms) of the last onset — see _handle_up_event

        # Render ops that carry a result (sync_burst) are correlated by id: every
        # down-op is stamped with a monotonic "id" and the reader thread hands each
        # result to that id's waiter queue. _render_lock still serializes waiters so
        # the no-id fallback (renderer does not echo id yet) stays unambiguous.
        self._render_lock = threading.Lock()
        self._op_id_lock = threading.Lock()
        self._op_id = 0
        self._waiters = {}               # op id -> {"action":..., "q": queue.Queue}
        self._waiters_lock = threading.Lock()

        self._show_seq = 0

        # ── Desired renderer state (the reconcile target) ──
        # Every renderer generation is brought up to THIS, so "restart the
        # renderer" is an idempotent reconcile: a child that dies mid-session
        # comes back with the same lease, the same display config AND the same
        # stims loaded (before this, a respawn silently lost the NPZ and every
        # later SHOW was a no-op while /status still said RENDERER_UP).
        self._desired_lock = threading.Lock()
        self._desired = {"lease": "idle",
                         "stims_path": None,
                         "display_cfg": dict(self.display_cfg),
                         "sync_layout": dict(self.sync_layout)}

        # ── Session channel + pd ingest sockets ──
        # Bind failures are recorded, not raised: main() turns them into FAULT so
        # the daemon stays diagnosable over /status instead of crash-looping.
        self._bind_errors = []
        self._cmd_sock = self._bind_udp(self.display_port, "session channel")
        self._pd_sock = self._bind_udp(self.pd_port, "pd ingest")
        self._pd_buf = deque(maxlen=10000)   # bounded: phase 3's correlator drains it

        self.lease = {"mode": "idle", "holder": None}

    def _bind_udp(self, port, what):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            s.settimeout(1.0)
            return s
        except OSError as e:
            self._bind_errors.append("%s :%d: %s" % (what, port, e))
            log("[displayd] bind failed for %s :%d: %s" % (what, port, e))
            return None

    # ── Alarm / event ring ──

    def _event(self, kind, msg, **extra):
        """Ring + journald only (state transitions, informational)."""
        entry = {"t": time.time(), "level": "event", "kind": kind, "msg": msg}
        entry.update(extra)
        with self._ring_lock:
            self._ring.append(entry)
        log("[displayd] %s: %s" % (kind, msg))

    def _alarm(self, kind, msg, **extra):
        """Ring + one journald line + display_health UDP to the leader ack_port."""
        entry = {"t": time.time(), "level": "alarm", "kind": kind, "msg": msg}
        entry.update(extra)
        with self._ring_lock:
            self._ring.append(entry)
        log("[displayd] ALARM %s: %s" % (kind, msg))
        if self._leader_addr:
            pkt = {"type": "display_health", "state": self.state, "alarm": entry}
            try:
                self._ack_sock.sendto(json.dumps(pkt, default=str).encode(),
                                      self._leader_addr)
            except OSError:
                pass

    def _set_state(self, state, reason=None):
        prev = self.state
        self.state = state
        self.state_since = time.time()
        if state == ST_FAULT:
            self.fault_reason = reason
            self._alarm("fault", "FAULT(%s)" % reason)
        else:
            self.fault_reason = None
            self._event("state", "%s -> %s" % (prev, state))

    def _fault(self, reason):
        self._set_state(ST_FAULT, reason=reason)

    # ── Bringup state machine ──

    def bringup(self):
        """Walk CONFIG_OK..RENDERER_UP from the top (PROTOCOL: /bringup always
        re-walks). Any step failure -> FAULT(step: reason). Returns /status."""
        with self._bringup_lock:
            # Fresh walk: take down a running renderer first so the RENDERER_UP
            # step is a clean spawn (and DRM is free for the modeset checks).
            self._stop_renderer()
            self._renderer_fail_streak = 0
            self._set_state(ST_BOOT)
            steps = [
                (ST_CONFIG_OK, self._step_config_ok),
                (ST_DLPC_ALIVE, self._step_dlpc_alive),
                (ST_VIDEO_UP, self._step_video_up),
                (ST_DLPC_LOCKED, self._step_dlpc_locked),
                (ST_CURTAIN_DOWN, self._step_curtain_down),
                (ST_RENDERER_UP, self._step_renderer_up),
            ]
            for state, fn in steps:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 — every failure is a FAULT reason
                    self._fault("%s: %s" % (state, e))
                    return self.status()
                self._set_state(state)
            # OPTICS_OK is NOT claimed in phase 2: optics stays "unverified"
            # until a manual /render sync_burst + external confirmation (the L3
            # correlator arrives in phase 3). /status exposes this honestly.
            return self.status()

    def _step_config_ok(self):
        """DPI connector present + our mode listed. Plain presence check — no
        config.txt hash (the critique's note)."""
        stats = sorted(glob.glob("/sys/class/drm/card*-DPI-1/status"))
        if not stats:
            raise RuntimeError("no card*-DPI-1 connector under /sys/class/drm "
                               "(KMS config.txt not active?)")
        status_path = stats[0]
        with open(status_path) as f:
            status = f.read().strip()
        if status != "connected":
            raise RuntimeError("DPI-1 status is %r (want 'connected')" % status)
        modes_path = os.path.join(os.path.dirname(status_path), "modes")
        try:
            with open(modes_path) as f:
                modes = f.read()
        except OSError:
            modes = ""
        if self.mode_str not in modes:
            raise RuntimeError("DPI-1 modes lack %s (got: %s)"
                               % (self.mode_str, " ".join(modes.split()) or "none"))

    def _open_dlpc(self):
        """Instantiate + open the DLPC wrapper once. All bus access after this
        goes through dlpc's own internal lock (it serializes the I2C bus)."""
        if dlpc_mod is None:
            raise RuntimeError("dlpc import failed: %s" % _DLPC_IMPORT_ERROR)
        if self.dlpc is None:
            # Double-checked under a dedicated lock: BOTH the L1 poller thread and
            # the bringup thread call this. Two Dlpc instances would each carry
            # their own bus lock and _io_error flag while the SDK's module-global
            # callbacks bind to only one of them — defeating dlpc.py's stale-read
            # protection and leaking an i2c fd.
            with self._dlpc_open_lock:
                if self.dlpc is None:
                    d = dlpc_mod.Dlpc()
                    d.open()
                    self.dlpc = d
        return self.dlpc

    def _step_dlpc_alive(self):
        """One ShortStatus read succeeds. This proves DLPC LOGIC is powered —
        never report it as 'projector on' (the I2C rail can back-feed).
        dlpc reads report failures in the result dict, never by raising."""
        d = self._open_dlpc()
        res = d.read_short()
        if not res.get("ok"):
            raise RuntimeError("ShortStatus read failed: %s" % res.get("error"))

    def _step_video_up(self):
        """Drive the projector video-enable line high (bench recipe, GPIO25).
        pinctrl is idempotent, so re-walks are safe."""
        r = subprocess.run(["pinctrl", "set", "25", "op", "dh"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError("pinctrl set 25 op dh failed: %s"
                               % (r.stderr.strip() or r.stdout.strip()))

    def _step_dlpc_locked(self):
        """Full init sequence + independent source readback. Shared with the
        auto REINIT_DLPC repair (L1 source-revert) and /recover dlpc_reinit.
        No sleep here: init_parallel runs the 2.5 s light settle itself on its
        success path (dlpc.py contract — callers must not sleep again)."""
        d = self._open_dlpc()
        res = d.init_parallel()
        if not res.get("ok"):
            raise RuntimeError("init_parallel: %s" % res.get("error"))
        src = d.read_source()
        if not (src.get("ok") and _source_is_parallel(src)):
            raise RuntimeError("source readback %r != external parallel" % (src,))

    def _step_curtain_down(self):
        d = self._open_dlpc()
        cur = d.read_curtain()
        if not (cur.get("ok") and _curtain_is_off(cur)):
            raise RuntimeError("curtain readback %r != off" % (cur,))

    def _step_renderer_up(self):
        self._start_renderer()

    # ── Desired renderer state (reconcile target) ──

    def _set_desired(self, **kw):
        with self._desired_lock:
            self._desired.update(kw)

    def _desired_config(self):
        """The display device config the renderer must init its Display with —
        composed exactly like engine/follower.py:67-73 does it: the rig's
        devices.display block merged with the photodiode card's sync-square prefs
        (authored on the photodiode, DRAWN by the display). Without this the
        renderer inits on Display() defaults: wrong resolution/refresh_hz and the
        session sync square in the wrong corner, so every trial's photodiode sync
        fails silently."""
        with self._desired_lock:
            cfg = dict(self._desired.get("display_cfg") or {})
            cfg.update(self._desired.get("sync_layout") or {})
        return cfg

    def _mode_op(self):
        """The {"op":"mode"} op for the current desired lease, carrying the device
        config (the renderer applies it; sending it on every mode op keeps a live
        renderer in step when the sync layout is overridden)."""
        with self._desired_lock:
            lease = self._desired.get("lease") or "idle"
        if lease not in ("idle", "setup", "session"):
            lease = "idle"      # "external" means no renderer at all
        return {"op": "mode", "lease": lease, "config": self._desired_config()}

    def _replay_desired(self):
        """Bring a freshly spawned renderer generation up to the desired state:
        mode (+ config) first, then the stims if a session has loaded any."""
        op = self._mode_op()
        self._send_down(op)
        with self._desired_lock:
            path = self._desired.get("stims_path")
        if path:
            self._send_down({"op": "load_stims", "path": path})
        self._event("renderer", "replayed desired state: lease=%s stims=%s"
                    % (op["lease"], path or "none"))

    # ── Renderer supervision ──

    def _spawn_renderer(self):
        """Spawn /usr/bin/python3 renderer.py with fd 3 = the child end of an
        AF_UNIX SOCK_DGRAM socketpair. preexec dup2's the inherited fd onto 3
        (whatever the parent happens to hold at 3 is untouched — the dup2 runs
        in the CHILD, post-fork); pass_fds=[3] keeps slot 3 across the close-fds
        sweep and exec. Child inherits our stdout/stderr -> journald."""
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        child_fd = child.fileno()
        env = dict(os.environ)
        # kmsdrm only: a leaked DISPLAY would let SDL silently pick x11/offscreen
        # (the renderer asserts the driver, but don't hand it the footgun).
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        proc = subprocess.Popen(
            ["/usr/bin/python3", os.path.join(HERE, "renderer.py")],
            pass_fds=[3],
            preexec_fn=lambda: os.dup2(child_fd, 3),
            env=env)
        child.close()
        parent.settimeout(1.0)
        return proc, parent

    def _start_renderer(self, timeout=40.0):
        """Spawn + wait for the startup contract: the ready ev (self-test passed)
        AND the first flip report. Raises with the renderer's verbatim fatal
        reason on failure — bringup/supervisor turn that into FAULT."""
        with self._renderer_lock:
            self._renderer_gen += 1
            gen = self._renderer_gen
            self._renderer_fatal = None
            self._renderer_ready.clear()
            self._renderer_first_flip.clear()
            self._flip_ts.clear()
            self._flip_ms.clear()
            proc, sock = self._spawn_renderer()
            self._renderer_proc = proc
            self._renderer_sock = sock
            self._renderer_started_t = time.time()
            self._renderer_should_run = True
            threading.Thread(target=self._renderer_reader, args=(sock, gen),
                             daemon=True).start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._renderer_fatal:
                raise RuntimeError("renderer fatal: %s" % self._renderer_fatal)
            rc = proc.poll()
            if rc is not None:
                raise RuntimeError("renderer exited rc=%s before ready" % rc)
            if self._renderer_ready.is_set() and self._renderer_first_flip.is_set():
                # Reconcile this generation to the FULL desired state (lease +
                # display config + stims), not just the lease: a renderer that
                # died mid-session must come back able to SHOW.
                self._replay_desired()
                return
            time.sleep(0.1)
        raise RuntimeError("renderer start timeout (no ready/first flip in %.0fs)" % timeout)

    def _stop_renderer(self):
        """Polite quit op, then SIGTERM, then SIGKILL. Bumps the generation so
        the reader thread for this spawn unwinds on its next timeout."""
        with self._renderer_lock:
            proc = self._renderer_proc
            sock = self._renderer_sock
            self._renderer_should_run = False
            self._renderer_gen += 1
            self._renderer_proc = None
            self._renderer_sock = None
        if sock is not None:
            try:
                sock.send(json.dumps({"op": "quit"}).encode())
            except OSError:
                pass
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(2.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(2.0)
                    except subprocess.TimeoutExpired:
                        pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _next_op_id(self):
        with self._op_id_lock:
            self._op_id += 1
            return self._op_id

    def _send_down(self, op):
        """One JSON datagram down the socketpair. False if no renderer. Every op
        carries a monotonic "id" so a result ev can be correlated to its request
        (see _dispatch_result)."""
        with self._renderer_lock:
            sock = self._renderer_sock
        if sock is None:
            return False
        if "id" not in op:
            op = dict(op)
            op["id"] = self._next_op_id()
        try:
            sock.send(json.dumps(op).encode())
            return True
        except OSError as e:
            log("[displayd] send_down failed: %s" % e)
            return False

    def _renderer_reader(self, sock, gen):
        """Per-spawn thread: drain up-events. Exits when the generation moves on
        (respawn/stop closed our socket) — DGRAM pairs give no EOF, hence the
        1 s recv timeout + generation check."""
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                if gen != self._renderer_gen:
                    return
                continue
            except OSError:
                return
            if not data:
                return
            try:
                ev = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if gen != self._renderer_gen:
                return
            self._handle_up_event(ev)

    def _record_flip(self, t, flip_ms):
        """One physical flip -> the fps / p99 windows."""
        self._flip_ts.append(t)
        if flip_ms is not None:
            try:
                self._flip_ms.append(float(flip_ms))
            except (TypeError, ValueError):
                pass
        self._renderer_first_flip.set()

    def _dispatch_result(self, ev):
        """Hand a {"ev":"result"} to the waiter that asked for it. Correlation is
        by the "id" we stamped on the way down. NOTE: the renderer does not echo
        "id" yet (it sends only {"ev":"result","op":<action>}) — until it does,
        fall back to the OLDEST waiter for that action name (dicts preserve
        insertion order). TODO(renderer): echo the request "id" and this fallback
        can go away."""
        rid = ev.get("id")
        waiter = None
        with self._waiters_lock:
            if rid is not None:
                waiter = self._waiters.pop(rid, None)
            if waiter is None:
                for key, w in self._waiters.items():
                    if w["action"] == ev.get("op"):
                        waiter = self._waiters.pop(key)
                        break
        if waiter is not None:
            try:
                waiter["q"].put_nowait(ev)
            except queue.Full:
                pass

    def _handle_up_event(self, ev):
        name = ev.get("ev")
        if name == "ready":
            self._renderer_info = {"driver": ev.get("driver"),
                                   "fps": ev.get("fps"),
                                   "flip_ms": ev.get("flip_ms")}
            self._renderer_ready.set()
            self._event("renderer", "ready: driver=%s fps=%s flip_ms=%s"
                        % (ev.get("driver"), ev.get("fps"), ev.get("flip_ms")))
        elif name == "flip":
            t = ev.get("t", time.time())
            ms = ev.get("flip_ms")
            # The renderer reports the SAME physical flip twice on the onset frame
            # (an "onset" ev, then report_flip's "flip" ev with an identical
            # flip_ms a few hundred microseconds later). Counting both inflated
            # fps and p99 exactly during trials — drop the echo.
            dd = self._onset_dedup
            self._onset_dedup = None
            if (dd is not None and ms is not None and dd[1] is not None
                    and abs(float(ms) - float(dd[1])) < 0.011
                    and 0.0 <= t - dd[0] <= 0.05):
                return
            self._record_flip(t, ms)
        elif name == "onset":
            # SHOW's first stimulus flip -> stim_onset ack to the leader, the
            # exact shape follower.py sends today (the leader logs it as-is).
            t = ev.get("t", time.time())
            self._record_flip(t, ev.get("flip_ms"))
            self._onset_dedup = (t, ev.get("flip_ms"))
            if self._leader_addr:
                ack = {"type": "stim_onset", "trial": ev.get("trial"),
                       "t": ev.get("t")}
                try:
                    self._ack_sock.sendto(json.dumps(ack).encode(), self._leader_addr)
                except OSError:
                    pass
        elif name == "flash":
            self._last_flash = ev.get("t_cmd")
            # Flashes are serialized (one heartbeat_flash op -> one flip), so the OLDEST
            # pending HB_FLASH owns this flip: report t_flip to the leader's L3 correlator.
            # No ring entry for a correlated heartbeat — at one per hb_period they would
            # evict the alarm history /status reports; an unsolicited flash still gets one.
            seq = self._hb_pending.popleft() if self._hb_pending else None
            if seq is None:
                self._event("flash", "flash t_cmd=%s" % ev.get("t_cmd"))
            elif self._leader_addr:
                pkt = {"type": "hb_flash", "seq": seq, "t_flip": ev.get("t_cmd")}
                try:
                    self._ack_sock.sendto(json.dumps(pkt).encode(), self._leader_addr)
                except OSError:
                    pass
        elif name == "result":
            self._dispatch_result(ev)
            if ev.get("op") == "sync_burst":
                self._last_sync_burst = ev
        elif name == "fatal":
            self._renderer_fatal = "%s: %s" % (ev.get("reason"), ev.get("detail"))
            self._alarm("renderer_fatal", "renderer fatal %s" % self._renderer_fatal,
                        reason=ev.get("reason"))
        elif name == "log":
            log("[renderer] %s" % ev.get("line"))
        else:
            log("[displayd] unknown up-event: %r" % (ev,))

    def _supervisor_loop(self):
        """Watch the renderer child. Restart policy (PROTOCOL): surface fatal
        reasons verbatim, at most ONE blind retry, then the MODESET_KICK path,
        then FAULT. A healthy minute of flips resets the streak."""
        while not self._stop.is_set():
            time.sleep(1.0)
            with self._renderer_lock:
                proc = self._renderer_proc
                want = self._renderer_should_run
            if not want or proc is None:
                continue
            rc = proc.poll()
            if rc is None:
                # Reset the ladder on healthy UPTIME, not on a recent flip: the
                # renderer has no idle redraw loop, so "a flip in the last 2 s"
                # is false between trials and the streak never reset — three
                # unrelated restarts hours apart would FAULT the daemon.
                if (self._renderer_fail_streak
                        and time.time() - self._renderer_started_t > 60.0):
                    self._renderer_fail_streak = 0
                    self._event("renderer",
                                "restart ladder reset (renderer healthy > 60 s)")
                continue
            # Child died while it should be running.
            reason = self._renderer_fatal or ("exit rc=%s" % rc)
            self._alarm("renderer_died", "renderer died (%s)" % reason)
            # A fresh renderer's optics are unproven: nothing has confirmed that THIS process's
            # output reaches the diode. Carrying "ok" across a respawn would be a stale claim.
            self.optics = "unverified"
            self._optics_ok_streak = self._optics_lost_streak = 0
            if not self._bringup_lock.acquire(blocking=False):
                continue   # a bringup/recover already owns the machinery
            try:
                self._stop_renderer()   # reap + close our socket end
                self._renderer_fail_streak += 1
                self._renderer_restarts += 1
                try:
                    if self._renderer_fail_streak == 1:
                        self._set_state(ST_RENDERER_RESTART)
                        self._start_renderer()
                        self._set_state(ST_RENDERER_UP)
                    elif self._renderer_fail_streak == 2:
                        self._do_modeset_kick()
                    else:
                        self._fault("renderer: %s (retry + modeset kick exhausted)" % reason)
                except Exception as e:  # noqa: BLE001
                    self._fault("renderer restart failed: %s" % e)
            finally:
                self._bringup_lock.release()

    def _do_modeset_kick(self):
        """Stop renderer -> hold the mode with modetest for 2 s -> respawn.
        NEVER unbind vc4_dpi (kernel oops). `timeout 2` kills modetest — the
        bounded hold IS the kick, so the rc (124) is deliberately ignored."""
        self._set_state(ST_MODESET_KICK)
        self._stop_renderer()
        try:
            subprocess.run(
                ["timeout", "2", "modetest", "-M", "vc4", "-s",
                 "DPI-1:%s" % self.mode_str],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError("modetest kick failed to run: %s" % e)
        self._start_renderer()
        self._set_state(ST_RENDERER_UP)

    # ── Render ops with a result (sync_burst) ──

    def render_op(self, body, timeout=None):
        """Forward {"op":"render",...body} to the renderer and wait for its
        {"ev":"result"} answer.

        The waiter's queue is registered under this op's id BEFORE the op goes
        down, so a result can never land in a gap and be dropped (the old
        single-slot payload+event lost results whose reader-thread set raced the
        stale-result clear() — a SYNC_TEST then reported flashes:0/timeout for a
        burst that really ran, aborting the session). A quiet timeout on a
        non-burst action still returns ok — the op was delivered."""
        body = dict(body or {})
        # A POST /render body must never choose the op ("op":"quit" would kill
        # the renderer): strip it from the body, then force it after the update.
        body.pop("op", None)
        body.pop("id", None)
        action = body.get("action")
        op = {"op": "render"}
        op.update(body)
        op["op"] = "render"
        with self._render_lock:
            oid = self._next_op_id()
            op["id"] = oid
            q = queue.Queue(maxsize=4)
            with self._waiters_lock:
                self._waiters[oid] = {"action": action, "q": q}
            try:
                if not self._send_down(op):
                    return {"ok": False,
                            "error": "renderer not running (state %s)" % self.state}
                if timeout is None:
                    if action == "sync_burst":
                        timeout = float(body.get("duration_s", 1.0) or 1.0) + 10.0
                    else:
                        timeout = 2.0
                try:
                    result = q.get(timeout=max(0.0, float(timeout)))
                except queue.Empty:
                    result = None
            finally:
                with self._waiters_lock:
                    self._waiters.pop(oid, None)
        if result is not None:
            return {"ok": True, "result": result}
        if action == "sync_burst":
            return {"ok": False, "error": "timeout waiting for sync_burst result"}
        return {"ok": True, "result": None}

    # ── Session channel (UDP :5575) — follower.py-compatible ──

    def _session_loop(self):
        if self._cmd_sock is None:
            log("[displayd] session channel DOWN (bind failed) — no session commands")
            return
        log("[displayd] session channel on :%d" % self.display_port)
        while not self._stop.is_set():
            try:
                data, addr = self._cmd_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            cmd = msg.get("cmd")
            if cmd == "SHOW":
                self._show_seq += 1
                if not self._send_down({"op": "show", "seq": self._show_seq,
                                        "trial": msg.get("trial")}):
                    self._alarm("show_dropped",
                                "SHOW trial %s with no renderer (state %s)"
                                % (msg.get("trial"), self.state))
            elif cmd in ("BG", "BLANK"):
                # Session background = the NPZ background gray, which is what
                # follower.py's display.blank() painted. The setup-time "blank"
                # render action defaults gray_value=0.0 (BLACK), so flag this as
                # session-originated: the renderer calls the device's plain
                # blank() for it.
                self._send_down({"op": "render", "action": "blank",
                                 "use_session_bg": True})
            elif cmd == "LOAD_STIMS":
                # The leader loading stims marks the start of a session: flip the
                # lease so /status shows who owns the display right now, and record
                # both in the desired state so a renderer respawn reloads them.
                path = msg.get("path")
                self.lease = {"mode": "session", "holder": "leader"}
                self._set_desired(lease="session", stims_path=path)
                self._send_down(self._mode_op())
                self._send_down({"op": "load_stims", "path": path})
                self._event("session", "LOAD_STIMS %s" % path)
            elif cmd == "HB_FLASH":
                # Leader-clocked L3 heartbeat: flash the sync square once and let the
                # flash ev carry seq -> t_flip back (see _handle_up_event). A drop here
                # (no renderer) is deliberately quiet: the leader's own heartbeat_lost
                # rule is the alarm, and renderer death alarms on its own path.
                self._hb_pending.append(msg.get("seq"))
                if not self._send_down({"op": "heartbeat_flash"}):
                    self._hb_pending.pop()
            elif cmd == "SYNC_TEST":
                self._handle_sync_test(msg, addr)
            elif cmd == "QUIT":
                # follower.py exited here; the daemon NEVER does (systemd owns
                # our lifetime). Drop back to idle so the next session is clean.
                self._event("session", "QUIT received — ignored (daemon does not exit)")
                if self.lease["mode"] == "session":
                    self.lease = {"mode": "idle", "holder": None}
                    # Session over: forget its NPZ too, so a later respawn does
                    # not reload stims for a session that has ended.
                    self._set_desired(lease="idle", stims_path=None)
                    self._send_down(self._mode_op())
            elif cmd == "PING":
                try:
                    self._cmd_sock.sendto(
                        json.dumps({"cmd": "PONG", "state": self.state}).encode(), addr)
                except OSError:
                    pass
            else:
                # Seq-numbered SESSION_BEGIN arrives with the phase-3 leader —
                # tolerate unknown cmds with a log line, never a crash.
                log("[displayd] unknown session cmd: %r" % cmd)

    def _renderer_is_up(self):
        """True only when a renderer process is alive AND we have reached the
        RENDERER_UP milestone (i.e. it can actually draw)."""
        if self.state not in (ST_RENDERER_UP, ST_OPTICS_OK):
            return False
        with self._renderer_lock:
            proc = self._renderer_proc
            sock = self._renderer_sock
        return proc is not None and proc.poll() is None and sock is not None

    def _handle_sync_test(self, msg, addr):
        """Photodiode INIT verify: run a bounded sync_burst in the renderer and
        reply SYNC_TEST_DONE to the SENDER (the leader's ephemeral socket) —
        the exact reply shape engine/follower.py's _handle_sync_test sends, so
        the phase-2 leader works unchanged. Failures ride the reply (a bare
        {flashes:0} is indistinguishable from a healthy burst that emitted
        nothing)."""
        if not self._renderer_is_up():
            # SILENCE, not an error reply. engine/leader.py's
            # _sync_burst_follower re-sends SYNC_TEST for up to ~20 s waiting for
            # the display to come up, and treats ANY error reply as a hard abort
            # — answering {"ok":false,"error":"renderer not running"} during
            # bringup would fail GO instead of waiting. follower.py could not
            # reply before its display was open; match that.
            log("[displayd] SYNC_TEST ignored — renderer not up (state %s)" % self.state)
            return
        # The leader ships the photodiode card's layout with the request; adopt it
        # as the desired sync layout so the renderer (this generation and every
        # later one) draws the session sync square in the same place.
        overrides = {k: msg[k] for k in
                     ("sync_corner", "sync_size_px", "sync_brightness") if k in msg}
        if overrides and overrides != {k: self.sync_layout.get(k) for k in overrides}:
            self.sync_layout.update(overrides)
            self._set_desired(sync_layout=dict(self.sync_layout))
            self._send_down(self._mode_op())
            self._event("sync_layout", "sync layout override from leader: %s" % overrides)
        params = dict(self.sync_layout)   # rig-yaml defaults (photodiode card)
        for k in ("sync_corner", "sync_size_px", "sync_brightness",
                  "every_n", "duration_s"):
            if k in msg:
                params[k] = msg[k]
        params.setdefault("every_n", 5)
        params.setdefault("duration_s", 1.0)
        params["action"] = "sync_burst"
        flashes = 0
        error = None
        res = self.render_op(params)
        if res.get("ok") and res.get("result"):
            r = res["result"]
            if r.get("error"):
                error = str(r["error"])
            flashes = int(r.get("flashes", 0) or 0)
        else:
            error = res.get("error") or "no result from renderer"
        reply = {"cmd": "SYNC_TEST_DONE", "flashes": flashes}
        if error:
            reply["ok"] = False
            reply["error"] = error
            log("[displayd] SYNC_TEST error: %s" % error)
        try:
            self._cmd_sock.sendto(json.dumps(reply).encode(), addr)
        except OSError:
            pass

    # ── Photodiode pulse ingest (UDP :5582) ──

    def _pd_loop(self):
        """Phase 2: bind + buffer only. The L3 onset/pulse correlator (phase 3)
        drains this deque; bounded so a chatty leader can't grow us unbounded."""
        if self._pd_sock is None:
            log("[displayd] pd ingest DOWN (bind failed)")
            return
        log("[displayd] pd ingest on :%d" % self.pd_port)
        while not self._stop.is_set():
            try:
                data, _addr = self._pd_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            now = time.time()
            try:
                msg = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                msg = None
            if isinstance(msg, dict):
                entry = {"t_recv": now}
                entry.update(msg)
                if msg.get("type") == "hb_verdict":
                    self._note_hb_verdict(msg)
            else:
                entry = {"t_recv": now, "raw": data.decode("utf-8", "replace")}
            self._pd_buf.append(entry)

    def _note_hb_verdict(self, msg):
        """One heartbeat verdict from the leader's L3 correlator. Streak thresholds match the
        leader's own (HB_OK_N / HB_LOST_N) so /status and the leader's alarm agree rather than
        telling the operator two different stories."""
        confirmed = bool(msg.get("confirmed"))
        if confirmed:
            self._optics_lost_streak = 0
            self._optics_ok_streak += 1
            if self._optics_ok_streak >= OPTICS_OK_N and self.optics != "ok":
                self.optics = "ok"
                self._event("optics", "optics confirmed by photodiode (L3)")
        else:
            self._optics_ok_streak = 0
            self._optics_lost_streak += 1
            if self._optics_lost_streak >= OPTICS_LOST_N and self.optics != "lost":
                self.optics = "lost"
                # No _alarm here: the leader raises heartbeat_lost off the same evidence and
                # already pages. Two alarms for one fault is how a pager gets ignored.
                self._event("optics", "optics NOT confirmed by photodiode (L3)")

    # ── L1 poller (DLPC health, 1 Hz) ──

    def _l1_loop(self):
        """Continuous from daemon start (bringup or not — logic power is worth
        watching even in FAULT). One sweep/second, ~8.6 ms of bus time."""
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._l1_sweep_once()
            except Exception as e:  # noqa: BLE001 — the poller must never die
                log("[displayd] L1 poller error: %s" % e)
            # Hold the 1 Hz cadence regardless of sweep duration.
            delay = max(0.1, 1.0 - (time.time() - t0))
            self._stop.wait(delay)

    def _l1_sweep_once(self):
        if self.dlpc is None:
            # Open lazily so a daemon started before I2C is up self-heals; back
            # off to 5 s while unopenable to keep the journal quiet.
            try:
                self._open_dlpc()
            except Exception as e:  # noqa: BLE001
                if not self._dlpc_unreachable:
                    self._dlpc_unreachable = True
                    self._alarm("dlpc_unreachable", "DLPC open failed: %s" % e)
                self._stop.wait(4.0)
                return
        res = self.dlpc.sweep()
        now = time.time()
        self._dlpc_snapshot = res
        self._dlpc_snapshot_t = now

        reads = res.get("reads") or {}
        errors = res.get("errors") or {}
        # sweep().ok is False when ANY ONE of the 18 reads failed — that is NOT a
        # power-loss signal. Only "nothing answered at all" (or the identity read
        # itself NAKing) means the DLPC is unreachable; anything else is PARTIAL
        # data, and bailing out on it silently disabled fault-bit latching, the
        # actuator-watchdog alarm and the SourceSelect auto-repair for the whole
        # outage while spamming a false logic-power alarm.
        if errors and (not _live_reads(reads) or _IDENTITY_READ in errors):
            if not self._dlpc_unreachable:
                self._dlpc_unreachable = True
                self._alarm("dlpc_unreachable", "DLPC NAK — logic power lost?",
                            errors=errors)
            return
        if self._dlpc_unreachable:
            self._dlpc_unreachable = False
            self._event("dlpc", "DLPC reachable again (logic powered)")
        if errors:
            self._note_read_errors(errors, len(reads), now)
        elif self._read_err_names:
            self._read_err_names = frozenset()
            self._read_err_t = 0.0
            self._event("dlpc", "all DLPC reads answering again")

        # Latch semantics: DLPC status bits clear on read, so the first sweep
        # after an event is history. Only FAULT-shaped bits latch — a healthy
        # register is full of 1s that mean "fine" (SystemInitialized, LED
        # states/enables, ActuatorDriveEnable, Application=MainApp), and
        # latching those buries the real breadcrumbs. Retention rule (chosen over
        # "clear once reported", which deleted a bit on the very next sweep — the
        # same second — and left /status.dlpc.latched permanently empty): a bit
        # stays latched until LATCH_RETENTION_S after it was LAST seen asserted,
        # keeping its original first-seen t as the published "since".
        def _is_fault_bit(name):
            n = name.lower()
            return ("error" in n or "timeout" in n or "abort" in n
                    or n == "dcpowersupply" or "notfound" in n)
        flags_now = set()
        for rname, rvals in reads.items():
            if not isinstance(rvals, dict):
                continue
            for k, v in rvals.items():
                if (v is True or v == 1) and _is_fault_bit(k):
                    flags_now.add("%s.%s" % (rname, k))
        new_bits = []
        for bit in flags_now:
            if bit not in self._latched:
                self._latched[bit] = now
                new_bits.append(bit)
            self._latched_last[bit] = now
        for bit in list(self._latched):
            last = self._latched_last.get(bit, self._latched[bit])
            if now - last > LATCH_RETENTION_S:
                del self._latched[bit]
                self._latched_last.pop(bit, None)
                self._event("dlpc", "latched bit expired after %ds: %s"
                            % (int(LATCH_RETENTION_S), bit))

        # Detections (edge-triggered on the new latch, not re-alarmed per sweep):
        for bit in new_bits:
            if "actuatorwatchdogtimertimeout" in bit.lower():
                self._alarm("input_signal_lost",
                            "DLPC actuator watchdog timeout — input signal lost",
                            bit=bit)
        self._check_source_revert(reads)

    def _note_read_errors(self, errors, n_ok, now):
        """Partial-sweep breadcrumb: some reads NAKed while the part is clearly
        answering. Deliberately an EVENT, not the dlpc_unreachable alarm (which
        means logic power lost). Throttled: re-reported when the failing set
        changes, otherwise once a minute."""
        names = frozenset(errors)
        if names == self._read_err_names and now - self._read_err_t < 60.0:
            return
        self._read_err_names = names
        self._read_err_t = now
        self._event("dlpc_read_errors",
                    "%d/%d DLPC reads failed (%s)"
                    % (len(names), len(names) + n_ok, ", ".join(sorted(names))),
                    errors=dict(errors))

    def _check_source_revert(self, reads):
        """SourceSelect != parallel after lock -> auto REINIT_DLPC. Throttled so
        a source that won't stick alarms once per 30 s instead of thrashing the
        bus; skipped when a bringup/recover already holds the machinery."""
        # Exact read name from dlpc._READS — a substring scan would fall back to
        # ExternalVideoSourceFormatSelect when this read NAKs and false-alarm.
        src_val = reads.get("SourceSelect")
        if src_val is None or _source_is_parallel(src_val):
            return
        if self.state not in _LOCKED_STATES:
            return
        now = time.time()
        if now - self._last_auto_reinit < 30.0:
            return
        self._last_auto_reinit = now
        self._alarm("source_revert",
                    "DLPC source reverted (%r) — auto REINIT_DLPC" % (src_val,))
        if not self._bringup_lock.acquire(blocking=False):
            return
        try:
            prev = self.state
            self._set_state(ST_REINIT_DLPC)
            try:
                self._step_dlpc_locked()
                self._set_state(prev)
                self._event("dlpc", "REINIT_DLPC ok — source locked again")
            except Exception as e:  # noqa: BLE001
                self._fault("REINIT_DLPC: %s" % e)
        finally:
            self._bringup_lock.release()

    # ── /recover actions ──

    def recover(self, action):
        with self._bringup_lock:
            try:
                if action == "modeset_kick":
                    self._do_modeset_kick()
                elif action == "dlpc_reinit":
                    prev = self.state
                    self._set_state(ST_REINIT_DLPC)
                    self._step_dlpc_locked()
                    # Restore the pre-repair state when it was a healthy locked
                    # one; from FAULT/early states only claim what was verified.
                    self._set_state(prev if prev in _LOCKED_STATES else ST_DLPC_LOCKED)
                elif action == "renderer_restart":
                    self._set_state(ST_RENDERER_RESTART)
                    self._stop_renderer()
                    self._renderer_restarts += 1
                    self._start_renderer()
                    self._set_state(ST_RENDERER_UP)
                else:
                    return {"ok": False, "error": "unknown recover action %r" % action}
            except Exception as e:  # noqa: BLE001
                self._fault("recover %s: %s" % (action, e))
        return self.status()

    # ── Lease ──

    def set_lease(self, mode, holder=None):
        """setup: renderer switches to its setup surface. external: an outside
        process (calibration tools) needs DRM master, so the renderer must DIE
        until /release — KMS allows one master per card."""
        if mode not in ("setup", "external"):
            return {"ok": False, "error": "lease mode must be 'setup' or 'external'"}
        self.lease = {"mode": mode, "holder": holder}
        self._set_desired(lease=mode)
        if mode == "setup":
            self._send_down(self._mode_op())
        else:
            with self._bringup_lock:
                self._stop_renderer()
        self._event("lease", "lease -> %s (%s)" % (mode, holder))
        return self.status()

    def release_lease(self):
        was_external = self.lease["mode"] == "external"
        self.lease = {"mode": "idle", "holder": None}
        self._set_desired(lease="idle")
        if was_external:
            # Take the display back from the outside process.
            with self._bringup_lock:
                try:
                    self._set_state(ST_RENDERER_RESTART)
                    self._start_renderer()
                    self._set_state(ST_RENDERER_UP)
                except Exception as e:  # noqa: BLE001
                    self._fault("release/respawn: %s" % e)
        else:
            self._send_down(self._mode_op())
        self._event("lease", "lease released")
        return self.status()

    # ── /status ──

    def _renderer_stats(self):
        with self._renderer_lock:
            proc = self._renderer_proc
        pid = proc.pid if (proc is not None and proc.poll() is None) else None
        fps = None
        ts = list(self._flip_ts)
        if len(ts) >= 2 and time.time() - ts[-1] < 2.0 and ts[-1] > ts[0]:
            fps = round((len(ts) - 1) / (ts[-1] - ts[0]), 2)
        p99 = None
        ms = sorted(self._flip_ms)
        if ms:
            p99 = round(ms[min(len(ms) - 1, int(0.99 * len(ms)))], 2)
        return {"pid": pid, "fps": fps, "flip_ms_p99": p99,
                "restarts": self._renderer_restarts,
                "info": self._renderer_info}

    def status(self):
        with self._ring_lock:
            alarms = [e for e in self._ring if e["level"] == "alarm"][-20:]
        st = {
            "state": self.state,
            "since": self.state_since,
            "optics": self.optics,
            "dlpc": {
                "sweep": self._dlpc_snapshot,
                "sweep_t": self._dlpc_snapshot_t,
                "latched": dict(self._latched),
                # Reads that NAKed in the last sweep while the part still
                # answered (partial data, NOT a power loss — see _l1_sweep_once).
                "read_errors": sorted(self._read_err_names),
            },
            "renderer": self._renderer_stats(),
            "lease": dict(self.lease),
            "alarms": alarms,
        }
        if self.fault_reason:
            st["fault"] = self.fault_reason
        if self._last_sync_burst:
            st["last_sync_burst"] = self._last_sync_burst
        return st

    def history(self, n=100):
        with self._ring_lock:
            return list(self._ring)[-max(1, int(n)):]

    # ── Lifecycle ──

    def start_threads(self):
        for name, fn in (("l1", self._l1_loop),
                         ("session", self._session_loop),
                         ("pd", self._pd_loop),
                         ("supervisor", self._supervisor_loop)):
            threading.Thread(target=fn, name=name, daemon=True).start()

    def shutdown(self):
        self._stop.set()
        self._stop_renderer()
        for sock in (self._cmd_sock, self._pd_sock, self._ack_sock):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass
        if self.dlpc is not None:
            try:
                self.dlpc.close()
            except Exception:  # noqa: BLE001
                pass


# ── Control REST (:5581, 127.0.0.1 only) ──

class _CtlHandler(BaseHTTPRequestHandler):
    displayd = None   # set once before serve_forever

    def log_message(self, fmt, *args):
        pass   # per-request lines are journald noise; the ring is the log

    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            obj = json.loads(self.rfile.read(n) or b"{}")
            return obj if isinstance(obj, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        d = self.displayd
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/status":
            self._json(200, d.status())
        elif parsed.path == "/health/history":
            q = urllib.parse.parse_qs(parsed.query)
            try:
                n = int(q.get("n", ["100"])[0])
            except ValueError:
                n = 100
            self._json(200, d.history(n))
        else:
            self._json(404, {"ok": False, "error": "unknown path %s" % parsed.path})

    def do_POST(self):
        d = self.displayd
        body = self._body()
        try:
            if self.path == "/bringup":
                self._json(200, d.bringup())
            elif self.path == "/lease":
                self._json(200, d.set_lease(body.get("mode"), body.get("holder")))
            elif self.path == "/release":
                self._json(200, d.release_lease())
            elif self.path == "/render":
                self._json(200, d.render_op(body))
            elif self.path == "/recover":
                self._json(200, d.recover(body.get("action")))
            else:
                self._json(404, {"ok": False, "error": "unknown path %s" % self.path})
        except Exception as e:  # noqa: BLE001 — a handler bug must not kill the server
            self._json(500, {"ok": False, "error": str(e)})


def main():
    parser = argparse.ArgumentParser(description="VRFarm display daemon (phase 2)")
    parser.add_argument("--rig", required=True, help="Rig yaml path")
    args = parser.parse_args()

    # A missing / empty / malformed rig yaml must NOT crash-loop under
    # Restart=always: the daemon's whole premise is that it stays diagnosable
    # over /status, so come up on defaults and report FAULT with the reason.
    config_error = None
    rig = {}
    try:
        with open(args.rig) as f:
            loaded = yaml.safe_load(f)
        if loaded is None:
            raise RuntimeError("rig yaml is empty")
        if not isinstance(loaded, dict):
            raise RuntimeError("rig yaml is not a mapping (got %s)"
                               % type(loaded).__name__)
        rig = loaded
    except Exception as e:  # noqa: BLE001 — any load failure is a FAULT reason
        config_error = "%s: %s" % (type(e).__name__, e)
        log("[displayd] rig config load failed (%s) — starting in FAULT" % config_error)

    d = Displayd(rig, args.rig)
    if _DLPC_IMPORT_ERROR:
        d._alarm("dlpc_import", "dlpc module unavailable: %s" % _DLPC_IMPORT_ERROR)

    # Localhost-only control plane. ThreadingHTTPServer: /bringup blocks for
    # seconds (light settle + renderer self-test) and /status must stay live.
    # This one bind IS fatal — without /status there is nothing to diagnose with
    # — but say so in one journald line instead of a bare traceback loop.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", d.ctl_port), _CtlHandler)
    except OSError as e:
        log("[displayd] FATAL: control REST cannot bind 127.0.0.1:%d (%s) — "
            "another displayd instance?" % (d.ctl_port, e))
        return 1
    server.daemon_threads = True
    _CtlHandler.displayd = d
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("[displayd] control REST on 127.0.0.1:%d" % d.ctl_port)

    d.start_threads()

    # systemd stops us with SIGTERM; treat ^C the same for bench runs.
    def _sig(_signum, _frame):
        d._stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    # READY once the sockets are bound, then walk bringup in the background so
    # systemd's startup timeout never races the 2.5 s light settle. A broken
    # config or a failed UDP bind skips bringup and parks in FAULT instead —
    # the daemon stays up and /status names the reason (POST /bringup can still
    # be used to retry by hand once the cause is fixed).
    sd_notify("READY=1")
    if config_error:
        d._fault("config: %s (%s)" % (config_error, args.rig))
    elif d._bind_errors:
        d._fault("bind: %s" % "; ".join(d._bind_errors))
    else:
        threading.Thread(target=d.bringup, daemon=True).start()

    # Main loop = watchdog heartbeat (WatchdogSec=15; ping every 5 s).
    while not d._stop.wait(5.0):
        sd_notify("WATCHDOG=1")

    log("[displayd] shutting down")
    d.shutdown()
    server.shutdown()


if __name__ == "__main__":
    sys.exit(main() or 0)
