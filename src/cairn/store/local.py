"""LocalRepo: a repo folder on this machine (the project's `.cairn/`, or a server's folder), its metadata and blobs.

    <repo>/
      repo.json                  the repo's id (ids.py, made once, never changed), its layout, and its remote's id
      metadata.sqlite3           projects, versions, aliases and their log, consumers (db.py; WAL mode)
      blobs/<aa>/<sha256>        file contents (blobs.BlobStore), shared by all versions and the outbox
      outbox/<id>/               versions on their way to the remote (store/outbox.py); their blobs are in blobs/
      versions/<id>/files/       read-only folders `pull` returns (folders.py)
      drafts/  build/  share/    folders that become versions, report builds, shared pages
      config.toml                the project's settings (config.py)
      token                      the owner token of `cairn server`, or a client's token for the remote (tokens.py)
      heartbeats/<id>            a running version's owner {time, host, pid}, rewritten while it runs (see `reap`)
      server.lock                locked by the `cairn server` serving this repo (db.ServerLock)
      tmp/                       uploads in progress

A version starts `running` (it may grow), then becomes `completed` or `failed`; a completed version
is never written again, except to delete it. Only completed versions resolve through `latest`, aliases, and labels, so
readers never see half-published data. Every change is one SQLite transaction: a publish (version,
files, completion, `latest`, aliases, consumers) commits whole or not at all, and version numbers are
handed out inside the transaction, so they are unique and contiguous however many threads and processes publish.

Without a remote the repo is the source of truth and numbers its versions. With one, the repo is the remote's
cache: it holds copies of the versions the remote numbered (`put_copy`, `origin` = the remote's id, with their
aliases mirrored by `mirror_aliases`), and the versions on their way there wait in the outbox, not here. Versions a
repo numbered itself before it had a remote are sent once (client/sync.py adopts them: `local_versions`,
`forget_versions`) and come back as copies with the remote's numbers.

Every version has an `id` (64 hex digits, ids.py), made when it starts or supplied by the client that created it; an
id names one version for good, in the whole repo and on every remote, and it is the only identity a version has: the
inputs a version records and the consumers table name versions by id. A short id is a prefix of the id: the shortest
of at least 7 digits that no other id of the project starts with (`short_ids`, computed when shown, never stored); a
reference of the id form names a version by any unique prefix of 4 or more digits (`ids`, refs.pick). Ids carry no
order: listings go by number, and "oldest first" by `created`.

Projects: a server's repo holds many, each served as if it were its own repo (store/project.py binds one), sharing
the one database and the one `blobs/`, so a file several projects publish is stored once. A project's own repo
(`.cairn/`) holds exactly one, which has no name: its rows have the project "" (`OWN`). References and the inputs
and consumers recorded never name a project: they name versions of the project they belong to.

Every process on the repo's host opens the repo directly, whether or not `cairn server` serves it: scripts,
commands, `cairn ui`, and the server itself. Any number may write at once: each write is one SQLite transaction
(serialized by SQLite's lock), caches follow the projects' generations, and gc rechecks inside a transaction
(gc.py). Other hosts reach a repo only through `cairn server`. At most one server serves a repo (`server.lock`,
db.ServerLock). A repo database is never written on a network filesystem (fs.py).

Deleting a version leaves a tombstone (status `deleted`: number, id, label, metadata, and inputs kept, the file
list dropped), so a number or id is never used again; references to it fail with Deleted, and lineage keeps its
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
from .blobs import CHUNK, BlobStore, BlobWriter, clean_entries, merged_files, now, resolve_entry
from .db import RETENTION_TABLE, Database, export
from .digest import manifest_digest
from .errors import (
    BadRange,
    Conflict,
    Deleted,
    Forbidden,
    InvalidRef,
    MissingBlobs,
    NotFound,
    OldRepo,
    Protected,
    StoreError,
    Unauthorized,
)  # noqa: F401
from .fs import network_filesystem, network_fs_message
from .ids import is_id, new_id, valid_id
from .refs import Ref


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
    return (
        (version.get("digest") or manifest_digest(version["files"])) == digest
        and sorted(version["inputs"]) == sorted(inputs)
        and version["metadata"] == metadata
    )


def _check_digest(given: str | None, actual: str, what: str) -> None:
    """A client's digest of the files it sent must be the one the store computes from them (Conflict, a 409, if not)."""
    if given is not None and given != actual:
        raise Conflict(f"{what}: the manifest digest {given} does not match the files received ({actual})")


REPO_FILE = "repo.json"
LAYOUT = 2  # 0.15: versions named by id (0.14's layout 1 named them by uid: `cairn upgrade` converts it)
OWN = ""  # the project of a project's own repo, which has one and no name for it


def valid_key(project: str) -> str:
    """A project as the core stores it: OWN, or a project name (refs.valid_project)."""
    return project if project == OWN else refs.valid_project(project)


def _read_id(root: Path, path: Path) -> str:
    """The id `repo.json` holds; OldRepo for a repo of an older layout (0.14), StoreError for a damaged file."""
    try:
        record = json.loads(path.read_text())
    except ValueError:
        raise StoreError(f'{path} is damaged: it must hold {{"id": <64 hex digits>, "layout": {LAYOUT}, ...}}')
    if isinstance(record, dict) and ("uid" in record or record.get("layout") in (None, 1)):
        raise OldRepo(root)
    try:
        if record.get("layout") != LAYOUT:
            raise StoreError(
                f"{root}: this repo has layout {record.get('layout')!r}, which this Cairn does not know (it knows "
                f"{LAYOUT}): it was made by a newer Cairn"
            )
        return valid_id(record["id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise StoreError(f'{path} is damaged: it must hold {{"id": <64 hex digits>, "layout": {LAYOUT}, ...}}')


def repo_id(root: Path) -> str:
    """The id in `<root>/repo.json`, made on first use (the folder then becomes private to its owner, mode 700).
    Two processes racing both end with the one that won (the file is linked into place, which fails if it exists),
    so a repo's id never changes. A repo of an older layout raises OldRepo (`cairn upgrade` converts it)."""
    root = Path(root)
    path = root / REPO_FILE
    for _ in range(3):
        try:
            return _read_id(root, path)
        except FileNotFoundError:
            pass
        temporary = root / f"{REPO_FILE}.{secrets.token_hex(6)}.tmp"
        temporary.write_text(json.dumps({"id": new_id(), "layout": LAYOUT, "created": now()}, indent=1))
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
    return _read_id(root, path)


def read_repo_file(root: Path) -> dict:
    """`<root>/repo.json` as a dict ({} if it is missing or damaged)."""
    try:
        value = json.loads((Path(root) / REPO_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def update_repo_file(root: Path, **fields) -> None:
    """Add fields to `<root>/repo.json` (write-temp-and-rename; the id stays). Never raises: it only records facts
    (the remote's id) that are learned again on the next contact."""
    root = Path(root)
    try:
        current = read_repo_file(root)
        if not current.get("id") or all(current.get(key) == value for key, value in fields.items()):
            return
        temporary = root / f"{REPO_FILE}.{secrets.token_hex(6)}.tmp"
        temporary.write_text(json.dumps({**current, **fields}, indent=1))
        os.replace(temporary, root / REPO_FILE)
    except OSError:
        pass


def outbox_blobs(root: Path) -> set[str]:
    """The blobs the outbox entries of the repo `root` need (`<root>/outbox/<id>/manifest.json`): never garbage."""
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
        if isinstance(record, dict) and record.get("sync") != "synced":  # synced: sent, its folder about to go
            found.update(
                item["sha256"] for item in record.get("files") or [] if isinstance(item, dict) and "sha256" in item
            )
    return found


def summary_row(states: dict[int, dict], aliases: dict[str, int]) -> tuple:
    """(latest, versions, updated, metadata JSON, aliases JSON): an artifact's summary row (db.py)."""
    stamps = [_stamp(state) for state in states.values()]
    head = states.get(aliases.get("latest"))
    return (
        aliases.get("latest"),
        len(states),
        max(stamps, default=""),
        json.dumps(head["metadata"] if head else {}),
        json.dumps(aliases),
    )


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
    def __init__(
        self,
        root: Path | str,
        allow_network_fs: bool = False,
        heartbeat_timeout: float = 600.0,
        where: str | None = None,
    ):
        """A folder on a network filesystem is refused (StoreError, before anything is written) unless
        `allow_network_fs`: the caller has made sure one process on one host is its only writer (`[repo]
        allow_network_fs`, `cairn server --allow-network-fs`), or only reads it. Opening the repo fails the running
        versions whose owner is gone (`reap`; `heartbeat_timeout` seconds without a heartbeat, or its process dead on
        this host); what it failed is in `self.reaped`. `where` says in messages where the repo's own project's
        versions were looked for (default: `the repo <root>`; "" says nothing, as a server does)."""
        self.root = Path(root)
        self.where = f"the repo {self.root}" if where is None else where
        if not allow_network_fs:
            fstype = network_filesystem(self.root)
            if fstype:
                raise StoreError(network_fs_message(self.root, fstype))
        self.root.mkdir(parents=True, exist_ok=True)
        self._blobs = BlobStore(self.root)  # creates blobs/ and tmp/
        self.id = repo_id(self.root)
        self._db = Database(self.root, self.id)
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
                rows = connection.execute(
                    "SELECT project, name, number, manifest FROM versions WHERE status = 'completed'"
                ).fetchall()
                for project, name, number, text in rows:
                    state = json.loads(text)
                    if not state.get("digest"):
                        state["digest"] = manifest_digest(state["files"])
                        connection.execute(
                            "UPDATE versions SET manifest = ? WHERE project = ? AND name = ? AND number = ?",
                            (json.dumps(state), project, name, number),
                        )
                connection.execute("INSERT OR REPLACE INTO meta VALUES ('digests', 'done')")
        except sqlite3.OperationalError:  # a read-only root: the digests are added by the first writer that opens it
            pass

    # ------------------------------------------------------------------ heartbeats

    def _beat_file(self, id: str) -> Path:
        return self.root / "heartbeats" / valid_id(id)

    def beat(self, id: str, owner: bool = True) -> None:
        """The owner of a running version says it is alive (never raises: a missed beat only makes the version
        look dead later). `owner=False`: another process vouches for it (a server, for a client's version that it
        syncs), so only its age counts, not whether this process lives."""
        path = self._beat_file(id)
        try:
            path.parent.mkdir(exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            who = {"host": socket.gethostname(), "pid": os.getpid()} if owner else {"host": None, "pid": None}
            temporary.write_text(json.dumps({"time": time.time(), **who}))
            os.replace(temporary, path)
        except OSError:
            pass

    def _clear_beat(self, id: str) -> None:
        try:
            self._beat_file(id).unlink(missing_ok=True)
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
            if path.name.startswith(".") or not is_id(path.name):
                continue
            try:
                beat = json.loads(path.read_text())
                stamp = float(beat["time"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            gone = (
                beat.get("host") == socket.gethostname()
                and isinstance(beat.get("pid"), int)
                and not pid_alive(beat["pid"])
            )
            if not gone and time.time() - stamp <= timeout:
                continue
            since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
            reason = f"the process died (no heartbeat since {since})"
            try:
                with self._write() as connection:
                    found = self._by_id(connection, path.name)
                    if found is not None and found[2]["status"] == "running":
                        project, name, state = found
                        self._save(
                            connection,
                            project,
                            name,
                            {**state, "status": "failed", "completed": now(), "error": reason},
                        )
                        failed.append((self._named(project, refs.exact(name, state["version"])), reason))
            except (StoreError, sqlite3.OperationalError, OSError):
                continue  # read-only or busy: the next open tries again
            self._clear_beat(path.name)
        return failed

    def _in(self, project: str) -> str:
        """Where a message says a version of `project` was looked for: ` in the repo ...` for the own project (`where`),
        nothing for a named project of a server (its client says which remote)."""
        return f" in {self.where}" if project == OWN and self.where else ""

    @staticmethod
    def _named(project: str, ref: str) -> str:
        """A reference as a repo-wide report lists it (reap, gc): with its project on a server's repo."""
        return ref if project == OWN else f"{ref} (project {project})"

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

    def open_file(self, project: str, ref: Ref | str) -> tuple[dict, Path, bool]:
        """(entry, blob path, immutable) of the file a reference with a path names (`name@v<n>//path`, `<id>//path`:
        any status; an alias or a label: a completed version). `immutable`: an exact reference to a completed version.
        """
        ref = refs.parse(ref)
        if not ref.path:
            raise InvalidRef(f"{ref} names a version, not a file in it (add //<path>)")
        name, version = self.find(project, ref)
        if version["status"] == "deleted":
            raise self._gone(project, name, version)
        entry = resolve_entry(version["files"], ref.path)
        if entry is None:
            raise NotFound(f"{refs.exact(name, version['version'])} has no file {ref.path}{self._in(project)}")
        immutable = version["status"] == "completed" and ref.exact
        return entry, self.blob_path(entry["sha256"]), immutable

    def read_file(self, project: str, ref: Ref | str, range_header: str | None = None) -> FileRead:
        """The file a reference with a path names (`open_file`), optionally a byte range (the value of an HTTP Range
        header). Raises NotFound and BadRange."""
        entry, blob, immutable = self.open_file(project, ref)
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

    def _record_of(self, connection: sqlite3.Connection, project: str) -> dict:
        row = connection.execute("SELECT record FROM projects WHERE name = ?", (valid_key(project),)).fetchone()
        if row is None:
            raise NotFound(self._unknown(project))
        return json.loads(row[0])

    def _unknown(self, project: str) -> str:
        return f"no project {project!r}" if project != OWN else f"nothing published yet{self._in(project)}"

    def create_project(self, name: str, title: str | None = None, description: str = "") -> dict:
        """Create a project (OWN: the repo's own); an existing one is returned unchanged."""
        valid_key(name)
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
            record.update(
                {key: value for key, value in (("title", title), ("description", description)) if value is not None}
            )
            connection.execute(
                "UPDATE projects SET record = ?, generation = generation + 1 WHERE name = ?", (json.dumps(record), name)
            )
            return record

    def projects(self) -> list[dict]:
        with self._db.read() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT record FROM projects ORDER BY name")]

    def overviews(self) -> list[dict]:
        """Every project record with its artifact count and last update."""
        with self._db.read() as connection:
            counts = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    "SELECT project, count(*), max(updated) FROM artifacts WHERE versions > 0 GROUP BY project"
                )
            }
            found = []
            for (text,) in connection.execute("SELECT record FROM projects ORDER BY name"):
                record = json.loads(text)
                count, updated = counts.get(record["name"], (0, None))
                found.append({**record, "artifacts": count, "updated": updated or record["created"]})
            return found

    # --------------------------------------------------------------- artifacts

    def _project(self, connection: sqlite3.Connection, project: str) -> None:
        if connection.execute("SELECT 1 FROM projects WHERE name = ?", (valid_key(project),)).fetchone() is None:
            raise NotFound(self._unknown(project))

    def _check(self, connection: sqlite3.Connection, project: str, name: str) -> None:
        self._project(connection, project)
        refs.valid_name(name)

    def artifacts(self, project: str) -> list[str]:
        with self._db.read() as connection:
            self._project(connection, project)
            return [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM artifacts WHERE project = ? AND versions > 0 ORDER BY name", (project,)
                )
            ]

    @staticmethod
    def _state(connection, project: str, name: str, number: int) -> dict | None:
        row = connection.execute(
            "SELECT manifest FROM versions WHERE project = ? AND name = ? AND number = ?", (project, name, number)
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _states(connection, project: str, name: str) -> dict[int, dict]:
        return {
            row[0]: json.loads(row[1])
            for row in connection.execute(
                "SELECT number, manifest FROM versions WHERE project = ? AND name = ? ORDER BY number", (project, name)
            )
        }

    @staticmethod
    def _by_id(connection, id: str) -> tuple[str, str, dict] | None:
        """(project, name, record) of the version with this id, anywhere in the repo (an id is unique in it)."""
        row = connection.execute("SELECT project, name, manifest FROM versions WHERE id = ?", (id,)).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    @staticmethod
    def _short(connection, project: str, id: str) -> str:
        """The short id of `id` in the project (refs.short_id): only its neighbours in the sorted index matter."""
        below = connection.execute(
            "SELECT id FROM versions WHERE project = ? AND id < ? ORDER BY id DESC LIMIT 1", (project, id)
        ).fetchone()
        above = connection.execute(
            "SELECT id FROM versions WHERE project = ? AND id > ? ORDER BY id LIMIT 1", (project, id)
        ).fetchone()
        return refs.short_id(id, [row[0] for row in (below, above) if row])

    def short_ids(self, project: str, ids) -> dict[str, str]:
        """{id: short id} in the project (an id the project does not have gets the prefix no version of it shares)."""
        with self._db.read() as connection:
            return {id: self._short(connection, project, id) for id in dict.fromkeys(ids) if is_id(id)}

    @staticmethod
    def _aliases(connection, project: str, name: str) -> dict[str, int]:
        """{alias: number} of an artifact: the aliases point at ids (the aliases table), shown by number."""
        return {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT a.alias, v.number FROM aliases a JOIN versions v ON v.id = a.id "
                "WHERE a.project = ? AND a.name = ? ORDER BY a.position",
                (project, name),
            )
        }

    @staticmethod
    def _alias_number(connection, project: str, name: str, alias: str) -> int | None:
        """The number of the version an alias points at (None without the alias)."""
        row = connection.execute(
            "SELECT v.number FROM aliases a JOIN versions v ON v.id = a.id "
            "WHERE a.project = ? AND a.name = ? AND a.alias = ?",
            (project, name, alias),
        ).fetchone()
        return row[0] if row else None

    def version_numbers(self, project: str, name: str) -> set[int]:
        """The artifact's version numbers (cheaper than `versions` when only they are needed)."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            return {
                row[0]
                for row in connection.execute(
                    "SELECT number FROM versions WHERE project = ? AND name = ?", (project, name)
                )
            }

    def version_ids(self, project: str) -> set[str]:
        """The ids of every version of the project (tombstones too)."""
        with self._db.read() as connection:
            return {row[0] for row in connection.execute("SELECT id FROM versions WHERE project = ?", (project,))}

    def last_number(self, project: str, name: str) -> int:
        """The artifact's highest version number (0 without versions); a new version always gets the next one."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            return connection.execute(
                "SELECT coalesce(max(number), 0) FROM versions WHERE project = ? AND name = ?", (project, name)
            ).fetchone()[0]

    def versions(self, project: str, name: str) -> list[dict]:
        with self._db.read() as connection:
            self._check(connection, project, name)
            return list(self._states(connection, project, name).values())

    def get_version(self, project: str, name: str, version: int | str) -> dict:
        """One version's manifest, by number or id, whatever its status."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            return self._lookup(connection, project, name, version)

    @staticmethod
    def _summary(row) -> dict:
        name, latest, versions, aliases, updated, metadata = row
        return {
            "name": name,
            "latest": latest,
            "versions": versions,
            "aliases": json.loads(aliases),
            "updated": updated,
            "metadata": json.loads(metadata),
        }

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
            row = connection.execute("SELECT generation FROM projects WHERE name = ?", (valid_key(project),)).fetchone()
            if row is None:
                raise NotFound(self._unknown(project))
            cached = self._listings.get(project)
            if cached is None or cached[0] != row[0]:
                rows = connection.execute(
                    f"{_SUMMARY} WHERE project = ? AND versions > 0 ORDER BY name", (project,)
                ).fetchall()
                cached = (row[0], [self._summary(item) for item in rows])
                self._listings[project] = cached
        return (project, cached[0]), cached[1]

    def aliases(self, project: str, name: str) -> dict[str, int]:
        """Where each alias points now (the last move wins)."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            return self._aliases(connection, project, name)

    def alias_log(self, project: str, name: str) -> list[dict]:
        """Every move of the artifact's aliases, oldest first: {alias, id, version, time} (id and version None for a
        removal; the version None too when it is no longer in the repo)."""
        with self._db.read() as connection:
            self._check(connection, project, name)
            return [
                {"alias": row[0], "id": row[1] or None, "version": row[2], "time": row[3]}
                for row in connection.execute(
                    "SELECT l.alias, l.id, v.number, l.time FROM alias_log l LEFT JOIN versions v ON v.id = l.id "
                    "AND l.id != '' WHERE l.project = ? AND l.name = ? ORDER BY l.seq",
                    (project, name),
                )
            ]

    # ------------------------------------------------------------------ writes (each one transaction)

    @staticmethod
    def _save(connection, project: str, name: str, state: dict, created: bool = False) -> None:
        """Store a version's record, and keep the artifact's summary row and the project's generation current."""
        values = (state["id"], state["label"], state["status"], json.dumps(state), project, name, state["version"])
        if created:
            connection.execute(
                "INSERT INTO versions (id, label, status, manifest, project, name, number) VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            connection.execute(
                "INSERT INTO artifacts (project, name, versions, updated) VALUES (?, ?, 1, ?) ON CONFLICT (project, name) "
                "DO UPDATE SET versions = versions + 1, updated = max(updated, excluded.updated)",
                (project, name, _stamp(state)),
            )
        else:
            connection.execute(
                "UPDATE versions SET id = ?, label = ?, status = ?, manifest = ? WHERE project = ? AND name = ? AND number = ?",
                values,
            )
            connection.execute(
                "UPDATE artifacts SET updated = max(updated, ?) WHERE project = ? AND name = ?",
                (_stamp(state), project, name),
            )
        connection.execute("UPDATE projects SET generation = generation + 1 WHERE name = ?", (project,))

    def _running(self, connection, project: str, name: str, version: int | str) -> dict:
        """The record of a running version, by number or id (StoreError if it is final)."""
        state = self._lookup(connection, project, name, version)
        if state["status"] != "running":
            raise StoreError(f"{refs.exact(name, state['version'])} is {state['status']} and can no longer change")
        return state

    def _input_ids(self, connection, project: str, inputs) -> list[str]:
        """The ids of the versions input references name (of the same project), in order, each once: every one must
        resolve to a completed version, or, named by its whole id, to a running one (the run a job's outputs come from,
        which completes after them). Unknown, failed, and deleted inputs are refused."""
        found: list[str] = []
        for item in inputs or []:
            ref = refs.parse(item)
            if ref.whole:
                known = self._by_id(connection, ref.id)
                if known is not None and known[0] == project and known[2]["status"] == "running":
                    if ref.id not in found:
                        found.append(ref.id)
                    continue
            _, version = self._resolve(connection, project, ref)
            if version["id"] not in found:
                found.append(version["id"])
        return found

    @staticmethod
    def _check_label(connection, project: str, name: str, label: str | None) -> None:
        """A new version's label: one a version may have (refs.new_label), and not an alias of its artifact (an alias
        and a label never share a word)."""
        if label is None:
            return
        refs.new_label(label)
        if connection.execute(
            "SELECT 1 FROM aliases WHERE project = ? AND name = ? AND alias = ?", (project, name, label)
        ).fetchone():
            raise InvalidRef(f"the label {label!r} is an alias of {name}: an alias and a label never share a word")

    @staticmethod
    def _check_alias(connection, project: str, name: str, alias: str) -> None:
        """An alias that may be set (refs.new_alias), and not a label of a version of its artifact."""
        refs.new_alias(alias)
        if connection.execute(
            "SELECT 1 FROM versions WHERE project = ? AND name = ? AND label = ? LIMIT 1", (project, name, alias)
        ).fetchone():
            raise InvalidRef(
                f"the alias {alias!r} is a label of a version of {name}: an alias and a label never share a word"
            )

    def _new(self, connection, project, name, label, metadata, source, id=None) -> dict:
        """A new running version's record, numbered next (unique: this runs inside a write transaction)."""
        refs.valid_name(name)
        self._check_label(connection, project, name, label)
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict):
            raise StoreError("metadata must be a dict")
        json.dumps(metadata)
        id = new_id() if id is None else valid_id(id)
        other = self._by_id(connection, id)
        if other is not None:
            raise Conflict(f"id {id} already names {self._named(other[0], refs.exact(other[1], other[2]['version']))}")
        number = connection.execute(
            "SELECT coalesce(max(number), 0) + 1 FROM versions WHERE project = ? AND name = ?", (project, name)
        ).fetchone()[0]
        return {
            "version": number,
            "id": id,
            "label": label,
            "status": "running",
            "created": now(),
            "files": [],
            "metadata": metadata,
            "inputs": [],
            "source": source,
        }

    def _merged(self, state: dict, entries) -> dict:
        clean = clean_entries(entries, removals=True)
        missing = self.missing(clean)  # inside the write transaction: a sweep cannot remove them meanwhile
        if missing:
            raise MissingBlobs(missing)
        files = merged_files(state["files"], clean)
        _check_tree(files)
        return {**state, "files": files}

    def _completed(self, connection, project, name, state: dict) -> None:
        """After a version completed: `latest` moves to it if its number is the highest completed one (a version
        started before another and completed after it does not take `latest` from it: `latest` is never clock order),
        and the versions it consumed learn of it."""
        head = self._alias_number(connection, project, name, "latest")
        if head is None or head < state["version"]:
            self._move_alias(connection, project, name, "latest", state["version"])
        self._consumed(connection, project, state)

    @staticmethod
    def _consumed(connection, project: str, state: dict) -> None:
        """Record the edges from a version's inputs to it (the consumers table)."""
        for input_id in state.get("inputs") or []:
            connection.execute(
                "INSERT OR IGNORE INTO consumers (project, input, consumer, time) VALUES (?, ?, ?, ?)",
                (project, input_id, state["id"], now()),
            )

    def _move_alias(self, connection, project, name, alias, version) -> None:
        """Point an alias at version number `version` (the aliases table stores its id)."""
        target = self._state(connection, project, name, version)["id"]
        row = connection.execute(
            "SELECT id FROM aliases WHERE project = ? AND name = ? AND alias = ?", (project, name, alias)
        ).fetchone()
        if row is not None and row[0] == target:
            return
        cursor = connection.execute(
            "INSERT INTO alias_log (project, name, alias, id, time) VALUES (?, ?, ?, ?, ?)",
            (project, name, alias, target, now()),
        )
        connection.execute(
            "INSERT INTO aliases VALUES (?, ?, ?, ?, ?) ON CONFLICT (project, name, alias) DO UPDATE SET id = excluded.id",
            (project, name, alias, target, cursor.lastrowid),
        )
        self._summarize(connection, project, name)

    def _summarize(self, connection, project: str, name: str) -> None:
        """Bring the artifact's summary row (latest number and metadata, aliases by number) up to date with the aliases
        table, and bump the project's generation."""
        aliases = self._aliases(connection, project, name)
        head = self._state(connection, project, name, aliases["latest"]) if "latest" in aliases else None
        connection.execute(
            "UPDATE artifacts SET latest = ?, metadata = ?, aliases = ? WHERE project = ? AND name = ?",
            (
                aliases.get("latest"),
                json.dumps(head["metadata"] if head else {}),
                json.dumps(aliases),
                project,
                name,
            ),
        )
        connection.execute("UPDATE projects SET generation = generation + 1 WHERE name = ?", (project,))

    def start_version(
        self,
        project: str,
        name: str,
        label: str | None = None,
        metadata: dict | None = None,
        source: dict | None = None,
        id: str | None = None,
    ) -> int:
        """Start a running version; returns its number. With `id` (made by the client), starting the same version again
        returns its number (a running one takes the label and metadata given now: its first answer may have been lost);
        an id that names a final version with another label or metadata is a Conflict."""
        with self._write() as connection:
            self._check(connection, project, name)
            if id is not None:
                known = self._by_id(connection, valid_id(id))
                if known is not None and known[:2] == (project, name):
                    state = known[2]
                    if state["status"] == "deleted":
                        raise Conflict(
                            f"id {id} named {name}@v{state['version']}, which was deleted: an id is never used again"
                        )
                    if state["label"] != label or state["metadata"] != (metadata or {}):
                        if state["status"] != "running":
                            raise Conflict(
                                f"id {id} already names {name}@v{state['version']} with another label or metadata"
                            )
                        # started before, and its answer lost; its metadata changed since: a running version may change
                        self._check_label(connection, project, name, label)
                        self._save(connection, project, name, {**state, "label": label, "metadata": metadata or {}})
                    return state["version"]
            state = self._new(connection, project, name, label, metadata, source, id)
            self._save(connection, project, name, state, created=True)
            return state["version"]

    def add_files(self, project: str, name: str, version: int | str, entries: list[dict]) -> None:
        """Add, replace or remove files in a running version (by number or id; entries as in
        `blobs.clean_entries(..., removals=True)`, applied in order, blobs already stored)."""
        with self._write() as connection:
            self._check(connection, project, name)
            self._save(
                connection, project, name, self._merged(self._running(connection, project, name, version), entries)
            )

    def set_metadata(self, project: str, name: str, version: int | str, metadata: dict) -> None:
        """Replace the metadata of a running version (a completed one never changes)."""
        if not isinstance(metadata, dict):
            raise StoreError("metadata must be a dict")
        json.dumps(metadata)
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._running(connection, project, name, version)
            self._save(connection, project, name, {**state, "metadata": metadata})

    def complete_version(
        self, project: str, name: str, version: int | str, inputs=None, digest: str | None = None
    ) -> None:
        """Freeze a version (by number or id) and move `latest` to it; `inputs` are the references it consumed. The
        version's manifest digest is computed from its files; a `digest` the client supplies must equal it (else
        Conflict, and nothing changes)."""
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._running(connection, project, name, version)
            consumed = self._input_ids(connection, project, inputs)
            actual = manifest_digest(state["files"])
            _check_digest(digest, actual, refs.exact(name, state["version"]))
            state = {**state, "status": "completed", "completed": now(), "inputs": consumed, "digest": actual}
            self._save(connection, project, name, state)
            self._completed(connection, project, name, state)
        self._clear_beat(state["id"])

    def fail_version(self, project: str, name: str, version: int | str, error: str | None = None) -> None:
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._running(connection, project, name, version)
            self._save(
                connection,
                project,
                name,
                {**state, "status": "failed", "completed": now(), **({"error": error} if error else {})},
            )
        self._clear_beat(state["id"])

    def publish(
        self,
        project: str,
        name: str,
        entries: list[dict],
        label: str | None = None,
        metadata: dict | None = None,
        inputs=None,
        aliases=(),
        source: dict | None = None,
        id: str | None = None,
        digest: str | None = None,
    ) -> int:
        """Start, fill, and complete a version in one step, in one transaction (all of it is stored, or none).

        When the latest completed version already has these files, inputs, and metadata, no version
        is created and its number is returned (the aliases are still applied). With `id`, publishing
        the same version again returns its number; a known id with other content is a Conflict. A `digest` the
        client supplies must equal the manifest digest of the entries (digest.py), else Conflict.
        """
        clean = sorted(clean_entries(entries), key=lambda entry: entry["path"])
        _check_tree(clean)
        sweeps = self._sweeps()
        missing = self.missing(clean)
        if missing:
            raise MissingBlobs(missing)
        for alias in aliases:
            refs.new_alias(alias)
        actual = manifest_digest(clean)
        _check_digest(digest, actual, name)
        with self._write() as connection:
            self._check(connection, project, name)
            if (
                self._sweeps(connection) != sweeps
            ):  # a blob sweep ran since the check: check again, inside the transaction
                missing = self.missing(clean)
                if missing:
                    raise MissingBlobs(missing)
            consumed = self._input_ids(connection, project, inputs)
            known = self._by_id(connection, valid_id(id)) if id is not None else None
            head = self._alias_number(connection, project, name, "latest")
            state = self._state(connection, project, name, head) if head is not None else None
            if known is not None:
                if known[2]["status"] == "deleted":
                    raise Conflict(
                        f"id {id} named {self._named(known[0], refs.exact(known[1], known[2]['version']))}, which "
                        "was deleted: an id is never used again"
                    )
                if (
                    known[:2] != (project, name)
                    or known[2]["status"] != "completed"
                    or not _same(known[2], actual, consumed, metadata or {})
                ):
                    raise Conflict(
                        f"id {id} already names {self._named(known[0], refs.exact(known[1], known[2]['version']))} "
                        "with other content"
                    )
                version = known[2]["version"]
            elif state is not None and _same(state, actual, consumed, metadata or {}):
                version = state["version"]
            else:
                record = self._new(connection, project, name, label, metadata, source, id)
                record = {
                    **record,
                    "files": clean,
                    "status": "completed",
                    "completed": now(),
                    "inputs": consumed,
                    "digest": actual,
                }
                self._save(connection, project, name, record, created=True)
                version = record["version"]
                self._completed(connection, project, name, record)
            for alias in aliases:
                self._set_alias(connection, project, name, alias, version)
        return version

    def _set_alias(self, connection, project, name, alias, version) -> None:
        self._check_alias(connection, project, name, alias)
        state = self._state(connection, project, name, version)
        if state is None or state["status"] != "completed":
            raise StoreError(f"alias {alias!r}: {name}@v{version} is not a completed version{self._in(project)}")
        self._move_alias(connection, project, name, alias, version)

    def set_alias(self, project: str, name: str, alias: str, version: int) -> None:
        """Point `alias` at a completed version. `latest` moves by itself and cannot be set; an alias is never 4 or more
        hex digits alone, nor a label of a version of the artifact."""
        refs.new_alias(alias)
        with self._write() as connection:
            self._check(connection, project, name)
            self._set_alias(connection, project, name, alias, version)

    # ------------------------------------------------------------------ copies of a remote's versions

    def meta(self, key: str) -> str | None:
        """A value of the database's meta table (e.g. `remote`: the id of the remote this repo syncs with)."""
        with self._db.read() as connection:
            return Database.meta(connection, key)

    def set_meta(self, key: str, value: str) -> None:
        with self._write() as connection:
            connection.execute(
                "INSERT INTO meta VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value)
            )

    def _evict(self, connection, project: str, name: str, number: int, id: str) -> None:
        """Make room for the remote's version `number` (id `id`): another row holding that number is a stale copy,
        dropped. A version this repo numbered itself and has not sent yet is never dropped (Conflict: it is adopted
        first, see client/sync.py)."""
        state = self._state(connection, project, name, number)
        if state is None or state["id"] == id:
            return
        if not state.get("origin"):
            raise Conflict(
                f"{refs.exact(name, number)} in the repo {self.root} is a version of this repo that the remote "
                "does not have yet; it is sent first"
            )
        self._drop_rows(connection, [(project, name, state)])

    def _drop_rows(self, connection, rows: list[tuple[str, str, dict]]) -> None:
        """Forget versions (not tombstones: they are gone from this repo), with their aliases and the edges they recorded."""
        touched = set()
        for project, name, state in rows:
            pointing = [
                row[0]
                for row in connection.execute(
                    "SELECT alias FROM aliases WHERE project = ? AND name = ? AND id = ?", (project, name, state["id"])
                )
            ]
            for alias in pointing:
                self._remove_alias(connection, project, name, alias)
            connection.execute("DELETE FROM versions WHERE id = ?", (state["id"],))
            if state["status"] != "deleted":
                connection.execute(
                    "UPDATE artifacts SET versions = versions - 1 WHERE project = ? AND name = ?", (project, name)
                )
            connection.execute("DELETE FROM consumers WHERE project = ? AND consumer = ?", (project, state["id"]))
            touched.add((project, name))
        for project, name in touched:
            self._fix_latest(connection, project, name)
            connection.execute("UPDATE projects SET generation = generation + 1 WHERE name = ?", (project,))

    def _fix_latest(self, connection, project: str, name: str) -> None:
        """`latest` names the highest completed number: the one it named while that is completed, else the highest
        completed one left."""
        head = self._alias_number(connection, project, name, "latest")
        state = self._state(connection, project, name, head) if head is not None else None
        if state is not None and state["status"] == "completed":
            return
        newest = connection.execute(
            "SELECT max(number) FROM versions WHERE project = ? AND name = ? AND status = 'completed'", (project, name)
        ).fetchone()[0]
        if newest is not None:
            self._move_alias(connection, project, name, "latest", newest)
        elif connection.execute(
            "SELECT 1 FROM aliases WHERE project = ? AND name = ? AND alias = 'latest'", (project, name)
        ).fetchone():
            self._remove_alias(connection, project, name, "latest")

    def put_copy(self, project: str, name: str, manifest: dict, origin: str) -> bool:
        """Record a version as the remote `origin` (its id and project) answered it: a copy, numbered by the remote.
        Only final versions are copied (completed, failed, deleted); a copy that is already here as given is left alone.
        A tombstone replaces a copy. `latest` moves to a completed copy newer than the one it names (`mirror_aliases`
        sets it exactly). Returns whether anything changed. One transaction."""
        number, id, status = manifest.get("version"), manifest.get("id"), manifest.get("status")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not is_id(id)
            or status not in ("completed", "failed", "deleted")
        ):
            return False
        valid_key(project)
        refs.valid_name(name)
        record = {
            key: value
            for key, value in manifest.items()
            if key not in ("pending", "sync", "aliases", "copied", "short_id")
        }
        record["inputs"] = [item for item in record.get("inputs") or [] if is_id(item)]
        record["origin"] = origin
        with self._write() as connection:
            if connection.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone() is None:
                connection.execute(
                    "INSERT INTO projects (name, record) VALUES (?, ?)",
                    (project, json.dumps({"name": project, "title": project, "description": "", "created": now()})),
                )
            known = self._by_id(connection, id)
            if known is not None and known[:2] != (project, name):
                raise Conflict(
                    f"id {id} names {refs.exact(known[1], known[2]['version'])} in this repo, not a version of {name}"
                )
            if known is not None:
                state = known[2]
                if {k: v for k, v in state.items() if k != "copied"} == record:
                    return False
                if state["version"] != number:
                    self._evict(connection, project, name, number, id)
                    connection.execute("UPDATE versions SET number = ? WHERE id = ?", (number, id))
                    self._summarize(connection, project, name)  # its aliases follow its id
                record["copied"] = state.get("copied") or now()
                self._save(connection, project, name, record)
                if state["status"] != "deleted" and status == "deleted":
                    connection.execute(
                        "UPDATE artifacts SET versions = versions - 1 WHERE project = ? AND name = ?", (project, name)
                    )
            else:
                self._evict(connection, project, name, number, id)
                record["copied"] = now()
                self._save(connection, project, name, record, created=True)
                if status == "deleted":
                    connection.execute(
                        "UPDATE artifacts SET versions = versions - 1 WHERE project = ? AND name = ?", (project, name)
                    )
            self._consumed(connection, project, record)
            if status == "completed":
                head = self._alias_number(connection, project, name, "latest")
                if head is None or head < number:
                    self._move_alias(connection, project, name, "latest", number)
            elif status == "deleted":
                for alias, target in self._aliases(connection, project, name).items():
                    if target == number and alias != "latest":
                        self._remove_alias(connection, project, name, alias)
                self._fix_latest(connection, project, name)
        return True

    def mirror_aliases(self, project: str, name: str, aliases: dict[str, int]) -> None:
        """Make the artifact's aliases here what the remote says they are (`{alias: number}`, every alias it has; the
        number names the copy here, whose id the alias stores): an alias whose version is not copied here, or not
        completed, is left out. One transaction."""
        with self._write() as connection:
            if (
                connection.execute("SELECT 1 FROM artifacts WHERE project = ? AND name = ?", (project, name)).fetchone()
                is None
            ):
                return
            current = self._aliases(connection, project, name)
            for alias, number in aliases.items():
                state = self._state(connection, project, name, number) if isinstance(number, int) else None
                if (
                    state is not None
                    and state.get("origin")
                    and state["status"] == "completed"
                    and current.get(alias) != number
                ):
                    self._move_alias(connection, project, name, alias, number)
            for alias in current:
                if alias not in aliases:
                    self._remove_alias(connection, project, name, alias)
            self._fix_latest(connection, project, name)

    def local_versions(self, project: str) -> list[tuple[str, dict]]:
        """(name, record) of every version of the project this repo numbered itself (no `origin`: not a remote's
        copy), oldest first (by `created`, then name and number)."""
        with self._db.read() as connection:
            rows = [
                (row[0], json.loads(row[1]))
                for row in connection.execute(
                    "SELECT name, manifest FROM versions WHERE project = ? AND json_extract(manifest, '$.origin') IS NULL "
                    "ORDER BY json_extract(manifest, '$.created'), name, number",
                    (project,),
                )
            ]
        return rows

    def forget_versions(self, ids) -> int:
        """Drop versions from this repo (not tombstones: they are gone from here, e.g. copies `cairn prune` trims, or
        versions handed to the outbox), with their aliases and the edges they recorded. Returns how many. One transaction.
        """
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return 0
        with self._write() as connection:
            rows = [found for id in wanted if (found := self._by_id(connection, id)) is not None]
            self._drop_rows(connection, rows)
            return len(rows)

    def copies(self, project: str) -> list[tuple[str, dict]]:
        """(name, record) of every copy of a remote's version in the project."""
        with self._db.read() as connection:
            return [
                (row[0], json.loads(row[1]))
                for row in connection.execute(
                    "SELECT name, manifest FROM versions WHERE project = ? AND json_extract(manifest, '$.origin') IS NOT NULL "
                    "ORDER BY name, number",
                    (project,),
                )
            ]

    # ------------------------------------------------------------- resolving

    def _has_artifact(self, connection, project: str, name: str) -> bool:
        return (
            connection.execute("SELECT 1 FROM artifacts WHERE project = ? AND name = ?", (project, name)).fetchone()
            is not None
        )

    def _candidates(self, connection, project: str, prefix: str, name: str | None = None) -> list[dict]:
        """The versions of the project (of one artifact, with `name`) whose id starts with `prefix`, at most 50: a range
        of the id index."""
        query = "SELECT name, manifest FROM versions WHERE project = ? AND id >= ? AND id < ?"
        values = [project, prefix, prefix + "g"]  # ids are 0-9a-f: every id with the prefix sorts below prefix + "g"
        if name is not None:
            query, values = query + " AND name = ?", [*values, name]
        rows = connection.execute(query + " ORDER BY id LIMIT 50", values).fetchall()
        found = []
        for row in rows:
            manifest = json.loads(row[1])
            found.append(refs.candidate(row[0], manifest, self._short(connection, project, manifest["id"])))
        return found

    def ids(self, project: str, prefix: str, name: str | None = None) -> list[dict]:
        """The versions (any status) whose id starts with `prefix` (4 to 64 hex digits): [{name, version, id, short_id,
        status}] (refs.pick chooses among them)."""
        ref = refs.parse(prefix)
        if ref.id is None or ref.path:
            raise InvalidRef(f"invalid id {prefix!r}: 4 to 64 hex digits")
        with self._db.read() as connection:
            self._project(connection, project)
            return self._candidates(connection, project, ref.id, name)

    def _find(self, connection, project: str, ref: Ref | str) -> tuple[str, dict]:
        """(name, record) of the version a reference names (refs.py): by id (a unique prefix) or `name@v<n>` whatever
        its status (a deleted one as its tombstone); by an alias, a label, or `latest`, a completed one."""
        ref = refs.parse(ref)
        self._project(connection, project)
        where = self._in(project)
        if ref.id is not None:
            found = refs.pick(ref.id, self._candidates(connection, project, ref.id), where)
            return found["name"], self._by_id(connection, found["id"])[2]
        if not self._has_artifact(connection, project, ref.name):
            raise NotFound(f"no artifact {ref.name}{where}")
        if ref.version is not None:
            return ref.name, self._lookup(connection, project, ref.name, ref.version)
        word = ref.word  # an alias or a label: never both (an alias and a label never share a word)
        number = self._alias_number(connection, project, ref.name, word)
        if number is None:
            number = connection.execute(
                "SELECT max(number) FROM versions WHERE project = ? AND name = ? AND label = ? AND status = 'completed'",
                (project, ref.name, word),
            ).fetchone()[0]
        if number is not None:
            return ref.name, self._state(connection, project, ref.name, number)
        raise NotFound(f"{ref.name}@{word} matches no completed version{where}")

    def _resolve(self, connection, project: str, ref: Ref | str) -> tuple[str, dict]:
        """(name, record) of the completed version a reference names (Deleted, or StoreError for another status)."""
        name, state = self._find(connection, project, ref)
        if state["status"] == "deleted":
            raise self._gone(project, name, state)
        if state["status"] != "completed":
            raise StoreError(
                f"{refs.exact(name, state['version'])} is {state['status']}, not completed{self._in(project)}"
            )
        return name, state

    def find(self, project: str, ref: Ref | str) -> tuple[str, dict]:
        """(name, version record) of the version a reference names, whatever its status for an id or `name@v<n>`
        (see `_find`); a bare name means `latest`."""
        with self._db.read() as connection:
            return self._find(connection, project, ref)

    def resolve(self, project: str, ref: Ref | str) -> tuple[str, dict]:
        """(name, version record) of the completed version a reference names; a bare name means `latest`."""
        with self._db.read() as connection:
            return self._resolve(connection, project, ref)

    def lineage(self, project: str, ref: Ref | str, marks: bool = False) -> dict[str, list[str]]:
        """The ids of the versions this one consumed (`inputs`) and of those that consumed it (`consumers`). Deleting a
        version keeps its edges: an id or `name@v<n>` of a deleted version answers too (and of a running run). With
        `marks`, `deleted` lists the ids among them (and the version's own) that were deleted, and `nodes` {id: {name,
        version, status, kind, short_id}} describes each of them."""
        with self._db.read() as connection:
            name, version = self._find(connection, project, ref)
            me = version["id"]
            consumers = [
                row[0]
                for row in connection.execute(
                    "SELECT consumer FROM consumers WHERE project = ? AND input = ? ORDER BY seq", (project, me)
                )
            ]
            found = {"id": me, "inputs": list(version["inputs"]), "consumers": consumers}
            if marks:
                found["nodes"] = self._node_marks(connection, project, [me, *found["inputs"], *consumers])
                found["deleted"] = [item for item, mark in found["nodes"].items() if mark["status"] == "deleted"]
        return found

    def _node_marks(self, connection, project: str, ids: list[str]) -> dict[str, dict]:
        """{id: {name, version, status, kind, short_id}} for lineage ids (kind from the version's metadata, e.g. "run")."""
        marks: dict[str, dict] = {}
        for id in ids:
            if id in marks:
                continue
            known = self._by_id(connection, id) if is_id(id) else None
            if known is None or known[0] != project:
                continue
            state = known[2]
            mark = {
                "name": known[1],
                "version": state["version"],
                "status": state["status"],
                "short_id": self._short(connection, project, id),
            }
            kind = (state.get("metadata") or {}).get("kind")
            if isinstance(kind, str):
                mark["kind"] = kind
            marks[id] = mark
        return marks

    # ------------------------------------------------------------- deleting (tombstones)

    @staticmethod
    def _gone(project: str, name: str, state: dict) -> Deleted:
        when = (state.get("deleted_at") or "")[:19]
        by = f" by {state['deleted_by']}" if state.get("deleted_by") else ""
        why = f": {state['reason']}" if state.get("reason") else ""
        return Deleted(f"{refs.exact(name, state['version'])} was deleted ({when}{by}){why}")

    def _lookup(self, connection, project: str, name: str, version: int | str) -> dict:
        """A version's record by number or whole id, whatever its status (NotFound if there is none)."""
        if isinstance(version, bool) or not (isinstance(version, int) or is_id(version)):
            raise InvalidRef(f"a version is a number or an id (64 hex digits), not {version!r}")
        if isinstance(version, int):
            state = self._state(connection, project, name, version)
        else:
            found = self._by_id(connection, version)
            state = found[2] if found and found[:2] == (project, name) else None
        if state is None:
            if not self._has_artifact(connection, project, name):
                raise NotFound(f"no artifact {name}{self._in(project)}")
            what = f"v{version}" if isinstance(version, int) else f"with the id {version}"
            raise NotFound(f"{name} has no version {what}{self._in(project)}")
        return state

    @staticmethod
    def _row_of(connection, project: str, id: str) -> tuple[str, int, str] | None:
        """(name, number, status) of the version of the project with this id (None: no such version)."""
        row = connection.execute(
            "SELECT name, number, status FROM versions WHERE project = ? AND id = ?", (project, id)
        ).fetchone()
        return tuple(row) if row else None

    def _protection(
        self, connection, project: str, name: str, state: dict, aliases: dict[str, int], deleting: set[str]
    ) -> list[str]:
        """Why a version may not be deleted without force: it is running, an alias other than `latest` points at it,
        or it is an input of a version of its project that is not deleted (`deleting`, ids, are going too)."""
        reasons = ["running"] if state["status"] == "running" else []
        reasons += [
            f"alias {alias}" for alias, number in aliases.items() if number == state["version"] and alias != "latest"
        ]
        for (consumer,) in connection.execute(
            "SELECT consumer FROM consumers WHERE project = ? AND input = ? ORDER BY seq", (project, state["id"])
        ).fetchall():
            row = self._row_of(connection, project, consumer)
            if consumer not in deleting and row is not None and row[2] != "deleted":
                reasons.append(f"input of {refs.exact(row[0], row[1])}")
        return reasons

    def _remove_alias(self, connection, project: str, name: str, alias: str) -> None:
        """Drop an alias (the log records the removal with an empty id)."""
        connection.execute("DELETE FROM aliases WHERE project = ? AND name = ? AND alias = ?", (project, name, alias))
        connection.execute(
            "INSERT INTO alias_log (project, name, alias, id, time) VALUES (?, ?, ?, '', ?)",
            (project, name, alias, now()),
        )
        self._summarize(connection, project, name)

    def _tombstone(
        self,
        connection,
        targets: list[tuple[str, str, dict]],
        *,
        reason: str | None,
        force: bool,
        by: str,
        skip: bool = False,
        apply: bool = True,
    ) -> dict:
        """Delete versions (project, name, record) in this transaction: each becomes a tombstone (status `deleted`,
        `deleted_at`, `deleted_by`, `reason`, its file list dropped; number, id, label, metadata, and inputs kept).
        Without `force`, protected versions refuse the whole call (Protected), or with `skip` are left out and
        reported. Aliases that pointed at a deleted version are removed; `latest` moves back to the newest completed
        version left. `apply=False` only says what would happen. Returns {deleted: [refs], ids: [ids], skipped: [{ref, reasons}]}.
        """
        aliases_of: dict[tuple[str, str], dict[str, int]] = {}
        chosen, protected = list(targets), []
        while True:  # leaving a protected version out can protect its inputs: repeat until nothing changes
            deleting = {state["id"] for project, name, state in chosen}
            keep, kept = [], []
            for project, name, state in chosen:
                aliases = aliases_of.setdefault((project, name), self._aliases(connection, project, name))
                reasons = self._protection(connection, project, name, state, aliases, deleting)
                if reasons and not force:
                    kept.append({"ref": refs.exact(name, state["version"]), "reasons": reasons})
                else:
                    keep.append((project, name, state))
            protected += kept
            if not kept:
                break
            chosen = keep
        if protected and not skip:
            listed = "; ".join(f"{item['ref']} ({', '.join(item['reasons'])})" for item in protected[:5])
            more = f" and {len(protected) - 5} more" if len(protected) > 5 else ""
            raise Protected(
                f"refused: protected versions {listed}{more}; delete with force to remove them anyway", protected
            )
        stamp, touched = now(), {}
        for project, name, state in chosen:
            if not apply:
                continue
            files = state.get("files") or []
            tomb = {
                key: state[key]
                for key in (
                    "version",
                    "id",
                    "label",
                    "created",
                    "completed",
                    "metadata",
                    "inputs",
                    "source",
                )
                if key in state
            }
            tomb.update(
                status="deleted",
                was=state["status"],
                files=[],
                file_count=sum("sha256" in entry for entry in files),
                size=sum(entry.get("size") or 0 for entry in files),
                deleted_at=stamp,
                deleted_by=by,
                reason=reason,
            )
            connection.execute(
                "UPDATE versions SET status = 'deleted', manifest = ? WHERE project = ? AND name = ? AND number = ?",
                (json.dumps(tomb), project, name, state["version"]),
            )
            connection.execute(
                "UPDATE artifacts SET versions = versions - 1 WHERE project = ? AND name = ?", (project, name)
            )
            for alias, number in list(aliases_of[(project, name)].items()):
                if number == state["version"] and alias != "latest":
                    self._remove_alias(connection, project, name, alias)
            touched[(project, name)] = True
        for project, name in touched:
            self._fix_latest(connection, project, name)
            connection.execute("UPDATE projects SET generation = generation + 1 WHERE name = ?", (project,))
        return {
            "deleted": [refs.exact(name, state["version"]) for project, name, state in chosen],
            "ids": [state["id"] for project, name, state in chosen],
            "skipped": protected,
        }

    @staticmethod
    def _reason(reason) -> str | None:
        if reason is not None and (not isinstance(reason, str) or len(reason) > 1000):
            raise ValueError("a reason is text of at most 1000 characters")
        return reason or None

    def delete_version(
        self,
        project: str,
        name: str,
        version: int | str,
        *,
        reason: str | None = None,
        force: bool = False,
        by: str | None = None,
    ) -> dict:
        """Delete one version (by number or id): it becomes a tombstone (see `_tombstone`) and its blobs are
        garbage once no other version names them (`gc`). Protected versions (an alias other than `latest` points at
        it, it is an input of a version that is not deleted in any project, or it is running) are refused with
        Protected unless `force`. Returns {deleted: [ref], ids: [id], skipped: []}."""
        reason = self._reason(reason)
        with self._write() as connection:
            self._check(connection, project, name)
            state = self._lookup(connection, project, name, version)
            if state["status"] == "deleted":
                raise self._gone(project, name, state)
            return self._tombstone(connection, [(project, name, state)], reason=reason, force=force, by=by or _who())

    def delete_artifact(
        self, project: str, name: str, *, reason: str | None = None, force: bool = False, by: str | None = None
    ) -> dict:
        """Delete every version of an artifact under the rules of `delete_version` (all of them, or none)."""
        reason = self._reason(reason)
        with self._write() as connection:
            self._check(connection, project, name)
            states = self._states(connection, project, name)
            if not states:
                raise NotFound(f"no artifact {name}{self._in(project)}")
            live = [(project, name, state) for state in states.values() if state["status"] != "deleted"]
            if not live:
                raise Deleted(f"every version of {name} was deleted already")
            return self._tombstone(connection, live, reason=reason, force=force, by=by or _who())

    def delete_project(
        self, project: str, *, reason: str | None = None, force: bool = False, by: str | None = None
    ) -> dict:
        """Delete a project. One with versions that are not deleted is refused (Protected) unless `force`, which
        deletes them all first, protected or not. The tombstones stay, so numbers and ids are never used again."""
        reason = self._reason(reason)
        with self._write() as connection:
            self._project(connection, project)
            live = [
                (row[0], row[1], json.loads(row[2]))
                for row in connection.execute(
                    "SELECT project, name, manifest FROM versions WHERE project = ? AND status != 'deleted' ORDER BY name, number",
                    (project,),
                )
            ]
            if live and not force:
                raise Protected(
                    f"project {project!r} has {len(live)} versions that are not deleted: delete them first, or force"
                )
            result = (
                self._tombstone(connection, live, reason=reason, force=True, by=by or _who())
                if live
                else {"deleted": [], "ids": [], "skipped": []}
            )
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
                connection.execute(
                    "INSERT INTO retention VALUES (?, ?, ?)", (project, rule["prefix"], json.dumps(rule))
                )
        return clean

    def _sweeps(self, connection=None) -> str | None:
        """How many blob sweeps ever ran here (publishes check their blobs again when it changed)."""
        if connection is None:
            with self._db.read() as reading:
                return Database.meta(reading, "sweeps")
        return Database.meta(connection, "sweeps")

    @staticmethod
    def _referenced(connection, projects: list[str] | None, excluded: set[str]) -> set[str]:
        """The blobs named by versions that are not deleted (of `projects`, or all), leaving out the `excluded` ids."""
        query, values = "SELECT id, manifest FROM versions WHERE status != 'deleted'", ()
        if projects is not None:
            query += f" AND project IN ({', '.join('?' for _ in projects)})"
            values = tuple(projects)
        found: set[str] = set()
        for id, manifest in connection.execute(query, values):
            if excluded and id in excluded:
                continue
            found.update(entry["sha256"] for entry in json.loads(manifest)["files"] if "sha256" in entry)
        return found

    def gc(
        self, dry_run: bool = True, grace: float | str = gc.GRACE, *, by: str = "gc", now: float | None = None
    ) -> dict:
        """Apply the retention rules, then remove the blobs no version names that are older than `grace` (seconds,
        or text like `24h`), and stale temporary uploads. `dry_run` (the default) changes nothing and says what would
        go. Safe beside concurrent publishes (gc.py says how). Returns {dry_run, grace, deleted: [refs],
        skipped: [{ref, reasons}], blobs, bytes, temporary, temporary_bytes}; a ref of a named project (a server's repo)
        says its project."""
        grace = gc.parse_duration(grace)
        clock = gc.now() if now is None else now
        report = {
            "dry_run": bool(dry_run),
            "grace": grace,
            "deleted": [],
            "skipped": [],
            "blobs": 0,
            "bytes": 0,
            "temporary": 0,
            "temporary_bytes": 0,
        }
        going: set[str] = set()
        with self._db.read() as connection:
            rules = self._rules(connection)
        for project, project_rules in rules.items():  # one transaction per project
            with self._db.read() if dry_run else self._write() as connection:
                if connection.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone() is None:
                    continue
                targets = []
                for (name,) in connection.execute(
                    "SELECT name FROM artifacts WHERE project = ? ORDER BY name", (project,)
                ).fetchall():
                    rule = gc.rule_for(project_rules, name)
                    if rule is None:
                        continue
                    states = self._states(connection, project, name)
                    for number in gc.expired(
                        list(states.values()), self._aliases(connection, project, name), rule, clock
                    ):
                        targets.append((project, name, states[number]))
                if targets:
                    result = self._tombstone(
                        connection, targets, reason="retention rule", force=False, by=by, skip=True, apply=not dry_run
                    )
                    going.update(result["ids"])
                    report["deleted"] += [self._named(project, ref) for ref in result["deleted"]]
                    report["skipped"] += [
                        {**item, "ref": self._named(project, item["ref"])} for item in result["skipped"]
                    ]
        cutoff = clock - grace
        with self._db.read() as connection:
            generations = dict(connection.execute("SELECT name, generation FROM projects").fetchall())
            referenced = self._referenced(connection, None, going if dry_run else set())
        referenced |= outbox_blobs(self.root)  # versions on their way to the remote: never garbage
        found = gc.candidates(self._blobs.blobs_name, referenced, cutoff)
        temporaries = gc.stale_temporaries(
            self._blobs.tmp_name, clock - max(grace, gc.STALE_UPLOAD)
        )  # never a live upload
        if dry_run:
            report.update(
                blobs=len(found),
                bytes=sum(size for _, _, size in found),
                temporary=len(temporaries),
                temporary_bytes=sum(size for _, size in temporaries),
            )
            return report
        with self._write() as connection:  # the sweep: serialized with every publish's own check of its blobs
            connection.execute(
                "INSERT INTO meta VALUES ('sweeps', '1') ON CONFLICT (key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
            )
            changed = [
                name
                for name, generation in connection.execute("SELECT name, generation FROM projects")
                if generations.get(name) != generation
            ]
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
