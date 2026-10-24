# Recording through brief writer stalls

The 12-hour ramp stopped after about seven and a half hours because the writer's block queue filled. The disk still had space. The queue drained during shutdown, but the logs did not establish whether the slowdown was temporary or sustained.

The reader now waits for a full `chunk_size` block instead of enqueueing whatever small number of samples happens to be available. With the default 1,000 samples at 10 kHz, each regular block represents 100 ms. The 200-block queue holds about 20 seconds of samples, plus the block being written. A normal stop also reads the samples already waiting in the DAQ buffer, including a final partial block; it does not keep chasing new arrivals.

A full queue gets up to five seconds to recover. While waiting, acquisition checks writer health and the native DAQ backlog. It stops with an error if the writer fails, the queue cannot recover before the deadline, or the observed native backlog reaches 90% of the configured driver buffer. No block is silently discarded or counted as written. Stop-boundary draining has a separate ten-second bound.

Power smoothing uses vectorized NaN-aware window sums and counts, preserving the existing causal history and NaN behavior across blocks. CSV writing formats numeric columns in bulk, retaining the existing column order and nine-decimal format.

Compression uses one worker with at most two waiting chunks. If that queue fills, the closed chunk stays as a published CSV with a warning. Readers and exports already accept both CSV and gzip. This avoids accumulating competing compressor threads. Compression integrity failures and finalization timeouts still fail the run; a temporary cleanup lock on a redundant CSV remains nonfatal when the gzip is verified.

## Diagnostics

Each run saves `diagnostics/writer.jsonl` at roughly ten-second intervals, with additional startup, failure and shutdown snapshots. The same latest measurements appear in `progress.json` on Windows and in the manifest's `recording_diagnostics` section. They include:

- Queued samples and seconds, high-water marks, and the oldest queued block's age.
- Queue-full events and accumulated/max enqueue wait.
- Writer service rate, last/max block wall and CPU time, and time since the last completed block.
- Compression queue depth, active duration, completed jobs and CSV fallbacks.
- Observed DAQ backlog, its high-water mark, and actual read-block sizes.
- Acquired, accepted and written sample counts.

The service rate measures samples per second spent writing recent blocks; it excludes time waiting for input. A quiet writer is not necessarily stalled. Compare the rate with backlog and enqueue waits before drawing conclusions.

Diagnostic-file failures produce a warning and do not replace the acquisition's original exception. Actual sample-write failures remain fatal. Failed runs retain their files and error status even if their queue drains and export verifies afterward.

The defaults can be set under `logging`:

```yaml
queue_blocks: 200
enqueue_timeout_sec: 5.0
compression_queue_chunks: 2
```

Increasing these values is not a substitute for investigating a writer that cannot keep up.

## Validate on Gamma 2

Commit and deploy the updated acquisition code to the Linux host and Windows VM while measurement is stopped. Keep the failed run's Windows spool and Linux export intact. This change does not repair or relabel that run.

First run the offline regressions on Windows if pytest is installed:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_writer_pressure.py tests/test_chunk_handoff.py
```

Then run the existing five-minute stage under the real workload:

```bash
pai-campaign run /opt/pai/pai-workloads/campaigns/gamma2-ramp.yaml --stage 5m --rerun
```

Inspect the recording diagnostics and confirm successful acquisition and verified export. The historical three-hour run already passed; the new code still needs a short live check before another 12-hour attempt. Use the explicit selection to skip the six-hour prerequisite:

```bash
sudo systemd-run --unit=pai-gamma2-ramp --collect \
  --uid="$(id -un)" \
  --working-directory=/opt/pai/pai-workloads \
  --property=KillMode=control-group \
  --property=TimeoutStopSec=120 \
  --setenv=PAI_CAMPAIGN_LOGS=/srv/pai/output/.pai/campaigns \
  --setenv=PYTHONUNBUFFERED=1 \
  /usr/local/bin/pai-campaign run \
  /opt/pai/pai-workloads/campaigns/gamma2-ramp.yaml \
  --only g2_ramp_qwen7b_steady_12h --all-stages
```

Keep the research suite stopped until the longer validation succeeds.
