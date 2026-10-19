# Setting Up Gamma 2

Gamma 2 is an Ubuntu 22.04 server with four TITAN RTX GPUs. This guide is
for the person setting it up or rebuilding it. If you just want to collect
data, start with the [README](../README.md) and [running guide](running.md).

The NI USB-6341 is attached to a Windows 11 VM called `daqwin`, because the
NI Linux driver used here does not support the USB DAQ. You start measurements
from Linux. The VM writes to a shared folder on the Linux disk, and Gamma 2
records NVML telemetry and sends completed runs to Globus.

```
 gamma2 (Ubuntu)                                   daqwin VM (Windows 11)
 ┌──────────────────────────────────────────┐      ┌───────────────────────┐
 │ pai hardware --name X  ──ssh─────────────┼─────▶│ pai hardware          │
 │   (Ctrl+C / pai stop → output/<run>/STOP)┼──┐   │   --run-id X_...      │
 │                                          │  │   │   --operator <you>    │
 │ /srv/pai/output/<run>/  ◀── Samba ────────┼──┴───┤ writes CSV chunks,   │
 │   nidaq_*.csv  latest.npz  manifest.json │      │ latest.npz, manifest  │
 │                                          │      │                       │
 │ nvidia-smi logger (started by the VM's   │◀─ssh─┤ NVML bracket          │
 │   pai over ssh; files pulled at run end) │      │ (192.168.122.1=gamma2)│
 │ dashboard  http://gamma2:8000/           │      │                       │
 │ pai archive push-ready  (systemd timer)  │      │ NI-DAQmx + USB-6341   │
 └──────────────────────────────────────────┘      └───────────────────────┘
```

The code is the same `pai` package the 3.102 Windows machine uses, plus a
"the DAQ is in a VM" mode: when the host's `.env` names the VM (`PAI_VM_*`),
`pai hardware` starts the run inside the VM over SSH (with `--run-id` so both
sides agree on the folder) and this side serves the dashboard, stops the run
through a `STOP` file the acquisition loop polls, and archives. The menu, the
subcommands and the flags are identical on both machines.

## Daily use (on gamma2)

Once setup is complete, SSH into Gamma 2 and use `pai` for the menu, or run
the commands below. You do not need an RDP session for routine collection.

```bash
pai                                   # the interactive menu (start, dashboard, replay, archive, ...)
pai hardware --name resnet_sweep      # starts acquisition in the VM; prints run folder + dashboard URL; Ctrl+C stops
pai hardware --name resnet_sweep -- python train.py   # run the job on gamma2 with power + NVML bracketed around it
pai hardware --name wiring_check --test              # test run: stays local, never archived
pai stop latest                       # stop the current run from any other shell
pai runs list                         # every run and its archive state
pai dashboard latest                  # dashboard on the newest run (http://gamma2:8000/)
pai dashboard <run> --replay          # play back a finished run
pai archive status <run>              # where a run is on its way to the cluster
pai fetch <run>                       # retry pulling NVML files for a run
pai runs delete <run>                 # delete a test run
```

Run folders land in `/srv/pai/output/<NAME>_<timestamp>/` (test runs under
`output/test/`). For analysis, the recommended route is to archive the run, download it, and
decompress the power files. If you need live plots, see the
[dashboard instructions](running.md#view-dashboard); access depends on the
server's network and dashboard configuration. Stopping takes 10-20 s: the VM notices the STOP
file, pulls the NVML files from gamma2 and checksums the run. The sweep timer checks for completed runs every ten minutes and submits
them to the lab archive. Transfer completion depends on Globus availability
and the size of the run. Everything in
[running.md](running.md) and [archiving.md](archiving.md) about run folders,
manifests, test runs and archive states applies unchanged.

## One-time setup

The steps below are for an admin. "Host" means the Gamma 2 Linux shell as
`admin`; "VM" means PowerShell inside `daqwin`, reached through RDP. Keep
the host and VM instructions separate as you work through them.

### 1. Host: VM and USB passthrough (already done on gamma2; recorded for rebuilds)

KVM: `sudo apt install qemu-kvm libvirt-daemon-system virtinst ovmf`, add
yourself to `libvirt,kvm`. The VM was created with `virt-install` (Windows 11
Pro, 4 vCPU, 8 GB, 64 GB qcow2 on SATA, e1000e NIC on libvirt's `default` NAT
network, UEFI + Secure Boot + emulated TPM, VNC on 127.0.0.1:5900 with a
password) and set to `virsh autostart daqwin`.

USB passthrough of the 6341 is **not** a libvirt `<hostdev>` (NI's driver
re-enumerates the device and libvirt loses it). It is two QEMU `usb-host`
filters matched by vendor/product, which re-attach by themselves whenever the
device (re)appears. Three pieces, all in `host/`:

1. `99-ni-usb-vm.rules` → `/etc/udev/rules.d/`, then
   `sudo udevadm control --reload && sudo udevadm trigger --action=add -s usb`.
2. In `/etc/libvirt/qemu.conf`: `security_driver = "none"`,
   `cgroup_controllers = [ "cpu", "cpuacct", "cpuset", "memory", "blkio" ]`,
   `namespaces = [ ]`; then `sudo systemctl restart libvirtd`. (These relax
   libvirt's sandboxing so QEMU can open a device libvirt did not assign;
   acceptable for a single-VM lab host.)
3. `daqwin-qemu-commandline.xml`: the `xmlns:qemu` attribute and the
   `<qemu:commandline>` block, added with `EDITOR=nano virsh edit daqwin`.

Check: `virsh qemu-monitor-command daqwin --hmp 'info usb'` lists `ni-boot`
as `USB-6341` at 480 Mb/s, and NI MAX in the VM shows `Dev1` present.

### 2. Host: service user, run folder, Samba

```bash
sudo groupadd --system pai
sudo useradd --system --gid pai --home /opt/pai --create-home --shell /bin/bash pai
sudo usermod -aG pai admin                      # every human who runs pai
sudo mkdir -p /srv/pai/output && sudo chown -R pai:pai /srv/pai && sudo chmod 2775 /srv/pai /srv/pai/output
sudo apt install samba
sudo cp host/smb-pai.conf /etc/samba/smb-pai.conf
echo 'include = /etc/samba/smb-pai.conf' | sudo tee -a /etc/samba/smb.conf
sudo systemctl restart smbd
```

The share is guest-accessible but bound to `virbr0` only, so it is reachable
solely from the VM subnet. Everything the VM writes becomes `pai:pai`,
group-writable, which is what lets `pai stop` (running as a human in
group `pai`) drop the STOP file.

### 3. Host: this repo, time, archive sweep

```bash
sudo chmod 755 /opt/pai
sudo -u pai git clone https://github.com/IanMwai/paihardware-gamma2.git /opt/pai/paihardware-gamma2
cd /opt/pai/paihardware-gamma2
sudo -u pai python3 -m venv .venv && sudo -u pai .venv/bin/pip install -e .[archive]
sudo -u pai cp .env.example .env
sudo ln -sf /opt/pai/paihardware-gamma2/.venv/bin/pai /usr/local/bin/pai
sudo apt install -y chrony
sudo cp deploy/gamma2/host/chrony-vm.conf /etc/chrony/conf.d/pai-vm.conf && sudo systemctl restart chrony
sudo cp deploy/gamma2/host/pai-archive-sweep.{service,timer} /etc/systemd/system/
sudo systemctl enable --now pai-archive-sweep.timer
```

Then edit `/opt/pai/paihardware-gamma2/.env` (as `pai`: `sudo -u pai nano .env`)
and set the host-side values:

```
PAI_OUTPUT_ROOT=/srv/pai/output
PAI_VM_NAME=daqwin
PAI_VM_USER=daqvnc1          # the Windows account setup-vm.ps1 is run as (it prints the name)
PAI_VM_PAI=C:\pai\paihardware-gamma2\.venv\Scripts\pai.exe
PAI_DASHBOARD_HOST=0.0.0.0
```

(`PAI_GLOBUS_LOCAL_ENDPOINT_ID` comes later, section 6.) With `PAI_VM_*` set,
`pai hardware` on gamma2 drives the VM; without them it would try to acquire
locally and fail, which is the only way to get a confusing error here.

SSH key for Gamma 2 → VM: each operator uses their own Linux account and
key. Start with the setup operator's key here, then authorize each additional
operator using [Adding Each Operator](#adding-each-operator).

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''    # if you have none
cat ~/.ssh/id_ed25519.pub                            # paste into setup-vm.ps1 -Gamma2PublicKey
```

### 4. VM: Windows side

Done once over RDP (`ssh -L 3389:<vm-ip>:3389 admin@gamma2`, then Windows
App to `127.0.0.1`; `virsh domifaddr daqwin` gives the IP):

1. NI-DAQmx (driver download from ni.com inside the VM, defaults, reboot).
   NI MAX → Devices and Interfaces → `NI USB-6341 "Dev1"` → Self-Test passes.
2. Python **3.12 or 3.13** (python.org installer, or `py install 3.12` if the
   Python install manager is present). Newer Pythons have no prebuilt wheels
   for numpy/pyyaml yet and `nidaqmx` does not support them; the setup script
   refuses them.
3. `git clone https://github.com/IanMwai/paihardware-gamma2.git C:\pai\paihardware-gamma2`
   (Git for Windows, or download the zip and unpack to that path).
4. Elevated PowerShell:
   ```powershell
   cd C:\pai\paihardware-gamma2\deploy\gamma2\windows
   powershell -ExecutionPolicy Bypass -File .\setup-vm.ps1 -Gamma2PublicKey "<contents of gamma2's ~/.ssh/id_ed25519.pub>"
   ```
   This installs the OpenSSH server with gamma2's key authorised (firewall
   opened on every profile), configures the SMB client for the guest share
   (guest logons, no signing requirement, no metadata caching), pins Windows
   Time to gamma2, disables sleep, builds the venv (`pip install -e
   .[hardware,remote]`), writes `.env` from `env.vm.example` with this
   account's key path, runs `pai devices`, and prints a public key plus the
   account name for `PAI_VM_USER`.
5. Put that printed key into `/home/admin/.ssh/authorized_keys` on gamma2
   (this is the VM → gamma2 link `pai` uses for the NVML logger), and set
   `PAI_VM_USER` in the host's `.env` to the printed account name.

The Windows account that runs acquisition is a fixed service identity;
humans are identified by `--operator` (`pai` records the Linux username).

### 5. Smoke test

```bash
pai devices                                 # runs inside the VM: Dev1 (USB-6341)
pai hardware --name smoke --test            # starts in the VM; prints the run folder and dashboard URL
# ... open the URL, watch traces, then Ctrl+C (10-20 s to finalize)
pai runs list                               # smoke_<ts>  [test run (local only)]
ls /srv/pai/output/test/smoke_*/nvml/       # nvml.csv gamma_info.txt
pai runs delete smoke_<ts>
```

If `nvml` is missing, the VM → gamma2 key (step 4.5) is missing or
`PAI_GAMMA_*` in the VM's `.env` is wrong; `pai fetch <run>` retries later.

### 6. Host: Globus

Globus Connect Personal for Linux runs headless on gamma2 as the `pai` user
with `/srv/pai` in its accessible paths; its endpoint UUID goes into
`/opt/pai/paihardware-gamma2/.env` as `PAI_GLOBUS_LOCAL_ENDPOINT_ID`.

The Globus CLI lives in the same venv as `pai` (`pip install -e .[archive]`
puts it there, exactly as on the 3.102 machine) and is put on everyone's
PATH the same way `pai` is:

```bash
sudo ln -sf /opt/pai/paihardware-gamma2/.venv/bin/globus /usr/local/bin/globus
globus version
```

Then once, as the account that owns the archive pushes:
`sudo -u pai -H globus login` and
`sudo -u pai -H globus session update globus.rc.fas.harvard.edu`. Archived
files on holylabs are owned by whichever FASRC identity that login used (same
model as the 3.102 machine). The sweep timer then pushes every
`ready_to_archive` run; `systemctl status pai-archive-sweep` and
`journalctl -u pai-archive-sweep` show what it did.

Globus logins are per Linux user (tokens live in `~/.globus`), so your own
`globus login` lets *you* push and browse (`pai archive push <run>`,
`globus ls ...`) while the timer keeps using the `pai` login.

To update the CLI (or `pai` and its dependencies in one go):

```bash
cd /opt/pai/paihardware-gamma2
sudo -u pai -H git pull
sudo -u pai -H .venv/bin/pip install -U -e .[archive]   # or: ... pip install -U globus-cli
globus version
```

### 7. Host: open it up to the lab

After the base setup, give each operator access using their own Linux
account and SSH key. Linux group membership and Windows SSH authorization
are separate requirements. `PAI_VM_USER` names the shared Windows account;
it does not need to match the operator's Linux username.

#### Adding Each Operator

1. On Gamma 2, the new operator checks for `~/.ssh/id_ed25519.pub`. If they
   have no key pair yet, they run `ssh-keygen -t ed25519` as themselves.
   Keep the private key in that account; only the `.pub` file is copied.
2. A setup operator with sudo access and an SSH key already accepted by the
   VM runs the script below. Replace `alice,bob` with the new Linux usernames.
   The script uses the invoking user's key (`SUDO_USER`) to reach Windows;
   `--users` lists whose public keys it will add. The invoking user can be
   `ian` or any authorized maintainer, not necessarily Linux `admin`.

   ```bash
   cd /opt/pai/paihardware-gamma2
   sudo bash deploy/gamma2/host/setup-shared.sh --users alice,bob
   ```

3. Each added user logs out and back in, then runs these from their own
   Gamma 2 shell. Use the actual `PAI_VM_USER` and VM address from the host
   configuration if they differ from this example.

   ```bash
   id -nG
   ssh -o BatchMode=yes daqvnc1@192.168.122.51 whoami
   pai devices
   ```

   `id -nG` should include `pai` and `libvirt`. SSH should print the Windows
   account, and `pai devices` should show `Dev1`. A first connection may ask
   you to verify the VM's host key; verify and accept it before repeating
   the non-interactive check.

If the invoking user has never been authorized in Windows, the script cannot
use SSH to authorize that same user. Follow the RDP steps below to establish
the first working connection. Do not copy another person's private key or
change `PAI_VM_USER` to your Linux username.

#### First Key or Repair Through RDP

Use this when the setup operator's own SSH test fails with `Permission denied
(publickey,password,keyboard-interactive)`.

1. In that operator's Gamma 2 shell, run `cat ~/.ssh/id_ed25519.pub` and copy
   the complete public-key line.
2. From your own computer, open an RDP tunnel using your own Gamma 2 account:

   ```bash
   ssh -L 3389:192.168.122.51:3389 username@10.243.84.164
   ```

   Connect your RDP client to `127.0.0.1:3389` and sign in with the Windows
   account. `virsh domifaddr daqwin` on Gamma 2 gives the current VM address.
3. In an elevated Windows PowerShell, open the authorized-key file:

   ```powershell
   notepad C:\ProgramData\ssh\administrators_authorized_keys
   ```

   Append the operator's public key on its own line and preserve all existing
   keys. This deployment uses a Windows administrator account for acquisition,
   so OpenSSH reads this file rather than the account's personal
   `.ssh/authorized_keys`. Preserve the restricted permissions from setup.
   If those permissions need repair, use:

   ```powershell
   icacls.exe C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F"
   ```

   The two SIDs identify Windows Administrators and SYSTEM. See Microsoft's
   [Windows OpenSSH key instructions](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement).
4. Back in that operator's Gamma 2 shell, test SSH and then run shared setup
   for the users who need access. For ian:

   ```bash
   ssh -o BatchMode=yes daqvnc1@192.168.122.51 whoami
   cd /opt/pai/paihardware-gamma2
   sudo bash deploy/gamma2/host/setup-shared.sh --users ian
   ```

This authorizes Linux → VM access. The VM → Linux key used for NVML is a
separate connection configured in step 4.5; keep its existing configuration.
The host and VM also need different `.env` files. Do not copy the host's
`PAI_VM_*` configuration into Windows.

#### What Shared Setup Installs

The script adds the listed users to `pai` and `libvirt` and attempts to copy
their public keys into Windows. A failed key-copy step stops setup. If you see `could not add ... key to
the VM`, resolve it and repeat the SSH check before collecting data. Successful setup
ends with `done. Users added to the group must log out and in again.`

If it stops after adding groups, check that the host `.env` exists and
contains `PAI_VM_USER`; a Git clone does not restore this ignored file.
A GitHub username prompt is separate from VM access: the script clones or
updates `pai-workloads` as Linux user `pai`, so that account needs repository
access even if the invoking Linux user's GitHub login works.

The optional `--vllm` flag installs vLLM. Treat `--data-volume SIZE` as a
separate storage operation: it creates and formats an LVM volume and moves
`/srv/pai`. Check existing mounts and free LVM space before using it. It is
not needed just to add another user.

* The workloads repo is installed at `/opt/pai/pai-workloads` and its
  commands (`nhpp-load`, `pai-campaign`) join `pai` and `globus` in
  `/usr/local/bin`. `--vllm` installs vLLM at `/opt/vllm` and puts `vllm`
  there too, so nobody is told a command does not exist.
* `/srv/hf` becomes the shared Hugging Face cache (`HF_HOME`, set for every
  login), so a model downloads once for the whole lab.
* `--data-volume SIZE` gives `/srv/pai` its own LVM volume. The root disk
  filling up is what pauses the VM mid-run, and a 10 kHz run is 1-2 GB per
  hour even gzipped, so do this before any long campaign. It refuses while a run is
  in progress and keeps the old copy at `/srv/pai.old` until you delete it.

After that, a lab member's whole interaction is:

```bash
CUDA_VISIBLE_DEVICES=1 vllm serve Qwen/Qwen2.5-7B-Instruct --dtype half --max-model-len 4096 --port 8001
pai hardware --name g2_nhpp_qwen7b_sin24 -- nhpp-load --url http://127.0.0.1:8001/v1 \
    --model Qwen/Qwen2.5-7B-Instruct --hours 24
```

Their run lands in `/srv/pai/output` as `pai:pai`. The sweep timer submits
completed runs using the lab's Globus login, so lab members do not need to
run `globus login` to collect data. They can check the transfer with
`pai archive status latest`. The
manifest records who ran it (`operator`), on what (`machine`, `gpus`) and
what it was (`workload`, from the workload's own `meta.json`), which is what
the dataset index is built from.

`sudo -u pai` remains only for maintenance: `git pull` in the two repos under
`/opt/pai`, `pip install -U` in their venvs, and the Globus login.

## How it differs from the 3.102 Windows machine

* Nobody uses the Windows desktop. Start/stop is `pai` on gamma2; the
  Windows login is a service account.
* `output/` is a share on gamma2 (`PAI_OUTPUT_ROOT` is a UNC path in the VM's
  `.env`). The VM never holds the only copy of anything.
* The dashboard is a URL on gamma2, not a browser window in the VM. Replay
  still works (`pai dashboard <run> --replay --host 0.0.0.0`) but is not part
  of the routine.
* Globus runs on gamma2. The VM has no endpoint and never pushes.
* The VM is configured to synchronize its clock with the host. Check the
  offset before aligning DAQ and NVML data, particularly after a VM pause
  or restart; the configuration alone does not guarantee a particular accuracy.

## Troubleshooting

* **`pai hardware` says the run did not start** - read
  `/srv/pai/output/.pai/<run>.log`. "Permission denied (publickey)" or a
  password prompt: `PAI_VM_USER` in the host `.env` is not the VM account, or
  gamma2's key is not in the VM's `administrators_authorized_keys`. Connection
  timing out: the VM's firewall rule `OpenSSH-Server-In-TCP` is limited to the
  Private profile (`Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -Profile
  Any`). "device not found" from DAQmx: the VM lost the DAQ, see next item.
* **`pai hardware` tries to use nidaqmx on gamma2** - `PAI_VM_*` is missing
  from `/opt/pai/paihardware-gamma2/.env`, so it thinks the DAQ is local.
* **NI MAX says Dev1 not present** - on gamma2,
  `virsh qemu-monitor-command daqwin --hmp 'info usbhost'` should list
  `3923:74f8 USB-6341`; if it is missing, check `lsusb | grep 3923` (cable),
  then `ls -l /dev/bus/usb/001/` (the NI node must be owned by `libvirt-qemu`;
  if not, the udev rule is not applied: `sudo udevadm trigger --action=add -s usb`).
  Unplugging and replugging the DAQ is safe: QEMU re-grabs it within a second.
* **Dashboard says "Stale" on a live run, or `pai hardware` cannot reach the
  VM** - check `virsh list --all`. libvirt pauses the VM when gamma2's disk
  fills up (its disk image cannot grow) and leaves it `paused` afterwards:
  `virsh resume daqwin`. The guest clock stops while paused and Windows only
  creeps back, so after a resume also run `w32tm /resync /force` in an admin
  PowerShell in the VM (or just `virsh shutdown daqwin && virsh start daqwin`,
  a fresh boot takes the host's clock). Until the clocks agree, "Stale" shows
  the offset between them and `started_at` is wrong.
* **Stopping seems slow** - 10-20 s is normal (STOP file noticed, NVML files
  pulled from gamma2, run checksummed). A second Ctrl+C does not speed it up;
  the run still finalizes on its own.
* **Run shows STALE on the dashboard** - acquisition died (VM rebooted, SSH
  session killed). The CSV chunks written so far are intact. The manifest stays
  `running`; mark it by hand or just start a new run.
* **Currents read a huge constant (±67 A) and power is nonsense** - the current
  inputs (`ai1/3/5/7`) are pinned at the ADC rail, i.e. they sit at the 12 V
  rail potential. The shunts must be in the ground return with `ai<n>` on the
  load side and `AI GND` on the supply side (single-ended, 80 mV/A). This is
  wiring, not software; NI MAX → Test Panels on `ai1` (RSE, ±10 V) reading
  ~10.8 V confirms it.
* **Share not reachable from the VM** - `Test-Path \\192.168.122.1\pai\output`
  in the VM. If false: `sudo systemctl status smbd` on gamma2, and in the VM
  confirm `AllowInsecureGuestAuth` is 1 and SMB signing is not required
  (setup-vm.ps1 sets both).
* **Sweep timer pushes nothing** - `sudo -u pai -H .venv/bin/pai archive push-ready`
  by hand prints the reason (usually `globus login` expired).

### Shared Setup Hangs With a PowerShell Error

An older `setup-shared.sh` passed a PowerShell command through nested shells,
which could expand `$f` before it reached the command. This produced a
command starting with `=C:\ProgramData...` and could leave PowerShell waiting
for input. Stop that attempt with `Ctrl+C` and update the host checkout.
The corrected script uses an encoded, non-interactive PowerShell command
and stops if key authorization fails. Rerun from your own Linux account:

```bash
sudo bash /opt/pai/paihardware-gamma2/deploy/gamma2/host/setup-shared.sh --users ian --vllm
```

Use your own username in place of `ian`. Leave storage migration out of an
access-repair run. This quoting error does not require copying `.env` files
or recreating the Windows account.
