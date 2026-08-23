#!/usr/bin/env python3
"""
displayd/renderer.py

Renderer child of displayd — the sole DRM master on the display Pi. Spawned by
displayd/displayd.py ONLY, with fd 3 = its end of an AF_UNIX SOCK_DGRAM socketpair
(one JSON object per datagram, both directions — see displayd/PROTOCOL.md).

Runs on the SYSTEM python3 (trixie, 3.13): the conda env's SDL has no kmsdrm backend,
so this process must never start under conda. All drawing lives in devices/display.py
(imported from ~/rig); this file is the command loop + the startup contract:

  1. SDL env pinned to kmsdrm before any pygame import; DISPLAY removed (an inherited
     X display would let SDL pick x11 and silently miss the DRM path).
  2. Display.start_display(kms=True): FULLSCREEN|SCALED vsync=1, driver asserted KMSDRM.
  3. 30-frame self-test: fps within 57.46±1.5 AND mean flip-block >= 8 ms — a
     non-blocking flip means vsync was not actually granted (frames tear/queue), and a
     wrong fps means the DLPC isn't being fed the mode we think.
  4. {"ev":"ready"} up the socketpair on pass; {"ev":"fatal"} + exit(3) on any failure,
     so displayd can surface the reason verbatim instead of guessing from a dead child.

The Display is initialized from the composed display config displayd sends in the "mode"
op ({"op":"mode","lease":...,"config":{...}}) — resolution/refresh_hz and the photodiode
sync-square layout. That op can arrive before step 2, so start() drains it first; an older
displayd sends no config and the Display defaults still apply. Parent death is watched
explicitly (PR_SET_PDEATHSIG + a getppid() check): a SOCK_DGRAM socketpair never reports
EOF, and an orphaned renderer would hold DRM master forever.

Session drawing (load_stims/show) is a port of engine/follower.py's NPZ handling with
the leader ACK replaced by an {"ev":"onset"} report; the "render" op carries the old
engine/display_worker.py setup actions unchanged. Between ops the last flipped frame
simply stays on the front buffer — no redraw loop, the poll timeout only bounds
command latency.
"""

from __future__ import annotations

import os

# SDL env — MUST be pinned before pygame is imported (devices/display.py imports it
# lazily inside methods, so setting these at module top is early enough).
os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
os.environ["SDL_HINT_NO_SIGNAL_HANDLERS"] = "1"   # per contract (matches devices/display.py)
# SDL2 reads hints from env by the hint's SDL-internal name, which for this hint is
# SDL_NO_SIGNAL_HANDLERS (no HINT_). Set both spellings so the hint actually takes:
# SDL must leave SIGTERM to Python or displayd's terminate() can't stop us cleanly.
os.environ["SDL_NO_SIGNAL_HANDLERS"] = "1"
os.environ.pop("DISPLAY", None)                   # never let SDL wander off to X11

import json
import select
import socket
import sys
import time
from collections import deque
from pathlib import Path

# devices/display.py lives one level up (~/rig on the Pi, the repo root in dev).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from devices.display import Display

# Self-test acceptance window (PROTOCOL.md, renderer startup contract clause 4).
TARGET_FPS = 57.46          # DLPC3436 parallel-RGB mode actual refresh
FPS_TOL = 1.5
MIN_FLIP_BLOCK_MS = 8.0     # vsync-locked flip must block a real fraction of the 17.4 ms frame
SELF_TEST_FRAMES = 30
SELF_TEST_WARMUP = 5        # unmeasured settle flips (first flips after set_mode are irregular)

# Throttle for {"ev":"flip"} inside per-frame loops. This used to be 1/60 s, which was a
# NO-OP: the panel frame period is 1/57.46 = 17.4 ms > 16.7 ms, so every single stimulus
# frame passed the test (~57 json.dumps + sendto per second inside the vsync-paced loop).
# displayd only derives an fps estimate and a flip_ms p99 from these, so a 0.25 s sample
# is plenty; timing-critical events (onset) are never throttled. See report_flip.
FLIP_REPORT_MIN_INTERVAL = 0.25

PARENT_CHECK_INTERVAL = 1.0   # s between getppid() checks (see Renderer.check_parent)

WARP_PATH = Path.home() / "rig" / "calibration" / "warp_map.npz"


def _install_parent_death_signal() -> bool:
    """Best-effort PR_SET_PDEATHSIG(SIGTERM): ask the kernel to signal us the instant
    displayd dies. Needed because an AF_UNIX SOCK_DGRAM socketpair never reports EOF when
    the peer closes — a SIGKILLed displayd leaves this process alive forever as the SOLE
    DRM master, and every later displayd bringup then fails its KMSDRM assert.

    Linux-specific and cleared across exec in some spawn paths, so it is only half the
    answer: Renderer.check_parent() watches getppid() as the portable backstop."""
    try:
        import ctypes
        import signal

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        return libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0) == 0
    except Exception:
        return False


class _SockStop:
    """Duck-typed stop event for Display.run_sync_test. The flash loop blocks the (only)
    thread, but its stop command arrives on the same socketpair — so is_set(), called once
    per frame by the loop, drains the socket non-blockingly: stop_sync/quit break the loop,
    anything else is deferred to the main loop's pending queue. Avoids the receiver thread
    display_worker.py needed (here every datagram already funnels through one place)."""

    def __init__(self, renderer):
        self._r = renderer
        self.stopped = False
        self.quit = False

    def is_set(self):
        if self.stopped:
            return True
        if not self._r.check_parent():
            # displayd died mid-flash-loop: end the loop so the renderer can exit and
            # release DRM master (this loop would otherwise flash forever).
            self.stopped = self.quit = True
            return True
        for msg in self._r.drain_nonblocking():
            op = msg.get("op")
            if op == "quit":
                self.stopped = self.quit = True
            elif op == "render" and msg.get("action") == "stop_sync":
                self.stopped = True
            else:
                self._r.pending.append(msg)
        return self.stopped


class Renderer:
    def __init__(self, sock):
        self.sock = sock
        self.dev = None
        self.stims = None            # dict(np.load(...)) — same shape follower.py uses
        self.sync_every_n = 0
        self.lease = "idle"
        self.pending = deque()       # ops deferred while a blocking flash loop owned the thread
        self._display_config = {}    # composed display config from displayd's "mode" op
        self._last_flip_report = 0.0
        self._flips_since_report = 0
        self._ppid = os.getppid()    # displayd; a change means it died (see check_parent)
        self._last_parent_check = time.time()
        self._quit = False

    # ── socketpair I/O ──

    def send(self, obj: dict):
        """One JSON datagram up to displayd. Best-effort: if displayd is gone the parent
        death is handled by its supervisor respawning us, not by us erroring out mid-frame."""
        try:
            self.sock.send(json.dumps(obj).encode())
        except OSError:
            pass

    def log(self, line: str):
        print(f"[renderer] {line}", flush=True)
        self.send({"ev": "log", "line": line})

    def drain_nonblocking(self):
        """All datagrams currently queued on the socketpair, parsed; never blocks."""
        msgs = []
        while True:
            r, _, _ = select.select([self.sock], [], [], 0)
            if not r:
                return msgs
            try:
                data = self.sock.recv(65536)
            except OSError:
                return msgs
            try:
                msgs.append(json.loads(data.decode()))
            except Exception:
                self.log(f"bad datagram ({len(data)} bytes) dropped")

    def report_flip(self, flip_ms: float, force: bool = False):
        """{"ev":"flip"} for this flip. flip_ms brackets the whole draw call (blit + flip);
        with prewarmed cached surfaces the blit is ~1 ms so it approximates the vsync block,
        and a cold surface build shows up honestly as a slow frame.

        Throttled to one report per FLIP_REPORT_MIN_INTERVAL so a per-frame loop does not
        pay a JSON encode + datagram send on every vsync (the old 1/60 s throttle never
        fired — see the constant). force=True for one-shot renders (startup blank, setup
        render actions, heartbeat restore): those are a handful per second at most, and
        displayd's RENDERER_UP transition waits for the very first flip report.

        "n" counts the flips represented by this report (this one included), so fps can be
        recovered exactly as sum(n)/span. displayd's _renderer_stats currently counts
        datagrams, so with the real throttle its "fps" is a SAMPLE rate, not the frame
        rate, until it uses "n"; flip_ms p99 is likewise a sample of frame cost."""
        self._flips_since_report += 1
        now = time.time()
        if not force and now - self._last_flip_report < FLIP_REPORT_MIN_INTERVAL:
            return
        n = self._flips_since_report
        self._flips_since_report = 0
        self._last_flip_report = now
        self.send({"ev": "flip", "t": now, "flip_ms": round(flip_ms, 2), "n": n})

    def _mark_flip_reported(self, t: float):
        """Account a flip that an {"ev":"onset"} already reported (displayd counts onset as
        a flip sample), so the loop's next flip report is a full interval later and the
        frame is not double-counted in "n"."""
        self._last_flip_report = t
        self._flips_since_report = 0

    def check_parent(self) -> bool:
        """False (and _quit set) once displayd is gone. The socketpair can NOT tell us: an
        AF_UNIX SOCK_DGRAM pair never reports EOF when the peer closes, so a SIGKILLed
        displayd would leave this process running forever as the sole DRM master and every
        future displayd bringup would fail its KMSDRM assert. PR_SET_PDEATHSIG covers most
        cases; this getppid() watch covers the rest (re-parenting to init/a subreaper).
        Throttled to PARENT_CHECK_INTERVAL — it is called from per-frame paths too."""
        now = time.time()
        if now - self._last_parent_check < PARENT_CHECK_INTERVAL:
            return True
        self._last_parent_check = now
        ppid = os.getppid()
        if ppid == self._ppid:
            return True
        self.log("displayd is gone (re-parented to pid %d) — exiting so DRM master is "
                 "released for the next bringup" % ppid)
        self._quit = True
        return False

    # ── startup ──

    def fatal(self, reason: str, detail):
        self.send({"ev": "fatal", "reason": reason, "detail": str(detail)})
        print(f"[renderer] FATAL {reason}: {detail}", file=sys.stderr, flush=True)
        sys.exit(3)

    def start(self):
        """Startup contract: kms display up, driver asserted, self-test passed, ready sent.
        Any failure -> fatal + exit(3); displayd surfaces the reason and decides on retry."""
        # displayd may already have queued the "mode" op — which now carries the composed
        # display config — before we got here (a datagram just sits in the socket buffer).
        # Drain it FIRST so Display.init() sees the real rig config; everything else waits
        # in self.pending for the main loop, since the display is not up yet.
        for msg in self.drain_nonblocking():
            if msg.get("op") == "mode":
                self.apply_mode(msg)
            else:
                self.pending.append(msg)

        cfg = dict(self._display_config)
        # Display.init reads background_gray from task_params (load_stims later overrides it
        # from the NPZ, exactly as engine/follower.py does).
        task_params = ({"background_gray": cfg["background_gray"]}
                       if "background_gray" in cfg else {})
        self.dev = Display()
        try:
            self.dev.init(rig_config=cfg, task_params=task_params)
        except Exception as e:
            # A malformed display config must be loud: silently falling back to defaults is
            # the exact failure this plumbing exists to end.
            self.fatal("BAD_CONFIG", e)
        if cfg:
            self.log("display config from displayd: %sx%s @ %s Hz, sync %s size=%s "
                     "brightness=%s"
                     % (self.dev.resolution[0], self.dev.resolution[1], self.dev.refresh_hz,
                        self.dev.sync_corner, self.dev.sync_size_px or "auto",
                        self.dev.sync_brightness))
        else:
            self.log("no display config from displayd (older daemon?) — Display defaults: "
                     "%sx%s, sync %s" % (self.dev.resolution[0], self.dev.resolution[1],
                                         self.dev.sync_corner))
        try:
            self.dev.start_display(kms=True)
        except RuntimeError as e:
            # The kms path raises RuntimeError only for the driver assert — the silent
            # null-backend fallback the contract exists to catch.
            self.fatal("NULL_BACKEND", e)
        except Exception as e:
            self.fatal("SDL_ERROR", e)

        try:
            fps, med_block, drops = self._self_test()
        except Exception as e:
            self.fatal("SDL_ERROR", e)
        # Fail on pace or on a non-blocking flip; a few dropped frames during bringup are
        # reported, not fatal (>1/3 of the window means something is genuinely wrong).
        if (abs(fps - TARGET_FPS) > FPS_TOL or med_block < MIN_FLIP_BLOCK_MS
                or drops > SELF_TEST_FRAMES // 3):
            self.fatal("FPS_OUT_OF_RANGE",
                       f"fps={fps:.2f} (want {TARGET_FPS}±{FPS_TOL}), "
                       f"median flip-block={med_block:.1f} ms (want >= {MIN_FLIP_BLOCK_MS}), "
                       f"drops={drops}/{SELF_TEST_FRAMES}")
        if drops:
            self.log(f"self-test: {drops}/{SELF_TEST_FRAMES} long frames (pace ok at "
                     f"{fps:.2f} fps)")
        self.send({"ev": "ready", "driver": "KMSDRM",
                   "fps": round(fps, 2), "flip_ms": round(med_block, 2), "drops": drops})

        # Warp: same location + semantics as follower.run(). Without it, shows fall back
        # to the flat show_rect path and the sync square loses its off-screen dead band.
        if self.dev.load_warp(str(WARP_PATH)):
            self.log(f"warp map loaded from {WARP_PATH}")
        else:
            self.log(f"no warp map at {WARP_PATH} — stimuli use flat fallback")

        # First real frame: session background. Also the first {"ev":"flip"} report —
        # displayd's RENDERER_UP transition waits for one.
        t0 = time.perf_counter()
        self.dev.blank()
        self.report_flip((time.perf_counter() - t0) * 1000.0, force=True)

    def _self_test(self):
        """30 measured flips of a trivial frame. fps comes from the MEDIAN frame interval,
        not the elapsed span: one dropped frame in 30 drags a span-derived mean to 55.6 fps
        (29 x 17.4 ms + one 34.8 ms frame) and would fail a perfectly vsync-locked display —
        seen on the rig during bringup, where a stray long frame right after modeset is
        normal. The median answers the real question ("is the pace one refresh period?"),
        drops are counted and reported separately, and flip-block (median time inside
        pygame.display.flip()) is what actually separates a locked flip from a free-running
        one."""
        import pygame
        scr = self.dev._screen
        for _ in range(SELF_TEST_WARMUP):
            scr.fill((0, 0, 0))
            pygame.display.flip()
        blocks, dts = [], []
        prev = time.perf_counter()
        for _ in range(SELF_TEST_FRAMES):
            scr.fill((0, 0, 0))
            t0 = time.perf_counter()
            pygame.display.flip()
            t1 = time.perf_counter()
            blocks.append((t1 - t0) * 1000.0)
            dts.append(t1 - prev)
            prev = t1
        med_dt = sorted(dts)[len(dts) // 2]
        fps = (1.0 / med_dt) if med_dt > 0 else 0.0
        nominal = 1.0 / TARGET_FPS
        drops = sum(1 for d in dts if d > 1.5 * nominal)
        return fps, sorted(blocks)[len(blocks) // 2], drops

    # ── main loop ──

    def run(self):
        while not self._quit:
            if not self.check_parent():
                break      # displayd died; quit pygame in shutdown() and release DRM master
            if self.pending:
                msg = self.pending.popleft()
            else:
                r, _, _ = select.select([self.sock], [], [], 0.05)
                if not r:
                    continue   # nothing to do: front buffer keeps the current frame presented
                try:
                    data = self.sock.recv(65536)
                except OSError:
                    break      # socketpair gone = displayd gone; exit and let it respawn us
                try:
                    msg = json.loads(data.decode())
                except Exception:
                    self.log("bad datagram dropped")
                    continue
            try:
                self.dispatch(msg)
            except Exception as e:
                # An op must not take the renderer down — displayd sees the failure in the
                # log stream and in missing flips, and owns the restart decision.
                self.log(f"op {msg.get('op')!r} failed: {type(e).__name__}: {e}")

    def dispatch(self, msg: dict):
        op = msg.get("op")
        if op == "mode":
            self.apply_mode(msg)
        elif op == "load_stims":
            self.op_load_stims(msg.get("path", ""))
        elif op == "show":
            self.op_show(msg)
        elif op == "render":
            self.op_render(msg)
        elif op == "heartbeat_flash":
            self.op_heartbeat_flash()
        elif op == "quit":
            self._quit = True
        else:
            self.log(f"unknown op {op!r} ignored")

    # ── mode / display config ──

    def apply_mode(self, msg: dict):
        """The "mode" op: the lease, plus (newer displayd) the composed display config in
        msg["config"]. No redraw — the current frame stays presented."""
        self.lease = str(msg.get("lease", "idle"))
        cfg = msg.get("config")
        if isinstance(cfg, dict) and cfg:
            self.apply_display_config(cfg)
        self.log(f"lease -> {self.lease}")

    def apply_display_config(self, cfg: dict):
        """Remember displayd's composed display config, and apply live what can be applied.

        This exists because the renderer used to hand Display.init() an EMPTY rig config:
        resolution/refresh_hz AND — critically — the photodiode sync-square layout
        (sync_corner / sync_size_px / sync_brightness, which engine/follower.py:67-73 merges
        into the display config from the photodiode card) all fell back to code defaults. A
        session then drew its sync square in the WRONG CORNER and every trial's photodiode
        sync failed silently. Config that arrives before start() is used for dev.init();
        config that arrives after can only change the sync layout (the SDL mode, and the
        self-test that validated it, are already fixed), so a resolution/refresh change is
        reported as needing a renderer restart rather than pretended to be applied.
        An older displayd sends no "config" key at all — the defaults still work."""
        self._display_config = dict(cfg)
        if self.dev is None:
            return   # start() has not run yet; it will pass this to Display.init()
        sync_keys = ("sync_corner", "sync_size_px", "sync_brightness")
        if any(k in cfg for k in sync_keys) and hasattr(self.dev, "set_sync_layout"):
            try:
                # None leaves a field unchanged (Display.set_sync_layout), and it drops the
                # cached rect so the next frame uses the new placement.
                self.dev.set_sync_layout(cfg.get("sync_corner"), cfg.get("sync_size_px"),
                                         cfg.get("sync_brightness"))
                self.log("sync layout -> corner=%s size_px=%s brightness=%s"
                         % (self.dev.sync_corner, self.dev.sync_size_px or "auto",
                            self.dev.sync_brightness))
            except Exception as e:
                self.log(f"sync layout update failed: {type(e).__name__}: {e}")
        if "background_gray" in cfg and self.stims is None:
            # Only before a session: once load_stims has run, the NPZ's background_gray is
            # the authority for dev.bg_gray (blank()/use_session_bg paint it) and a config
            # replay must not stomp it mid-session.
            try:
                self.dev.bg_gray = float(cfg["background_gray"])
            except (TypeError, ValueError):
                pass
        try:
            res = cfg.get("resolution")
            if res is not None and tuple(res) != tuple(self.dev.resolution):
                self.log(f"config resolution {tuple(res)} != active "
                         f"{tuple(self.dev.resolution)} — needs a renderer restart")
            hz = cfg.get("refresh_hz")
            if hz is not None and abs(float(hz) - float(self.dev.refresh_hz)) > 0.01:
                self.log(f"config refresh_hz {hz} != active {self.dev.refresh_hz} — "
                         "needs a renderer restart")
        except Exception:
            pass   # a malformed value here is a log-only concern, never a render failure

    # ── session ops (port of follower.py's NPZ path) ──

    def op_load_stims(self, path: str):
        """follower.load_stims: NPZ -> dict, n_trials / sync_square_every_n / background_gray
        pulled the same way, then the same synchronous prewarm so no trial pays a cold
        surface build on the onset path."""
        try:
            self.stims = dict(np.load(path, allow_pickle=True))
        except Exception as e:
            self.stims = None
            self.log(f"load_stims failed: {path}: {e}")
            return
        n = int(self.stims.get("n_trials", [0])[0])
        self.sync_every_n = int(self.stims.get("sync_square_every_n", [0])[0])
        if "background_gray" in self.stims:
            self.dev.bg_gray = float(self.stims["background_gray"][0])
        line = f"loaded {n} trials from {path}"
        if self.sync_every_n > 0:
            line += f"; sync square every {self.sync_every_n} frames"
        self.log(line)
        self._prewarm_stims()

    def _prewarm_stims(self):
        """follower._prewarm_stims: build every unique spherical stimulus surface up front
        (RAM-budgeted in display.prewarm). No-op without a warp or visual-angle fields."""
        if self.stims is None or getattr(self.dev, "_warp", None) is None:
            return
        if not ("stim_size_deg" in self.stims and "stim_az_deg" in self.stims):
            return
        n = int(self.stims.get("n_trials", [0])[0])
        if n <= 0:
            return
        bg = float(self.stims["background_gray"][0])
        shape = str(self.stims["shape"][0]) if "shape" in self.stims else "square"
        seen, combos = set(), []
        for t in range(n):
            az = float(self.stims["stim_az_deg"][t])
            alt = float(self.stims["stim_alt_deg"][t])
            size = float(self.stims["stim_size_deg"][t])
            frac = float(self.stims["corr_contrast"][t])
            k = (round(az, 2), round(alt, 2), round(size, 2), round(frac, 4))
            if k not in seen:
                seen.add(k)
                combos.append((az, alt, size, frac))
        if not combos:
            return
        t0 = time.time()
        built, skipped = self.dev.prewarm(combos, bg, shape)
        line = f"pre-warmed {built} stimulus surface(s) in {time.time() - t0:.1f}s"
        if skipped:
            line += f"; {skipped} left lazy (RAM budget)"
        self.log(line)

    def op_show(self, msg: dict):
        """follower._handle_show with the leader ACK replaced by {"ev":"onset"} up the
        socketpair (displayd forwards it to the leader's ack_port). Blocks this thread for
        the trial duration, exactly like follower — command latency during a stimulus is
        acceptable; correctness of the frame loop is not negotiable."""
        seq = msg.get("seq")
        trial = int(msg.get("trial", -1))
        if self.stims is None:
            self.log(f"show seq={seq}: no stims loaded")
            return
        n = int(self.stims.get("n_trials", [0])[0])
        if trial < 0 or trial >= n:
            self.log(f"show seq={seq}: trial {trial} out of range (0..{n - 1})")
            return

        dev = self.dev
        corr_contrast = float(self.stims["corr_contrast"][trial])
        bg_gray = float(self.stims["background_gray"][0])
        shape = str(self.stims["shape"][0]) if "shape" in self.stims else "square"
        duration = float(self.stims["duration_s"][trial]) if "duration_s" in self.stims else 2.0

        # Prefer true spherical rendering (warp loaded + visual-angle fields); else flat rect.
        spherical = (getattr(dev, "_warp", None) is not None
                     and "stim_size_deg" in self.stims
                     and "stim_az_deg" in self.stims)
        if spherical:
            az = float(self.stims["stim_az_deg"][trial])
            alt = float(self.stims["stim_alt_deg"][trial])
            size_deg = float(self.stims["stim_size_deg"][trial])

            def draw(sync):
                dev.show_patch_spherical(az, alt, size_deg, corr_contrast,
                                         bg_gray, sync_square=sync, shape=shape)

            # Warm THIS trial's surface before any timing starts: _prewarm_stims builds
            # every combo at load_stims, but it is RAM-budgeted and may have left this one
            # lazy — a cold build would then land on the onset frame and delay the stimulus.
            # Cache-only (the same helper prewarm uses), so no drawing logic is duplicated;
            # the key must match show_patch_spherical's default apply_lum=True.
            if hasattr(dev, "_get_patch_surface"):
                try:
                    dev._get_patch_surface(az, alt, size_deg, corr_contrast, bg_gray,
                                           shape, True)
                except Exception as e:
                    self.log(f"surface warm failed for trial {trial}: "
                             f"{type(e).__name__}: {e}")
        else:
            px_x = float(self.stims["px_x"][trial])
            px_y = float(self.stims["px_y"][trial])
            px_size = float(self.stims["px_size"][trial])

            def draw(sync):
                dev.show_rect(px_x, px_y, px_size, corr_contrast,
                              bg_gray, sync_square=sync, shape=shape)

        if self.sync_every_n > 0:
            self._show_synced(draw, duration, seq, trial)
        else:
            # Single-flip path: one render, hold for duration, blank.
            t0 = time.perf_counter()
            draw(False)
            ms = (time.perf_counter() - t0) * 1000.0
            onset_t = time.time()
            self.send({"ev": "onset", "seq": seq, "trial": trial,
                       "t": onset_t, "flip_ms": round(ms, 2)})   # never throttled
            self._mark_flip_reported(onset_t)
            time.sleep(duration)
            dev.blank()

    def _show_synced(self, draw, duration, seq, trial):
        """follower._show_synced: per-frame render loop with the photodiode sync square ON
        every Nth frame starting at frame 0. The vsync-locked flip paces the loop (kms path
        always grants it, but keep the Clock fallback so a degraded display can't busy-spin);
        the wall-clock bound keeps the stimulus up for `duration` regardless of pacing.

        Per-frame cost note: draw() re-derives the patch cache key (a 7-tuple of rounds +
        one dict hit) and the sync rect on every frame although only the sync flag changes.
        Both are already cheap — the surface itself is cached (prewarm / _get_patch_surface,
        warmed for this trial in op_show) and Display._sync_patch_rect memoizes into
        _sync_rect — and the only way to hoist them would be for the renderer to keep its
        own copy of show_patch_spherical's blit + _draw_sync_border + flip, i.e. to fork the
        drawing logic out of devices/display.py. Not worth the divergence risk for a few
        microseconds inside a 17.4 ms frame, so it is deliberately left as-is."""
        import pygame
        clock = None if getattr(self.dev, "_vsync", False) else pygame.time.Clock()
        refresh_hz = int(getattr(self.dev, "refresh_hz", 60) or 60)
        onset_t = None
        fi = 0
        while True:
            patch_on = (fi % self.sync_every_n == 0)
            t0 = time.perf_counter()
            draw(patch_on)
            ms = (time.perf_counter() - t0) * 1000.0
            if fi == 0:
                # Onset is timing-critical: sent on the frame itself, never throttled.
                onset_t = time.time()
                self.send({"ev": "onset", "seq": seq, "trial": trial,
                           "t": onset_t, "flip_ms": round(ms, 2)})
                self._mark_flip_reported(onset_t)
            else:
                self.report_flip(ms)
            fi += 1
            if time.time() - onset_t >= duration:
                break
            if clock is not None:
                clock.tick(refresh_hz)   # no vsync: pace to the refresh instead of spinning
        self.dev.blank()

    # ── render op (the old display_worker action set) ──

    def op_render(self, msg: dict):
        """Setup-time actions, params identical to engine/display_worker.py. Every action
        answers with an {"ev":"result","op":<action>,...} so displayd's POST /render has a
        result to return; sync_test acks BEFORE its blocking loop (stop_sync ends it)."""
        dev = self.dev
        action = msg.get("action")

        def result(obj):
            # Echo the request id so displayd correlates by id, not by action name
            # (two same-action ops in flight would otherwise swap answers).
            obj.update({"ev": "result", "op": action, "id": msg.get("id")})
            self.send(obj)

        try:
            if action == "blank":
                t0 = time.perf_counter()
                if msg.get("use_session_bg"):
                    # Session-originated BG/BLANK: paint the SESSION background (the NPZ's
                    # background_gray, held in dev.bg_gray — what engine/follower.py's
                    # display.blank() always did). The setup-time "blank" action defaults
                    # gray_value to 0.0 = black, so honouring it here would show the animal
                    # a black pre-session field that jumps to gray mid-session.
                    dev.blank()
                else:
                    dev.blank_with_gray(msg.get("gray_value", 0.0))
                self.report_flip((time.perf_counter() - t0) * 1000.0, force=True)
                result({"ok": True})
            elif action == "checkers":
                t0 = time.perf_counter()
                dev.show_checkers(use_warp=msg.get("apply_warp", True))
                self.report_flip((time.perf_counter() - t0) * 1000.0, force=True)
                result({"ok": True})
            elif action == "stimulus":
                t0 = time.perf_counter()
                dev.show_patch_spherical(
                    msg.get("az_deg", 0.0), msg.get("alt_deg", 0.0),
                    msg.get("size_deg", 8.0), msg.get("corr_contrast", 0.5),
                    msg.get("bg_gray", 0.0), shape=msg.get("shape", "square"),
                    apply_lum=msg.get("apply_lum", True))
                self.report_flip((time.perf_counter() - t0) * 1000.0, force=True)
                result({"ok": True})
            elif action == "reload_warp":
                # load_warp caches in memory and never re-reads on its own — this makes a
                # freshly generated warp take effect without a renderer restart.
                loaded = dev.load_warp(str(WARP_PATH))
                result({"ok": True, "reloaded": bool(loaded),
                        "message": "warp reloaded" if loaded else "warp_map.npz not found"})
            elif action == "sync_test":
                # Ack BEFORE the blocking flash loop (the HTTP caller must return now);
                # _SockStop polls the socketpair each frame so stop_sync/quit can end it.
                if hasattr(dev, "set_sync_layout"):
                    dev.set_sync_layout(msg.get("sync_corner"), msg.get("sync_size_px"),
                                        msg.get("sync_brightness"))
                result({"ok": True, "running": True, "message": "sync test running"})
                stop = _SockStop(self)
                dev.run_sync_test(int(msg.get("every_n", 5)), stop)
                if stop.quit:
                    self._quit = True
                # result already sent; no second reply (would desync /render)
            elif action == "stop_sync":
                # Outside a running sync test this is a no-op: the in-test stop is consumed
                # by _SockStop before the main loop ever sees it.
                result({"ok": True})
            elif action == "sync_burst":
                # Run to completion, then reply with the count — the caller needs the
                # number (photodiode ±1 check), plus per-flash flip times for correlation.
                if hasattr(dev, "set_sync_layout"):
                    dev.set_sync_layout(msg.get("sync_corner"), msg.get("sync_size_px"),
                                        msg.get("sync_brightness"))
                flash_times = []
                flashes = dev.run_sync_burst(int(msg.get("every_n", 5)),
                                             float(msg.get("duration_s", 1.0)),
                                             flash_times=flash_times)
                result({"ok": True, "flashes": int(flashes),
                        "flash_times": [round(t, 4) for t in flash_times]})
            else:
                result({"ok": False, "error": f"unknown render action: {action}"})
        except Exception as e:
            result({"ok": False, "error": str(e)})

    # ── heartbeat flash ──

    def op_heartbeat_flash(self):
        """One frame with the red photodiode sync square over the session background, then
        restore the background. The backbuffer's contents after a swap are undefined, so the
        flash frame is composed from scratch: background repaint (no flip), sync square on
        top, one flip — t_cmd is the wall clock right after that flip returns (frame
        on-glass, vsync-paced), which the phase-3 correlator matches to a photodiode pulse."""
        import pygame
        dev = self.dev
        self._paint_background()
        dev._draw_sync_border(True)
        pygame.display.flip()
        t_cmd = time.time()
        self.send({"ev": "flash", "t_cmd": t_cmd})
        # Restore: full background repaint + flip (Display.blank).
        t0 = time.perf_counter()
        dev.blank()
        self.report_flip((time.perf_counter() - t0) * 1000.0, force=True)

    def _paint_background(self):
        """Compose the session background into the backbuffer WITHOUT flipping — Display's
        blank always flips, but the heartbeat flash needs square-over-background in a single
        frame. Mirrors blank_with_gray's two paths (warp-corrected cached field / plain
        fill), sharing its surface cache."""
        dev = self.dev
        lin = max(0.0, min(1.0, dev.bg_gray))
        if dev._warp is not None and dev._corr_map is not None:
            surf = dev._blank_cache.get(round(lin, 4))
            if surf is None:
                surf = dev._build_field_surface(lin)
                dev._blank_cache[round(lin, 4)] = surf
            dev._screen.blit(surf, (0, 0))
        else:
            rgb = max(0, min(255, int(lin * 255)))
            dev._screen.fill((0, rgb, rgb))

    # ── shutdown ──

    def shutdown(self):
        try:
            if self.dev is not None:
                self.dev.shutdown()
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def main():
    # Parent-death detection, half 1: the kernel SIGTERMs us when displayd dies. Best
    # effort — half 2 (Renderer.check_parent's getppid() watch) covers the cases where
    # this is unavailable or was cleared across an exec.
    if not _install_parent_death_signal():
        print("[renderer] PR_SET_PDEATHSIG unavailable — falling back to the getppid() "
              "watch", file=sys.stderr, flush=True)
    if os.getppid() == 1:
        # displayd died between fork and now: we would never see a ppid CHANGE, and an
        # orphan renderer holds DRM master forever. Refuse to start.
        print("[renderer] orphaned at startup (ppid 1) — displayd is gone; exiting",
              file=sys.stderr, flush=True)
        sys.exit(3)

    # fd 3 is the socketpair end displayd handed us; anything else here is a spawn bug.
    try:
        sock = socket.socket(fileno=3)
    except OSError as e:
        print(f"[renderer] fd 3 is not a socket ({e}) — must be spawned by displayd",
              file=sys.stderr, flush=True)
        sys.exit(3)
    sock.setblocking(False)   # all reads go through select; sends on a socketpair don't block

    r = Renderer(sock)
    r.start()                 # exits(3) with a fatal report on any startup-contract failure
    try:
        r.run()
    finally:
        r.shutdown()
        print("[renderer] exit", flush=True)


if __name__ == "__main__":
    main()
