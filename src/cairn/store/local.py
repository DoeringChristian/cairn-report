"""LocalStore: projects, artifacts, and immutable versions in a folder.

    <root>/
      store.json                 the store's uid (UUIDv7), made once, never changed
      metadata.sqlite3           projects, versions, aliases and their log, consumers (db.py; WAL mode)
      blobs/<aa>/<sha256-rest>   file contents (blobs.BlobStore)
      server.lock                locked by the `cairn server` serving this root (db.ServerLock)

A version starts `running` (it may grow), then becomes `completed` or `failed`; a completed version
is never written again. Only completed versions resolve through `latest`, aliases, and labels, so
readers never see half-published data. Every change is one SQLite transaction: a publish (version,
files, completion, `latest`, aliases, consumers) commits whole or not at all, and version numbers are
handed out inside the transaction, so they are unique however many threads and processes publish.

Every version has a `uid` (a UUIDv7, see uids.py), made when it starts or supplied by the client
that created it (a client's local replica); a uid names one version for good, in the whole store. A root written
before the database existed (JSON Lines files) is imported when it is first opened (db.import_legacy);
versions stored without a uid get their deterministic one (`uids.legacy`), and the root gets its
store.json.

One writer process per root: while `cairn server` serves a root it locks `server.lock`, and other
processes may read the root but not change its metadata (they publish through the server's URL);
owner commands on the server's host (`cairn token create`) are the exception, and SQLite's locking
keeps their small writes safe. Without a server, several processes on one host may write a root
(SQLite serializes them). Never write a root over a network filesystem (fs.py).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..ranges import byte_range
from . import refs
from .blobs import CHUNK, BlobStore, BlobWriter, clean_entries, now, resolve_entry
from .db import Database, ServerLock, export
from .digest import manifest_digest
from .errors import BadRange, Conflict, Forbidden, NotFound, StoreError, Unauthorized  # noqa: F401
from .fs import network_filesystem
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


def store_uid(root: Path) -> str:
    """The uid in `<root>/store.json`, made on first use. Two processes racing both end with the one that won
    (the file is linked into place, which fails if it exists), so a store's uid never changes."""
    path = root / "store.json"
    for _ in range(3):
        try:
            return valid_uid(json.loads(path.read_text())["uid"])
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            raise StoreError(f"{path} is damaged: it must hold {{\"uid\": <UUIDv7>}}")
        temporary = root / f"store.json.{secrets.token_hex(6)}.tmp"
        temporary.write_text(json.dumps({"uid": new_uid(), "created": now()}, indent=1))
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError:  # a filesystem without hard links: rename, accepting a tiny race on first use
            if not path.exists():
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return valid_uid(json.loads(path.read_text())["uid"])


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


_WARNED: set[str] = set()
_SUMMARY = "SELECT name, latest, versions, aliases, updated, metadata FROM artifacts"


class LocalStore:
    def __init__(self, root: Path | str, project: str | None = None, network_warning: bool = True, served: bool = False):
        """`project` is the default for references without one. A store root on a network filesystem warns once
        per process when it is first written (pass `network_warning=False` when the caller has decided).
        `served`: this process may change the metadata while a server holds the root (the server itself, and
        owner commands on the server's host; see the module doc)."""
        self.root, self.project = Path(root), project
        self._blobs = BlobStore(self.root)  # creates blobs/, projects/, tmp/
        self._network_checked = not network_warning
        self._served = served
        self.uid = store_uid(self.root)
        self._db = Database(self.root, self.uid)
        self._add_digests()
        self._listings: dict[str, tuple[int, list[dict]]] = {}

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

    def _writing(self) -> None:
        """Warn once if this root lies on a network filesystem (several writers there can corrupt it, see fs.py)."""
        if self._network_checked:
            return
        self._network_checked = True
        key = str(self.root.resolve())
        if key in _WARNED:
            return
        _WARNED.add(key)
        fstype = network_filesystem(self.root)
        if fstype:
            print(f"warning: the store folder {self.root} is on a network filesystem ({fstype}); writes from several "
                  "processes or hosts can corrupt it. Run `cairn server` on one machine and point clients at its URL.",
                  file=sys.stderr, flush=True)

    def _write(self):
        """A metadata write transaction (db.Database.write); refused while another process serves this root."""
        self._writing()
        if not self._served and ServerLock.held(self.root):
            raise StoreError(f"{self.root} is served by `cairn server`: change it through the server's URL "
                             "(a second writer beside the server is refused)")
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

    def blob_file(self, project: str, name: str, version: int, entry: dict, cache_root=None) -> Path:
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

    def put_file(self, sha: str, source: Path | str) -> bool:
        """Store a local file as a blob: a copy-on-write clone where the filesystem has them, else a copy (verified)."""
        self._writing()
        return self._blobs.put_file(sha, Path(source))

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
                      connection.execute("SELECT project, count(*), max(updated) FROM artifacts GROUP BY project")}
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
            return [row[0] for row in connection.execute("SELECT name FROM artifacts WHERE project = ? ORDER BY name", (project,))]

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
                rows = connection.execute(f"{_SUMMARY} WHERE project = ? ORDER BY name", (project,)).fetchall()
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

    def _running(self, connection, project: str, name: str, version: int) -> dict:
        state = self._state(connection, project, name, version)
        if state is None:
            raise NotFound(f"{project}:{name}@v{version} does not exist")
        if state["status"] != "running":
            raise StoreError(f"{project}:{name}@v{version} is {state['status']} and can no longer change")
        return state

    def _exact_inputs(self, connection, inputs) -> list[str]:
        """Exact `project:name@v<n>` strings for input references; each must resolve to a completed version."""
        exact: list[str] = []
        for item in inputs or []:
            project, name, version = self._resolve(connection, item)
            text = refs.exact(project, name, version["version"])
            if text not in exact:
                exact.append(text)
        return exact

    def _new(self, connection, project, name, label, metadata, source, uid=None) -> dict:
        """A new running version's record, numbered next (unique: this runs inside a write transaction)."""
        if label is not None:
            refs.valid_word(label, "label")
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
        missing = self.missing(clean)
        if missing:
            raise StoreError(f"{len(missing)} blobs are not in the store yet, e.g. {missing[0]}")
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
                    if state["label"] != label or state["metadata"] != (metadata or {}):
                        raise Conflict(f"uid {uid} already names {project}:{name}@v{state['version']} with another label or metadata")
                    return state["version"]
            state = self._new(connection, project, name, label, metadata, source, uid)
            self._save(connection, project, name, state, created=True)
            return state["version"]

    def add_files(self, project: str, name: str, version: int, entries: list[dict]) -> None:
        """Add or replace files in a running version (entries as in `blobs.clean_entries`, blobs already uploaded)."""
        with self._write() as connection:
            self._check(connection, project, name)
            self._save(connection, project, name, self._merged(self._running(connection, project, name, version), entries))

    def complete_version(self, project: str, name: str, version: int, inputs=None, digest: str | None = None) -> None:
        """Freeze a version and move `latest` to it; `inputs` are the references it consumed. The version's manifest
        digest is computed from its files; a `digest` the client supplies must equal it (else Conflict, and nothing changes)."""
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._running(connection, project, name, version)
            exact = self._exact_inputs(connection, inputs)
            actual = manifest_digest(state["files"])
            _check_digest(digest, actual, f"{project}:{name}@v{version}")
            self._save(connection, project, name, {**state, "status": "completed", "completed": now(), "inputs": exact, "digest": actual})
            self._completed(connection, project, name, version, exact)

    def fail_version(self, project: str, name: str, version: int) -> None:
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._running(connection, project, name, version)
            self._save(connection, project, name, {**state, "status": "failed", "completed": now()})

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
        missing = self.missing(clean)  # blobs are never removed, so checking before the transaction is safe
        if missing:
            raise StoreError(f"{len(missing)} blobs are not in the store yet, e.g. {missing[0]}")
        for alias in aliases:
            refs.valid_word(alias, "alias")
        actual = manifest_digest(clean)
        _check_digest(digest, actual, f"{project}:{name}")
        with self._write() as connection:
            self._check(connection, project, name)
            exact = self._exact_inputs(connection, inputs)
            known = self._by_uid(connection, valid_uid(uid)) if uid is not None else None
            head = connection.execute("SELECT number FROM aliases WHERE project = ? AND name = ? AND alias = 'latest'",
                                      (project, name)).fetchone()
            state = self._state(connection, project, name, head[0]) if head else None
            if known is not None:
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

    def lineage(self, ref: Ref | str) -> dict[str, list[str]]:
        """The versions this one consumed and the versions that consumed it, as exact references."""
        with self._db.read() as connection:
            project, name, version = self._resolve(connection, ref)
            me = refs.exact(project, name, version["version"])
            consumers = [row[0] for row in connection.execute("SELECT consumer FROM consumers WHERE input = ? ORDER BY seq", (me,))]
        return {"inputs": list(version["inputs"]), "consumers": consumers}
