import dataclasses
import gzip
import json
from pathlib import Path
import shutil

import pytest

from gpu_power_monitor import reconcile as repair
from gpu_power_monitor.config import AcquisitionConfig
from gpu_power_monitor.supervisor import read_json,write_json


@pytest.fixture
def failed(tmp_path,monkeypatch):
    session=tmp_path/'spool'/'failed_run';data=session/'data';data.mkdir(parents=True)
    destination=tmp_path/'destination'/'failed_run'
    raw=data/'chunk.csv';raw.write_text('time_s,total_power_w\n0,24\n1,24\n')
    gz=data/'chunk.csv.gz';gz.write_bytes(gzip.compress(raw.read_bytes()))
    write_json(data/'manifest.json',{'run_id':'data','status':'error','error':'acquisition_failed','stats':{'sample_count':4}})
    write_json(session/'spec.json',{'run_id':'failed_run'})
    write_json(session/'status.json',{'state':'error','reason':None})
    write_json(session/'owner.json',{'worker':999999999})
    (session/'worker.log').write_text('Traceback\nRuntimeError: CSV writer failed: WinError 32 sharing violation\n')
    shutil.copytree(data,destination)
    write_json(destination/'manifest.json',{'status':'running'})
    cfg=AcquisitionConfig();cfg=dataclasses.replace(cfg,storage=dataclasses.replace(cfg.storage,output_root=tmp_path/'destination'))
    monkeypatch.setattr(repair,'session_path',lambda _:session)
    monkeypatch.setattr(repair,'verify_inactive',lambda _:None)
    monkeypatch.setattr(repair,'load_config',lambda _:cfg)
    return session,data,destination


def snapshots(root):return {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_inspection_is_read_only_and_verifies_duplicates(failed):
    session,data,destination=failed;before=snapshots(session.parent.parent)
    report=repair.reconcile('failed_run')
    assert report['canonical_chunks']==1 and len(report['validated_duplicate_pairs'])==1
    assert 'WinError 32' in report['worker_error'] and not report['applied']
    assert before==snapshots(session.parent.parent)


def test_apply_preserves_all_data_and_repairs_terminal_error(failed):
    session,data,destination=failed
    originals={p:p.read_bytes() for folder in (data,destination) for p in folder.glob('*.csv*')}
    report=repair.reconcile('failed_run',apply=True)
    assert report['applied']
    for folder in (data,destination):
        manifest=read_json(folder/'manifest.json')
        assert manifest['status']=='error' and manifest['run_id']=='failed_run'
        assert manifest['error'].endswith('WinError 32 sharing violation')
        assert manifest['stats']=={} and manifest['archive_status']=='archive_error'
        assert [item['path'] for item in manifest['files']]==['chunk.csv.gz']
        assert list(folder.glob('manifest.json.before-reconcile-*'))
    assert all(path.read_bytes()==content for path,content in originals.items())
    assert read_json(session/'status.json')['reason'].endswith('WinError 32 sharing violation')
    assert not list(data.glob('EXPORT'))
    # Repeating inspection must not inventory the metadata backups as dataset files.
    assert repair.reconcile('failed_run')['canonical_chunks']==1


def test_checksum_failure_never_repairs_metadata(failed):
    session,data,destination=failed;(destination/'chunk.csv.gz').write_bytes(b'corrupt')
    before=snapshots(session.parent.parent)
    with pytest.raises(RuntimeError,match='verification failed'):repair.reconcile('failed_run',apply=True)
    assert before==snapshots(session.parent.parent)


def test_reconcile_refuses_live_processes(failed,monkeypatch):
    def live(_):raise RuntimeError('worker still running')
    monkeypatch.setattr(repair,'verify_inactive',live)
    with pytest.raises(RuntimeError,match='still running'):repair.reconcile('failed_run',apply=True)


def test_reconcile_never_promotes_a_completed_run(failed):
    session,data,destination=failed;write_json(data/'manifest.json',{'status':'completed'})
    with pytest.raises(RuntimeError,match='failed or interrupted'):repair.reconcile('failed_run',apply=True)


def test_reconcile_prefers_original_manifest_error_over_cleanup_traceback(failed):
    session,data,destination=failed
    metadata=read_json(data/'manifest.json');metadata['error']='original DAQ error'
    write_json(data/'manifest.json',metadata)
    (session/'worker.log').write_text('RuntimeError: secondary cleanup error\n')
    assert repair.reconcile('failed_run')['worker_error']=='original DAQ error'
