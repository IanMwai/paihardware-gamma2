import dataclasses
import gzip
import json
from pathlib import Path
import pytest
from gpu_power_monitor.config import AcquisitionConfig, RemoteConfig, StorageConfig, VmConfig
from gpu_power_monitor import cli, remote, supervisor as s, vm
from gpu_power_monitor.manifest import build_file_inventory


def test_required_nvml_filters_to_physical_uuid(monkeypatch):
    commands=[]
    class Client:
        def close(self):pass
    monkeypatch.setattr(remote,'connect',lambda cfg:Client())
    def command(client,cmd,**kwargs):
        commands.append(cmd)
        return (0,'123\n','') if 'nohup' in cmd else (0,'','')
    monkeypatch.setattr(remote,'run_command',command)
    remote.NvmlLogger(RemoteConfig(gpu_ids=('GPU-abc',),required=True),'run').start()
    assert 'nvidia-smi -i GPU-abc --query-gpu=' in commands[0]
    assert 'timestamp,index,uuid,name' in commands[0]


def test_required_nvml_rejects_missing_and_unselected_gpu_samples(tmp_path):
    path=tmp_path/'nvml.csv'
    path.write_text('timestamp, index, uuid, power.draw [W]\n2026/10/24 03:00:00.000, 1, GPU-b, 42\n2026/10/24 03:00:01.000, 1, GPU-b, 43\n')
    data=remote.validate_nvml(path,('GPU-b',))
    assert data['observed_span_s']==1 and data['sample_counts']=={'GPU-b':2}
    with pytest.raises(remote.RemoteUnavailable):remote.validate_nvml(path,('GPU-a',))
    with pytest.raises(remote.RemoteUnavailable):remote.validate_nvml(path,('GPU-b','GPU-c'))
    path.write_text('timestamp, index, uuid, power.draw [W]\n')
    with pytest.raises(remote.RemoteUnavailable):remote.validate_nvml(path,('GPU-b',))


def test_nvml_count_must_equal_daq_selection(tmp_path,monkeypatch):
    cfg=dataclasses.replace(AcquisitionConfig(),storage=StorageConfig(output_root=tmp_path))
    monkeypatch.setattr('gpu_power_monitor.config.load_config',lambda *a:cfg)
    assert cli.run_hardware(simulate=False,display=False,gpus='GPU2',nvml_gpus='0,1')==2


def test_research_config_preserves_unsmoothed_samples():
    from gpu_power_monitor.config import load_config
    path=Path(__file__).resolve().parents[1]/'configs/research.yaml'
    config=load_config(path)
    assert config.processing.power_average_samples==1
    assert len(config.channels.gpus)==4


def prune_fixture(tmp_path,monkeypatch):
    session=tmp_path/'spool'/'run';data=session/'data';data.mkdir(parents=True)
    dest=tmp_path/'linux'/'run';dest.mkdir(parents=True)
    raw=data/'nidaq_test.csv';raw.write_bytes(b't_s,GPU2_P_W\n0,42\n')
    zipped=data/'nidaq_test.csv.gz'
    with gzip.open(zipped,'wb') as f:f.write(raw.read_bytes())
    (dest/zipped.name).write_bytes(zipped.read_bytes())
    # Retain extra Linux-only data as well as all diagnostics and manifests.
    (dest/'result.json').write_text('{}')
    s.write_json(session/'spec.json',{'run_id':'run'})
    s.write_json(data/'manifest.json',{'status':'completed'})
    s.write_json(dest/'manifest.json',{'status':'completed','storage':{'status':'verified'},
        'archive_status':'archived_verified','archive':{'verified_at':'2026-10-24','task_id':'globus-task'},
        'files':build_file_inventory(dest)})
    monkeypatch.setattr(s,'session_path',lambda run:session)
    monkeypatch.setattr(s,'verify_inactive',lambda path:None)
    cfg=dataclasses.replace(AcquisitionConfig(),storage=StorageConfig(output_root=dest.parent))
    monkeypatch.setattr('gpu_power_monitor.config.load_config',lambda *a:cfg)
    return session,data,dest


def test_reclamation_retains_linux_data_and_local_metadata(tmp_path,monkeypatch):
    session,data,dest=prune_fixture(tmp_path,monkeypatch)
    before={p.name:p.read_bytes() for p in dest.iterdir()}
    result=s.prune_spool('run')
    assert result['state']=='pruned' and not list(data.glob('*.csv*'))
    assert (data/'manifest.json').exists() and (session/'spec.json').exists()
    assert before=={p.name:p.read_bytes() for p in dest.iterdir()}
    assert s.prune_spool('run')['already_pruned']


@pytest.mark.parametrize('fault',['failed','pending','corrupt_linux','corrupt_spool','active'])
def test_reclamation_refuses_unverified_or_active_copies(tmp_path,monkeypatch,fault):
    session,data,dest=prune_fixture(tmp_path,monkeypatch)
    if fault=='failed':s.write_json(data/'manifest.json',{'status':'error'})
    elif fault=='pending':
        record=s.read_json(dest/'manifest.json');record['archive_status']='transfer_pending';s.write_json(dest/'manifest.json',record)
    elif fault=='corrupt_linux':(dest/'result.json').write_text('changed')
    elif fault=='corrupt_spool':(data/'nidaq_test.csv').write_text('changed')
    else:monkeypatch.setattr(s,'verify_inactive',lambda p:(_ for _ in ()).throw(RuntimeError('active')))
    with pytest.raises(RuntimeError):s.prune_spool('run')
    assert len(list(data.glob('*.csv*')))==2


def test_old_vm_cannot_silently_ignore_selected_nvml_scope(tmp_path,monkeypatch):
    from types import SimpleNamespace
    cfg=dataclasses.replace(AcquisitionConfig(),storage=StorageConfig(output_root=tmp_path),
        vm=VmConfig(host='vm.invalid',user='daq',pai='pai.exe'))
    monkeypatch.setattr('gpu_power_monitor.config.load_config',lambda *a:cfg)
    calls=[]
    lease=SimpleNamespace(state={'state':'ready','progress':{}},close=lambda:calls.append('closed'))
    monkeypatch.setattr(vm,'start_acquisition',lambda *a,**kw:SimpleNamespace(lease=lease))
    monkeypatch.setattr(vm,'wait_for_start',lambda *a:None)
    monkeypatch.setattr(vm,'control',lambda *a:calls.append(a[2]))
    monkeypatch.setattr(cli,'_run_workload',lambda *a,**kw:pytest.fail('started workload with unconfirmed scope'))
    assert cli.run_hardware(simulate=False,display=False,name='scope',gpus='GPU2',nvml_gpus='GPU-b',workload=['true'])==1
    assert calls==['stop','closed']


def test_progress_advertises_the_selected_measurement_scope(tmp_path):
    from gpu_power_monitor.acquisition import acquire
    from gpu_power_monitor.config import select_gpus, ProcessingConfig
    cfg=select_gpus(dataclasses.replace(AcquisitionConfig(),sample_rate_hz=100,chunk_size=10,
        processing=ProcessingConfig(power_average_samples=1),remote=RemoteConfig(gpu_ids=('GPU-b',))), 'GPU2')
    progress=tmp_path/'progress.json'
    acquire(cfg,simulate=True,duration_sec=.02,run_dir=tmp_path/'run',progress_path=progress)
    state=json.loads(progress.read_text())
    assert state['daq_labels']==['GPU2'] and state['nvml_gpus']==['GPU-b']
    assert state['power_average_samples']==1
