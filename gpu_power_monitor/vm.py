"""Acquisition inside a VM on this host (the gamma2 deployment).

On gamma2 the NI USB-6341 belongs to a Windows VM (NI has no Linux driver for
USB DAQs). `pai` on the host therefore starts `pai hardware` *inside* the VM
over SSH, with `--run-id` so both sides agree on the run folder, which lives on
a share the host serves. The host then watches the run folder: it serves the
dashboard, stops the run by dropping a STOP file (see utils.request_stop), and
archives the result. From the user's point of view `pai` behaves exactly as it
does on a machine with the DAQ attached.

Nothing here is used unless `vm.*` is configured (PAI_VM_* in .env).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .config import VmConfig
from .manifest import load_manifest


class VmUnavailable(RuntimeError):
    """The VM cannot be reached; the message says what to check."""


def resolve_host(cfg: VmConfig) -> str:
    """The VM's address: configured, or looked up from libvirt by domain name."""
    if cfg.host:
        return cfg.host
    try:
        proc = subprocess.run(
            ["virsh", "-c", "qemu:///system", "domifaddr", cfg.name],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise VmUnavailable(f"cannot ask libvirt for the IP of VM {cfg.name!r}: {exc}") from exc
    for line in proc.stdout.splitlines():
        if "ipv4" in line:
            return line.split()[3].split("/")[0]
    raise VmUnavailable(
        f"VM {cfg.name!r} has no IP address (is it running? `virsh list --all`); "
        "or pin it with PAI_VM_HOST in .env"
    )


def _ssh(cfg: VmConfig, host: str) -> list[str]:
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new", f"{cfg.user}@{host}",
    ]


def _quote(value: str) -> str:
    # Values reach a Windows shell (PowerShell or cmd); double quotes work in
    # both and run ids / operators never contain one (sanitize_folder_name).
    return '"' + value.replace('"', "") + '"'


def run_pai(cfg: VmConfig, args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run `pai <args>` inside the VM and return the completed process."""
    host = resolve_host(cfg)
    command = " ".join([cfg.pai, *(_quote(a) if " " in a else a for a in args)])
    try:
        return subprocess.run(_ssh(cfg, host) + [command], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise VmUnavailable(f"`pai {' '.join(args)}` in the VM did not answer within {timeout:.0f}s") from exc


def start_acquisition(
    cfg: VmConfig,
    *,
    run_id: str,
    operator: str,
    test: bool,
    gpus: str | None,
    log_path: Path,
) -> subprocess.Popen:
    """Launch `pai hardware --run-id <run_id>` in the VM, detached.

    The SSH session stays open for the life of the run (Windows OpenSSH ends
    the remote process when the session drops, so it must outlive this
    process: new session, no controlling terminal). Output goes to log_path.
    """
    host = resolve_host(cfg)
    parts = [cfg.pai, "hardware", "--run-id", _quote(run_id)]
    if operator:
        parts += ["--operator", _quote(operator)]
    if test:
        parts.append("--test")
    if gpus:
        parts += ["--gpus", _quote(gpus)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    return subprocess.Popen(
        _ssh(cfg, host) + [" ".join(parts)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def wait_for_start(run_dir: Path, proc: subprocess.Popen, timeout_sec: float, log_path: Path) -> None:
    """Block until the VM has created the run's manifest (or the SSH died)."""
    deadline = time.time() + timeout_sec
    manifest = Path(run_dir) / "manifest.json"
    while time.time() < deadline:
        if manifest.exists():
            return
        if proc.poll() is not None:
            raise VmUnavailable(
                f"acquisition in the VM exited before starting (code {proc.returncode}); "
                f"see {log_path}:\n{_tail(log_path)}"
            )
        time.sleep(0.5)
    raise VmUnavailable(
        f"acquisition in the VM did not start within {timeout_sec:.0f}s; see {log_path}:\n{_tail(log_path)}"
    )


def run_status(run_dir: Path) -> str:
    try:
        return str(load_manifest(run_dir).get("status", "running"))
    except (FileNotFoundError, ValueError, OSError):
        return "running"


def wait_for_end(run_dir: Path, proc: subprocess.Popen | None = None, timeout_sec: float | None = None) -> str:
    """Block until the run's manifest leaves 'running' (or the SSH session ends).

    Returns the final status. Callers handle KeyboardInterrupt themselves (by
    requesting a stop and calling this again with a timeout).
    """
    deadline = None if timeout_sec is None else time.time() + timeout_sec
    while True:
        status = run_status(run_dir)
        if status != "running":
            return status
        if proc is not None and proc.poll() is not None:
            # Give the share a moment to show the final manifest write.
            time.sleep(2.0)
            return run_status(run_dir)
        if deadline is not None and time.time() >= deadline:
            return status
        time.sleep(1.0)


def _tail(path: Path, lines: int = 8) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"
