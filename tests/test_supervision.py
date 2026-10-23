import base64
import datetime as dt
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from gpu_power_monitor import cli, supervisor as sup, vm
from gpu_power_monitor.config import VmConfig
from gpu_power_monitor.logging_writer import ChunkWriter
from gpu_power_monitor.manifest import load_manifest
from gpu_power_monitor.processing import ProcessedBlock


def make_spec(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv('PAI_VM_SPOOL_ROOT', str(tmp_path/'spool'))
    monkeypatch.delenv('PAI_OUTPUT_ROOT', raising=False)
    cfg = tmp_path/'config.yaml'
    cfg.write_text(f'''sample_rate_hz: 1000
chunk_size: 100
remote:
  enabled: false
storage:
  output_root: {tmp_path / 'destination'}
logging:
  chunk_duration_sec: 0.2
''')
    return dict(run_id='supervised_run', config=str(cfg), simulate=True,
                maximum_sec=20, duration_sec=.3, **kwargs)


@pytest.mark.parametrize('cause,expected', [
    ('lease', 'controller_lost'), ('deadline', 'deadline_exceeded'),
    ('stall', 'acquisition_stalled'), ('stop', 'user_stop')])
def test_watchdog_causes_and_escalation(cause, expected):
    watch = sup.Watchdog(0, 5 if cause == 'deadline' else 1000)
    watch.step(0, {'heartbeat': 'a'}, {'sequence': 1})
    now = 61 if cause in ('lease','stall') else 6
    control = {'heartbeat': 'a' if cause == 'lease' else 'b'}
    if cause == 'stop': control['stop_reason'] = 'user_stop'
    reason, force = watch.step(now, control, {'sequence': 1 if cause=='stall' else 2})
    assert reason == expected and not force
    assert watch.step(now+30, control, {'sequence': 3}) == (expected, True)


def test_heartbeat_does_not_mask_stalled_acquisition():
    watch = sup.Watchdog(0, 1000)
    watch.step(0, {'heartbeat':'a'}, {'sequence':1})
    reason, force = watch.step(61, {'heartbeat':'b'}, {'sequence':1})
    assert reason == 'acquisition_stalled'


def test_finalization_is_bounded_without_sample_progress():
    watch = sup.Watchdog(0, 1000)
    watch.step(0, {'heartbeat':'a', 'stop_reason':'workload_completed'}, {'sequence':1,'phase':'finalizing'})
    assert not watch.step(100, {'heartbeat':'b'}, {'sequence':1,'phase':'finalizing'})[1]
    assert watch.step(300, {'heartbeat':'c'}, {'sequence':1,'phase':'finalizing'})[1]


def test_real_supervised_simulation_exports_verified_chunks(tmp_path, monkeypatch):
    spec = make_spec(tmp_path, monkeypatch)
    assert sup.launch(spec) == 0
    session = sup.session_path(spec['run_id'])
    state = sup.read_json(session/'status.json')
    data = load_manifest(tmp_path/'destination'/spec['run_id'])
    assert state['state']=='completed' and state['storage_status']=='verified'
    assert data['status']=='completed' and data['storage']['status']=='verified'
    assert data['stats']['sample_count']>0
    assert data['stats']['sample_count']==sup.read_json(session/'progress.json')['written_samples']
    for item in data['files']:
        from gpu_power_monitor.manifest import sha256_file
        assert sha256_file(tmp_path/'destination'/spec['run_id']/item['path'])==item['sha256']
    assert (session/'data'/'manifest.json').exists()  # never delete the spool automatically


def test_real_controller_loss_is_interruption_not_success(tmp_path, monkeypatch):
    spec = make_spec(tmp_path, monkeypatch)
    spec['duration_sec'] = None
    monkeypatch.setattr(sup, 'LEASE_SECONDS', .1)
    assert sup.launch(spec)==1
    data = load_manifest(tmp_path/'destination'/spec['run_id'])
    assert data['status']=='interrupted'
    assert data['supervision']['reason']=='controller_lost'
    assert data['archive_status']=='archive_error'


def test_stop_and_heartbeat_use_independent_files(tmp_path, monkeypatch):
    spec = make_spec(tmp_path, monkeypatch)
    path = sup.session_path(spec['run_id']); path.mkdir(parents=True)
    sup.write_json(path/'spec.json',spec)
    sup.control(spec['run_id'], 'stop')
    sup.control(spec['run_id'], 'heartbeat')
    assert sup.read_json(path/'stop.json')['reason']=='user_stop'
    assert sup.read_json(path/'control.json')['heartbeat']


def test_disk_space_preflight_refuses_before_any_process(tmp_path, monkeypatch):
    spec = make_spec(tmp_path, monkeypatch)
    monkeypatch.setattr(sup.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    monkeypatch.setattr(sup.subprocess, 'Popen', lambda *a,**kw: pytest.fail('started without space'))
    assert sup.launch(spec)==1
    assert sup.read_json(sup.session_path(spec['run_id'])/'status.json')['reason']=='insufficient_spool_space'


def test_blocked_export_retains_local_data_and_times_out(tmp_path, monkeypatch):
    spec = make_spec(tmp_path, monkeypatch)
    session = sup.session_path(spec['run_id']); (session/'data').mkdir(parents=True)
    (session/'EXPORT').touch()
    (session/'data'/'chunk.csv').write_text('partial evidence')
    sup.write_json(session/'data'/'manifest.json', {'status':'error'})
    clock=SimpleNamespace(now=1.)
    monkeypatch.setattr(sup, 'EXPORT_SECONDS', 1)
    monkeypatch.setattr(sup.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(sup.time, 'sleep', lambda delay: setattr(clock,'now',clock.now+delay))
    monkeypatch.setattr(sup,'copy_verified',lambda *a: (_ for _ in ()).throw(OSError('share down')))
    assert sup.export(session,tmp_path/'destination')==1
    assert (session/'data'/'chunk.csv').read_text()=='partial evidence'
    assert sup.read_json(session/'export.json')['status']=='retrying'


def test_unresponsive_worker_is_killed_and_never_completed(tmp_path, monkeypatch):
    spec = make_spec(tmp_path, monkeypatch); spec['duration_sec']=None
    clock=SimpleNamespace(now=0.)
    class Proc:
        pid=99999999
        returncode=None
        def poll(self): return self.returncode
    procs=[]
    def popen(*a,**kw):
        p=Proc(); procs.append(p); return p
    monkeypatch.setattr(sup.subprocess,'Popen',popen)
    monkeypatch.setattr(sup.time,'monotonic',lambda:clock.now)
    monkeypatch.setattr(sup.time,'sleep',lambda delay:setattr(clock,'now',clock.now+delay))
    killed=[]
    def kill(p):
        if p.returncode is None: killed.append(p); p.returncode=-9
    monkeypatch.setattr(sup,'kill_tree',kill)
    monkeypatch.setattr(sup,'EXPORT_SECONDS',1)
    assert sup.launch(spec)==1
    assert procs[0] in killed
    manifest=load_manifest(sup.session_path(spec['run_id'])/'data')
    assert manifest['status']!='completed' and manifest['supervision']['forced']


def block():
    a=np.array([1.,2.])
    return ProcessedBlock(time_s=a,labels=['GPU1'],voltage_v=[a],current_a=[a],power_w=[a],total_power_w=a)


def test_writer_queue_full_fails_instead_of_dropping(tmp_path, monkeypatch):
    entered, release=threading.Event(),threading.Event()
    writer=ChunkWriter(tmp_path,'test',10,dt.datetime.now(),['GPU1'],queue_blocks=1)
    original=writer._write_block_locked
    def delayed(value):
        entered.set(); release.wait(2); original(value)
    monkeypatch.setattr(writer,'_write_block_locked',delayed)
    try:
        writer.write_block(block()); assert entered.wait(1)
        writer.write_block(block())
        with pytest.raises(RuntimeError,match='avoid silent data loss'): writer.write_block(block())
    finally:
        release.set(); writer.finalize(3)
    assert writer.samples_written==4


def test_writer_exception_reaches_finalize_without_deadlock(tmp_path,monkeypatch):
    writer=ChunkWriter(tmp_path,'test',10,dt.datetime.now(),['GPU1'])
    monkeypatch.setattr(writer,'_write_block_locked',lambda value: (_ for _ in ()).throw(OSError('disk full')))
    writer.write_block(block())
    with pytest.raises(RuntimeError,match='disk full'): writer.finalize(2)


def test_writer_shutdown_timeout_is_bounded(tmp_path,monkeypatch):
    entered,release=threading.Event(),threading.Event()
    writer=ChunkWriter(tmp_path,'test',10,dt.datetime.now(),['GPU1'])
    original=writer._write_block_locked
    def delayed(value): entered.set(); release.wait(2); original(value)
    monkeypatch.setattr(writer,'_write_block_locked',delayed)
    writer.write_block(block()); assert entered.wait(1)
    try:
        with pytest.raises(TimeoutError): writer.finalize(2,timeout_sec=.01)
    finally:
        release.set(); writer.thread.join(2)


def test_stop_timeout_returns_failure_not_fake_ack(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('PAI_OUTPUT_ROOT',str(tmp_path))
    monkeypatch.delenv('PAI_VM_HOST',raising=False)
    run=tmp_path/'stale';run.mkdir();(run/'manifest.json').write_text('{"status":"running"}')
    assert cli.run_stop('stale',timeout_sec=0)==1
    assert 'unconfirmed' in capsys.readouterr().out


def test_recovery_refuses_active_supervisor(tmp_path,monkeypatch):
    spec=make_spec(tmp_path,monkeypatch)
    session=sup.session_path(spec['run_id']); session.mkdir(parents=True)
    sup.write_json(session/'spec.json',spec);sup.write_json(session/'owner.json',{'worker':12})
    monkeypatch.setattr(sup,'pid_alive',lambda pid: bool(pid))
    with pytest.raises(RuntimeError,match='still running'):sup.recover(spec['run_id'])


def test_ps_launch_quotes_paths_without_variable_expansion():
    cfg=VmConfig(host='vm',user='daq',pai=r"C:\pai space\.venv\Scripts\pai.exe")
    command=vm._supervisor_command(cfg,'launch',{'run_id':'hello'})
    decoded=base64.b64decode(command.split()[-1]).decode('utf-16le')
    assert "& 'C:\\pai space\\.venv\\Scripts\\python.exe'" in decoded
    assert 'gpu_power_monitor.supervisor launch' in decoded


@pytest.mark.parametrize('value',[-1,float('inf'),float('nan')])
def test_invalid_deadlines_rejected(value):
    assert cli.run_hardware(simulate=True,display=False,workload_seconds=value)==2


def test_network_spool_is_rejected(monkeypatch):
    monkeypatch.setenv('PAI_VM_SPOOL_ROOT',r'\\192.168.122.1\pai\spool')
    with pytest.raises(ValueError,match='network share'):sup.local_root()


def test_stop_reason_cannot_be_overwritten_by_workload_completion(tmp_path,monkeypatch):
    spec=make_spec(tmp_path,monkeypatch)
    path=sup.session_path(spec['run_id']);path.mkdir(parents=True)
    sup.write_json(path/'spec.json',spec)
    sup.control(spec['run_id'],'stop','user_stop')
    sup.control(spec['run_id'],'stop','workload_completed')
    assert sup.read_json(path/'stop.json')['reason']=='user_stop'


def test_dead_supervisor_is_not_reported_live(tmp_path,monkeypatch):
    spec=make_spec(tmp_path,monkeypatch)
    path=sup.session_path(spec['run_id']);path.mkdir(parents=True)
    sup.write_json(path/'spec.json',spec)
    sup.write_json(path/'status.json',{'state':'ready'})
    sup.write_json(path/'owner.json',{'supervisor':100})
    monkeypatch.setattr(sup,'pid_alive',lambda _:False)
    state=sup.control(spec['run_id'],'status')
    assert state['state']=='error' and not state['worker_stopped']
    assert sup.read_json(path/'data'/'STOP')['reason']=='supervisor_lost'


def test_windows_spawn_uses_wmi_process_creation(tmp_path,monkeypatch):
    import os
    spec=make_spec(tmp_path,monkeypatch)
    recorded=[]
    monkeypatch.setattr(sup,'local_root',lambda:tmp_path/'spool')
    monkeypatch.setattr(sup,'os',SimpleNamespace(name='nt',environ=os.environ))
    def run(args,**kw):
        recorded.append(args)
        return SimpleNamespace(returncode=0,stdout='1234',stderr='')
    monkeypatch.setattr(sup.subprocess,'run',run)
    assert sup.spawn(base64.b64encode(json.dumps(spec).encode()).decode())==0
    assert 'Invoke-CimMethod -ClassName Win32_Process' in recorded[0][-1]
    assert 'gpu_power_monitor.supervisor launch' in recorded[0][-1]


def test_writer_startup_failure_kills_already_started_worker(tmp_path,monkeypatch):
    spec=make_spec(tmp_path,monkeypatch)
    proc=SimpleNamespace(pid=999999,returncode=None)
    calls=[];killed=[]
    def popen(*a,**kw):
        calls.append(1)
        if len(calls)>1:raise OSError('cannot start exporter')
        return proc
    monkeypatch.setattr(sup.subprocess,'Popen',popen)
    monkeypatch.setattr(sup,'kill_tree',lambda p:killed.append(p))
    with pytest.raises(OSError,match='exporter'):sup.launch(spec)
    assert killed==[proc]


def test_verified_export_cache_rechecks_changed_destination(tmp_path,monkeypatch):
    spec=make_spec(tmp_path,monkeypatch)
    session=sup.session_path(spec['run_id']); data=session/'data';data.mkdir(parents=True)
    chunk=data/'chunk.csv';chunk.write_text('verified data')
    from gpu_power_monitor.manifest import sha256_file
    checksum=sha256_file(chunk)
    sup.write_json(data/'manifest.json',{'status':'completed','files':[{'path':'chunk.csv','sha256':checksum}]})
    (session/'EXPORT').touch()
    destination=tmp_path/'destination'
    assert sup.export(session,destination)==0
    calls=[];original=sup.copy_verified
    def checked(*args): calls.append(args);return original(*args)
    monkeypatch.setattr(sup,'copy_verified',checked)
    assert sup.export(session,destination)==0 and calls==[]
    (destination/'chunk.csv').write_text('corrupted copy')
    assert sup.export(session,destination)==0 and len(calls)==1
    assert (destination/'chunk.csv').read_text()=='verified data'


def test_manifest_hash_deadline_is_bounded(tmp_path):
    import time
    from gpu_power_monitor.manifest import sha256_file
    path=tmp_path/'file';path.write_bytes(b'content')
    with pytest.raises(TimeoutError,match='verification deadline'):sha256_file(path,deadline=time.monotonic()-1)


def test_supervisor_loss_is_not_a_stop_acknowledgment(tmp_path,monkeypatch,capsys):
    from gpu_power_monitor.config import AcquisitionConfig
    cfg=AcquisitionConfig(vm=VmConfig(host='vm',user='daq',pai='pai.exe'))
    run=tmp_path/'run';run.mkdir()
    monkeypatch.setattr(cli,'_resolve_run',lambda _:run)
    monkeypatch.setattr('gpu_power_monitor.config.load_config',lambda _:cfg)
    monkeypatch.setattr(vm,'control',lambda *a,**kw:{'state':'error','reason':'supervisor_lost','worker_stopped':False})
    assert cli.run_stop('run')==1
    text=capsys.readouterr().out
    assert 'stop is unconfirmed' in text and 'Stopped and finalized' not in text


@pytest.mark.parametrize('command', [None, 'python pai.exe hardware --run-id old_run', 'python other.py'])
def test_legacy_recovery_requires_process_proof(tmp_path,monkeypatch,command):
    import os
    from dataclasses import replace
    from gpu_power_monitor.config import AcquisitionConfig
    cfg=AcquisitionConfig();cfg=replace(cfg,storage=replace(cfg.storage,output_root=tmp_path))
    run=tmp_path/'old_run';run.mkdir()
    sup.write_json(run/'manifest.json',{'run_id':'old_run','status':'running'})
    monkeypatch.setattr(sup,'local_root',lambda:tmp_path/'spool')
    monkeypatch.setattr(sup,'os',SimpleNamespace(name='nt',environ=os.environ))
    monkeypatch.setattr(sup,'windows_processes',lambda:[{'CommandLine':command}])
    monkeypatch.setattr('gpu_power_monitor.config.load_config',lambda _:cfg)
    if command!='python other.py':
        with pytest.raises(RuntimeError,match='recovery refused'):sup.recover('old_run')
        assert load_manifest(run)['status']=='running'
    else:
        assert sup.recover('old_run')['state']=='interrupted'
        assert load_manifest(run)['status']=='interrupted'
        assert sup.read_json(run/'latest_status.json')['state']=='ERROR'
