#!/usr/bin/env python3
"""
shepherd/shepherd.py

shepherd — a lightweight, out-of-process health monitor for a VRFarm rig Pi.

WHY A SEPARATE PROCESS. The whole point is to watch the things that can silently
ruin a session — the Pi 5 software-encodes H.264 with no hardware encoder and no
fan, so a long run can thermally throttle (the encoder then slows and DROPS frames,
corrupting the frame timing the whole pipeline exists to preserve), the disk can
fill, or pi_api itself can wedge mid-session. A monitor that lived *inside* pi_api
would freeze at exactly the moment you most want its data. shepherd runs in its own
process with its own memory and its own scheduler slot, so a stall in pi_api (or a
crash) cannot take the watchdog down with it — and shepherd's API probe is what
*detects* that stall.

WHAT IT IS NOT. It does not stream telemetry anywhere, it does not touch the HDF5,
and it does not do anything heavy. Each cycle is a handful of /proc and /sys reads
(microseconds, non-blocking) plus one short-timeout HTTP probe of the local API.
The sampling cost is far below anything it watches.

OUTPUTS.
  1. A metrics log — one JSON object per sample — under <output_dir> (a plain,
     separate file; nothing goes into the session .h5).
  2. Alerts. Two levels, WARNING and CRITICAL, per the thresholds in config.yaml.
     Alerts are EDGE-TRIGGERED (fired on a level change, not every sample, so the
     log isn't spammed) and delivered two ways: appended to a local alerts log, and
     sent as a UDP `shepherd_alert` event to the controller so they surface in the
     experiment-UI log — orange for warning, red for critical.

DEPENDENCIES. stdlib + PyYAML only. The API probe uses urllib (stdlib), not
requests, to keep shepherd's footprint minimal.

RUN.  python3 shepherd/shepherd.py --config shepherd/config.yaml
      (or install shepherd.service to run it continuously — see README.md)
"""

from __future__ import annotations
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml


# ─────────────────────────────────────────────────────────────────────────────
# Samplers.  Each returns a plain value (or None when the source isn't available
# on this machine — e.g. no vcgencmd off a Pi). None NEVER raises and NEVER alerts;
# an absent metric is simply absent. Deltas (CPU %, disk write rate) need the
# previous reading, so those samplers are methods on Monitor that hold state.
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def sample_soc_temp_c() -> float | None:
    """Hottest thermal zone in °C. On a Pi the SoC zone reads here; the value that
    matters for throttling is the maximum across zones."""
    best = None
    tz = Path("/sys/class/thermal")
    if not tz.exists():
        return None
    for zone in tz.glob("thermal_zone*/temp"):
        raw = _read(str(zone))
        if raw and raw.strip().lstrip("-").isdigit():
            c = int(raw.strip()) / 1000.0     # millidegrees -> °C
            best = c if best is None else max(best, c)
    return best


def sample_mem() -> tuple[float | None, float | None]:
    """(% used, available MB) from /proc/meminfo. 'used' is defined against
    MemAvailable (what apps can actually get), not the misleading free/total."""
    txt = _read("/proc/meminfo")
    if not txt:
        return None, None
    kv = {}
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            kv[parts[0][:-1]] = int(parts[1])   # kB
    total = kv.get("MemTotal")
    avail = kv.get("MemAvailable")
    if not total or avail is None:
        return None, None
    pct = 100.0 * (total - avail) / total
    return pct, avail / 1024.0


def sample_load1() -> float | None:
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def _is_partition(dev: str) -> bool:
    """True for a partition (nvme0n1p2, mmcblk0p1, sda1) vs a whole disk
    (nvme0n1, mmcblk0, sda). nvme/mmcblk partitions carry a 'p' before the number."""
    import re
    return bool(re.search(r"(p\d+|(?<=sd[a-z])\d+)$", dev))


def decode_throttled(hex_str: str) -> dict:
    """Decode the bitmask from `vcgencmd get_throttled`. 'now' bits are the live
    state (what to alert on); the *_since_boot bits are sticky history."""
    v = int(hex_str, 16)
    return {
        "under_voltage_now":     bool(v & (1 << 0)),
        "arm_freq_capped_now":   bool(v & (1 << 1)),
        "throttled_now":         bool(v & (1 << 2)),
        "soft_temp_limit_now":   bool(v & (1 << 3)),
        "under_voltage_since":   bool(v & (1 << 16)),
        "throttled_since":       bool(v & (1 << 18)),
        "soft_temp_limit_since": bool(v & (1 << 19)),
        "raw": hex_str,
    }


def sample_throttled() -> dict | None:
    """vcgencmd is a subprocess fork, so callers rate-limit this (see Monitor).
    Absent off a Pi -> None."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"],
                             capture_output=True, text=True, timeout=2)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or "throttled=" not in out.stdout:
        return None
    return decode_throttled(out.stdout.strip().split("throttled=", 1)[1])


class Monitor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        sh = cfg.get("shepherd", {})
        self.interval = float(sh.get("interval_s", 1.0))
        self.mount = sh.get("disk_mount") or str(Path.home())
        self.disk_devs = tuple(sh.get("disk_devices", ["nvme", "mmcblk", "sd"]))
        # vcgencmd is a fork; sample throttle state every Nth cycle, not every cycle.
        self.throttle_every = max(1, int(sh.get("throttle_sample_every", 5)))
        # API probe
        ap = sh.get("api_probe", {})
        self.api_enabled = bool(ap.get("enabled", True))
        self.api_url = ap.get("url", "http://127.0.0.1:5080/api/status")
        self.api_timeout = float(ap.get("timeout_s", 2.0))
        # delta state
        self._prev_cpu = None            # (idle, total) aggregate
        self._prev_cores = None          # list of (idle, total)
        self._prev_disk = None           # (sectors_written, monotonic_t)
        self._prev_frames = None         # (camera_frames, monotonic_t)
        self._cycle = 0
        self._last_throttled = None

    # ---- CPU from /proc/stat ----
    @staticmethod
    def _parse_stat():
        txt = _read("/proc/stat")
        agg = None
        cores = []
        if not txt:
            return agg, cores
        for line in txt.splitlines():
            if not line.startswith("cpu"):
                continue
            f = line.split()
            if len(f) < 5:
                continue
            nums = list(map(int, f[1:]))
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)   # idle + iowait
            total = sum(nums)
            if f[0] == "cpu":
                agg = (idle, total)
            else:
                cores.append((idle, total))
        return agg, cores

    def sample_cpu(self):
        agg, cores = self._parse_stat()
        pct = None
        per_core = None
        if agg and self._prev_cpu:
            di = agg[0] - self._prev_cpu[0]
            dt = agg[1] - self._prev_cpu[1]
            if dt > 0:
                pct = 100.0 * (1.0 - di / dt)
        if cores and self._prev_cores and len(cores) == len(self._prev_cores):
            per_core = []
            for (i, t), (pi, pt) in zip(cores, self._prev_cores):
                d = t - pt
                per_core.append(round(100.0 * (1.0 - (i - pi) / d), 1) if d > 0 else None)
        self._prev_cpu = agg
        self._prev_cores = cores
        return pct, per_core

    # ---- disk ----
    def sample_disk(self):
        import shutil
        used_pct = free_gb = None
        try:
            du = shutil.disk_usage(self.mount)
            used_pct = 100.0 * du.used / du.total
            free_gb = du.free / 1e9
        except OSError:
            pass
        # write rate from /proc/diskstats (field 10 = sectors written, 512 B each).
        # Sum WHOLE-disk devices only; skipping partitions avoids double-counting the
        # same writes (a partition row and its parent disk row both report them).
        write_mbs = None
        txt = _read("/proc/diskstats")
        now = time.monotonic()
        if txt:
            sectors = 0
            for line in txt.splitlines():
                f = line.split()
                if len(f) < 10 or not f[2].startswith(self.disk_devs):
                    continue
                if _is_partition(f[2]):
                    continue
                sectors += int(f[9])
            if self._prev_disk:
                ds = sectors - self._prev_disk[0]
                dt = now - self._prev_disk[1]
                if dt > 0 and ds >= 0:
                    write_mbs = ds * 512 / 1e6 / dt
            self._prev_disk = (sectors, now)
        return used_pct, free_gb, write_mbs

    # ---- API probe ----
    def sample_api(self):
        if not self.api_enabled:
            return {"enabled": False}
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(self.api_url, timeout=self.api_timeout) as r:
                body = json.loads(r.read().decode())
            latency_ms = (time.monotonic() - t0) * 1000
            info = {"ok": True, "latency_ms": round(latency_ms, 1),
                    "process_running": bool(body.get("process_running")),
                    "camera_recording": bool(body.get("camera_recording"))}
            # live encode fps from the camera frame counter, if the API exposes it
            frames = body.get("camera_frames")
            now = time.monotonic()
            if isinstance(frames, int):
                if self._prev_frames:
                    df = frames - self._prev_frames[0]
                    dt = now - self._prev_frames[1]
                    if dt > 0 and df >= 0:
                        info["camera_fps"] = round(df / dt, 2)
                self._prev_frames = (frames, now)
            return info
        except Exception as e:
            return {"ok": False, "error": str(e)[:80]}

    def sample(self) -> dict:
        self._cycle += 1
        cpu_pct, per_core = self.sample_cpu()
        mem_pct, mem_avail = sample_mem()
        used_pct, free_gb, write_mbs = self.sample_disk()
        temp = sample_soc_temp_c()          # once — the samplers touch the filesystem
        load1 = sample_load1()
        m = {
            "t": time.time(),
            "cpu_percent": round(cpu_pct, 1) if cpu_pct is not None else None,
            "cpu_per_core": per_core,
            "load1": round(load1, 2) if load1 is not None else None,
            "mem_percent": round(mem_pct, 1) if mem_pct is not None else None,
            "mem_available_mb": round(mem_avail) if mem_avail is not None else None,
            "soc_temp_c": round(temp, 1) if temp is not None else None,
            "disk_used_percent": round(used_pct, 1) if used_pct is not None else None,
            "disk_free_gb": round(free_gb, 1) if free_gb is not None else None,
            "disk_write_mbs": round(write_mbs, 1) if write_mbs is not None else None,
        }
        # throttle state, rate-limited (fork cost)
        if self._cycle % self.throttle_every == 1 or self._last_throttled is None:
            t = sample_throttled()
            if t is not None:
                self._last_throttled = t
        if self._last_throttled is not None:
            m["throttled"] = self._last_throttled
        m["api"] = self.sample_api()
        return m


# ─────────────────────────────────────────────────────────────────────────────
# Threshold engine. Config drives which metrics are watched, the warn/critical
# levels, the compare direction, and the message text. Alerts are edge-triggered:
# an alert fires when a metric CHANGES level (ok->warn, ok/warn->critical, and back
# to ok as a recovery). A sustained critical re-alerts every realert_critical_s so
# it can't be lost if the operator missed the first one.
# ─────────────────────────────────────────────────────────────────────────────

LEVEL_ORDER = {"ok": 0, "warning": 1, "critical": 2}


class ThresholdEngine:
    def __init__(self, cfg: dict):
        self.metrics = cfg.get("metrics", {})
        self.realert = float(cfg.get("shepherd", {}).get("realert_critical_s", 30))
        self._state = {}         # metric -> {"level":..., "since": t}

    @staticmethod
    def _level_for(value, spec) -> str:
        """ok / warning / critical for a numeric value, per direction + thresholds."""
        direction = spec.get("direction", "high")
        warn = spec.get("warn")
        crit = spec.get("critical")
        if value is None:
            return "ok"
        if direction == "high":
            if crit is not None and value >= crit:
                return "critical"
            if warn is not None and value >= warn:
                return "warning"
        else:  # "low"
            if crit is not None and value <= crit:
                return "critical"
            if warn is not None and value <= warn:
                return "warning"
        return "ok"

    def evaluate(self, metric_key, value, level_override=None, ctx=None):
        """Return an alert dict if this reading should fire one, else None.
        level_override lets special cases (throttled bits, API down) set the level
        directly instead of via numeric thresholds."""
        spec = self.metrics.get(metric_key)
        if not spec or not spec.get("enabled", True):
            return None
        level = level_override or self._level_for(value, spec)
        prev = self._state.get(metric_key, {"level": "ok", "since": 0.0})
        now = time.time()
        changed = level != prev["level"]
        # re-alert a still-critical condition periodically
        restate = (level == "critical" and not changed
                   and now - prev["since"] >= self.realert)
        if not changed and not restate:
            return None
        self._state[metric_key] = {"level": level, "since": now if changed else prev["since"]}
        if level == "ok":
            # recovery notice (only worth sending if we were previously alerting)
            if prev["level"] == "ok":
                return None
            msg = spec.get("recover_msg", f"{metric_key} back to normal")
            return self._alert(metric_key, "ok", value, spec, msg, ctx)
        key = "critical_msg" if level == "critical" else "warn_msg"
        template = spec.get(key, f"{metric_key} {level}: {{value}}")
        return self._alert(metric_key, level, value, spec, template, ctx)

    @staticmethod
    def _alert(metric_key, level, value, spec, template, ctx):
        fmt = {"value": value, "warn": spec.get("warn"),
               "critical": spec.get("critical"), "unit": spec.get("unit", "")}
        if ctx:
            fmt.update(ctx)
        try:
            message = template.format(**fmt)
        except (KeyError, ValueError, IndexError):
            message = template   # a bad placeholder must never crash the monitor
        return {"metric": metric_key, "level": level, "value": value, "message": message}


# ─────────────────────────────────────────────────────────────────────────────
# Alert delivery: local file + UDP to the controller (so it lands in the UI log).
# ─────────────────────────────────────────────────────────────────────────────

class AlertSink:
    def __init__(self, cfg: dict, alerts_path: Path):
        sh = cfg.get("shepherd", {})
        udp = sh.get("alerts", {}).get("udp", {})
        self.udp_enabled = bool(udp.get("enabled", True))
        self.udp_dest = (udp.get("host", "192.168.10.1"), int(udp.get("port", 5571)))
        self.host = socket.gethostname()
        self.alerts_path = alerts_path
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if self.udp_enabled else None

    def send(self, alert: dict):
        line = (f"{datetime.now().isoformat(timespec='seconds')} "
                f"[{alert['level'].upper():8}] {alert['metric']}: {alert['message']}")
        # local, human-readable
        try:
            with open(self.alerts_path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print(line, flush=True)
        # UDP -> controller event port -> experiment UI log (best-effort; never blocks)
        if self._sock is not None:
            payload = {"type": "shepherd_alert", "host": self.host, "t": time.time(), **alert}
            try:
                self._sock.sendto(json.dumps(payload).encode(), self.udp_dest)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────

def evaluate_all(engine: ThresholdEngine, sink: AlertSink, m: dict, mount: str = ""):
    """Run every configured numeric metric plus the special cases through the engine."""
    ctx = {"mount": mount}
    for key in ("cpu_percent", "mem_percent", "soc_temp_c",
                "disk_used_percent", "disk_write_mbs"):
        if key in m and m[key] is not None:
            a = engine.evaluate(key, m[key], ctx=ctx)
            if a:
                sink.send(a)
    # disk_free_gb is a 'low' alarm — value falls toward the threshold
    if m.get("disk_free_gb") is not None:
        a = engine.evaluate("disk_free_gb", m["disk_free_gb"], ctx=ctx)
        if a:
            sink.send(a)
    # camera encode fps: only meaningful while recording; 'low' direction
    api = m.get("api", {})
    if api.get("camera_recording") and api.get("camera_fps") is not None:
        a = engine.evaluate("camera_fps", api["camera_fps"], ctx=ctx)
        if a:
            sink.send(a)
    # ---- special cases (level set directly, not by numeric threshold) ----
    # thermal / voltage throttling reported by the SoC itself
    thr = m.get("throttled")
    if thr:
        if thr.get("throttled_now") or thr.get("under_voltage_now"):
            a = engine.evaluate("throttled", thr["raw"], level_override="critical")
            if a:
                sink.send(a)
        elif thr.get("soft_temp_limit_now"):
            a = engine.evaluate("throttled", thr["raw"], level_override="warning")
            if a:
                sink.send(a)
        else:
            a = engine.evaluate("throttled", thr["raw"], level_override="ok")
            if a:
                sink.send(a)
    # API unresponsive — the wedge shepherd exists to catch. Only critical when a
    # session is thought to be live (process was running on the last good probe).
    if api.get("enabled", True):
        if api.get("ok"):
            a = engine.evaluate("api_health", 1, level_override="ok")
            if a:
                sink.send(a)
        else:
            a = engine.evaluate("api_health", 0, level_override="critical",
                                 ctx={"error": api.get("error", "no response")})
            if a:
                sink.send(a)


def main():
    ap = argparse.ArgumentParser(description="shepherd — VRFarm rig health monitor")
    default_cfg = Path(__file__).with_name("config.yaml")
    ap.add_argument("--config", default=str(default_cfg))
    ap.add_argument("--once", action="store_true", help="sample once and exit (for testing)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    sh = cfg.get("shepherd", {})
    out_dir = Path(os.path.expanduser(sh.get("output_dir", "~/shepherd_logs")))
    out_dir.mkdir(parents=True, exist_ok=True)
    # deterministic-ish run name from wall clock (avoids Date.now-style nondeterminism concerns;
    # shepherd is a long-lived process, one file per launch is what you want)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    metrics_path = out_dir / f"shepherd_{stamp}.jsonl"
    alerts_path = out_dir / f"shepherd_{stamp}.alerts.log"

    monitor = Monitor(cfg)
    engine = ThresholdEngine(cfg)
    sink = AlertSink(cfg, alerts_path)

    print(f"[shepherd] host={socket.gethostname()} interval={monitor.interval}s "
          f"metrics={metrics_path}", flush=True)

    running = {"go": True}

    def _stop(*_):
        running["go"] = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # prime the delta samplers so the first real sample has valid CPU/disk rates
    monitor.sample()
    time.sleep(min(monitor.interval, 1.0))

    with open(metrics_path, "a") as mf:
        while running["go"]:
            t0 = time.monotonic()
            try:
                m = monitor.sample()
                mf.write(json.dumps(m) + "\n")
                mf.flush()
                evaluate_all(engine, sink, m, mount=monitor.mount)
            except Exception as e:            # a bad sample must never kill the watchdog
                print(f"[shepherd] sample error: {e}", flush=True)
            if args.once:
                print(json.dumps(m, indent=1))
                break
            dt = time.monotonic() - t0
            if dt < monitor.interval:
                time.sleep(monitor.interval - dt)
    print("[shepherd] stopped", flush=True)


if __name__ == "__main__":
    main()
