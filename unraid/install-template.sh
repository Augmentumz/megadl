#!/bin/bash
# Adds the MegaDL template to Unraid so it shows up under
# Docker > Add Container > Template.
#
# Run in the Unraid terminal, either with the URL of your published template:
#   bash install-template.sh https://raw.githubusercontent.com/<you>/megadl/main/unraid/megadl.xml
# or with a local file:
#   bash install-template.sh /mnt/user/appdata/megadl-src/unraid/megadl.xml
set -euo pipefail

SRC="${1:-}"
DEST_DIR=/boot/config/plugins/dockerMan/templates-user
DEST="${DEST_DIR}/my-MegaDL.xml"

if [ -z "${SRC}" ]; then
    echo "Usage: $0 <template URL or path to megadl.xml>" >&2
    exit 1
fi
if [ ! -d "${DEST_DIR}" ]; then
    echo "${DEST_DIR} not found. Run this on your Unraid server." >&2
    exit 1
fi

if [ -f "${SRC}" ]; then
    cp "${SRC}" "${DEST}"
else
    curl -fsSL "${SRC}" -o "${DEST}"
fi

if grep -q "YOUR_GITHUB_USERNAME" "${DEST}"; then
    echo "Note: the template still says YOUR_GITHUB_USERNAME. Edit the Repository"
    echo "field when you add the container (or fix the XML and run this again)."
fi
echo "Installed ${DEST}"
echo "Open Docker > Add Container and choose MegaDL from the Template list."
