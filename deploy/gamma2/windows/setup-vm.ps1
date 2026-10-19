<#
  setup-vm.ps1 - one-time setup of the daqwin Windows VM for headless, gamma2-
  driven acquisition. Run in an elevated PowerShell inside the VM:

      Set-ExecutionPolicy -Scope Process Bypass
      .\setup-vm.ps1 -Gamma2PublicKey "ssh-ed25519 AAAA... admin@gamma2"

  Prerequisites already done by hand: NI-DAQmx installed and Dev1 present in
  NI MAX; Python 3.12 or 3.13 installed (3.14+ has no prebuilt wheels for
  numpy/pyyaml yet and nidaqmx does not support it: `py install 3.12` with the
  Python install manager, or the python.org installer); this repo cloned to
  C:\pai\paihardware-gamma2 (the path the host expects; change PAI_VM_PAI in
  the host's .env if you put it elsewhere).

  Run it as the Windows account that will run acquisition (an administrator).
  That account name is what PAI_VM_USER on gamma2 must be set to; the script
  prints it at the end.

  What it does:
    1. OpenSSH Server: installed, started, auto-start, firewall open on every
       network profile (libvirt's NAT counts as "Public"); gamma2's public key
       authorised so gamma2's `pai` can SSH in as this (administrator) account.
    2. SMB client set up for gamma2's guest share: insecure-guest logons
       allowed, signing not required (Windows 11 24H2+ demands it by default
       and guest sessions cannot sign), and the client-side caches turned off
       so the STOP file is noticed promptly.
    3. Windows Time pinned to gamma2 (192.168.122.1) so both clocks agree.
    4. Sleep/hibernate disabled (the VM is an appliance).
    5. Python venv (3.12/3.13) + `pip install -e .[hardware,remote]`, `.env`
       from the example with this account's key path filled in.
    6. An SSH key for the NVML link back to gamma2; its public half is printed
       at the end - add it to ~admin/.ssh/authorized_keys on gamma2.
#>
param(
    [Parameter(Mandatory = $true)] [string] $Gamma2PublicKey,
    [string] $RepoDir = "C:\pai\paihardware-gamma2",
    [string] $Gamma2Host = "192.168.122.1"
)
$ErrorActionPreference = "Stop"
if ($Gamma2PublicKey -notmatch '^ssh-(ed25519|rsa) [A-Za-z0-9+/=]+') {
    throw "-Gamma2PublicKey must be the contents of gamma2's ~/.ssh/id_ed25519.pub (starts with 'ssh-ed25519 AAAA')"
}

Write-Host "== 1. OpenSSH Server"
if ((Get-WindowsCapability -Online -Name 'OpenSSH.Server*').State -ne 'Installed') {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
# The capability's firewall rule only covers Private networks; the VM's NAT
# link is classified Public, which silently blocks gamma2.
if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Profile Any
# Administrators authenticate against this file, not ~/.ssh/authorized_keys.
$authKeys = "C:\ProgramData\ssh\administrators_authorized_keys"
if (-not (Test-Path $authKeys) -or -not (Select-String -Path $authKeys -SimpleMatch $Gamma2PublicKey -Quiet)) {
    Add-Content -Path $authKeys -Value $Gamma2PublicKey
}
icacls $authKeys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
# PowerShell as the SSH shell: friendlier than cmd for anything typed by hand.
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
    -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null

Write-Host "== 2. SMB client for gamma2's guest share (only reachable on the VM subnet)"
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters" `
    -Name AllowInsecureGuestAuth -Value 1 -PropertyType DWord -Force | Out-Null
# 24H2+ requires SMB signing on every connection; guest sessions cannot sign.
Set-SmbClientConfiguration -RequireSecuritySignature $false -Force
# No client-side metadata caching: acquisition polls for a STOP file that
# gamma2 creates, and the default "file not found" cache would hide it for
# several seconds. Same for latest.npz / manifest.json freshness.
Set-SmbClientConfiguration -FileNotFoundCacheLifetimeInSeconds 0 `
    -FileInfoCacheLifetimeInSeconds 0 -DirectoryCacheLifetimeInSeconds 0 -Force
Restart-Service LanmanWorkstation -Force
$share = "\\$Gamma2Host\pai\output"
if (Test-Path $share) { Write-Host "   share reachable: $share" }
else { Write-Warning "cannot reach $share - set up smb-pai.conf on gamma2 first; continuing" }

Write-Host "== 3. Time: sync from gamma2"
w32tm /config /manualpeerlist:"$Gamma2Host,0x8" /syncfromflags:manual /reliable:no /update | Out-Null
Restart-Service w32time
w32tm /resync /nowait | Out-Null

Write-Host "== 4. Never sleep"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 0

Write-Host "== 5. Python environment in $RepoDir"
if (-not (Test-Path $RepoDir)) { throw "Repo not found at $RepoDir - clone it there first." }
Push-Location $RepoDir
if (-not (Test-Path ".venv")) {
    # Pick a Python with prebuilt wheels for numpy/pyyaml and nidaqmx support.
    $py = $null
    foreach ($v in "3.13", "3.12") {
        if (& py "-$v" -c "print(1)" 2>$null) { $py = $v; break }
    }
    if (-not $py) {
        throw "No Python 3.12 or 3.13 found. Install one (e.g. `py install 3.12`) and re-run; " +
              "newer Pythons have no prebuilt wheels for the dependencies."
    }
    Write-Host "   creating venv with Python $py"
    & py "-$py" -m venv .venv
}
$pyver = (& .\.venv\Scripts\python.exe -c "import sys; print('%d.%d' % sys.version_info[:2])")
if ($pyver -notin @("3.10", "3.11", "3.12", "3.13")) {
    throw "The existing .venv uses Python $pyver; delete .venv and re-run so it is rebuilt with 3.12/3.13."
}
.\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv\Scripts\pip.exe install --quiet -e ".[hardware,remote]"
if (-not (Test-Path ".env")) {
    # The example assumes the account is called `daq`; substitute the real one.
    (Get-Content "deploy\gamma2\windows\env.vm.example") -replace 'C:\\Users\\daq\\', "C:\Users\$env:USERNAME\" |
        Set-Content ".env"
    Write-Host "   wrote .env from the example (key path under C:\Users\$env:USERNAME)"
}
.\.venv\Scripts\pai.exe devices
Pop-Location

Write-Host "== 6. SSH key for the NVML link back to gamma2"
$sshDir = "$env:USERPROFILE\.ssh"
if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir | Out-Null }
$key = "$sshDir\id_ed25519"
if (-not (Test-Path $key)) { ssh-keygen -t ed25519 -N '""' -f $key -C "daqwin-nvml" | Out-Null }
Write-Host ""
Write-Host "Add this line to /home/admin/.ssh/authorized_keys on gamma2:"
Get-Content "$key.pub"
Write-Host ""
Write-Host "On gamma2, /opt/pai/paihardware-gamma2/.env needs:  PAI_VM_USER=$env:USERNAME"
Write-Host "Done. From gamma2: pai hardware --name smoke --test   (Ctrl+C stops it)"
