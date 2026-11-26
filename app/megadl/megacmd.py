"""Thin wrapper around the MEGAcmd command line tools (mega-*).

MEGAcmd is MEGA's official command-line client. A background process
(mega-cmd-server) holds the session and runs the transfers; each mega-*
command is a short-lived client that talks to it over a unix socket in
$HOME/.megaCmd.

Output formats parsed here were taken from the MEGAcmd source
(src/megacmdexecuter.cpp, src/megacmdcommonutils.cpp).
"""

import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger("megadl.megacmd")

# Column separator for `mega-transfers --col-separator`. Paths are printed
# untruncated when a separator is used.
SEP = "|~|"
COLUMNS = "TYPE,TAG,SOURCEPATH,DESTINYPATH,PROGRESS,STATE"

# PROGRESS looks like "45.32% of   1.20 GB" (or "----% of 0.00  B").
_PROGRESS_RE = re.compile(
    r"(?P<pct>[0-9.]+|-+)%\s+of\s+(?P<size>[0-9.]+)\s*(?P<unit>[KMGT]?B)"
)
_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}

# "[err: 18:22:01] something" / "[API:err: ...]" prefixes on client messages.
_LOG_PREFIX_RE = re.compile(r"^\s*\[[^\]]{0,60}\]\s*")

ACTIVE_STATES = {"QUEUED", "ACTIVE", "PAUSED", "RETRYING", "COMPLETING"}
FINISHED_STATES = {"COMPLETED", "CANCELLED", "FAILED"}


@dataclass
class Transfer:
    tag: int
    path: str          # local destination path of the file
    source: str
    percent: float     # 0..100
    total: int         # bytes (approximate: MEGAcmd prints 2 decimals)
    done: int          # bytes (estimated from percent)
    state: str


class MegaCmdError(Exception):
    pass


def clean_message(*texts):
    """Turn MEGAcmd client output into a short human readable message."""
    lines = []
    for text in texts:
        for line in (text or "").splitlines():
            line = _LOG_PREFIX_RE.sub("", line).strip()
            if line and line not in lines:
                lines.append(line)
    return " ".join(lines[-3:])[:500]


def parse_size(size, unit):
    try:
        return int(float(size) * _UNITS.get(unit.upper(), 1))
    except ValueError:
        return 0


def parse_transfers(text):
    """Parse `mega-transfers --col-separator=SEP --output-cols=COLUMNS` output."""
    rows = []
    header = None
    for line in (text or "").splitlines():
        if SEP not in line:
            continue  # "DOWNLOADS ARE PAUSED", "Showing first N transfers", ...
        parts = line.split(SEP)
        if header is None:
            if "TAG" in parts and "STATE" in parts:
                header = [p.strip() for p in parts]
            continue
        if len(parts) != len(header):
            continue
        row = dict(zip(header, parts))
        try:
            tag = int(row.get("TAG", "").strip())
        except ValueError:
            continue
        path = row.get("DESTINYPATH", "")
        if not path or path.startswith("-----"):
            continue
        percent, total = 0.0, 0
        m = _PROGRESS_RE.search(row.get("PROGRESS", ""))
        if m:
            total = parse_size(m.group("size"), m.group("unit"))
            if not m.group("pct").startswith("-"):
                try:
                    percent = max(0.0, min(100.0, float(m.group("pct"))))
                except ValueError:
                    percent = 0.0
        state = row.get("STATE", "").strip().upper()
        done = total if state == "COMPLETED" else int(total * percent / 100.0)
        rows.append(Transfer(
            tag=tag,
            path=os.path.normpath(path),
            source=row.get("SOURCEPATH", "").strip(),
            percent=100.0 if state == "COMPLETED" else percent,
            total=total,
            done=done,
            state=state,
        ))
    return rows


class MegaCmd:
    def __init__(self, home, bin_dir=""):
        self.home = home
        self.bin_dir = bin_dir

    # ------------------------------------------------------------------ basics
    def _exe(self, name):
        exe = name if name.startswith("mega-") else "mega-" + name
        return os.path.join(self.bin_dir, exe) if self.bin_dir else exe

    def _env(self):
        env = os.environ.copy()
        env["HOME"] = self.home
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")
        return env

    def run(self, command, *args, timeout=30):
        """Run a mega-<command>. Returns (returncode, stdout, stderr)."""
        argv = [self._exe(command), *[str(a) for a in args]]
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                env=self._env(),
                cwd=self.home,
            )
        except FileNotFoundError:
            return 127, "", f"{argv[0]} not found. Is MEGAcmd installed?"
        except subprocess.TimeoutExpired:
            return 124, "", f"mega-{command} timed out after {timeout}s"
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        if proc.returncode != 0:
            # Never log the arguments: login passes the account password.
            log.debug("mega-%s exited %s: %s", command, proc.returncode,
                      clean_message(out, err))
        return proc.returncode, out, err

    # ------------------------------------------------------------------ server
    # MEGAcmd's own clients start mega-cmd-server in the background whenever
    # they can't reach it (see createSocket in megacmdshellcommunications.cpp),
    # so we never launch it ourselves: two launchers would fight over it.
    # We only find its process to notice restarts and to stop it on shutdown.
    def server_pid(self):
        """PID of the running mega-cmd-server owned by us, or None."""
        uid = os.getuid()
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                if os.stat(f"/proc/{name}").st_uid != uid:
                    continue
                with open(f"/proc/{name}/cmdline", "rb") as f:
                    argv0 = f.read().split(b"\0", 1)[0]
            except OSError:
                continue
            if os.path.basename(argv0) == b"mega-cmd-server":
                return int(name)
        return None

    def ping(self, timeout=30):
        """Talk to the server, which starts it if it isn't running."""
        rc, _out, _err = self.run("version", timeout=timeout)
        return rc == 0

    def wait_until_ready(self, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ping():
                return True
            time.sleep(2)
        return False

    def stop_server(self, timeout=10):
        pid = self.server_pid()
        if not pid:
            return
        log.info("Stopping mega-cmd-server")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.server_pid() == pid:
            time.sleep(0.25)
        if self.server_pid() == pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    # ----------------------------------------------------------------- account
    def whoami(self):
        """Returns the account e-mail, or None when not logged in."""
        rc, out, _err = self.run("whoami", timeout=60)
        if rc != 0:
            return None
        m = re.search(r"Account e-mail:\s*(\S+)", out)
        return m.group(1) if m else None

    def login(self, email, password, auth_code=None):
        args = []
        if auth_code:
            args.append(f"--auth-code={auth_code}")
        args += [email, password]
        rc, out, err = self.run("login", *args, timeout=300)
        message = clean_message(out, err)
        needs_code = "authentication app" in (out + err).lower()
        return rc == 0 and not needs_code, message, needs_code

    def logout(self):
        rc, out, err = self.run("logout", timeout=60)
        return rc == 0, clean_message(out, err)

    # --------------------------------------------------------------- transfers
    def get(self, link, dest_dir, password=None):
        """Queue a public link download. Returns (ok, message)."""
        dest = dest_dir if dest_dir.endswith("/") else dest_dir + "/"
        args = ["-q", "--ignore-quota-warn"]
        if password:
            args.append(f"--password={password}")
        args += [link, dest]
        rc, out, err = self.run("get", *args, timeout=180)
        return rc == 0, clean_message(out, err)

    def transfers(self, completed=False, limit=100000):
        args = ["--only-downloads", f"--limit={limit}",
                f"--col-separator={SEP}", f"--output-cols={COLUMNS}"]
        if completed:
            args[:0] = ["--show-completed", "--only-completed"]
        rc, out, err = self.run("transfers", *args, timeout=60)
        if rc != 0:
            raise MegaCmdError(clean_message(out, err) or f"mega-transfers failed ({rc})")
        return parse_transfers(out)

    def _tag_op(self, flag, tags):
        tags = [str(t) for t in tags]
        if not tags:
            return True, ""
        rc, out, err = self.run("transfers", flag, *tags, timeout=60)
        return rc == 0, clean_message(out, err)

    def pause(self, tags):
        return self._tag_op("-p", tags)

    def resume(self, tags):
        return self._tag_op("-r", tags)

    def cancel(self, tags):
        return self._tag_op("-c", tags)

    # ---------------------------------------------------------------- settings
    def set_download_limit(self, bytes_per_second):
        value = max(0, int(bytes_per_second))
        rc, out, err = self.run("speedlimit", "-d", f"{value}B", timeout=30)
        return rc == 0, clean_message(out, err)

    def get_download_limit(self):
        rc, out, _err = self.run("speedlimit", "-d", timeout=30)
        if rc != 0:
            return None
        if "unlimited" in out:
            return 0
        m = re.search(r"=\s*([0-9]+)", out)
        return int(m.group(1)) if m else None

    def set_download_connections(self, connections):
        rc, out, err = self.run("speedlimit", "--download-connections",
                                str(int(connections)), timeout=30)
        return rc == 0, clean_message(out, err)

    def set_permissions(self, files="666", folders="777"):
        ok1, msg1 = True, ""
        ok2, msg2 = True, ""
        if files:
            rc, out, err = self.run("permissions", "--files", "-s", files)
            ok1, msg1 = rc == 0, clean_message(out, err)
        if folders:
            rc, out, err = self.run("permissions", "--folders", "-s", folders)
            ok2, msg2 = rc == 0, clean_message(out, err)
        return ok1 and ok2, " ".join(m for m in (msg1, msg2) if m)
