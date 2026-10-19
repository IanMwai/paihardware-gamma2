# Data Collection Workflow for Gamma 2

This workflow adapts [paihardware](https://github.com/IanMwai/paihardware)
to Gamma 2. It does the same job, measuring GPU power, with a different set of
cards and a different hardware setup. Gamma 2 has four TITAN RTX GPUs. An NI
USB-6341 measures their power at 10 kHz through voltage taps and shunts, while
`nvidia-smi` records what the cards are doing over the same period.

Acquisition runs inside a Windows virtual machine (VM) on the server. You
start it from the Linux shell with `pai`. The VM writes compressed CSV files
to the Linux server, and Globus transfers completed runs to the lab's FASRC
holylabs storage. These runs will form an open dataset of GPU power profiles.

## Before You Start

You need an account on Gamma 2 and an FASRC account in the lab's group to
read the archived data. Reach out to Ian or Raja for a Gamma 2 account. Your
Gamma 2 account also needs to be in the `pai` group, with SSH access to the
VM; an admin can set this up using the
[shared-access instructions](docs/gamma2.md#7-host-open-it-up-to-the-lab).

## Check That the System Works

SSH into Gamma 2 from your own computer:

```bash
ssh username@10.243.84.164
```

Then check that the DAQ responds and record a short idle run:

```bash
pai devices
pai hardware --name g2_idle_1m --test -- sleep 60
pai runs list
```

`pai devices` should show `Dev1` (USB-6341). The second command records one
minute, then stops. Because it uses `--test`, the run stays local and is not
archived. Its folder is under `/srv/pai/output/test/`. Check the run folder
for power files, `manifest.json`, and the `nvml/` telemetry files. If anything
is missing, start with [Troubleshooting](docs/troubleshooting.md).

## Collect a Run

For a ten-minute idle measurement that will be archived:

```bash
pai hardware --name g2_idle_10m -- sleep 600
```

To measure a workload, put its command after `--`. Acquisition stops when
that command finishes. For example, if an inference server is already
running on port 8001:

```bash
pai hardware --name g2_nhpp_qwen7b_sin_1h -- nhpp-load --url http://127.0.0.1:8001/v1 \
    --model Qwen/Qwen2.5-7B-Instruct --hours 1
```

The workload tools come from [pai-workloads](https://github.com/IanMwai/pai-workloads).
To stop early, press `Ctrl+C`, or run `pai stop latest` in another shell.
Allow about 10–20 seconds for the VM to stop, collect the telemetry, and
finish the manifest.

Normal runs go to `/srv/pai/output/<name>_<timestamp>/`. The archive timer
checks for completed runs every ten minutes; the transfer can take longer.
Use `pai archive status latest` to check whether a run has reached holylabs.
You do not need your own Globus login to use the configured archive timer.

## Commands You Will Regularly Use

| Command | When to Use It |
| --- | --- |
| `pai` | Open the interactive menu if you do not remember a command. |
| `pai devices` | Check that the VM can see the DAQ. |
| `pai hardware --name NAME [--test] [-- CMD ...]` | Start a measurement, optionally around a workload. |
| `pai stop latest` / `pai stop NAME` | Stop a run from another shell. |
| `pai runs list` | Find a run and check its archive state. |
| `pai archive status latest` | Check the Globus transfer and verification status. |
| `pai archive push-ready` | Retry runs waiting to be archived. The timer also does this. |
| `pai fetch NAME` | Retry collecting a run's NVML files. |
| `pai runs delete NAME` | Remove a test run you no longer need. |
| `pai dashboard latest [--replay]` | View a run live or replay it, if you need the dashboard. |
| `vllm serve <model> --dtype half --max-model-len 4096 --port 8001` | Start an inference server. Port 8000 is reserved for the dashboard. |

Use names that explain the experiment, such as `g2_nhpp_qwen7b_sin_24h`:
`<machine>_<workload>_<model>_<profile>[_<length>]`, in lower case without
spaces. The timestamp is added for you. A 24-hour run at 10 kHz takes roughly
30–50 GB, even with compression, so check available disk space before a long run.

## Viewing and Analyzing the Data

For routine analysis, let the run finish and archive, then download it through
Globus and decompress the `.csv.gz` files. Keep the archived copy unchanged.
The [archive guide](docs/archiving.md) explains access, and the
[dataset guide](docs/dataset.md) describes the files and their limitations.

The live dashboard is optional. An SSH session has no graphical browser, so
viewing plots requires a browser on your computer with access to the server,
or a remote desktop session. The configured host dashboard is at
[http://gamma2:8000/](http://gamma2:8000/); see the
[viewing instructions](docs/running.md#view-dashboard) for tunneling and VM access.

For the usual collection workflow, I recommend working from archived runs.
It avoids the extra setup, and lets you check the DAQ, NVML, and workload
timestamps together before analysis. If the VM has paused or restarted,
check its clock before starting another measurement.

## More Detailed Instructions

- [Running measurements](docs/running.md): GPU selection, workload logs, dashboard controls, and test runs.
- [Archiving and access](docs/archiving.md): FASRC, Globus, transfer status, and local cleanup.
- [Dataset files](docs/dataset.md): manifests, workload metadata, naming, and planned releases.
- [Hardware](docs/hardware.md): wiring, calibration, and the GPU index map.
- [How the workflow works](docs/overview.md): where each part runs and how files move.
- [Gamma 2 setup](docs/gamma2.md): rebuilding the VM, shared storage, and lab access.
- [Lab PC setup](docs/setup.md): the Windows setup this deployment builds on.
- [Troubleshooting](docs/troubleshooting.md): what to check when a command fails.
