"""
devices/photodiode.py

Photodiode TTL sync input for stimulus frame timing.

Reads a CLEAN, pre-filtered 3.3V sync pulse from a Teensy (teensy/<rig>/photodiode_sync_v2_3):
the Teensy watches the raw photodiode and applies the steady/hold-off filtering, then
emits one square pulse per sync frame. So this device just timestamps the rising edges —
no on-Pi glitch/hold-off filtering. (The old self-filtering version is stashed at
devices/photodiode_filtered.py in case the Teensy path is abandoned.)

GPIO input via lgpio hardware alerts (nanosecond ticks) — works on Pi 5 (RP1) and Pi 4 (BCM), no daemon.

The Teensy's USB serial carries the amplitude telemetry (P / B lines), what firmware runs (I line)
and, from a DEBUG build, the analog trace the Setup UI's Debug window draws next to this Pi's GPIO
edges (debug_start / debug_read / teensy_set).
"""

from __future__ import annotations
import itertools
import threading
import time
from collections import deque

from .base import Device, DeviceInfo, IOType, register_device

SERIAL_RETRY_S = 1.0        # reopen attempts while the Teensy port is away (reflash, replug)
DEBUG_SAMPLES = 40000       # debug ring: ~9 s of a DEBUG build's "D" lines (~4.4 kHz)
DEBUG_READ_MAX = 12000      # samples per debug_read()
DEBUG_IDLE_S = 10.0         # nobody read the debug ring this long -> stop filling it
OFFSET_BUCKET_S = 4.0       # see _dbg_store: the serial estimate of the Teensy clock offset
MATCH_S = 0.002             # an "E" line and a GPIO edge closer than this are one pulse
EDGE_SETTLE_S = 0.06        # debug_read hands out a GPIO edge once its "E" line had time to arrive
LINE_TAGS = (b"P", b"B", b"I", b"D", b"E", b"X")     # first byte of every v2_2 / v2_3 line

# The tunables firmware v2_3 reports in its "I" line and a DEBUG build takes over serial
# ("S <NAME> <value>"): type, min, max. The same names are the sketch's constants, which the
# Setup UI reads and writes (controller/setup.py).
TEENSY_PARAMS = {
    "MIN_PULSE_V": (float, 0.0, 3.3),
    "START_FRAC": (float, 0.001, 1.0),
    "END_FRAC": (float, 0.001, 1.0),
    "STEADY_US": (int, 0, 20000),
    "HOLDOFF_US": (int, 0, 2000000),
}


def check_teensy_params(params, current=None):
    """(clean, errors) for tunables on their way to the Teensy — over serial (teensy_set) or into
    the sketch before an upload (controller/setup.py): known names, numbers, inside the ranges
    the firmware itself enforces, and END_FRAC below START_FRAC (which the firmware does not
    check) against `current`, the values the ones not given keep."""
    clean, errors = {}, []
    for name, val in (params or {}).items():
        spec = TEENSY_PARAMS.get(name)
        if spec is None:
            errors.append(f"{name}: not a tunable")
            continue
        try:
            val = spec[0](float(val))
        except (TypeError, ValueError, OverflowError):
            errors.append(f"{name}: not a number")
            continue
        if not spec[1] <= val <= spec[2]:          # also false for NaN
            errors.append(f"{name} = {val}: outside {spec[1]} .. {spec[2]}")
            continue
        clean[name] = val
    merged = {**(current or {}), **clean}
    if (not errors and "END_FRAC" in merged and "START_FRAC" in merged
            and merged["END_FRAC"] >= merged["START_FRAC"]):
        errors.append("END_FRAC must be below START_FRAC")
    return clean, errors


def _snapshot(dq) -> list:
    """list(deque) of a deque another thread appends to without a lock (it can raise while the
    deque grows)."""
    for _ in range(5):
        try:
            return list(dq)
        except RuntimeError:
            continue
    return []

@register_device
class Photodiode(Device):
    info = DeviceInfo("photodiode", "Photodiode (Teensy sync)", IOType.GPIO_IN, ["lgpio"])

    @classmethod
    def task_params_schema(cls):
        return {
            "pulse_every_n_frames": {
                "type": "int", "default": 5,
                "label": "Sync pulse every N frames", "min": 1, "max": 60,
            },
        }

    def init(self, rig_config: dict, task_params: dict):
        import lgpio
        self._lgpio = lgpio
        self.gpio = rig_config.get("gpio", 24)
        self.pulse_every_n = task_params.get("pulse_every_n_frames",
                             rig_config.get("pulse_every_n_frames", 5))
        # lgpio handle to the header gpiochip. Pi 5 (current OS) & Pi 4 expose the 40-pin header as
        # gpiochip0; very early Pi 5 images used gpiochip4 — set rig 'gpiochip' to override.
        self._chipnum = int(rig_config.get("gpiochip", 0))
        try:
            self._chip = lgpio.gpiochip_open(self._chipnum)
        except Exception as e:
            raise RuntimeError(f"lgpio: cannot open gpiochip{self._chipnum} ({e})")
        # Claim the pin for alerts on BOTH edges with a pull-down; the per-mode callbacks below filter
        # to the edge they want. Filtering lives on the Teensy now — clear the debounce (0) so the
        # clean Teensy edges pass through untouched.
        lgpio.gpio_claim_alert(self._chip, self.gpio, lgpio.BOTH_EDGES, lgpio.SET_PULL_DOWN)
        lgpio.gpio_set_debounce_micros(self._chip, self.gpio, 0)
        self._cb = None
        self._callback = None
        self._active = False
        self._pulse_idx = 0
        self._ref_tick = None
        self._ref_time = None
        self._trial_pulses = []

        # ── Teensy USB serial: photodiode amplitude telemetry ───────────────────────────────────
        # Three firmware protocols are understood (auto-detected per line):
        #   v2_3 (current): v2_2's lines plus "I ..." (version, DEBUG build or not, the live
        #        tunables — the answer to "?"), "X ..." (a refused command) and, in a DEBUG build,
        #        "D <t_us> <V> <start_V> <end_V>" at ~4.4 kHz and "E <t_us>" per accepted pulse.
        #        A DEBUG build takes "S <NAME> <value>" (teensy_set). See the sketch's header.
        #   v2_2: "P <median_V> <peak_V> <baseline_V> <width_us>" per detected pulse +
        #        "B <floor_V> <ceil_V>" once per second. Per-pulse stats are computed on the Teensy
        #        at full ADC rate — the fix for the v2_1 aliasing (a 100 Hz stream sampled at random
        #        phase mostly missed the ~1.2 ms optical pulses; bench 2026-08-22: p99 varied
        #        0.12-2.7 V across identical runs).
        #   v2_1 (legacy): a bare V float per line at ~100 Hz (also the 1st field of the DEBUG
        #        builds before v2_3).
        # The rolling window keeps P medians + B floors (v2_2+) or raw samples (v2_1), so the
        # percentile machinery below works for both: top 1% ~ pulse amplitude, bottom 1% ~ floor.
        # No serial_port configured (or no pyserial) -> V features silently disabled; pulse timing
        # over GPIO is unaffected.
        self.serial_port = rig_config.get("serial_port")            # e.g. /dev/ttyACM0; None = off
        self.v_high = float(rig_config.get("v_high", 3.0))          # saturated: top 1% >= this
        self.v_low = float(rig_config.get("v_low", 0.3))           # too weak:  bottom 1% <= this
        self.v_window_s = float(rig_config.get("v_window_s", 10.0))
        self.v_realert_s = float(rig_config.get("v_realert_s", 30.0))
        self.verify_duration_s = float(rig_config.get("verify_duration_s", 1.0))
        self.verify_enabled = bool(rig_config.get("verify_enabled", True))
        # AMPLITUDE series: v2_2 P medians (per pulse) or v2_1 raw samples. Kept separate from
        # the baseline series — mixing them made the percentiles depend on the event-rate ratio
        # instead of the signal. maxlen is a bound, not the window: percentile consumers pass
        # a time window (v2_1's 100 Hz vs v2_2's per-pulse rate span very different history).
        self._v = deque(maxlen=max(100, int(self.v_window_s * 100)))   # (t, V)
        self._b = deque(maxlen=60)     # BASELINE series: v2_2 "B" lines, (t, floor_V, ceil_V)
        self._v_lock = threading.Lock()
        # v2_2 per-pulse telemetry (latest values; None until the first tagged line arrives).
        # Plain reference assignments (atomic in CPython) — read via get_v_status.
        self._last_pulse = None        # {"t", "median_V", "peak_V", "baseline_V", "width_us"}
        self._last_baseline = None     # {"t", "floor_V", "ceil_V"}
        # Edge-triggered warning state per channel ("range", "clip", "hum"): warn on the
        # rising edge, re-alert every v_realert_s while sustained, recovery notice on clear.
        self._warn_state = {}
        self._serial = None
        self._serial_seen = False      # the port was open at least once
        self._serial_err = None        # why it is not open now
        self._serial_thread = None
        self._serial_stop = threading.Event()
        self._wlock = threading.Lock()                 # serial writes ("?" and "S ..." commands)
        # Firmware v2_3: its "I" line (version, DEBUG build or not, the live tunables), refused
        # commands ("X ..."), and when each kind of line last arrived (the guess for older firmware).
        self._fw = None                # {"version", "debug", "params", "t"}
        self._fw_seq = 0               # counts I lines: the "S" / "?" senders wait on it
        self._fw_asks = 0
        self._x_lines = deque(maxlen=20)               # (t, text)
        self._line_seen = {}
        # Debug window: ring of the DEBUG build's samples (Teensy seconds, V, start_V, end_V), the
        # Teensy's accepted-pulse marks and this Pi's GPIO edges. See debug_start / debug_read.
        self._dbg_on = False
        self._dbg_cb = None
        self._dbg_ref = (0, 0.0)
        self._dbg_last_read = 0.0
        self._dbg_lock = threading.Lock()
        self._dbg = deque(maxlen=DEBUG_SAMPLES)
        self._dbg_acc = deque(maxlen=4000)             # Teensy time of each "E" line
        self._dbg_edges = deque(maxlen=4000)           # (count, Pi wall time) per GPIO rising edge
        self._dbg_edge_n = 0
        self._off_fine = deque(maxlen=20)              # (when matched, GPIO time - Teensy time)
        # Starts at the clock, not at 0: a reader's cursors must never fit the ring of the next
        # device (Initialize / Reinit / a pi_api restart re-create it).
        self._dbg_epoch = int(time.time() * 1000)
        self._dbg_clear_locked()
        if self.serial_port:
            self._open_serial()

    def _open_serial(self):
        """Start reading the Teensy serial port. Never raises: without pyserial the V features
        are off; a port that cannot be opened now is retried every SERIAL_RETRY_S by the reader
        thread, which also reopens it after it drops (a firmware upload reboots the Teensy)."""
        try:
            import serial
        except Exception as e:
            print(f"[photodiode] pyserial unavailable ({e}); V monitoring disabled", flush=True)
            return
        self._pyserial = serial
        self._connect_serial()
        self._serial_stop.clear()
        self._serial_thread = threading.Thread(target=self._serial_reader, daemon=True)
        self._serial_thread.start()

    def _connect_serial(self) -> bool:
        try:
            ser = self._pyserial.Serial(self.serial_port, 115200, timeout=0.25, write_timeout=0.5)
        except Exception as e:
            if str(e) != self._serial_err:            # say it once, not at every retry
                print(f"[photodiode] cannot open {self.serial_port} ({e}); retrying every "
                      f"{SERIAL_RETRY_S:g} s", flush=True)
            self._serial_err = str(e)
            return False
        again = self._serial_seen
        self._fw = None                               # whatever runs there now tells us itself
        self._line_seen = {}
        self._dbg_clear()
        self._serial = ser
        self._serial_seen = True
        self._serial_err = None
        self._fw_asks = 1
        self._serial_write(b"?\n")                    # firmware v2_3 answers with its "I" line
        if again:
            print(f"[photodiode] serial port {self.serial_port} open again", flush=True)
        return True

    def _serial_write(self, data: bytes) -> bool:
        ser = self._serial
        if ser is None:
            return False
        try:
            with self._wlock:
                ser.write(data)
            return True
        except Exception as e:
            print(f"[photodiode] serial write failed ({str(e)[:80]})", flush=True)
            return False

    def _warn_edge(self, key, active, message):
        """Edge-triggered warning channel: warn on the rising edge, re-alert every
        v_realert_s while `active` stays true, emit a recovery notice on clear."""
        now = time.time()
        st = self._warn_state.get(key)
        if active:
            if st is None:
                self._warn_state[key] = now
                self._emit_warning(message, "warning")
            elif (now - st) >= self.v_realert_s:
                self._warn_state[key] = now
                self._emit_warning(message, "warning")
        elif st is not None:
            del self._warn_state[key]
            self._emit_warning(f"Photodiode {key} cleared", "ok")

    def _serial_reader(self):
        """Daemon, and the ONLY reader of the port (a second reader steals bytes and garbles
        both): takes whatever arrived, hands the complete lines to _parse_lines, runs the
        periodic checks, and reopens the port when it drops."""
        buf = b""
        fresh = True                # the first line after opening is a fragment, unless tagged
        last_check = 0.0
        while not self._serial_stop.is_set():
            ser = self._serial
            if ser is None:
                if self._serial_stop.wait(SERIAL_RETRY_S):
                    break
                self._connect_serial()
                buf, fresh = b"", True
                continue
            try:
                data = ser.read(ser.in_waiting or 1)
            except Exception as e:
                if self._serial_stop.is_set():
                    break
                # Close at once: a dead handle keeps the tty name busy, and the Teensy then
                # comes back from a reflash as the next ttyACM number.
                self._serial = None
                self._fw = None                       # a reflash is the usual reason: ask again
                self._line_seen = {}
                self._serial_err = f"port dropped ({str(e)[:80]})"
                try:
                    ser.close()
                except Exception:
                    pass
                print(f"[photodiode] serial {self._serial_err}; reopening", flush=True)
                continue
            now = time.time()
            streaming = False
            if data:
                buf += data
                if b"\n" in data:
                    lines = buf.split(b"\n")
                    buf = lines.pop()
                    if fresh:
                        fresh = False
                        if lines[0][:1] not in LINE_TAGS:
                            lines = lines[1:]
                    streaming = self._parse_lines(lines, now)
                elif len(buf) > 4096:
                    buf = b""                         # no line end in sight: not our protocol
            if now - last_check >= 2.0:
                last_check = now
                self._periodic(now)
            if streaming:
                # A DEBUG build sends ~4.4k lines/s. Let a couple of ms pile up, so the port is
                # read a few hundred times a second and not once per line.
                time.sleep(0.002)

    def _periodic(self, now):
        try:
            self._check_v_range()
        except Exception:
            pass
        if self._fw is None and self._fw_asks < 3 and self._serial is not None:
            self._fw_asks += 1                        # v2_3 answers; older firmware never will
            self._serial_write(b"?\n")
        if self._dbg_on and now - self._dbg_last_read > DEBUG_IDLE_S:
            self.debug_stop()                         # the Debug window went away without saying so

    def _parse_lines(self, lines, now) -> bool:
        """One chunk of complete lines into the amplitude/baseline series, the firmware info and
        the debug ring; per-pulse clipping/hum classification. Dispatch is on the raw bytes (no
        per-line decode — a DEBUG build streams ~4.4k lines/s). True when the chunk held debug
        samples."""
        dbg = self._dbg_on
        seen = self._line_seen
        samples, accepts = [], []
        n_d = 0
        for line in lines:
            if line[:1] == b"D":
                # v2_3 DEBUG build: D <t_us> <V> <start_V> <end_V>
                n_d += 1
                if dbg:
                    p = line.split()
                    try:
                        samples.append((int(p[1]), float(p[2]), float(p[3]), float(p[4])))
                    except (ValueError, IndexError):
                        pass
                continue
            parts = line.split()
            if not parts:
                continue
            tag = parts[0]
            try:
                if tag == b"P" and len(parts) >= 5:
                    # v2_2 per-pulse report: median peak baseline width_us.
                    median_v, peak_v = float(parts[1]), float(parts[2])
                    base_v, width_us = float(parts[3]), int(float(parts[4]))
                    self._last_pulse = {"t": now, "median_V": median_v, "peak_V": peak_v,
                                        "baseline_V": base_v, "width_us": width_us}
                    with self._v_lock:
                        self._v.append((now, median_v))
                    seen["P"] = now
                    # Clipping: the Teensy pin saturates at 3.3 V; peaks at/above v_high mean
                    # the divider needs adjusting before the signal clips.
                    self._warn_edge("clipping", peak_v >= self.v_high,
                                    f"Photodiode pulse clipping (peak {peak_v:.2f} V >= "
                                    f"{self.v_high})")
                    # Width discriminates optics from electricity: a real optical sub-pulse is
                    # ~1.2 ms; mains-hum crossings are ~10 ms.
                    self._warn_edge("hum", width_us >= 5000,
                                    f"Photodiode pulses look like mains hum (width {width_us} "
                                    f"us, expect ~1200) — check amp/ground")
                elif tag == b"B" and len(parts) >= 3:
                    # v2_2 idle line, 1 Hz: baseline-follower floor + peak-follower ceiling.
                    floor_v, ceil_v = float(parts[1]), float(parts[2])
                    self._last_baseline = {"t": now, "floor_V": floor_v, "ceil_V": ceil_v}
                    with self._v_lock:
                        self._b.append((now, floor_v, ceil_v))
                    seen["B"] = now
                elif tag == b"E" and len(parts) >= 2:
                    # v2_3 DEBUG build: E <t_us>, the moment the Teensy raised its output.
                    if dbg:
                        accepts.append(int(parts[1]))
                elif tag == b"I" and len(parts) >= 3:
                    self._on_info(parts, now)
                elif tag == b"X":
                    text = line.decode("ascii", "replace").strip()[2:]
                    self._x_lines.append((now, text))
                    print(f"[photodiode] the Teensy refused a command: {text}", flush=True)
                else:
                    # Bare float: the legacy v2_1 100 Hz stream and the DEBUG builds before
                    # v2_3 (4-trace stream, 1st field = V). Do NOT remove — a Teensy that was
                    # not reflashed still speaks this.
                    v = float(tag)
                    if not 0.0 <= v <= 3.5:           # not a voltage: a torn line
                        continue
                    with self._v_lock:
                        self._v.append((now, v))
                    seen["V"] = now
            except (ValueError, IndexError):
                continue
        if n_d:
            seen["D"] = now
        if samples or accepts:
            self._dbg_store(samples, accepts, now)
        return n_d > 0

    def _on_info(self, parts, now):
        """I <version> DEBUG=<0|1> <NAME>=<value> ... — firmware v2_3's answer to "?" and to
        every "S". A malformed line raises ValueError, which the caller skips."""
        kv = dict(p.decode("ascii").split("=", 1) for p in parts[2:])
        debug = kv.pop("DEBUG", "0") == "1"
        params = {k: (int(float(v)) if TEENSY_PARAMS.get(k, (float,))[0] is int else float(v))
                  for k, v in kv.items()}
        self._fw = {"version": parts[1].decode("ascii"), "debug": debug, "params": params, "t": now}
        self._fw_seq += 1

    def _v_percentiles(self, t_start=None, t_end=None, min_n=10):
        """(p1, p99) over the AMPLITUDE series, optionally within [t_start, t_end]; None if
        fewer than min_n samples. Under v2_2 the series holds per-pulse medians (a 1 s burst
        yields only ~12), so windowed callers pass a smaller min_n."""
        import numpy as np
        with self._v_lock:
            vals = [v for (t, v) in self._v
                    if (t_start is None or t >= t_start) and (t_end is None or t <= t_end)]
        if len(vals) < min_n:
            return None
        return float(np.percentile(vals, 1)), float(np.percentile(vals, 99))

    def _check_v_range(self):
        """Range monitor over the last v_window_s (time-windowed — the deque's maxlen is only
        a bound; sample RATE differs 100x between protocols).
        v2_2 (B lines seen recently): saturation check only, from pulse medians. The v2_1
        "too weak" check is meaningless here — the diode sits in the black dead band, so the
        floor is always near zero, and ABSENCE of pulses (the real weakness signal) is the
        correlation layer's job, not a percentile's.
        v2_1/DEBUG bare-float stream: original two-sided behavior."""
        now = time.time()
        pr = self._v_percentiles(t_start=now - self.v_window_s)
        v22 = self._last_baseline is not None and (now - self._last_baseline["t"]) < 3.0
        if v22:
            if pr is not None:
                p1, p99 = pr
                self._warn_edge("range", p99 >= self.v_high,
                                f"Photodiode V saturating (top 1% = {p99:.2f} V >= {self.v_high})")
            return
        if pr is None:
            return
        p1, p99 = pr
        if p99 >= self.v_high:
            msg = f"Photodiode V saturating (top 1% = {p99:.2f} V >= {self.v_high})"
        elif p1 <= self.v_low:
            msg = f"Photodiode V too weak (bottom 1% = {p1:.2f} V <= {self.v_low})"
        else:
            msg = ""
        self._warn_edge("range", bool(msg), msg or "in range")

    def _emit_warning(self, message, level):
        """Fire a UI-log event through the stream callback (Leader._publish -> SSE). If not streaming
        (e.g. setup), just print — pi_api captures it."""
        evt = {"event": "photodiode_warning", "level": level, "message": message, "t": time.time()}
        if self._callback:
            try:
                self._callback(evt)
                return
            except Exception:
                pass
        print(f"[photodiode] {level}: {message}", flush=True)

    def get_v_status(self) -> dict:
        """Live photodiode-voltage readout for the setup-UI card: latest V, the rolling 1%/99% band,
        a short recent trace, and the warn thresholds. available=False when there is no serial
        (pyserial missing / port never opened). A port that WAS open and dropped stays available:
        its telemetry just goes stale, which is what the heartbeat diagnosis reads as a dead
        sensor chain."""
        if getattr(self, "_serial", None) is None and not getattr(self, "_serial_seen", False):
            return {"available": False}
        with self._v_lock:
            n = len(self._v)
            latest = self._v[-1][1] if n else None
            # Last 120 amplitude samples: ~1.2 s of the 100 Hz v2_1 stream, or minutes of
            # v2_2 per-pulse medians — the card's trace, not a fixed time span.
            recent = [round(v, 3) for (_, v) in list(self._v)[-120:]]
        pr = self._v_percentiles(t_start=time.time() - self.v_window_s)
        return {"available": True, "n": n, "v": latest,
                "p1": (pr[0] if pr else None), "p99": (pr[1] if pr else None),
                "recent": recent, "v_high": self.v_high, "v_low": self.v_low,
                # v2_2 per-pulse telemetry (None until the first tagged line arrives)
                "last_pulse": self._last_pulse, "last_baseline": self._last_baseline,
                "fw": self.fw_info()}

    # ── Teensy firmware: what runs there, and its live tunables (v2_3) ───────────────────────────

    def fw_info(self) -> dict:
        """What the Teensy runs, as far as the serial port tells: from its "I" line (firmware
        v2_3: version, DEBUG build or not, the five tunables), else guessed from the lines it
        sends (B lines = a production build before v2_3, bare numbers = an old DEBUG build)."""
        now = time.time()
        fw, seen = self._fw, self._line_seen
        out = {"serial_open": self._serial is not None, "serial_error": self._serial_err,
               "stream": (now - seen.get("D", 0)) < 1.5}
        if fw is not None:
            out.update(known=True, version=fw["version"], debug=fw["debug"],
                       params=dict(fw["params"]), age_s=round(now - fw["t"], 1))
        else:
            old_debug = (now - seen.get("V", 0)) < 3.0
            old_prod = (now - seen.get("B", 0)) < 3.0
            out.update(known=False, params=None,
                       version="older than v2_3" if (old_debug or old_prod) else None,
                       debug=True if old_debug else False if old_prod else None)
        return out

    def fw_query(self, timeout=0.6) -> dict:
        """Ask the Teensy ("?") and wait for its "I" line; fw_info() either way."""
        seq = self._fw_seq
        if self._serial_write(b"?\n"):
            deadline = time.time() + timeout
            while self._fw_seq == seq and time.time() < deadline:
                time.sleep(0.02)
        return self.fw_info()

    def teensy_set(self, params: dict, timeout=1.5) -> dict:
        """Send tunables to a DEBUG build ("S <NAME> <value>" each) and report what the Teensy
        then says it runs. They live in the Teensy's RAM only: the next upload or power cycle
        brings back what the sketch was compiled with."""
        fw = self.fw_query()
        if not fw["serial_open"]:
            return {"ok": False, "error": "the Teensy serial port is not open", "fw": fw}
        if not fw["known"]:
            return {"ok": False, "fw": fw,
                    "error": "the Teensy does not answer '?' — its firmware is older than v2_3; upload the sketch"}
        if not fw["debug"]:
            return {"ok": False, "fw": fw, "error": "the Teensy runs the production build — it takes "
                                                    "parameters only in the DEBUG build"}
        clean, errors = check_teensy_params(params, fw["params"])
        if errors or not clean:
            return {"ok": False, "error": "; ".join(errors) or "no parameters given", "fw": fw}
        seq, t_send = self._fw_seq, time.time()
        for name, val in clean.items():
            self._serial_write(f"S {name} {val:d}\n".encode() if isinstance(val, int)
                               else f"S {name} {val:.3f}\n".encode())
        deadline = t_send + timeout
        while self._fw_seq < seq + len(clean) and time.time() < deadline:
            time.sleep(0.02)
        fw = self.fw_info()
        refused = [text for (t, text) in list(self._x_lines) if t >= t_send]
        now_runs = fw.get("params") or {}
        differ = [n for n, v in clean.items() if abs(now_runs.get(n, float("nan")) - v) > 0.0006]
        if self._fw_seq < seq + len(clean):
            err = "the Teensy did not confirm in time"
        elif refused:
            err = "the Teensy refused: " + "; ".join(refused)
        elif differ:
            err = "the Teensy reports other values for " + ", ".join(differ)
        else:
            err = None
        return {"ok": err is None, "error": err, "fw": fw, "sent": clean}

    def verify_sync(self, sync_burst, duration_s=None):
        """Universal init check: flash the display for ~duration_s while counting our own rising edges
        and sampling V, then pass/fail. `sync_burst(duration_s) -> emitted_flash_count` is supplied by
        the caller (the flash runs on the FOLLOWER, so the transport differs: setup POSTs to the
        follower, the experiment UDPs it). Returns {ok, detected, emitted, p1, p99, reason}."""
        duration_s = self.verify_duration_s if duration_s is None else float(duration_s)
        started_here = self._cb is None
        if started_here:
            self.start_stream(lambda e: None)    # register the RISING-edge counter (_on_edge)
        self.reset_trial()                       # zero pulse count + anchor timebase on the main thread
        t0 = time.time()
        try:
            emitted = int(sync_burst(duration_s))
        except Exception as e:
            if started_here:
                self.stop_stream()
                self._callback = None
            return {"ok": False, "detected": 0, "emitted": None, "p1": None, "p99": None,
                    "reason": f"display flash failed: {e}"}
        detected = self._pulse_idx
        if started_here:
            self.stop_stream()
            self._callback = None
        # A burst that emitted nothing means the DISPLAY path is broken (the follower replies
        # {flashes: 0} when its draw raised) — without this gate, "detected >= emitted - 1" is
        # trivially true for emitted 0 and the strongest barrier in the chain false-passes
        # exactly when the display is dead (bench 2026-08-22).
        if emitted <= 0:
            return {"ok": False, "detected": detected, "emitted": emitted, "p1": None, "p99": None,
                    "reason": f"display emitted no flashes (emitted={emitted}) — display path broken"}
        # V gate (only when the serial stream is available): fail if the flash peak never rose
        # (99% <= v_low) or the signal is saturated (1% >= v_high).
        p1 = p99 = None
        v_ok = True
        if self._serial is not None:
            # Amplitude series only (P medians under v2_2, raw under v2_1). min_n=3: a 1 s
            # v2_2 burst yields only ~12 P lines, and the old min of 10 silently disabled
            # this gate for short bursts.
            pr = self._v_percentiles(t0, time.time(), min_n=3)
            if pr is not None:
                p1, p99 = pr
                v_ok = (p99 > self.v_low) and (p1 < self.v_high)
        # "Missing-only": a colour-sequential DLP renders each flash as several brief sub-pulses, so
        # the photodiode legitimately detects MORE pulses than flashes emitted. Only MISSING flashes
        # (detected below emitted) indicate a broken display->diode path; extra edges are expected.
        # (±1 missing tolerated, per spec.)
        count_ok = detected >= emitted - 1
        ok = bool(count_ok and v_ok)
        if not count_ok:
            reason = f"missed flashes: detected {detected} of {emitted} emitted"
        elif not v_ok and p99 is not None and p99 <= self.v_low:
            reason = f"no flash detected: peak V (99% = {p99:.2f}) <= {self.v_low}"
        elif not v_ok:
            reason = f"saturated: baseline V (1% = {p1:.2f}) >= {self.v_high}"
        else:
            reason = "ok"
        return {"ok": ok, "detected": detected, "emitted": emitted, "p1": p1, "p99": p99,
                "reason": reason}

    def start_stream(self, callback):
        lgpio = self._lgpio
        if self._cb:                     # cancel a prior callback so a re-start can't double-deliver
            self._cb.cancel()
            self._cb = None
        self._callback = callback
        self._active = True
        self._pulse_idx = 0
        self._ref_tick = None
        self._cb = lgpio.callback(self._chip, self.gpio, lgpio.RISING_EDGE, self._on_edge)

    def stop_stream(self):
        self._active = False
        if self._cb:
            self._cb.cancel()
            self._cb = None

    # ── Debug window (setup UI): the DEBUG build's analog stream + this Pi's GPIO edges ─────────

    def debug_start(self) -> dict:
        """Start filling the debug ring: the Teensy's "D" samples and "E" marks (a DEBUG build
        of firmware v2_3 sends them) plus every rising edge this Pi sees on the sync GPIO.
        Returns fw_query(). Filling stops by itself DEBUG_IDLE_S after the last debug_read."""
        self._dbg_clear()
        self._dbg_last_read = time.time()
        if self._dbg_cb is None:
            # lgpio ticks are CLOCK_MONOTONIC ns: pair the two clocks here, off the callback.
            self._dbg_ref = (time.monotonic_ns(), time.time())
            self._dbg_cb = self._lgpio.callback(self._chip, self.gpio, self._lgpio.RISING_EDGE,
                                                self._on_dbg_edge)
        self._dbg_on = True
        return self.fw_query()

    def debug_stop(self):
        self._dbg_on = False
        cb, self._dbg_cb = self._dbg_cb, None
        if cb is not None:
            try:
                cb.cancel()
            except Exception:
                pass

    def _on_dbg_edge(self, chip, gpio, level, tick):
        # Lock-free on purpose: this runs on lgpio's notify thread, which the session's own
        # edge callbacks share. Only this thread writes the counter; readers snapshot the deque.
        if level != 1 or not self._dbg_on:
            return
        ref_tick, ref_time = self._dbg_ref
        self._dbg_edge_n += 1
        self._dbg_edges.append((self._dbg_edge_n, ref_time + (tick - ref_tick) / 1e9))

    def _dbg_clear(self):
        with self._dbg_lock:
            self._dbg_clear_locked()

    def _dbg_clear_locked(self):
        self._dbg.clear()
        self._dbg_acc.clear()
        self._dbg_edges.clear()
        self._off_fine.clear()
        self._dbg_n = self._dbg_acc_n = self._dbg_matched_n = 0
        self._dbg_last_us = None
        self._dbg_wraps = 0
        self._dbg_t0 = None
        self._off_cur = self._off_prev = None
        self._off_t0 = 0.0
        self._dbg_epoch += 1                 # readers drop what they hold when this changes

    def _dbg_store(self, samples, accepts, now):
        """Debug samples and accepted-pulse marks into the ring, on the TEENSY's clock: its
        micros() unwrapped to seconds (it rolls over every 71.6 min). The Pi clock is tied to it
        in _dbg_offset_locked."""
        with self._dbg_lock:
            last, wraps = self._dbg_last_us, self._dbg_wraps
            for t_us, v, hi, lo in samples:
                if last is not None and t_us < last:
                    if last - t_us > 0x80000000:
                        wraps += 1                    # micros() rolled over
                    else:
                        self._dbg_clear_locked()      # time ran backwards: the Teensy restarted
                        wraps = 0
                last = t_us
                self._dbg.append(((wraps * 4294967296 + t_us) * 1e-6, v, hi, lo))
                self._dbg_n += 1
            self._dbg_last_us, self._dbg_wraps = last, wraps
            if last is None:
                return
            for t_us in accepts:
                d = t_us - last                       # an E line sits between D lines of its chunk
                w = wraps + 1 if d < -0x80000000 else wraps - 1 if d > 0x80000000 else wraps
                self._dbg_acc.append((w * 4294967296 + t_us) * 1e-6)
                self._dbg_acc_n += 1
            if samples:
                if self._dbg_t0 is None:
                    self._dbg_t0 = self._dbg[0][0]
                # Arrival minus Teensy time is the clock offset plus the USB / read delay, which
                # is never negative: the smallest value of the last 1-2 buckets is the offset.
                off = now - self._dbg[-1][0]
                if self._off_cur is None or now - self._off_t0 > OFFSET_BUCKET_S:
                    self._off_prev, self._off_cur, self._off_t0 = self._off_cur, off, now
                elif off < self._off_cur:
                    self._off_cur = off

    def _dbg_coarse_locked(self):
        vals = [o for o in (self._off_cur, self._off_prev) if o is not None]
        return min(vals) if vals else None

    def _dbg_match_locked(self, now, edges):
        """Tie the two clocks exactly where that is possible: an "E" line is the Teensy time at
        which OUT_PIN rose, and the GPIO edge it caused carries the kernel's timestamp. Each
        pair closer than MATCH_S (by the serial estimate) gives one exact offset sample."""
        coarse = self._dbg_coarse_locked()
        n_new = min(self._dbg_acc_n - self._dbg_matched_n, len(self._dbg_acc))
        if coarse is None or n_new <= 0:
            return
        for ts in list(self._dbg_acc)[len(self._dbg_acc) - n_new:]:
            pred = ts + coarse
            if pred > now - EDGE_SETTLE_S + 0.01:     # give the GPIO callback time to deliver
                break
            self._dbg_matched_n += 1
            best = None
            for _, te in reversed(edges):
                if te - pred < -MATCH_S:
                    break
                if te - pred <= MATCH_S and (best is None or abs(te - pred) < abs(best - pred)):
                    best = te
            if best is not None:
                self._off_fine.append((now, best - ts))

    def _dbg_offset_locked(self, now):
        """(Pi wall time - Teensy time, how it is known): "gpio" = from matched pulses (exact to
        the GPIO timestamp), "serial" = from serial arrival times (a few 100 us late at worst)."""
        fine = self._off_fine
        if fine and now - fine[-1][0] < 5.0:
            # Median of the last few matches (one wrong pairing must not move the axis), but
            # only fresh ones: the two crystals drift apart by ~0.1 ms within seconds.
            vals = sorted(o for t, o in list(fine)[-5:] if now - t < 2.0) or [fine[-1][1]]
            return vals[len(vals) // 2], "gpio"
        coarse = self._dbg_coarse_locked()
        return (coarse, "serial") if coarse is not None else (None, None)

    def debug_read(self, since=0, edges_since=0, accepts_since=0, epoch=None) -> dict:
        """What the Debug window draws, from the cursors of the previous call on: samples
        (t, v, hi, lo as parallel lists), `accepts` (Teensy says: pulse accepted) and `edges`
        (this Pi's GPIO saw a rising edge), all in seconds on one axis — the Teensy clock since
        the first sample held. A different `epoch` means the ring was cleared (restart of the
        Teensy, reconnect, debug_start): the cursors are then ignored. Edges come EDGE_SETTLE_S
        late; `sync` says how they were placed (see _dbg_offset_locked)."""
        now = time.time()
        self._dbg_last_read = now
        edges = _snapshot(self._dbg_edges)
        with self._dbg_lock:
            self._dbg_match_locked(now, edges)
            if epoch != self._dbg_epoch:
                since = edges_since = accepts_since = 0
            held = len(self._dbg)
            start = min(held, max(0, since - (self._dbg_n - held)))
            start = max(start, held - DEBUG_READ_MAX)        # a reader that fell behind skips ahead
            samples = list(itertools.islice(self._dbg, start, None))
            n_acc = min(len(self._dbg_acc), max(0, self._dbg_acc_n - accepts_since))
            accepts = list(self._dbg_acc)[len(self._dbg_acc) - n_acc:] if n_acc else []
            t0, t_last = self._dbg_t0, (self._dbg[-1][0] if held else None)
            off, sync = self._dbg_offset_locked(now)
            out = {"ok": True, "on": self._dbg_on, "epoch": self._dbg_epoch, "seq": self._dbg_n,
                   "aseq": self._dbg_acc_n, "sync": sync,
                   "accepts_1s": (sum(1 for a in self._dbg_acc if a > t_last - 1.0)
                                  if t_last is not None else 0)}
        # An edge is handed out once, so only when its place on the axis is final: after the "E"
        # line of its pulse had time to arrive and be matched (above).
        ready = [(i, te) for i, te in edges if i > edges_since and te <= now - EDGE_SETTLE_S]
        new_edges = [te for _, te in ready]
        out["eseq"] = ready[-1][0] if ready else edges_since
        out["edges_1s"] = sum(1 for _, te in edges if te > now - 1.0)
        if t0 is None:
            samples, accepts, new_edges = [], [], []
        out["t"] = [round(s[0] - t0, 6) for s in samples]
        out["v"] = [s[1] for s in samples]
        out["hi"] = [s[2] for s in samples]
        out["lo"] = [s[3] for s in samples]
        out["accepts"] = [round(a - t0, 6) for a in accepts]
        out["edges"] = [round(te - off - t0, 6) for te in new_edges] if off is not None else []
        out["fw"] = self.fw_info()
        return out

    def reset_trial(self):
        """Reset the per-trial pulse buffer AND re-anchor the trial timebase on the MAIN thread from
        a paired (hardware ns tick, wall clock) read — so the first pulse's timestamp comes from the
        jitter-free hardware tick, not a time.time() sampled inside the GIL-scheduled lgpio callback.
        lgpio alert ticks are CLOCK_MONOTONIC ns (the kernel GPIO-event timestamp), so
        time.monotonic_ns() is the matching main-thread reference. Using time.time_ns()
        (CLOCK_REALTIME) here mismatched the clocks and put every pulse ~1.7e9 s in the past
        (sync_ok=0 every trial, pulses off the raster)."""
        self._pulse_idx = 0
        self._trial_pulses = []
        try:
            self._ref_tick = time.monotonic_ns()
            self._ref_time = time.time()
        except Exception:
            self._ref_tick = None            # fall back to first-pulse anchoring in _on_edge
            self._ref_time = None

    def _on_edge(self, chip, gpio, level, tick):
        if not self._active:
            return
        # The line is claimed BOTH_EDGES, so this RISING-only detected stream must drop
        # non-rising deliveries itself: level 0 = falling
        # (the trailing edge of the Teensy's ~2 ms pulse — logging it double-counts every flash),
        # level 2 = lgpio watchdog/timeout. Keep only level 1 = the rising edge = one per sync flash.
        if level != 1:
            return
        # No hold-off/glitch filtering here — the Teensy already delivered a clean, single pulse.
        # Timebase is anchored in reset_trial (main thread); fall back to the first pulse if that
        # reference read failed, so timing still works if reset_trial wasn't called.
        if self._ref_tick is None:
            self._ref_tick = tick
            self._ref_time = time.time()
        # lgpio tick is CLOCK_MONOTONIC ns; the main-thread (monotonic_ns, time) anchor makes the
        # precise edge time = ref wall clock + hardware-tick delta (ns -> s), free of callback jitter.
        t_precise = self._ref_time + (tick - self._ref_tick) / 1e9
        self._pulse_idx += 1
        self._trial_pulses.append(t_precise)
        if self._callback:
            self._callback({
                "event": "sync_pulse",
                "t": t_precise,
                "pulse_idx": self._pulse_idx,
            })

    def check(self) -> dict:
        try:
            level = self._lgpio.gpio_read(self._chip, self.gpio)
            return {"ok": True, "message": f"GPIO{self.gpio} level={level}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def hdf5_datasets(self) -> dict:
        import numpy as np
        import h5py
        return {"sync_pulses": h5py.vlen_dtype(np.float64)}

    def hdf5_trial_data(self, ctx: dict) -> dict:
        return {"sync_pulses": ctx.get("sync_pulses", self._trial_pulses)}

    def close(self):
        self.stop_stream()
        self.debug_stop()
        self._serial_stop.set()
        ser = self._serial
        if ser is not None:
            try:
                ser.cancel_read()            # wake the reader out of its blocking read
            except Exception:
                pass
        th = self._serial_thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout=1.5)             # so it cannot reopen the port after we close it
        ser, self._serial = self._serial, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        try:
            self._lgpio.gpio_free(self._chip, self.gpio)
        except Exception:
            pass
        try:
            self._lgpio.gpiochip_close(self._chip)
        except Exception:
            pass
