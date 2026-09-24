"""
controller/registry.py

One RigState per loaded rig — everything app/app.py used to keep in module globals (phase,
session, trials, the teardown lock, the Slack webhook, the SSE queue) — and the RigRegistry
that owns them and maps sender IPs back to rigs for the UDP demux.
"""
from __future__ import annotations
import ipaddress
import logging
import queue
import threading
import time
from pathlib import Path

from shared.config import load_rig, get_leader_pi, get_follower_pis
from shared.notify import notify as _notify
from controller import settings

PHASES = ("setup", "connected", "deployed", "running", "ended", "offline")
MAX_SUBSCRIBERS = 8          # SSE listeners per rig (browser tabs); the oldest is evicted past this
SUBSCRIBER_QUEUE = 500


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return ip in ("localhost",)


def _rig_logger(name: str) -> logging.Logger:
    """logs/<rig>/app.log — deploy / go / stop lines for this rig only."""
    log_dir = settings.ROOT / "logs" / name
    log_dir.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger(f"vrfarm.rig.{name}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        h = logging.FileHandler(log_dir / "app.log")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(h)
    return lg


class RigState:
    """Everything the controller knows about one loaded rig."""

    def __init__(self, name: str, path: Path, config: dict):
        self.name = name
        self.path = Path(path)
        self.config = config
        self.loaded_at = time.time()

        # experiment state (was app/app.py `state`)
        self.phase = "setup"
        self.deployed = False
        self.task_config: dict | None = None
        self.task_path: str | None = None
        self.session: dict = {}
        self.session_id: str | None = None
        self.session_dir: str | None = None
        self.camera_override: dict = {}
        self.trial_table: list = []

        # live-session bookkeeping (was module globals)
        self.trials: list = []
        self.session_end_seen = False
        self.rt_hits_path: str | None = None
        self.display_notify_t: dict[str, float] = {}
        self.last_event_t: float | None = None

        # concurrency
        self.teardown_lock = threading.Lock()
        self._busy = threading.Lock()
        self.busy_kind: str | None = None
        self._subs: list[queue.Queue] = []
        self._sub_lock = threading.Lock()

        # shared with the other tabs
        self.pi_status: dict = {}
        self.data: dict = {"inventory": None, "inventory_t": None, "status": {},
                           "last_sync_t": None, "disk_free_gb": None, "engine_running": None}

        self.slack_webhook: str | None = None
        self.resolve_slack()
        self.logger = _rig_logger(name)

    # ── config-derived ──

    def resolve_slack(self) -> None:
        """Per-rig webhook from the rig's `slack:` block. '' = explicitly off, None = fall back to
        the VRFARM_SLACK_WEBHOOK env var, else the URL. Same rules app/app.py applied globally."""
        slack = (self.config or {}).get("slack") or {}
        url = (slack.get("webhook_url") or "").strip()
        if not slack.get("enabled"):
            self.slack_webhook = ""
        elif url:
            self.slack_webhook = url
        else:
            self.slack_webhook = None

    def notify(self, text: str) -> None:
        _notify(text, webhook=self.slack_webhook)

    @property
    def pis(self) -> list:
        return list((self.config or {}).get("pis") or [])

    @property
    def api_port(self) -> int:
        return int((self.config.get("network") or {}).get("api_port", 5080))

    def leader(self) -> dict:
        return get_leader_pi(self.config)

    def followers(self) -> list:
        return get_follower_pis(self.config)

    def pi_ips(self) -> set:
        return {pi.get("ip") for pi in self.pis if pi.get("ip")}

    # ── SSE fan-out ──

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE)
        with self._sub_lock:
            while len(self._subs) >= MAX_SUBSCRIBERS:
                self._subs.pop(0)
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def broadcast(self, event: dict) -> None:
        with self._sub_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    def drain(self) -> int:
        """Empty every subscriber queue (Live restarts fresh). Returns the number dropped."""
        n = 0
        with self._sub_lock:
            subs = list(self._subs)
        for q in subs:
            while True:
                try:
                    q.get_nowait()
                    n += 1
                except queue.Empty:
                    break
        return n

    # ── busy lock (deploy / go / sync / purge / poweroff) ──

    def try_busy(self, kind: str) -> bool:
        if self._busy.acquire(blocking=False):
            self.busy_kind = kind
            return True
        return False

    def release_busy(self) -> None:
        self.busy_kind = None
        try:
            self._busy.release()
        except RuntimeError:
            pass

    # ── views ──

    def snapshot(self) -> dict:
        hr, med = session_metrics(self, self.trials)
        return {
            "name": self.name,
            "phase": self.phase,
            "deployed": self.deployed,
            "session_id": self.session_id,
            "n_trials": len(self.trials),
            "n_planned": len(self.trial_table) if self.trial_table else None,
            "hit_rate": hr,
            "median_rt_ms": med,
            "task": Path(self.task_path).stem if self.task_path else None,
            "subject": (self.session or {}).get("subject_id"),
            "busy": self.busy_kind,
            "last_event_t": self.last_event_t,
            "loaded_at": self.loaded_at,
            "slack": ("off" if self.slack_webhook == "" else
                      "rig" if self.slack_webhook else "env"),
        }


# ── session metrics (shared by the demux, Slack text and the fleet badges) ──

def event_is_go(rs: RigState, event: dict) -> bool:
    """Whether a trial event is a GO trial (should lick), per the task go_rule + stim azimuth.
    Mirrors the engine (_classify_go) and the UI (_isGo): all->go, right->az>0, else az<0."""
    rule = ((rs.task_config or {}).get("stimulus") or {}).get("go_rule", "left")
    if rule == "all":
        return True
    az = event.get("stim_az", 0) or 0
    return az > 0 if rule == "right" else az < 0


def session_metrics(rs: RigState, trials: list):
    """(hit_rate, median_rt_ms) matching the live plots: Cumulative HR = go hits / go trials;
    median RT = median of go-hit RTs (ms). Either is None when undefined."""
    go = [t for t in trials if event_is_go(rs, t)]
    hits = [t for t in go if t.get("outcome") == "hit"]
    hit_rate = (len(hits) / len(go)) if go else None
    rts = sorted(t["rt_ms"] for t in hits
                 if isinstance(t.get("rt_ms"), (int, float)) and not isinstance(t.get("rt_ms"), bool)
                 and t["rt_ms"] == t["rt_ms"])
    if rts:
        n = len(rts)
        med = rts[n // 2] if n % 2 else (rts[n // 2 - 1] + rts[n // 2]) / 2
    else:
        med = None
    return hit_rate, med


def metrics_suffix(rs: RigState, trials: list) -> str:
    """', median RT=N ms and hit rate=0.xx' — appended to the end / global-timeout messages."""
    hr, med = session_metrics(rs, trials)
    med_str = f"{round(med)} ms" if med is not None else "n/a"
    hr_str = f"{hr:.2f}" if hr is not None else "n/a"
    return f", median RT={med_str} and hit rate={hr_str}"


# ── registry ──

class RigRegistry:
    def __init__(self):
        self.rigs: dict[str, RigState] = {}
        self._lock = threading.RLock()
        self.ip_map: dict[str, set] = {}        # ip -> {rig names}; loopback maps to many
        self.unknown_senders: dict[str, dict] = {}

    # files
    def rig_path(self, name: str) -> Path:
        return settings.rigs_dir() / f"{name}.yaml"

    def list_rig_files(self) -> list[str]:
        d = settings.rigs_dir()
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.yaml") if not p.stem.startswith(("_", ".")))

    # lifecycle
    def load(self, name: str) -> RigState:
        """Load (or re-read) rigs/<name>.yaml into the registry. A running rig is never re-read."""
        path = self.rig_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Rig config not found: {path}")
        cfg = load_rig(path)
        with self._lock:
            rs = self.rigs.get(name)
            if rs is not None:
                if rs.phase == "running":
                    raise RuntimeError(f"rig '{name}' is running a session")
                rs.config = cfg
                rs.path = path
                rs.resolve_slack()
            else:
                self._check_ip_clash(name, cfg)
                rs = RigState(name, path, cfg)
                self.rigs[name] = rs
            self._rebuild_ip_map()
            return rs

    def reload_config(self, name: str) -> RigState:
        return self.load(name)

    def unload(self, name: str) -> None:
        with self._lock:
            rs = self.rigs.get(name)
            if rs is None:
                return
            if rs.phase == "running":
                raise RuntimeError(f"rig '{name}' is running a session")
            if rs.busy_kind:
                raise RuntimeError(f"rig '{name}' is busy ({rs.busy_kind})")
            del self.rigs[name]
            self._rebuild_ip_map()

    def get(self, name: str) -> RigState:
        rs = self.rigs.get(name)
        if rs is None:
            raise KeyError(name)
        return rs

    def _check_ip_clash(self, name: str, cfg: dict) -> None:
        try:
            lip = get_leader_pi(cfg).get("ip")
        except Exception:
            lip = None
        if not lip or _is_loopback(lip):
            return
        for other in self.rigs.values():
            if other.name == name:
                continue
            try:
                if get_leader_pi(other.config).get("ip") == lip:
                    raise RuntimeError(f"leader IP {lip} already belongs to loaded rig '{other.name}'")
            except (KeyError, ValueError):
                continue

    def _rebuild_ip_map(self) -> None:
        m: dict[str, set] = {}
        for rs in self.rigs.values():
            for ip in rs.pi_ips():
                m.setdefault(ip, set()).add(rs.name)
        self.ip_map = m

    # demux support
    def by_ip(self, ip: str) -> RigState | None:
        """The rig a datagram from `ip` belongs to, when that is unambiguous."""
        names = self.ip_map.get(ip)
        if names and len(names) == 1:
            return self.rigs.get(next(iter(names)))
        return None

    def note_unknown(self, ip: str, event: dict) -> None:
        e = self.unknown_senders.setdefault(ip, {"count": 0, "last_t": None, "sample": None})
        e["count"] += 1
        e["last_t"] = time.time()
        if e["sample"] is None:
            e["sample"] = {k: event.get(k) for k in ("type", "rig", "host") if k in event}

    # cross-rig guards
    def subject_in_use(self, subject: str, exclude: str | None = None) -> str | None:
        for rs in self.rigs.values():
            if rs.name == exclude:
                continue
            if rs.phase == "running" and (rs.session or {}).get("subject_id") == subject:
                return rs.name
        return None

    def any_running(self) -> list[str]:
        return [n for n, rs in self.rigs.items() if rs.phase == "running"]

    def snapshot_all(self) -> dict:
        return {n: rs.snapshot() for n, rs in self.rigs.items()}


registry = RigRegistry()
