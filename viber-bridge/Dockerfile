# Viber Desktop (Xvfb + fluxbox + x11vnc + noVNC) + FastAPI bridge.
# Build context: repository root (see docker-compose.yml).
FROM ubuntu:noble-20260911

# viber.deb is served from an unversioned URL: the checksum pins the exact build.
# Get it with: curl -fsSL "$VIBER_DEB_URL" | sha256sum
ARG VIBER_DEB_URL=https://download.cdn.viber.com/cdn/desktop/Linux/viber.deb
ARG VIBER_DEB_SHA256
# Not 1000: that is the first login user on the host, who could then reach the session via /proc/<pid>/root.
ARG APP_UID=10001

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
      python3 python3-venv sqlite3 dbus-bin \
      at-spi2-core gir1.2-atspi-2.0 python3-gi \
      tesseract-ocr tesseract-ocr-ukr tesseract-ocr-eng \
      fonts-dejavu-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -m 1777 /tmp/.X11-unix

# viber.deb bundles Qt but declares only a few of the system libs it links against.
# List = NEEDED of Viber, libqxcb, QtWebEngineProcess and bundled Qt libs, minus bundled ones.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libasound2t64 libatomic1 libbrotli1 libdbus-1-3 libdrm2 libexpat1 libfontconfig1 libfreetype6 \
      libegl1 libgbm1 libgl1 libgl1-mesa-dri libglx0 libopengl0 \
      libglib2.0-0t64 libgssapi-krb5-2 libnspr4 libnss3 libopus0 libpulse0 libsnappy1v5 libssl3t64 libudev1 \
      libice6 libsm6 libx11-6 libx11-xcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxi6 \
      libxkbcommon0 libxkbcommon-x11-0 libxkbfile1 libxrandr2 libxrender1 libxshmfence1 libxss1 libxtst6 \
      libxcb1 libxcb-dri3-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render0 \
      libxcb-render-util0 libxcb-shape0 libxcb-shm0 libxcb-sync1 libxcb-util1 libxcb-xfixes0 libxcb-xkb1 \
      libxml2 libxslt1.1 libzstd1 zlib1g \
 && rm -rf /var/lib/apt/lists/*

RUN test -n "$VIBER_DEB_SHA256" || { echo "VIBER_DEB_SHA256 build arg is required" >&2; exit 1; } \
 && curl -fsSL -o /tmp/viber.deb "$VIBER_DEB_URL" \
 && echo "$VIBER_DEB_SHA256  /tmp/viber.deb" | sha256sum -c - \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/viber.deb \
 && rm -rf /tmp/viber.deb /var/lib/apt/lists/* \
 && test -x /opt/viber/Viber \
 && missing=$(ldd /opt/viber/Viber /opt/viber/plugins/platforms/libqxcb.so /opt/viber/libexec/QtWebEngineProcess \
      | awk '/not found/ {print $1}' | sort -u) \
 && { [ -z "$missing" ] || { echo "Viber needs missing libs (exit 127 at runtime):" $missing >&2; exit 1; }; }

# Ubuntu images ship user "ubuntu" with uid 1000; replace it with "viber".
RUN (userdel -r ubuntu 2>/dev/null || true) \
 && useradd -m -u "$APP_UID" -s /bin/bash viber \
 && mkdir -p /home/viber/.ViberPC /data /debug \
 && chown -R viber:viber /home/viber /data /debug \
 && chmod 700 /home/viber/.ViberPC /data /debug

COPY viber-bridge/requirements.lock /opt/bridge/requirements.lock
RUN python3 -m venv /opt/bridge/venv \
 && /opt/bridge/venv/bin/pip install --no-cache-dir --no-deps -r /opt/bridge/requirements.lock \
 && /opt/bridge/venv/bin/pip check \
 && ln -s /usr/lib/python3/dist-packages/gi /opt/bridge/venv/lib/python3.12/site-packages/gi \
 && /opt/bridge/venv/bin/python -c "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi, Gio, GLib"

COPY common/bridge_core /opt/bridge/bridge_core
COPY viber-bridge/app /opt/bridge/app
COPY viber-bridge/tools /opt/bridge/tools
COPY viber-bridge/supervisord.conf /etc/supervisor/viber-bridge.conf
COPY --chmod=755 viber-bridge/entrypoint.sh /usr/local/bin/entrypoint.sh

USER viber
WORKDIR /opt/bridge
EXPOSE 8101 6080

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD /opt/bridge/venv/bin/python -c "import os,urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8101/health', headers={'X-Bridge-Secret': os.environ['BRIDGE_SECRET']}), timeout=8)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
