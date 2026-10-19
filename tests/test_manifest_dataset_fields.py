import json

from gpu_power_monitor import manifest as m


GAMMA_INFO = """GPU 0: NVIDIA TITAN RTX (UUID: GPU-aaa)
GPU 1: NVIDIA TITAN RTX (UUID: GPU-bbb)
index, name, driver_version, vbios_version, power.limit [W]
0, NVIDIA TITAN RTX, 550.120, 90.02.0B.00.01, 280.00 W
1, NVIDIA TITAN RTX, 550.120, 90.02.0B.00.01, 280.00 W
"""


def test_parse_gpu_info(tmp_path):
    path = tmp_path / "gamma_info.txt"
    path.write_text(GAMMA_INFO)
    gpus = m.parse_gpu_info(path)
    assert [g["index"] for g in gpus] == [0, 1]
    assert gpus[0]["name"] == "NVIDIA TITAN RTX"
    assert gpus[0]["driver_version"] == "550.120"
    assert gpus[1]["power_limit_w"] == 280.0
    assert m.parse_gpu_info(tmp_path / "missing.txt") == []


def test_workload_meta_and_gpus_land_in_manifest(tmp_path):
    run = tmp_path / "run"
    (run / "workload").mkdir(parents=True)
    (run / "nvml").mkdir()
    (run / "workload" / "meta.json").write_text(json.dumps({"workload": "nhpp-load", "model": "qwen7b"}))
    (run / "nvml" / "gamma_info.txt").write_text(GAMMA_INFO)
    m.save_manifest(run, m.RunManifest(measurement_name="x", run_id="run", machine="gamma2"))
    data = m.finalize_manifest(run, end_time_s=1.0)
    assert data["machine"] == "gamma2"
    assert data["workload"] == {"workload": "nhpp-load", "model": "qwen7b"}
    assert data["gpus"][1]["name"] == "NVIDIA TITAN RTX"
    assert data["manifest_version"] == 2
    assert data["status"] == "completed"


def test_finalize_without_meta_is_unchanged(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    m.save_manifest(run, m.RunManifest(measurement_name="x", run_id="run"))
    data = m.finalize_manifest(run, end_time_s=1.0)
    assert data.get("workload") is None and data["gpus"] == []
