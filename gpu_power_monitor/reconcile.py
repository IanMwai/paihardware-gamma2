"""Verify and reconcile a failed supervised run; inspection is the default.

Run on Windows after updating its checkout:
    python -m gpu_power_monitor.reconcile RUN_ID [--apply]
Never deletes or rewrites data chunks, and never promotes a failed run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import uuid

from .chunks import canonical_chunks, logical_name, digest, validate_pair
from .config import load_config
from .manifest import build_file_inventory, load_workload_meta
from .supervisor import read_json, session_path, verify_inactive, write_json
from .utils import utc_now_iso


def worker_exception(session: Path) -> str | None:
    # The log may be large; keep only the tail in memory.
    from collections import deque
    with open(session / "worker.log", encoding="utf-8", errors="replace") as handle:
        tail = deque(handle, maxlen=100)
    for line in reversed(tail):
        if re.match(r"^(?:[\w.]*Error|[\w.]*Exception):", line):
            return line.strip()
    return None


def reconcile(run_id: str, *, apply: bool = False) -> dict:
    session = session_path(run_id)
    spec = read_json(session / "spec.json")
    if spec.get("run_id") != run_id:
        raise RuntimeError("No matching supervised run specification")
    verify_inactive(session)
    data = session / "data"
    local = read_json(data / "manifest.json")
    if local.get("status") not in {"error", "interrupted"}:
        raise RuntimeError("Reconciliation requires a failed or interrupted local terminal manifest")
    cfg = load_config(Path(spec["config"]) if spec.get("config") else Path(__file__).resolve().parents[1]/"configs"/"default.yaml")
    destination = Path(cfg.storage.output_root) / ("test" if spec.get("test") else "") / run_id
    if not (destination / "manifest.json").is_file():
        raise RuntimeError("Linux copy is missing; preserve the spool and export it before reconciling")
    canonical = canonical_chunks(data)
    pairs = []
    for chunk in canonical:
        name = logical_name(chunk)
        if (data/name).exists() and (data/(name+'.gz')).exists():
            pairs.append({"logical_name": name, "canonical": chunk.name,
                          "gzip_integrity": "matching" if validate_pair(data/name,data/(name+".gz")) else "invalid; CSV fallback"})
    files = build_file_inventory(data)
    for item in files:
        other = destination/item['path']
        if not other.is_file() or other.stat().st_size != item['size_bytes'] or digest(other) != item['sha256']:
            raise RuntimeError(f"Linux checksum verification failed: {item['path']}; no metadata changed")
    # Validate any duplicates present only in the Linux copy too.
    exported_chunks = canonical_chunks(destination)
    if {logical_name(p) for p in exported_chunks} != {logical_name(p) for p in canonical}:
        raise RuntimeError("Linux copy has missing or unexpected logical chunks; no metadata changed")
    linux_workload = []
    if (destination/'workload').is_dir():
        linux_workload = [dict(item,path='workload/'+item['path'])
                          for item in build_file_inventory(destination/'workload')]
    original_error = str(local.get('error') or '').strip()
    generic = original_error in {'', 'None', 'acquisition_failed'} or original_error.startswith('acquisition_failed (worker exit ')
    detail = (worker_exception(session) or original_error or 'acquisition_failed') if generic else original_error
    report = {"run_id":run_id, "status":local['status'], "worker_error":detail,
              "canonical_chunks":len(canonical), "validated_duplicate_pairs":pairs,
              "verified_files":len(files), "destination":str(destination), "applied":False}
    if not apply:
        return report
    # Recheck after the expensive verification, before writing any metadata.
    verify_inactive(session)
    backup_id = uuid.uuid4().hex
    paths = [data/'manifest.json', data/'latest_status.json', session/'status.json',
             destination/'manifest.json', destination/'latest_status.json', destination/'supervisor_status.json']
    for path in paths:
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + '.before-reconcile-' + backup_id))
    metadata = dict(local, run_id=run_id, error=detail, archive_status='archive_error')
    metadata['supervision'] = dict(local.get('supervision',{}), worker_error=detail, reason=detail)
    metadata['reconciliation'] = {"at":utc_now_iso(), "backup_id":backup_id,
                                  "duplicate_pairs":pairs, "verified_files":len(files)}
    if pairs:
        metadata['stats'] = {}
        metadata['reconciliation']['stats_status'] = 'not_recomputed; previous values retained in backup'
    metadata['storage'] = {'status':'verified', 'local_spool':str(data)}
    metadata['files'] = files
    write_json(data/'manifest.json',metadata)
    linux = dict(metadata)
    # The workload runs on Linux; preserve its task metadata and checksum logs.
    if (destination/'workload').is_dir():
        linux['files'] = files + linux_workload
        linux['workload'] = load_workload_meta(destination)
    write_json(destination/'manifest.json',linux)
    final = {'state':metadata['status'], 'reason':detail, 'worker_error':detail,
             'worker_stopped':True, 'storage_status':'verified', 'local_spool':str(data)}
    write_json(session/'status.json', final)
    write_json(destination/'supervisor_status.json', dict(final, generated_at_epoch=__import__('time').time()))
    for folder in (data,destination):
        latest = read_json(folder/'latest_status.json')
        write_json(folder/'latest_status.json',dict(latest,state='ERROR'))
    report.update(applied=True,backup_id=backup_id)
    return report


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    parser.add_argument('--apply',action='store_true',help='Apply metadata repair after verification; originals are backed up')
    args=parser.parse_args(argv)
    try:
        print(json.dumps(reconcile(args.run_id,apply=args.apply),indent=2))
        return 0
    except (OSError,RuntimeError,ValueError) as exc:
        print(f'[ERROR] {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
