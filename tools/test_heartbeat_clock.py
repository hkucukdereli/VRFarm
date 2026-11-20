#!/usr/bin/env python3
"""tools/test_heartbeat_clock.py — the display heartbeat and the stim_onset ack against a follower
whose clock is wrong, no Pi: a real engine/leader.py `Leader` (no devices, no session) flashes
heartbeats at a fake displayd on loopback UDP. The fake "flips" 20 ms after the command, writes
its OWN clock (the real one plus an offset) into the report like displayd does, and 41 ms later
plays the photodiode's three sub-pulses into the engine on the real clock.

The engine must judge by when the report ARRIVES on its own clock (kernel receive stamp), so the
follower's offset may be anything; the offset must only be measured and named.

    conda activate vrfarm && python tools/test_heartbeat_clock.py        (Linux; ~25 s)
"""
import json
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.leader as L                                     # noqa: E402

fails = 0

FLIP_DELAY_S = 0.020     # HB_FLASH received -> flip
LIGHT_DELAY_S = 0.041    # flip -> first photodiode edge (cheddar: 36-48 ms)
SUB_PULSE_S = 0.0087     # one flash = 3 sub-pulses
PERIOD_S = 0.3


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    fails += 0 if cond else 1


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Rig:
    """A Leader with the heartbeat switched on, a fake displayd, and a fake controller."""

    def __init__(self, offset_s=0.0, report=True, light=True, kernel_stamp=True,
                 flip_delay_s=FLIP_DELAY_S):
        self.offset_s, self.report, self.light = offset_s, report, light
        self.flip_delay_s = flip_delay_s
        net = {k: free_port() for k in ("command_port", "event_port", "ack_port", "display_port",
                                        "displayd_pd_port")}
        rig = {"name": "hbtest", "network": net, "devices": {},
               "pis": [{"name": "lead", "ip": "127.0.0.1", "role": "leader", "devices": []},
                       {"name": "dlp", "ip": "127.0.0.1", "role": "follower", "devices": []}]}
        self.leader = L.Leader(rig, {"stimulus": {}, "session": {}},
                               {"subject_id": "T", "date": "20261120", "session_num": 1})
        ld = self.leader
        if not kernel_stamp:    # the socket as it was before 2026-11-20: arrival = time of the read
            ld._ack_sock.setsockopt(socket.SOL_SOCKET, L.SO_TIMESTAMPNS, 0)
            ld._ack_rx_stamped = False
        self.ack_addr = ("127.0.0.1", net["ack_port"])
        self._disp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)      # displayd :5575
        self._disp.bind(("127.0.0.1", net["display_port"]))
        self._disp.settimeout(0.05)
        self._pd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)        # displayd :5582
        self._pd.bind(("127.0.0.1", net["displayd_pd_port"]))
        self._pd.settimeout(0.05)
        self._ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # controller :5571
        self._ctl.bind(("127.0.0.1", net["event_port"]))
        self._ctl.settimeout(0.05)
        ld._mac_addr = ("127.0.0.1", net["event_port"])
        self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.flips = {}        # seq -> real flip time
        self.verdicts = {}     # seq -> confirmed (what displayd is told)
        self.events = []       # what the controller is told
        self.judged = []       # the engine's own heartbeat records
        judge = ld._hb_judge
        ld._hb_judge = lambda hb, ok, now: (self.judged.append(dict(hb, confirmed=ok)),
                                            judge(hb, ok, now))[1]
        self._stop = False
        self._threads = [threading.Thread(target=f, daemon=True)
                         for f in (self._displayd, self._collect_pd, self._collect_ctl)]
        for t in self._threads:
            t.start()

    # the follower: flip, report on its own clock, then the light the photodiode sees
    def _displayd(self):
        while not self._stop:
            try:
                msg = json.loads(self._disp.recvfrom(4096)[0])
            except socket.timeout:
                continue
            if msg.get("cmd") != "HB_FLASH":
                continue
            time.sleep(self.flip_delay_s)
            t_flip = time.time()
            self.flips[msg["seq"]] = t_flip
            if self.report:
                self._tx.sendto(json.dumps({"type": "hb_flash", "seq": msg["seq"],
                                            "t_flip": t_flip + self.offset_s}).encode(),
                                self.ack_addr)
            if self.light:
                for k in range(3):
                    time.sleep(max(0.0, t_flip + LIGHT_DELAY_S + k * SUB_PULSE_S - time.time()))
                    self.leader._on_sync_pulse({"event": "sync_pulse", "t": time.time()})

    def _collect_pd(self):
        while not self._stop:
            try:
                msg = json.loads(self._pd.recvfrom(4096)[0])
            except socket.timeout:
                continue
            if msg.get("type") == "hb_verdict":
                self.verdicts[msg["seq"]] = msg["confirmed"]

    def _collect_ctl(self):
        while not self._stop:
            try:
                self.events.append(json.loads(self._ctl.recvfrom(65536)[0]))
            except socket.timeout:
                continue

    def run(self, n, block_after_cmd_s=0.0):
        """n heartbeats through the engine's own tick. block_after_cmd_s: the trial loop does not
        come back to its sockets for this long after each flash command (a reward pulse, an HDF5
        flush) — the report then waits in the socket."""
        ld = self.leader
        ld.running = True
        ld._hb_enabled = True
        ld._hb_period_s = PERIOD_S
        ld._hb_next = time.time() + 0.05
        while len(self.judged) < n:
            seq = ld._hb_seq
            ld._check_udp()
            if ld._hb_seq != seq and block_after_cmd_s:
                time.sleep(block_after_cmd_s)
            time.sleep(0.001)
        ld._hb_enabled = False
        time.sleep(0.15)
        return self

    def alarms(self, kind):
        return [e["alarm"] for e in self.events
                if e.get("type") == "display_health" and (e.get("alarm") or {}).get("kind") == kind]

    def close(self):
        self._stop = True
        for t in self._threads:
            t.join(timeout=1)
        ld = self.leader
        for s in (ld._event_sock, ld._cmd_sock, ld._ack_sock, self._disp, self._pd, self._ctl,
                  self._tx):
            s.close()


def old_rule(hb, clusters):
    """The confirm test before 2026-11-20: a pulse in [t_flip - 20 ms, t_flip + 70 ms], t_flip
    being the FOLLOWER-clock time in the report."""
    return any(hb["t_flip"] - 0.020 <= c <= hb["t_flip"] + 0.070 for c in clusters)


def main():
    if not sys.platform.startswith("linux"):
        print("skip: kernel receive stamps (SO_TIMESTAMPNS) are Linux only")
        return

    print("1. the receive stamp is the arrival, not the read")
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    check(L._enable_rx_stamp(rx), "SO_TIMESTAMPNS accepted on a UDP socket")
    t0 = time.time()
    tx.sendto(b"{}", rx.getsockname())
    time.sleep(0.15)
    _data, t_rx = L._recv_stamped(rx)
    check(-0.0005 <= t_rx - t0 < 0.005,
          f"read 150 ms late, stamp is {(t_rx - t0) * 1e3:.3f} ms after the send")
    rx.setsockopt(socket.SOL_SOCKET, L.SO_TIMESTAMPNS, 0)
    t0 = time.time()
    tx.sendto(b"{}", rx.getsockname())
    time.sleep(0.05)
    _data, t_rx = L._recv_stamped(rx)
    t1 = time.time()
    check(t_rx - t0 >= 0.049 and t_rx <= t1, "without the option the time of the read is returned")
    rx.close()
    tx.close()

    print("2. heartbeats against a follower clock that is right, behind, ahead, far off")
    for off in (0.0, -0.034, +0.080, -1.5, +3600.0):
        r = Rig(offset_s=off).run(8)
        ld = r.leader
        s = ld._hb_summary()
        tag = f"offset {off * 1e3:+.0f} ms"
        check(len(r.judged) == 8 and all(h["confirmed"] for h in r.judged),
              f"{tag}: {sum(h['confirmed'] for h in r.judged)}/8 heartbeats confirmed")
        check(not r.alarms("heartbeat_lost"), f"{tag}: no heartbeat_lost alarm")
        check(all(r.verdicts.get(h["seq"]) is True for h in r.judged),
              f"{tag}: displayd is told confirmed for every seq")
        lat = [h["t_rx"] - r.flips[h["seq"]] for h in r.judged]
        check(all(-0.0005 <= x < 0.005 for x in lat),
              f"{tag}: t_rx is the report's arrival (flip + {max(lat) * 1e3:.2f} ms at most)")
        med = s["follower_clock_offset_ms"]["median"]
        check(abs(med - off * 1e3) < 3.0, f"{tag}: measured as {med:+.1f} ms")
        rl = s["report_to_light_ms"]
        check(abs(rl["median"] - LIGHT_DELAY_S * 1e3) < 4.0 and rl["n"] == 8,
              f"{tag}: flip report -> light {rl['median']:.1f} ms (the fake's 41)")
        warn = r.alarms("clock_offset")
        if abs(off) > L.HB_CLOCK_WARN_S:
            check(len(warn) == 1 and warn[0]["level"] == "warn"
                  and abs(warn[0]["offset_s"] - off) < 0.003
                  and ("behind" if off < 0 else "ahead") in warn[0]["msg"],
                  f"{tag}: one clock_offset warning, level warn, says "
                  f"{'behind' if off < 0 else 'ahead'}")
            # the same flashes under the rule this replaces
            n_old = sum(old_rule(h, [h["t_light"]]) for h in r.judged)
            check(n_old == 0, f"{tag}: the follower-clock rule would have confirmed {n_old}/8")
        else:
            check(not warn, f"{tag}: no clock_offset warning")
        check(s["n"] == 8 and s["n_confirmed"] == 8 and s["n_no_report"] == 0
              and s["rx_stamp"] == "kernel", f"{tag}: summary {ld._hb_summary_text(s)}")
        r.close()

    print("3. a dark display is still a dark display")
    r = Rig(offset_s=-0.034, light=False).run(4)
    check(not any(h["confirmed"] for h in r.judged), "reports arrive, no light: 0/4 confirmed")
    lost = r.alarms("heartbeat_lost")
    check(len(lost) == 1 and lost[0]["level"] == "alarm" and lost[0]["n_missed"] == L.HB_LOST_N,
          f"heartbeat_lost raised once, after {L.HB_LOST_N} misses")
    check(all(r.verdicts.get(h["seq"]) is False for h in r.judged),
          "displayd is told unconfirmed")
    s = r.leader._hb_summary()
    check(s["n"] == 4 and s["n_confirmed"] == 0 and "report_to_light_ms" not in s,
          f"summary: {r.leader._hb_summary_text(s)}")
    r.close()

    print("4. no flip report: judged from the command time")
    r = Rig(report=False).run(6)
    s = r.leader._hb_summary()
    check(all(h["confirmed"] and h["t_rx"] is None for h in r.judged),
          "6/6 confirmed without a report")
    check(s["n_no_report"] == 6 and "follower_clock_offset_ms" not in s
          and "report_to_light_ms" not in s, f"summary: {r.leader._hb_summary_text(s)}")
    r.close()
    r = Rig(report=False, light=False).run(3)
    check(not any(h["confirmed"] for h in r.judged) and len(r.alarms("heartbeat_lost")) == 1,
          "no report and no light: alarm")
    r.close()

    print("5. a trial loop that reads its socket 150 ms late")
    r = Rig(offset_s=-0.034).run(6, block_after_cmd_s=0.15)
    lat = [h["t_rx"] - r.flips[h["seq"]] for h in r.judged]
    check(all(h["confirmed"] for h in r.judged) and max(lat) < 0.005,
          f"kernel stamp: 6/6 confirmed, t_rx within {max(lat) * 1e3:.2f} ms of the flip")
    r.close()
    r = Rig(offset_s=-0.034, kernel_stamp=False).run(6, block_after_cmd_s=0.15)
    lat = [h["t_rx"] - r.flips[h["seq"]] for h in r.judged]
    check(not any(h["confirmed"] for h in r.judged) and min(lat) > 0.1,
          f"time of the read instead: 0/6 confirmed, t_rx {min(lat) * 1e3:.0f} ms after the flip "
          f"(why the stamp comes from the kernel)")
    check(r.leader._hb_summary()["rx_stamp"] == "read", "summary says rx_stamp read")
    r.close()

    r = Rig(offset_s=-0.034, flip_delay_s=0.17).run(6)
    late = [(h["t_light"] or 0) - h["t_cmd"] for h in r.judged]
    check(all(h["confirmed"] for h in r.judged) and min(late) > L.HB_EVAL_AGE_S,
          f"a flip 170 ms after the command: 6/6 confirmed, light {min(late) * 1e3:.0f} ms after "
          f"the command (judged only once the window around the report has closed)")
    r.close()

    print("6. stim_onset ack: the trial's flip time is the ack's arrival")
    r = Rig()
    ld = r.leader
    ld.running = True
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def ack(**kw):
        tx.sendto(json.dumps({"type": "stim_onset", **kw}).encode(), r.ack_addr)
        return time.time()

    t_show = time.time()
    time.sleep(0.02)                      # SHOW -> flip
    t_sent = ack(trial=7, t=time.time() - 0.034)
    time.sleep(0.08)                      # the engine is busy; the ack waits in the socket
    got = ld._await_onset_ack(7, 0.05)
    check(got is not None and abs(got - t_sent) < 0.003 and got > t_show,
          f"follower 34 ms behind: ack time is the arrival ({(got - t_sent) * 1e3:+.2f} ms from "
          f"the send), {(got - t_show) * 1e3:.1f} ms after SHOW (the follower's own time: "
          f"{(t_sent - 0.034 - t_show) * 1e3:.1f} ms)")
    ack(trial=7, t=time.time())
    time.sleep(0.02)
    ld._check_udp()
    check(ld._onset_acks[7] == got, "a duplicate ack keeps the first arrival")
    t_sent = ack(trial=8)
    time.sleep(0.02)
    ld._check_udp()
    check(abs(ld._onset_acks.get(8, 0) - t_sent) < 0.003, "an ack without t still times the flip")
    ack(trial="x", t=1.0)
    ack(t=1.0)
    tx.sendto(b"\xff not json", r.ack_addr)
    time.sleep(0.02)
    ld._check_udp()
    check(set(ld._onset_acks) == {7, 8}, "acks without a usable trial number are dropped")
    check(ld._await_onset_ack(99, 0.02) is None, "no ack within the budget: None")
    for i in range(60):
        ack(trial=100 + i, t=time.time() - 0.034)
    time.sleep(0.05)
    ld._check_udp()
    check(len(ld._onset_acks) == 50 and 159 in ld._onset_acks and 7 not in ld._onset_acks,
          "bounded at 50 trials, oldest dropped")
    time.sleep(0.1)
    fwd = [e for e in r.events if e.get("type") == "stim_onset"]
    check(len(fwd) >= 62 and fwd[0]["trial"] == 7 and fwd[0]["rig"] == "hbtest",
          "every ack is still republished to the controller")
    warn = r.alarms("clock_offset")
    check(len(warn) == 1 and abs(warn[0]["offset_s"] + 0.034) < 0.003,
          "the acks alone (no heartbeat) name the 34 ms clock offset, once")
    s = ld._hb_summary()
    check(s["n"] == 0 and abs(s["follower_clock_offset_ms"]["median"] + 34) < 3,
          f"summary without heartbeats: {ld._hb_summary_text(s)}")
    tx.close()
    r.close()

    print("7. nothing to say")
    r = Rig()
    check(r.leader._hb_summary() is None, "no heartbeat and no ack: no summary")
    r.close()

    print("8. the session end writes the summary where the loaders find it")
    import tempfile
    import h5py
    import yaml
    from shared.consolidate import consolidate_session
    r = Rig(offset_s=-0.034).run(6)
    ld = r.leader
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        ld.hdf5_file = h5py.File(d / f"{ld.session_id}.h5", "w")
        ld.hdf5_file.create_dataset("trial_index", data=[0])
        ld._finalize_session(d, 1, 1)
        meta = yaml.safe_load((d / "metadata.yaml").read_text())     # plain YAML, no numpy tags
        hb = meta.get("display_heartbeat") or {}
        check(hb.get("n") == 6 and hb.get("n_confirmed") == 6 and hb.get("rx_stamp") == "kernel",
              f"metadata.yaml display_heartbeat: {hb.get('n_confirmed')}/{hb.get('n')}, "
              f"rx_stamp {hb.get('rx_stamp')}")
        check(abs(hb["follower_clock_offset_ms"]["median"] + 34) < 3
              and 30 < hb["report_to_light_ms"]["median"] < 50,
              f"offset {hb['follower_clock_offset_ms']['median']} ms, "
              f"report to light {hb['report_to_light_ms']['median']} ms")
        res = consolidate_session(d, remux_video=False)
        with h5py.File(d / f"{ld.session_id}.h5", "r") as f:
            folded = json.loads(f.attrs["display_heartbeat"])
        check(res.get("ok") and folded == hb, "consolidate folds it into the .h5 as a JSON attribute")
    r.close()

    print(f"\n{'FAILED: ' + str(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
