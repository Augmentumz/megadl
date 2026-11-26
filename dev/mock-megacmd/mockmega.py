#!/usr/bin/env python3
"""A fake MEGAcmd for developing/testing MegaDL without a MEGA connection.

It imitates the output formats of the real mega-* commands (taken from the
MEGAcmd source) closely enough for MegaDL's parser. Transfers "download" by
growing sparse files. Special words in a link change behaviour:

  bad    -> mega-get fails ("Failed to get public node")
  fail   -> transfer fails at ~30%
  quota  -> transfer sits in RETRYING for 20 s first
  tiny   -> 1 KB file that finishes almost instantly
  /folder/ or #F! -> folder with three files in two levels

Usage: run as `mega-<command>` via the wrapper scripts in this folder.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HOME = os.environ.get("HOME", "/tmp")
STATE_DIR = os.path.join(HOME, ".mockmega")
STATE = os.path.join(STATE_DIR, "state.json")
LOCK = os.path.join(STATE_DIR, "lock")
SEP_DEFAULT = " "


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"email": None, "limit": 0, "next_tag": 1, "transfers": [], "completed": [],
                "server_pid": None}


def save(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


class Locked:
    def __enter__(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.f = open(LOCK, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.state = load()
        return self.state

    def __exit__(self, *exc):
        save(self.state)
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def size_to_text(n):
    unit, red = "B", float(n)
    if n > 1099511627776 * 2:
        red, unit = n / 1099511627776, "TB"
    elif n > 1073741824 * 2:
        red, unit = n / 1073741824, "GB"
    elif n > 1048576 * 2:
        red, unit = n / 1048576, "MB"
    elif n > 1024 * 2:
        red, unit = n / 1024, "KB"
    return f"{red:.2f} " + (" " if red == 0 else "") + unit


def err(msg, code=1):
    sys.stderr.write(f"[err: {time.strftime('%H:%M:%S')}] {msg}\n")
    sys.exit(code)


def text_to_size(text):
    t = text.upper()
    if t[-1:].isdigit():
        t += "B"
    units = {"B": 1, "K": 1024, "M": 1048576, "G": 1073741824, "T": 1 << 40}
    return sum(int(float(n) * units[u]) for n, u in re.findall(r"([0-9.]+)([BKMGT])", t))


# ---------------------------------------------------------------- commands
def cmd_server(_args):
    with Locked() as s:
        if s.get("server_pid"):
            try:
                os.kill(s["server_pid"], 0)
                print("Another instance of MEGAcmd Server is running.", file=sys.stderr)
                sys.exit(254)
            except OSError:
                pass
        s["server_pid"] = os.getpid()
        # transfers of a "previous run" are lost, like an anonymous session
        s["transfers"], s["completed"] = [], []
    print("[mock] MEGAcmd server started", flush=True)
    last = time.time()
    while True:
        time.sleep(0.3)
        now = time.time()
        dt, last = now - last, now
        with Locked() as s:
            tick(s, dt, now)


def tick(s, dt, now):
    active = [t for t in s["transfers"] if t["state"] in ("QUEUED", "ACTIVE", "RETRYING")]
    running = []
    for t in active:
        if t["state"] == "RETRYING" and now < t.get("retry_until", 0):
            continue
        if len(running) < 3:
            t["state"] = "ACTIVE"
            running.append(t)
        else:
            t["state"] = "QUEUED"
    per = 6 * 1048576
    if s["limit"]:
        per = min(per, s["limit"] / max(1, len(running)))
    for t in running:
        t["done"] = min(t["total"], t["done"] + per * dt)
        if t.get("fail") and t["done"] > 0.3 * t["total"]:
            t["state"] = "FAILED"
        elif t["done"] >= t["total"]:
            t["state"] = "COMPLETED"
            os.makedirs(os.path.dirname(t["path"]), exist_ok=True)
            with open(t["path"], "wb") as f:
                f.truncate(int(t["total"]))
    for t in [t for t in s["transfers"] if t["state"] in ("COMPLETED", "FAILED", "CANCELLED")]:
        s["transfers"].remove(t)
        s["completed"].insert(0, t)
    # folder records finish when all their children are done
    for f in s.get("folders", []):
        if not f.get("done") and not any(x.get("folder_tag") == f["tag"] for x in s["transfers"]):
            f["done"] = True
            s["completed"].insert(0, {"tag": f["tag"], "path": f["path"], "total": f["total"],
                                      "done": f["total"], "state": "COMPLETED"})


def cmd_version(_args):
    print("MEGAcmd version: 2.7.1.0: code 2070100 (mock)")


def cmd_whoami(_args):
    s = load()
    if not s.get("email"):
        err("Not logged in.", 57)
    print(f"Account e-mail: {s['email']}")


def cmd_login(args):
    code = None
    rest = []
    for a in args:
        if a.startswith("--auth-code="):
            code = a.split("=", 1)[1]
        elif a.startswith("--"):
            continue
        else:
            rest.append(a)
    if len(rest) != 2:
        err("Extra args required in non-interactive mode. Usage: login ...", 2)
    email, password = rest
    time.sleep(1.0)
    if password == "wrong":
        err("Login failed: invalid email or password", 9)
    if "2fa" in email and not code:
        print("Enter the code generated by your authentication app: ")
        err("Login failed: incorrect authentication", 9)
    if "2fa" in email and code != "123456":
        err("Login failed: incorrect authentication", 9)
    with Locked() as s:
        s["email"] = email
    print("Fetching nodes ...\nLogin complete as " + email)


def cmd_logout(_args):
    with Locked() as s:
        s["email"] = None
        s["limit"] = 0
    print("Logging out...")


def link_name(link, folder):
    h = hashlib.sha1(link.encode()).hexdigest()[:6]
    if folder:
        return f"Photo album {h}"
    words = ["Holiday video", "Linux ISO", "Project backup", "Podcast episode", "Album rip"]
    ext = [".mkv", ".iso", ".tar.gz", ".mp3", ".flac"]
    i = int(h, 16) % len(words)
    return f"{words[i]} {h}{ext[i]}"


def cmd_get(args):
    flags = [a for a in args if a.startswith("-")]
    words = [a for a in args if not a.startswith("-")]
    if not words:
        err("Usage: get ...", 2)
    link = words[0]
    dest = words[1] if len(words) > 1 else os.getcwd() + "/"
    if "bad" in link:
        err("Failed to get public node: Invalid argument", 9)
        return
    if not os.path.isdir(dest):
        err(f"{dest} is not a valid Download Folder", 9)
    folder = "/folder/" in link or "#F!" in link
    name = link_name(link, folder)
    seed = int(hashlib.sha1(link.encode()).hexdigest()[:6], 16)
    with Locked() as s:
        def add(path, total, folder_tag=None):
            t = {"tag": s["next_tag"], "path": path, "total": total, "done": 0.0,
                 "state": "QUEUED", "folder_tag": folder_tag}
            s["next_tag"] += 1
            if "fail" in link:
                t["fail"] = True
            if "quota" in link:
                t["state"] = "RETRYING"
                t["retry_until"] = time.time() + 20
            s["transfers"].append(t)
            return t
        if folder:
            root = os.path.join(dest, name)
            os.makedirs(os.path.join(root, "day 2"), exist_ok=True)
            ftag = s["next_tag"]
            s["next_tag"] += 1
            sizes = [8 + seed % 7, 12 + seed % 5, 20 + seed % 9]
            s.setdefault("folders", []).append(
                {"tag": ftag, "path": root, "total": sum(sizes) * 1048576})
            add(os.path.join(root, "IMG_0001.jpg"), sizes[0] * 1048576, ftag)
            add(os.path.join(root, "IMG_0002.jpg"), sizes[1] * 1048576, ftag)
            add(os.path.join(root, "day 2", "VID_0003.mp4"), sizes[2] * 1048576, ftag)
        else:
            total = 1024 if "tiny" in link else (30 + seed % 90) * 1048576
            add(os.path.join(dest, name), total)
    if "-q" not in flags:
        print("Download finished")


def cmd_transfers(args):
    opts = {}
    tags = []
    for a in args:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opts[k] = v
        elif a.startswith("-"):
            opts[a.lstrip("-")] = True
        else:
            tags.append(a)
    for op, new in (("p", "PAUSED"), ("r", "QUEUED"), ("c", "CANCELLED")):
        if opts.get(op):
            with Locked() as s:
                for tag in tags:
                    t = next((x for x in s["transfers"] if str(x["tag"]) == tag), None)
                    if not t:
                        sys.stderr.write(f"[err: {time.strftime('%H:%M:%S')}] Could not find transfer with tag: {tag}\n")
                        continue
                    if op == "r" and t["state"] != "PAUSED":
                        continue
                    t["state"] = new
                    word = {"p": "paused", "r": "resumed", "c": "cancelled"}[op]
                    print(f"Transfer {tag} {word} successfully.")
                if op == "c":
                    tick(s, 0, time.time())
            return
    s = load()
    sep = opts.get("col-separator", "  ")
    limit = int(opts.get("limit", 10))
    rows = []
    if opts.get("show-completed"):
        rows += s["completed"][:limit + 1]
    if not opts.get("only-completed"):
        rows += [t for t in s["transfers"]][: max(0, limit + 1 - len(rows))]
    cols = opts.get("output-cols", "TYPE,TAG,SOURCEPATH,DESTINYPATH,PROGRESS,STATE").split(",")
    if rows:
        print(sep.join(cols))
    for t in rows[:limit]:
        pct = 100.0 * t["done"] / t["total"] if t["total"] else 0
        values = {"TYPE": "⇓", "TAG": str(t["tag"]), "SOURCEPATH": "",
                  "DESTINYPATH": t["path"],
                  "PROGRESS": f"{pct:.2f}% of {size_to_text(int(t['total'])):>10}",
                  "STATE": t["state"]}
        print(sep.join(values[c] for c in cols))
    if len(rows) > limit:
        print(f" ...  Showing first {limit} transfers ...")


def cmd_speedlimit(args):
    words = [a for a in args if not a.startswith("-")]
    if "--download-connections" in args:
        if words:
            print(f"Download max connections = {words[0]}")
        return
    if words:
        with Locked() as s:
            s["limit"] = text_to_size(words[0])
    s = load()
    lim = s["limit"]
    print("Download speed limit = " + (f"{lim} B/s" if lim else "unlimited"))


def cmd_permissions(args):
    words = [a for a in args if not a.startswith("-")]
    kind = "files" if "--files" in args else "folders"
    print(f"Default {kind} permissions: {words[0] if words else '600'}")


COMMANDS = {
    "cmd-server": cmd_server, "version": cmd_version, "whoami": cmd_whoami,
    "login": cmd_login, "logout": cmd_logout, "get": cmd_get,
    "transfers": cmd_transfers, "speedlimit": cmd_speedlimit,
    "permissions": cmd_permissions,
}

def server_alive():
    pid = load().get("server_pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_server():
    """Like real MEGAcmd clients: start the server if it isn't running."""
    if server_alive():
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(STATE_DIR, "server.out"), "ab") as out:
        subprocess.Popen([os.path.join(here, "mega-cmd-server")], stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(30):
        time.sleep(0.1)
        if server_alive():
            return


if __name__ == "__main__":
    name = sys.argv[1]
    if name != "cmd-server":
        ensure_server()
    if name not in COMMANDS:
        err(f"mock: command not implemented: {name}", 2)
    COMMANDS[name](sys.argv[2:])
