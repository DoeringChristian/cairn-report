"""The metadata database of a store root: projects, versions, aliases (current and their log), consumers, and a
summary row per artifact, in one SQLite file (`<root>/metadata.sqlite3`, WAL mode). Blobs stay files.

    projects   (name) -> record (JSON: name, title, description, created), generation (bumped by every write
               to the project, so a listing can be cached until it changes)
    artifacts  (project, name) -> latest, versions, updated, metadata (of latest), aliases (JSON, current)
    versions   (project, name, number) -> uid, label, status, manifest (JSON: the whole version record);
               indexed by uid and by label
    aliases    (project, name, alias) -> number, position (aliases keep the order they first appeared in)
    alias_log  every alias move, in order: project, name, alias, number, time
    consumers  input ref -> consumer ref, in the order they were recorded
    retention  (project, prefix) -> rule (JSON: keep_last, keep_days, keep_aliased; see gc.py); made when first set
    meta       schema, legacy (import state), sweeps (how many blob sweeps ran; gc.py)

A deleted version stays in `versions` as a tombstone (status `deleted`, its file list dropped), so its number and
uid are never handed out again; `artifacts.versions` counts the versions that are not deleted.

Every write is one short transaction (`BEGIN IMMEDIATE`), so a crash leaves either all of a publish or
none of it. Writers in one process queue on a lock before they take SQLite's; writers in other processes
on the same host wait for SQLite's lock (`busy_timeout`). Commits are durable (`synchronous=FULL`).
SQLite's locking needs a local filesystem: never put a root on NFS and the like (fs.py).

A root written before the database existed (JSON Lines files under `projects/`) is imported on first open,
in one transaction, and the old files are kept as `*.migrated` (see `import_legacy`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ..formats import read_jsonl
from .uids import legacy

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
    status TEXT NOT NULL, manifest TEXT NOT NULL, PRIMARY KEY (project, name, number)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS versions_by_uid ON versions (uid);
CREATE INDEX IF NOT EXISTS versions_by_label ON versions (project, name, label, status);
CREATE TABLE IF NOT EXISTS aliases (
    project TEXT NOT NULL, name TEXT NOT NULL, alias TEXT NOT NULL, number INTEGER NOT NULL, position INTEGER NOT NULL,
    PRIMARY KEY (project, name, alias)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alias_log (
    seq INTEGER PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL, alias TEXT NOT NULL, number INTEGER NOT NULL,
    time TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS alias_log_by_artifact ON alias_log (project, name, seq);
CREATE TABLE IF NOT EXISTS consumers (
    seq INTEGER PRIMARY KEY, input TEXT NOT NULL, consumer TEXT NOT NULL, time TEXT NOT NULL, UNIQUE (input, consumer));
"""
RETENTION_TABLE = ("CREATE TABLE IF NOT EXISTS retention (project TEXT NOT NULL, prefix TEXT NOT NULL, rule TEXT NOT NULL, "
                   "PRIMARY KEY (project, prefix)) WITHOUT ROWID")
TABLES += RETENTION_TABLE + ";\n"


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
        if self.meta(connection, "legacy") != "done":
            import_legacy(self)

    @staticmethod
    def _ready(connection: sqlite3.Connection) -> bool:
        """The schema exists and any legacy import is done."""
        try:
            rows = dict(connection.execute("SELECT key, value FROM meta WHERE key IN ('schema', 'legacy')").fetchall())
        except sqlite3.OperationalError:  # no meta table yet
            return False
        return rows.get("schema") == str(SCHEMA) and rows.get("legacy") == "done"

    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=BUSY_SECONDS, isolation_level=None, check_same_thread=False)
            connection.execute("PRAGMA journal_mode=WAL")
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


# ------------------------------------------------------------------ the JSON Lines layout before the database


def _legacy_files(root: Path) -> list[Path]:
    projects = root / "projects"
    if not projects.is_dir():
        return []
    return sorted([*projects.glob("*/project.json"), *projects.rglob("*.jsonl")])


def import_legacy(database: Database) -> None:
    """Import a root written before the database (projects/<p>/project.json, consumers.jsonl, and per artifact
    versions.jsonl and aliases.jsonl) in one transaction, then rename the old files to `*.migrated`.

    The last state line per version wins; a version without a uid gets its deterministic legacy uid
    (`uids.legacy`, the same one earlier versions derived). A crash after the commit only leaves files
    unrenamed: the next open renames them (the import itself never runs twice)."""
    root = database.root
    with database.write() as connection:
        if Database.meta(connection, "legacy") is None:
            projects = root / "projects"
            for record_file in sorted(projects.glob("*/project.json")) if projects.is_dir() else []:
                _import_project(connection, database.store_uid, record_file.parent)
            connection.execute("INSERT INTO meta VALUES ('legacy', 'imported')")
    renamed = True
    for file in _legacy_files(root):
        try:
            os.replace(file, file.with_name(file.name + ".migrated"))
        except FileNotFoundError:  # another process renamed it first
            pass
        except OSError:  # a read-only root: the database is what counts now; try again next time
            renamed = False
    if renamed:
        with database.write() as connection:
            connection.execute("UPDATE meta SET value = 'done' WHERE key = 'legacy'")


def _import_project(connection: sqlite3.Connection, store_uid: str, folder: Path) -> None:
    from .local import summary_row

    record = json.loads((folder / "project.json").read_text())
    project = folder.name
    connection.execute("INSERT OR IGNORE INTO projects (name, record) VALUES (?, ?)", (project, json.dumps(record)))
    base = folder / "artifacts"
    for log in sorted(base.rglob("versions.jsonl")) if base.is_dir() else []:
        name = log.parent.relative_to(base).as_posix()
        states: dict[int, dict] = {}
        for row in read_jsonl(log):
            if isinstance(row.get("version"), int):
                states[row["version"]] = row
        for number, state in sorted(states.items()):
            if not state.get("uid"):
                state["uid"] = legacy(f"{store_uid}/{project}/{name}/{number}", state.get("created"))
            connection.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (project, name, number, state["uid"], state.get("label"), state["status"], json.dumps(state)))
        aliases: dict[str, int] = {}
        for row in read_jsonl(log.parent / "aliases.jsonl"):
            if not isinstance(row.get("alias"), str) or not isinstance(row.get("version"), int):
                continue
            cursor = connection.execute("INSERT INTO alias_log (project, name, alias, number, time) VALUES (?, ?, ?, ?, ?)",
                                        (project, name, row["alias"], row["version"], row.get("time") or ""))
            connection.execute("INSERT INTO aliases VALUES (?, ?, ?, ?, ?) ON CONFLICT (project, name, alias) "
                               "DO UPDATE SET number = excluded.number", (project, name, row["alias"], row["version"], cursor.lastrowid))
            aliases[row["alias"]] = row["version"]
        connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)", (project, name, *summary_row(states, aliases)))
    for row in read_jsonl(folder / "consumers.jsonl"):
        if isinstance(row.get("input"), str) and isinstance(row.get("consumer"), str):
            connection.execute("INSERT OR IGNORE INTO consumers (input, consumer, time) VALUES (?, ?, ?)",
                               (row["input"], row["consumer"], row.get("time") or ""))


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
            "consumers.jsonl": ("SELECT input, consumer, time FROM consumers ORDER BY seq",
                                lambda row: {"input": row[0], "consumer": row[1], "time": row[2]}),
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
    runs, so a second server is refused and other processes know not to write (LocalStore checks `held`).
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
            raise SystemExit(f"{self.path.parent} is already served (by pid {holder}); one server per store root")
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
