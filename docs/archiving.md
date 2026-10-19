# Archiving Runs and Getting the Data

On Gamma 2, the shared archive timer handles transfers. After a measurement,
run `pai archive status latest` and wait for `archived_verified`. Then use
your own FASRC account to read the run on holylabs or transfer a copy to your
computer through Globus. Decompress the downloaded power files for analysis
and leave the archive unchanged.

You do not need a personal Globus login on Gamma 2 for the configured timer
to archive your runs. The per-profile Windows login instructions below are
for the separate lab-PC deployment; Gamma 2's admin setup is in
[the deployment guide](gamma2.md#6-host-globus).

## Archive storage

Completed full-resolution runs are archived to persistent lab storage on the
cluster (FASRC **holylabs** for Harvard labs, which has snapshots and disaster
recovery, unlike scratch storage, which auto-purges). Each lab points
`PAI_ARCHIVE_ROOT` at its own path; the default is the Le Xie lab's
`/n/holylabs/lexie_lab/Lab/gpu_power_logs`.

The archive directory is group-writable with **setgid** (new run folders
inherit the lab group) and the **sticky bit** (only an entry's owner or a privileged user can remove or rename it in that directory). To create one with
those modes (on a cluster login node):

```bash
mkdir -p /n/holylabs/<your_lab>/Lab/gpu_power_logs
chmod 3775 /n/holylabs/<your_lab>/Lab/gpu_power_logs
```

Transfers go through **Globus** (the cluster path is not mounted on the DAQ
machine), which checksum-verifies every file.


## Data access and permissions

To read archived data, you need an FASRC account in the lab group. Ask Ian
or Raja if you are unsure which group or archive path to use.

- **Runs are archived to the shared lab directory**, not to anyone's home
  directory. Files are *owned by* whichever FASRC identity is logged into
  Globus on the DAQ machine (currently `itoyota` on the 3.102 machine), but
  the directory's setgid bit (mode `3775`) makes every run folder inherit the
  lab group, so ownership does not gate access.
- **Reading archived runs requires your own FASRC account** in the lab's
  group (`lexie_lab` for the Le Xie lab). Then either:
  - `ssh <username>@login.rc.fas.harvard.edu` and read
    `/n/holylabs/lexie_lab/Lab/gpu_power_logs` directly, or
  - use the Globus web app (or CLI) with your own FASRC identity to transfer
    a run to your laptop.

  New members request an FASRC account through the
  [FASRC portal](https://portal.rc.fas.harvard.edu/) with either PI as a sponsor.
- **Globus login on the DAQ machine is only needed to push archives**, and
  tokens are stored per Windows user profile. Each operator logs in once under
  their own Windows account (see
  [Archiving — one-time setup](#one-time-setup)). Nobody needs a Globus login
  just to *read* data on the cluster.
- **Write rights:**
  - *DAQ machine, `output/`* : whoever is logged into Windows and runs `pai`.
  - *Archive directory* : every lab-group member can add new runs (group
    `rwx` + setgid), everyone in the group can read everything, and the
    sticky bit (mode `3775`) restricts removal and renaming of entries in
    that directory. It does not prevent edits to group-writable files inside
    a run, so leave archived files unchanged. The archive is effectively **append-only**: nothing in this
    tool ever deletes from it, and local cleanup only removes the DAQ
    machine's copy after Globus has checksum-verified the archived one.


## Archiving (automatic, via Globus)

When a run completes cleanly, `pai` automatically submits a Globus transfer of
the run folder to `<archive_root>/<run_id>` (`storage.globus.auto_push: true`).
Menu option 5 does the same thing for any past run, and shows a plain-English
status per run.

### One-time setup

**Per machine** : **Globus Connect Personal** must be installed and running,
with every user's `output/` folder inside its accessible paths (GCP tray icon
→ Options → Access). Put its endpoint UUID in each clone's `.env` as
`PAI_GLOBUS_LOCAL_ENDPOINT_ID` (see above).

**Per Windows profile** : Globus logins do **not** carry across Windows
accounts. Every lab member who operates the DAQ under their own profile must
run both of these once, in their own terminal:

1. Log in to Globus (a browser opens; sign in with your university key):

   ```powershell
   globus login
   ```

2. Authenticate your FASRC identity (FASRC requires this *in addition to*
   `globus login`, and asks again when the session expires; the symptom is a
   "Session reauthentication required" error on any transfer or `globus ls`):

   ```powershell
   globus session update globus.rc.fas.harvard.edu
   ```

If your pushes are skipped with a "Not logged in to Globus" message, you are
in a Windows profile that has not completed these two steps.

### How a run flows to the archive

`ready_to_archive` → `transfer_pending` (Globus task submitted) →
`archived_verified` (task succeeded; Globus checksum-verified every file).
The status refreshes whenever the interactive menu starts, on menu option 5,
and on `pai archive status <run>`. Manual submit/retry:

```bash
pai archive push output/<run_id>
pai archive status output/<run_id>
pai archive push-ready               # push every run that is ready_to_archive / push_failed
```

`push-ready` is the unattended form (gamma2 runs it from a systemd timer):
runs already pending, archived, in progress, or marked test are left alone,
and a sweep with nothing to do exits 0.

If you submit a run again, `pai` normally refuses it with "Run
is already archived" (menu option 5 likewise reports rather than resubmits a
pending transfer), every run has one fixed destination
(`<archive_root>/<run_id>`), and transfers use Globus checksum sync. Even a
deliberate re-push only sends files that are missing or differ on the archive
side.

### If the cluster or Globus endpoint is down

An unavailable endpoint does not require restarting the measurement. Globus
keeps the transfer queued and retries until it succeeds or the deadline
passes (`storage.globus.deadline_days`, default 7). The source endpoint must
be online for files to transfer, so keep the local copy available. If a transfer is stuck or fails, `pai archive
status` and the interactive menu print what is wrong in plain English and what
to fix (e.g. "Globus Connect Personal is not running on this machine", "run:
globus login"). A run is never cleanup-eligible until it is
`archived_verified`, so a failed or pending push can never cost data.

### Manual fallback

If the Globus CLI is unavailable, transfer the run folder with Globus Connect
Personal by hand, then record it (this is what later unlocks cleanup):

```bash
pai archive mark-archived output/<run_id>
# --destination overrides the default <archive_root>/<run_id>
```

Optionally re-verify the checksums on the cluster (repo checkout + Python there):

```bash
python3 -m gpu_power_monitor.cli archive verify <archived_copy> --archived-run-dir <archived_copy>
```

If the archive target is ever mounted/mapped directly, `copy` transfers,
verifies, and marks in one step (`copy` refuses to run when the archive root
is not actually mounted, so it cannot silently copy to the local disk):

```bash
pai archive copy output/<run_id>
pai archive status output/<run_id>
```


## Local cleanup

Old runs only need deleting when the local disk fills up. Cleanup is
intentionally conservative: a local run is only eligible after it is marked
`archived_verified` and older than the retention period (`storage.retention_days`,
default 14 days).

```bash
pai archive cleanup output/<run_id>
pai archive cleanup output/<run_id> --delete
```

