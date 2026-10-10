# gamma2 deployment: acquisition inside a VM, driven from the Linux host

gamma2 is a 4× Titan RTX Ubuntu 22.04 server. Its NI USB-6341 cannot be used
from Linux (NI's Linux driver covers PCIe/PXI cards only, not USB DAQs), so
acquisition runs in a Windows 11 VM (`daqwin`) on gamma2 with the DAQ passed
through to it. Everything else happens on gamma2 itself: the run folders live
on gamma2's disk, the dashboard is served from gamma2, NVML telemetry is
logged on gamma2, and Globus pushes from gamma2. Nobody logs into Windows to
take a measurement.

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

Exactly as on the lab PC: `pai` for the menu, or the subcommands directly.

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
`output/test/`). The dashboard is `http://gamma2:8000/` from anywhere on the
Harvard network (off campus: `ssh -L 8000:localhost:8000 admin@gamma2`, then
`http://localhost:8000/`). Stopping takes 10-20 s: the VM notices the STOP
file, pulls the NVML files from gamma2 and checksums the run. Completed runs
are pushed to the lab archive by the sweep timer within ten minutes. Anything
in the main README about run folders, manifests, test runs and archive states
applies unchanged.

## One-time setup

The order matters only where noted. "host" = gamma2 shell as `admin`,
"VM" = PowerShell inside daqwin (via RDP, see the VM section).

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

SSH key for gamma2 → VM (used by `pai hardware`, as the human running it; one
key per human, or a shared `admin` key):

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
`/opt/pai/paihardware-gamma2/.env` as `PAI_GLOBUS_LOCAL_ENDPOINT_ID`. Then
once: `sudo -u pai -H /opt/pai/paihardware-gamma2/.venv/bin/globus login` and
`... globus session update globus.rc.fas.harvard.edu`. Archived files on
holylabs are owned by whichever FASRC identity that login used (same model as
the 3.102 machine). The sweep timer then pushes every `ready_to_archive` run;
`systemctl status pai-archive-sweep` and `journalctl -u pai-archive-sweep`
show what it did.

## How it differs from the 3.102 Windows machine

* Nobody uses the Windows desktop. Start/stop is `pai` on gamma2; the
  Windows login is a service account.
* `output/` is a share on gamma2 (`PAI_OUTPUT_ROOT` is a UNC path in the VM's
  `.env`). The VM never holds the only copy of anything.
* The dashboard is a URL on gamma2, not a browser window in the VM. Replay
  still works (`pai dashboard <run> --replay --host 0.0.0.0`) but is not part
  of the routine.
* Globus runs on gamma2. The VM has no endpoint and never pushes.
* Clocks: the VM's clock is driven by the host and additionally NTP-pinned to
  it, so `started_at` (VM) and the NVML timestamps (gamma2) are the same clock
  to well under a millisecond.

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
