#!/bin/bash
# Prepare /config, then run MegaDL as the Unraid user (PUID/PGID, default
# nobody:users = 99:100) so downloaded files get normal share ownership.
set -euo pipefail

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-000}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

echo "MegaDL: starting as uid=${PUID} gid=${PGID} umask=${UMASK}"

if ! getent group "${PGID}" >/dev/null; then
    groupadd -g "${PGID}" megadl
fi
if ! getent passwd "${PUID}" >/dev/null; then
    useradd -u "${PUID}" -g "${PGID}" -d "${CONFIG_DIR}" -M -s /usr/sbin/nologin megadl
fi

mkdir -p "${CONFIG_DIR}/.megaCmd"
chown "${PUID}:${PGID}" "${CONFIG_DIR}"
chown -R "${PUID}:${PGID}" "${CONFIG_DIR}/.megaCmd"
chmod 700 "${CONFIG_DIR}/.megaCmd"   # MEGAcmd refuses folders others can read
find "${CONFIG_DIR}" -maxdepth 1 -name 'megadl.db*' -exec chown "${PUID}:${PGID}" {} +

# MEGA's SDK identifies the device by machine-id; keep it stable across
# container re-creation (Unraid recreates containers on every update).
if [ ! -s "${CONFIG_DIR}/machine-id" ]; then
    tr -d '-' < /proc/sys/kernel/random/uuid > "${CONFIG_DIR}/machine-id"
fi
cp "${CONFIG_DIR}/machine-id" /etc/machine-id

if ! setpriv --reuid="${PUID}" --regid="${PGID}" --init-groups test -w "${DOWNLOAD_ROOT:-/data}"; then
    echo "MegaDL: WARNING: ${DOWNLOAD_ROOT:-/data} is not writable by uid ${PUID}." \
         "Check the path mapping and share permissions."
fi

umask "${UMASK}"
export HOME="${CONFIG_DIR}"
exec setpriv --reuid="${PUID}" --regid="${PGID}" --init-groups python3 -m megadl
