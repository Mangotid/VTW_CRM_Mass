#!/usr/bin/env bash
# PDM messenger hub: initial server setup (Ubuntu 24.04).
# Idempotent: safe to re-run; every step checks current state first.
#
# Usage: sudo ./ops/bootstrap.sh [step ...]
#   steps (default: all, in this order):
#     preflight packages timezone swap user ssh firewall docker unattended backup
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${BOOTSTRAP_CONF:-$SCRIPT_DIR/bootstrap.conf}"
LOG=/var/log/pdm-bootstrap.log
ALL_STEPS=(preflight packages timezone swap user ssh firewall docker unattended backup)
DOCKER_GPG_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88

# ---------------------------------------------------------------- helpers

log()  { printf '%s [%s] %s\n' "$(date '+%F %T')" "${STEP:-main}" "$*" | tee -a "$LOG"; }
warn() { log "WARNING: $*"; }
die()  { log "ERROR: $*"; exit 1; }
trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

# Write a file only if content differs; returns 0 if changed, 1 if unchanged.
write_if_changed() {
  local path=$1 mode=$2 tmp
  tmp=$(mktemp)
  cat >"$tmp"
  if [[ -f "$path" ]] && cmp -s "$tmp" "$path"; then
    rm -f "$tmp"; return 1
  fi
  install -D -m "$mode" -o root -g root "$tmp" "$path"
  rm -f "$tmp"
  log "wrote $path"
  return 0
}

apt_install() {
  local missing=()
  for p in "$@"; do
    dpkg-query -W -f='${Status}' "${p%%=*}" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
  done
  if ((${#missing[@]})); then
    log "installing: ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" >>"$LOG" 2>&1
  fi
}

# ---------------------------------------------------------------- steps

step_preflight() {
  [[ $EUID -eq 0 ]] || die "run as root (sudo)"
  # shellcheck source=/dev/null
  . /etc/os-release
  [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || die "expected Ubuntu 24.04, got ${PRETTY_NAME:-unknown}"
  [[ "$(uname -m)" == x86_64 ]] || die "expected x86_64"
  [[ -f "$CONF" ]] || die "config not found: $CONF (copy ops/bootstrap.conf.example)"

  local key_tmp
  key_tmp=$(mktemp)
  printf '%s\n' "$DEPLOY_SSH_PUBKEYS" | sed '/^\s*$/d' >"$key_tmp"
  [[ -s "$key_tmp" ]] || die "DEPLOY_SSH_PUBKEYS is empty"
  grep -q 'replace' "$key_tmp" && die "DEPLOY_SSH_PUBKEYS still contains the example key"
  ssh-keygen -l -f "$key_tmp" >/dev/null 2>&1 || die "DEPLOY_SSH_PUBKEYS is not a valid public key list"
  rm -f "$key_tmp"

  [[ "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid DEPLOY_USER"
  [[ "$SSH_PORT" =~ ^[0-9]+$ && "$WG_PORT" =~ ^[0-9]+$ && "$SWAP_SIZE_GB" =~ ^[0-9]+$ ]] || die "ports/swap must be numeric"
  if ! ss -Htln "sport = :$SSH_PORT" | grep -q .; then
    die "nothing listens on TCP $SSH_PORT; SSH_PORT must be the current sshd port"
  fi
  log "preflight ok ($PRETTY_NAME, config $CONF)"
}

step_packages() {
  log "apt update + full-upgrade"
  apt-get update >>"$LOG" 2>&1
  DEBIAN_FRONTEND=noninteractive apt-get -y \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
    full-upgrade >>"$LOG" 2>&1
  apt_install ca-certificates curl gnupg ufw wireguard-tools unattended-upgrades jq sqlite3 rsync
  [[ -f /var/run/reboot-required ]] && warn "reboot required after upgrades (reboot when convenient)"
  return 0
}

step_timezone() {
  if [[ "$(timedatectl show -p Timezone --value)" != "$TIMEZONE" ]]; then
    timedatectl set-timezone "$TIMEZONE"
    log "timezone set to $TIMEZONE"
  fi
  timedatectl set-ntp true
  log "timezone $TIMEZONE, NTP on"
}

step_swap() {
  local size=$((SWAP_SIZE_GB * 1024 * 1024 * 1024))
  if swapon --show=NAME --noheadings | grep -qx /swapfile; then
    log "/swapfile already active"
  elif [[ -n "$(swapon --show --noheadings)" ]]; then
    log "other swap already active, leaving as is: $(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
  else
    if [[ ! -f /swapfile ]]; then
      fallocate -l "$size" /swapfile
      chmod 600 /swapfile
      mkswap /swapfile >>"$LOG"
      log "created /swapfile (${SWAP_SIZE_GB}G)"
    fi
    chmod 600 /swapfile
    swapon /swapfile
  fi
  if [[ -f /swapfile ]] && ! grep -qE '^/swapfile\s' /etc/fstab; then
    cp -a /etc/fstab "/etc/fstab.pdm-bak.$(date +%Y%m%d%H%M%S)"
    echo '/swapfile none swap sw 0 0' >>/etc/fstab
    log "added /swapfile to /etc/fstab"
  fi
  write_if_changed /etc/sysctl.d/60-pdm-swap.conf 644 <<'EOF' && sysctl -q -p /etc/sysctl.d/60-pdm-swap.conf || true
vm.swappiness = 10
EOF
}

step_user() {
  if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos '' "$DEPLOY_USER" >>"$LOG"
    log "created user $DEPLOY_USER (no password; key login only)"
  fi
  if [[ "$DEPLOY_SUDO" == yes ]] && ! id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx sudo; then
    usermod -aG sudo "$DEPLOY_USER"
    log "added $DEPLOY_USER to sudo (set a password: passwd $DEPLOY_USER)"
  fi
  local home ak
  home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
  ak="$home/.ssh/authorized_keys"
  install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
  touch "$ak"
  while IFS= read -r key; do
    [[ -z "${key// }" ]] && continue
    grep -qxF "$key" "$ak" || { echo "$key" >>"$ak"; log "added SSH key: ${key%% *} ...${key: -20}"; }
  done <<<"$DEPLOY_SSH_PUBKEYS"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$ak"
  chmod 600 "$ak"
}

step_ssh() {
  local home ak
  home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6) || die "run step 'user' first"
  ak="$home/.ssh/authorized_keys"
  [[ -s "$ak" ]] || die "$ak is empty: refusing to disable password login"

  local root_login=prohibit-password
  [[ "$SSH_DISABLE_ROOT_LOGIN" == yes ]] && root_login=no
  # sshd uses the first value it reads; drop-ins are read in lexical order. 10- beats
  # cloud-init's 50-, but a provider file like 00-*.conf can still win: checked below.
  if write_if_changed /etc/ssh/sshd_config.d/10-pdm-hardening.conf 644 <<EOF
# Managed by ops/bootstrap.sh
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PermitRootLogin $root_login
X11Forwarding no
MaxAuthTries 4
LoginGraceTime 30
EOF
  then
    install -d -m 755 /run/sshd
    sshd -t || { rm -f /etc/ssh/sshd_config.d/10-pdm-hardening.conf; die "sshd config test failed, change reverted"; }
    # Ubuntu 24.04 uses ssh.socket; ssh.service may be inactive until the first connection.
    if systemctl is-active -q ssh; then systemctl reload ssh; fi
    log "sshd reloaded: key-only login, PermitRootLogin $root_login"
    warn "KEEP THIS SESSION OPEN. In a new terminal check: ssh -p $SSH_PORT $DEPLOY_USER@<host>"
  else
    log "sshd hardening already in place"
  fi
  check_sshd_effective "$root_login"
}

# Fails if another sshd config file overrides our drop-in (effective values from sshd -T).
check_sshd_effective() {
  local want_root=$1 eff key want got bad=0
  install -d -m 755 /run/sshd
  eff=$(sshd -T 2>/dev/null) || die "sshd -T failed"
  for pair in passwordauthentication=no kbdinteractiveauthentication=no "permitrootlogin=$want_root"; do
    key=${pair%%=*} want=${pair#*=}
    got=$(awk -v k="$key" '$1 == k { print $2; exit }' <<<"$eff")
    [[ "$got" == "$want" ]] && continue
    bad=1
    warn "effective $key is '$got', expected '$want'. Set in:"
    grep -HinE "^[[:space:]]*${key}[[:space:]]" /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null \
      | while IFS= read -r line; do warn "  $line"; done
  done
  ((bad == 0)) || die "another sshd config file overrides 10-pdm-hardening.conf (first value wins); fix the lines above, then: sshd -t && systemctl reload ssh"
  log "sshd effective config ok: password login off, PermitRootLogin $want_root"
}

step_firewall() {
  {
    ufw default deny incoming
    ufw default allow outgoing
    ufw limit "$SSH_PORT/tcp" comment 'ssh'
    ufw allow "$WG_PORT/udp" comment 'wireguard'
  } >>"$LOG"
  if ! ufw status | grep -q '^Status: active'; then
    ufw --force enable >>"$LOG"
    log "ufw enabled"
  fi
  log "ufw: deny incoming except ${SSH_PORT}/tcp (limited), ${WG_PORT}/udp"
  # Note: Docker-published ports bypass ufw; bridge ports are bound to the WireGuard IP only.
}

step_docker() {
  for p in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed'; then
      die "conflicting package '$p' is installed; remove it manually after checking its containers/data"
    fi
  done

  install -d -m 755 /etc/apt/keyrings
  if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod 644 /etc/apt/keyrings/docker.asc
  fi
  gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc 2>/dev/null | grep -q "^fpr:::::::::$DOCKER_GPG_FPR:" \
    || die "Docker GPG key fingerprint mismatch (/etc/apt/keyrings/docker.asc)"

  # shellcheck source=/dev/null
  . /etc/os-release
  if write_if_changed /etc/apt/sources.list.d/docker.sources 644 <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $VERSION_CODENAME
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  then
    apt-get update >>"$LOG" 2>&1
  fi

  local want=(
    "docker-ce=$DOCKER_CE_VERSION"
    "docker-ce-cli=$DOCKER_CE_VERSION"
    "containerd.io=$CONTAINERD_VERSION"
    "docker-buildx-plugin=$DOCKER_BUILDX_VERSION"
    "docker-compose-plugin=$DOCKER_COMPOSE_VERSION"
  )
  local spec pkg ver cur
  for spec in "${want[@]}"; do
    pkg=${spec%%=*}; ver=${spec#*=}
    cur=$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null || true)
    if [[ -n "$cur" && "$cur" != "$ver" ]]; then
      warn "$pkg is $cur (config wants $ver); not changing an installed Docker. Upgrade deliberately."
    fi
  done
  apt_install "${want[@]}"
  apt-mark hold docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >>"$LOG"

  if [[ -f /etc/docker/daemon.json ]]; then
    log "/etc/docker/daemon.json exists, not touching it"
  else
    write_if_changed /etc/docker/daemon.json 644 <<'EOF' || true
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "live-restore": true
}
EOF
    if [[ -z "$(docker ps -q 2>/dev/null)" ]]; then
      systemctl restart docker
    else
      warn "containers are running; restart Docker manually to apply daemon.json"
    fi
  fi

  # Containers publish ports on the WireGuard IP: start Docker after wg0 is up.
  if write_if_changed /etc/systemd/system/docker.service.d/10-after-wireguard.conf 644 <<'EOF'
[Unit]
Wants=wg-quick@wg0.service
After=wg-quick@wg0.service
EOF
  then
    systemctl daemon-reload
  fi

  systemctl enable --now docker >>"$LOG" 2>&1
  if ! id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx docker; then
    usermod -aG docker "$DEPLOY_USER"
    log "added $DEPLOY_USER to docker group (root-equivalent; re-login to apply)"
  fi
  log "docker $(docker version --format '{{.Server.Version}}'), compose $(docker compose version --short)"
}

step_unattended() {
  write_if_changed /etc/apt/apt.conf.d/20auto-upgrades 644 <<'EOF' || true
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
  local reboot=false
  [[ "$AUTO_REBOOT" == yes ]] && reboot=true
  write_if_changed /etc/apt/apt.conf.d/52pdm-unattended-upgrades 644 <<EOF || true
// Managed by ops/bootstrap.sh. Docker packages are on hold and come from a non-security origin.
Unattended-Upgrade::Automatic-Reboot "$reboot";
Unattended-Upgrade::Automatic-Reboot-Time "$AUTO_REBOOT_TIME";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
EOF
  systemctl enable --now unattended-upgrades >>"$LOG" 2>&1
  log "unattended-upgrades on (auto reboot: $reboot)"
}

step_backup() {
  if [[ -z "${BACKUP_UUID:-}" ]]; then
    log "BACKUP_UUID empty, skipping. Disks:"
    lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINTS | tee -a "$LOG"
    return 0
  fi
  local dev fstype root_dev
  dev=$(blkid -U "$BACKUP_UUID" 2>/dev/null) || die "no block device with UUID $BACKUP_UUID"
  fstype=$(blkid -s TYPE -o value "$dev")
  [[ -n "$fstype" ]] || die "$dev has no filesystem; create one manually (the script never formats)"
  root_dev=$(findmnt -n -o SOURCE /)
  [[ "$dev" != "$root_dev" ]] || die "UUID $BACKUP_UUID is the root filesystem"

  install -d -m 700 -o root -g root "$BACKUP_MOUNT"
  if ! grep -qE "^UUID=$BACKUP_UUID\s" /etc/fstab; then
    if grep -qE "^\S+\s+$BACKUP_MOUNT\s" /etc/fstab; then
      die "$BACKUP_MOUNT already has a different fstab entry; fix /etc/fstab manually"
    fi
    cp -a /etc/fstab "/etc/fstab.pdm-bak.$(date +%Y%m%d%H%M%S)"
    printf 'UUID=%s %s %s defaults,nofail,noatime,x-systemd.device-timeout=10s 0 2\n' \
      "$BACKUP_UUID" "$BACKUP_MOUNT" "$fstype" >>/etc/fstab
    systemctl daemon-reload
    log "added $BACKUP_MOUNT ($fstype, $dev) to /etc/fstab"
  fi
  if ! findmnt -n "$BACKUP_MOUNT" >/dev/null; then
    mount "$BACKUP_MOUNT"
  fi
  [[ "$(findmnt -n -o UUID "$BACKUP_MOUNT")" == "$BACKUP_UUID" ]] || die "$BACKUP_MOUNT is mounted from a different device"
  chown root:root "$BACKUP_MOUNT"
  chmod 700 "$BACKUP_MOUNT"
  log "backup disk mounted at $BACKUP_MOUNT ($(df -h --output=size,avail "$BACKUP_MOUNT" | tail -1 | xargs))"
}

# ---------------------------------------------------------------- main

main() {
  [[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
  touch "$LOG"; chmod 600 "$LOG"
  [[ -f "$CONF" ]] || { echo "config not found: $CONF (copy ops/bootstrap.conf.example)" >&2; exit 1; }
  # shellcheck source=bootstrap.conf.example
  . "$CONF"

  local steps=("$@")
  ((${#steps[@]})) || steps=("${ALL_STEPS[@]}")
  for s in "${steps[@]}"; do
    declare -F "step_$s" >/dev/null || die "unknown step: $s (known: ${ALL_STEPS[*]})"
  done
  [[ "${steps[0]}" == preflight ]] || { STEP=preflight; step_preflight; }
  for s in "${steps[@]}"; do
    STEP=$s
    log "--- $s"
    "step_$s"
  done
  STEP=main
  log "done. Log: $LOG"
  [[ -f /var/run/reboot-required ]] && warn "reboot required"
  return 0
}

main "$@"
