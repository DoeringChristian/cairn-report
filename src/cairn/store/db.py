"""The metadata database of a store root: projects, versions, aliases (current and their log), consumers, and a
summary row per artifact, in one SQLite file (`<root>/metadata.sqlite3`, WAL mode). Blobs stay files.

    projects   (name) -> record (JSON: name, title, description, created), generation (bumped by every write
               to the project, so a listing can be cached until it changes)
    artifacts  (project, name) -> latest, versions, updated, metadata (of latest), aliases (JSON, current)
    versions   (project, name, number) -> uid, label, status, manifest (JSON: the whole version record), idhash
               (the SHA-256 of the uid, which short ids are prefixes of); indexed by uid, by label, and by idhash
    aliases    (project, name, alias) -> number, position (aliases keep the order they first appeared in)
    alias_log  every alias move, in order: project, name, alias, number, time
    consumers  (project, input ref) -> consumer ref, in the order they were recorded (refs: `name@v<n>`)
    retention  (project, prefix) -> rule (JSON: keep_last, keep_days, keep_aliased; see gc.py); made when first set
    meta       schema, sweeps (how many blob sweeps ran; gc.py)

A server's repo holds many projects; a project's own repo (`.cairn/`) holds one, under the project name "" (local.py,
OWN). A deleted version stays in `versions` as a tombstone (status `deleted`, its file list dropped), so its number and
uid are never handed out again; `artifacts.versions` counts the versions that are not deleted.

Every write is one short transaction (`BEGIN IMMEDIATE`), so a crash leaves either all of a publish or
none of it. Writers in one process queue on a lock before they take SQLite's; writers in other processes
on the same host wait for SQLite's lock (`busy_timeout`). Commits are durable (`synchronous=FULL`).
SQLite's locking needs a local filesystem: never put a root on NFS and the like (fs.py).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

FILE = "metadata.sqlite3"
SCHEMA = 1
BUSY_SECONDS = 60

TABLES = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY, record TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS artifacts (
    project TEXT NOT NULL, name TEXT NOT NULL, latest INTEGER, versions INTEGER NOT NULL DEFAULT 0,
    updated TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}', aliases TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS versions (
    project TEXT NOT NULL, name TEXT NOT NULL, number INTEGER NOT NULL, uid TEXT NOT NULL, label TEXT,
    status TEXT NOT NULL, manifest TEXT NOT NULL, idhash TEXT NOT NULL, PRIMARY KEY (project, name, number)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS versions_by_uid ON versions (uid);
CREATE INDEX IF NOT EXISTS versions_by_idhash ON versions (project, idhash);
CREATE INDEX IF NOT EXISTS versions_by_label ON versions (project, name, label, status);
CREATE TABLE IF NOT EXISTS aliases (
    project TEXT NOT NULL, name TEXT NOT NULL, alias TEXT NOT NULL, number INTEGER NOT NULL, position INTEGER NOT NULL,
    PRIMARY KEY (project, name, alias)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alias_log (
    seq INTEGER PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL, alias TEXT NOT NULL, number INTEGER NOT NULL,
    time TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS alias_log_by_artifact ON alias_log (project, name, seq);
CREATE TABLE IF NOT EXISTS consumers (
    seq INTEGER PRIMARY KEY, project TEXT NOT NULL, input TEXT NOT NULL, consumer TEXT NOT NULL, time TEXT NOT NULL,
    UNIQUE (project, input, consumer));
"""
RETENTION_TABLE = ("CREATE TABLE IF NOT EXISTS retention (project TEXT NOT NULL, prefix TEXT NOT NULL, rule TEXT NOT NULL, "
                   "PRIMARY KEY (project, prefix)) WITHOUT ROWID")
TABLES += RETENTION_TABLE + ";\n"


def _wal(connection: sqlite3.Connection) -> None:
    """Put the database in WAL mode (once: it stays so in the file). Switching needs a moment alone with the file and
    does not wait for SQLite's busy timeout, so processes opening a new repo at the same moment (a job array) retry."""
    deadline = time.monotonic() + BUSY_SECONDS
    while True:
        try:
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if not ("locked" in str(error) or "busy" in str(error)) or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


class Database:
    """One store root's metadata database: a connection per thread, and `write()` transactions."""

    def __init__(self, root: Path, store_uid: str):
        self.root, self.path = Path(root), Path(root) / FILE
        self.store_uid = store_uid
        self._local = threading.local()
        self.write_lock = threading.Lock()  # this process's writers queue here, not in SQLite's busy loop
        fresh = not self.path.exists()
        connection = self.connection()
        if not fresh and self._ready(connection):  # the usual open: a read, no write lock
            return
        with self.write_lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in TABLES.split(";"):  # one by one: executescript would commit
                    if statement.strip():
                        connection.execute(statement)
                if fresh or self.meta(connection, "schema") is None:
                    connection.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _ready(connection: sqlite3.Connection) -> bool:
        """The schema exists."""
        try:
            row = connection.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        except sqlite3.OperationalError:  # no meta table yet
            return False
        return row is not None and row[0] == str(SCHEMA)

    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=BUSY_SECONDS, isolation_level=None, check_same_thread=False)
            _wal(connection)
            connection.execute("PRAGMA synchronous=FULL")
            self._local.connection = connection
        return connection

    @staticmethod
    def meta(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    @contextmanager
    def write(self):
        """A write transaction on this thread's connection (committed at the end, rolled back on an exception)."""
        connection = self.connection()
        with self.write_lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    @contextmanager
    def read(self):
        """A read transaction: every statement in it sees the same committed state."""
        connection = self.connection()
        if connection.in_transaction:  # inside this thread's write: read what it wrote so far
            yield connection
            return
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            connection.execute("COMMIT")


def export(database: Database, folder: Path) -> dict[str, int]:
    """Readable JSON Lines copies of the metadata (for backups and inspection): projects.jsonl, versions.jsonl
    (every version's record with its project and name), aliases.jsonl (the alias log), consumers.jsonl.
    Written from one consistent snapshot; returns the row count per file."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    counts = {}
    with database.read() as connection:
        queries = {
            "projects.jsonl": ("SELECT record FROM projects ORDER BY name", lambda row: json.loads(row[0])),
            "versions.jsonl": ("SELECT project, name, manifest FROM versions ORDER BY project, name, number",
                               lambda row: {"project": row[0], "name": row[1], **json.loads(row[2])}),
            "aliases.jsonl": ("SELECT project, name, alias, number, time FROM alias_log ORDER BY seq",
                              lambda row: {"project": row[0], "name": row[1], "alias": row[2], "version": row[3], "time": row[4]}),
            "consumers.jsonl": ("SELECT project, input, consumer, time FROM consumers ORDER BY seq",
                                lambda row: {"project": row[0], "input": row[1], "consumer": row[2], "time": row[3]}),
        }
        for file, (query, shape) in queries.items():
            temporary = folder / f".{file}.tmp"
            count = 0
            with temporary.open("w", encoding="utf-8") as handle:
                for row in connection.execute(query):
                    handle.write(json.dumps(shape(row)) + "\n")
                    count += 1
            os.replace(temporary, folder / file)
            counts[file] = count
    return counts


class ServerLock:
    """`<root>/server.lock`: locked (flock, exclusive) by the `cairn server` serving the root, for as long as it
    runs, so a second server is refused and other processes know not to write (LocalRepo checks `held`).
    flock is reliable on local filesystems only, which is all a store root may live on (fs.py)."""

    def __init__(self, root: Path):
        self.path = Path(root) / "server.lock"
        self.handle = None

    def acquire(self) -> None:
        """Lock the root for this process; SystemExit if another process holds it."""
        try:
            import fcntl
        except ImportError:  # no flock (Windows): nothing to enforce
            return
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip() or "another process"
            handle.close()
            raise SystemExit(f"{self.path.parent} is already served (by pid {holder}); one server per repo")
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self.handle = handle

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()  # closing drops the lock
            self.handle = None

    @staticmethod
    def held(root: Path) -> bool:
        """Whether a server holds the root's lock right now."""
        path = Path(root) / "server.lock"
        try:
            import fcntl

            handle = open(path, "rb")
        except (ImportError, FileNotFoundError):
            return False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            handle.close()
        return False
