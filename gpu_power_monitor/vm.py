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
import base64
import json
import threading
import sys
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


def vm_repo_root(cfg: VmConfig) -> str:
    """The repo checkout inside the VM, derived from the pai.exe path
    (C:\\pai\\paihardware-gamma2\\.venv\\Scripts\\pai.exe -> C:\\pai\\paihardware-gamma2)."""
    pai = cfg.pai.replace("/", "\\")
    marker = "\\.venv\\"
    idx = pai.lower().find(marker)
    return pai[:idx] if idx > 0 else pai.rsplit("\\", 1)[0]


def vm_config_path(cfg: VmConfig, config_name: str) -> str:
    """configs/<name> inside the VM's checkout, as a Windows path."""
    return vm_repo_root(cfg) + "\\configs\\" + config_name.replace("/", "\\")


def run_pai(cfg: VmConfig, args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run `pai <args>` inside the VM and return the completed process."""
    host = resolve_host(cfg)
    command = " ".join([cfg.pai, *(_quote(a) if " " in a else a for a in args)])
    try:
        return subprocess.run(_ssh(cfg, host) + [command], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise VmUnavailable(f"`pai {' '.join(args)}` in the VM did not answer within {timeout:.0f}s") from exc


def _supervisor_command(cfg: VmConfig, action: str, payload: dict) -> str:
    # Encoded PowerShell avoids both Linux-shell and Windows-shell interpolation.
    python = cfg.pai.replace("/", "\\").rsplit("\\", 1)[0] + "\\python.exe"
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    def ps(value):
        return "'" + value.replace("'", "''") + "'"
    command = (f"Set-Location -LiteralPath {ps(vm_repo_root(cfg))}; "
               f"& {ps(python)} -u -m gpu_power_monitor.supervisor {action} {encoded}; exit $LASTEXITCODE")
    return "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(command.encode("utf-16le")).decode()


def control(cfg: VmConfig, run_id: str, action: str, reason: str = "user_stop") -> dict:
    command = _supervisor_command(cfg, "control", {"run_id": run_id, "action": action, "reason": reason})
    try:
        result = subprocess.run(_ssh(cfg, resolve_host(cfg)) + [command], capture_output=True,
                                text=True, timeout=12)
        if result.returncode:
            raise VmUnavailable(result.stderr.strip() or "VM control failed")
        return json.loads(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        raise VmUnavailable(f"VM control unavailable: {exc}") from exc


class ControllerLease:
    def __init__(self, cfg, run_id):
        self.cfg, self.run_id = cfg, run_id
        self.state = {"state": "starting"}
        self.last_contact = time.monotonic()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.closed.is_set():
            try:
                state = control(self.cfg, self.run_id, "heartbeat")
                if state.get("state") != "unknown":
                    self.state = state
                    self.last_contact = time.monotonic()
            except VmUnavailable:
                pass
            self.closed.wait(5)

    def failure(self):
        state = self.state.get("state")
        if state in {"error", "interrupted", "stopping"}:
            return f"VM acquisition {state}: {self.state.get('reason')}"
        if time.monotonic() - self.last_contact > 60:
            return "VM control connection lost; acquisition stop unconfirmed"
        return None

    def close(self):
        self.closed.set()
        self.thread.join(timeout=13)


def start_acquisition(
    cfg: VmConfig, *, run_id: str, operator: str, machine: str, test: bool,
    gpus: str | None, log_path: Path, config_name: str | None = None,
    duration_sec: float | None = None, maximum_sec: float = 87000,
    nvml_gpus: list[str] | None = None,
) -> subprocess.Popen:
    """Launch the Windows-local supervisor, not an unbounded DAQ process."""
    spec = dict(run_id=run_id, operator=operator, machine=machine, test=test, gpus=gpus,
                duration_sec=duration_sec, maximum_sec=maximum_sec, nvml_gpus=nvml_gpus)
    if config_name:
        spec["config"] = vm_config_path(cfg, config_name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(_ssh(cfg, resolve_host(cfg)) + [_supervisor_command(cfg, "spawn", spec)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    proc.lease = ControllerLease(cfg, run_id)
    return proc


def wait_for_start(run_dir: Path, proc: subprocess.Popen, timeout_sec: float, log_path: Path) -> None:
    """Block until the VM has created the run's manifest (or the SSH died)."""
    deadline = time.monotonic() + timeout_sec
    manifest = Path(run_dir) / "manifest.json"
    while time.monotonic() < deadline:
        lease = getattr(proc, "lease", None)
        if lease is not None:
            if lease.failure():
                raise VmUnavailable(lease.failure())
            if lease.state.get("state") == "ready":
                Path(run_dir).mkdir(parents=True, exist_ok=True)
                return
        elif manifest.exists():
            return
        if proc.poll() is not None and (lease is None or proc.returncode != 0):
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
    deadline = None if timeout_sec is None else time.monotonic() + timeout_sec
    while True:
        lease = getattr(proc, "lease", None)
        if lease is not None:
            state = lease.state.get("state")
            if state in {"completed", "interrupted", "error"}:
                return state
        status = run_status(run_dir)
        if lease is None and status != "running":
            return status
        if lease is None and proc is not None and proc.poll() is not None:
            # Give the share a moment to show the final manifest write.
            time.sleep(2.0)
            return run_status(run_dir)
        if deadline is not None and time.monotonic() >= deadline:
            return status
        time.sleep(1.0)


def _tail(path: Path, lines: int = 8) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"
