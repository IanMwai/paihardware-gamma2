# When Something Does Not Work

Start with `pai devices` and `pai runs list`. These tell you whether the VM
can see the DAQ and whether your run was created. For VM, shared-folder, or
clock problems, the [Gamma 2 setup guide](gamma2.md#troubleshooting) has the
admin checks.

## The Measurement Will Not Start

If `pai devices` shows nothing, check the USB connection and look for `Dev1`
in NI MAX inside the VM. If NI MAX cannot see it either, check USB
passthrough using the deployment guide.

If you see `nidaqmx is not installed` on a Windows DAQ machine, activate its
virtual environment and run `pip install -e '.[hardware]'`. If that error
appears on Gamma 2's Linux host, first check the host's `PAI_VM_*` settings;
the host should be starting acquisition in Windows.

## Windows Rejects My SSH Key

If `pai devices` or shared setup reports `Permission denied`, check access
from your own Gamma 2 Linux account:

```bash
ssh -o BatchMode=yes daqvnc1@192.168.122.51 whoami
```

Use the actual Windows acquisition username and VM address from the host
`.env`. Linux group membership does not authorize an SSH key in Windows.
Each operator needs their own key added, even if Linux `admin` can connect.
Follow [Adding Each Operator](gamma2.md#adding-each-operator), or the
[RDP bootstrap steps](gamma2.md#first-key-or-repair-through-rdp) if the setup
operator's own key is not yet accepted. You can continue using your own
Linux account throughout.

The corrected shared setup script stops after a failed key-copy step. Older
copies continued despite that failure. Check SSH and `pai devices` yourself
before treating access as configured. If PowerShell prints a command beginning
with `=C:\ProgramData`, stop it with `Ctrl+C` and update the host checkout;
see the [setup quoting fix](gamma2.md#shared-setup-hangs-with-a-powershell-error).
Do not copy the Linux host `.env` into Windows to solve an SSH failure.

## The Run Has No NVML Files

Read the run's `nvml.status` in `manifest.json` and the terminal message.
The VM needs `PAI_GAMMA_*` configured and the remote dependency installed
with `pip install -e '.[remote]'`. On Gamma 2, these settings connect the VM
back to the Linux host. If the logger ran but the files were not collected,
retry with `pai fetch <run>`.

## The Run Has Not Reached the Archive

Run `pai archive status <run>`. It reports whether the transfer is waiting,
failed, or verified. A test run stays local by design.

For a normal run marked local only, check `PAI_GLOBUS_LOCAL_ENDPOINT_ID`,
the Globus CLI, and the archive account's login. On Gamma 2, ask the admin
to check the shared timer's login. On a lab PC using your Windows profile,
run `globus login` and then
`globus session update globus.rc.fas.harvard.edu`.

## The Dashboard Is Empty or Stale

For an empty dashboard, compare the device name in `configs/default.yaml`
with `pai devices`. For a stale dashboard, check whether acquisition is
still running and whether the VM has paused. A VM pause can also leave its
clock behind the Linux host; follow the clock checks in the deployment guide.

You can collect data without opening the dashboard. For analysis, wait for
the archive to verify, download the run, and work from the recorded files.

## Port 8000 Is Already in Use

Choose an unused port with `--port <number>` on `pai dashboard` or
`pai hardware --display`. The workload examples use port 8001 for vLLM, so
check that before choosing it for the dashboard.

## Acquisition Cannot Keep Up With the Hardware

NI's "The application is not able to keep up with the hardware acquisition"
means samples were not read quickly enough and the DAQ buffer was overwritten.
Treat that run as partial, even if an older controller printed `exit 0`.

Update both the Linux and Windows copies of the measurement package. The
controller now returns failure for an errored manifest or failed workload;
the VM also releases a DAQ task that fails during startup. The dashboard
preview is decimated before buffering and published at most twice a second.
CSV logging remains at the configured 10 kHz, and whole-run preview statistics
still use every processed sample. Turning off the browser alone did not remove
the previous snapshot work from the acquisition loop.

These software changes reduce a measured source of overhead; they do not
prove the USB, VM, and shared-storage path can sustain a long acquisition.
Before another full campaign, check a 30-minute test and then a short loaded
measurement. Keep the sample rate unchanged, inspect the VM log and manifest,
and check CSV sample coverage before accepting a run. If acquisition still
falls behind, investigate VM scheduling, USB delivery, and shared-disk stalls.

The workloads campaign now verifies manifests before trusting successful exit
codes. Its trace campaign stops on an errored or unverified acquisition. Old
campaign entries marked exit 0 with an errored manifest are retried on resume;
missing or unreadable manifests also cannot establish success. Select runs
explicitly with `--only` when resuming to avoid repeating a removed local run.
