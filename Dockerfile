# Base Alpine (musl) volontairement : le Docker 20.10.3 de DSM 7.1 bloque l'appel
# système clone3 qu'utilisent les glibc récentes (Debian bookworm et suivantes),
# ce qui empêcherait Python de créer des threads. musl n'utilise pas clone3.
FROM python:3.15-alpine

ARG APP_VERSION=dev
ARG GIT_SHA=dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data BACKUP_DIR=/backups PORT=8099 LISTEN_HOST=0.0.0.0 \
    APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA}

LABEL org.opencontainers.image.source="https://github.com/jgondellier/ha-guardian" \
      org.opencontainers.image.description="Surveillance et intervention sur Home Assistant depuis un NAS Synology" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

RUN apk upgrade --no-cache && apk add --no-cache iputils tzdata

WORKDIR /srv
COPY requirements.txt .
# pip et setuptools ne servent qu'à l'installation : on les retire de l'image finale
# (ils embarquent leurs propres bibliothèques, parfois vulnérables)
RUN pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y pip setuptools wheel 2>/dev/null; \
    rm -rf /usr/local/lib/python3*/ensurepip
COPY app ./app

# Données persistantes : toujours sur un volume, même si l'on oublie de les monter
# (indispensable avec un système de fichiers en lecture seule)
VOLUME ["/data", "/backups"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8099'), timeout=4)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host ${LISTEN_HOST} --port ${PORT} --proxy-headers --no-server-header"]
