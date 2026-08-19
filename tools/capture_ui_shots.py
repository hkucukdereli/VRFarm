#!/usr/bin/env python
"""
tools/capture_ui_shots.py — regenerate the UI screenshots used by docs/SETUP_UI.md
and docs/EXPERIMENT_UI.md, with no rig hardware attached.

It starts everything it needs (mock Pi + both Flask UIs) on loopback, drives the
pages with Playwright, writes PNGs to docs/images/, and tears the processes down.
(docs/assets/ is the gitignored local stash for audits and design notes — not this.)
Because it uses the `demo` rig (both Pis at 127.0.0.1) it can never reach the real
rig — a stray Deploy or Install in a captured state hits the mock, not a Pi.

    pip install playwright && playwright install chromium
    python tools/capture_ui_shots.py                  # all shots
    python tools/capture_ui_shots.py --only exp-05    # one shot (prefix match)
    python tools/capture_ui_shots.py --keep-open      # leave servers up to poke at

Shots that need real hardware (a live camera frame, the projector, the geometry
calibration popup) cannot come from here — they are listed as TODO in the docs and
have to be taken on the rig. Everything else regenerates from this script.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "images"

SETUP_PORT = 4999
EXP_PORT = 5055           # not 5000: macOS AirPlay Receiver squats on it
MOCK_API_PORT = 5080

# A short, dense session: enough trials to fill the rasters and plots quickly.
MOCK_ENV = {"MOCK_N": "14", "MOCK_ITI": "1.2", "MOCK_TRIAL_S": "0.8", "MOCK_END": "end"}

VIEWPORT = {"width": 1600, "height": 1080}
SCALE = 2                 # retina — crisp text in the docs

# The task the experiment walkthrough loads and deploys. Deploy makes the UI re-save the
# task YAML, and that save round-trips through yaml.dump — which silently strips every
# comment and reflows the inline lists. So we snapshot the file first and put it back.
DOC_TASK = "go_nogo_v1"


# ── process plumbing ──

def _wait_port(port: int, timeout: float = 25.0, host: str = "127.0.0.1") -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.25)
    return False


def _spawn(name: str, args: list[str], env_extra: dict | None = None) -> subprocess.Popen:
    # setup/app.py opens a browser tab 1.5 s after boot and has no --no-browser flag;
    # webbrowser honours $BROWSER, so point it at a no-op instead of Chrome.
    # (Do NOT set WERKZEUG_RUN_MAIN here — werkzeug then expects an inherited socket fd
    # from a reloader parent that doesn't exist, and dies on KeyError WERKZEUG_SERVER_FD.)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "BROWSER": "echo",
           **(env_extra or {})}
    env.pop("VRFARM_SLACK_WEBHOOK", None)      # never notify a real channel from a doc run
    log = open(ROOT / "logs" / f"capture_{name}.log", "w")
    p = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    print(f"  started {name} (pid {p.pid})")
    return p


def start_servers() -> list[subprocess.Popen]:
    (ROOT / "logs").mkdir(exist_ok=True)
    procs = [
        _spawn("mock_pi", ["tools/mock_pi.py"], MOCK_ENV),
        _spawn("setup_ui", ["setup/app.py"]),
        _spawn("exp_ui", ["app/app.py", "--port", str(EXP_PORT), "--no-browser"]),
    ]
    for port, what in ((MOCK_API_PORT, "mock pi_api"), (SETUP_PORT, "setup UI"), (EXP_PORT, "experiment UI")):
        if not _wait_port(port):
            stop_servers(procs)
            raise SystemExit(f"{what} never came up on :{port} — see logs/capture_*.log")
    return procs


def stop_servers(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


# ── screenshot helpers ──

class Shots:
    """Writes PNGs, honouring a --only prefix filter, and reports what it skipped."""

    def __init__(self, only: str | None):
        self.only = only
        self.written: list[str] = []

    def want(self, name: str) -> bool:
        return self.only is None or name.startswith(self.only)

    def page(self, page, name: str, full: bool = False) -> None:
        if not self.want(name):
            return
        path = ASSETS / f"{name}.png"
        page.screenshot(path=str(path), full_page=full)
        self._done(name, path)

    def element(self, page, selector: str, name: str, padding: int = 8) -> None:
        """Screenshot one element plus a little breathing room around it."""
        if not self.want(name):
            return
        el = page.locator(selector).first
        el.scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        box = el.bounding_box()
        if not box:
            print(f"  !! {name}: selector {selector} has no box — skipped")
            return
        clip = {"x": max(0, box["x"] - padding), "y": max(0, box["y"] - padding),
                "width": box["width"] + 2 * padding, "height": box["height"] + 2 * padding}
        path = ASSETS / f"{name}.png"
        page.screenshot(path=str(path), clip=clip)
        self._done(name, path)

    def _done(self, name: str, path: Path) -> None:
        self.written.append(name)
        print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")


ANNOTATE_JS = """
(items) => {
  document.querySelectorAll('.doc-badge').forEach(e => e.remove());
  items.forEach(([sel, label]) => {
    const el = document.querySelector(sel);
    if (!el) { console.warn('badge: no match for', sel); return; }
    const r = el.getBoundingClientRect();
    const b = document.createElement('div');
    b.className = 'doc-badge';
    b.textContent = label;
    Object.assign(b.style, {
      position: 'absolute', zIndex: 99999,
      left: (r.left + window.scrollX - 11) + 'px',
      top:  (r.top + window.scrollY + r.height / 2 - 11) + 'px',
      width: '22px', height: '22px', borderRadius: '50%',
      background: '#ff5c8a', color: '#fff',
      font: '700 13px/22px ui-sans-serif, system-ui, sans-serif',
      textAlign: 'center', boxShadow: '0 0 0 2px #fff, 0 2px 6px rgba(0,0,0,.5)',
      pointerEvents: 'none',
    });
    document.body.appendChild(b);
  });
}
"""


def annotate(page, items: list[tuple[str, str]]) -> None:
    """Pin numbered badges on elements so a figure can be walked through in prose."""
    page.evaluate(ANNOTATE_JS, items)
    page.wait_for_timeout(120)


def clear_badges(page) -> None:
    page.evaluate("document.querySelectorAll('.doc-badge').forEach(e => e.remove())")


class TaskFileGuard:
    """Restore experiments/<task>.yaml after the run — Deploy rewrites it without comments."""

    def __init__(self, task: str):
        self.path = ROOT / "experiments" / f"{task}.yaml"
        self.original: bytes | None = None

    def __enter__(self):
        if self.path.exists():
            self.original = self.path.read_bytes()
        return self

    def __exit__(self, *exc):
        if self.original is not None and self.path.read_bytes() != self.original:
            self.path.write_bytes(self.original)
            print(f"  restored {self.path.relative_to(ROOT)} (Deploy had rewritten it)")
        return False


def quiet(page) -> None:
    """Stop the caret blinking and freeze CSS transitions so reruns are byte-stable."""
    page.add_style_tag(content="*,*::before,*::after{transition:none!important;"
                               "animation:none!important;caret-color:transparent!important}")


# ── the setup UI walkthrough ──

def capture_setup(pw, shots: Shots) -> None:
    print("\nSetup UI (:%d)" % SETUP_PORT)
    browser = pw.chromium.launch()
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=SCALE)
    page.goto(f"http://127.0.0.1:{SETUP_PORT}/", wait_until="networkidle")
    quiet(page)
    page.wait_for_timeout(600)

    shots.page(page, "setup-01-fresh", full=True)
    shots.element(page, "div.card:has(#rig-select)", "setup-15-rig-card")
    shots.element(page, "#device-catalog", "setup-02-catalog")

    # Load the demo rig — this also runs checkWarp() + checkAllPis() against the mock.
    page.select_option("#rig-select", "demo")
    page.click("text=Load Rig")
    page.wait_for_timeout(4000)                       # SSH probe times out, then the API check answers
    shots.page(page, "setup-03-rig-loaded", full=True)
    shots.element(page, "#pi-list", "setup-04-pi-cards")

    # Initialize every device (the master gate that unlocks all per-device controls).
    page.click("#init-btn")
    page.wait_for_timeout(3500)
    shots.page(page, "setup-05-initialized", full=True)
    shots.element(page, ".device-tabs", "setup-06-device-tabs")

    for dev, name in [("camera", "setup-07-camera"), ("display", "setup-08-display"),
                      ("reward", "setup-09-reward"), ("lick_sensor", "setup-10-lick"),
                      ("photodiode", "setup-11-photodiode"), ("encoder", "setup-12-encoder"),
                      ("calibration_probe", "setup-13-probe")]:
        if not shots.want(name):
            continue
        tab = page.locator(f'.device-tabs .device-tab:has-text("{_tab_label(dev)}")').first
        if tab.count() == 0:
            print(f"  !! no tab for {dev} — skipped {name}")
            continue
        tab.click()
        page.wait_for_timeout(400)
        shots.element(page, f'.device-panel[data-device="{dev}"]', name)

    # Intensity-calibration panel: the one modal-ish overlay in the app.
    if shots.want("setup-14-intensity"):
        page.locator('.device-tabs .device-tab:has-text("Stimulus Display")').first.click()
        page.wait_for_timeout(300)
        sel = page.locator('.device-panel[data-device="display"] select#lum-mode-select')
        if sel.count():
            sel.select_option("manual")
            page.wait_for_timeout(200)
            page.locator('.device-panel[data-device="display"] button:has-text("Intensity Cal")').first.click()
            page.wait_for_timeout(800)
            shots.element(page, "#lum-panel", "setup-14-intensity")
        else:
            print("  !! intensity mode select not found — skipped setup-14-intensity")

    browser.close()


def _tab_label(dev: str) -> str:
    return {"camera": "Camera", "display": "Stimulus Display", "reward": "Reward Valve",
            "lick_sensor": "Lick Sensor", "photodiode": "Photodiode",
            "encoder": "Running Wheel", "calibration_probe": "Calibration Probe"}[dev]


# ── the experiment UI walkthrough ──

def capture_experiment(pw, shots: Shots) -> None:
    print("\nExperiment UI (:%d)" % EXP_PORT)
    browser = pw.chromium.launch()
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=SCALE)
    page.on("dialog", lambda d: d.accept())           # Deploy's alert(), Quit's confirm()
    page.goto(f"http://127.0.0.1:{EXP_PORT}/", wait_until="networkidle")
    quiet(page)
    page.wait_for_timeout(600)

    shots.page(page, "exp-01-fresh", full=True)

    # A task loads with no rig at all — the parameter form is fully capturable dry.
    page.select_option("#task-select", DOC_TASK)
    page.click("#btn-load-task")
    page.wait_for_timeout(900)
    shots.element(page, "#experiment-subsections", "exp-02-task-params")
    shots.element(page, "#card-session", "exp-03-session-card")

    # Load Rig = the connect step (no separate Connect button exists).
    page.select_option("#rig-select", "demo")
    page.click("#btn-load-rig")
    page.wait_for_timeout(4000)
    shots.page(page, "exp-04-connected", full=True)
    shots.element(page, "#pi-status", "exp-05-pi-status")

    # Session identity is enforced at Deploy, so fill it before clicking.
    page.fill("#subject-id", "demo01")
    page.fill("#session-num", "1")
    page.fill("#session-notes", "Dry run against tools/mock_pi.py — no hardware attached.")
    page.click("#btn-deploy")
    page.wait_for_timeout(9000)
    shots.page(page, "exp-06-deployed", full=True)

    # GO — then let the mock play trials so the rasters and live plots fill up.
    page.click("#btn-go")
    page.wait_for_timeout(17000)
    shots.page(page, "exp-07-running", full=True)
    shots.element(page, "#panel-raster", "exp-08-events-raster")
    shots.element(page, "#panel-liveplots", "exp-09-live-plots")
    shots.element(page, "div.card:has(table.trial-table)", "exp-10-trial-table")

    # Let it run to session_end (the mock sends it after MOCK_N trials).
    page.wait_for_timeout(18000)
    shots.page(page, "exp-11-ended", full=True)

    browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="capture only shots whose name starts with this")
    ap.add_argument("--keep-open", action="store_true", help="leave the servers running at the end")
    ap.add_argument("--skip-setup", action="store_true")
    ap.add_argument("--skip-experiment", action="store_true")
    args = ap.parse_args()

    if shutil.which("ssh") is None:
        print("note: no ssh on PATH — the setup UI's per-Pi SSH probe will just fail fast")

    ASSETS.mkdir(parents=True, exist_ok=True)
    shots = Shots(args.only)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("playwright missing — pip install playwright && playwright install chromium")

    print("starting servers…")
    procs = start_servers()
    try:
        with sync_playwright() as pw, TaskFileGuard(DOC_TASK):
            if not args.skip_setup:
                capture_setup(pw, shots)
            if not args.skip_experiment:
                capture_experiment(pw, shots)
    finally:
        if args.keep_open:
            print(f"\nservers still up: setup :{SETUP_PORT}  experiment :{EXP_PORT}  mock :{MOCK_API_PORT}")
            print("stop them with:  pkill -f 'mock_pi.py|setup/app.py|app/app.py'")
        else:
            stop_servers(procs)

    print(f"\n{len(shots.written)} shot(s) -> {ASSETS.relative_to(ROOT)}/")
    if args.only and not shots.written:
        print(f"nothing matched --only {args.only!r}")


if __name__ == "__main__":
    main()
