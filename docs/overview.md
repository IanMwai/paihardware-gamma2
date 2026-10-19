# How the Workflow Works

You run `pai` from Gamma 2's Linux shell. It starts acquisition in `daqwin`,
the Windows VM that has the NI USB-6341 attached. Windows is needed here
because the NI Linux driver used for this setup does not support the USB DAQ.
The measurement software is shared with the lab-PC deployment.

The run folder lives on Gamma 2, under `/srv/pai/output/`, and is shared with
the VM. Windows writes the power measurements there as they arrive. Gamma 2
records GPU telemetry and handles the Globus archive transfer. You only need
the Windows desktop for setup or troubleshooting.

## From Starting a Run to Archiving It

1. Run `pai hardware`. The DAQ reads eight channels, one voltage and current
   pair per GPU, at 10 kHz. CSV files rotate every 60 seconds and are
   compressed when each chunk closes. If the remote credentials are set,
   `pai` also starts the GPU telemetry logger on Gamma 2.
2. Leave the command running while you collect data. You can put a workload
   after `--` so that the measurement ends when the workload does. The
   optional dashboard reads the rolling `latest.npz` and
   `latest_status.json` files; it does not control acquisition.
3. Stop with `Ctrl+C` or `pai stop latest`. The VM stops acquisition, collects
   the NVML files into `nvml/`, and finishes `manifest.json`. The manifest
   records run statistics and the file inventory with SHA-256 checksums.
4. Once the run is ready, Globus transfers it to holylabs and verifies its
   checksums. Gamma 2's timer checks for runs to submit every ten minutes.
   Follow progress with `pai archive status <run>`.
5. Keep the local copy until the archive is verified. `pai archive cleanup`
   only allows deletion after verification and the retention period, which
   defaults to 14 days.

Only one measurement can use the DAQ at a time. Test runs use `--test` and
stay local unless you explicitly promote them. See
[Running measurements](running.md) for examples.

## Where the Archive Lives

The default destination is the Le Xie lab's shared holylabs directory on
Harvard FASRC. It belongs to the lab workflow; it is not an operator's home
folder. You need an FASRC account in the lab group to read the archived runs.
See [Data access and permissions](archiving.md#data-access-and-permissions).

Another lab can set `PAI_ARCHIVE_ROOT` and, if needed,
`PAI_GLOBUS_REMOTE_ENDPOINT_ID` in `.env` to use its own storage.

## If You Change the Software

Keep credentials and machine-specific settings in the gitignored `.env`
file. Use `.env.example` to document which variables are needed. Changes to
`configs/default.yaml` affect every machine, so explain them in the pull
request.

Before opening a pull request, install the development dependencies with
`pip install -e '.[dev]'` and run `python -m pytest tests`. These tests do not
need DAQ hardware or Globus.
