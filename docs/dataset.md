# From Recorded Runs to a Shareable Dataset

Completed measurements are kept in the lab archive at
`/n/holylabs/lexie_lab/Lab/gpu_power_logs/<run_id>/`. Each folder includes its
power files, available NVML and workload logs, and a `manifest.json` that
explains how the run was collected.

Keep those archived folders unchanged. If you want to analyze a run, download
a copy and decompress its `.csv.gz` files there. The manifest is the place to
check the GPU configuration, timing, telemetry status, and checksums before
combining it with other runs.

A dataset release will select runs from this archive and put an index and
derived files in a separate versioned directory, such as
`/n/holylabs/lexie_lab/Lab/gpu_power_dataset/v0.1/`. Keeping releases separate
means the same analysis can be rebuilt without rearranging the original data.
The release contents below are planned; the index builder is still to come.

## What a run carries (manifest v2)

Written by the acquisition at the start and completed at the end of the run:

| field | meaning |
| --- | --- |
| `run_id`, `measurement_name` | folder name (`<name>_<YYYYMMDD_HHMMSS>`) and the `--name` given |
| `machine` | which GPU server the shunts are on (`gamma2`, the lab PC's hostname); `--machine` or `PAI_MACHINE` |
| `operator` | who started it (`--operator`, else the login name) |
| `gpus` | the cards as nvidia-smi reports them (index, name, driver, VBIOS, power limit), from `nvml/gamma_info.txt` |
| `workload` | the workload's own description of itself, copied from `workload/meta.json` (see below) |
| `sample_rate_hz`, `channels`, `scaling`, `processing` | exactly how the DAQ was configured, including the shunt wiring and calibrated scale per GPU |
| `started_at`, `ended_at`, `duration_s` | UTC wall clock and run length |
| `stats` | sample count, average and peak total power, energy in joules |
| `files` | every file in the run with size and SHA-256 (what Globus verifies) |
| `run_kind`, `archive_status`, `archive` | `archive`/`test`; `ready_to_archive` → `transfer_pending` → `archived_verified`; Globus task and destination |
| `manifest_version` | `2` for runs with the dataset fields; older runs have none |

## What a workload writes: `workload/meta.json`

`pai hardware --name X -- <command>` exports `PAI_RUN_DIR` and `PAI_RUN_ID`
to the command. A workload that wants to be indexable writes
`$PAI_RUN_DIR/workload/meta.json` before it starts and keeps its own logs in
the same folder, so they are checksummed and archived with the power data.
The keys below are the shared vocabulary; add keys freely, never rename
these:

| key | values |
| --- | --- |
| `workload` | the tool: `nhpp-load`, `burn`, ... |
| `type` | `inference`, `training`, `synthetic`, `idle` |
| `model` | the served or trained model, as its hub name |
| `profile` | `sinusoid`, `trace:<file>`, `step`, `sustained`, `burst`, ... |
| `experiment_hours`, `compress`, `wall_hours` | the profile's nominal length, any time compression, and the resulting wall time (`compress: 1` for dataset runs) |
| `seed` | so the run is reproducible |
| `request_log` / `phase_log` | the file next to `meta.json` with per-request or per-phase timestamps |

The workloads that do this live in the
[`pai-workloads`](https://github.com/IanMwai/pai-workloads) repo, together
with `pai-campaign`, which runs a YAML list of such runs back to back.

## Naming runs

`<machine>_<workload>_<model>_<profile>[_<length>]`, lower case, no spaces:
`g2_nhpp_qwen7b_sin_24h`, `g2_burn_step_5x60`, `g2_idle_1h`. The timestamp
is appended automatically. Names are for people browsing `pai runs list`
and the archive; the manifest is for the index.

## Resolution and size

Power measurements are sampled at 10 kHz. Chunks
are gzipped as they close (`logging.compress`, on by default), which cuts
the CSVs 3-5×; a 24 h run is roughly 30-50 GB on disk and in the archive, a
10-minute characterization run under 1 GB. Every reader in the pipeline
(stats, replay, the index builder) accepts `.csv` and `.csv.gz` alike.

## What Is Planned for the First Release

`gpu_power_dataset/v0.1/`: an `index.csv` with one row per run (run id,
machine, GPU, workload, model, profile, duration, energy, average and peak
power, source checksum); a datasheet describing the wiring, the fact that
cable shunts exclude the ~30 W that arrives through the PCIe slot, clock
alignment between DAQ, NVML and request logs, and known gaps; and derived
1 Hz and 10 Hz per-GPU power aligned with NVML and the request log as
Parquet. The 10 kHz chunks stay in the archive, referenced by run id. The
curated folder is what gets mirrored publicly later; the archive never
leaves the lab.

The index builder (`pai dataset index`) is the next piece of the pipeline
and is written against real runs from the first campaign.
