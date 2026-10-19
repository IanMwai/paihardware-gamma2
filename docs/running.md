# Running Measurements

Run these commands from Gamma 2 after you SSH in. For a first check, use
`pai devices`, then `pai hardware --name g2_idle_1m --test -- sleep 60`.
That records a minute of idle power without sending a test run to FASRC.

## Start acquisition

If you prefer to choose from a menu, run:

```bash
pai
```

The menu lets you start a run, inspect previous runs, or check an archive
transfer. The commands below are useful once you know what you want to run.

If you prefer the CLI directly, the equivalents are below. The `--name` flag
is **optional** everywhere: without it, runs are auto-named `GPU Run 0`,
`GPU Run 1`, and so on, counting up from your existing runs.

For routine collection, start without a dashboard:

```bash
pai hardware --name g2_idle_10m -- sleep 600
```

To also start the optional dashboard:

```bash
pai hardware --display
```

Acquisition only (headless):

```bash
pai hardware --name "training_run_001"
```

To check the software with generated data and no NI hardware:

```bash
pai hardware --simulate --display
```

For a finite smoke test, add `--duration-sec 10`.

Measure only some of the wired GPUs (labels or 1-based indices; the menu asks
the same question). The run's CSVs, manifest, and dashboard then carry just
those GPUs:

```bash
pai hardware --gpus 1              # GPU1 only
pai hardware --gpus "GPU1,GPU3"    # any subset
```

A run that should **not** end up on the cluster (trying a feature, checking
wiring) is started with `--test` — see
[Test runs (local scratch)](#test-runs-local-scratch):

```bash
pai hardware --test --simulate
```

To measure a workload, put its command after `--`. Acquisition starts before
the command and stops after it exits. With NVML configured, the run includes
both power measurements and GPU telemetry around the job:

```bash
pai hardware --name resnet_sweep -- python train.py
```

The workload sees `PAI_RUN_DIR` and `PAI_RUN_ID`, so it can write its own logs
into `<run>/workload/` and have them archived with the measurement.

Workloads themselves (an inference server under a daily arrival pattern,
training sweeps, ...) live in the separate
[`pai-workloads`](https://github.com/IanMwai/pai-workloads) repo, so this
one stays about measuring. For example:

```bash
pai hardware --name day_inference -- nhpp-load --url http://127.0.0.1:8001/v1 \
    --model Qwen/Qwen2.5-7B-Instruct --hours 24 --compress 12
```

**On gamma2** every one of these commands works unchanged; the only difference
is where the samples come from. The host's `.env` names the VM that owns the
DAQ, so `pai hardware` starts the same `pai hardware` inside the VM over SSH
(with `--run-id` so both sides agree on the run folder), prints the run folder
and the dashboard URL, and waits. `Ctrl+C` stops the run in the VM cleanly, as
does `pai stop latest` from any other shell. The run folder is on gamma2's
disk, shared to the VM, so the CSV chunks are on gamma2 as they are written.

Runs are written under `output/<measurement_name>_<timestamp>/`. Press `Ctrl+C`
to stop a run cleanly, or from any machine that can see the run folder:

```bash
pai stop "<run_id>"        # or: pai stop latest
```

This drops a `STOP` marker into the run folder; the acquisition loop polls for
it (every 0.5 s) and takes the same clean exit path as `Ctrl+C` (NVML fetch,
manifest, checksums), then removes the marker. It is how gamma2 stops a run
inside the VM.

Two further flags: `--run-id NAME` creates exactly `output/NAME/` (no
timestamp appended; refused if it exists), which is how the gamma2 host tells
the VM where to write; `--operator WHO` records who started the run in the
manifest (default: `PAI_OPERATOR`, else your username).


## View dashboard

For most analysis, let the run finish, check that it is archived, and download
it through Globus. Decompress the power files in your analysis copy and check
the DAQ, NVML, and workload timestamps together.

If you need to inspect traces during a run, the dashboard reads the snapshot
files that acquisition writes. Start it with:

```bash
pai dashboard output/<run_id>
pai dashboard latest                 # the newest run
```

An SSH session does not open a graphical browser. With the documented Gamma 2
host configuration (`PAI_DASHBOARD_HOST=0.0.0.0`), the server exposes the
dashboard at [http://gamma2:8000/](http://gamma2:8000/), subject to network
access. To reach the host dashboard through SSH, run this on your computer:

```bash
ssh -L 8000:localhost:8000 username@10.243.84.164
```

Keep that connection open and visit [http://localhost:8000/](http://localhost:8000/).
Port 8000 must be available on your computer.

If you need the Windows desktop instead, forward its RDP port:

```bash
ssh -L 3389:192.168.122.51:3389 username@10.243.84.164
```

Then connect an RDP client to `127.0.0.1:3389`. This assumes the VM is still
at `192.168.122.51`; `virsh domifaddr daqwin` on Gamma 2 shows its current
address. RDP gives you the VM desktop, not a forwarded web page.

These extra steps are why archived runs are the recommended analysis route.
The dashboard only reads files. Clock offsets between the VM and Linux can
still affect timestamps and the stale-data indicator, so check synchronization
after a VM pause or restart. See [Gamma 2 troubleshooting](gamma2.md#troubleshooting).

For a video wall, open the browser fullscreen (or pass `--fullscreen`). Useful
flags: `--port <n>` and `--no-browser`.

Controls (in the browser):

- Each GPU is one color-coded trace per panel; **click a GPU in the header
  legend** to hide/show it everywhere (traces, autoscale, stats, totals).
  The choice is remembered by the browser across runs and reloads.
- Hovering a panel shows a **measurement cursor**: a time line mirrored across
  all three panels with the voltage, current, and power values at that instant
  snapped to the traces (visible GPUs only). Works live, paused, zoomed, and
  in replay.
- `p`: pause/resume the display only (the run keeps acquiring and logging).
  During replay this is play/pause.
- `z`: zoom. Pauses the display, then drag a box on any panel to zoom in.
  Resume (`p`) or Reset View (`r`) both exit the zoom and return to live.
- `r`: reset view: default window, autoscale, live scrolling (in replay:
  restart playback from the beginning at 1×).
- `+` / `-`: widen/narrow the live time window.
- `t`: toggle light/dark theme.

Panels draw an oscilloscope-style graticule: minor gridlines land on round
units (…, 10 ms, 100 ms, 1 s, 2 s, 5 s… on time; same 1-2-5 ladder on the
y-axes), with brighter labelled major lines every 5 minors. The steps adapt
to the window width, zoom, and browser size, so each tiny box is always a
known round division; read spans off the grid like on a scope or in LTspice.

Dashboard states:

- **Live** : acquisition is writing samples right now.
- **Stale** : the last snapshot is real data but the writer went quiet
  (crash/kill, or you opened an old run); the badge shows how old the data is.
- **Run Complete** : the run ended cleanly.
- **Replay** : playing back a recorded run (see below).
- **Archived** : the run has been archived to the cluster.
- **Idle** : no data yet.
- **Error** : acquisition died with an error.
- **Disconnected** : this page lost the dashboard server (e.g. the run was
  stopped with Ctrl+C); the last traces stay on screen.

**Replay** loads the recorded run and plays it back like a live run: play/pause,
speed 1× / 10× / 60×, or "Full run" to see the whole trace at once. Opening a
dashboard on a finished/stale run offers a "▶ Replay run" button. Replay data
is served downsampled (up to 50k points); the CSVs keep full resolution.

**Tabs:** the dashboard reuses an already-open tab. If a tab from a previous
run is still open when a new run starts on the same port, that tab reloads
itself into the new run instead of a second tab opening.


## GPU telemetry from gamma (NVML)

The DAQ measures power through the cable shunts. NVML adds the GPU-side
view, including what each card reports while the workload runs. On Gamma 2,
the VM uses the `PAI_GAMMA_*` SSH settings to start this logger on the Linux
host. You do not need to start it separately:

1. At run start, `pai` SSHes into gamma, records GPU/driver metadata
   (`nvidia-smi -L`, driver + VBIOS versions → `gamma_info.txt`), and launches
   `nvidia-smi --query-gpu=... -lms 100` writing one CSV row per GPU per
   interval to `pai_runs/<run_id>/nvml.csv` on gamma.
2. At run end, the logger is stopped and everything is pulled into
   `<run>/nvml/` **before** the manifest is finalized — so the NVML files are
   in the checksummed inventory and travel to the FASRC archive with the power
   data. A copy also stays on gamma as a backup.

A telemetry failure does not make the power files unusable. If the logger
cannot be reached or is not configured, `pai` prints the problem and records
it in `nvml.status` in the manifest. Check that field before treating a run
as complete. If the files were recorded but not fetched, retry with:

```bash
pai fetch "GPU Run 3_20260918_141530"    # or menu option 7
```

A late fetch refreshes the manifest's file inventory; if the run was already
archived without the NVML files, `pai fetch` says so and tells you to push
again (the re-push only transfers the missing files).

Notes:

- NVML/nvidia-smi refreshes at ~10 Hz — that is the point of this project
  (10 kHz analog power next to standard NVML-rate telemetry). The actual
  logging interval is recorded in the manifest (`nvml.interval_ms`).
- Timestamps in `nvml.csv` come from **gamma's clock**; the manifest records
  both machines' clocks (run `started_at` locally, `date -u` in
  `gamma_info.txt`) for alignment during analysis.
- Simulated runs (`--simulate`) never start the gamma logger: fake power data
  next to real telemetry would only mislead.


## Test runs (local scratch)

Not every run belongs on FASRC. A run started with `pai hardware --test` (or
by answering "y" to the test-run prompt in the menu) is marked
`run_kind: test` in its manifest and:

- is **never archived**: the automatic push skips it, and
  `pai archive push`/`copy` refuse it;
- **can be deleted at will**, without the archived-and-past-retention rules
  that protect real runs:

  ```bash
  pai runs delete "Test Run 0_20260714_121045"
  ```

Unnamed test runs auto-name as `Test Run 0`, `Test Run 1`, … (separate from
the `GPU Run N` counter). Manage the split with `pai runs`:

```bash
pai runs list             # every local run with its kind + archive state
pai runs promote <run>    # test -> archive-bound (then push it as usual)
pai runs demote <run>     # archive-bound -> test; refused once the run is on
                          # (or on its way to) the cluster
pai runs delete <run>     # delete a test run (refuses archive-bound runs)
```

Menu option 6 does the same interactively. Runs recorded before this feature
existed count as archive-bound, so nothing older becomes deletable.

