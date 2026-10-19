#!/usr/bin/env bash
# WireGuard tunnel hub <-> Odoo. Run on each server as root.
#
#   sudo ./wg-setup.sh keys                 # step 1 (both servers): generate keys, print PUBLIC key
#   sudo PEER_PUBLIC_KEY=... ./wg-setup.sh hub
#   sudo PEER_PUBLIC_KEY=... HUB_ENDPOINT=<hub public IP> ./wg-setup.sh odoo
#   sudo ./wg-setup.sh status
#
# Private keys stay in /etc/wireguard/*.key (0600) and are loaded via PostUp,
# so wg0.conf contains no secrets. Idempotent: existing keys/config are never overwritten.
set -Eeuo pipefail

WG_DIR=/etc/wireguard
IFACE=wg0
WG_NET_PREFIX=${WG_NET_PREFIX:-10.77.0}
HUB_IP=${HUB_IP:-$WG_NET_PREFIX.1}
ODOO_IP=${ODOO_IP:-$WG_NET_PREFIX.2}
WG_PORT=${WG_PORT:-51820}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[wg-setup] $*"; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
command -v wg >/dev/null || die "wireguard-tools not installed (apt install wireguard-tools)"

valid_key() { [[ "$1" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$ ]]; }

ensure_keys() {
  install -d -m 700 "$WG_DIR"
  if [[ ! -s "$WG_DIR/$IFACE.key" ]]; then
    (umask 077; wg genkey >"$WG_DIR/$IFACE.key")
    log "generated $WG_DIR/$IFACE.key"
  fi
  chmod 600 "$WG_DIR/$IFACE.key"
  wg pubkey <"$WG_DIR/$IFACE.key" >"$WG_DIR/$IFACE.pub"
  chmod 644 "$WG_DIR/$IFACE.pub"
}

render() {
  local role=$1 tmpl out
  [[ -n "${PEER_PUBLIC_KEY:-}" ]] || die "PEER_PUBLIC_KEY is required (public key of the other server)"
  valid_key "$PEER_PUBLIC_KEY" || die "PEER_PUBLIC_KEY does not look like a WireGuard public key"
  [[ "$PEER_PUBLIC_KEY" != "$(cat "$WG_DIR/$IFACE.pub")" ]] || die "PEER_PUBLIC_KEY is this server's own key"
  if [[ "$role" == odoo ]]; then
    [[ -n "${HUB_ENDPOINT:-}" ]] || die "HUB_ENDPOINT (hub public IP or DNS name) is required"
  fi

  local psk_line=""
  if [[ -s "$WG_DIR/$IFACE.psk" ]]; then
    chmod 600 "$WG_DIR/$IFACE.psk"
    psk_line="PostUp = wg set %i peer $PEER_PUBLIC_KEY preshared-key $WG_DIR/$IFACE.psk"
  else
    log "no $WG_DIR/$IFACE.psk: tunnel will work without a preshared key (see README)"
  fi

  tmpl="$SCRIPT_DIR/$role-wg0.conf.example"
  out=$(mktemp)
  sed -e "s|{{HUB_IP}}|$HUB_IP|g" -e "s|{{ODOO_IP}}|$ODOO_IP|g" -e "s|{{WG_PORT}}|$WG_PORT|g" \
      -e "s|{{PEER_PUBLIC_KEY}}|$PEER_PUBLIC_KEY|g" -e "s|{{HUB_ENDPOINT}}|${HUB_ENDPOINT:-}|g" \
      -e "s|{{KEY_FILE}}|$WG_DIR/$IFACE.key|g" "$tmpl" \
    | awk -v psk="$psk_line" '/{{PSK_POSTUP}}/{ if (psk != "") print psk; next } { print }' >"$out"
  grep -q '{{' "$out" && die "unfilled placeholder in $tmpl"

  if [[ -f "$WG_DIR/$IFACE.conf" ]]; then
    if cmp -s "$out" "$WG_DIR/$IFACE.conf"; then
      log "$WG_DIR/$IFACE.conf is up to date"
    else
      log "$WG_DIR/$IFACE.conf exists and differs; NOT overwriting. Diff (new on the right):"
      diff -u "$WG_DIR/$IFACE.conf" "$out" || true
      rm -f "$out"
      return 0
    fi
  else
    install -m 600 "$out" "$WG_DIR/$IFACE.conf"
    log "wrote $WG_DIR/$IFACE.conf"
  fi
  rm -f "$out"

  systemctl enable "wg-quick@$IFACE" >/dev/null 2>&1
  if systemctl is-active -q "wg-quick@$IFACE"; then
    log "wg-quick@$IFACE already running; apply changes with: systemctl restart wg-quick@$IFACE"
  else
    systemctl start "wg-quick@$IFACE"
    log "wg-quick@$IFACE started"
  fi

  # Containers publish ports on the WireGuard IP: Docker must start after wg0.
  # Only writes a drop-in + daemon-reload; does not restart Docker.
  if [[ -d /etc/systemd/system ]] && command -v docker >/dev/null; then
    local dropin=/etc/systemd/system/docker.service.d/10-after-wireguard.conf
    if [[ ! -f "$dropin" ]]; then
      install -d -m 755 "$(dirname "$dropin")"
      printf '[Unit]\nWants=wg-quick@%s.service\nAfter=wg-quick@%s.service\n' "$IFACE" "$IFACE" >"$dropin"
      systemctl daemon-reload
      log "added $dropin (takes effect on next Docker start)"
    fi
  fi
}

case "${1:-}" in
  keys)
    ensure_keys
    log "PUBLIC key of this server (give it to the other side):"
    cat "$WG_DIR/$IFACE.pub"
    ;;
  psk)
    ensure_keys
    if [[ ! -s "$WG_DIR/$IFACE.psk" ]]; then
      (umask 077; wg genpsk >"$WG_DIR/$IFACE.psk")
      log "generated $WG_DIR/$IFACE.psk; copy it to the other server's $WG_DIR/$IFACE.psk over SSH (0600)"
    else
      log "$WG_DIR/$IFACE.psk already exists"
    fi
    ;;
  hub|odoo)
    ensure_keys
    render "$1"
    ;;
  status)
    wg show "$IFACE" || true
    ;;
  *)
    sed -n '2,12p' "$0"; exit 2
    ;;
esac
