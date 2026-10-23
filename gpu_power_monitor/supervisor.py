"""Windows-local acquisition supervisor and SSH control entry point.

Control never reads the SMB destination. A separate process exports closed
files; an unavailable destination cannot prevent a local stop or deadline.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

from .utils import atomic_replace_text, sanitize_folder_name, utc_now_iso

LEASE_SECONDS = 60.0
STALL_SECONDS = 60.0
STOP_SECONDS = 30.0
FINALIZE_SECONDS = 300.0
EXPORT_SECONDS = 600.0
TERMINAL = {"completed", "interrupted", "error"}


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    if path.name == "status.json":
        data = dict(data, generated_at_epoch=time.time())
    atomic_replace_text(path, json.dumps(data, sort_keys=True))


def local_root() -> Path:
    from .config import load_dotenv
    load_dotenv()
    value = os.environ.get("PAI_VM_SPOOL_ROOT") or str(
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PAI" / "spool")
    if value.startswith(("\\\\", "//")):
        raise ValueError("PAI_VM_SPOOL_ROOT must be Windows-local, not a network share")
    root = Path(value).expanduser().absolute()
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(root.anchor)) not in (2, 3, 6):
            raise ValueError("PAI_VM_SPOOL_ROOT must be on an accessible local volume")
    return root


def session_path(run_id: str) -> Path:
    if not run_id or sanitize_folder_name(run_id) != run_id or run_id in {".", ".."}:
        raise ValueError("invalid run id")
    return local_root() / run_id


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=10)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=10)


class Watchdog:
    """Pure monotonic policy; worker progress is a changing local sequence."""
    def __init__(self, now: float, maximum: float):
        self.started = self.lease_at = self.progress_at = now
        self.maximum = maximum
        self.lease = self.progress = None
        self.reason = None
        self.stopping_at = None
        self.finalizing_at = None

    def step(self, now: float, control: dict, progress: dict) -> tuple[str | None, bool]:
        if control.get("heartbeat") != self.lease:
            self.lease, self.lease_at = control.get("heartbeat"), now
        if progress.get("sequence") != self.progress:
            self.progress, self.progress_at = progress.get("sequence"), now
        if progress.get("phase") == "finalizing" and self.finalizing_at is None:
            self.finalizing_at = now
        reason = control.get("stop_reason")
        if not reason and now - self.lease_at >= LEASE_SECONDS:
            reason = "controller_lost"
        if not reason and now - self.started >= self.maximum:
            reason = "deadline_exceeded"
        if not reason and self.finalizing_at is None and now - self.progress_at >= STALL_SECONDS:
            reason = "acquisition_stalled"
        if self.reason is None and reason:
            self.reason, self.stopping_at = reason, now
        force = self.stopping_at is not None and now - self.stopping_at >= STOP_SECONDS
        if self.finalizing_at is not None:
            force = now - self.finalizing_at >= FINALIZE_SECONDS
        return self.reason, force


def worker(spec: dict, session: Path) -> int:
    from .acquisition import acquire
    from .config import load_config, select_gpus
    cfg = load_config(Path(spec["config"]) if spec.get("config") else Path(__file__).resolve().parents[1] / "configs" / "default.yaml")
    cfg = select_gpus(cfg, spec.get("gpus"))
    cfg = dataclasses.replace(cfg, measurement_name=spec["run_id"],
                              operator=spec.get("operator", ""), machine=spec.get("machine", ""),
                              test_run=spec.get("test", False),
                              storage=dataclasses.replace(cfg.storage, output_root=session))
    acquire(cfg, simulate=spec.get("simulate", False), duration_sec=spec.get("duration_sec"),
            run_dir=session / "data", progress_path=session / "progress.json")
    return 0


def copy_verified(src: Path, dest: Path, expected: str | None = None) -> str:
    from .manifest import sha256_file
    checksum = expected or sha256_file(src)
    if dest.exists() and dest.stat().st_size == src.stat().st_size and sha256_file(dest) == checksum:
        return checksum
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".copying")
    shutil.copyfile(src, tmp)
    if sha256_file(tmp) != checksum:
        raise OSError(f"checksum mismatch: {src.name}")
    os.replace(tmp, dest)
    return checksum


def export(session: Path, destination: Path) -> int:
    """Only this expendable process touches the network share."""
    from .manifest import sha256_file
    from .utils import chunk_paths
    data = session / "data"
    copied = read_json(session / "copy-index.json")
    def stamp(path):
        info = path.stat()
        return [info.st_size, info.st_mtime_ns]
    def sync(source, expected=None):
        rel = source.relative_to(data).as_posix()
        target = destination / rel
        cached = copied.get(rel, {})
        source_stamp = stamp(source)
        if (cached.get("source") == source_stamp and target.exists()
                and cached.get("destination") == stamp(target)
                and (expected is None or cached.get("sha256") == expected)):
            return cached["sha256"]
        checksum = copy_verified(source, target, expected)
        copied[rel] = {"source": source_stamp, "destination": stamp(target), "sha256": checksum}
        write_json(session / "copy-index.json", copied)
        return checksum
    export_started = None
    while True:
        if (session / "EXPORT").exists():
            export_started = export_started or time.monotonic()
            if time.monotonic()-export_started > EXPORT_SECONDS:
                return 1
        try:
            destination.mkdir(parents=True, exist_ok=True)
            if (destination / "STOP").exists() and not (session / "stop.json").exists():
                write_json(session / "stop.json", {"reason": "user_stop"})
            state = read_json(session / "status.json")
            if state:
                write_json(destination / "supervisor_status.json", state)
            manifest = read_json(data / "manifest.json")
            for source in chunk_paths(data) if data.exists() else []:
                sync(source)
            for name in ("latest_status.json", "latest.npz"):
                source = data / name
                if source.exists():
                    # Preview files are mutable and are not part of verification.
                    shutil.copyfile(source, destination / name)
            terminal = (session / "EXPORT").exists()
            if terminal:
                for name in copied:
                    if not (data / name).exists():
                        (destination / name).unlink(missing_ok=True)
                for item in manifest.get("files", []):
                    source = data / item["path"]
                    sync(source, item["sha256"])
                # Preserve partial/PENDING data on failed runs as evidence too.
                for source in data.rglob("*"):
                    if source.is_file() and source.name != "manifest.json":
                        rel = source.relative_to(data)
                        sync(source)
                manifest["storage"] = {"status": "verified", "local_spool": str(data)}
                manifest["files"] = [
                    {"path": p.relative_to(data).as_posix(), "size_bytes": p.stat().st_size,
                     "sha256": sync(p)}
                    for p in sorted(data.rglob("*")) if p.is_file() and p.name not in
                    {"manifest.json", "latest.npz", "latest_status.json", "STOP"}]
            elif manifest:
                manifest = dict(manifest, status="running", archive_status="local_only")
            if manifest:
                write_json(destination / "manifest.json", manifest)
            if terminal:
                write_json(session / "export.json", {"status": "verified"})
                return 0
        except Exception as exc:
            write_json(session / "export.json", {"status": "retrying", "error": str(exc)})
        time.sleep(2)


def launch(spec: dict) -> int:
    from .config import load_config
    session = session_path(spec["run_id"])
    session.mkdir(parents=True, exist_ok=False)
    write_json(session / "spec.json", spec)
    cfg = load_config(Path(spec["config"]) if spec.get("config") else Path(__file__).resolve().parents[1] / "configs" / "default.yaml")
    maximum = float(spec.get("maximum_sec", 86400 + 600))
    if maximum <= 0 or not math.isfinite(maximum):
        raise ValueError("maximum_sec must be finite and positive")
    estimate = int(maximum * cfg.sample_rate_hz * (2 + 3 * len(cfg.channels.gpus)) * 20 * 1.2 + 1e9)
    if shutil.disk_usage(session).free < estimate:
        write_json(session / "status.json", {"state": "error", "reason": "insufficient_spool_space",
                                             "required_bytes": estimate})
        return 1
    write_json(session / "control.json", {"heartbeat": uuid.uuid4().hex})
    log = open(session / "worker.log", "ab", buffering=0)
    proc = transfer = None
    try:
        proc = subprocess.Popen([sys.executable, "-u", "-m", "gpu_power_monitor.supervisor", "worker", str(session)],
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        destination = Path(cfg.storage.output_root) / ("test" if spec.get("test") else "") / spec["run_id"]
        transfer = subprocess.Popen([sys.executable, "-u", "-m", "gpu_power_monitor.supervisor", "export", str(session), str(destination)],
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except BaseException:
        if proc is not None:
            kill_tree(proc)
        if transfer is not None:
            kill_tree(transfer)
        log.close()
        write_json(session / "status.json", {"state": "error", "reason": "process_start_failed"})
        raise
    write_json(session / "owner.json", {"supervisor": os.getpid(), "worker": proc.pid, "exporter": transfer.pid})
    watch = Watchdog(time.monotonic(), maximum)
    forced = False
    try:
        while proc.poll() is None:
            progress = read_json(session / "progress.json")
            ctl = read_json(session / "control.json")
            ctl["stop_reason"] = read_json(session / "stop.json").get("reason")
            reason, force = watch.step(time.monotonic(), ctl, progress)
            state = "stopping" if reason else progress.get("phase", "starting")
            write_json(session / "status.json", {"state": state, "reason": reason,
                       "pid": proc.pid, "progress": progress, "generated_at": utc_now_iso()})
            if reason:
                (session / "data").mkdir(exist_ok=True)
                write_json(session / "data" / "STOP", {"reason": reason})
            if force:
                forced = True
                kill_tree(proc)
                break
            time.sleep(.5)
        data = session / "data"
        data.mkdir(exist_ok=True)
        diagnostics = data / "diagnostics"
        diagnostics.mkdir(exist_ok=True)
        shutil.copyfile(session / "worker.log", diagnostics / "worker.log")
        manifest = read_json(data / "manifest.json") or {"run_id": spec["run_id"], "run_kind": "test" if spec.get("test") else "archive"}
        reason = watch.reason or read_json(session / "stop.json").get("reason")
        successful = proc.returncode == 0 and manifest.get("status") == "completed" and reason in (None, "workload_completed") and not forced
        if not successful:
            state = "interrupted" if reason in {"user_stop", "controller_lost"} else "error"
            manifest.update(status=state, archive_status="archive_error", ended_at=utc_now_iso(),
                            error=reason or "acquisition_failed")
        if forced:
            manifest["error"] = "shutdown_timeout"
        manifest["supervision"] = {"forced": forced, "reason": reason, "worker_exit": proc.returncode,
                                    "local_spool": str(data), "maximum_sec": maximum}
        manifest["acquisition_status"] = manifest["status"]
        manifest["workload_status"] = "completed" if reason == "workload_completed" else ("not_requested" if successful else "unconfirmed")
        manifest["storage"] = {"status": "pending", "local_spool": str(data)}
        write_json(data / "manifest.json", manifest)
        write_json(data / "latest_status.json", dict(read_json(data / "latest_status.json"),
                   state="COMPLETE" if successful else "ERROR", generated_at_epoch=time.time()))
        (session / "EXPORT").touch()
        write_json(session / "status.json", {"state": "exporting", "acquisition_status": manifest["status"],
                   "reason": reason, "forced": forced, "worker_stopped": True})
        deadline = time.monotonic() + EXPORT_SECONDS
        while transfer.poll() is None and time.monotonic() < deadline:
            time.sleep(.5)
        verified = read_json(session / "export.json").get("status") == "verified"
        if not verified:
            kill_tree(transfer)
        final = manifest["status"] if verified else "error"
        write_json(session / "status.json", {"state": final, "acquisition_status": manifest["status"],
                   "reason": reason if verified else "export_unverified", "forced": forced,
                   "worker_stopped": True, "storage_status": "verified" if verified else "unverified",
                   "local_spool": str(data)})
        return 0 if final == "completed" else 1
    finally:
        kill_tree(proc)
        kill_tree(transfer)
        log.close()


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            # Access denied is not proof of death.
            if ctypes.get_last_error() == 5:
                raise RuntimeError("cannot verify process ownership/liveness")
            return False
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise RuntimeError("cannot query process status")
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False


def windows_processes() -> list[dict]:
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pai.exe') } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=12)
    if result.returncode:
        raise RuntimeError("recovery refused: process verification failed")
    processes = json.loads(result.stdout.strip() or "[]")
    return [processes] if isinstance(processes, dict) else processes


def recover(run_id: str) -> dict:
    """Explicit recovery never invents a successful completion."""
    from .config import load_config
    session = session_path(run_id)
    spec = read_json(session / "spec.json")
    if spec:
        owner = read_json(session / "owner.json")
        if not owner:
            raise RuntimeError("recovery refused: missing process ownership record")
        if any(pid_alive(owner.get(key)) for key in ("supervisor", "worker", "exporter")):
            raise RuntimeError("recovery refused: supervisor, acquisition, or exporter still running")
        if os.name == "nt":
            for process in windows_processes():
                command = process.get("CommandLine")
                if not command or str(session).casefold() in command.casefold():
                    raise RuntimeError("recovery refused: live child or inaccessible process details")
        path = session / "data"
    else:
        # Legacy runs have no local supervisor record. Query exact run-id
        # arguments on Windows before permitting a metadata repair.
        if os.name != "nt":
            raise RuntimeError("legacy recovery must verify Windows processes on the VM")
        processes = windows_processes()
        import re
        pattern = r'--run-id\s+"?' + re.escape(run_id) + r'(?:"|\s|$)'
        for process in processes:
            command = process.get("CommandLine")
            if not command or re.search(pattern, command):
                raise RuntimeError("recovery refused: active acquisition or inaccessible process details")
        cfg = load_config(Path(__file__).resolve().parents[1] / "configs" / "default.yaml")
        candidates = [Path(cfg.storage.output_root)/run_id, Path(cfg.storage.output_root)/"test"/run_id]
        path = next((p for p in candidates if (p/"manifest.json").is_file()), None)
        if path is None:
            raise RuntimeError("legacy run not found")
    manifest = read_json(path / "manifest.json")
    if manifest.get("status") == "completed":
        raise RuntimeError("recovery refused: run is already completed")
    manifest.update(status="interrupted", archive_status="archive_error", ended_at=utc_now_iso(),
                    error="manual_recovery_after_process_exit")
    write_json(path / "manifest.json", manifest)
    write_json(path / "latest_status.json", dict(read_json(path / "latest_status.json"),
               state="ERROR", generated_at_epoch=time.time()))
    if spec:
        (session / "EXPORT").touch()
        cfg = load_config(Path(spec["config"]) if spec.get("config") else Path(__file__).resolve().parents[1]/"configs"/"default.yaml")
        destination = Path(cfg.storage.output_root)/("test" if spec.get("test") else "")/run_id
        with open(session/"worker.log", "ab") as log:
            proc = subprocess.Popen([sys.executable, "-u", "-m", "gpu_power_monitor.supervisor",
                                     "export", str(session), str(destination)],
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        write_json(session/"owner.json", {"exporter": proc.pid})
        write_json(session/"status.json", {"state": "interrupted", "worker_stopped": True,
                   "reason": "manual_recovery_after_process_exit", "storage_status": "pending"})
    return {"state": "interrupted", "worker_stopped": True, "reason": manifest["error"]}


def control(run_id: str, action: str, reason: str = "user_stop") -> dict:
    session = session_path(run_id)
    if action == "recover":
        return recover(run_id)
    if not (session / "spec.json").exists():
        return {"state": "unknown", "reason": "no_local_session"}
    # Independent files avoid a heartbeat overwriting a concurrent stop.
    if action == "heartbeat":
        control_data = read_json(session / "control.json")
        control_data["heartbeat"] = uuid.uuid4().hex
        write_json(session / "control.json", control_data)
    elif action == "stop":
        try:
            with open(session / "stop.json", "x", encoding="utf-8") as handle:
                json.dump({"reason": reason}, handle)
        except FileExistsError:
            pass
    elif action != "status":
        raise ValueError("unknown control action")
    state = read_json(session / "status.json") or {"state": "starting"}
    owner = read_json(session / "owner.json")
    if owner.get("supervisor") and state.get("state") not in TERMINAL and not pid_alive(owner["supervisor"]):
        # Do not claim the worker is dead merely because its supervisor died.
        (session / "data").mkdir(exist_ok=True)
        write_json(session / "data" / "STOP", {"reason": "supervisor_lost"})
        return {"state": "error", "reason": "supervisor_lost", "worker_stopped": False}
    return state


def spawn(encoded: str) -> int:
    """Start outside OpenSSH's Windows job via WMI, then return immediately.

    WMI owns the process, so closing the launch SSH connection cannot kill
    the supervisor while leaving its acquisition child alive.
    """
    spec = json.loads(base64.b64decode(encoded))
    session = session_path(spec["run_id"])
    if session.exists():
        raise RuntimeError("local session already exists")
    command = subprocess.list2cmdline([sys.executable, "-u", "-m",
                                      "gpu_power_monitor.supervisor", "launch", encoded])
    if os.name == "nt":
        def quote(value): return "'" + str(value).replace("'", "''") + "'"
        ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{"
              f"CommandLine={quote(command)}; CurrentDirectory={quote(Path(__file__).resolve().parents[1])}"
              "}; if ($r.ReturnValue -ne 0) { throw ('WMI process creation failed: '+$r.ReturnValue) }; "
              "$r.ProcessId")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                                capture_output=True, text=True, timeout=12)
        if result.returncode:
            raise RuntimeError(result.stderr or "cannot detach Windows supervisor")
        print(result.stdout.strip(), flush=True)
    else:
        proc = subprocess.Popen([sys.executable, "-u", "-m", "gpu_power_monitor.supervisor", "launch", encoded],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        print(proc.pid, flush=True)
    return 0


def main() -> int:
    action, *args = sys.argv[1:]
    if action == "spawn":
        return spawn(args[0])
    if action == "launch":
        spec = json.loads(base64.b64decode(args[0]))
        root = session_path(spec["run_id"]).parent
        root.mkdir(parents=True, exist_ok=True)
        # WMI-created processes have no terminal handles. Keep errors locally.
        with open(root / (spec["run_id"] + ".supervisor.log"), "a", buffering=1) as log:
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = log
            try:
                return launch(spec)
            except BaseException:
                import traceback
                traceback.print_exc()
                raise
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
    if action == "worker":
        session = Path(args[0])
        return worker(read_json(session / "spec.json"), session)
    if action == "export":
        return export(Path(args[0]), Path(args[1]))
    if action == "control":
        payload = json.loads(base64.b64decode(args[0]))
        print(json.dumps(control(**payload)), flush=True)
        return 0
    raise ValueError("unknown supervisor command")


if __name__ == "__main__":
    raise SystemExit(main())
