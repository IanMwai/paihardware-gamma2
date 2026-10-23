"""`pai hardware` when the DAQ is in a VM on this host (gamma2).

The SSH launch is replaced by a local subprocess running the same
`pai hardware --simulate --run-id ...`, which is exactly what the VM runs,
minus the network. Everything else (waiting for the manifest, the STOP file,
waiting for completion, auto-push bookkeeping) is the real code path.
"""

import subprocess
import sys

import pytest

from gpu_power_monitor import cli, vm
from gpu_power_monitor.manifest import load_manifest


@pytest.fixture
def vm_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PAI_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setenv("PAI_VM_HOST", "vm.invalid")
    monkeypatch.setenv("PAI_VM_USER", "daq")
    monkeypatch.setenv("PAI_VM_PAI", "pai.exe")
    monkeypatch.setenv("PAI_DASHBOARD_HOST", "127.0.0.1")

    launched = []

    def fake_start(cfg, *, run_id, operator, machine, test, gpus, log_path, config_name=None, duration_sec=None, maximum_sec=None):
        launched.append({"run_id": run_id, "operator": operator, "machine": machine, "test": test, "gpus": gpus})
        args = [sys.executable, "-m", "gpu_power_monitor.cli", "hardware", "--simulate",
                "--run-id", run_id, "--operator", operator, "--machine", machine]
        if test:
            args.append("--test")
        if gpus:
            args += ["--gpus", gpus]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen(args, stdout=open(log_path, "ab"), stderr=subprocess.STDOUT)

    monkeypatch.setattr(vm, "start_acquisition", fake_start)
    return tmp_path, launched


def test_vm_mode_brackets_a_workload(vm_env):
    out, launched = vm_env
    rc = cli.run_hardware(
        simulate=False, display=False, name="vmtest", test=True, gpus="GPU1,GPU3",
        workload=[sys.executable, "-c", "import time; time.sleep(1.5)"],
    )
    assert rc == 0
    assert launched and launched[0]["test"] is True and launched[0]["gpus"] == "GPU1,GPU3"
    run_dir = out / "test" / launched[0]["run_id"]
    assert run_dir.name.startswith("vmtest_")
    manifest = load_manifest(run_dir)
    assert manifest["status"] == "completed"
    assert manifest["run_kind"] == "test"
    assert manifest["operator"] == launched[0]["operator"]
    assert manifest["machine"] == launched[0]["machine"] and manifest["machine"]
    assert [g["label"] for g in manifest["channels"]["gpus"]] == ["GPU1", "GPU3"]
    assert not (run_dir / "STOP").exists()
    assert (out / ".pai" / f"{run_dir.name}.log").exists()


def test_vm_mode_reports_a_run_that_never_starts(vm_env, monkeypatch):
    out, _ = vm_env

    def dead_start(cfg, *, run_id, operator, machine, test, gpus, log_path, config_name=None, duration_sec=None, maximum_sec=None):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ssh: connect to host vm.invalid port 22: Connection timed out\n")
        return subprocess.Popen([sys.executable, "-c", "raise SystemExit(255)"])

    monkeypatch.setattr(vm, "start_acquisition", dead_start)
    assert cli.run_hardware(simulate=False, display=False, name="never") == 1


def test_simulate_stays_local_even_with_vm_configured(vm_env):
    out, launched = vm_env
    rc = cli.run_hardware(simulate=True, display=False, name="local", duration_sec=0.3)
    assert rc == 0
    assert launched == []
    assert any(p.name.startswith("local_") for p in out.iterdir())


def test_local_workload_bracketing(tmp_path, monkeypatch):
    monkeypatch.setenv("PAI_OUTPUT_ROOT", str(tmp_path))
    rc = cli.run_hardware(
        simulate=True, display=False, name="bracket",
        workload=[sys.executable, "-c", "import time; time.sleep(0.5)"],
    )
    assert rc == 0
    run_dir = next(p for p in tmp_path.iterdir() if p.name.startswith("bracket_"))
    assert load_manifest(run_dir)["status"] == "completed"


def test_vm_config_enabled_rules(monkeypatch, tmp_path):
    from gpu_power_monitor.config import VmConfig, load_config

    assert not VmConfig().enabled
    assert VmConfig(user="daq", pai="pai.exe", name="daqwin").enabled
    assert VmConfig(user="daq", pai="pai.exe", host="10.0.0.5").enabled
    assert not VmConfig(user="daq", pai="pai.exe").enabled
    cfg = tmp_path / "c.yaml"
    cfg.write_text("sample_rate_hz: 1000\n", encoding="utf-8")
    monkeypatch.setenv("PAI_VM_NAME", "daqwin")
    monkeypatch.setenv("PAI_VM_USER", "daqvnc1")
    monkeypatch.setenv("PAI_VM_PAI", r"C:\pai\x\.venv\Scripts\pai.exe")
    monkeypatch.setenv("PAI_DASHBOARD_HOST", "0.0.0.0")
    c = load_config(cfg)
    assert c.vm.enabled and c.vm.name == "daqwin" and c.display.host == "0.0.0.0"


def test_vm_config_path_from_pai_exe():
    from gpu_power_monitor import vm
    from gpu_power_monitor.config import VmConfig

    cfg = VmConfig(name="daqwin", user="u", pai=r"C:\pai\paihardware-gamma2\.venv\Scripts\pai.exe")
    assert vm.vm_repo_root(cfg) == r"C:\pai\paihardware-gamma2"
    assert vm.vm_config_path(cfg, "lab.yaml") == r"C:\pai\paihardware-gamma2\configs\lab.yaml"


def test_vm_config_name_only_for_repo_configs(tmp_path):
    import pytest

    from gpu_power_monitor import cli

    assert cli._vm_config_name(None) is None
    assert cli._vm_config_name(cli._default_config()) is None
    assert cli._vm_config_name(cli._repo_root() / "configs" / "lab.yaml") == "lab.yaml"
    with pytest.raises(SystemExit):
        cli._vm_config_name(tmp_path / "elsewhere.yaml")
