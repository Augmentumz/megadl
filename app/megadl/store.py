"""SQLite persistence for download jobs and settings."""

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    link          TEXT NOT NULL,
    link_password TEXT,
    dest_dir      TEXT NOT NULL,
    root_path     TEXT,
    name          TEXT,
    kind          TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    desired       TEXT NOT NULL DEFAULT 'run',
    total_bytes   INTEGER NOT NULL DEFAULT 0,
    done_bytes    INTEGER NOT NULL DEFAULT 0,
    files_total   INTEGER NOT NULL DEFAULT 0,
    files_done    INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    preexisting   INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    submitted_at  REAL,
    completed_at  REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the first release, created on existing databases.
MIGRATIONS = {
    "skipped": "TEXT NOT NULL DEFAULT '[]'",        # JSON list of unticked paths
    "files_skipped": "INTEGER NOT NULL DEFAULT 0",
}

JOB_FIELDS = (
    "link", "link_password", "dest_dir", "root_path", "name", "kind", "status",
    "desired", "total_bytes", "done_bytes", "files_total", "files_done",
    "error", "preexisting", "submitted_at", "completed_at",
    "skipped", "files_skipped",
)


class Store:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            have = {r["name"] for r in self._db.execute("PRAGMA table_info(jobs)")}
            for column, decl in MIGRATIONS.items():
                if column not in have:
                    self._db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {decl}")
            self._db.commit()

    # ------------------------------------------------------------------- jobs
    def add_job(self, link, dest_dir, link_password=None, desired="run"):
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO jobs (link, link_password, dest_dir, desired, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (link, link_password, dest_dir, desired, time.time()),
            )
            self._db.commit()
            return cur.lastrowid

    def get_job(self, job_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def update_job(self, job_id, **fields):
        fields = {k: v for k, v in fields.items() if k in JOB_FIELDS}
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
            self._db.commit()

    def delete_job(self, job_id):
        with self._lock:
            self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._db.commit()

    # --------------------------------------------------------------- settings
    def get_settings(self, defaults):
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM settings").fetchall()
        result = dict(defaults)
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except ValueError:
                pass
        return result

    def set_settings(self, **values):
        with self._lock:
            for key, value in values.items():
                self._db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )
            self._db.commit()
