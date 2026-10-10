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
 │ pai-run start NAME ──ssh──────────────────┼─────▶│ pai hardware          │
 │ pai-run stop  ──touch output/<run>/STOP ──┼──┐   │   --run-id NAME_...   │
 │                                          │  │   │   --operator $USER    │
 │ /srv/pai/output/<run>/  ◀── Samba ────────┼──┴───┤ writes CSV chunks,   │
 │   nidaq_*.csv  latest.npz  manifest.json │      │ latest.npz, manifest  │
 │                                          │      │                       │
 │ nvidia-smi logger (started by pai over   │◀─ssh─┤ NVML bracket          │
 │   ssh from the VM; files pulled at end)  │      │ (192.168.122.1=gamma2)│
 │ pai dashboard latest --host 0.0.0.0      │      │                       │
 │ pai archive push-ready  (systemd timer)  │      │ NI-DAQmx + USB-6341   │
 └──────────────────────────────────────────┘      └───────────────────────┘
```

The code is the same `pai` package as the 3.102 Windows machine uses, plus a
few controls that make remote driving possible: `--run-id` and `--operator`
on `pai hardware`, `pai stop <run>` (a STOP file the acquisition loop polls),
`pai dashboard --host`, `pai archive push-ready`, and `PAI_OUTPUT_ROOT` honoured
by every subcommand.

## Daily use (on gamma2)

```bash
pai-run start resnet_sweep            # starts acquisition in the VM, prints the run folder + dashboard URL
pai-run status                        # what is running
pai-run stop                          # stops cleanly: NVML files pulled, manifest + checksums written
pai-run with resnet_sweep -- python train.py     # start, run the workload, stop
pai-run start wiring_check --test     # test run: stays local, never archived
pai-run dashboard latest              # (re)open the dashboard on the newest run
```

Run folders land in `/srv/pai/output/<NAME>_<timestamp>/` (test runs under
`output/test/`). The dashboard is `http://gamma2:8000/` from anywhere on the
Harvard network. Completed runs are pushed to the lab archive by the sweep
timer within ten minutes; `pai archive status <run>` (host-side `pai`) shows
where a run is. Anything in the main README about run folders, manifests,
test runs and archive states applies unchanged.

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
sudo usermod -aG pai admin                      # every human who runs pai-run
sudo mkdir -p /srv/pai/output && sudo chown -R pai:pai /srv/pai && sudo chmod 2775 /srv/pai /srv/pai/output
sudo apt install samba
sudo cp host/smb-pai.conf /etc/samba/smb-pai.conf
echo 'include = /etc/samba/smb-pai.conf' | sudo tee -a /etc/samba/smb.conf
sudo systemctl restart smbd
```

The share is guest-accessible but bound to `virbr0` only, so it is reachable
solely from the VM subnet. Everything the VM writes becomes `pai:pai`,
group-writable, which is what lets `pai-run stop` (running as a human in
group `pai`) drop the STOP file.

### 3. Host: this repo, pai-run, time, archive sweep

```bash
sudo -u pai git clone https://github.com/IanMwai/paihardware-gamma2.git /opt/pai/paihardware-gamma2
cd /opt/pai/paihardware-gamma2
sudo -u pai python3 -m venv .venv && sudo -u pai .venv/bin/pip install -e .[archive]
sudo -u pai cp .env.example .env     # set PAI_OUTPUT_ROOT=/srv/pai/output and, later, the Globus endpoint
sudo install -m 755 deploy/gamma2/host/pai-run /usr/local/bin/pai-run
sudo cp deploy/gamma2/host/pai-gamma2.conf.example /etc/pai-gamma2.conf    # edit if paths differ
sudo cp deploy/gamma2/host/chrony-vm.conf /etc/chrony/conf.d/pai-vm.conf && sudo systemctl restart chrony
sudo cp deploy/gamma2/host/pai-archive-sweep.{service,timer} /etc/systemd/system/
sudo systemctl enable --now pai-archive-sweep.timer
```

Humans also want `pai` on their own PATH for `pai archive status` etc.; a
symlink does: `sudo ln -s /opt/pai/paihardware-gamma2/.venv/bin/pai /usr/local/bin/pai`.

SSH key for gamma2 → VM (used by pai-run, as the human running it; one key
per human, or a shared `admin` key):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''    # if you have none
cat ~/.ssh/id_ed25519.pub                            # paste into setup-vm.ps1 -Gamma2PublicKey
```

### 4. VM: Windows side

Done once over RDP (`ssh -L 3389:<vm-ip>:3389 admin@gamma2`, then Windows
App to `127.0.0.1`; `virsh domifaddr daqwin` gives the IP):

1. NI-DAQmx (driver download from ni.com inside the VM, defaults, reboot).
   NI MAX → Devices and Interfaces → `NI USB-6341 "Dev1"` → Self-Test passes.
2. Python 3.10+ from python.org with "Add python.exe to PATH".
3. `git clone https://github.com/IanMwai/paihardware-gamma2.git C:\pai\paihardware-gamma2`
   (Git for Windows, or download the zip and unpack to that path).
4. Elevated PowerShell:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   cd C:\pai\paihardware-gamma2\deploy\gamma2\windows
   .\setup-vm.ps1 -Gamma2PublicKey "ssh-ed25519 AAAA... admin@gamma2"
   ```
   This installs the OpenSSH server with gamma2's key authorised, enables SMB
   guest access for the share, pins Windows Time to gamma2, disables sleep,
   builds the venv (`pip install -e .[hardware,remote]`), writes `.env` from
   `env.vm.example`, runs `pai devices`, and prints a public key.
5. Put that printed key into `/home/admin/.ssh/authorized_keys` on gamma2
   (this is the VM → gamma2 link `pai` uses for the NVML logger).

The Windows account that runs acquisition is a fixed service identity (`daq`);
humans are identified by `--operator` (pai-run passes the Linux username).

### 5. Smoke test

```bash
ssh daq@$(virsh domifaddr daqwin | awk '/ipv4/{print $4}' | cut -d/ -f1) 'C:\pai\paihardware-gamma2\.venv\Scripts\pai.exe devices'
pai-run start smoke --test
pai-run status          # state LIVE, dashboard URL
pai-run stop            # status completed, nvml fetched
ls /srv/pai/output/test/smoke_*/
```

If `nvml` says skipped, the VM → gamma2 key (step 4.5) is missing or
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

* Nobody uses the Windows desktop. Start/stop is `pai-run` on gamma2; the
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

* **`pai-run start` times out** - read `/srv/pai/output/.pai-run/<run>.log`.
  "Permission denied (publickey)": gamma2's key is not in the VM's
  `administrators_authorized_keys`. "device not found" from DAQmx: the VM lost
  the DAQ, see next item.
* **NI MAX says Dev1 not present** - on gamma2,
  `virsh qemu-monitor-command daqwin --hmp 'info usbhost'` should list
  `3923:74f8 USB-6341`; if it is missing, check `lsusb | grep 3923` (cable),
  then `ls -l /dev/bus/usb/001/` (the NI node must be owned by `libvirt-qemu`;
  if not, the udev rule is not applied: `sudo udevadm trigger --action=add -s usb`).
  Unplugging and replugging the DAQ is safe: QEMU re-grabs it within a second.
* **Run shows STALE on the dashboard** - acquisition died (VM rebooted, SSH
  session killed). The CSV chunks written so far are intact. The manifest stays
  `running`; mark it by hand or just start a new run.
* **Share not reachable from the VM** - `Test-Path \\192.168.122.1\pai\output`
  in the VM. If false: `sudo systemctl status smbd` on gamma2, and in the VM
  confirm `AllowInsecureGuestAuth` is 1 (setup-vm.ps1 sets it).
* **Sweep timer pushes nothing** - `sudo -u pai -H .venv/bin/pai archive push-ready`
  by hand prints the reason (usually `globus login` expired).
