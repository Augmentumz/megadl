"""Path helpers that keep every file operation inside DOWNLOAD_ROOT."""

import os
import shutil

from . import config

ROOT = config.DOWNLOAD_ROOT


class PathError(ValueError):
    pass


def inside(path, parent):
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def resolve(rel):
    """Map a GUI path (relative to the download root) to an absolute path."""
    rel = (rel or "").replace("\\", "/").strip().strip("/")
    path = os.path.realpath(os.path.join(ROOT, rel))
    if not inside(path, ROOT):
        raise PathError("That folder is outside the downloads root.")
    return path


def relative(path):
    path = os.path.realpath(path)
    if not inside(path, ROOT):
        raise PathError("That folder is outside the downloads root.")
    rel = os.path.relpath(path, ROOT)
    return "" if rel == "." else rel.replace(os.sep, "/")


def display(path):
    """Show a container path the way the user sees it on the host."""
    try:
        rel = relative(path)
    except PathError:
        return path
    label = config.HOST_PATH_LABEL
    if not rel:
        return label
    return (label.rstrip("/") + "/" + rel) if label != "/" else "/" + rel


def valid_folder_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or len(name.encode("utf-8")) > 255:
        return None
    if "/" in name or "\\" in name or "\0" in name or '"' in name:
        return None
    return name


def list_folders(rel, show_hidden=False):
    path = resolve(rel)
    if not os.path.isdir(path):
        raise PathError("That folder doesn't exist anymore.")
    folders = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        folders.append(entry.name)
                except OSError:
                    continue
    except PermissionError:
        raise PathError("No permission to read that folder.")
    folders.sort(key=str.casefold)
    here = relative(path)
    parent = None
    if path != ROOT:
        parent = relative(os.path.dirname(path))
    return {
        "path": here,
        "display": display(path),
        "parent": parent,
        "folders": [
            {"name": n, "path": (here + "/" + n) if here else n} for n in folders
        ],
        "writable": os.access(path, os.W_OK),
    }


def make_folder(rel, name):
    parent = resolve(rel)
    clean = valid_folder_name(name)
    if not clean:
        raise PathError("Folder names can't be empty or contain / \\ or \".")
    target = os.path.join(parent, clean)
    if os.path.exists(target):
        raise PathError("A folder with that name already exists.")
    os.mkdir(target)
    return relative(target)


def disk_usage(path):
    try:
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "free": usage.free}
    except OSError:
        return None


def safe_remove(path, dest_dir):
    """Delete a finished/partial download, refusing anything suspicious."""
    if not path:
        return False
    real = os.path.realpath(path)
    dest = os.path.realpath(dest_dir)
    if real in (ROOT, dest) or not inside(real, ROOT) or not inside(real, dest):
        raise PathError("Refusing to delete a path outside the download folder.")
    if os.path.isdir(real) and not os.path.islink(path):
        shutil.rmtree(real)
        return True
    if os.path.lexists(path):
        os.remove(path)
        return True
    return False
