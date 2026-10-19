# Viber Desktop (Xvfb + fluxbox + x11vnc + noVNC) + FastAPI bridge.
FROM ubuntu:noble-20260911

# viber.deb is served from an unversioned URL: the checksum pins the exact build.
# Get it with: curl -fsSL "$VIBER_DEB_URL" | sha256sum
ARG VIBER_DEB_URL=https://download.cdn.viber.com/cdn/desktop/Linux/viber.deb
ARG VIBER_DEB_SHA256
ARG APP_UID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Europe/Kyiv \
    DISPLAY=:0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl tzdata \
      xvfb fluxbox x11vnc novnc websockify supervisor \
      xclip xdotool xdg-utils x11-utils imagemagick dbus-x11 \
      python3 python3-venv sqlite3 \
      fonts-dejavu-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

RUN test -n "$VIBER_DEB_SHA256" || { echo "VIBER_DEB_SHA256 build arg is required" >&2; exit 1; } \
 && curl -fsSL -o /tmp/viber.deb "$VIBER_DEB_URL" \
 && echo "$VIBER_DEB_SHA256  /tmp/viber.deb" | sha256sum -c - \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/viber.deb \
 && rm -rf /tmp/viber.deb /var/lib/apt/lists/* \
 && test -x /opt/viber/Viber

# Ubuntu images ship user "ubuntu" with uid 1000; replace it with "viber".
RUN (userdel -r ubuntu 2>/dev/null || true) \
 && useradd -m -u "$APP_UID" -s /bin/bash viber \
 && mkdir -p /home/viber/.ViberPC /data /debug \
 && chown -R viber:viber /home/viber /data /debug \
 && chmod 700 /home/viber/.ViberPC /data /debug

COPY requirements.lock /opt/bridge/requirements.lock
RUN python3 -m venv /opt/bridge/venv \
 && /opt/bridge/venv/bin/pip install --no-cache-dir --no-deps -r /opt/bridge/requirements.lock \
 && /opt/bridge/venv/bin/pip check

COPY app /opt/bridge/app
COPY supervisord.conf /etc/supervisor/viber-bridge.conf
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh

USER viber
WORKDIR /opt/bridge
EXPOSE 8101 6080

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD /opt/bridge/venv/bin/python -c "import os,urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8101/health', headers={'X-Bridge-Secret': os.environ['BRIDGE_SECRET']}), timeout=8)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
