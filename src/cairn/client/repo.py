"""The project's repo, as everything above it uses it: one interface (`ProjectRepo`), two compositions.

    StandaloneRepo   a repo without a remote: its one project in its LocalRepo (a LocalProject, store/project.py) is
                     the source of truth
    SyncedRepo       a repo with a remote: the remote (`<server>/<project>`, client/remote.py) is the source of truth;
                     the repo keeps the versions on their way there (the outbox, store/outbox.py, sent by the sync
                     engine, client/sync.py) and copies of what it read (its database, or nothing on a network
                     filesystem)

A repo holds one project, so nothing here names a project. client/open.py decides which composition a project gets;
nothing above this module asks which it has. Both offer the repo interface's reads and changes (store/interface.py),
plus what the object API, the CLI, the report builder, runs, and the UI need on top of it, with the same meaning in
both:

    resolve(ref, offline, warn) -> (name, manifest)     the completed version a reference names (store/refs.py parses it)
    find(ref, offline, warn) -> (name, manifest)        the version it names, whatever its status (an id or
                                                        `name@v<n>`; aliases and labels name completed ones)
    ids(prefix, name) -> [candidates]                   the versions whose id starts with a prefix (refs.pick)
    short_ids(ids) -> {id: short id}                    the shortest unique prefixes (of at least 7 digits)
    local_folder(...), pull(...)                        read-only folders
    publish_folder(...) -> manifest                     stage a folder into the repo, then publish it
    start_version / add_files / set_metadata / complete_version / fail_version / beat     a running version, by id
    sync(), pending(), status()                         what waits for the remote (nothing without one)
    verify(ref, repair), prune(older_than, max_size, dry_run)    maintenance
    live                                                whether running versions are synced while they run
    own                                                 whether this is the project's own repo (open.py): only then
                                                        do pulls record inputs of the process's run

A version the source of truth has not numbered yet (pending) has `version` None and `pending` True in its manifest;
its id (or a unique prefix of it) names it.

With a remote, reads ask the remote and copy what they get into the repo (the repo is the remote's cache), and add
this repo's pending versions (outbox entries: an id, no number). When the remote does not answer, they answer from
the repo: its copies, its mirrored aliases, and its pending versions, with a warning that names what was used. An
exact reference the repo has (`name@v<n>` of a copy, a whole id) never asks the remote. Changes to the source of
truth (aliases, deletions, retention) go to the remote and are then mirrored into the repo; while the remote does not
answer they fail with a message that says so. Every write goes to the repo first (the outbox) and is sent at once
when the remote answers, else by a background uploader, the next command, or `cairn sync`.

Before its remote ever answered, a repo may hold versions it numbered itself (it had no remote then). Their numbers
mean nothing to the remote, so they count as pending (an id, no number) until the sync engine has sent them.
"""

from __future__ import annotations

import atexit
import sqlite3
from contextlib import contextmanager
import sys
import threading
import time
from pathlib import Path

from ..ranges import byte_range
from ..store import refs
from ..store.blobs import CHUNK, BlobStore, blob_path, merged_files, now, resolve_entry
from ..store.digest import manifest_digest
from ..store.errors import BadRange, Deleted, Forbidden, MissingBlobs, NotFound, StoreError, Unauthorized, Unreachable
from ..store.folders import cached, hostname, inside, materialize_version, pull as pull_local
from ..store.hashes import HashCache
from ..store.ids import is_id, new_id
from ..store.inputs import drop_inputs, record_input
from ..store.outbox import Outbox, exact_ref, manifest_of, usable
from ..store.project import LocalProject
from ..store.prune import prune as prune_folders
from ..store.refs import Ref
from ..store.verify import (
    Report,
    check_blobs,
    verify_copies,
    verify_folders,
    verify_outbox,
    verify_store,
    verify_version,
)
from ..store.local import FileRead, repo_id
from ..term import hint, note, reason, short_path
from .http import budget, entries_for, scan, transferring
from .remote import Remote
from .sync import NotRecorded, Sync, Waiting

FINAL = ("completed", "failed", "deleted")


def _warn(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _alive(name: str, manifest: dict) -> dict:
    if manifest.get("status") == "deleted":
        raise Deleted(
            f"{exact_ref(name, manifest)} was deleted ({(manifest.get('deleted_at') or '')[:19]})"
            + (f": {manifest['reason']}" if manifest.get("reason") else "")
        )
    return manifest


class ProjectRepo:
    """What both compositions share: staging a folder into the repo's blobs (see the module doc for the interface)."""

    root: Path
    live = False  # running versions are synced while they run (a remote's UI shows them live)
    url: str | None = None  # the remote's address, `<server>/<project>` (None without one)
    own = True  # the project's own repo (open.py): its pulls are inputs of the process's run
    where = ""  # where the repo's versions are looked for, for messages (" in the repo ...", " on the remote ...")

    def stage(
        self, folder: Path, exclude, project_root: Path, link: bool = False
    ) -> tuple[list[dict], dict[str, Path]]:
        """Manifest entries for a folder (its inputs record excluded), each blob kept in the repo: a copy-on-write clone,
        else a copy (`link`: a hard link where no clone is possible, for a folder deleted right after: a published
        draft), hashed again there, so a file that changes after this returns does not change the version.

        Returns (entries, {sha: source file}) for `restage`."""
        cache = HashCache.of(self.root, project_root)
        files = scan(Path(folder), "", project_root, exclude)
        drop_inputs(files)
        entries, _ = entries_for(files, cache)
        blobs = BlobStore(self.root)
        kept, sources = [], {}
        for entry in entries:
            if "sha256" in entry:
                source = files[entry["path"]]["file"]
                before = entry["sha256"]
                if not blobs.stage_file(entry, source, link):
                    continue
                if entry["sha256"] != before:
                    cache.forget(source)
                sources[entry["sha256"]] = source
            kept.append(entry)
        cache.save()
        return kept, sources

    def restage(self, entries: list[dict], sources: dict[str, Path]) -> None:
        """After a version naming the entries was recorded: any blob that went missing in between is kept again."""
        blobs = BlobStore(self.root)
        for entry in entries:
            if "sha256" in entry and not blob_path(self.root, entry["sha256"]).is_file():
                sha = entry["sha256"]
                if not blobs.stage_file(entry, sources[sha]) or entry["sha256"] != sha:
                    raise StoreError(f"{sources[sha]} changed while it was published; publish again")

    def same_as(self, other) -> bool:
        """Whether `other` is the same repo (with the same remote)."""
        return (
            type(other) is type(self)
            and other.url == self.url
            and Path(other.root).resolve() == Path(self.root).resolve()
        )

    def local_folder(self, name: str, version: dict) -> Path:
        """The read-only folder of a resolved version in the repo (`versions/<id>/files/`, made once)."""
        return materialize_version(self, name, version, self.root)

    def short_id(self, id: str) -> str:
        """The short id of a version for a message (`short_ids`; the first 7 digits when it cannot be computed)."""
        try:
            return self.short_ids([id]).get(id) or id[: refs.SHORT]
        except (StoreError, ValueError):
            return id[: refs.SHORT]

    def use_token(self, token: str) -> None:
        """Talk to the remote with this token (nothing without a remote)."""

    def resume(self) -> None:
        """Send what waits for the remote in the background (nothing without a remote, or when nothing waits)."""


# ---------------------------------------------------------------------- without a remote


def _forward(name: str):
    def method(self, *args, **kwargs):
        return getattr(self.local, name)(*args, **kwargs)

    method.__name__ = name
    method.__doc__ = f"`LocalProject.{name}`: the repo is the source of truth."
    return method


class StandaloneRepo(ProjectRepo):
    """A repo without a remote: its one project (a LocalProject of its LocalRepo) numbers, aliases, and deletes."""

    def __init__(self, local: LocalProject):
        self.local = local
        self.root = local.root
        local.create()  # the repo's own project exists from the start (an empty listing, not an unknown project)

    def __repr__(self) -> str:
        return f"<cairn.StandaloneRepo {self.root}>"

    @property
    def id(self) -> str:
        return self.local.id

    @property
    def where(self) -> str:
        return f" in {self.local.where}" if self.local.where else ""

    def resolve(self, ref: Ref | str, offline: bool = False, warn=None) -> tuple[str, dict]:
        return self.local.resolve(ref)

    def find(self, ref: Ref | str, offline: bool = False, warn=None) -> tuple[str, dict]:
        return self.local.find(ref)

    def pull(self, ref: str, record: bool = True, offline: bool = False) -> Path:
        return pull_local(self.local, ref, record and self.own)

    def short_ids(self, ids, offline: bool = False) -> dict[str, str]:
        """{id: short id} in the project (the repo is the source of truth: `offline` changes nothing)."""
        return self.local.short_ids(ids)

    # ------------------------------------------------------------------ writing

    def publish_folder(
        self,
        name: str,
        folder: Path,
        *,
        exclude=(),
        project_root: Path,
        link: bool = False,
        label: str | None = None,
        metadata: dict | None = None,
        inputs=(),
        aliases=(),
        source: dict | None = None,
        id: str | None = None,
    ) -> dict:
        """Stage the folder and publish it as the next version (none when nothing changed: the latest is returned)."""
        for attempt in range(3):
            entries, _ = self.stage(folder, exclude, project_root, link)
            try:
                number = self.local.publish(
                    name,
                    entries,
                    label=label,
                    metadata=metadata,
                    inputs=inputs,
                    aliases=aliases,
                    source=source,
                    id=id,
                )
                break
            except MissingBlobs:  # garbage collection removed a blob since staging: stage it again
                if attempt == 2:
                    raise
        return self.local.get_version(name, number)

    def start_version(
        self,
        name: str,
        label: str | None = None,
        metadata: dict | None = None,
        source: dict | None = None,
        id: str | None = None,
    ) -> dict:
        id = id or new_id()
        self.local.beat(id)  # before the version exists: a crash right after still leaves one that can be failed
        number = self.local.start_version(name, label, metadata, source, id=id)
        return self.local.get_version(name, number)

    def add_files(self, name: str, id: str, entries: list[dict]) -> None:
        if entries:
            self.local.add_files(name, id, entries)

    def set_metadata(self, name: str, id: str, metadata: dict) -> None:
        self.local.set_metadata(name, id, metadata)

    def complete_version(self, name: str, id: str, inputs=(), aliases=()) -> dict:
        self.local.complete_version(name, id, list(inputs))
        number = self.local.get_version(name, id)["version"]
        for alias in aliases:
            self.local.set_alias(name, alias, number)
        return self.local.get_version(name, id)

    def fail_version(self, name: str, id: str, error: str | None = None) -> None:
        if self.local.get_version(name, id)["status"] == "running":
            self.local.fail_version(name, id, error)

    def beat(self, id: str) -> None:
        self.local.beat(id)

    # ------------------------------------------------------------------ waiting for a remote: nothing

    def sync(self, report=None, refresh: bool = True, deadline: float | None = None) -> dict:
        return {
            "remote": None,
            "sent": [],
            "rejected": [],
            "waiting": [],
            "failed": [],
            "adopted": 0,
            "refreshed": 0,
            "lines": [],
        }

    def pending(self) -> list[dict]:
        return []

    def status(self) -> dict:
        return {"remote": None, "entries": [], "reaped": list(self.local.reaped)}

    # ------------------------------------------------------------------ maintenance

    def verify(self, ref: str | None = None, repair: bool = False) -> Report:
        if ref:
            name, version = self.find(ref)
            return verify_version(self.root, exact_ref(name, version), version, own=True, repair=False)
        report = verify_store(self.local.repo)  # its blobs too, never repaired: they are the repo's own versions
        return report.merge(verify_folders(self.root, repair))

    def prune(self, older_than: float | None = None, max_size: int | None = None, dry_run: bool = False) -> dict:
        return prune_folders(self.root, older_than, max_size, dry_run, keep=None)


for _name in (
    "record",
    "summaries",
    "summaries_stamped",
    "summary",
    "artifacts",
    "versions",
    "get_version",
    "aliases",
    "ids",
    "lineage",
    "read_file",
    "blob_file",
    "blob_path",
    "set_alias",
    "delete_version",
    "delete_artifact",
    "retention",
    "set_retention",
    "gc",
    "export_metadata",
):
    setattr(StandaloneRepo, _name, _forward(_name))
del _name


# ---------------------------------------------------------------------- with a remote


class NoCopies:
    """The copies of a repo that has no database (it is on a network filesystem): it keeps none, so what the remote
    does not answer cannot be read here."""

    reaped: list = []

    def _none(self, *args, **kwargs):
        raise Unreachable(
            "this repo keeps no copies of the remote's versions (it has no database: it is on a network "
            "filesystem), so only the remote can answer"
        )

    record = summaries = summary = versions = get_version = aliases = resolve = find = ids = lineage = _none

    def short_ids(self, ids) -> dict:
        return {}

    def put_copy(self, name, manifest, origin) -> bool:
        return False

    def mirror_aliases(self, name, aliases) -> None:
        return None

    def meta(self, key):
        return None

    def set_meta(self, key, value) -> None:
        return None

    def local_versions(self) -> list:
        return []

    def forget_versions(self, ids) -> int:
        return 0

    def copies(self) -> list:
        return []


def masked(manifest: dict) -> dict:
    """A version of the repo database as a repo with a remote shows it: a copy as it is; a version the repo numbered
    itself, before it had a remote, as pending (its number means nothing to the remote)."""
    if manifest.get("origin"):
        return manifest
    status = "pending" if manifest.get("status") == "completed" else manifest.get("status")
    return {**manifest, "version": None, "status": status, "pending": True, "sync": "pending"}


class SyncedRepo(ProjectRepo):
    """A repo with a remote (see the module doc). `copies` is the repo's own project in its database (a LocalProject)
    or NoCopies."""

    live = True

    def __init__(self, remote: Remote, root: Path | str, copies, heartbeat_timeout: float):
        self.remote, self.copies = remote, copies
        self.root = Path(root)
        self.own_id = repo_id(self.root)  # repo.json, also without a database (a network filesystem)
        self.url = remote.url
        self.heartbeat_timeout = heartbeat_timeout
        self.outbox = Outbox(self.root)
        self.engine = Sync(self.outbox, remote, copies, self.root, heartbeat_timeout)
        self.quiet_pending = False  # `cairn publish` says itself why a version is pending (on stdout, with the reason)
        self.pending_reasons: dict[str, str] = {}  # id -> why it stayed pending
        self._down_until = 0.0  # the remote did not answer: reads and background sends skip it until then
        self._quick: SyncedRepo | None = None  # this repo with short request timeouts (`quick`)
        self._reached = False  # reach() looked at the remote once (and adopted what the repo numbered itself)
        self.ask = _Guarded(self)

    def __repr__(self) -> str:
        return f"<cairn.SyncedRepo {self.root} remote {self.url}>"

    @property
    def where(self) -> str:
        return f" on the remote {self.url}"

    @property
    def id(self) -> str | None:
        """The remote's id (the source of truth): the one the repo remembers, else asked (once)."""
        known = self.engine.known_id
        if known:
            return known
        try:
            return self.engine.check_remote()
        except Unreachable:
            return None

    def with_timeout(self, timeout: float) -> "SyncedRepo":
        """The same repo with a remote that gives up after `timeout` seconds (attempts that must not hold up a command)."""
        from .http import Client

        fast = Client.__new__(Client)
        fast.__dict__.update(self.remote.client.__dict__)
        fast.timeout = timeout
        remote = Remote(fast, self.remote.project)
        remote._id = self.remote._id
        repo = SyncedRepo(remote, self.root, self.copies, self.heartbeat_timeout)
        repo.own = self.own
        return repo

    # ------------------------------------------------------------------ copies

    def copy(self, name: str, manifest: dict) -> None:
        """Record a remote's answer in the repo (a copy). Never fails a read: the repo is a cache."""
        if manifest.get("pending") or manifest.get("status") not in FINAL:
            return
        origin = self.engine.origin()
        if origin is None:
            return
        try:
            self.copies.put_copy(name, manifest, origin)
        except (StoreError, ValueError, sqlite3.Error, OSError) as error:
            _warn(f"warning: could not record {exact_ref(name, manifest)} in the repo ({error})")

    def mirror(self, name: str, aliases: dict[str, int] | None = None) -> None:
        """Mirror the remote's aliases of an artifact into the repo (asked now unless given). Never raises."""
        try:
            self.engine.mirror(name, aliases)
        except (StoreError, ValueError, sqlite3.Error, OSError):
            pass

    def _copied(self, name: str, version: int | str) -> dict | None:
        """The repo's own record of an exact version (a copy by number; by id also a version it numbered itself,
        shown pending), else None."""
        try:
            state = self.copies.get_version(name, version)
        except (StoreError, ValueError):
            return None
        if isinstance(version, int) and not state.get("origin"):
            return None  # this repo's own number, not the remote's
        return masked(state)

    def _known_id(self, id: str) -> tuple[str, dict] | None:
        """(name, manifest) of a version by its whole id, from what this repo has (never asking the remote): its
        outbox, its database, or a read-only folder a pull made."""
        record = self.outbox.load(id)
        if record is not None:
            return record["name"], manifest_of(record)
        try:
            name, state = self.copies.find(Ref.of(id))
            return name, masked(state)
        except (StoreError, ValueError):
            pass
        found = cached(self.root, id)
        if found and found.get("name") and (found.get("manifest") or {}).get("version") is not None:
            return found["name"], found["manifest"]
        return None

    # ------------------------------------------------------------------ this repo's pending versions

    def records(self) -> list[dict]:
        """This remote's outbox entries, oldest first (by `created`)."""
        return self.engine.records()

    def _pending_of(self, name: str) -> list[dict]:
        return [record for record in self.records() if record["name"] == name]

    def _pending_match(self, name: str, word: str) -> dict | None:
        """The usable pending version that `name@word` (an alias, a label, `latest`) names: of those, the one sent last
        (the outbox's order, `Sync.ordered`), which will arrive last and so get the highest number."""
        found = [
            record
            for record in self._pending_of(name)
            if usable(record) and (word == "latest" or word in record.get("aliases", []) or record.get("label") == word)
        ]
        return self.engine.ordered(found)[-1] if found else None

    def _entry(self, name: str, id: str) -> dict | None:
        record = self.outbox.load(id)
        return record if record and record["name"] == name else None

    # ------------------------------------------------------------------ the project and listings

    def _fallback(self, call, local):
        """`call()` against the remote; while it does not answer, `local()` from the repo's copies."""
        try:
            return call()
        except Unreachable:
            return local()

    def record(self) -> dict:
        """The project's record (the remote's; the repo's while the remote does not answer, or before it has the
        project)."""

        def made() -> dict:
            return {
                "name": self.remote.project,
                "title": self.remote.project,
                "description": "",
                "created": min((record["created"] for record in self.records()), default=now()),
            }

        try:
            return {
                **self._fallback(self.ask.record, lambda: {**self.copies.record(), "name": self.remote.project}),
                "name": self.remote.project,
            }
        except (NotFound, Unreachable):
            return made()

    def _waiting_summary(self, name: str, records: list[dict]) -> dict:
        newest = self.engine.ordered(records)[-1]
        return {
            "name": name,
            "latest": None,
            "versions": len(records),
            "aliases": {},
            "updated": newest.get("completed") or newest["created"],
            "metadata": newest.get("metadata") or {},
            "pending": len(records),
        }

    def summaries(self) -> list[dict]:
        def local():
            try:
                return self.copies.summaries()
            except NotFound:
                return []

        try:
            found = list(self._fallback(self.ask.summaries, local))
        except NotFound:  # the remote has no such project yet: what the outbox holds of it, if anything
            found = []
        except Unreachable:  # no copies to answer from (no database): the outbox is still listed
            if not self.records():
                raise
            found = []
        known = {item["name"] for item in found}
        waiting: dict[str, list[dict]] = {}
        for record in self.records():
            if record["name"] not in known:
                waiting.setdefault(record["name"], []).append(record)
        return found + [self._waiting_summary(name, records) for name, records in sorted(waiting.items())]

    def summary(self, name: str) -> dict:
        try:
            return self._fallback(lambda: self.ask.summary(name), lambda: self.copies.summary(name))
        except NotFound:
            records = self._pending_of(name)
            if not records:
                raise
            return self._waiting_summary(name, records)

    def artifacts(self) -> list[str]:
        return [item["name"] for item in self.summaries()]

    def versions(self, name: str) -> list[dict]:
        """Every version, by number (the remote's, else the repo's copies), then this repo's pending ones (number None),
        oldest first by `created`."""
        down = None
        try:
            found = self.ask.versions(name)
        except NotFound:
            found = []
        except Unreachable:
            try:
                found = sorted(
                    (masked(state) for state in self.copies.versions(name)),
                    key=lambda state: (state["version"] is None, state["version"] or 0, state.get("created") or ""),
                )
            except NotFound:
                found = []
            except Unreachable as error:
                found, down = [], error
        known = {state["id"] for state in found}
        found += [manifest_of(record) for record in self._pending_of(name) if record["id"] not in known]
        if not found and down is not None:
            raise down
        return found

    def get_version(self, name: str, version: int | str) -> dict:
        """One manifest by number or id, whatever its status: this repo's pending version, its copy, else the remote's
        (copied when final); a read-only folder a pull made answers for an id while the remote does not."""
        if isinstance(version, str):
            if not is_id(version):
                raise refs.InvalidRef(f"a version is a number or an id (64 hex digits), not {version!r}")
            record = self._entry(name, version)
            if record is not None:
                return manifest_of(record)
        local = self._copied(name, version)
        if local is not None and (local.get("status") in FINAL or local.get("pending")):
            return local
        try:
            manifest = self.ask.get_version(name, version)
        except Unreachable:
            if local is not None:
                return local
            known = self._known_id(version) if isinstance(version, str) else None
            if known is not None and known[0] == name:
                return known[1]
            raise
        self.copy(name, manifest)
        return manifest

    def aliases(self, name: str) -> dict[str, int]:
        try:
            found = self.ask.aliases(name)
        except NotFound:
            return {}
        except Unreachable:
            try:
                return self.copies.aliases(name)
            except NotFound:
                return {}
        self.mirror(name, found)
        return found

    # ------------------------------------------------------------------ ids

    def short_ids(self, ids, offline: bool = False) -> dict[str, str]:
        """{id: short id}: unique in the project as the remote has it (asked unless `offline` or down) and among this
        repo's pending versions and copies; the longest of these wins, so it is unique in all of them."""
        ids = [id for id in dict.fromkeys(ids) if is_id(id)]
        if not ids:
            return {}
        pending = [record["id"] for record in self.records()]
        found = {id: refs.short_id(id, pending) for id in ids}
        answers = []
        try:
            answers.append(self.copies.short_ids(ids))
        except (StoreError, ValueError):
            pass
        if not offline:
            try:
                answers.append(self.ask.short_ids(ids))
            except (StoreError, ValueError):
                pass
        for answer in answers:
            for id, short in answer.items():
                if id in found and len(short) > len(found[id]):
                    found[id] = short
        return found

    def ids(self, prefix: str, name: str | None = None, offline: bool = False) -> list[dict]:
        """The versions whose id starts with `prefix`: this repo's pending ones and the remote's (the repo's copies
        while the remote does not answer, or `offline`)."""
        ref = refs.parse(prefix)
        if ref.id is None or ref.path:
            raise refs.InvalidRef(f"invalid id {prefix!r}: 4 to 64 hex digits")
        prefix = ref.id
        mine = [
            record
            for record in self.records()
            if (name is None or record["name"] == name) and record["id"].startswith(prefix)
        ]
        shorts = self.short_ids([record["id"] for record in mine], offline=True) if mine else {}
        found = {
            record["id"]: refs.candidate(record["name"], manifest_of(record), shorts[record["id"]]) for record in mine
        }

        def local() -> list[dict]:
            try:
                items = self.copies.ids(prefix, name)
            except (StoreError, ValueError):
                return []
            shown = [(item, self._copied(item["name"], item["id"])) for item in items]  # own numbers mean nothing here
            return [{**item, "version": copied["version"]} if copied else item for item, copied in shown]

        try:
            answered = local() if offline else self._fallback(lambda: self.ask.ids(prefix, name), local)
        except NotFound:
            answered = []
        for item in answered:
            found.setdefault(item["id"], item)
        return list(found.values())

    def _by_id(self, ref: Ref, offline: bool) -> tuple[str, dict]:
        """(name, manifest) of the version an id (or a unique prefix of it) names, whatever its status: a whole id the
        repo knows never asks the remote."""
        if ref.whole:
            known = self._known_id(ref.id)
            if known is not None:
                return known
        found = refs.pick(ref.id, self.ids(ref.id, None, offline), self.where)
        return found["name"], self.get_version(found["name"], found["id"])

    # ------------------------------------------------------------------ resolving

    def resolve(self, ref: Ref | str, offline: bool = False, warn=None) -> tuple[str, dict]:
        """(name, manifest) of the completed version a reference names (refs.py; a bare name: `latest`), or a pending
        one of this repo (with a warning)."""
        ref = refs.parse(ref)
        if ref.id is not None:
            name, manifest = self._by_id(ref, offline)
            _alive(name, manifest)
            if manifest.get("pending") and manifest.get("status") == "pending" and manifest.get("sync") == "pending":
                (warn or _warn)(self._pending_warning(str(ref.at(None)), name, manifest["id"]))
                return name, manifest
            if manifest.get("sync") == "rejected":
                raise StoreError(f"{ref.at(None)} was refused by the remote {self.url}: {manifest.get('error')}")
            if manifest.get("status") != "completed":
                raise StoreError(f"{exact_ref(name, manifest)} is {manifest.get('status')}, not completed{self.where}")
            return name, manifest
        return self._resolve(ref, offline, warn)

    def find(self, ref: Ref | str, offline: bool = False, warn=None) -> tuple[str, dict]:
        """(name, manifest) of the version a reference names, whatever its status for an id or `name@v<n>` (a deleted
        one as its tombstone); an alias or a label names a completed version."""
        ref = refs.parse(ref)
        if ref.id is not None:
            return self._by_id(ref, offline)
        if ref.version is None:
            return self._resolve(ref, offline, warn)
        try:
            return self._resolve(ref, offline, warn)
        except Deleted:
            pass
        except (NotFound, Unreachable):
            raise
        except StoreError:  # running or failed: not resolvable, but the version exists
            pass
        return ref.name, self.get_version(ref.name, ref.version)

    def _resolve(self, ref: Ref, offline: bool = False, warn=None) -> tuple[str, dict]:
        """`name@v<n>` or `name@<word>`: an exact version the repo has; else the remote (and the answer is copied),
        unless a pending version of this repo has that word: it is sent after every version the remote has now, so it
        will get a higher number, and `latest` is the highest number (never a clock). While the remote does not answer
        (or with `offline`), the same from the repo's copies, with a warning."""
        say = warn or _warn
        name = ref.name
        word = ref.word
        key = str(ref.at(None)) if ref.selector else f"{name}@latest"
        if ref.version is not None:
            local = self._copied(name, ref.version)
            if local is not None:
                _alive(name, local)
                if local.get("status") == "completed":
                    return name, local
        pending = None if word is None else self._pending_match(name, word)
        if not offline:
            try:
                name, version = self.ask.resolve(Ref(name, ref.selector))
            except Unreachable:
                if ref.version is not None:
                    raise Unreachable(
                        f"{self.url} not reachable, and the repo {short_path(self.root)} does not have {key}"
                    ) from None
                reason = f"{self.url} not reachable"
            except NotFound:
                if pending is None:
                    raise
                say(self._pending_warning(key, name, pending["id"]))
                return name, manifest_of(pending)
            else:
                self.copy(name, version)
                if ref.version is None:
                    self.mirror(name)
                if pending is not None:
                    say(self._pending_warning(key, name, pending["id"]))
                    return name, manifest_of(pending)
                return name, version
        else:
            if ref.version is not None:
                raise NotFound(f"offline: the repo {short_path(self.root)} does not have {key}")
            reason = "offline"
        try:
            found = masked(self.copies.resolve(Ref(name, ref.selector))[1])
        except StoreError:
            found = None
        if pending is not None:
            say(self._pending_warning(key, name, pending["id"]))
            return name, manifest_of(pending)
        if found is None:
            raise Unreachable(f"{reason}, and the repo {short_path(self.root)} has no version for {key}")
        if found.get("pending"):
            say(self._pending_warning(key, name, found["id"]))
            return name, found
        short = self.short_ids([found["id"]], offline=True).get(found["id"], found["id"][: refs.SHORT])
        say(f"warning: {reason}; using the repo's {exact_ref(name, found)} ({short}) for {key}")
        return name, found

    def lineage(self, ref: Ref | str, marks: bool = False) -> dict[str, list[str]]:
        ref = refs.parse(ref)
        record = self.outbox.load(ref.id) if ref.whole else None

        def own() -> dict:
            found = {"id": record["id"], "inputs": list(record.get("inputs") or []), "consumers": []}
            if marks:
                found.update(deleted=[], nodes={})
            return found

        if record is not None and record.get("number") is None:  # the remote does not know it yet
            return own()
        try:
            return self.ask.lineage(ref.at(None), marks=marks)
        except (Unreachable, NotFound):
            if record is not None:
                return own()
            return self.copies.lineage(ref.at(None), marks=marks)

    # ------------------------------------------------------------------ files

    def _local_manifest(self, ref: Ref) -> dict | None:
        """The manifest of an exact version (a whole id, `name@v<n>`) if the repo has it, else None."""
        if ref.whole:
            known = self._known_id(ref.id)
            return known[1] if known else None
        if ref.version is not None:
            return self._copied(ref.name, ref.version)
        return None

    def read_file(self, ref: Ref | str, range_header: str | None = None) -> FileRead:
        """A file of a version (a reference with a path): from the repo's blobs when it has the version and the
        contents, else from the remote."""
        ref = refs.parse(ref)
        if not ref.path:
            raise refs.InvalidRef(f"{ref} names a version, not a file in it (add //<path>)")
        manifest = self._local_manifest(ref)
        if manifest is not None and manifest.get("status") != "deleted":
            entry = resolve_entry(manifest["files"], ref.path)
            blob = blob_path(self.root, entry["sha256"]) if entry else None
            if blob is not None and blob.is_file():
                total = blob.stat().st_size
                wanted = byte_range(range_header, total)
                if wanted is False:
                    raise BadRange(total)
                first, last = wanted or (0, total - 1)
                immutable = manifest.get("status") == "completed" and not manifest.get("pending")
                return FileRead(total, first, last, entry["sha256"], immutable, _chunks(blob, first, last))
            if manifest.get("pending"):
                raise NotFound(f"{ref.at(None)} has no file {ref.path} in the repo {self.root}")
        return self.ask.read_file(ref, range_header)

    def blob_file(self, name: str, version: int | str | None, entry: dict, root) -> Path:
        """The entry's contents in the repo's blobs: already there (a pending version's always are), else downloaded
        (`version`: a number or an id)."""
        target = blob_path(root, entry["sha256"])
        if target.is_file():
            return target
        if version is None:
            raise StoreError(f"{name}: the contents of {entry['path']} ({entry['sha256'][:12]}) left the repo {root}")
        return self.ask.blob_file(name, version, entry, root)

    def pull(self, ref: str, record: bool = True, offline: bool = False) -> Path:
        """A read-only local copy (or of the file after `//`), its version recorded as an input (by id)."""
        parsed = refs.parse(ref)
        name, version = self.resolve(parsed, offline)
        folder = self.local_folder(name, version)
        exact = exact_ref(name, version)
        path = inside(folder, parsed, exact)
        if record and self.own:
            record_input(version["id"], exact)
        return path

    # ------------------------------------------------------------------ changes to the source of truth

    def _changes(self, call, what: str):
        """`call()` against the remote; while it does not answer, a clear refusal (it changes the source of truth)."""
        try:
            return call()
        except Unreachable as error:
            raise Unreachable(f"{what} needs the remote (the source of truth): {error}") from None

    def set_alias(self, name: str, alias: str, version: int) -> None:
        refs.new_alias(alias)
        self._changes(lambda: self.remote.set_alias(name, alias, version), f"setting the alias {alias}")
        self.mirror(name)

    def _mirror_versions(self, names) -> None:
        for name in names:
            try:
                for manifest in self.remote.versions(name):
                    self.copy(name, manifest)
            except StoreError:
                continue
            self.mirror(name)

    def delete_version(self, name: str, version: int | str, *, reason: str | None = None, force: bool = False) -> dict:
        what = refs.exact(name, version) if isinstance(version, int) else f"{name} ({version[: refs.SHORT]})"
        result = self._changes(
            lambda: self.remote.delete_version(name, version, reason=reason, force=force), f"deleting {what}"
        )
        self._mirror_versions([name])
        return result

    def delete_artifact(self, name: str, *, reason: str | None = None, force: bool = False) -> dict:
        result = self._changes(
            lambda: self.remote.delete_artifact(name, reason=reason, force=force), f"deleting {name}"
        )
        self._mirror_versions([name])
        return result

    def retention(self) -> list[dict]:
        return self.remote.retention()

    def set_retention(self, rules: list[dict]) -> list[dict]:
        return self._changes(lambda: self.remote.set_retention(rules), "changing retention rules")

    def gc(self, dry_run: bool = True, grace=None) -> dict:
        return self._changes(lambda: self.remote.gc(dry_run=dry_run, grace=grace), "garbage collection")

    # ------------------------------------------------------------------ writing: the outbox first

    def _input_ids(self, inputs) -> list[str]:
        """Inputs as an entry records them: the whole id of the version each reference names, resolved now (what the
        job read: the remote when it answers, else the repo). A reference that names no version here is refused: a
        stored input is always a whole id, never a short id or a number. Only a whole id may name a running version
        (the run a job's outputs come from); any other reference must name a completed one (as LocalRepo checks)."""
        found: list[str] = []
        for item in inputs or []:
            ref = refs.parse(item)
            if ref.whole:
                id = ref.id
            else:
                id = self.resolve(ref, warn=lambda line: None)[1]["id"]
            if id not in found:
                found.append(id)
        return found

    def _new_entry(
        self,
        kind: str,
        name: str,
        *,
        files=(),
        label=None,
        metadata=None,
        inputs=(),
        aliases=(),
        source=None,
        id: str | None = None,
    ) -> dict:
        """Commit a new outbox entry (durably, before anything is sent). Its name, label, and aliases are checked here,
        as the remote checks them (refs.py), so a version the remote would refuse never waits in the outbox."""
        import os

        refs.valid_name(name)
        if label is not None:
            refs.new_label(label)
        for alias in aliases:
            refs.new_alias(alias)
            if alias == label:
                raise refs.InvalidRef(
                    f"{alias!r} is both the label and an alias: an alias and a label never share a word"
                )
        running = kind == "running"
        record = {
            "id": id or new_id(),
            "kind": kind,
            "sync": "running" if running else "pending",
            "status": "running" if running else "completed",
            "url": self.url,
            "store": self.engine.known_id,
            "name": name,
            "label": label,
            "metadata": metadata or {},
            "inputs": self._input_ids(inputs),
            "aliases": list(aliases),
            "source": source,
            "files": list(files),
            "created": now(),
            "owner": {"host": hostname(), "pid": os.getpid()},
            "number": None,
            "pushed": {},
            "error": None,
        }
        if not running:
            record["digest"] = manifest_digest(record["files"])  # known before anything is uploaded
            record["completed"] = record["created"]
        self.outbox.create(record, running)
        return record

    def _pending_warning(self, key: str, name: str, id: str) -> str:
        short = self.short_ids([id], offline=True).get(id, id[: refs.SHORT])
        return f"warning: {key} is {name} ({short}), pending here (the remote does not have it yet)"

    def _settled(self, record: dict) -> dict:
        """The manifest of an entry that was just sent (or not): the remote's numbered version, else the pending one."""
        name, id = record["name"], record["id"]
        current = self._entry(name, id)
        if current is not None:
            return manifest_of(current)
        try:
            return self.get_version(name, id)
        except NotFound:  # nothing changed since the latest: the remote kept its own version, not this id
            if record.get("number") is not None:
                return self.get_version(name, record["number"])
            return self.resolve(Ref(name, None), warn=lambda line: None)[1]

    def down(self) -> bool:
        """Whether the remote was slow to fail a moment ago (DOWN_FOR seconds), in this process: then it is not asked for
        reads and sends that would hold up the caller (a refused connection fails at once and is always tried again)."""
        return time.monotonic() < max(self._down_until, _DOWN.get(self.url, 0.0))

    @property
    def quick(self) -> "SyncedRepo":
        """This repo with a remote that gives up after QUICK seconds per request: what a process sends and reads while it
        works (a run, a publish, the UI), so a remote that stops answering costs it seconds, not minutes. `sync()` and
        the background uploader take their time."""
        if self._quick is None:
            self._quick = self.with_timeout(QUICK)
            self._quick._quick = self._quick
        self._quick.remote._id = self._quick.remote._id or self.remote._id
        self._quick.remote.client.token = self.remote.client.token
        return self._quick

    def reach(self) -> None:
        """Before the first request of this process: a quick look whether the remote answers at all (PROBE seconds), so a
        host that hangs makes one short wait, not one long one per request. Raises Unreachable, also while the remote
        is down (`down`)."""
        if self.down():
            raise Unreachable(f"{self.url} not reachable (a moment ago)")
        if self._reached:
            return
        began = time.monotonic()
        try:
            if self.remote._id is None:
                self.remote._id = self.with_timeout(PROBE).remote.id
            self.engine.check_remote()  # this repo's remote, not another one, not the repo itself (StoreError)
            if self.copies.local_versions():  # versions numbered before the repo had a remote: sent first
                self._reached = True  # (the flush below asks the remote itself)
                try:
                    self.engine.flush()
                finally:
                    self._reached = False
        except Unreachable:
            self._failed(began)
            raise
        self._reached = True

    def _failed(self, began: float) -> None:
        """The remote did not answer: if finding that out took long (a host that drops packets), skip it for a while (every
        repo of this process does)."""
        if time.monotonic() - began >= SLOW:
            self._down_until = _DOWN[self.url] = time.monotonic() + DOWN_FOR

    @contextmanager
    def _waiting(self):
        """What a process sends or reads now waits for the remote at most SEND_BUDGET seconds in all (unless the command
        or the run's phase set a budget already, `http.budget`); a remote that took SLOW seconds or more, even if it
        answered, is left to the background for DOWN_FOR seconds (`_failed`): a slow remote never slows a run."""
        began, moved = time.monotonic(), transferring()
        with budget(SEND_BUDGET, default=True):
            yield
        self._failed(began + transferring() - moved)  # the time spent waiting, not moving bytes

    def _send_now(self, record: dict) -> dict:
        """Send an entry that is final now; when the remote does not answer it stays pending (a warning says so, and a
        background uploader keeps trying). Returns the version's manifest."""
        with self._waiting():
            return self._send(record)

    def _send(self, record: dict) -> dict:
        began = time.monotonic()
        try:
            self.reach()
            self.quick.engine.send(record)
        except Waiting as error:
            if isinstance(
                error.__cause__, (Unauthorized, Forbidden)
            ):  # the caller should know at once; it stays pending
                raise type(error.__cause__)(
                    f"{error.__cause__}; {record['name']} ({self.engine.short(record['id'])}) "
                    "stays pending until a token that may write sends it"
                ) from None
            self.pending_reasons[record["id"]] = reason(error)
            if not self.quiet_pending:
                _warn(f"pending {record['name']} ({self.engine.short(record['id'])}): {reason(error)}")
            start_uploader(self)
        except Unreachable as error:
            if not isinstance(error, NotRecorded):
                self._failed(began)
            what = "failed" if record["status"] == "failed" else "pending"
            self.pending_reasons[record["id"]] = reason(error)
            if not self.quiet_pending:
                _warn(
                    f"{what} {record['name']} ({self.engine.short(record['id'])}): {reason(error)}\n"
                    + hint("run `cairn sync` to send it now", sys.stderr)
                )
            start_uploader(self)
        return self._settled(record)

    def publish_folder(
        self,
        name: str,
        folder: Path,
        *,
        exclude=(),
        project_root: Path,
        link: bool = False,
        label: str | None = None,
        metadata: dict | None = None,
        inputs=(),
        aliases=(),
        source: dict | None = None,
        id: str | None = None,
    ) -> dict:
        """Stage the folder, commit it to the outbox, and send it now if the remote answers (else it is pending)."""
        entries, sources = self.stage(folder, exclude, project_root, link)
        with self._waiting():
            same = None if aliases else self._unchanged(name, entries, metadata, inputs)
            if same is not None:  # like a repo without a remote: nothing changed since the latest, so no new version
                return same
            record = self._new_entry(
                "publish",
                name,
                files=entries,
                label=label,
                metadata=metadata,
                inputs=inputs,
                aliases=aliases,
                source=source,
                id=id,
            )
            self.restage(entries, sources)
            return self._send(record)

    def _unchanged(self, name: str, entries: list[dict], metadata, inputs) -> dict | None:
        """The latest version (the remote's, else what the repo knows) when it has these files, metadata, and inputs."""
        try:
            latest = self._resolve(Ref(name, None), warn=lambda line: None)[1]
        except StoreError:
            return None
        same = (
            (latest.get("digest") or manifest_digest(latest.get("files") or [])) == manifest_digest(entries)
            and (latest.get("metadata") or {}) == (metadata or {})
            and sorted(latest.get("inputs") or []) == sorted(self._input_ids(inputs))
        )
        return latest if same else None

    def _running(self, name: str, id: str) -> dict:
        """The outbox entry of a running version of this process (refused once a sender failed it: its heartbeat
        expired, so the job lost it)."""
        record = self._entry(name, id)
        if record is None:
            raise StoreError(f"{name} ({id[: refs.SHORT]}) is not a running version of the repo {self.root}")
        if record["status"] != "running":
            what = record.get("error") or record["status"]
            raise StoreError(
                f"{name} ({self.engine.short(id)}) was {record['status']} meanwhile ({what}); publish the folder again"
            )
        return record

    def _try(self, record: dict) -> None:
        """Send what is new of a running entry now; while the remote does not answer, it waits in the outbox."""
        with self._waiting():
            try:
                self.reach()
                self.quick.engine.send(record, claim=False)
            except Unreachable:  # also NotRecorded: the next attempt records it
                pass

    def start_version(
        self,
        name: str,
        label: str | None = None,
        metadata: dict | None = None,
        source: dict | None = None,
        id: str | None = None,
    ) -> dict:
        record = self._new_entry("running", name, label=label, metadata=metadata, source=source, id=id)
        self._try(record)
        return manifest_of(self.outbox.load(record["id"]) or record)

    def add_files(self, name: str, id: str, entries: list[dict]) -> None:
        record = self._running(name, id)
        record["files"] = merged_files(record["files"], entries)  # removals too: `push` sends them
        self.outbox.save(record)
        self.outbox.beat(id)
        self._try(record)

    def set_metadata(self, name: str, id: str, metadata: dict) -> None:
        record = self._running(name, id)
        record["metadata"] = metadata
        self.outbox.save(record)
        self._try(record)

    def complete_version(self, name: str, id: str, inputs=(), aliases=()) -> dict:
        record = self._running(name, id)
        for alias in aliases:
            refs.new_alias(alias)
        record.update(
            sync="pending",
            status="completed",
            inputs=self._input_ids(inputs),
            aliases=list(aliases),
            digest=manifest_digest(record["files"]),
            completed=now(),
        )
        self.outbox.save(record)
        return self._send_now(record)

    def fail_version(self, name: str, id: str, error: str | None = None) -> None:
        record = self._entry(name, id)
        if record is None or record["status"] != "running":
            return
        record.update(sync="pending", status="failed", completed=now(), error=error)
        self.outbox.save(record)
        self._send_now(record)

    def beat(self, id: str) -> None:
        self.outbox.beat(id)

    def use_token(self, token: str) -> None:
        self.remote.client.token = token
        self.remote.client.token_source = "the caller"

    def resume(self) -> None:
        if any(record["sync"] == "pending" for record in self.records()):
            start_uploader(self, first_delay=0)

    # ------------------------------------------------------------------ what waits for the remote

    def sync(self, report=None, refresh: bool = True, deadline: float | None = None) -> dict:
        """Send the outbox now (the sync engine; until `deadline`, a time.monotonic() value, if given), then (`refresh`)
        mirror the remote's aliases of the artifacts the repo has versions of (`refreshed`: how many; skipped while the
        remote does not answer). `report(line)` hears what happened."""
        result = self.engine.flush(deadline)
        result.update(remote=self.url, refreshed=0)
        if refresh and not [record for record in result["waiting"] if record["sync"] != "running"]:
            try:
                result["refreshed"] = self.engine.refresh()
            except StoreError:
                pass
        if report:
            for line in result["lines"]:
                report(line)
        return result

    def pending(self) -> list[dict]:
        """This repo's outbox entries (pending, running, rejected)."""
        return self.records()

    def status(self) -> dict:
        entries = [
            {**record, "heartbeat": self.outbox.last_beat(record)} if record["sync"] == "running" else record
            for record in self.records()
        ]
        sent = {record["id"] for record in entries}
        for name, state in self.copies.local_versions():  # numbered here before the repo had a remote: not sent yet
            if state["id"] not in sent and state["status"] != "deleted":
                entries.append(
                    {
                        **state,
                        "name": name,
                        "number": None,
                        "owner": {},
                        "sync": "running" if state["status"] == "running" else "pending",
                        "error": None if state["status"] != "failed" else state.get("error"),
                    }
                )
        return {"remote": self.url, "entries": entries, "reaped": list(getattr(self.copies, "reaped", []))}

    # ------------------------------------------------------------------ maintenance

    def _keep(self) -> set[str]:
        """The blobs only this repo has: the outbox's and those of versions it numbered itself."""
        keep = self.outbox.needed()
        keep |= {
            entry["sha256"]
            for _, state in self.copies.local_versions()
            for entry in state.get("files") or []
            if "sha256" in entry
        }
        return keep

    def verify(self, ref: str | None = None, repair: bool = False) -> Report:
        if ref:
            name, version = self.find(ref)
            return verify_version(
                self.root, exact_ref(name, version), version, own=bool(version.get("pending")), repair=repair
            )
        report = Report()
        check_blobs(self.root / "blobs", report, repair=repair, keep=self._keep())
        report.merge(verify_copies(self.copies.copies()))
        report.merge(verify_folders(self.root, repair))
        return report.merge(verify_outbox(self.outbox, self.root))

    def prune(self, older_than: float | None = None, max_size: int | None = None, dry_run: bool = False) -> dict:
        return prune_folders(self.root, older_than, max_size, dry_run, keep=self._keep())


DOWN_FOR = 30.0  # seconds a remote that was slow to fail is not asked again for reads and background sends
SLOW = 2.0  # seconds: a failure that took this long counts as slow (a refused connection takes milliseconds)
PROBE = 3.0  # seconds the first look at the remote may take
QUICK = 10.0  # seconds a request a process makes while it works may wait for each answer or move of bytes (`quick`)
SEND_BUDGET = 3.0  # seconds one publish, start, or sync of a running version waits for the remote in all (`_waiting`)
_DOWN: dict[str, float] = {}  # remote URL -> until when this process does not ask it (`SyncedRepo.down`)


class _Guarded:
    """The remote as reads use it: after it was slow to fail, it is not asked again for DOWN_FOR seconds (the repo answers
    at once instead of every read waiting for the remote to time out). Explicit syncs and changes ask it anyway."""

    def __init__(self, repo: "SyncedRepo"):
        self._repo = repo

    def __getattr__(self, name: str):
        target = getattr(self._repo.quick.remote, name)

        def call(*args, **kwargs):
            repo = self._repo
            repo.reach()
            if repo.down():
                raise Unreachable(f"{repo.url} not reachable (a moment ago)")
            began = time.monotonic()
            try:
                return target(*args, **kwargs)
            except Unreachable:
                repo._failed(began)
                raise

        return call


def _chunks(blob: Path, first: int, last: int):
    with blob.open("rb") as handle:
        handle.seek(first)
        remaining = last - first + 1
        while remaining > 0:
            chunk = handle.read(min(CHUNK, remaining))
            if not chunk:
                break
            yield chunk
            remaining -= len(chunk)


# ---------------------------------------------------------------------- the background uploader

_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()
_exit_flush: dict[str, SyncedRepo] = {}
EXIT_TIMEOUT = 4.0  # seconds the last attempt at exit may wait for the remote, in all


def start_uploader(repo: SyncedRepo, first_delay: float = 1.0, max_delay: float = 60.0) -> threading.Thread:
    """A daemon thread that sends the repo's outbox until nothing waits (backing off while the remote is down), and
    one more quick attempt when the process exits. One per repo and process."""
    key = f"{repo.root}|{repo.url}"
    with _threads_lock:
        if key not in _exit_flush:
            _exit_flush[key] = repo
            if len(_exit_flush) == 1:
                atexit.register(_flush_at_exit)
        thread = _threads.get(key)
        if thread is not None and thread.is_alive():
            return thread

        def loop():
            delay = first_delay
            while True:
                time.sleep(delay)
                try:
                    result = repo.sync(report=note, refresh=False)
                    left = [
                        record for record in result["waiting"] if record["sync"] != "running"
                    ]  # running: its process sends it
                except Exception:  # noqa: BLE001 - never let the uploader die noisily; the next sync retries
                    left = [True]
                if not left:
                    return
                delay = min(delay * 2, max_delay)

        thread = threading.Thread(target=loop, name=f"cairn-uploader {repo.url}", daemon=True)
        _threads[key] = thread
        thread.start()
        return thread


def _flush_at_exit() -> None:
    """At exit: one quick attempt to send what this process left pending (the next command or `cairn sync` sends the rest)."""
    for repo in list(_exit_flush.values()):
        try:
            if not repo.down() and any(record["sync"] == "pending" for record in repo.records()):
                with budget(EXIT_TIMEOUT):  # waiting; uploads go on while bytes flow (QUICK: the stall timeout)
                    repo.quick.engine.flush()
        except Exception:  # noqa: BLE001 - never a traceback at exit
            pass
