# Keeping Gamma 2 runs under control

A measurement now has a Windows supervisor as well as a Linux controller.
The supervisor starts acquisition as a child process and watches it without
reading the Linux share. Control messages go over SSH to files on Windows.
The launch uses Windows WMI so closing the launch SSH connection cannot take
down the supervisor alone. If WMI creation fails, acquisition does not start.

The Linux controller renews its lease every five seconds. Windows requests a
stop after 60 seconds without renewal, after 60 seconds without acquisition
progress, or when the acquisition's maximum lifetime expires. A normal stop
gets 30 seconds to enter finalization. Finalization has a separate five-minute
limit. If the child remains stuck, Windows terminates that child's process
tree. Cancellation, controller loss, forced termination, and data failures
are unsuccessful runs and cannot advance a campaign.

## Updating the two machines

Stop existing acquisition before installing this version. Update the hardware
checkout and its virtual environment on **both Linux and Windows**, and update
`pai-workloads` on Linux. Older acquisition processes keep their old behavior;
updating files does not add supervision to them. No setup script needs to be
rerun for these Python changes. Test WMI launch and actual NI acquisition on
the VM before leaving a campaign unattended.

## Windows needs local space

Acquisition writes its data and progress to `%LOCALAPPDATA%\PAI\spool` by
default. To use a larger Windows-local disk, add this to the Windows repo's
`.env` before launching a run:

```dotenv
PAI_VM_SPOOL_ROOT=D:\pai-spool
```

Do not point this setting at the Linux SMB share. The spool is part of the
stop and recovery mechanism. The launch checks free space for the maximum
window using a conservative uncompressed estimate plus a 1 GB reserve; a
four-GPU 10 kHz day can require about 300 GB by this estimate. Compression
savings are not assumed. Choose a volume large enough for the run and retained
previous runs. Local data is **not automatically deleted** after export.

A separate exporter copies closed chunks to `/srv/pai/output`, verifies SHA-256
checksums, and publishes a completed manifest only after verification. Failed
runs also retain partial/PENDING files as evidence. The final export has a
ten-minute limit. If the share is blocked, the supervisor can still stop the
DAQ, and the local spool remains available for recovery. Export failure must
be resolved before treating the dataset as complete.

Writer errors and a full writer queue now fail acquisition instead of silently
dropping blocks. Finalization checks acquired versus written sample counts.
Statistics are accumulated while writing, so finalization does not load a
whole day of samples into memory. Final file hashing is still required.

## Starting and stopping

The ramp campaign supplies `workload_seconds`. Linux permits the requested
workload window plus at most 120 seconds for drain. Windows has a separate
acquisition limit of the window plus ten minutes. Model startup happens before
acquisition. The generic VM command defaults to a 24-hour acquisition limit
plus ten minutes if no window is supplied. For custom commands, supply a
finite window explicitly:

```bash
pai hardware --name example --workload-seconds 60 -- sleep 60
```

`--duration-sec` now reaches the VM for a finite acquisition without a workload.
All deadline calculations use monotonic clocks. A controller thread renews the
lease while acquisition finalizes and exports.

```bash
pai stop RUN_ID
```

This command now waits for acknowledgment. It reports stopped and finalized,
stopped with incomplete data/export pending, or an unconfirmed stop. Interrupted
runs and unconfirmed stops return nonzero. If SSH control is unavailable it
tries the shared STOP marker, but it does not claim that writing a marker
proves anything stopped. The exporter relays legacy shared STOP requests when
the share is accessible. A frozen acquisition is handled by the supervisor.

Run listings distinguish `run interrupted` and `status stale; VM verification
needed` from a live measurement. A stale preview alone is not proof that a
process has exited.

## Recovering an old or killed run

After process cleanup, repair the killed three-hour run from Linux:

```bash
pai runs recover g2_ramp_qwen7b_steady_3h_20261019_232227
```

Recovery asks Windows to verify that the acquisition is gone. It refuses if a
relevant process is alive or process details cannot be checked. Legacy runs
are checked using Windows process command lines; supervised runs use recorded
process ownership. The manifest becomes **interrupted**, never completed.
Partial data is retained and is not automatically archived. For supervised
runs with an incomplete export, recovery starts another export attempt;
verify its outcome before using the exported files. A missing ownership record
requires manual investigation rather than guessing that the DAQ is free.

## Validation before longer collection

1. Run the one-minute ramp and check samples, request errors, and archive status.
2. Run another short test, terminate its Linux controller, and confirm that
   Windows stops acquisition after lease expiry. This test must be interrupted,
   never successful. Also test closing the SSH terminal that launched the
   systemd service: that must leave the service running.
3. Test an explicit stop and confirm no acquisition processes remain.
4. Check that a blocked share leaves data locally and never reports a verified
   export. Check disk-full, writer failure, and stalled acquisition handling
   using the offline tests before live fault injection.
5. Repeat ten minutes and one hour, then retry three hours. Advance only after
   inspecting the measurements. Automated completion is not a quality review.

Do not use a full-rate production trace as the first unattended test. Its file
parsing and inference backlog add setup time; a bounded deadline may reject
that run rather than letting an overloaded replay continue indefinitely.

## Compression and failed-run reconciliation

The writer now publishes one settled representation of each chunk through a
local `.chunks` registry. With compression enabled, the exporter never reads
the intermediate CSV while gzip is still working. It starts only after a
verified gzip, or an intact CSV fallback, has been published. The registry is
control metadata and is not included in the dataset inventory.

A Windows sharing violation while deleting a redundant CSV is recorded as
`cleanup_pending`; it does not fail acquisition when a verified gzip exists.
Compression failures retain the original CSV and publish it with a warning.
Unrecoverable writes and conflicting CSV/gzip contents remain failures.
Readers validate duplicate representations and count a logical chunk once.
The terminal exporter reloads its manifest after the EXPORT marker, preserves
the real acquisition exception, and will not verify a still-running manifest.
NVML directories use the actual run ID rather than the spool subfolder `data`.

For the failed three-hour run, first update the hardware package on Linux and
Windows. Keep both the Linux copy and the Windows spool. In Administrator
PowerShell from `C:\pai\paihardware-gamma2`, inspect without changing files:

```powershell
.\.venv\Scripts\python.exe -m gpu_power_monitor.reconcile g2_ramp_qwen7b_steady_3h_20261023_202815
```

Inspection checks process ownership, validates CSV/gzip pairs, verifies the
canonical files against the Linux copy, and reports the original exception.
If it fails, preserve both copies and investigate the named file or process.
Do not manually remove duplicates or mark the run completed.

To apply the verified metadata reconciliation:

```powershell
.\.venv\Scripts\python.exe -m gpu_power_monitor.reconcile g2_ramp_qwen7b_steady_3h_20261023_202815 --apply
```

Apply checks process inactivity again, backs up existing metadata with a unique
`.before-reconcile-...` suffix, and preserves all data files. It retains error
or interrupted status and disables automatic archiving. When duplicate pairs
exist, previously calculated statistics are cleared rather than risking a
double count; the old values remain in the metadata backup. This command does
not re-run the workload or turn 54 minutes into a successful three-hour run.
The tool refuses to reconcile a completed run.

Back on Linux, `pai runs list` should show the failed run as errored rather
than stale. The research campaign remains separate from this repair.

### Validation on Windows and Gamma 2

The offline suite contains injected sharing violations and a Windows-only
native lock regression. If pytest is available in the Windows environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_chunk_handoff.py tests/test_reconcile.py
```

Before another long run, collect a watched five-minute ramp to exercise several
one-minute chunk rotations, then repeat the one-hour stage. These are live
hardware validation steps; passing simulated tests alone does not establish
DAQ/Windows reliability.

```bash
pai-campaign run /opt/pai/pai-workloads/campaigns/gamma2-ramp.yaml --stage 5m --rerun
pai-campaign run /opt/pai/pai-workloads/campaigns/gamma2-ramp.yaml --stage 1h --rerun
```

Run these sequentially and review each result before the next. The commands
above run in the foreground, so keep the SSH session open. Confirm distinct
NVML run directories, completed acquisition, verified export, no writer errors,
and no duplicated logical chunks before retrying three hours under systemd.
