"""
controller/ssh.py

The controller's SSH / scp / rsync helpers, in one place (setup/app.py had three slightly
different ssh wrappers plus five inline subprocess calls). Passwordless keys and sudo from
the controller to every Pi are assumed (CLAUDE.md). A target on 127.0.0.1 runs the command
locally instead — that is how the smoke tests exercise the Data tab with no Pi.
"""
from __future__ import annotations
import ipaddress
import shlex
import subprocess
from pathlib import Path

DEFAULT_USER = "vruser"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new"]


def target(pi: dict) -> str:
    """'user@ip' for a Pi dict from the rig YAML."""
    return f"{pi.get('user') or DEFAULT_USER}@{pi['ip']}"


def host_of(tgt: str) -> str:
    return tgt.split("@", 1)[1] if "@" in tgt else tgt


def is_local(tgt: str) -> bool:
    h = host_of(tgt)
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _argv(tgt: str, cmd: str) -> list[str]:
    if is_local(tgt):
        return ["bash", "-lc", cmd]
    return ["ssh", *SSH_OPTS, tgt, cmd]


def ssh(tgt: str, cmd: str, timeout: int = 60, check: bool = True) -> str:
    """Run `cmd` on `tgt`, return stdout. Raises RuntimeError with stderr on failure when check."""
    r = subprocess.run(_argv(tgt, cmd), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"SSH failed ({tgt}): {r.stderr.strip() or r.stdout.strip() or r.returncode}")
    return r.stdout


def ssh_result(tgt: str, cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Like ssh() but never raises; the caller reads returncode / stdout / stderr."""
    return subprocess.run(_argv(tgt, cmd), capture_output=True, text=True, timeout=timeout)


def ssh_merged(tgt: str, cmd: str, timeout: int = 60) -> str:
    """stdout + stderr interleaved (for tools that report on stderr, e.g. arduino-cli)."""
    r = subprocess.run(_argv(tgt, cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"SSH failed ({tgt}): {r.stdout.strip()[-800:]}")
    return r.stdout


def ssh_popen(tgt: str, cmd: str) -> subprocess.Popen:
    return subprocess.Popen(_argv(tgt, cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def ssh_ok(tgt: str, timeout: int = 8) -> bool:
    """True when `echo ok` round-trips (keys, host reachable, sshd up)."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", tgt, "echo ok"]
                           if not is_local(tgt) else ["bash", "-lc", "echo ok"],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0 and "ok" in r.stdout
    except Exception:
        return False


def scp(local: str, remote: str, timeout: int = 300, recursive: bool = False) -> None:
    """Copy local -> 'user@ip:path' (or local path when the target is loopback)."""
    if ":" in remote and is_local(remote.split(":", 1)[0]):
        dst = Path(remote.split(":", 1)[1]).expanduser()
        dst.parent.mkdir(parents=True, exist_ok=True)
        args = ["cp", "-r", local, str(dst)] if recursive else ["cp", local, str(dst)]
    else:
        args = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
        if recursive:
            args.append("-r")
        args += [local, remote]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"SCP failed: {r.stderr.strip()}")


def q(s: str) -> str:
    """Shell-quote for remote command lines."""
    return shlex.quote(str(s))
