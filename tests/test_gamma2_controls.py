"""Controls added for the gamma2 deployment, where acquisition runs headless in
a Windows VM and is driven from the Linux host: STOP-file stop, explicit run
ids, operator in the manifest, PAI_OUTPUT_ROOT honoured by the CLI, `latest`
run lookup, and the unattended `archive push-ready` sweep."""

import dataclasses
import json
import threading
import time

from gpu_power_monitor import cli
from gpu_power_monitor.acquisition import acquire
from gpu_power_monitor.config import AcquisitionConfig, RemoteConfig, load_config
from gpu_power_monitor.manifest import load_manifest, save_manifest
from gpu_power_monitor.utils import STOP_FILE_NAME, request_stop


def _fast_config(tmp_path, **overrides):
    config = AcquisitionConfig(sample_rate_hz=1000, chunk_size=100, **overrides)
    storage = dataclasses.replace(config.storage, output_root=tmp_path)
    return dataclasses.replace(config, storage=storage, remote=RemoteConfig(enabled=False))


def test_stop_file_ends_run_cleanly_and_is_removed(tmp_path):
    config = _fast_config(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def _stop_soon():
        time.sleep(0.6)
        request_stop(run_dir)

    threading.Thread(target=_stop_soon, daemon=True).start()
    started = time.time()
    acquire(config, simulate=True, run_dir=run_dir)  # no duration: only STOP ends it
    assert time.time() - started < 10, "STOP file should have ended the run"

    manifest = load_manifest(run_dir)
    assert manifest["status"] == "completed"
    assert manifest["archive_status"] == "ready_to_archive"
    assert not (run_dir / STOP_FILE_NAME).exists(), "marker must not survive the run"
    assert all(f["path"] != STOP_FILE_NAME for f in manifest["files"])


def test_operator_recorded_in_manifest(tmp_path):
    config = _fast_config(tmp_path, operator="ian")
    run_dir = acquire(config, simulate=True, duration_sec=0.3, run_dir=tmp_path / "run")
    assert load_manifest(run_dir)["operator"] == "ian"
    # Empty operator is recorded as null, not "".
    config = _fast_config(tmp_path)
    run_dir = acquire(config, simulate=True, duration_sec=0.3, run_dir=tmp_path / "run2")
    assert load_manifest(run_dir)["operator"] is None


def test_operator_env_override(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sample_rate_hz: 1000\n", encoding="utf-8")
    monkeypatch.setenv("PAI_OPERATOR", "kevin")
    assert load_config(cfg).operator == "kevin"


def test_relative_output_root_resolves_inside_repo(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("storage:\n  output_root: output\n", encoding="utf-8")
    root = load_config(cfg).storage.output_root
    assert root.is_absolute()
    assert root == cli._repo_root() / "output"


def test_cli_output_root_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PAI_OUTPUT_ROOT", str(tmp_path / "share"))
    assert cli._output_root() == tmp_path / "share"


def test_run_id_creates_exact_folder_and_refuses_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("PAI_OUTPUT_ROOT", str(tmp_path))
    rc = cli.run_hardware(
        simulate=True, display=False, run_id="resnet_sweep_20261010_120000",
        operator="ian", duration_sec=0.3,
    )
    assert rc == 0
    run_dir = tmp_path / "resnet_sweep_20261010_120000"
    assert run_dir.is_dir(), "run folder must be exactly the given id (no timestamp appended)"
    manifest = load_manifest(run_dir)
    assert manifest["measurement_name"] == "resnet_sweep_20261010_120000"
    assert manifest["operator"] == "ian"
    # Same id again: refused, and the first run is untouched.
    rc = cli.run_hardware(
        simulate=True, display=False, run_id="resnet_sweep_20261010_120000", duration_sec=0.3
    )
    assert rc == 1
    assert load_manifest(run_dir)["status"] == "completed"
    # Illegal folder characters are refused up front.
    assert cli.run_hardware(simulate=True, display=False, run_id="bad/name", duration_sec=0.3) == 1


def test_resolve_latest_and_stop_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PAI_OUTPUT_ROOT", str(tmp_path))
    older = tmp_path / "GPU Run 0_20260101_000000"
    newer = tmp_path / "GPU Run 1_20260102_000000"
    for d in (older, newer):
        d.mkdir()
        save_manifest(d, {"measurement_name": d.name, "run_id": d.name, "status": "running"})
        time.sleep(0.05)
    assert cli._resolve_run("latest") == newer
    assert cli.run_stop("latest") == 0
    assert (newer / STOP_FILE_NAME).exists()
    assert not (older / STOP_FILE_NAME).exists()
    assert cli.run_stop("no-such-run") == 1


def test_push_ready_sweeps_only_waiting_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("PAI_OUTPUT_ROOT", str(tmp_path))
    states = {
        "waiting_a": ("completed", "ready_to_archive", "archive"),
        "waiting_b": ("completed", "push_failed", "archive"),
        "already": ("completed", "archived_verified", "archive"),
        "running": ("running", "local_only", "archive"),
        "scratch": ("completed", "local_only", "test"),
    }
    for name, (status, archive_status, kind) in states.items():
        d = tmp_path / name
        d.mkdir()
        save_manifest(d, {
            "measurement_name": name, "run_id": name, "status": status,
            "archive_status": archive_status, "run_kind": kind,
        })
    pushed = []
    from gpu_power_monitor import globus_push

    monkeypatch.setattr(globus_push, "globus_unready_reason", lambda cfg: None)
    monkeypatch.setattr(globus_push, "push_run", lambda run, cfg, root: pushed.append(run.name) or "task-1")
    assert cli.run_push_ready() == 0
    assert sorted(pushed) == ["waiting_a", "waiting_b"]

    # Nothing waiting is a quiet success; Globus not ready is an error only when there is work.
    pushed.clear()
    for name in ("waiting_a", "waiting_b"):
        data = load_manifest(tmp_path / name)
        data["archive_status"] = "transfer_pending"
        save_manifest(tmp_path / name, data)
    assert cli.run_push_ready() == 0
    assert pushed == []


def test_dot_folders_are_not_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("PAI_OUTPUT_ROOT", str(tmp_path))
    run = tmp_path / "GPU Run 0_20260101_000000"
    run.mkdir()
    save_manifest(run, {"measurement_name": run.name, "run_id": run.name, "status": "completed"})
    time.sleep(0.05)
    (tmp_path / ".pai").mkdir()              # newer than the run, but tooling state
    (tmp_path / "test" / ".cache").mkdir(parents=True)
    assert [r.name for r in cli._list_runs()] == [run.name]
    assert cli._resolve_run("latest") == run


def test_archive_status_unknown_run_is_clean(tmp_path, monkeypatch, capsys):
    from gpu_power_monitor import cli

    monkeypatch.setattr(cli, "_output_root", lambda: tmp_path)
    assert cli.run_archive("status", "gamma2_idle_baseline_*") == 1
    assert "No such run" in capsys.readouterr().out


def test_resolve_run_accepts_unique_glob(tmp_path, monkeypatch):
    from gpu_power_monitor import cli

    (tmp_path / "gamma2_idle_baseline_20261014_023221").mkdir()
    monkeypatch.setattr(cli, "_output_root", lambda: tmp_path)
    assert cli._resolve_run("gamma2_idle_baseline_*").name == "gamma2_idle_baseline_20261014_023221"


def test_workload_gets_run_dir_env(tmp_path, monkeypatch):
    import subprocess

    from gpu_power_monitor import cli

    seen = {}

    def fake_run(cmd, env=None):
        seen["env"] = env
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_dir = tmp_path / "demo_run"
    assert cli._run_workload(["true"], run_dir) == 0
    assert seen["env"]["PAI_RUN_DIR"] == str(run_dir)
    assert seen["env"]["PAI_RUN_ID"] == "demo_run"
