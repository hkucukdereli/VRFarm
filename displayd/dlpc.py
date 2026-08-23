"""
displayd/dlpc.py

DLPC3436 I2C wrapper for the display daemon (PROTOCOL.md "dlpc.py API").
Imports the vendored TI SDK from ~/dlp at open() time (sys.path append
~/dlp + ~/dlp/api) and drives the projector controller over /dev/i2c-22,
8-bit slave address 0x36. Pure stdlib; no pygame, no flask.

The generated TI API (api/dlpc343x_xpr4.py) has three bench-proven traps this
wrapper exists to neutralize:

  1. Every Write* function's except block reads `Summary.Successful == False`
     (comparison, not assignment), so write summaries ALWAYS claim success.
     Never trusted here: writes are checked via the transport-error flag set by
     our own I2C callbacks, and init_parallel() verifies with readbacks.
  2. Read functions that return try-local variables (ReadControllerDeviceId,
     ReadSourceSelect, ...) have a `return` inside `finally`, so a failed I2C
     read surfaces as NameError (unbound local) — treated as "no response".
  3. Read functions that return module-global objects (ShortStatus,
     SystemStatus, CommunicationStatus, AutoFramingInformation) also `return`
     inside `finally`, which SWALLOWS an in-flight OSError and hands back the
     previous (stale) globals with Successful still True. Our callbacks record
     the OSError in _io_error before re-raising; _invoke() discards any value
     produced while that flag is set.

All bus access — sweep, init sequence, convenience reads — is serialized by a
single threading.Lock owned by the instance, so the 1 Hz L1 poller can never
interleave with an init sequence. The SDK registers its I2C callbacks in
module globals, so run exactly one Dlpc instance per process.

sweep() read payload shapes (what the L1 poller keys on):
  reads["SourceSelect"]["source"]              -> {"name": "ExternalParallelPort", "value": 1}
  reads["SystemStatus"]["ActuatorWatchdogTimerTimeout"] -> 0|1
  reads["DisplayImageCurtain"]["enabled"]      -> 0|1
  (bit-field reads serialize each field to a plain int; enum results to
  {"name", "value"}; multi-value reads to labeled dicts — see _READS.)
"""

import copy
import os
import subprocess
import sys
import threading
import time
from enum import Enum


class _StepFailed(Exception):
    """Internal: aborts the init sequence at the first failed step."""
    pass


def _ser(x):
    """JSON-safe serialization of SDK results (same approach as the bench
    probe): Enum -> {name, value}; result objects/classes -> dict of public
    attrs; tuples -> lists; scalars pass through; anything else -> repr."""
    if isinstance(x, Enum):
        return {"name": x.name, "value": x.value}
    if hasattr(x, "__dict__") and vars(x):
        return {k: _ser(v) for k, v in vars(x).items() if not k.startswith("_")}
    if isinstance(x, (list, tuple)):
        return [_ser(v) for v in x]
    if isinstance(x, (int, float, str, bool)) or x is None:
        return x
    return repr(x)


# The bench 18-read set (~8.6 ms full sweep). Each entry: (name, call, fields).
# fields labels the payload values after the (untrustworthy) Summary is
# dropped; None means the single payload object serializes to a dict itself
# (the module-global result classes — their bit fields become plain ints).
_READS = [
    ("ControllerDeviceId", lambda a: a.ReadControllerDeviceId(), ("device_id",)),
    ("ShortStatus", lambda a: a.ReadShortStatus(), None),
    ("SystemStatus", lambda a: a.ReadSystemStatus(), None),
    ("CommunicationStatus", lambda a: a.ReadCommunicationStatus(), None),
    ("SystemSoftwareVersion", lambda a: a.ReadSystemSoftwareVersion(),
     ("patch", "minor", "major")),
    ("FirmwareBuildVersion", lambda a: a.ReadFirmwareBuildVersion(),
     ("patch", "minor", "major")),
    ("DmdDeviceId", lambda a: a.ReadDmdDeviceId(a.DmdDataSelection.DmdDeviceId),
     ("device_id",)),
    ("FpgaVersion", lambda a: a.ReadFpgaVersion(),
     ("version", "eco_revision", "arm_sw_version")),
    ("FpgaStatus", lambda a: a.ReadFpgaStatus(),
     ("display_mode", "keying_status")),
    ("SourceSelect", lambda a: a.ReadSourceSelect(),
     ("source", "external_calibration")),
    ("InputImageSize", lambda a: a.ReadInputImageSize(),
     ("pixels_per_line", "lines_per_frame")),
    ("AutoFramingInformation", lambda a: a.ReadAutoFramingInformation(), None),
    ("DmdSequencerSyncMode", lambda a: a.ReadDmdSequencerSyncMode(),
     ("sync_mode", "auto_sync")),
    ("ParallelVideoControl", lambda a: a.ReadParallelVideoControl(),
     ("clock_sample", "ivalid_polarity", "hsync_polarity", "vsync_polarity")),
    ("ExternalVideoSourceFormatSelect",
     lambda a: a.ReadExternalVideoSourceFormatSelect(), ("format",)),
    ("DisplayImageCurtain", lambda a: a.ReadDisplayImageCurtain(),
     ("enabled", "color")),
    ("RgbLedEnable", lambda a: a.ReadRgbLedEnable(), ("red", "green", "blue")),
    ("RgbLedCurrent", lambda a: a.ReadRgbLedCurrent(), ("red", "green", "blue")),
]

# Of the 18, these five are boot constants: silicon/firmware identity that cannot change
# while the controller is powered. sweep() reads each ONCE per open() and merges the cached
# value back in — same keys, same order, same payload shape, ~5/18 fewer I2C round trips
# every second. A failed read is never cached, so it is retried on the next sweep; once
# cached, a later bus failure still surfaces through the 13 dynamic reads.
_STATIC_READS = frozenset((
    "ControllerDeviceId", "SystemSoftwareVersion", "FirmwareBuildVersion",
    "DmdDeviceId", "FpgaVersion",
))


class Dlpc(object):

    def __init__(self, dlp_dir="~/dlp", bus=22, address=0x36):
        # address is the 8-bit form (0x36); the SDK's LinuxI2C shifts to 7-bit.
        self.dlp_dir = os.path.expanduser(dlp_dir)
        self.bus = bus
        self.address = address
        self._lock = threading.Lock()
        self._api = None       # api.dlpc343x_xpr4 module, imported in open()
        self._i2c = None       # linuxi2c.LinuxI2C, opened in open()
        self._io_error = None  # transport failure flag; see _write_cb/_read_cb
        self._static_cache = {}  # _STATIC_READS payloads, refreshed on open()

    # ---------------------------------------------------------------- open/close

    def open(self):
        """Import the vendored SDK and open the I2C bus. Idempotent."""
        with self._lock:
            if self._i2c is not None:
                return
            # New bus session -> the boot-constant reads are re-taken on the next sweep
            # (they are only constant for as long as the controller stays powered).
            self._static_cache = {}
            # The SDK is not a package: ~/dlp holds linuxi2c.py and the api/
            # namespace dir. Appended (not inserted) so stdlib and repo modules
            # always shadow the vendored names.
            for p in (self.dlp_dir, os.path.join(self.dlp_dir, "api")):
                if p not in sys.path:
                    sys.path.append(p)
            import api.dlpc343x_xpr4 as _api  # noqa: N813 (vendored name)
            import linuxi2c
            self._api = _api
            # Registering our callbacks routes every SDK command through this
            # instance's transport (and its _io_error accounting).
            self._api.DLPC343X_XPR4init(self._read_cb, self._write_cb)
            i2c_dev = linuxi2c.LinuxI2C(self.bus, self.address)
            i2c_dev.open()
            self._i2c = i2c_dev

    def close(self):
        with self._lock:
            if self._i2c is not None:
                try:
                    self._i2c.close()
                except Exception:
                    pass
                self._i2c = None

    # ---------------------------------------------------------------- transport

    # Callbacks handed to DLPC343X_XPR4init. They record any transport failure
    # in _io_error BEFORE re-raising, because the SDK's finally-returns swallow
    # the exception for writes and for global-object reads — the flag is the
    # only reliable failure signal (pitfalls 1 and 3 in the module docstring).

    def _write_cb(self, writebytes, protocoldata):
        try:
            self._i2c.write(writebytes)
        except Exception as e:
            self._io_error = "%s: %s" % (type(e).__name__, e)
            raise

    def _read_cb(self, readbytecount, writebytes, protocoldata):
        try:
            self._i2c.write(writebytes)
            return self._i2c.read(readbytecount)
        except Exception as e:
            self._io_error = "%s: %s" % (type(e).__name__, e)
            raise

    def _require_open(self):
        if self._i2c is None or self._api is None:
            raise RuntimeError("Dlpc.open() not called")

    def _invoke(self, fn):
        """Call one SDK function defensively (lock must be held).
        Returns (result, error): error is None only when the transport round
        trip actually succeeded — the SDK's own Summary is never consulted."""
        self._io_error = None
        try:
            res = fn(self._api)
        except NameError:
            # finally-return of a try-local after a failed read (pitfall 2):
            # the controller did not answer.
            return None, self._io_error or "no response (NameError in SDK read)"
        except Exception as e:
            return None, "%s: %s" % (type(e).__name__, e)
        if self._io_error is not None:
            # The SDK swallowed the transport error and returned stale
            # module-global state (pitfall 3). Discard the value.
            return None, self._io_error
        return res, None

    @staticmethod
    def _label(res, fields):
        """Shape one read result: drop the Summary (res[0]), label the rest."""
        payload = res[1:] if isinstance(res, tuple) else (res,)
        if fields is None:
            return _ser(payload[0])
        return {k: _ser(v) for k, v in zip(fields, payload)}

    # ---------------------------------------------------------------- sweep

    def sweep(self):
        """Run the 18-read health sweep. Returns {"ok", "reads", "errors"}
        (+ "t" wall time and "sweep_ms"); a read appears in exactly one of
        reads/errors. ok means every read answered.

        The five _STATIC_READS are boot constants: hit the bus only until each
        has answered once, then served from the per-open cache. The output dict
        is byte-for-byte the shape it always was (cached values merged in, in
        _READS order) — only sweep_ms drops."""
        out = {"ok": True, "t": time.time(), "reads": {}, "errors": {}}
        t0 = time.perf_counter()
        with self._lock:
            self._require_open()
            for name, fn, fields in _READS:
                static = name in _STATIC_READS
                if static:
                    cached = self._static_cache.get(name)
                    if cached is not None:
                        # Copy: the caller owns the sweep dict and must never be able
                        # to mutate the cache through it.
                        out["reads"][name] = copy.deepcopy(cached)
                        continue
                res, err = self._invoke(fn)
                if err is not None:
                    out["errors"][name] = err
                    continue
                try:
                    value = self._label(res, fields)
                except Exception as e:  # malformed payload ≠ healthy read
                    out["errors"][name] = "serialize: %s: %s" % (type(e).__name__, e)
                    continue
                out["reads"][name] = value
                if static:
                    self._static_cache[name] = copy.deepcopy(value)
        out["sweep_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        out["ok"] = not out["errors"]
        return out

    # ---------------------------------------------------------------- init

    @staticmethod
    def _pinctrl(args):
        """Run one `pinctrl <args>`. Returns None on success, else an error string
        (never raises: a missing/failing pinctrl is a reported init step, not a
        traceback out of the init sequence)."""
        try:
            proc = subprocess.run(["pinctrl"] + list(args),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=10)
        except Exception as e:
            return "%s: %s" % (type(e).__name__, e)
        if proc.returncode != 0:
            return (proc.stderr.decode(errors="replace").strip()
                    or "pinctrl rc=%d" % proc.returncode)
        return None

    def init_parallel(self):
        """Port of dlp/init_parallel_mode.py, in-process and verified:
        GPIO24/26 tri-stated (flash selects released) -> GPIO25 high (RGB666
        buffer enable) -> curtain on -> source select external
        parallel -> input size 1920x1080 -> actuator DAC on -> RGB666 ->
        chroma swap -> parallel video polarities -> CCA off -> delay ->
        curtain off -> readback source+curtain -> 2.5 s light settle.

        Returns {"ok", "steps": [{"step","ok","error","ms"}...], "error"}.
        Write summaries lie (pitfall 1), so each write is checked via the
        transport flag and the end state is proven by readbacks. The curtain
        is lowered in a finally so no exception can strand it ON. The 2.5 s
        settle runs here (success path only) — callers must not sleep again.
        Holds the bus lock throughout, so a concurrent sweep cannot interleave.
        """
        steps = []
        error = None
        curtain_up = False

        def run(name, fn):
            t0 = time.perf_counter()
            res, err = self._invoke(fn)
            steps.append({"step": name, "ok": err is None, "error": err,
                          "ms": round((time.perf_counter() - t0) * 1000.0, 2)})
            if err is not None:
                raise _StepFailed("%s: %s" % (name, err))
            return res

        def fail_last(msg):
            steps[-1]["ok"] = False
            steps[-1]["error"] = msg
            raise _StepFailed("%s: %s" % (steps[-1]["step"], msg))

        with self._lock:
            self._require_open()
            try:
                # ---- GPIO preamble: TI's InitGPIO, reduced to what is safe under KMS ----
                # dlp/init_parallel_mode.py ALWAYS ran InitGPIO() before touching the
                # DLPC. Its docstring (dlp/api/dlpc343x_xpr4_evm.py) is a hardware
                # warning, not a style note:
                #   BCM24 -> SPI_SELECT_ASIC (flash select; tri-state for video mode)
                #   BCM25 -> RGB_BUFFER_SEL  (RGB666 buffer enable; drive high)
                #   BCM26 -> SPI_SELECT_FPGA (flash select; tri-state for video mode)
                #   "Do NOT attempt to enable RGB666 buffers and access ASIC/FPGA flash
                #    devices simultaneously. Damage to flash devices may occur."
                # So the two flash-select lines must be RELEASED BEFORE GPIO25 goes high.
                # That ordering is the entire safety content of InitGPIO, and driving 25
                # high on its own (what this step used to do) skipped it: whatever a
                # previous flash-write tool, an aborted run, or the boot default left on
                # 24/26 stayed asserted while the RGB666 buffer came on.
                #
                # Deliberately NOT reproduced from InitGPIO, because under displayd the
                # Pi is in full KMS (dtoverlay=vc4-kms-dpi-generic, see
                # dlp/sample_config/config_kms.txt) rather than the firmware-DPI setup
                # TI's script and display_calibration/start_projector.sh assumed:
                #   * `set 0-21 a2` (the ALT2/DPI mux, start_projector.sh step 1) — the
                #     KERNEL owns and muxes GPIO0-21 for the DPI panel; re-driving them
                #     from userspace fights the vc4 driver instead of helping it.
                #   * `set 1-27 ip pn` wholesale — that range covers GPIO22/23, which are
                #     the i2c-gpio bus (bus=22) this very sequence is talking to the DLPC
                #     over, and GPIO25 itself. Tri-stating exactly 24 and 26 gets the
                #     safety without pulling the control bus out from under us.
                #   * `set 0 op pn` + `gpio drive 0 ...` — a drive-strength tweak on a
                #     kernel-owned DPI pin; same reason, and not safety-relevant.
                # pinctrl writes the pad registers synchronously, so no settle delay is
                # needed between the tri-state and the buffer enable (TI's sleep(1)
                # covered a full 27-pin reset done through fire-and-forget shell calls).
                def gpio_step(name, calls):
                    t0 = time.perf_counter()
                    gpio_err = None
                    for args in calls:
                        gpio_err = self._pinctrl(args)
                        if gpio_err is not None:
                            break
                    steps.append({
                        "step": name, "ok": gpio_err is None, "error": gpio_err,
                        "ms": round((time.perf_counter() - t0) * 1000.0, 2)})
                    if gpio_err is not None:
                        raise _StepFailed("%s: %s" % (name, gpio_err))

                # Flash selects released FIRST (see above) — abort if that cannot be
                # proven, rather than enabling the buffer over an asserted flash select.
                gpio_step("gpio_tristate_flash_selects",
                          [["set", "24", "ip", "pn"], ["set", "26", "ip", "pn"]])
                # GPIO25 drives the EVM's parallel-video (RGB666) buffer enable; must be
                # high before the DLPC will lock to the DPI signal. Idempotent.
                gpio_step("gpio25_high", [["set", "25", "op", "dh"]])

                api = self._api
                run("curtain_on",
                    lambda a: a.WriteDisplayImageCurtain(1, a.Color.Black))
                curtain_up = True
                run("source_select_parallel",
                    lambda a: a.WriteSourceSelect(a.Source.ExternalParallelPort,
                                                  a.Enable.Disable))
                run("input_image_size_1920x1080",
                    lambda a: a.WriteInputImageSize(1920, 1080))
                run("actuator_dac_enable",
                    lambda a: a.WriteActuatorGlobalDacOutputEnable(a.Enable.Enable))
                run("format_rgb666",
                    lambda a: a.WriteExternalVideoSourceFormatSelect(
                        a.ExternalVideoFormat.Rgb666))
                run("chroma_swap_cbcr",
                    lambda a: a.WriteVideoChromaChannelSwapSelect(
                        a.ChromaChannelSwap.Cbcr))
                run("parallel_video_control",
                    lambda a: a.WriteParallelVideoControl(
                        a.ClockSample.FallingEdge, a.Polarity.ActiveHigh,
                        a.Polarity.ActiveLow, a.Polarity.ActiveLow))
                run("cca_disable",
                    lambda a: a.WriteColorCoordinateAdjustmentControl(0))
                # TI's sequence pauses before dropping the curtain so the
                # source settings latch (WriteDelay is controller-side).
                run("delay", lambda a: a.WriteDelay(50))
                time.sleep(1.0)
                run("curtain_off",
                    lambda a: a.WriteDisplayImageCurtain(0, a.Color.Black))
                curtain_up = False

                # Readbacks: the only trustworthy success signal (pitfall 1).
                res = run("readback_source", lambda a: a.ReadSourceSelect())
                if res[1] is not api.Source.ExternalParallelPort:
                    fail_last("source readback = %s (want ExternalParallelPort)"
                              % getattr(res[1], "name", res[1]))
                res = run("readback_curtain",
                          lambda a: a.ReadDisplayImageCurtain())
                if int(res[1]) != 0:
                    fail_last("curtain readback = on (want off)")

                # Light settle: lamp/actuator stabilization before any optics
                # check downstream (DLPC_LOCKED contract).
                time.sleep(2.5)
            except _StepFailed as e:
                error = str(e)
            except Exception as e:
                error = "%s: %s" % (type(e).__name__, e)
            finally:
                if curtain_up:
                    # Never strand the curtain ON after an aborted sequence.
                    # _invoke so a dead bus cannot raise out of this finally.
                    _, err = self._invoke(
                        lambda a: a.WriteDisplayImageCurtain(0, a.Color.Black))
                    steps.append({"step": "curtain_off_emergency",
                                  "ok": err is None, "error": err, "ms": 0.0})
        return {"ok": error is None, "steps": steps, "error": error}

    # ---------------------------------------------------------------- reads

    def read_source(self):
        """{"ok", "source": <Source name>|None, "external_calibration":
        <Enable name>|None, "error"} — DLPC_LOCKED checks source ==
        "ExternalParallelPort"."""
        with self._lock:
            self._require_open()
            res, err = self._invoke(lambda a: a.ReadSourceSelect())
        if err is not None:
            return {"ok": False, "source": None,
                    "external_calibration": None, "error": err}
        return {"ok": True, "source": res[1].name,
                "external_calibration": res[2].name, "error": None}

    def read_curtain(self):
        """{"ok", "on": bool|None, "color": <Color name>|None, "error"} —
        CURTAIN_DOWN checks on == False."""
        with self._lock:
            self._require_open()
            res, err = self._invoke(lambda a: a.ReadDisplayImageCurtain())
        if err is not None:
            return {"ok": False, "on": None, "color": None, "error": err}
        return {"ok": True, "on": bool(int(res[1])), "color": res[2].name,
                "error": None}

    def read_short(self):
        """{"ok", "status": {bit: 0|1, ...}|None, "error"} — one successful
        read is the DLPC_ALIVE criterion (logic powered, NOT projector on)."""
        with self._lock:
            self._require_open()
            res, err = self._invoke(lambda a: a.ReadShortStatus())
        if err is not None:
            return {"ok": False, "status": None, "error": err}
        return {"ok": True, "status": _ser(res[1]), "error": None}


if __name__ == "__main__":
    # Bench check: python3 dlpc.py [--init]  — prints JSON to stdout.
    import json
    d = Dlpc()
    d.open()
    try:
        if "--init" in sys.argv[1:]:
            print(json.dumps(d.init_parallel(), indent=1))
        print(json.dumps(d.sweep(), indent=1))
    finally:
        d.close()
