import csv
import datetime as dt
import gzip
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from gpu_power_monitor import chunks, logging_writer as lw, supervisor as sup
from gpu_power_monitor.manifest import build_file_inventory, compute_run_stats
from gpu_power_monitor.processing import ProcessedBlock
from gpu_power_monitor.utils import chunk_paths


def raw_chunk(folder, name='chunk.csv', text='time_s,total_power_w\n0,24\n1,24\n'):
    folder.mkdir(parents=True, exist_ok=True)
    raw=folder/name;raw.write_text(text)
    return raw


def zipped(raw):
    path=raw.with_name(raw.name+'.gz');path.write_bytes(gzip.compress(raw.read_bytes()));return path


def block():
    times=np.array([0.,.1,.2]);v=np.ones(3)*12;i=np.ones(3)*2
    return ProcessedBlock(time_s=times,labels=['GPU1'],voltage_v=[v],current_a=[i],power_w=[v*i],total_power_w=v*i)


@pytest.mark.parametrize('temporary',[True,False])
def test_source_cleanup_lock_is_nonfatal_and_samples_count_once(tmp_path,monkeypatch,temporary):
    original=Path.unlink;attempts=[]
    def locked(path,*a,**kw):
        if path.name.endswith('.csv') and 'PENDING' not in path.name:
            attempts.append(path)
            if not temporary or len(attempts)<3:raise PermissionError('WinError 32 sharing violation')
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,'unlink',locked)
    writer=lw.ChunkWriter(tmp_path,'nidaq',1,dt.datetime.now(),['GPU1'],compress=True)
    writer.write_block(block());paths=writer.finalize(.3)
    assert writer.error is None and len(paths)==1 and paths[0].name.endswith('.csv.gz')
    assert chunk_paths(tmp_path)==paths
    assert compute_run_stats(tmp_path)['sample_count']==3
    assert len([r for r in build_file_inventory(tmp_path) if r['path'].endswith(('.csv','.gz'))])==1
    notices=chunks.ready_records(tmp_path)[0]['warnings']
    assert bool(notices)==(not temporary)
    assert len(attempts)==(3 if temporary else 5)


def test_intermediate_csv_not_visible_before_compression_settles(tmp_path,monkeypatch):
    entered,release=threading.Event(),threading.Event();original=lw._gzip_chunk
    def delayed(*a):entered.set();release.wait(3);return original(*a)
    monkeypatch.setattr(lw,'_gzip_chunk',delayed)
    writer=lw.ChunkWriter(tmp_path,'nidaq',1,dt.datetime.now(),['GPU1'],compress=True)
    writer.write_block(block());writer.stop_event.set()
    assert entered.wait(1)
    try:
        assert list(tmp_path.glob('*.csv'))
        assert chunk_paths(tmp_path)==[]
    finally:
        release.set()
    assert writer.finalize(.3)[0].suffix=='.gz'


def test_compression_failure_publishes_intact_plain_csv(tmp_path,monkeypatch):
    def broken(*a,**kw):raise OSError('compression unavailable')
    monkeypatch.setattr(gzip,'open',broken)
    writer=lw.ChunkWriter(tmp_path,'nidaq',1,dt.datetime.now(),['GPU1'],compress=True)
    writer.write_block(block());paths=writer.finalize(.3)
    assert len(paths)==1 and paths[0].suffix=='.csv' and writer.error is None
    assert chunks.ready_records(tmp_path)[0]['warnings'][0].startswith('compression_failed')
    assert compute_run_stats(tmp_path)['sample_count']==3


def test_duplicate_legacy_chunks_are_verified_and_counted_once(tmp_path):
    raw=raw_chunk(tmp_path);gz=zipped(raw)
    assert chunk_paths(tmp_path)==[gz]
    assert compute_run_stats(tmp_path)['sample_count']==2
    assert [item['path'] for item in build_file_inventory(tmp_path)]==[gz.name]


def test_corrupt_gzip_falls_back_to_original(tmp_path):
    raw=raw_chunk(tmp_path);gz=zipped(raw);gz.write_bytes(gz.read_bytes()[:-5])
    with pytest.warns(RuntimeWarning,match='Invalid gzip'):
        assert chunk_paths(tmp_path)==[raw]


def test_corrupt_gzip_without_original_is_integrity_failure(tmp_path):
    raw=raw_chunk(tmp_path);gz=zipped(raw);raw.unlink();gz.write_bytes(b'broken gzip')
    with pytest.raises(chunks.ChunkIntegrityError,match='no original'):chunk_paths(tmp_path)


def test_valid_but_conflicting_csv_and_gzip_fail(tmp_path):
    raw=raw_chunk(tmp_path);zipped(raw);raw.write_text('time_s,total_power_w\n0,99\n')
    with pytest.raises(chunks.ChunkIntegrityError,match='Conflicting'):chunk_paths(tmp_path)


def test_corrupted_published_file_is_not_exportable(tmp_path):
    chunks.begin_publication(tmp_path);raw=raw_chunk(tmp_path)
    chunks.publish(raw,[],source_digest=chunks.digest(raw));raw.write_text('changed data')
    with pytest.raises(chunks.ChunkIntegrityError,match='checksum'):chunk_paths(tmp_path)


def test_export_marker_mid_copy_reloads_terminal_manifest(tmp_path,monkeypatch):
    session=tmp_path/'session';data=session/'data';raw_chunk(data)
    sup.write_json(data/'manifest.json',{'status':'running'})
    original=sup.copy_verified
    calls=[]
    def copy(source,*args):
        result=original(source,*args);calls.append(source)
        sup.write_json(data/'manifest.json',{'status':'error','error':'original disk error'})
        (session/'EXPORT').touch()
        return result
    monkeypatch.setattr(sup,'copy_verified',copy)
    destination=tmp_path/'destination'
    assert sup.export(session,destination)==0
    manifest=sup.read_json(destination/'manifest.json')
    assert manifest['status']=='error' and manifest['error']=='original disk error'
    assert calls


def test_export_refuses_running_terminal_manifest(tmp_path,monkeypatch):
    session=tmp_path/'session';data=session/'data';raw_chunk(data)
    sup.write_json(data/'manifest.json',{'status':'running'});(session/'EXPORT').touch()
    clock=SimpleNamespace(value=1.)
    monkeypatch.setattr(sup.time,'monotonic',lambda:clock.value)
    monkeypatch.setattr(sup.time,'sleep',lambda delay:setattr(clock,'value',clock.value+delay))
    monkeypatch.setattr(sup,'EXPORT_SECONDS',1)
    assert sup.export(session,tmp_path/'destination')==1
    assert sup.read_json(session/'export.json')['status']!='verified'


def test_export_uses_only_published_representation(tmp_path,monkeypatch):
    session=tmp_path/'session';data=session/'data';data.mkdir(parents=True)
    chunks.begin_publication(data)
    raw=raw_chunk(data);gz=zipped(raw)
    chunks.publish(gz,['cleanup pending'],source_digest=chunks.digest(raw))
    sup.write_json(data/'manifest.json',{'status':'error'});(session/'EXPORT').touch()
    original=sup.copy_verified;seen=[]
    def copy(path,*a):seen.append(path);return original(path,*a)
    monkeypatch.setattr(sup,'copy_verified',copy)
    assert sup.export(session,tmp_path/'destination')==0
    assert raw not in seen and gz in seen and raw.exists()
    assert [r['path'] for r in sup.read_json(tmp_path/'destination'/'manifest.json')['files']]==[gz.name]


def test_supervised_worker_passes_actual_run_identity(tmp_path,monkeypatch):
    from gpu_power_monitor import acquisition
    from gpu_power_monitor.config import AcquisitionConfig
    captured=[]
    monkeypatch.setattr('gpu_power_monitor.config.load_config',lambda _:AcquisitionConfig())
    monkeypatch.setattr(acquisition,'acquire',lambda *a,**kw:captured.append(kw))
    for name in ['run_one','run_two']:
        assert sup.worker({'run_id':name},tmp_path/name)==0
    assert [kwargs['run_identity'] for kwargs in captured]==['run_one','run_two']


def test_supervisor_retains_worker_exception(tmp_path,monkeypatch):
    monkeypatch.setenv('PAI_VM_SPOOL_ROOT',str(tmp_path/'spool'))
    cfg=tmp_path/'config.yaml';cfg.write_text(f'storage:\n  output_root: {tmp_path/"destination"}\n')
    detail='CSV writer failed: WinError 32 locked original'
    session=tmp_path/'spool'/'run'
    class Proc:
        pid=99999999
        def __init__(self,code):self.returncode=code
        def poll(self):return self.returncode
    calls=[]
    def popen(*a,**kw):
        calls.append(1)
        if len(calls)==1:
            (session/'data').mkdir()
            sup.write_json(session/'data'/'manifest.json',{'status':'error','error':detail})
            return Proc(1)
        sup.write_json(session/'export.json',{'status':'verified'})
        return Proc(0)
    monkeypatch.setattr(sup.subprocess,'Popen',popen)
    assert sup.launch({'run_id':'run','config':str(cfg),'maximum_sec':10})==1
    manifest=sup.read_json(session/'data'/'manifest.json')
    assert manifest['error']==detail
    assert sup.read_json(session/'status.json')['reason']==detail
    assert manifest['supervision']['worker_error']==detail


def test_nvml_directories_and_manifest_ids_use_run_identity(tmp_path,monkeypatch):
    import dataclasses
    import time
    from gpu_power_monitor import acquisition,daq
    from gpu_power_monitor.config import AcquisitionConfig
    identities=[]
    class Source(daq.SimulatedDaqSource):
        def __init__(self,channels,rate,size):super().__init__(rate,size,gpus=channels.gpus)
        def available_samples(self):time.sleep(.005);return super().available_samples()
    class Logger:
        def __init__(self,config,run_id):identities.append(run_id)
        def start(self):pass
        def stop_and_fetch(self,path):return {'status':'fetched','files':[]}
    monkeypatch.setattr(acquisition,'NIDaqSource',Source)
    monkeypatch.setattr(acquisition,'remote_unready_reason',lambda _:None)
    monkeypatch.setattr(acquisition,'NvmlLogger',Logger)
    cfg=AcquisitionConfig(sample_rate_hz=1000,chunk_size=10)
    cfg=dataclasses.replace(cfg,logging=dataclasses.replace(cfg.logging,compress=False))
    for name in ['one','two']:
        folder=tmp_path/name/'data'
        acquisition.acquire(cfg,run_dir=folder,duration_sec=.02,run_identity=name)
        assert sup.read_json(folder/'manifest.json')['run_id']==name
    assert identities==['one','two']


@pytest.mark.skipif(__import__('os').name!='nt',reason='requires Windows file-sharing semantics')
def test_native_windows_delete_lock_retains_verified_gzip(tmp_path):
    import ctypes
    from ctypes import wintypes
    raw=raw_chunk(tmp_path);gz=raw.with_name(raw.name+'.gz')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=(wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,
                                wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
    # Permit reads/writes but deny deletion, reproducing WinError 32.
    handle=kernel.CreateFileW(str(raw),0x80000000,3,None,3,128,None)
    assert handle not in (None,ctypes.c_void_p(-1).value)
    try:
        canonical,notices,_=lw._gzip_chunk(raw,gz)
        assert canonical==gz and notices and raw.exists()
        assert chunks.validate_pair(raw,gz)
        assert compute_run_stats(tmp_path)['sample_count']==2
    finally:
        kernel.CloseHandle(handle)


def test_worker_exception_is_preserved_when_cleanup_also_fails(tmp_path,monkeypatch):
    import dataclasses
    from gpu_power_monitor import acquisition,daq
    from gpu_power_monitor.config import AcquisitionConfig,RemoteConfig
    class BrokenSource(daq.SimulatedDaqSource):
        def start(self):raise RuntimeError('original DAQ error')
    original=lw.ChunkWriter.finalize
    def broken_cleanup(self,*a,**kw):
        original(self,*a,**kw)
        raise OSError('secondary cleanup failure')
    monkeypatch.setattr(acquisition,'SimulatedDaqSource',BrokenSource)
    monkeypatch.setattr(lw.ChunkWriter,'finalize',broken_cleanup)
    cfg=AcquisitionConfig(remote=RemoteConfig(enabled=False))
    cfg=dataclasses.replace(cfg,storage=dataclasses.replace(cfg.storage,output_root=tmp_path))
    with pytest.raises(OSError,match='secondary cleanup'):
        acquisition.acquire(cfg,simulate=True,run_dir=tmp_path/'run')
    manifest=sup.read_json(tmp_path/'run'/'manifest.json')
    assert manifest['error']=='original DAQ error'
    assert manifest['cleanup_error']=='secondary cleanup failure'


def test_malformed_publication_is_integrity_error(tmp_path):
    chunks.begin_publication(tmp_path)
    (tmp_path/'.chunks'/'chunk.csv.json').write_text('{"path":"chunk.csv","logical_name":"chunk.csv"}')
    with pytest.raises(chunks.ChunkIntegrityError,match='metadata'):chunk_paths(tmp_path)
