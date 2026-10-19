#!/usr/bin/env bash
# gamma2: make the measurement stack usable by every lab member, not only the
# admin who installed it. Idempotent; rerun whenever something is added.
#
#   sudo bash deploy/gamma2/host/setup-shared.sh [--users alice,bob] [--vllm]
#                                                [--data-volume 10T]
#
# What it does:
#   * group `pai` owns the data; listed users join it (log out/in to apply).
#   * /srv/pai/output is group-writable with setgid, so runs started by any
#     user land as pai:pai and the sweep timer (user pai) can archive them.
#   * /opt/pai/pai-workloads: the workloads repo + venv; its commands go on
#     everyone's PATH via /usr/local/bin (nhpp-load, pai-campaign, ...).
#   * --vllm: /opt/vllm venv with vLLM, `vllm` on PATH (a ~3 GB install).
#   * /srv/hf: one shared Hugging Face cache (HF_HOME) so a model is
#     downloaded once; set for all logins in /etc/profile.d/pai.sh.
#   * --data-volume SIZE: carve a dedicated LVM volume for /srv/pai so runs can
#     never fill the root disk again (that pauses the VM). Refuses while a run
#     is in progress; stops smbd/GCP for the copy, then restarts them.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

USERS=""; WITH_VLLM=0; DATA_VOL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --users) USERS="$2"; shift 2 ;;
    --vllm) WITH_VLLM=1; shift ;;
    --data-volume) DATA_VOL="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done

PAI_HOME=/opt/pai
PIPELINE=$PAI_HOME/paihardware-gamma2
WORKLOADS=$PAI_HOME/pai-workloads
WORKLOADS_REPO=https://github.com/IanMwai/pai-workloads.git
HF=/srv/hf

log() { echo "[setup-shared] $*"; }

# ---------------------------------------------------------------- group + data dir
getent group pai >/dev/null || groupadd pai
id pai >/dev/null 2>&1 || { echo "user 'pai' missing - run the base setup first"; exit 1; }
for u in ${USERS//,/ }; do
  id "$u" >/dev/null 2>&1 || { echo "no such user: $u"; exit 1; }
  # pai: write runs; libvirt: `pai hardware` asks libvirt for the VM's address
  usermod -aG pai,libvirt "$u"; log "added $u to groups pai, libvirt"
done
install -d -o pai -g pai -m 2775 /srv/pai/output/.pai/campaigns
install -d -o pai -g pai -m 2775 /srv/pai /srv/pai/output /srv/pai/output/test /srv/pai/output/.pai
chmod 2775 /srv/pai/output /srv/pai/output/test /srv/pai/output/.pai
# existing runs: group-writable so any member can `pai stop` / write workload logs
find /srv/pai/output -mindepth 1 -maxdepth 2 -type d ! -perm -g+w -exec chmod g+w {} + 2>/dev/null || true

# ---------------------------------------------------------------- VM ssh keys
# `pai hardware` reaches the VM as the human running it, so each user's key
# must be in the VM's administrators_authorized_keys. The admin running this
# script already has access; copy the listed users' keys through it.
VM_IP=$(virsh domifaddr daqwin 2>/dev/null | awk '/ipv4/ {sub("/.*","",$4); print $4}' | head -1)
VM_USER=$(grep -E '^PAI_VM_USER=' "$PIPELINE/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
if [[ -n $USERS && -n $VM_IP && -n ${SUDO_USER:-} ]]; then
  for u in ${USERS//,/ }; do
    pub=$(getent passwd "$u" | cut -d: -f6)/.ssh/id_ed25519.pub
    if [[ ! -r $pub ]]; then
      log "$u has no ~/.ssh/id_ed25519.pub yet - they should run: ssh-keygen -t ed25519 -N '' ; then rerun this script"
      continue
    fi
    key=$(cat "$pub")
    # append once; the file is ACL-restricted on Windows, hence PowerShell
    if sudo -u "$SUDO_USER" ssh -o BatchMode=yes -o ConnectTimeout=10 "$VM_USER@$VM_IP" \
        "powershell -NoProfile -Command \"\$f='C:\\ProgramData\\ssh\\administrators_authorized_keys'; if (-not (Select-String -Quiet -SimpleMatch '$key' \$f)) { Add-Content -Path \$f -Value '$key' }\"" ; then
      log "VM accepts $u's ssh key"
    else
      log "could not add $u's key to the VM (is $SUDO_USER's own key accepted there?)"
    fi
  done
fi

# ---------------------------------------------------------------- workloads
if [[ ! -d $WORKLOADS/.git ]]; then
  sudo -u pai -H git clone "$WORKLOADS_REPO" "$WORKLOADS"
else
  sudo -u pai -H git -C "$WORKLOADS" pull --ff-only
fi
[[ -x $WORKLOADS/.venv/bin/python ]] || sudo -u pai -H python3 -m venv "$WORKLOADS/.venv"
sudo -u pai -H "$WORKLOADS/.venv/bin/pip" install -q -e "$WORKLOADS"
for cmd in "$WORKLOADS"/.venv/bin/{nhpp-load,pai-campaign}; do
  [[ -x $cmd ]] && ln -sf "$cmd" /usr/local/bin/"$(basename "$cmd")"
done
ln -sf "$PIPELINE/.venv/bin/pai" /usr/local/bin/pai
[[ -x $PIPELINE/.venv/bin/globus ]] && ln -sf "$PIPELINE/.venv/bin/globus" /usr/local/bin/globus
log "commands on PATH: $(ls /usr/local/bin | grep -E '^(pai|nhpp-load|pai-campaign|globus|vllm)$' | tr '\n' ' ')"

# ---------------------------------------------------------------- shared HF cache
install -d -o pai -g pai -m 2775 "$HF"
cat > /etc/profile.d/pai.sh <<'PROFILE'
# PAI GPU power measurement: shared model cache so weights download once.
export HF_HOME=/srv/hf
# Campaign logs where every lab member can see and resume them.
export PAI_CAMPAIGN_LOGS=/srv/pai/output/.pai/campaigns
# Dashboard for the current run (any machine on the lab VPN): http://gamma2:8000/
PROFILE
chmod 644 /etc/profile.d/pai.sh
# adopt models already downloaded into personal caches
for d in /home/*/.cache/huggingface/hub/models--*; do
  [[ -d $d && ! -e $HF/hub/$(basename "$d") ]] || continue
  install -d -o pai -g pai -m 2775 "$HF/hub"
  mv "$d" "$HF/hub/" && chown -R pai:pai "$HF/hub/$(basename "$d")" && log "moved $(basename "$d") into $HF"
done
chmod -R g+w "$HF" 2>/dev/null || true

# ---------------------------------------------------------------- vLLM
if [[ $WITH_VLLM -eq 1 ]]; then
  [[ -x /opt/vllm/bin/python ]] || python3 -m venv /opt/vllm
  log "installing vLLM into /opt/vllm (several minutes) ..."
  /opt/vllm/bin/pip install -q -U pip
  /opt/vllm/bin/pip install -q vllm
  ln -sf /opt/vllm/bin/vllm /usr/local/bin/vllm
  log "vllm $(/opt/vllm/bin/vllm --version 2>/dev/null | tail -1) on PATH"
fi

# ---------------------------------------------------------------- data volume
if [[ -n $DATA_VOL ]]; then
  if mountpoint -q /srv/pai; then
    log "/srv/pai is already its own mount; skipping volume creation"
  else
    if ls /srv/pai/output/*/*_to_PENDING.csv /srv/pai/output/test/*/*_to_PENDING.csv >/dev/null 2>&1; then
      echo "a run is in progress (open chunk found) - stop it first"; exit 1
    fi
    VG=$(vgs --noheadings -o vg_name | head -1 | tr -d ' ')
    log "creating ${DATA_VOL} volume 'pai' in VG $VG"
    lvcreate -L "$DATA_VOL" -n pai "$VG"
    mkfs.ext4 -q -L pai-data "/dev/$VG/pai"
    systemctl stop smbd globusconnectpersonal 2>/dev/null || true
    mkdir -p /mnt/pai-new && mount "/dev/$VG/pai" /mnt/pai-new
    log "copying /srv/pai -> new volume ..."
    rsync -aHAX --info=progress2 /srv/pai/ /mnt/pai-new/
    umount /mnt/pai-new && rmdir /mnt/pai-new
    mv /srv/pai /srv/pai.old
    install -d -o pai -g pai -m 2775 /srv/pai
    grep -q "LABEL=pai-data" /etc/fstab || echo "LABEL=pai-data /srv/pai ext4 defaults,nofail 0 2" >> /etc/fstab
    mount /srv/pai
    chown pai:pai /srv/pai && chmod 2775 /srv/pai
    systemctl start smbd globusconnectpersonal 2>/dev/null || true
    log "/srv/pai now on /dev/$VG/pai; old copy kept at /srv/pai.old (delete when happy)"
  fi
fi

log "done. Users added to the group must log out and in again."
