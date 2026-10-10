<#
  setup-vm.ps1 - one-time setup of the daqwin Windows VM for headless, gamma2-
  driven acquisition. Run in an elevated PowerShell inside the VM:

      Set-ExecutionPolicy -Scope Process Bypass
      .\setup-vm.ps1 -Gamma2PublicKey "ssh-ed25519 AAAA... admin@gamma2"

  Prerequisites already done by hand: NI-DAQmx installed and Dev1 present in
  NI MAX; Python 3.10+ installed with "Add to PATH"; this repo cloned to
  C:\pai\paihardware-gamma2 (the path pai-run expects; change PAI_VM_PAI in
  /etc/pai-gamma2.conf on gamma2 if you put it elsewhere).

  What it does:
    1. OpenSSH Server: installed, started, auto-start; gamma2's public key
       authorised so `pai-run` can SSH in as this (administrator) account.
    2. SMB guest access enabled so the UNC output path on gamma2's Samba share
       works from non-interactive SSH sessions (no stored credentials needed).
    3. Windows Time pinned to gamma2 (192.168.122.1) so both clocks agree.
    4. Sleep/hibernate disabled (the VM is an appliance).
    5. Python venv + `pip install -e .[hardware,remote]`, `.env` from the example.
    6. An SSH key for the NVML link back to gamma2; its public half is printed
       at the end - add it to ~admin/.ssh/authorized_keys on gamma2.
#>
param(
    [Parameter(Mandatory = $true)] [string] $Gamma2PublicKey,
    [string] $RepoDir = "C:\pai\paihardware-gamma2",
    [string] $Gamma2Host = "192.168.122.1"
)
$ErrorActionPreference = "Stop"

Write-Host "== 1. OpenSSH Server"
if ((Get-WindowsCapability -Online -Name 'OpenSSH.Server*').State -ne 'Installed') {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
# Administrators authenticate against this file, not ~/.ssh/authorized_keys.
$authKeys = "C:\ProgramData\ssh\administrators_authorized_keys"
if (-not (Test-Path $authKeys) -or -not (Select-String -Path $authKeys -SimpleMatch $Gamma2PublicKey -Quiet)) {
    Add-Content -Path $authKeys -Value $Gamma2PublicKey
}
icacls $authKeys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
# PowerShell as the SSH shell: friendlier than cmd for anything typed by hand.
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
    -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null

Write-Host "== 2. SMB guest access (gamma2's share is only reachable on the VM subnet)"
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters" `
    -Name AllowInsecureGuestAuth -Value 1 -PropertyType DWord -Force | Out-Null
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
if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv\Scripts\pip.exe install --quiet -e ".[hardware,remote]"
if (-not (Test-Path ".env")) {
    Copy-Item "deploy\gamma2\windows\env.vm.example" ".env"
    Write-Host "   wrote .env from the example (edit if your paths differ)"
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
Write-Host "Done. From gamma2: pai-run start smoke_test --test   (then pai-run stop)"
