# Telegram userbot bridge (Telethon + FastAPI).
# Build context: repository root (see docker-compose.yml).
FROM python:3.12.14-slim-bookworm

# Not 1000: that is the first login user on the host, who could then reach the session via /proc/<pid>/root.
ARG APP_UID=10002
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Kyiv \
    PATH=/opt/bridge/venv/bin:$PATH

RUN useradd -m -u "$APP_UID" -s /usr/sbin/nologin tg \
 && mkdir -p /session /data \
 && chown tg:tg /session /data \
 && chmod 700 /session /data

COPY telegram-userbot/requirements.lock /opt/bridge/requirements.lock
RUN python -m venv /opt/bridge/venv \
 && pip install --no-cache-dir --no-deps -r /opt/bridge/requirements.lock \
 && pip check

COPY common/bridge_core /opt/bridge/bridge_core
COPY telegram-userbot/app /opt/bridge/app
COPY telegram-userbot/login.py /opt/bridge/login.py

USER tg
WORKDIR /opt/bridge
EXPOSE 8102

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8102/health', headers={'X-Bridge-Secret': os.environ['BRIDGE_SECRET']}), timeout=8)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8102", "--no-access-log", "--log-level", "warning"]
