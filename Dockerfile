# syntax=docker/dockerfile:1
#
# MegaDL: web GUI for downloading mega.nz links, built on MEGA's official
# command-line client (MEGAcmd). Designed for Unraid, works anywhere.

FROM debian:12-slim

# MEGA publishes MEGAcmd packages per distro: https://mega.nz/linux/repo/
ARG MEGA_DISTRO=Debian_12

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HOME=/config \
    PUID=99 \
    PGID=100 \
    UMASK=000 \
    WEBUI_PORT=8080 \
    DOWNLOAD_ROOT=/data \
    CONFIG_DIR=/config

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg tini \
        python3 python3-flask python3-waitress; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL "https://mega.nz/linux/repo/${MEGA_DISTRO}/Release.key" \
        | gpg --dearmor -o /etc/apt/keyrings/meganz-archive-keyring.gpg; \
    printf 'Types: deb\nURIs: https://mega.nz/linux/repo/%s/\nSuites: ./\nSigned-By: /etc/apt/keyrings/meganz-archive-keyring.gpg\n' \
        "${MEGA_DISTRO}" > /etc/apt/sources.list.d/megaio.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends megacmd; \
    rm -rf /var/lib/apt/lists/*; \
    command -v mega-cmd-server; \
    command -v mega-get

COPY app/ /app/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh && mkdir -p /config /data

WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WEBUI_PORT}/healthz" >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
