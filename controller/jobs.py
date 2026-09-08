"""
controller/jobs.py

Background jobs for the Data tab (sync / sync-and-poweroff / purge): one job runs at a time,
in its own thread; inside a job the rigs run in parallel. The page polls /api/data/jobs/<id>
for progress, the log and the result. Cancel sets a flag the job checks between steps and
terminates the rsync that is running right now.
"""
from __future__ import annotations
import collections
import threading
import time
import traceback
import uuid


class Job:
    def __init__(self, kind: str, rigs: list, params: dict | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.rigs = list(rigs)
        self.params = params or {}
        self.status = "queued"          # queued | running | done | failed | cancelled
        self.created = time.time()
        self.started = None
        self.finished = None
        self.error = None
        self.cancel = threading.Event()
        self.progress = {r: {"state": "queued", "pct": 0, "bytes_done": 0, "bytes_total": 0,
                             "current": None, "rate": None, "eta_s": None} for r in self.rigs}
        self.result = {r: {"synced": [], "failed": [], "purged": [], "poweroff": None} for r in self.rigs}
        self.log = collections.deque(maxlen=2000)
        self.procs = {}                 # rig -> running subprocess (for cancel)
        self._lock = threading.Lock()

    def say(self, rig, msg, level="info"):
        with self._lock:
            self.log.append({"t": time.time(), "rig": rig, "level": level, "msg": str(msg)})
        print(f"[job {self.id} {self.kind}] {rig or '-'}: {msg}", flush=True)

    def set_progress(self, rig, **kw):
        with self._lock:
            self.progress.setdefault(rig, {}).update(kw)

    @property
    def overall_pct(self) -> float:
        tot = sum(p.get("bytes_total") or 0 for p in self.progress.values())
        if tot <= 0:
            done = [p for p in self.progress.values() if p.get("state") in ("done", "failed", "cancelled", "skipped")]
            return round(100.0 * len(done) / max(1, len(self.progress)), 1)
        got = sum(min(p.get("bytes_done") or 0, p.get("bytes_total") or 0) for p in self.progress.values())
        return round(100.0 * got / tot, 1)

    def to_dict(self, log_tail: int = 300) -> dict:
        with self._lock:
            log = list(self.log)[-log_tail:]
            return {
                "id": self.id, "kind": self.kind, "rigs": self.rigs, "params": self.params,
                "status": self.status, "created": self.created, "started": self.started,
                "finished": self.finished, "error": self.error, "cancelled": self.cancel.is_set(),
                "progress": {r: dict(p) for r, p in self.progress.items()},
                "overall_pct": self.overall_pct, "result": self.result, "log": log,
            }


class JobRunner:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self._lock = threading.Lock()
        self._current: Job | None = None
        self._queue: collections.deque = collections.deque()
        self._worker = None

    def submit(self, job: Job, fn) -> Job:
        """fn(job) does the work; runs after any job already queued."""
        with self._lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            self._queue.append((job, fn))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="data-jobs", daemon=True)
                self._worker.start()
        return job

    def _loop(self):
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                job, fn = self._queue.popleft()
                self._current = job
            job.status = "running"
            job.started = time.time()
            try:
                if job.cancel.is_set():
                    job.status = "cancelled"
                else:
                    fn(job)
                    job.status = "cancelled" if job.cancel.is_set() else "done"
            except Exception as e:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                job.say(None, "".join(traceback.format_exception(e))[-1500:], "error")
            finally:
                job.finished = time.time()
                with self._lock:
                    self._current = None

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def current(self) -> Job | None:
        return self._current

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancel.set()
        for rig, p in list(job.procs.items()):
            try:
                p.terminate()
                job.say(rig, "cancelled: terminated the running copy", "warning")
            except Exception:
                pass
        return True

    def recent(self, n: int = 20) -> list:
        return [self.jobs[j].to_dict(log_tail=0) for j in self.order[-n:]]


runner = JobRunner()
