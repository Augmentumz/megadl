"""Download manager: supervises MEGAcmd, queues links and tracks progress.

How a download is tracked
-------------------------
`mega-get -q LINK DEST/` queues a download inside MEGAcmd and returns right
away. MEGAcmd identifies each *file* transfer by a numeric tag, and
`mega-transfers` lists those tags with their local destination path.

Links are submitted one at a time. Before submitting, we remember the highest
tag seen so far (the job's "tag floor"). The first new transfer whose path is
inside the job's destination folder tells us the job's root: DEST/<name> for
a file link, DEST/<folder>/... for a folder link. From then on every transfer
under that root belongs to the job, which lets us pause, resume and cancel a
whole folder download as one item.
"""

import json
import logging
import os
import queue
import re
import threading
import time

from . import config, paths
from .megacmd import ACTIVE_STATES, FINISHED_STATES, MegaCmdError

log = logging.getLogger("megadl.manager")

# Job statuses
QUEUED = "queued"            # waiting for us to hand it to MEGAcmd
STARTING = "starting"        # handed over, waiting for the transfer to appear
PENDING = "pending"          # in MEGAcmd's queue
DOWNLOADING = "downloading"
WAITING = "waiting"          # MEGAcmd is retrying (transfer quota, network)
PAUSED = "paused"
COMPLETED = "completed"
FAILED = "failed"

IN_MEGA = {STARTING, PENDING, DOWNLOADING, WAITING, PAUSED}
UNFINISHED = IN_MEGA | {QUEUED}

LINK_RE = re.compile(r"https?://(?:www\.)?mega(?:\.co)?\.nz/[^\s<>\"']+", re.I)

GHOST_TTL = 600          # keep cancelling transfers of deleted jobs this long
ACTION_COOLDOWN = 5      # don't repeat pause/resume for a tag more often
FOLDER_SETTLE = 10       # seconds with nothing left before a folder is "done"

DEFAULT_SETTINGS = {
    "speed_limit": 0,            # bytes per second, 0 = unlimited
    "download_connections": 0,   # 0 = MEGAcmd default
    "default_dest": config.DEFAULT_DOWNLOAD_DIR,
}


def extract_links(text):
    seen, links = set(), []
    for match in LINK_RE.finditer(text or ""):
        link = match.group(0).rstrip(".,;)]}")
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links


def is_folder_link(link):
    return "/folder/" in link or "#F!" in link


def rel_path(job, path):
    """Path of a file inside a folder download, e.g. "Season 1/ep1.mkv"."""
    root = job.get("root_path")
    if not root or not path.startswith(root + os.sep):
        return None
    return os.path.relpath(path, root).replace(os.sep, "/")


def unticked(job):
    """Files of a folder download the user doesn't want (held, then dropped)."""
    try:
        return set(json.loads(job.get("skipped") or "[]"))
    except ValueError:
        return set()


class JobRuntime:
    """In-memory state for a job that only lives while MEGAcmd is running."""

    def __init__(self):
        self.tags = {}             # tag -> Transfer
        self.tag_floor = 0
        self.snapshot = None       # names in dest before submitting
        self.speed = 0.0
        self.last_done = None
        self.last_t = None
        self.finished_since = None
        self.persist_t = 0.0
        self.recover = False       # candidate for re-queue after a restart
        self.owned_name = None     # name we created before a retry
        self.actions = {}          # tag -> (op, monotonic time)


class Manager:
    def __init__(self, mega, store):
        self.mega = mega
        self.store = store
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.submit_q = queue.Queue()

        self.jobs = {j["id"]: j for j in store.list_jobs()}
        self.rt = {}
        self.ghosts = {}           # deleted jobs: id -> (job, runtime, expires)
        self.settings = store.get_settings(DEFAULT_SETTINGS)

        self.server_state = "starting"
        self.server_message = "Starting MEGAcmd..."
        self.server_up_since = None
        self.account_email = None
        self.account_checked = False
        self.busy_account = False
        self.max_tag = 0
        self.poll_error = None
        self._server_pid = None
        self._needs_setup = True
        self._recovery_done = False
        self._ticks = 0
        self._threads = []

        for job in self.jobs.values():
            if job["status"] in IN_MEGA:
                rt = self._runtime(job["id"])
                rt.recover = True
        for job in sorted(self.jobs.values(), key=lambda j: j["id"]):
            if job["status"] == QUEUED:
                self.submit_q.put(job["id"])

    # ================================================================ public
    def start(self):
        for target, name in ((self._poll_loop, "poller"), (self._submit_loop, "submitter")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self):
        self.stop_event.set()
        for t in self._threads:
            t.join(timeout=5)
        self.mega.stop_server()

    def add_links(self, text, dest_rel, password=None, start_paused=False):
        links = extract_links(text)
        if not links:
            raise ValueError("Paste at least one mega.nz link.")
        dest = paths.resolve(dest_rel if dest_rel is not None else self.settings["default_dest"])
        if '"' in dest:
            raise ValueError('Folder paths with a " character are not supported by MEGAcmd.')
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"Couldn't create the folder: {exc.strerror}")
        if not os.access(dest, os.W_OK):
            raise ValueError("The app can't write to that folder. Check the share permissions.")
        ids, skipped = [], 0
        with self.lock:
            busy = {(j["link"], j["dest_dir"]) for j in self.jobs.values()
                    if j["status"] in UNFINISHED}
            for link in links:
                if (link, dest) in busy:
                    skipped += 1  # already downloading to the same folder
                    continue
                job_id = self.store.add_job(link, dest, password or None,
                                            desired="pause" if start_paused else "run")
                self.jobs[job_id] = self.store.get_job(job_id)
                ids.append(job_id)
        for job_id in ids:
            self.submit_q.put(job_id)
        return ids, skipped

    def files(self, job_id):
        """Files of a folder download, for the expandable file list."""
        with self.lock:
            job = self._job(job_id)
            rt = self.rt.get(job_id)
            skipped = unticked(job)
            latest = {}
            for tag, t in (rt.tags.items() if rt else ()):
                rel = rel_path(job, t.path)
                if rel is not None and (rel not in latest or tag > latest[rel][0]):
                    latest[rel] = (tag, t)
            files = [{
                "path": rel,
                "size": t.total,
                "done": t.done,
                "percent": t.percent,
                "state": t.state,
                "selected": rel not in skipped or t.state == "COMPLETED",
            } for rel, (_tag, t) in latest.items()]
            status = job["status"]
        files.sort(key=lambda f: f["path"].casefold())
        return {
            "files": files,
            "editable": status in UNFINISHED,
            "reading": status in (QUEUED, STARTING) or (status in IN_MEGA and not files),
        }

    def select_files(self, job_id, rels, selected):
        """Tick (download) or untick (hold, then drop) files of a folder download."""
        if not isinstance(rels, list) or len(rels) > 100000:
            raise ValueError("Send a list of file paths.")
        rels = {r for r in rels if isinstance(r, str) and r and not r.startswith("/")
                and ".." not in r.split("/")}
        with self.lock:
            job = self._job(job_id)
            if job.get("kind") != "folder":
                raise ValueError("Only folder downloads have files to choose from.")
            if job["status"] not in UNFINISHED:
                raise ValueError("This download has finished, so its files can't be changed.")
            rt = self.rt.get(job_id)
            if not selected and rt:
                # A file that already finished downloading can't be unticked.
                done = {rel_path(job, t.path) for t in rt.tags.values() if t.state == "COMPLETED"}
                rels -= done
            skipped = unticked(job)
            skipped = (skipped - rels) if selected else (skipped | rels)
            self._save(job_id, skipped=json.dumps(sorted(skipped)))
            pause, resume = [], []
            for tag, t in (rt.tags.items() if rt else ()):
                if rel_path(job, t.path) not in rels:
                    continue
                if not selected and t.state in ("QUEUED", "ACTIVE", "RETRYING"):
                    pause.append(tag)
                elif selected and t.state == "PAUSED" and job["desired"] == "run":
                    resume.append(tag)
            if rt:
                rt.finished_since = None
        if pause:
            self.mega.pause(pause)
        if resume:
            self.mega.resume(resume)

    def pause(self, job_id):
        with self.lock:
            job = self._job(job_id)
            if job["status"] in (COMPLETED, FAILED):
                return
            self._save(job_id, desired="pause")
            tags = self._tags_in(job_id, {"QUEUED", "ACTIVE", "RETRYING"})
        if tags:
            self.mega.pause(tags)

    def resume(self, job_id):
        with self.lock:
            job = self._job(job_id)
            if job["status"] == FAILED:
                self._requeue(job_id)
                return
            if job["status"] == COMPLETED:
                return
            self._save(job_id, desired="run")
            skipped = unticked(job)
            # Unticked files stay paused.
            tags = [tag for tag in self._tags_in(job_id, {"PAUSED"})
                    if rel_path(job, self.rt[job_id].tags[tag].path) not in skipped]
        if tags:
            self.mega.resume(tags)

    def retry(self, job_id):
        with self.lock:
            job = self._job(job_id)
            if job["status"] != FAILED:
                raise ValueError("Only failed downloads can be retried.")
            self._requeue(job_id)

    def delete(self, job_id, delete_files=False):
        with self.lock:
            job = self._job(job_id)
            rt = self.rt.pop(job_id, JobRuntime())
            self.jobs.pop(job_id, None)
            tags = [tag for tag, t in rt.tags.items() if t.state in ACTIVE_STATES]
            if job["status"] in IN_MEGA or tags:
                now = time.monotonic()
                self.ghosts[job_id] = (job, rt, now + GHOST_TTL, now)
        self.store.delete_job(job_id)
        if tags:
            self.mega.cancel(tags)
        if delete_files and job.get("root_path") and not job.get("preexisting"):
            threading.Thread(
                target=self._delete_files_later, args=(job, bool(tags)), daemon=True
            ).start()

    def pause_all(self):
        for job_id in list(self.jobs):
            if self.jobs.get(job_id, {}).get("status") in UNFINISHED:
                self.pause(job_id)

    def resume_all(self):
        for job_id in list(self.jobs):
            job = self.jobs.get(job_id)
            if job and job["status"] in UNFINISHED and job["desired"] == "pause":
                self.resume(job_id)

    def clear_finished(self):
        with self.lock:
            done = [j for j, job in self.jobs.items() if job["status"] == COMPLETED]
        for job_id in done:
            self.delete(job_id, delete_files=False)
        return len(done)

    def update_settings(self, **values):
        clean = {}
        if "speed_limit" in values:
            clean["speed_limit"] = max(0, int(values["speed_limit"] or 0))
        if "download_connections" in values:
            n = int(values["download_connections"] or 0)
            if n < 0 or n > 16:
                raise ValueError("Connections per download must be between 1 and 16.")
            clean["download_connections"] = n
        if "default_dest" in values:
            dest = paths.resolve(values["default_dest"] or "")
            rel = paths.relative(dest)
            if rel != self.settings.get("default_dest"):
                # Only a new choice needs checking; the default folder is
                # created on demand, so it may not exist yet.
                if not os.path.isdir(dest):
                    try:
                        os.makedirs(dest, exist_ok=True)
                    except OSError as exc:
                        raise ValueError(f"Couldn't create that folder: {exc.strerror}")
                clean["default_dest"] = rel
        self.store.set_settings(**clean)
        with self.lock:
            self.settings.update(clean)
        if self.server_state == "up":
            if "speed_limit" in clean:
                ok, msg = self.mega.set_download_limit(clean["speed_limit"])
                if not ok:
                    raise ValueError(msg or "MEGAcmd rejected the speed limit.")
            if clean.get("download_connections"):
                ok, msg = self.mega.set_download_connections(clean["download_connections"])
                if not ok:
                    raise ValueError(msg or "MEGAcmd rejected the connection count.")
        return dict(self.settings)

    def login(self, email, password, auth_code=None):
        if self.server_state != "up":
            raise ValueError("MEGAcmd isn't running yet. Try again in a moment.")
        self.busy_account = True
        try:
            ok, message, needs_code = self.mega.login(email.strip(), password, auth_code or None)
            self._refresh_account()
            self._apply_settings()
            return {"ok": ok and bool(self.account_email), "message": message,
                    "needs_code": needs_code, "email": self.account_email}
        finally:
            self.busy_account = False

    def logout(self):
        self.busy_account = True
        try:
            ok, message = self.mega.logout()
            self._refresh_account()
            self._apply_settings()
            return {"ok": ok, "message": message}
        finally:
            self.busy_account = False

    def snapshot(self):
        with self.lock:
            views = [self._view(job) for job in self.jobs.values()]
            settings = dict(self.settings)
        views.sort(key=lambda v: v["id"], reverse=True)
        try:
            dest = paths.resolve(settings["default_dest"])
        except paths.PathError:
            dest = paths.ROOT
        return {
            "server": {"state": self.server_state, "message": self.server_message},
            "account": {"email": self.account_email, "checked": self.account_checked,
                        "busy": self.busy_account},
            "settings": settings,
            "default_dest_display": paths.display(dest),
            "disk": paths.disk_usage(dest),
            "speed": sum(v["speed"] for v in views),
            "jobs": views,
            "poll_error": self.poll_error,
        }

    # ============================================================ internals
    def _job(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def _runtime(self, job_id):
        rt = self.rt.get(job_id)
        if rt is None:
            rt = self.rt[job_id] = JobRuntime()
        return rt

    def _save(self, job_id, **fields):
        job = self.jobs.get(job_id)
        if job is None:
            return
        changed = {k: v for k, v in fields.items() if job.get(k) != v}
        if changed:
            job.update(changed)
            self.store.update_job(job_id, **changed)

    def _tags_in(self, job_id, states):
        rt = self.rt.get(job_id)
        if not rt:
            return []
        return [tag for tag, t in rt.tags.items() if t.state in states]

    def _requeue(self, job_id):
        job = self._job(job_id)
        self.ghosts.pop(job_id, None)  # a timed-out attempt must not cancel the retry
        rt = self._runtime(job_id)
        rt.tags.clear()
        rt.speed = 0.0
        rt.last_done = None
        rt.finished_since = None
        rt.recover = False
        # The retry may land in "<name> (1)", so detect the root again, but
        # remember that a folder/file we created earlier is still ours.
        if job.get("root_path") and not job.get("preexisting"):
            rt.owned_name = os.path.basename(job["root_path"])
        self._save(job_id, status=QUEUED, desired="run", error=None, root_path=None,
                   submitted_at=None, completed_at=None, done_bytes=0)
        self.submit_q.put(job_id)

    def _delete_files_later(self, job, wait):
        if wait:
            time.sleep(3)  # let MEGAcmd finish cancelling and remove temp files
        try:
            paths.safe_remove(job["root_path"], job["dest_dir"])
            log.info("Deleted %s", job["root_path"])
        except (OSError, paths.PathError) as exc:
            log.warning("Could not delete %s: %s", job["root_path"], exc)

    def _view(self, job):
        rt = self.rt.get(job["id"])
        status = job["status"]
        if job["desired"] == "pause" and status in UNFINISHED:
            status = PAUSED
        total, done = job["total_bytes"] or 0, job["done_bytes"] or 0
        speed = rt.speed if rt and status == DOWNLOADING else 0.0
        eta = (total - done) / speed if speed > 1024 and total > done else None
        name = job["name"] or _name_from_link(job["link"])
        root = job.get("root_path")
        return {
            "id": job["id"],
            "name": name,
            "named": bool(job["name"]),
            "link": job["link"],
            "kind": job["kind"] or ("folder" if is_folder_link(job["link"]) else "file"),
            "dest": paths.display(job["dest_dir"]),
            "status": status,
            "desired": job["desired"],
            "total": total,
            "done": done,
            "percent": (100.0 * done / total) if total else (100.0 if status == COMPLETED else 0.0),
            "speed": speed,
            "eta": eta,
            "files_total": job["files_total"],
            "files_done": job["files_done"],
            "files_skipped": job.get("files_skipped") or 0,
            "error": job["error"],
            "can_delete_files": bool(root and not job["preexisting"] and os.path.lexists(root)),
            "created_at": job["created_at"],
            "completed_at": job["completed_at"],
        }

    # --------------------------------------------------------------- server
    def _ensure_server(self):
        """Make sure MEGAcmd answers; notice when its server (re)started.

        We don't launch mega-cmd-server ourselves: any mega-* command starts
        it when it isn't running, which is how MEGAcmd is meant to be used.
        """
        pid = self.mega.server_pid()
        if self.server_state == "up":
            if pid is not None and pid == self._server_pid:
                return True
            if pid is None and self._server_pid is None and self.mega.ping(timeout=15):
                return True  # can't see the process here, but it answers
            log.warning("MEGAcmd server stopped; starting it again")
        self.server_state = "starting"
        self.server_message = "Starting MEGAcmd..."
        if not self.mega.wait_until_ready(timeout=120):
            self.server_state = "down"
            self.server_message = ("MEGAcmd didn't start. Check "
                                   "/config/.megaCmd/megacmdserver.log")
            return False
        for _ in range(6):  # the process can take a moment to show up
            self._server_pid = self.mega.server_pid()
            if self._server_pid:
                break
            time.sleep(0.5)
        log.info("MEGAcmd server is running (pid %s)", self._server_pid)
        self._on_server_started()
        return True

    def _on_server_started(self):
        with self.lock:
            self.server_state = "up"
            self.server_message = ""
            self.server_up_since = time.monotonic()
            self.max_tag = 0
            self._needs_setup = True
            self._recovery_done = False
            # Tags are only valid for one server run.
            for job_id, job in self.jobs.items():
                rt = self._runtime(job_id)
                rt.tags.clear()
                rt.tag_floor = 0
                rt.actions.clear()
                if job["status"] in IN_MEGA:
                    rt.recover = True
            self.ghosts.clear()

    def _apply_settings(self):
        ok, msg = self.mega.set_permissions(config.FILE_PERMISSIONS, config.FOLDER_PERMISSIONS)
        if not ok:
            log.warning("Could not set default permissions: %s", msg)
        ok, msg = self.mega.set_download_limit(self.settings.get("speed_limit", 0))
        if not ok:
            log.warning("Could not set speed limit: %s", msg)
        if self.settings.get("download_connections"):
            ok, msg = self.mega.set_download_connections(self.settings["download_connections"])
            if not ok:
                log.warning("Could not set connections: %s", msg)

    def _refresh_account(self):
        self.account_email = self.mega.whoami()
        self.account_checked = True

    # ---------------------------------------------------------------- loops
    def _poll_loop(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self._tick()
            except Exception:  # keep the loop alive whatever happens
                log.exception("Poll failed")
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.2, config.POLL_INTERVAL - elapsed))

    def _tick(self):
        if not self._ensure_server():
            self.stop_event.wait(10)
            return
        self._ticks += 1
        if self._needs_setup:
            self._needs_setup = False
            self._apply_settings()
            self._refresh_account()
        elif self._ticks % 20 == 0 and not self.busy_account:
            self._refresh_account()

        try:
            active = self.mega.transfers()
            completed = self.mega.transfers(completed=True, limit=2000) \
                if self._need_completed(active) else []
            self.poll_error = None
        except MegaCmdError as exc:
            self.poll_error = str(exc)
            return
        actions = self._process(active, completed)
        for op, tags in actions.items():
            if tags:
                getattr(self.mega, op)(sorted(tags))
        self._maybe_recover()

    def _need_completed(self, active):
        """Do we need MEGAcmd's list of finished transfers this round?"""
        present = {t.tag for t in active}
        with self.lock:
            for job, rt, _ghost in self._entries():
                if job["status"] == STARTING and not rt.tags:
                    return True  # tiny files can finish between two polls
                for tag, t in rt.tags.items():
                    if t.state in ACTIVE_STATES and tag not in present:
                        return True
        return False

    def _entries(self):
        """(job, runtime, is_ghost) for everything that can own transfers."""
        now = time.monotonic()
        for job_id in [i for i, g in self.ghosts.items() if g[2] < now]:
            self.ghosts.pop(job_id, None)
        out = [(job, self._runtime(job["id"]), False) for job in self.jobs.values()]
        out += [(g[0], g[1], True) for g in self.ghosts.values()]
        return out

    def _match(self, t, entries):
        """Find the job a transfer we haven't seen before belongs to.

        Order matters:
        1. live jobs whose root folder/file is already known,
        2. jobs (live or just deleted) still waiting for their first
           transfer, oldest submission first,
        3. recently deleted *folder* jobs, whose remaining files may still be
           getting queued by MEGAcmd (they will be cancelled).
        """
        def under(root):
            return t.path == root or t.path.startswith(root + os.sep)

        for job, rt, ghost in entries:
            root = job.get("root_path")
            if not ghost and root and job["status"] in IN_MEGA and \
                    t.tag > rt.tag_floor and under(root):
                return job, rt
        starting = [(job, rt, ghost) for job, rt, ghost in entries
                    if job["status"] == STARTING and not job.get("root_path")
                    and t.tag > rt.tag_floor]
        starting.sort(key=lambda e: e[0].get("submitted_at") or 0)
        found = self._match_starting(t, starting)
        if found:
            return found
        now = time.monotonic()
        for job, rt, ghost in entries:
            root = job.get("root_path")
            if ghost and root and job.get("kind") == "folder" and t.tag > rt.tag_floor \
                    and under(root) and now - self.ghosts[job["id"]][3] < 120:
                return job, rt
        return None

    def _match_starting(self, t, starting):
        for job, rt, ghost in starting:
            dest = job["dest_dir"].rstrip(os.sep)
            if not t.path.startswith(dest + os.sep):
                continue
            rel = os.path.relpath(t.path, dest)
            first = rel.split(os.sep)[0]
            root = os.path.join(dest, first)
            kind = "folder" if os.sep in rel else "file"
            preexisting = 1 if (rt.snapshot is not None and first in rt.snapshot
                                and first != rt.owned_name) else 0
            job.update(root_path=root, name=first, kind=kind, preexisting=preexisting)
            if not ghost:
                self.store.update_job(job["id"], root_path=root, name=first,
                                      kind=kind, preexisting=preexisting)
            return job, rt
        return None

    def _process(self, active, completed):
        actions = {"pause": set(), "resume": set(), "cancel": set()}
        now = time.monotonic()
        with self.lock:
            entries = self._entries()
            owner = {}
            for job, rt, ghost in entries:
                for tag in rt.tags:
                    owner[tag] = (job, rt)
            present = {t.tag for t in active}
            for t in active + completed:
                self.max_tag = max(self.max_tag, t.tag)

            # Deeper paths first, so a folder download is recognised as such.
            def attach(rows, allow_new):
                for t in sorted(rows, key=lambda r: -r.path.count(os.sep)):
                    found = owner.get(t.tag)
                    if found is None and allow_new:
                        found = self._match(t, entries)
                        if found:
                            owner[t.tag] = found
                    if found is None:
                        continue
                    job, rt = found
                    prev = rt.tags.get(t.tag)
                    if prev is not None and prev.state in FINISHED_STATES:
                        continue
                    rt.tags[t.tag] = t
                    if job.get("kind") == "file" and job.get("root_path") and \
                            t.path.startswith(job["root_path"] + os.sep):
                        job["kind"] = "folder"
                        if self.jobs.get(job["id"]) is job:
                            self.store.update_job(job["id"], kind="folder")

            attach(active, True)
            attach([t for t in completed if t.tag not in present], True)

            # Transfers that vanished without showing up as completed.
            for job, rt, ghost in entries:
                for tag, t in list(rt.tags.items()):
                    if t.state in ACTIVE_STATES and tag not in present:
                        exists = os.path.exists(t.path)
                        t.state = "COMPLETED" if exists else "FAILED"
                        if exists:
                            t.done, t.percent = t.total, 100.0

            for job, rt, ghost in entries:
                if ghost:
                    actions["cancel"].update(
                        tag for tag, t in rt.tags.items() if t.state in ACTIVE_STATES)
                    continue
                self._enforce(job, rt, actions, now)
                self._aggregate(job, rt, now, actions)
        return actions

    def _enforce(self, job, rt, actions, now):
        if job["status"] not in IN_MEGA:
            return
        skipped = unticked(job)
        folder = job.get("kind") == "folder"
        for tag, t in rt.tags.items():
            if folder and t.path == job.get("root_path"):
                continue  # the folder's own record, not a file
            held = rel_path(job, t.path) in skipped
            if t.state in ("QUEUED", "ACTIVE", "RETRYING") and (job["desired"] == "pause" or held):
                op = "pause"
            elif t.state == "PAUSED" and job["desired"] == "run" and not held:
                op = "resume"
            else:
                continue
            last = rt.actions.get(tag)
            if last and last[0] == op and now - last[1] < ACTION_COOLDOWN:
                continue
            rt.actions[tag] = (op, now)
            actions[op].add(tag)

    def _aggregate(self, job, rt, now, actions):
        if not rt.tags or job["status"] not in IN_MEGA:
            return
        root = job.get("root_path")
        rows = list(rt.tags.values())
        if job.get("kind") == "folder":
            root_rows = [r for r in rows if r.path == root]
            file_rows = [r for r in rows if r.path != root]
        else:
            root_rows, file_rows = [], rows
        if not file_rows:
            return
        skipped = unticked(job)
        def is_held(r):
            return r.state != "COMPLETED" and rel_path(job, r.path) in skipped
        held = [r for r in file_rows if is_held(r)]
        file_rows = [r for r in file_rows if not is_held(r)]
        total = sum(r.total for r in file_rows)
        done = sum(r.done for r in file_rows)
        files_done = sum(1 for r in file_rows if r.state == "COMPLETED")
        unfinished = [r for r in file_rows if r.state in ACTIVE_STATES]
        failed = [r for r in file_rows if r.state in ("FAILED", "CANCELLED")]
        fields = {"total_bytes": total, "done_bytes": done,
                  "files_total": len(file_rows), "files_done": files_done,
                  "files_skipped": len(held)}

        if not file_rows:
            # Every file is unticked: wait for the user rather than finishing.
            rt.finished_since = None
            status = PAUSED
        elif unfinished:
            rt.finished_since = None
            states = {r.state for r in unfinished}
            if job["desired"] == "pause":
                status = PAUSED
            elif states & {"ACTIVE", "COMPLETING"}:
                status = DOWNLOADING
            elif "RETRYING" in states:
                status = WAITING
            elif states == {"PAUSED"}:
                status = PAUSED
            else:
                status = PENDING
        else:
            if rt.finished_since is None:
                rt.finished_since = now
            settled = (job.get("kind") != "folder"
                       or any(r.state in FINISHED_STATES for r in root_rows)
                       or now - rt.finished_since >= FOLDER_SETTLE)
            if not settled:
                status = DOWNLOADING
            elif failed:
                status = FAILED
                if len(file_rows) == 1:
                    fields["error"] = ("Cancelled in MEGAcmd" if failed[0].state == "CANCELLED"
                                       else "MEGA reported the download as failed. Use Retry.")
                else:
                    fields["error"] = f"{len(failed)} of {len(file_rows)} files failed. Use Retry."
            else:
                status = COMPLETED
                fields["done_bytes"] = total
                fields["completed_at"] = time.time()
                fields["error"] = None
            if status in (COMPLETED, FAILED):
                # Unticked files were only held; drop them now.
                actions["cancel"].update(
                    tag for tag, t in rt.tags.items()
                    if t.state in ACTIVE_STATES and rel_path(job, t.path) in skipped)

        # speed (smoothed)
        if status == DOWNLOADING and rt.last_done is not None and now > rt.last_t:
            inst = max(0, done - rt.last_done) / (now - rt.last_t)
            rt.speed = inst if rt.speed == 0 else 0.6 * rt.speed + 0.4 * inst
        elif status != DOWNLOADING:
            rt.speed = 0.0
        rt.last_done, rt.last_t = done, now

        fields["status"] = status
        important = status != job["status"] or fields.get("error") != job.get("error")
        if important or now - rt.persist_t >= 5:
            rt.persist_t = now
            self._save(job["id"], **fields)
        else:
            job.update(fields)  # keep memory fresh, write to disk later

    def _pause_new_files(self, job_id, dest, seconds=8):
        """For a job added paused, pause its files as soon as MEGAcmd lists them.

        The poller would also do it, but only every POLL_INTERVAL seconds,
        which lets the first files download for a moment. Submissions are one
        at a time, so new transfers in this folder belong to this job.
        """
        with self.lock:
            job = self.jobs.get(job_id)
            rt = self.rt.get(job_id)
            if not job or not rt or job["desired"] != "pause":
                return
            floor = rt.tag_floor
        prefix = dest.rstrip(os.sep) + os.sep
        paused = set()
        last_new = time.monotonic()
        deadline = last_new + seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                rows = self.mega.transfers()
            except MegaCmdError:
                rows = []
            new = [t.tag for t in rows if t.tag > floor and t.tag not in paused
                   and t.path.startswith(prefix) and t.state in ("QUEUED", "ACTIVE", "RETRYING")]
            if new:
                self.mega.pause(new)
                paused.update(new)
                last_new = time.monotonic()
            elif paused and time.monotonic() - last_new > 1.5:
                return  # MEGAcmd has finished listing the files
            with self.lock:
                job = self.jobs.get(job_id)
                if not job or job["desired"] != "pause":
                    return
            self.stop_event.wait(0.25)

    def _maybe_recover(self):
        """After a restart, re-queue downloads MEGAcmd didn't resume itself."""
        if self._recovery_done or self.server_up_since is None:
            return
        if time.monotonic() - self.server_up_since < config.RECOVERY_GRACE_SECONDS:
            return
        self._recovery_done = True
        with self.lock:
            for job_id, job in list(self.jobs.items()):
                rt = self.rt.get(job_id)
                if rt and rt.recover and not rt.tags and job["status"] in IN_MEGA:
                    log.info("Re-queuing interrupted download %s", job_id)
                    desired = job["desired"]
                    self._requeue(job_id)
                    self._save(job_id, desired=desired)
                if rt:
                    rt.recover = False

    def _submit_loop(self):
        while not self.stop_event.is_set():
            try:
                job_id = self.submit_q.get(timeout=1)
            except queue.Empty:
                continue
            while not self.stop_event.is_set() and self.server_state != "up":
                self.stop_event.wait(1)
            try:
                self._submit(job_id)
            except Exception:
                log.exception("Submitting job %s failed", job_id)
                with self.lock:
                    self._save(job_id, status=FAILED, error="Internal error, see container log.")

    def _submit(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["status"] != QUEUED:
                return
            link, dest, password = job["link"], job["dest_dir"], job["link_password"]
        try:
            os.makedirs(dest, exist_ok=True)
            snapshot = set(os.listdir(dest))
        except OSError as exc:
            with self.lock:
                self._save(job_id, status=FAILED, error=f"Can't use the folder: {exc.strerror}")
            return
        try:  # learn the highest existing tag so we only match newer ones
            rows = self.mega.transfers() + self.mega.transfers(completed=True, limit=20)
        except MegaCmdError:
            rows = []
        with self.lock:
            if job_id not in self.jobs:
                return
            for t in rows:
                self.max_tag = max(self.max_tag, t.tag)
            rt = self._runtime(job_id)
            rt.tags.clear()
            rt.tag_floor = self.max_tag
            rt.snapshot = snapshot
            rt.recover = False
            rt.finished_since = None
            self._save(job_id, status=STARTING, submitted_at=time.time(), error=None)

        ok, message = self.mega.get(link, dest, password)
        if not ok:
            with self.lock:
                self._save(job_id, status=FAILED,
                           error=message or "MEGA refused the link. Check that it is complete.")
            return
        self._pause_new_files(job_id, dest)

        timeout = 900 if is_folder_link(link) else 120
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            with self.lock:
                job = self.jobs.get(job_id)
                rt = self.rt.get(job_id)
                if job is None or rt is None or rt.tags or job["status"] != STARTING:
                    return
            self.stop_event.wait(0.5)
        if self.stop_event.is_set():
            return
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job["status"] == STARTING and not self.rt[job_id].tags:
                log.warning("Job %s: no transfer appeared within %ss", job_id, timeout)
                # Treat late transfers like those of a deleted job (cancel them).
                now = time.monotonic()
                self.ghosts[job_id] = (dict(job), self.rt[job_id], now + GHOST_TTL, now)
                self.rt[job_id] = JobRuntime()
                self._save(job_id, status=FAILED,
                           error="MEGA didn't start this download. Use Retry.")


def _name_from_link(link):
    m = re.search(r"/(file|folder)/([A-Za-z0-9_-]+)", link) or re.search(r"#F?!([A-Za-z0-9_-]+)", link)
    handle = m.group(m.lastindex) if m else link[-8:]
    return f"{'Folder' if is_folder_link(link) else 'File'} {handle}"
