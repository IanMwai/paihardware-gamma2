# Setting Up a Lab PC

Use this guide when the DAQ is attached to a Windows lab PC. For Gamma 2,
follow [the VM setup guide](gamma2.md). Both deployments use the same
measurement package, but their storage and login setup differ.

## Setting up a new machine

Before installing the package, install the NI-DAQmx driver on Windows and
reboot. You also need Python on PATH; use 3.12 or 3.13 for the documented
Gamma 2 setup.

In PowerShell:

```powershell
git clone https://github.com/IanMwai/paihardware-gamma2.git
cd paihardware-gamma2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[hardware,archive,remote]
```

This installs the acquisition dependencies (`numpy`, `pyyaml`, `nidaqmx`), the
Globus CLI for archiving, `paramiko` for the gamma NVML link, and creates the
`pai` command. On machines without
the DAQ hardware (e.g. for `--simulate` runs or replaying recorded data),
plain `pip install -e .` is enough. If `pai` is ever not on PATH, every
`pai ...` command below also works as `python -m gpu_power_monitor.cli ...`.

> **Tip:** to avoid activating the venv in every new window, add an
> auto-activation snippet to your PowerShell profile, e.g.
> ```powershell
> # in $PROFILE (adjust the path to where you cloned the project):
> $pai = "$HOME\Desktop\paihardware-gamma2"
> if ($PWD.Path -like "$pai*" -and (Test-Path "$pai\.venv\Scripts\Activate.ps1")) {
>     & "$pai\.venv\Scripts\Activate.ps1"
> }
> ```

### Machine-specific settings (`.env`)

Shared settings live in `configs/default.yaml` and should not need editing.
Anything specific to a machine or lab goes in a `.env` file at the project
root, which is gitignored:

```powershell
Copy-Item .env.example .env
notepad .env
```

| Variable | What it is | Who needs to set it |
| --- | --- | --- |
| `PAI_GLOBUS_LOCAL_ENDPOINT_ID` | UUID of this machine's Globus Connect Personal endpoint (GCP tray icon → Web: Collection Details) | Every DAQ machine that archives |
| `PAI_GLOBUS_REMOTE_ENDPOINT_ID` | Destination collection UUID; defaults to "Harvard FAS RC Holyoke" (serves `/n/holylabs`) | Only if archiving somewhere else |
| `PAI_ARCHIVE_ROOT` | Archive path on the cluster; defaults to the Le Xie lab path | Other labs (e.g. Minlan's lab sets its own holylabs path) |
| `PAI_OUTPUT_ROOT` | Local run directory; defaults to `output/` | Rarely |
| `PAI_GAMMA_HOST` / `PAI_GAMMA_USER` | SSH host/user of the GPU workload machine for NVML logging | Machines with a gamma link |
| `PAI_GAMMA_PASSWORD` or `PAI_GAMMA_KEY_PATH` | SSH auth for gamma (key preferred where possible) | Same |

With `PAI_GLOBUS_LOCAL_ENDPOINT_ID` unset, everything still works. Runs just
stay local, and `pai` prints how to archive them once Globus is configured.
Likewise with the `PAI_GAMMA_*` variables unset: runs are power-only and `pai`
notes that NVML logging was skipped.

### Per-user setup on a shared machine

Each Windows profile keeps its **own clone** (e.g.
`C:\Users\<netid>\Desktop\paihardware-gamma2`) with its own venv, `.env`, and
`output/` folder. No file in the repo contains an absolute path, so nothing
needs editing when you clone to a different location: the config, output
directory, and dashboard all resolve relative to your clone. Per-user
checklist:

1. Clone + install (top of this section).
2. Copy `.env.example` → `.env`. On an already-configured machine, reuse the
   machine's `PAI_GLOBUS_LOCAL_ENDPOINT_ID` (ask whoever set up Globus Connect
   Personal, or read it from the GCP tray icon → Web: Collection Details).
3. Make sure GCP's accessible paths include *your* clone's folder (GCP tray
   icon → Options → Access), otherwise your pushes fail with a permission
   error while everyone else's work fine.
4. Do the per-profile Globus login (see
   [Archiving — one-time setup](archiving.md#one-time-setup)).

### Smoke test with simulated data (no hardware needed)

Start with simulated data so you can check the installation before
connecting to the DAQ:

```powershell
pai hardware --simulate --display --name "sim_test"
```

A browser should open showing live (simulated) voltage, current, and power traces.
Press `Ctrl+C` in PowerShell to stop. If this works, the basic software path is working. Next, check the driver,
device name, and wiring with the real DAQ.

### Confirm the DAQ device name

The config assumes the device is named `Dev1`. Confirm what your hardware
actually enumerates as:

```powershell
pai devices
```

This lists every connected NI device, e.g. `Dev1  (USB-6212)`. If it shows a
different name (e.g. `Dev2`), set `channels.device` in `configs/default.yaml` to
match. (You can also confirm the name in NI MAX → Devices and Interfaces.)


## Staying up to date, and contributing

The repo lives at <https://github.com/IanMwai/paihardware-gamma2> (the
3.102 deployment is <https://github.com/IanMwai/paihardware>). It is a
**personal repo** (maintained by Ian Toyota): lab members do not have write
access; use it, pull updates, and contribute via fork + pull request. If you
hit an issue, email Ian, ping him on the lab Slack, or open a GitHub issue on
the repo.

**Staying current** : cloning (step 1 of setup) already connects your copy to
GitHub, so there is nothing extra to initialize. Every now and then, from your
clone:

```powershell
git pull
```

Because the package is installed editable (`pip install -e .`), pulled code
changes take effect immediately; you only re-run
`pip install -e .[hardware,archive]` if dependencies changed. If you edited
files locally and `git pull` complains, stash first (`git stash`, pull, then
`git stash pop`). Your `.env`, venv, and `output/` are untouched by pulls
because they are not tracked by git.

**Contributing** : fork + PR.

1. Click **Fork** on <https://github.com/IanMwai/paihardware-gamma2>.
2. Clone *your fork*, and add this repo as `upstream`:

   ```bash
   git remote add upstream https://github.com/IanMwai/paihardware-gamma2.git
   ```

3. Branch, make your change, and run the tests (`python -m pytest tests`;
   they pass without DAQ hardware or Globus).
4. Push to your fork and open a pull request against `IanMwai/paihardware-gamma2`.
   Keep your fork in sync with `git fetch upstream && git merge upstream/main`.

