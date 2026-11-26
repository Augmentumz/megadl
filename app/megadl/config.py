"""Runtime configuration, read once from environment variables."""

import os


def _env(name, default=""):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


# Folder inside the container that the user can browse and download into.
# On Unraid this is mapped to /mnt/user (all shares) by the template.
DOWNLOAD_ROOT = os.path.realpath(_env("DOWNLOAD_ROOT", "/data"))

# The host path that DOWNLOAD_ROOT is mapped to. Only used to show friendly
# paths in the GUI (for example /mnt/user/Downloads instead of /data/Downloads).
HOST_PATH_LABEL = _env("HOST_PATH_LABEL", "/mnt/user").rstrip("/") or "/"

# Persistent app data: job database, MEGAcmd session (~/.megaCmd), logs.
CONFIG_DIR = _env("CONFIG_DIR", "/config")

# Default download folder, relative to DOWNLOAD_ROOT. Can be changed in the GUI.
DEFAULT_DOWNLOAD_DIR = _env("DEFAULT_DOWNLOAD_DIR", "")

WEBUI_PORT = _env_int("WEBUI_PORT", 8080)
WEBUI_USER = _env("WEBUI_USER", "admin")
WEBUI_PASSWORD = _env("WEBUI_PASSWORD", "")

# Unix permissions MEGAcmd gives new files/folders. MEGAcmd's own default is
# 600/700, which hides downloads from SMB users on Unraid, so we default to
# the usual Unraid share permissions.
FILE_PERMISSIONS = _env("FILE_PERMISSIONS", "666")
FOLDER_PERMISSIONS = _env("FOLDER_PERMISSIONS", "777")

# Where the mega-* commands live. Empty means "use PATH".
MEGACMD_BIN_DIR = _env("MEGACMD_BIN_DIR", "")

POLL_INTERVAL = _env_float("POLL_INTERVAL", 1.5)

# After MEGAcmd (re)starts, how long to wait for it to resume cached transfers
# before re-queuing unfinished downloads ourselves.
RECOVERY_GRACE_SECONDS = _env_float("RECOVERY_GRACE_SECONDS", 60)

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
