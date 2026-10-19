#!/usr/bin/env bash
# Prepare runtime state and hand over to supervisord. Runs as user "viber".
set -euo pipefail

die() { echo "{\"level\":\"error\",\"logger\":\"entrypoint\",\"event\":\"$1\"}" >&2; exit 1; }

[[ -n "${BRIDGE_SECRET:-}" ]] || die "BRIDGE_SECRET is not set"
[[ ${#BRIDGE_SECRET} -ge 32 ]] || die "BRIDGE_SECRET must be at least 32 characters"
[[ -n "${ODOO_URL:-}" ]] || die "ODOO_URL is not set"
[[ -n "${VNC_PASSWORD:-}" ]] || die "VNC_PASSWORD is not set"

export SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1280x800x24}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/dbus-session"

for dir in /home/viber/.ViberPC /data /debug; do
  [[ -w "$dir" ]] || die "$dir is not writable by uid $(id -u)"
done

# Stale X lock after an unclean stop.
rm -f /tmp/.X0-lock /tmp/.X11-unix/X0

# VNC password file lives in /tmp only (never in a volume).
install -d -m 700 /tmp/vnc
x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vnc/passwd >/dev/null 2>&1
chmod 600 /tmp/vnc/passwd

# Route viber:// deep links to Viber Desktop (.desktop name to be confirmed on first build).
if [[ -f /usr/share/applications/viber.desktop ]]; then
  xdg-mime default viber.desktop x-scheme-handler/viber
else
  echo '{"level":"warning","logger":"entrypoint","event":"viber.desktop not found, xdg-open may not handle viber://"}'
fi

# Children must not inherit the VNC password.
exec env -u VNC_PASSWORD /usr/bin/supervisord -c /etc/supervisor/viber-bridge.conf
