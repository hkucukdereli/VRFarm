"""
controller/app.py

The VRFarm controller: ONE Flask app, one port, every rig. Serves the shell (four tabs down
the left: Network / Setup / Experiment / Data), mounts the per-tab blueprints, and opens the
single UDP socket that receives events from every rig's leader.

    conda activate vrfarm
    python controller/app.py            # http://localhost:5000  (--port to change)
"""
from __future__ import annotations
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, render_template, request, abort   # noqa: E402

from controller import settings, events                              # noqa: E402
from controller.registry import registry                             # noqa: E402
from controller import experiment, setup, network, data              # noqa: E402


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.register_blueprint(experiment.bp)
    app.register_blueprint(setup.bp)
    app.register_blueprint(network.bp)
    app.register_blueprint(data.bp)

    def _tasks():
        return sorted(p.stem for p in (ROOT / "experiments").glob("*.yaml"))

    def _known(rig: str) -> bool:
        return rig in registry.list_rig_files()

    def _ensure_loaded(rig: str):
        """Opening a rig's tab loads its YAML into the registry (no Pi contact). Connecting to
        the Pis stays an explicit button in the Experiment page."""
        if rig not in registry.rigs:
            try:
                registry.load(rig)
            except RuntimeError as e:
                abort(409, str(e))

    # ── pages ──

    @app.route("/")
    def shell():
        return render_template("shell.html")

    @app.route("/network")
    def network_page():
        return render_template("network.html")

    @app.route("/data")
    def data_page():
        return render_template("data.html")

    @app.route("/setup/<rig>")
    def setup_page(rig):
        if not _known(rig):
            abort(404)
        _ensure_loaded(rig)
        return render_template("setup.html", rig=rig)

    @app.route("/experiment/<rig>")
    def experiment_page(rig):
        if not _known(rig):
            abort(404)
        _ensure_loaded(rig)
        return render_template("experiment.html", rig=rig, tasks=_tasks())

    # ── global API ──

    @app.route("/api/rigs")
    def api_rigs():
        out = []
        for name in registry.list_rig_files():
            rs = registry.rigs.get(name)
            out.append({"name": name, "loaded": rs is not None, "phase": rs.phase if rs else None})
        return jsonify({"rigs": out, "groups": settings.groups()})

    @app.route("/api/fleet")
    def api_fleet():
        d = events.demux
        return jsonify({"rigs": registry.snapshot_all(),
                        "unknown_senders": registry.unknown_senders,
                        "udp": (d.stats if d else None)})

    @app.route("/api/groups/<gname>/load", methods=["POST"])
    def api_group_load(gname):
        members = settings.groups().get(gname)
        if members is None:
            return jsonify({"ok": False, "error": f"no group '{gname}'"}), 404
        results = {}
        for name in members:
            try:
                registry.load(name)
                results[name] = {"ok": True}
            except Exception as e:
                results[name] = {"ok": False, "error": str(e)}
        return jsonify({"ok": all(r["ok"] for r in results.values()), "rigs": members, "results": results})

    @app.route("/api/device_schemas")
    def api_device_schemas():
        """task_params_schema for every registered device (the Setup device catalog)."""
        from devices.base import DEVICE_REGISTRY
        import devices.lick_sensor, devices.reward, devices.camera          # noqa: F401,E401
        import devices.photodiode, devices.display                         # noqa: F401,E401
        import devices.calibration_probe, devices.encoder                  # noqa: F401,E401
        schemas = {}
        for name, cls in DEVICE_REGISTRY.items():
            schemas[name] = {
                "label": cls.info.label,
                "io_type": cls.info.io_type.value,
                "required_packages": cls.info.required_packages,
                "needs_calibration": cls().needs_calibration,
                "task_params": cls.task_params_schema(),
            }
        return jsonify(schemas)

    def _quit_blockers() -> str | None:
        running = registry.any_running()
        if running:
            return f"session running on {', '.join(running)}"
        busy = [f"{n} ({rs.busy_kind})" for n, rs in registry.rigs.items() if rs.busy_kind]
        if busy:
            return f"busy: {', '.join(busy)}"
        return None

    def _reachable_pis() -> list[dict]:
        """Every Pi of every rig file (loaded or not) that answers SSH, followers listed before
        their leader. Loopback Pis (mock/test rigs) are never included."""
        import socket
        import yaml
        from concurrent.futures import ThreadPoolExecutor
        from controller.ssh import target as ssh_target, is_local
        pis = []
        for name in registry.list_rig_files():
            try:
                cfg = yaml.safe_load(registry.rig_path(name).read_text()) or {}
            except Exception:
                continue
            rig_pis = [p for p in (cfg.get("pis") or []) if p.get("ip")]
            rig_pis.sort(key=lambda p: p.get("role") == "leader")          # followers first
            for p in rig_pis:
                if not is_local(ssh_target(p)):
                    pis.append({"rig": name, "name": p.get("name", p["ip"]), "ip": p["ip"],
                                "role": p.get("role", ""), "user": p.get("user") or "vruser"})

        def up(p):
            try:
                with socket.create_connection((p["ip"], 22), timeout=1.5):
                    return True
            except OSError:
                return False
        with ThreadPoolExecutor(max_workers=16) as ex:
            alive = list(ex.map(up, pis))
        return [p for p, a in zip(pis, alive) if a]

    @app.route("/api/quit/preview")
    def quit_preview():
        return jsonify({"ok": True, "blocked": _quit_blockers(), "pis": _reachable_pis()})

    @app.route("/api/quit", methods=["POST"])
    def quit_app():
        blocked = _quit_blockers()
        if blocked:
            return jsonify({"ok": False, "error": blocked}), 409
        poweroff = []
        if (request.get_json(silent=True) or {}).get("poweroff_pis"):
            from controller.ssh import target as ssh_target, ssh_result
            for p in _reachable_pis():      # followers before their leader, one rig at a time
                r = ssh_result(ssh_target(p), "sudo -n poweroff", timeout=15)
                err = (r.stderr or "").lower()
                # 255 with no sudo/password complaint = the Pi dropped the link while going down
                ok = r.returncode == 0 or (r.returncode == 255 and "sudo" not in err and "password" not in err)
                poweroff.append({"rig": p["rig"], "name": p["name"], "ip": p["ip"], "ok": ok,
                                 "error": None if ok else (r.stderr or "").strip()[-200:]})
                print(f"[quit] poweroff {p['name']} ({p['ip']}): {'sent' if ok else 'FAILED ' + err[-120:]}")
        func = request.environ.get("werkzeug.server.shutdown")
        if func:
            func()
        else:
            threading.Timer(0.3, lambda: os._exit(0)).start()
        return jsonify({"ok": True, "poweroff": poweroff})

    return app


def main():
    import argparse
    import webbrowser
    cfg = settings.load()
    parser = argparse.ArgumentParser(description="VRFarm controller UI")
    parser.add_argument("--port", type=int, default=int(cfg.get("ui_port") or 5000))
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--rigs-dir", default=None, help="use another rigs/ folder (tests)")
    args = parser.parse_args()
    if args.rigs_dir:
        settings.set_rigs_dir(args.rigs_dir)

    app = create_app()
    # With --debug, werkzeug forks a reloader child; only the child may own the UDP socket.
    if not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        events.start_demux(int(cfg.get("event_port") or 5571))

    url = f"http://localhost:{args.port}"
    if not args.no_browser and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
