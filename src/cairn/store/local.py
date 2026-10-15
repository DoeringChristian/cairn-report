"""LocalRepo: a repo folder on this machine (the project's `.cairn/`, or a server's folder), its metadata and blobs.

    <repo>/
      repo.json                  the repo's uid (UUIDv7, made once, never changed), its layout, and its remote's uid
      metadata.sqlite3           projects, versions, aliases and their log, consumers (db.py; WAL mode)
      blobs/<aa>/<sha256>        file contents (blobs.BlobStore), shared by all versions and the outbox
      outbox/<uid>/              versions on their way to the remote (client/outbox.py); their blobs are in blobs/
      versions/<uid>/files/      read-only folders `pull` returns (cache.py)
      drafts/  build/  share/    folders that become versions, report builds, shared pages
      token  tokens.jsonl        the owner token and scoped tokens, for `cairn server` and `cairn ui` (tokens.py)
      heartbeats/<uid>           a running version's owner {time, host, pid}, rewritten while it runs (see `reap`)
      server.lock                locked by the `cairn server` serving this repo (db.ServerLock)
      tmp/                       uploads in progress

A version starts `running` (it may grow), then becomes `completed` or `failed`; a completed version
is never written again, except to delete it. Only completed versions resolve through `latest`, aliases, and labels, so
readers never see half-published data. Every change is one SQLite transaction: a publish (version,
files, completion, `latest`, aliases, consumers) commits whole or not at all, and version numbers are
handed out inside the transaction, so they are unique and contiguous however many threads and processes publish.

Without a remote the repo is the source of truth and numbers its versions. With one, the repo is the remote's
cache: it holds copies of the versions the remote numbered (`put_copy`, `origin` = the remote's uid, with their
aliases mirrored by `mirror_aliases`), and the versions on their way there wait in the outbox, not here. Versions a
repo numbered itself before it had a remote are sent once (client/sync.py adopts them: `local_versions`,
`forget_versions`) and come back as copies with the remote's numbers.

Every version has a `uid` (a UUIDv7, see uids.py), made when it starts or supplied by the client
that created it; a uid names one version for good, in the whole repo and on every remote.

Every process on the repo's host opens the repo directly, whether or not `cairn server` serves it: scripts,
commands, `cairn ui`, and the server itself. Any number may write at once: each write is one SQLite transaction
(serialized by SQLite's lock), caches follow the projects' generations, and gc rechecks inside a transaction
(gc.py). Other hosts reach a repo only through `cairn server`. At most one server serves a repo (`server.lock`,
db.ServerLock). A repo database is never written on a network filesystem (fs.py).

Deleting a version leaves a tombstone (status `deleted`: number, uid, label, metadata, and inputs kept, the file
list dropped), so a number or uid is never used again; references to it fail with Deleted, and lineage keeps its
edges. Protected versions (an alias other than `latest` points at them, a version that is not deleted consumed
them, or they are running) are refused unless forced. `gc` applies retention rules and removes unreferenced blobs
(gc.py); blobs an outbox entry needs are referenced too.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..ranges import byte_range
from . import gc, refs
from .folders import pid_alive
from .blobs import CHUNK, BlobStore, BlobWriter, clean_entries, now, resolve_entry
from .db import RETENTION_TABLE, Database, export
from .digest import manifest_digest
from .errors import BadRange, Conflict, Deleted, Forbidden, MissingBlobs, NotFound, Protected, StoreError, Unauthorized  # noqa: F401
from .fs import network_filesystem, network_fs_message
from .refs import Ref
from .uids import UID, new_uid, valid_uid


@dataclass
class FileRead:
    """A file of a version, or a byte range of it: `chunks` yields bytes `first`..`last` of `total`.

    `sha256` identifies the contents (the ETag); `immutable` says a reader may cache it forever (an
    exact `@v<n>` of a completed version). Call `close()` when not reading `chunks` to the end.
    """
    total: int
    first: int
    last: int
    sha256: str
    immutable: bool
    chunks: Iterator[bytes]

    @property
    def partial(self) -> bool:
        return (self.first, self.last) != (0, self.total - 1)

    @property
    def size(self) -> int:
        return max(self.last - self.first + 1, 0)

    def close(self) -> None:
        self.chunks.close()


def _same(version: dict, digest: str, inputs: list[str], metadata: dict) -> bool:
    """Whether a completed version has these files (by manifest digest), inputs, and metadata."""
    return (version.get("digest") or manifest_digest(version["files"])) == digest and \
        sorted(version["inputs"]) == sorted(inputs) and version["metadata"] == metadata


def _check_digest(given: str | None, actual: str, what: str) -> None:
    """A client's digest of the files it sent must be the one the store computes from them (Conflict, a 409, if not)."""
    if given is not None and given != actual:
        raise Conflict(f"{what}: the manifest digest {given} does not match the files received ({actual})")


REPO_FILE = "repo.json"
LAYOUT = 1


def repo_uid(root: Path) -> str:
    """The uid in `<root>/repo.json`, made on first use (the folder then becomes private to its owner, mode 700).
    Two processes racing both end with the one that won (the file is linked into place, which fails if it exists),
    so a repo's uid never changes."""
    path = root / REPO_FILE
    for _ in range(3):
        try:
            return valid_uid(json.loads(path.read_text())["uid"])
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            raise StoreError(f"{path} is damaged: it must hold {{\"uid\": <UUIDv7>, ...}}")
        temporary = root / f"{REPO_FILE}.{secrets.token_hex(6)}.tmp"
        temporary.write_text(json.dumps({"uid": new_uid(), "layout": LAYOUT, "created": now()}, indent=1))
        try:
            os.link(temporary, path)
            try:
                os.chmod(root, 0o700)
            except OSError:
                pass
        except FileExistsError:
            pass
        except OSError:  # a filesystem without hard links: rename, accepting a tiny race on first use
            if not path.exists():
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return valid_uid(json.loads(path.read_text())["uid"])


def read_repo_file(root: Path) -> dict:
    """`<root>/repo.json` as a dict ({} if it is missing or damaged)."""
    try:
        value = json.loads((Path(root) / REPO_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def update_repo_file(root: Path, **fields) -> None:
    """Add fields to `<root>/repo.json` (write-temp-and-rename; the uid stays). Never raises: it only records facts
    (the remote's uid) that are learned again on the next contact."""
    root = Path(root)
    try:
        current = read_repo_file(root)
        if not current.get("uid") or all(current.get(key) == value for key, value in fields.items()):
            return
        temporary = root / f"{REPO_FILE}.{secrets.token_hex(6)}.tmp"
        temporary.write_text(json.dumps({**current, **fields}, indent=1))
        os.replace(temporary, root / REPO_FILE)
    except OSError:
        pass


def outbox_blobs(root: Path) -> set[str]:
    """The blobs the outbox entries of the repo `root` need (`<root>/outbox/<uid>/manifest.json`): never garbage."""
    found: set[str] = set()
    folder = Path(root) / "outbox"
    try:
        entries = list(os.scandir(folder))
    except FileNotFoundError:
        return found
    for entry in entries:
        if ".tmp-" in entry.name:
            continue
        try:
            record = json.loads(Path(entry.path, "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            found.update(item["sha256"] for item in record.get("files") or [] if isinstance(item, dict) and "sha256" in item)
    return found


def summary_row(states: dict[int, dict], aliases: dict[str, int]) -> tuple:
    """(latest, versions, updated, metadata JSON, aliases JSON): an artifact's summary row (db.py)."""
    stamps = [_stamp(state) for state in states.values()]
    head = states.get(aliases.get("latest"))
    return (aliases.get("latest"), len(states), max(stamps, default=""), json.dumps(head["metadata"] if head else {}),
            json.dumps(aliases))


def _check_tree(entries: list[dict]) -> None:
    """No path may be both a file and a folder."""
    paths = {entry["path"] for entry in entries}
    for path in paths:
        parts = path.split("/")
        for end in range(1, len(parts)):
            if "/".join(parts[:end]) in paths:
                raise StoreError(f"{path}: {'/'.join(parts[:end])} is a file in this version")


def _stamp(state: dict) -> str:
    return state.get("completed") or state.get("created") or ""

_SUMMARY = "SELECT name, latest, versions, aliases, updated, metadata FROM artifacts"


class LocalRepo:
    def __init__(self, root: Path | str, project: str | None = None, allow_network_fs: bool = False,
                 heartbeat_timeout: float = 600.0):
        """`project` is the default for references without one. A folder on a network filesystem is refused
        (StoreError, before anything is written) unless `allow_network_fs`: the caller has made sure one process on
        one host is its only writer (`[repo] allow_network_fs`, `cairn server --allow-network-fs`), or only reads it.
        Opening the repo fails the running versions whose owner is gone (`reap`; `heartbeat_timeout` seconds without
        a heartbeat, or its process dead on this host); what it failed is in `self.reaped`."""
        self.root, self.project = Path(root), project
        if not allow_network_fs:
            fstype = network_filesystem(self.root)
            if fstype:
                raise StoreError(network_fs_message(self.root, fstype))
        self.root.mkdir(parents=True, exist_ok=True)
        self._blobs = BlobStore(self.root)  # creates blobs/ and tmp/
        self.uid = repo_uid(self.root)
        self._db = Database(self.root, self.uid)
        self._add_digests()
        self._listings: dict[str, tuple[int, list[dict]]] = {}
        self.reaped = self.reap(heartbeat_timeout)

    def _add_digests(self) -> None:
        """Give every completed version that has no manifest digest its digest, once per root, in one transaction
        (a root written before digests existed). Idempotent and deterministic, so racing processes agree."""
        if Database.meta(self._db.connection(), "digests") == "done":
            return
        try:
            with self._db.write() as connection:
                if Database.meta(connection, "digests") == "done":
                    return
                rows = connection.execute("SELECT project, name, number, manifest FROM versions WHERE status = 'completed'").fetchall()
                for project, name, number, text in rows:
                    state = json.loads(text)
                    if not state.get("digest"):
                        state["digest"] = manifest_digest(state["files"])
                        connection.execute("UPDATE versions SET manifest = ? WHERE project = ? AND name = ? AND number = ?",
                                           (json.dumps(state), project, name, number))
                connection.execute("INSERT OR REPLACE INTO meta VALUES ('digests', 'done')")
        except sqlite3.OperationalError:  # a read-only root: the digests are added by the first writer that opens it
            pass

    # ------------------------------------------------------------------ heartbeats

    def _beat_file(self, uid: str) -> Path:
        return self.root / "heartbeats" / valid_uid(uid)

    def beat(self, uid: str) -> None:
        """The owner of a running version says it is alive (never raises: a missed beat only makes the version
        look dead later)."""
        path = self._beat_file(uid)
        try:
            path.parent.mkdir(exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps({"time": time.time(), "host": socket.gethostname(), "pid": os.getpid()}))
            os.replace(temporary, path)
        except OSError:
            pass

    def _clear_beat(self, uid: str) -> None:
        try:
            self._beat_file(uid).unlink(missing_ok=True)
        except OSError:
            pass

    def reap(self, timeout: float = 600.0) -> list[tuple[str, str]]:
        """Fail the running versions whose owner is gone: no heartbeat for `timeout` seconds, or its process (on
        this host) is dead. Only versions that have a heartbeat (`beat`) are judged. Returns [(ref, reason)].
        Skipped on a read-only root."""
        folder = self.root / "heartbeats"
        if not folder.is_dir():
            return []
        failed = []
        for path in sorted(folder.iterdir()):
            if path.name.startswith(".") or not UID.fullmatch(path.name):
                continue
            try:
                beat = json.loads(path.read_text())
                stamp = float(beat["time"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            gone = beat.get("host") == socket.gethostname() and isinstance(beat.get("pid"), int) and not pid_alive(beat["pid"])
            if not gone and time.time() - stamp <= timeout:
                continue
            since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
            reason = f"the process died (no heartbeat since {since})"
            try:
                with self._write() as connection:
                    found = self._by_uid(connection, path.name)
                    if found is not None and found[2]["status"] == "running":
                        project, name, state = found
                        self._save(connection, project, name, {**state, "status": "failed", "completed": now(), "error": reason})
                        failed.append((refs.exact(project, name, state["version"]), reason))
            except (StoreError, sqlite3.OperationalError, OSError):
                continue  # read-only or busy: the next open tries again
            self._clear_beat(path.name)
        return failed

    def _writing(self) -> None:
        """Every write passes here (the network filesystem was checked when the repo was opened)."""

    def _write(self):
        """A metadata write transaction (db.Database.write)."""
        self._writing()
        return self._db.write()

    def verify(self):
        """Re-hash the store (store/verify.py): every blob against its name, every version's blobs present, every
        completed version's manifest digest against its files; returns a `verify.Report`."""
        from .verify import verify_store

        return verify_store(self)

    def export_metadata(self, folder: Path | str) -> dict[str, int]:
        """Readable JSON Lines copies of the metadata (db.export); returns the row count per file."""
        return export(self._db, Path(folder))

    # ------------------------------------------------------------------ blobs

    def blob_path(self, sha: str) -> Path:
        return self._blobs.blob_path(sha)

    def blob_file(self, project: str, name: str, version: int, entry: dict, root=None) -> Path:
        """A local file with the contents of a version's entry (here: the blob itself)."""
        return self.blob_path(entry["sha256"])

    def open_file(self, project: str, name: str, selector: str | None, path: str) -> tuple[dict, Path, bool]:
        """(entry, blob path, immutable) of a file of the version `selector` names (`v<n>` or a uid of any status,
        else an alias or label of a completed one). `immutable`: an exact selector of a completed version."""
        if selector and refs.VERSION.fullmatch(selector):
            version = self.get_version(project, name, int(selector[1:]))
        elif selector and UID.fullmatch(selector):
            version = self.get_version(project, name, selector)
        else:
            version = self.resolve(Ref(project, name, selector))[2]
        if version["status"] == "deleted":
            raise self._gone(project, name, version)
        entry = resolve_entry(version["files"], path)
        if entry is None:
            raise NotFound(f"{name} has no file {path}")
        immutable = version["status"] == "completed" and bool(selector) and bool(refs.VERSION.fullmatch(selector) or UID.fullmatch(selector))
        return entry, self.blob_path(entry["sha256"]), immutable

    def read_file(self, project: str, name: str, selector: str | None, path: str, range_header: str | None = None) -> FileRead:
        """A file of the version `selector` names (`v<n>` of any status, else an alias or label of a completed one),
        optionally a byte range (the value of an HTTP Range header). Raises NotFound and BadRange."""
        entry, blob, immutable = self.open_file(project, name, selector, path)
        total = blob.stat().st_size
        wanted = byte_range(range_header, total)
        if wanted is False:
            raise BadRange(total)
        first, last = wanted or (0, total - 1)

        def chunks():
            with blob.open("rb") as handle:
                handle.seek(first)
                remaining = last - first + 1
                while remaining > 0:
                    chunk = handle.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    yield chunk
                    remaining -= len(chunk)

        return FileRead(total, first, last, entry["sha256"], immutable, chunks())

    def has_blob(self, sha: str) -> bool:
        return self._blobs.has_blob(sha)

    def put_blob(self, sha: str, stream, size: int) -> bool:
        self._writing()
        return self._blobs.put_blob(sha, stream, size)

    def blob_writer(self, sha: str, size: int | None = None) -> BlobWriter:
        """A writer that hashes and stores one blob as its bytes arrive (blobs.BlobWriter); the server streams uploads into it."""
        self._writing()
        return self._blobs.writer(sha, size)

    def put_file(self, sha: str, source: Path | str, link: bool = False) -> bool:
        """Store a local file as a blob: a copy-on-write clone where the filesystem has them, else a copy (verified);
        `link`: a hard link instead of the copy, for a source that is deleted right after (a published draft)."""
        self._writing()
        return self._blobs.put_file(sha, Path(source), link=link)

    def missing(self, entries: list[dict]) -> list[str]:
        return self._blobs.missing(entries)

    def missing_hashes(self, hashes) -> list[str]:
        """The hashes not stored yet, sorted, in one batched check."""
        return self._blobs.missing_hashes(hashes)

    # --------------------------------------------------------------- projects

    @staticmethod
    def _record_of(connection: sqlite3.Connection, project: str) -> dict:
        row = connection.execute("SELECT record FROM projects WHERE name = ?", (refs.valid_project(project),)).fetchone()
        if row is None:
            raise NotFound(f"unknown project {project!r}")
        return json.loads(row[0])

    def create_project(self, name: str, title: str | None = None, description: str = "") -> dict:
        """Create a project; an existing one is returned unchanged."""
        refs.valid_project(name)
        with self._db.read() as connection:
            row = connection.execute("SELECT record FROM projects WHERE name = ?", (name,)).fetchone()
        if row is not None:
            return json.loads(row[0])
        with self._write() as connection:
            row = connection.execute("SELECT record FROM projects WHERE name = ?", (name,)).fetchone()
            if row is not None:
                return json.loads(row[0])
            record = {"name": name, "title": title or name, "description": description, "created": now()}
            connection.execute("INSERT INTO projects (name, record) VALUES (?, ?)", (name, json.dumps(record)))
            return record

    def get_project(self, name: str) -> dict:
        with self._db.read() as connection:
            return self._record_of(connection, name)

    def update_project(self, name: str, title: str | None = None, description: str | None = None) -> dict:
        with self._write() as connection:
            record = self._record_of(connection, name)
            record.update({key: value for key, value in (("title", title), ("description", description)) if value is not None})
            connection.execute("UPDATE projects SET record = ?, generation = generation + 1 WHERE name = ?", (json.dumps(record), name))
            return record

    def projects(self) -> list[dict]:
        with self._db.read() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT record FROM projects ORDER BY name")]

    def overviews(self) -> list[dict]:
        """Every project record with its artifact count and last update."""
        with self._db.read() as connection:
            counts = {row[0]: (row[1], row[2]) for row in
                      connection.execute("SELECT project, count(*), max(updated) FROM artifacts WHERE versions > 0 GROUP BY project")}
            found = []
            for (text,) in connection.execute("SELECT record FROM projects ORDER BY name"):
                record = json.loads(text)
                count, updated = counts.get(record["name"], (0, None))
                found.append({**record, "artifacts": count, "updated": updated or record["created"]})
            return found

    # --------------------------------------------------------------- artifacts

    @staticmethod
    def _project(connection: sqlite3.Connection, project: str) -> None:
        if connection.execute("SELECT 1 FROM projects WHERE name = ?", (refs.valid_project(project),)).fetchone() is None:
            raise NotFound(f"unknown project {project!r}")

    def _check(self, connection: sqlite3.Connection, project: str, name: str) -> None:
        self._project(connection, project)
        refs.valid_name(name)

    def artifacts(self, project: str) -> list[str]:
        with self._db.read() as connection:
            self._project(connection, project)
            return [row[0] for row in connection.execute("SELECT name FROM artifacts WHERE project = ? AND versions > 0 ORDER BY name",
                                                         (project,))]

    @staticmethod
    def _state(connection, project: str, name: str, number: int) -> dict | None:
        row = connection.execute("SELECT manifest FROM versions WHERE project = ? AND name = ? AND number = ?",
                                 (project, name, number)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _states(connection, project: str, name: str) -> dict[int, dict]:
        return {row[0]: json.loads(row[1]) for row in connection.execute(
            "SELECT number, manifest FROM versions WHERE project = ? AND name = ? ORDER BY number", (project, name))}

    @staticmethod
    def _by_uid(connection, uid: str) -> tuple[str, str, dict] | None:
        """(project, name, record) of the version with this uid, anywhere in the store."""
        row = connection.execute("SELECT project, name, manifest FROM versions WHERE uid = ?", (uid,)).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    @staticmethod
    def _aliases(connection, project: str, name: str) -> dict[str, int]:
        return {row[0]: row[1] for row in connection.execute(
            "SELECT alias, number FROM aliases WHERE project = ? AND name = ? ORDER BY position", (project, name))}

    def version_numbers(self, project: str, name: str) -> set[int]:
        """The artifact's version numbers (cheaper than `versions` when only they are needed)."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            return {row[0] for row in connection.execute("SELECT number FROM versions WHERE project = ? AND name = ?", (project, name))}

    def last_number(self, project: str, name: str) -> int:
        """The artifact's highest version number (0 without versions); a new version always gets the next one."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            return connection.execute("SELECT coalesce(max(number), 0) FROM versions WHERE project = ? AND name = ?",
                                      (project, name)).fetchone()[0]

    def versions(self, project: str, name: str) -> list[dict]:
        with self._db.read() as connection:
            self._check(connection, project, name)
            return list(self._states(connection, project, name).values())

    def get_version(self, project: str, name: str, version: int | str) -> dict:
        """One version's manifest, by number or uid, whatever its status."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            if isinstance(version, str):
                found = self._by_uid(connection, version)
                state = found[2] if found and found[:2] == (project, name) else None
            else:
                state = self._state(connection, project, name, version)
        if state is None:
            raise NotFound(f"{project}:{name} has no version {version if isinstance(version, str) else f'v{version}'}")
        return state

    @staticmethod
    def _summary(row) -> dict:
        name, latest, versions, aliases, updated, metadata = row
        return {"name": name, "latest": latest, "versions": versions, "aliases": json.loads(aliases), "updated": updated,
                "metadata": json.loads(metadata)}

    def summary(self, project: str, name: str) -> dict:
        """The artifact at a glance: latest completed version (number and metadata), version count, aliases, last change."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            row = connection.execute(f"{_SUMMARY} WHERE project = ? AND name = ?", (project, name)).fetchone()
        if row is None:
            return {"name": name, "latest": None, "versions": 0, "aliases": {}, "updated": "", "metadata": {}}
        return self._summary(row)

    def summaries(self, project: str) -> list[dict]:
        return list(self.summaries_stamped(project)[1])

    def summaries_stamped(self, project: str) -> tuple[tuple, list[dict]]:
        """(stamp, summaries): the stamp changes whenever the listing may have (the project's generation, which
        every write to the project bumps), so callers may cache on it; the list is cached here the same way."""
        with self._db.read() as connection:
            row = connection.execute("SELECT generation FROM projects WHERE name = ?", (refs.valid_project(project),)).fetchone()
            if row is None:
                raise NotFound(f"unknown project {project!r}")
            cached = self._listings.get(project)
            if cached is None or cached[0] != row[0]:
                rows = connection.execute(f"{_SUMMARY} WHERE project = ? AND versions > 0 ORDER BY name", (project,)).fetchall()
                cached = (row[0], [self._summary(item) for item in rows])
                self._listings[project] = cached
        return (project, cached[0]), cached[1]

    def aliases(self, project: str, name: str) -> dict[str, int]:
        """Where each alias points now (the last move wins)."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            return self._aliases(connection, project, name)

    def alias_log(self, project: str, name: str) -> list[dict]:
        """Every move of the artifact's aliases, oldest first: {alias, version, time}."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            return [{"alias": row[0], "version": row[1], "time": row[2]} for row in connection.execute(
                "SELECT alias, number, time FROM alias_log WHERE project = ? AND name = ? ORDER BY seq", (project, name))]

    # ------------------------------------------------------------------ writes (each one transaction)

    @staticmethod
    def _save(connection, project: str, name: str, state: dict, created: bool = False) -> None:
        """Store a version's record, and keep the artifact's summary row and the project's generation current."""
        values = (state["uid"], state["label"], state["status"], json.dumps(state), project, name, state["version"])
        if created:
            connection.execute("INSERT INTO versions (uid, label, status, manifest, project, name, number) VALUES (?, ?, ?, ?, ?, ?, ?)", values)
            connection.execute("INSERT INTO artifacts (project, name, versions, updated) VALUES (?, ?, 1, ?) ON CONFLICT (project, name) "
                               "DO UPDATE SET versions = versions + 1, updated = max(updated, excluded.updated)", (project, name, _stamp(state)))
        else:
            connection.execute("UPDATE versions SET uid = ?, label = ?, status = ?, manifest = ? WHERE project = ? AND name = ? AND number = ?", values)
            connection.execute("UPDATE artifacts SET updated = max(updated, ?) WHERE project = ? AND name = ?", (_stamp(state), project, name))
        connection.execute("UPDATE projects SET generation = generation + 1 WHERE name = ?", (project,))

    def _running(self, connection, project: str, name: str, version: int | str) -> dict:
        """The record of a running version, by number, `v<n>`, or uid (StoreError if it is final)."""
        state = self._lookup(connection, project, name, version)
        if state["status"] != "running":
            raise StoreError(f"{refs.exact(project, name, state['version'])} is {state['status']} and can no longer change")
        return state

    def _exact_inputs(self, connection, inputs) -> list[str]:
        """Exact `project:name@v<n>` strings for input references; each must resolve to a completed version, or, named
        by its exact uid, to a running one (the run a job's outputs come from, which completes after them). Unknown,
        failed, and deleted inputs are refused."""
        exact: list[str] = []
        for item in inputs or []:
            ref = refs.parse(item) if isinstance(item, str) else item
            if ref.uid:
                project = ref.project or self.project
                found = self._by_uid(connection, ref.uid) if project else None
                if found is not None and found[:2] == (project, ref.name) and found[2]["status"] == "running":
                    text = refs.exact(project, ref.name, found[2]["version"])
                    if text not in exact:
                        exact.append(text)
                    continue
            project, name, version = self._resolve(connection, ref)
            text = refs.exact(project, name, version["version"])
            if text not in exact:
                exact.append(text)
        return exact

    def _new(self, connection, project, name, label, metadata, source, uid=None) -> dict:
        """A new running version's record, numbered next (unique: this runs inside a write transaction)."""
        if label is not None:
            refs.valid_label(label)
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict):
            raise StoreError("metadata must be a dict")
        json.dumps(metadata)
        uid = new_uid() if uid is None else valid_uid(uid)
        other = self._by_uid(connection, uid)
        if other is not None:
            raise Conflict(f"uid {uid} already names {refs.exact(other[0], other[1], other[2]['version'])}")
        number = connection.execute("SELECT coalesce(max(number), 0) + 1 FROM versions WHERE project = ? AND name = ?",
                                    (project, name)).fetchone()[0]
        return {"version": number, "uid": uid, "label": label, "status": "running", "created": now(), "files": [],
                "metadata": metadata, "inputs": [], "source": source}

    def _merged(self, state: dict, entries) -> dict:
        clean = clean_entries(entries)
        missing = self.missing(clean)  # inside the write transaction: a sweep cannot remove them meanwhile
        if missing:
            raise MissingBlobs(missing)
        merged = {entry["path"]: entry for entry in state["files"]}
        merged.update({entry["path"]: entry for entry in clean})
        files = sorted(merged.values(), key=lambda entry: entry["path"])
        _check_tree(files)
        return {**state, "files": files}

    def _completed(self, connection, project, name, version, exact) -> None:
        """After a version completed: `latest` moves to it, and the versions it consumed learn of it."""
        self._move_alias(connection, project, name, "latest", version)
        me = refs.exact(project, name, version)
        for text in exact:
            connection.execute("INSERT OR IGNORE INTO consumers (input, consumer, time) VALUES (?, ?, ?)", (text, me, now()))

    def _move_alias(self, connection, project, name, alias, version) -> None:
        row = connection.execute("SELECT number FROM aliases WHERE project = ? AND name = ? AND alias = ?", (project, name, alias)).fetchone()
        if row is not None and row[0] == version:
            return
        cursor = connection.execute("INSERT INTO alias_log (project, name, alias, number, time) VALUES (?, ?, ?, ?, ?)",
                                    (project, name, alias, version, now()))
        connection.execute("INSERT INTO aliases VALUES (?, ?, ?, ?, ?) ON CONFLICT (project, name, alias) DO UPDATE SET number = excluded.number",
                           (project, name, alias, version, cursor.lastrowid))
        aliases = json.dumps(self._aliases(connection, project, name))
        if alias == "latest":
            head = self._state(connection, project, name, version)
            connection.execute("UPDATE artifacts SET latest = ?, metadata = ?, aliases = ? WHERE project = ? AND name = ?",
                               (version, json.dumps(head["metadata"]), aliases, project, name))
        else:
            connection.execute("UPDATE artifacts SET aliases = ? WHERE project = ? AND name = ?", (aliases, project, name))
        connection.execute("UPDATE projects SET generation = generation + 1 WHERE name = ?", (project,))

    def start_version(self, project: str, name: str, label: str | None = None, metadata: dict | None = None,
                      source: dict | None = None, uid: str | None = None) -> int:
        """Start a running version; returns its number. With `uid` (made by the client), starting the same version
        again returns its number; a uid this artifact already has for a different label or metadata is a Conflict."""
        with self._write() as connection:
            self._check(connection, project, name)
            if uid is not None:
                known = self._by_uid(connection, valid_uid(uid))
                if known is not None and known[:2] == (project, name):
                    state = known[2]
                    if state["status"] == "deleted":
                        raise Conflict(f"uid {uid} named {project}:{name}@v{state['version']}, which was deleted: a uid is never used again")
                    if state["label"] != label or state["metadata"] != (metadata or {}):
                        raise Conflict(f"uid {uid} already names {project}:{name}@v{state['version']} with another label or metadata")
                    return state["version"]
            state = self._new(connection, project, name, label, metadata, source, uid)
            self._save(connection, project, name, state, created=True)
            return state["version"]

    def add_files(self, project: str, name: str, version: int | str, entries: list[dict]) -> None:
        """Add or replace files in a running version (by number or uid; entries as in `blobs.clean_entries`, blobs
        already stored)."""
        with self._write() as connection:
            self._check(connection, project, name)
            self._save(connection, project, name, self._merged(self._running(connection, project, name, version), entries))

    def set_metadata(self, project: str, name: str, version: int | str, metadata: dict) -> None:
        """Replace the metadata of a running version (a completed one never changes)."""
        if not isinstance(metadata, dict):
            raise StoreError("metadata must be a dict")
        json.dumps(metadata)
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._running(connection, project, name, version)
            self._save(connection, project, name, {**state, "metadata": metadata})

    def complete_version(self, project: str, name: str, version: int | str, inputs=None, digest: str | None = None) -> None:
        """Freeze a version (by number or uid) and move `latest` to it; `inputs` are the references it consumed. The
        version's manifest digest is computed from its files; a `digest` the client supplies must equal it (else
        Conflict, and nothing changes)."""
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._running(connection, project, name, version)
            exact = self._exact_inputs(connection, inputs)
            actual = manifest_digest(state["files"])
            _check_digest(digest, actual, refs.exact(project, name, state["version"]))
            self._save(connection, project, name, {**state, "status": "completed", "completed": now(), "inputs": exact, "digest": actual})
            self._completed(connection, project, name, state["version"], exact)
        self._clear_beat(state["uid"])

    def fail_version(self, project: str, name: str, version: int | str, error: str | None = None) -> None:
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._running(connection, project, name, version)
            self._save(connection, project, name, {**state, "status": "failed", "completed": now(),
                                                   **({"error": error} if error else {})})
        self._clear_beat(state["uid"])

    def publish(self, project: str, name: str, entries: list[dict], label: str | None = None, metadata: dict | None = None,
                inputs=None, aliases=(), source: dict | None = None, uid: str | None = None,
                digest: str | None = None) -> int:
        """Start, fill, and complete a version in one step, in one transaction (all of it is stored, or none).

        When the latest completed version already has these files, inputs, and metadata, no version
        is created and its number is returned (the aliases are still applied). With `uid`, publishing
        the same version again returns its number; a known uid with other content is a Conflict. A `digest` the
        client supplies must equal the manifest digest of the entries (digest.py), else Conflict.
        """
        clean = sorted(clean_entries(entries), key=lambda entry: entry["path"])
        _check_tree(clean)
        sweeps = self._sweeps()
        missing = self.missing(clean)
        if missing:
            raise MissingBlobs(missing)
        for alias in aliases:
            refs.valid_word(alias, "alias")
        actual = manifest_digest(clean)
        _check_digest(digest, actual, f"{project}:{name}")
        with self._write() as connection:
            self._check(connection, project, name)
            if self._sweeps(connection) != sweeps:  # a blob sweep ran since the check: check again, inside the transaction
                missing = self.missing(clean)
                if missing:
                    raise MissingBlobs(missing)
            exact = self._exact_inputs(connection, inputs)
            known = self._by_uid(connection, valid_uid(uid)) if uid is not None else None
            head = connection.execute("SELECT number FROM aliases WHERE project = ? AND name = ? AND alias = 'latest'",
                                      (project, name)).fetchone()
            state = self._state(connection, project, name, head[0]) if head else None
            if known is not None:
                if known[2]["status"] == "deleted":
                    raise Conflict(f"uid {uid} named {refs.exact(known[0], known[1], known[2]['version'])}, which was deleted: "
                                   "a uid is never used again")
                if known[:2] != (project, name) or known[2]["status"] != "completed" or not _same(known[2], actual, exact, metadata or {}):
                    raise Conflict(f"uid {uid} already names {refs.exact(known[0], known[1], known[2]['version'])} with other content")
                version = known[2]["version"]
            elif state is not None and _same(state, actual, exact, metadata or {}):
                version = state["version"]
            else:
                record = self._new(connection, project, name, label, metadata, source, uid)
                record = {**record, "files": clean, "status": "completed", "completed": now(), "inputs": exact,
                          "digest": actual}
                self._save(connection, project, name, record, created=True)
                version = record["version"]
                self._completed(connection, project, name, version, exact)
            for alias in aliases:
                self._set_alias(connection, project, name, alias, version)
        return version

    def _set_alias(self, connection, project, name, alias, version) -> None:
        refs.valid_word(alias, "alias")
        state = self._state(connection, project, name, version)
        if state is None or state["status"] != "completed":
            raise StoreError(f"alias {alias!r}: {project}:{name}@v{version} is not a completed version")
        self._move_alias(connection, project, name, alias, version)

    def set_alias(self, project: str, name: str, alias: str, version: int) -> None:
        """Point `alias` at a completed version. `latest` moves by itself and cannot be set."""
        if alias == "latest":
            raise StoreError("'latest' moves automatically when a version completes")
        with self._write() as connection:
            self._check(connection, project, name)
            self._set_alias(connection, project, name, alias, version)


    # ------------------------------------------------------------------ copies of a remote's versions

    def meta(self, key: str) -> str | None:
        """A value of the database's meta table (e.g. `remote`: the uid of the remote this repo syncs with)."""
        with self._db.read() as connection:
            return Database.meta(connection, key)

    def set_meta(self, key: str, value: str) -> None:
        with self._write() as connection:
            connection.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def _evict(self, connection, project: str, name: str, number: int, uid: str) -> None:
        """Make room for the remote's version `number` (uid `uid`): another row holding that number is a stale copy,
        dropped. A version this repo numbered itself and has not sent yet is never dropped (Conflict: it is adopted
        first, see client/sync.py)."""
        state = self._state(connection, project, name, number)
        if state is None or state["uid"] == uid:
            return
        if not state.get("origin"):
            raise Conflict(f"{refs.exact(project, name, number)} in the repo {self.root} is a version of this repo that the remote "
                           "does not have yet; it is sent first")
        self._drop_rows(connection, [(project, name, state)])

    def _drop_rows(self, connection, rows: list[tuple[str, str, dict]]) -> None:
        """Forget versions (not tombstones: they are gone from this repo), with their aliases and the edges they recorded."""
        touched = set()
        for project, name, state in rows:
            me = refs.exact(project, name, state["version"])
            connection.execute("DELETE FROM versions WHERE uid = ?", (state["uid"],))
            if state["status"] != "deleted":
                connection.execute("UPDATE artifacts SET versions = versions - 1 WHERE project = ? AND name = ?", (project, name))
            connection.execute("DELETE FROM consumers WHERE consumer = ?", (me,))
            for alias, number in self._aliases(connection, project, name).items():
                if number == state["version"]:
                    self._remove_alias(connection, project, name, alias)
            touched.add((project, name))
        for project, name in touched:
            self._fix_latest(connection, project, name)
            connection.execute("UPDATE projects SET generation = generation + 1 WHERE name = ?", (project,))

    def _fix_latest(self, connection, project: str, name: str) -> None:
        """`latest` names a completed version: the one it named, else the newest completed one left."""
        row = connection.execute("SELECT number FROM aliases WHERE project = ? AND name = ? AND alias = 'latest'", (project, name)).fetchone()
        state = self._state(connection, project, name, row[0]) if row else None
        if state is not None and state["status"] == "completed":
            return
        newest = connection.execute("SELECT max(number) FROM versions WHERE project = ? AND name = ? AND status = 'completed'",
                                    (project, name)).fetchone()[0]
        if newest is not None:
            self._move_alias(connection, project, name, "latest", newest)
        elif row is not None:
            self._remove_alias(connection, project, name, "latest")

    def put_copy(self, project: str, name: str, manifest: dict, origin: str) -> bool:
        """Record a version as the remote `origin` (its uid) answered it: a copy, numbered by the remote. Only final
        versions are copied (completed, failed, deleted); a copy that is already here as given is left alone. A
        tombstone replaces a copy. `latest` moves to a completed copy newer than the one it names (`mirror_aliases`
        sets it exactly). Returns whether anything changed. One transaction."""
        number, uid, status = manifest.get("version"), manifest.get("uid"), manifest.get("status")
        if isinstance(number, bool) or not isinstance(number, int) or not isinstance(uid, str) or \
                status not in ("completed", "failed", "deleted"):
            return False
        valid_uid(uid)
        refs.valid_project(project)
        refs.valid_name(name)
        record = {key: value for key, value in manifest.items() if key not in ("pending", "sync", "aliases", "copied")}
        record["origin"] = origin
        with self._write() as connection:
            if connection.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone() is None:
                connection.execute("INSERT INTO projects (name, record) VALUES (?, ?)",
                                   (project, json.dumps({"name": project, "title": project, "description": "", "created": now()})))
            known = self._by_uid(connection, uid)
            if known is not None and known[:2] != (project, name):
                raise Conflict(f"uid {uid} names {refs.exact(known[0], known[1], known[2]['version'])} in this repo, "
                               f"not a version of {project}:{name}")
            if known is not None:
                state = known[2]
                if {k: v for k, v in state.items() if k != "copied"} == record:
                    return False
                if state["version"] != number:
                    self._evict(connection, project, name, number, uid)
                    connection.execute("UPDATE versions SET number = ? WHERE uid = ?", (number, uid))
                record["copied"] = state.get("copied") or now()
                self._save(connection, project, name, record)
                if state["status"] != "deleted" and status == "deleted":
                    connection.execute("UPDATE artifacts SET versions = versions - 1 WHERE project = ? AND name = ?", (project, name))
            else:
                self._evict(connection, project, name, number, uid)
                record["copied"] = now()
                self._save(connection, project, name, record, created=True)
                if status == "deleted":
                    connection.execute("UPDATE artifacts SET versions = versions - 1 WHERE project = ? AND name = ?", (project, name))
            me = refs.exact(project, name, number)
            for text in record.get("inputs") or []:
                connection.execute("INSERT OR IGNORE INTO consumers (input, consumer, time) VALUES (?, ?, ?)", (text, me, now()))
            if status == "completed":
                head = connection.execute("SELECT number FROM aliases WHERE project = ? AND name = ? AND alias = 'latest'",
                                          (project, name)).fetchone()
                if head is None or head[0] < number:
                    self._move_alias(connection, project, name, "latest", number)
            elif status == "deleted":
                for alias, target in self._aliases(connection, project, name).items():
                    if target == number and alias != "latest":
                        self._remove_alias(connection, project, name, alias)
                self._fix_latest(connection, project, name)
        return True

    def mirror_aliases(self, project: str, name: str, aliases: dict[str, int]) -> None:
        """Make the artifact's aliases here what the remote says they are (`{alias: number}`, every alias it has): an
        alias whose version is not copied here, or not completed, is left out. One transaction."""
        with self._write() as connection:
            if connection.execute("SELECT 1 FROM artifacts WHERE project = ? AND name = ?", (project, name)).fetchone() is None:
                return
            current = self._aliases(connection, project, name)
            for alias, number in aliases.items():
                state = self._state(connection, project, name, number) if isinstance(number, int) else None
                if state is not None and state["status"] == "completed" and current.get(alias) != number:
                    self._move_alias(connection, project, name, alias, number)
            for alias in current:
                if alias not in aliases:
                    self._remove_alias(connection, project, name, alias)
            self._fix_latest(connection, project, name)

    def local_versions(self) -> list[tuple[str, str, dict]]:
        """(project, name, record) of every version this repo numbered itself (no `origin`: not a remote's copy),
        oldest first (by uid, which is creation order)."""
        with self._db.read() as connection:
            rows = [(row[0], row[1], json.loads(row[2])) for row in connection.execute(
                "SELECT project, name, manifest FROM versions WHERE json_extract(manifest, '$.origin') IS NULL ORDER BY uid")]
        return rows

    def forget_versions(self, uids) -> int:
        """Drop versions from this repo (not tombstones: they are gone from here, e.g. copies `cairn prune` trims, or
        versions handed to the outbox), with their aliases and the edges they recorded. Returns how many. One transaction."""
        wanted = set(uids)
        if not wanted:
            return 0
        with self._write() as connection:
            rows = [found for uid in sorted(wanted) if (found := self._by_uid(connection, uid)) is not None]
            self._drop_rows(connection, rows)
            return len(rows)

    def copies(self) -> list[tuple[str, str, dict]]:
        """(project, name, record) of every copy of a remote's version in this repo."""
        with self._db.read() as connection:
            return [(row[0], row[1], json.loads(row[2])) for row in connection.execute(
                "SELECT project, name, manifest FROM versions WHERE json_extract(manifest, '$.origin') IS NOT NULL ORDER BY uid")]

    # ------------------------------------------------------------- resolving

    def _resolve(self, connection, ref: Ref | str) -> tuple[str, str, dict]:
        ref = refs.parse(ref) if isinstance(ref, str) else ref
        project = ref.project or self.project
        if project is None:
            raise StoreError(f"reference {ref} has no project and the store has no default")
        self._check(connection, project, ref.name)
        if connection.execute("SELECT 1 FROM artifacts WHERE project = ? AND name = ?", (project, ref.name)).fetchone() is None:
            raise NotFound(f"no artifact {project}:{ref.name}")
        if ref.exact:
            if ref.version is not None:
                state = self._state(connection, project, ref.name, ref.version)
            else:
                found = self._by_uid(connection, ref.uid)
                state = found[2] if found and found[:2] == (project, ref.name) else None
            if state is None:
                raise NotFound(f"{project}:{ref.name} has no version {ref.selector}")
            if state["status"] == "deleted":
                raise self._gone(project, ref.name, state)
            if state["status"] != "completed":
                raise StoreError(f"{project}:{ref.name}@{ref.selector} is {state['status']}, not completed")
            return project, ref.name, state
        selector = ref.selector or "latest"
        row = connection.execute("SELECT number FROM aliases WHERE project = ? AND name = ? AND alias = ?",
                                 (project, ref.name, selector)).fetchone()
        if row is None:
            row = connection.execute("SELECT max(number) FROM versions WHERE project = ? AND name = ? AND label = ? AND status = 'completed'",
                                     (project, ref.name, selector)).fetchone()
        if row is None or row[0] is None:
            raise NotFound(f"{project}:{ref.name}@{selector} matches no completed version")
        return project, ref.name, self._state(connection, project, ref.name, row[0])

    def resolve(self, ref: Ref | str) -> tuple[str, str, dict]:
        """(project, name, version record) for a reference; no selector means `latest`."""
        with self._db.read() as connection:
            return self._resolve(connection, ref)

    def lineage(self, ref: Ref | str, marks: bool = False) -> dict[str, list[str]]:
        """The versions this one consumed and the versions that consumed it, as exact references. Deleting a version
        keeps its edges: an exact reference to a deleted version answers too. With `marks`, `deleted` lists the
        references among them (and the version itself) that were deleted."""
        ref = refs.parse(ref) if isinstance(ref, str) else ref
        with self._db.read() as connection:
            if ref.exact:  # an exact version of any status: a running run already has consumers
                project, name = ref.project or self.project, ref.name
                self._check(connection, project, name)
                version = self._lookup(connection, project, name, ref.selector)
            else:
                project, name, version = self._resolve(connection, ref)
            me = refs.exact(project, name, version["version"])
            consumers = [row[0] for row in connection.execute("SELECT consumer FROM consumers WHERE input = ? ORDER BY seq", (me,))]
            found = {"inputs": list(version["inputs"]), "consumers": consumers}
            if marks:
                found["deleted"] = [item for item in [me, *found["inputs"], *consumers] if self._tombstoned(connection, item)]
                found["nodes"] = self._node_marks(connection, [me, *found["inputs"], *consumers])
        return found

    def _node_marks(self, connection, items: list[str]) -> dict[str, dict]:
        """{ref: {"status", "kind"}} for lineage references (status of the version, kind from its metadata, e.g. "run")."""
        marks: dict[str, dict] = {}
        for item in items:
            if item in marks:
                continue
            try:
                ref = refs.parse(item)
                state = self._lookup(connection, ref.project or self.project, ref.name, ref.selector)
            except (StoreError, ValueError):
                continue
            mark = {"status": state["status"]}
            kind = (state.get("metadata") or {}).get("kind")
            if isinstance(kind, str):
                mark["kind"] = kind
            marks[item] = mark
        return marks

    # ------------------------------------------------------------- deleting (tombstones)

    @staticmethod
    def _gone(project: str, name: str, state: dict) -> Deleted:
        when = (state.get("deleted_at") or "")[:19]
        by = f" by {state['deleted_by']}" if state.get("deleted_by") else ""
        why = f": {state['reason']}" if state.get("reason") else ""
        return Deleted(f"{refs.exact(project, name, state['version'])} was deleted ({when}{by}){why}")

    def _lookup(self, connection, project: str, name: str, version: int | str | None) -> dict:
        """A version's record by number, `v<n>`, or uid, whatever its status (NotFound if there is none)."""
        if isinstance(version, str) and refs.VERSION.fullmatch(version):
            version = int(version[1:])
        if isinstance(version, bool) or not isinstance(version, (int, str)):
            raise ValueError(f"a version is a number, 'v<n>', or a uid, not {version!r}")
        if isinstance(version, int):
            state = self._state(connection, project, name, version)
        else:
            found = self._by_uid(connection, valid_uid(version))
            state = found[2] if found and found[:2] == (project, name) else None
        if state is None:
            raise NotFound(f"{project}:{name} has no version {version if isinstance(version, str) else f'v{version}'}")
        return state

    @staticmethod
    def _row_status(connection, ref: str) -> str | None:
        """The status of the version an exact `project:name@v<n>` names (None: no such version)."""
        try:
            parsed = refs.parse(ref)
        except ValueError:
            return None
        if parsed.project is None or parsed.version is None:
            return None
        row = connection.execute("SELECT status FROM versions WHERE project = ? AND name = ? AND number = ?",
                                 (parsed.project, parsed.name, parsed.version)).fetchone()
        return row[0] if row else None

    def _tombstoned(self, connection, ref: str) -> bool:
        return self._row_status(connection, ref) == "deleted"

    def _protection(self, connection, project: str, name: str, state: dict, aliases: dict[str, int], deleting: set[str]) -> list[str]:
        """Why a version may not be deleted without force: it is running, an alias other than `latest` points at it,
        or it is an input of a version that is not deleted (in any project; `deleting` are going too)."""
        reasons = ["running"] if state["status"] == "running" else []
        reasons += [f"alias {alias}" for alias, number in aliases.items() if number == state["version"] and alias != "latest"]
        me = refs.exact(project, name, state["version"])
        for (consumer,) in connection.execute("SELECT consumer FROM consumers WHERE input = ? ORDER BY seq", (me,)):
            status = self._row_status(connection, consumer)
            if consumer not in deleting and status not in (None, "deleted"):
                reasons.append(f"input of {consumer}")
        return reasons

    def _remove_alias(self, connection, project: str, name: str, alias: str) -> None:
        """Drop an alias (the log records the removal as a move to version 0)."""
        connection.execute("DELETE FROM aliases WHERE project = ? AND name = ? AND alias = ?", (project, name, alias))
        connection.execute("INSERT INTO alias_log (project, name, alias, number, time) VALUES (?, ?, ?, 0, ?)", (project, name, alias, now()))
        aliases = json.dumps(self._aliases(connection, project, name))
        if alias == "latest":
            connection.execute("UPDATE artifacts SET latest = NULL, metadata = '{}', aliases = ? WHERE project = ? AND name = ?",
                               (aliases, project, name))
        else:
            connection.execute("UPDATE artifacts SET aliases = ? WHERE project = ? AND name = ?", (aliases, project, name))

    def _tombstone(self, connection, targets: list[tuple[str, str, dict]], *, reason: str | None, force: bool, by: str,
                   skip: bool = False, apply: bool = True) -> dict:
        """Delete versions (project, name, record) in this transaction: each becomes a tombstone (status `deleted`,
        `deleted_at`, `deleted_by`, `reason`, its file list dropped; number, uid, label, metadata, and inputs kept).
        Without `force`, protected versions refuse the whole call (Protected), or with `skip` are left out and
        reported. Aliases that pointed at a deleted version are removed; `latest` moves back to the newest completed
        version left. `apply=False` only says what would happen. Returns {deleted: [refs], skipped: [{ref, reasons}]}."""
        aliases_of: dict[tuple[str, str], dict[str, int]] = {}
        chosen, protected = list(targets), []
        while True:  # leaving a protected version out can protect its inputs: repeat until nothing changes
            deleting = {refs.exact(project, name, state["version"]) for project, name, state in chosen}
            keep, kept = [], []
            for project, name, state in chosen:
                aliases = aliases_of.setdefault((project, name), self._aliases(connection, project, name))
                reasons = self._protection(connection, project, name, state, aliases, deleting)
                if reasons and not force:
                    kept.append({"ref": refs.exact(project, name, state["version"]), "reasons": reasons})
                else:
                    keep.append((project, name, state))
            protected += kept
            if not kept:
                break
            chosen = keep
        if protected and not skip:
            listed = "; ".join(f"{item['ref']} ({', '.join(item['reasons'])})" for item in protected[:5])
            more = f" and {len(protected) - 5} more" if len(protected) > 5 else ""
            raise Protected(f"refused: protected versions {listed}{more}; delete with force to remove them anyway", protected)
        stamp, touched = now(), {}
        for project, name, state in chosen:
            if not apply:
                continue
            files = state.get("files") or []
            tomb = {key: state[key] for key in ("version", "uid", "label", "created", "completed", "metadata", "inputs", "source")
                    if key in state}
            tomb.update(status="deleted", was=state["status"], files=[], file_count=sum("sha256" in entry for entry in files),
                        size=sum(entry.get("size") or 0 for entry in files), deleted_at=stamp, deleted_by=by, reason=reason)
            connection.execute("UPDATE versions SET status = 'deleted', manifest = ? WHERE project = ? AND name = ? AND number = ?",
                               (json.dumps(tomb), project, name, state["version"]))
            connection.execute("UPDATE artifacts SET versions = versions - 1 WHERE project = ? AND name = ?", (project, name))
            for alias, number in list(aliases_of[(project, name)].items()):
                if number == state["version"] and alias != "latest":
                    self._remove_alias(connection, project, name, alias)
            touched[(project, name)] = True
        for project, name in touched:
            row = connection.execute("SELECT number FROM aliases WHERE project = ? AND name = ? AND alias = 'latest'", (project, name)).fetchone()
            if row is None or self._state(connection, project, name, row[0])["status"] == "deleted":
                newest = connection.execute("SELECT max(number) FROM versions WHERE project = ? AND name = ? AND status = 'completed'",
                                            (project, name)).fetchone()[0]
                if newest is not None:
                    self._move_alias(connection, project, name, "latest", newest)
                elif row is not None:
                    self._remove_alias(connection, project, name, "latest")
            connection.execute("UPDATE projects SET generation = generation + 1 WHERE name = ?", (project,))
        return {"deleted": [refs.exact(project, name, state["version"]) for project, name, state in chosen], "skipped": protected}

    @staticmethod
    def _reason(reason) -> str | None:
        if reason is not None and (not isinstance(reason, str) or len(reason) > 1000):
            raise ValueError("a reason is text of at most 1000 characters")
        return reason or None

    def delete_version(self, project: str, name: str, version: int | str, *, reason: str | None = None, force: bool = False,
                       by: str | None = None) -> dict:
        """Delete one version (by number, `v<n>`, or uid): it becomes a tombstone (see `_tombstone`) and its blobs are
        garbage once no other version names them (`gc`). Protected versions (an alias other than `latest` points at
        it, it is an input of a version that is not deleted in any project, or it is running) are refused with
        Protected unless `force`. Returns {deleted: [ref], skipped: []}."""
        reason = self._reason(reason)
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._lookup(connection, project, name, version)
            if state["status"] == "deleted":
                raise self._gone(project, name, state)
            return self._tombstone(connection, [(project, name, state)], reason=reason, force=force, by=by or _who())

    def delete_artifact(self, project: str, name: str, *, reason: str | None = None, force: bool = False, by: str | None = None) -> dict:
        """Delete every version of an artifact under the rules of `delete_version` (all of them, or none)."""
        reason = self._reason(reason)
        with self._write() as connection:
            self._check(connection, project, name)
            states = self._states(connection, project, name)
            if not states:
                raise NotFound(f"no artifact {project}:{name}")
            live = [(project, name, state) for state in states.values() if state["status"] != "deleted"]
            if not live:
                raise Deleted(f"every version of {project}:{name} was deleted already")
            return self._tombstone(connection, live, reason=reason, force=force, by=by or _who())

    def delete_project(self, project: str, *, reason: str | None = None, force: bool = False, by: str | None = None) -> dict:
        """Delete a project. One with versions that are not deleted is refused (Protected) unless `force`, which
        deletes them all first, protected or not. The tombstones stay, so numbers and uids are never used again."""
        reason = self._reason(reason)
        with self._write() as connection:
            self._project(connection, project)
            live = [(row[0], row[1], json.loads(row[2])) for row in connection.execute(
                "SELECT project, name, manifest FROM versions WHERE project = ? AND status != 'deleted' ORDER BY name, number", (project,))]
            if live and not force:
                raise Protected(f"project {project!r} has {len(live)} versions that are not deleted: delete them first, or force")
            result = self._tombstone(connection, live, reason=reason, force=True, by=by or _who()) if live else {"deleted": [], "skipped": []}
            connection.execute("DELETE FROM projects WHERE name = ?", (project,))
            try:
                connection.execute("DELETE FROM retention WHERE project = ?", (project,))
            except sqlite3.OperationalError:  # no retention rules were ever set in this root
                pass
            self._listings.pop(project, None)
            return {**result, "project": project}

    # ------------------------------------------------------------- retention and garbage collection (gc.py)

    @staticmethod
    def _rules(connection, project: str | None = None) -> dict[str, list[dict]]:
        query, values = "SELECT project, rule FROM retention", ()
        if project is not None:
            query, values = query + " WHERE project = ?", (project,)
        found: dict[str, list[dict]] = {}
        try:
            for row in connection.execute(query + " ORDER BY project, prefix", values):
                found.setdefault(row[0], []).append(json.loads(row[1]))
        except sqlite3.OperationalError:  # a root from before retention rules: none set
            pass
        return found

    def retention(self, project: str) -> list[dict]:
        """A project's retention rules: [{prefix, keep_last, keep_days, keep_aliased}] (gc.py)."""
        with self._db.read() as connection:
            self._project(connection, project)
            return self._rules(connection, project).get(project, [])

    def set_retention(self, project: str, rules: list[dict]) -> list[dict]:
        """Replace a project's retention rules (an empty list removes them). They act only when `gc` runs."""
        clean = gc.clean_rules(rules)
        with self._write() as connection:
            self._project(connection, project)
            connection.execute(RETENTION_TABLE)  # roots made before retention rules lack the table
            connection.execute("DELETE FROM retention WHERE project = ?", (project,))
            for rule in clean:
                connection.execute("INSERT INTO retention VALUES (?, ?, ?)", (project, rule["prefix"], json.dumps(rule)))
        return clean

    def _sweeps(self, connection=None) -> str | None:
        """How many blob sweeps ever ran here (publishes check their blobs again when it changed)."""
        if connection is None:
            with self._db.read() as reading:
                return Database.meta(reading, "sweeps")
        return Database.meta(connection, "sweeps")

    @staticmethod
    def _referenced(connection, projects: list[str] | None, excluded: set[str]) -> set[str]:
        """The blobs named by versions that are not deleted (of `projects`, or all), leaving out `excluded` refs."""
        query, values = "SELECT project, name, number, manifest FROM versions WHERE status != 'deleted'", ()
        if projects is not None:
            query += f" AND project IN ({', '.join('?' for _ in projects)})"
            values = tuple(projects)
        found: set[str] = set()
        for project, name, number, manifest in connection.execute(query, values):
            if excluded and refs.exact(project, name, number) in excluded:
                continue
            found.update(entry["sha256"] for entry in json.loads(manifest)["files"] if "sha256" in entry)
        return found

    def gc(self, dry_run: bool = True, grace: float | str = gc.GRACE, *, by: str = "gc", now: float | None = None) -> dict:
        """Apply the retention rules, then remove the blobs no version names that are older than `grace` (seconds,
        or text like `24h`), and stale temporary uploads. `dry_run` (the default) changes nothing and says what would
        go. Safe beside concurrent publishes (gc.py says how). Returns {dry_run, grace, deleted: [refs],
        skipped: [{ref, reasons}], blobs, bytes, temporary, temporary_bytes}."""
        grace = gc.parse_duration(grace)
        clock = gc.now() if now is None else now
        report = {"dry_run": bool(dry_run), "grace": grace, "deleted": [], "skipped": [], "blobs": 0, "bytes": 0,
                  "temporary": 0, "temporary_bytes": 0}
        with self._db.read() as connection:
            rules = self._rules(connection)
        for project, project_rules in rules.items():  # one transaction per project
            with (self._db.read() if dry_run else self._write()) as connection:
                if connection.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone() is None:
                    continue
                targets = []
                for (name,) in connection.execute("SELECT name FROM artifacts WHERE project = ? ORDER BY name", (project,)).fetchall():
                    rule = gc.rule_for(project_rules, name)
                    if rule is None:
                        continue
                    states = self._states(connection, project, name)
                    for number in gc.expired(list(states.values()), self._aliases(connection, project, name), rule, clock):
                        targets.append((project, name, states[number]))
                if targets:
                    result = self._tombstone(connection, targets, reason="retention rule", force=False, by=by, skip=True,
                                             apply=not dry_run)
                    report["deleted"] += result["deleted"]
                    report["skipped"] += result["skipped"]
        cutoff = clock - grace
        with self._db.read() as connection:
            generations = dict(connection.execute("SELECT name, generation FROM projects").fetchall())
            referenced = self._referenced(connection, None, set(report["deleted"]) if dry_run else set())
        referenced |= outbox_blobs(self.root)  # versions on their way to the remote: never garbage
        found = gc.candidates(self._blobs.blobs_name, referenced, cutoff)
        temporaries = gc.stale_temporaries(self._blobs.tmp_name, clock - max(grace, gc.STALE_UPLOAD))  # never a live upload
        if dry_run:
            report.update(blobs=len(found), bytes=sum(size for _, _, size in found), temporary=len(temporaries),
                          temporary_bytes=sum(size for _, size in temporaries))
            return report
        with self._write() as connection:  # the sweep: serialized with every publish's own check of its blobs
            connection.execute("INSERT INTO meta VALUES ('sweeps', '1') ON CONFLICT (key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
            changed = [name for name, generation in connection.execute("SELECT name, generation FROM projects")
                       if generations.get(name) != generation]
            if changed:
                referenced |= self._referenced(connection, changed, set())
            referenced |= outbox_blobs(self.root)  # again: an entry may have been written since
            for sha, path, size in found:
                if sha in referenced:
                    continue
                try:
                    if os.stat(path).st_mtime >= cutoff:  # uploaded again meanwhile
                        continue
                    os.unlink(path)
                except FileNotFoundError:
                    continue
                report["blobs"] += 1
                report["bytes"] += size
        for path, size in temporaries:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            report["temporary"] += 1
            report["temporary_bytes"] += size
        return report



def _who() -> str:
    """Who deletes, for a tombstone (`deleted_by`) written by this process."""
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name: say so
        return "local"
