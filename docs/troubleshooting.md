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
