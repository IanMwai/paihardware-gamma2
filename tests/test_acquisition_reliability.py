import dataclasses
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from gpu_power_monitor import acquisition, cli, daq, vm
from gpu_power_monitor.config import AcquisitionConfig, ChannelConfig, RemoteConfig, VmConfig
from gpu_power_monitor.live_buffer import LiveBuffer
from gpu_power_monitor.manifest import create_manifest, load_manifest, update_manifest
from gpu_power_monitor.processing import ProcessedBlock


def config(tmp_path):
    base = AcquisitionConfig(sample_rate_hz=1000, chunk_size=100)
    return dataclasses.replace(base, storage=dataclasses.replace(base.storage, output_root=tmp_path),
                               remote=RemoteConfig(enabled=False))


def test_startup_error_closes_source_and_records_error(tmp_path, monkeypatch):
    stopped = []
    class BrokenSource(daq.SimulatedDaqSource):
        def start(self):
            raise RuntimeError('resource reserved')
        def stop(self):
            stopped.append(True)
    monkeypatch.setattr(acquisition, 'SimulatedDaqSource', BrokenSource)
    with pytest.raises(RuntimeError, match='resource reserved'):
        acquisition.acquire(config(tmp_path), simulate=True, run_dir=tmp_path/'run')
    data = load_manifest(tmp_path/'run')
    assert data['status'] == 'error' and 'resource reserved' in data['error']
    assert stopped == [True]


def test_daq_start_failure_closes_partially_created_task():
    closed = []
    task = SimpleNamespace(ai_channels=SimpleNamespace(add_ai_voltage_chan=lambda *a, **kw: None),
                           timing=SimpleNamespace(cfg_samp_clk_timing=lambda **kw: None),
                           close=lambda: closed.append(True))
    def fail():
        raise RuntimeError('resource reserved')
    task.start = fail
    source = object.__new__(daq.NIDaqSource)
    source._nidaqmx = SimpleNamespace(Task=lambda: task)
    source._TerminalConfiguration = SimpleNamespace(RSE=1)
    source._AcquisitionType = SimpleNamespace(CONTINUOUS=1)
    source._Edge = SimpleNamespace(RISING=1)
    source.channels = ChannelConfig()
    source.sample_rate_hz, source.chunk_size, source.task = 10000, 1000, None
    with pytest.raises(RuntimeError, match='resource reserved'):
        source.start()
    assert closed == [True] and source.task is None


@pytest.mark.parametrize('status,workload_code,expected', [('error', 0, 1), ('completed', 2, 2), ('completed', 0, 0)])
def test_vm_result_requires_both_measurement_and_workload_success(tmp_path, monkeypatch, status, workload_code, expected):
    cfg = dataclasses.replace(config(tmp_path), vm=VmConfig(host='vm.invalid', user='daq', pai='pai.exe'))
    run = tmp_path/'run'
    monkeypatch.setattr(vm, 'start_acquisition', lambda *a, **kw: object())
    def started(*a, **kw):
        run.mkdir()
        create_manifest(run, cfg)
        update_manifest(run, status=status, error='DAQ overrun' if status == 'error' else None)
    monkeypatch.setattr(vm, 'wait_for_start', started)
    monkeypatch.setattr(vm, 'wait_for_end', lambda *a, **kw: status)
    monkeypatch.setattr(cli, '_run_workload', lambda *a, **kw: workload_code)
    pushed = []
    monkeypatch.setattr(cli, '_auto_push', lambda *a: pushed.append(True))
    rc = cli._run_hardware_in_vm(cfg, display=False, test=True, gpus=None, port=8000,
                                  run_id='run', explicit_dir=run, workload=['sleep', '1'])
    assert rc == expected
    assert bool(pushed) == (expected == 0)
    if workload_code:
        assert load_manifest(run)['workload_exit_code'] == workload_code
        assert load_manifest(run)['status'] == 'error'


def test_local_workload_exit_is_not_discarded(tmp_path, monkeypatch):
    monkeypatch.setenv('PAI_OUTPUT_ROOT', str(tmp_path))
    monkeypatch.setattr(cli, '_auto_push', lambda *a: pytest.fail('archived failed workload'))
    assert cli.run_hardware(simulate=True, display=False, name='failedworkload', test=True,
                             workload=[sys.executable, '-c', 'raise SystemExit(3)']) == 3
    run = next(p for p in (tmp_path/'test').iterdir() if p.is_dir())
    assert load_manifest(run)['status'] == 'error'


def block(start, n=1000):
    times = np.arange(start, start+n, dtype=float)/10000
    ones = np.ones(n)
    return ProcessedBlock(time_s=times, labels=['GPU1', 'GPU2'],
                          voltage_v=[ones*12, ones*12], current_a=[ones, ones*2],
                          power_w=[ones*12, ones*24], total_power_w=ones*36)


def test_preview_memory_is_bounded_but_stats_keep_full_rate(tmp_path, monkeypatch):
    live = LiveBuffer(tmp_path, 10000, 60, ['GPU1', 'GPU2'])
    monkeypatch.setattr(live, 'write_latest', lambda **kw: None)
    for start in range(0, 600000, 1000):
        value = block(start)
        if start == 0:
            value.total_power_w[7] = 999  # intentionally off the preview sampling grid
        live.append(value)
    assert len(live.time_s) <= 2500
    assert all(len(values) <= 2500 for values in live.power_w)
    assert live._run_count == 600000 and live._run_peak == 999
    assert live._energy_j == pytest.approx((600000*36 + 999-36)/10000)
    preview = live._downsample()
    assert len(preview['time_s']) <= 2500 and preview['time_s'][-1] == pytest.approx(59.9999)
    assert all(len(values) == len(preview['time_s']) for values in preview.values())


def test_snapshot_writes_are_throttled_and_terminal_state_forced(tmp_path, monkeypatch):
    from gpu_power_monitor import live_buffer
    now = SimpleNamespace(value=0.)
    monkeypatch.setattr(live_buffer.time, 'monotonic', lambda: now.value)
    live = LiveBuffer(tmp_path, 10000, 60, ['GPU1', 'GPU2'])
    writes = []
    monkeypatch.setattr(live, 'write_latest', lambda state: writes.append(state))
    live.append(block(0))
    now.value = .1
    live.append(block(1000))
    now.value = .6
    live.append(block(2000))
    live.mark_state('COMPLETE')
    assert writes == ['LIVE', 'LIVE', 'COMPLETE']
