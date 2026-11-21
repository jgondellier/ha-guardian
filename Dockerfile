# Base Debian « bullseye » volontairement : le Docker 20.10.3 de DSM 7.1
# bloque l'appel système clone3 utilisé par les images plus récentes (bookworm),
# ce qui empêche Python de créer des threads.
FROM python:3.11-slim-bullseye

ARG APP_VERSION=dev
ARG GIT_SHA=dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data BACKUP_DIR=/backups PORT=8099 \
    APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA}

LABEL org.opencontainers.image.source="https://github.com/jgondellier/ha-guardian" \
      org.opencontainers.image.description="Surveillance et intervention sur Home Assistant depuis un NAS Synology" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8099'), timeout=4)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
