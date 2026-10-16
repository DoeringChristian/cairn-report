"""The project's repo, as everything above it uses it: one interface (`ProjectRepo`), two compositions.

    StandaloneRepo   a repo without a remote: a LocalRepo (store/local.py) is the source of truth
    SyncedRepo       a repo with a remote: the remote (client/remote.py) is the source of truth; the repo keeps the
                     versions on their way there (the outbox, store/outbox.py, sent by the sync engine,
                     client/sync.py) and copies of what it read (its database, or nothing on a network filesystem)

client/open.py decides which one a project gets; nothing above this module asks which it has. Both offer the repo
interface's reads and changes (store/interface.py), plus what the object API, the CLI, the report builder, runs, and
the UI need on top of it, with the same meaning in both:

    resolve(ref, offline, warn) -> (project, name, manifest)    local_folder(...), pull(...)    read-only folders
    publish_folder(...) -> manifest                              stage a folder into the repo, then publish it
    start_version / add_files / set_metadata / complete_version / fail_version / beat     a running version, by uid
    sync(), pending(), status()                                  what waits for the remote (nothing without one)
    verify(ref, repair), prune(older_than, max_size, dry_run)    maintenance
    live                                                         whether running versions are synced while they run

A version the source of truth has not numbered yet (pending) has `version` None and `pending` True in its manifest;
`project:name@<uid>` names it.

With a remote, reads ask the remote and copy what they get into the repo (the repo is the remote's cache), and add
this repo's pending versions (outbox entries: a uid, no number). When the remote does not answer, they answer from
the repo: its copies, its mirrored aliases, and its pending versions, with a warning that names what was used. An
exact reference the repo has (`name@v<n>` of a copy, `name@<uid>`) never asks the remote. Changes to the source of
truth (aliases, deletions, retention) go to the remote and are then mirrored into the repo; while the remote does not
answer they fail with a message that says so. Every write goes to the repo first (the outbox) and is sent at once
when the remote answers, else by a background uploader, the next command, or `cairn sync`.

Before its remote ever answered, a repo may hold versions it numbered itself (it had no remote then). Their numbers
mean nothing to the remote, so they count as pending (a uid, no number) until the sync engine has sent them.
"""

from __future__ import annotations

import atexit
import sqlite3
import sys
import threading
import time
from pathlib import Path

from ..ranges import byte_range
from ..store import refs
from ..store.blobs import CHUNK, BlobStore, blob_path, now, resolve_entry
from ..store.digest import manifest_digest
from ..store.errors import BadRange, Deleted, Forbidden, MissingBlobs, NotFound, StoreError, Unauthorized, Unreachable
from ..store.folders import cached, hostname, inside, materialize_version, pull as pull_local
from ..store.hashes import HashCache
from ..store.inputs import drop_inputs, record_input
from ..store.local import FileRead, LocalRepo, repo_uid
from ..store.outbox import Outbox, exact_ref, manifest_of, usable
from ..store.prune import prune as prune_folders
from ..store.refs import Ref
from ..store.uids import UID, new_uid
from ..store.verify import Report, check_blobs, verify_copies, verify_folders, verify_outbox, verify_store, verify_version
from .http import entries_for, scan
from .remote import Remote
from .sync import NotRecorded, Sync, Waiting

FINAL = ("completed", "failed", "deleted")


def _warn(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _alive(project: str, name: str, manifest: dict) -> dict:
    if manifest.get("status") == "deleted":
        raise Deleted(f"{exact_ref(project, name, manifest)} was deleted ({(manifest.get('deleted_at') or '')[:19]})"
                      + (f": {manifest['reason']}" if manifest.get("reason") else ""))
    return manifest


class ProjectRepo:
    """What both compositions share: staging a folder into the repo's blobs (see the module doc for the interface)."""

    root: Path
    project: str | None
    live = False  # running versions are synced while they run (a remote's UI shows them live)
    url: str | None = None  # the remote's address (None without one)

    def stage(self, folder: Path, exclude, project_root: Path, link: bool = False) -> tuple[list[dict], dict[str, Path]]:
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
        return type(other) is type(self) and other.url == self.url and Path(other.root).resolve() == Path(self.root).resolve()

    def local_folder(self, project: str, name: str, version: dict) -> Path:
        """The read-only folder of a resolved version in the repo (`versions/<uid>/files/`, made once)."""
        return materialize_version(self, project, name, version, self.root)

    def use_token(self, token: str) -> None:
        """Talk to the remote with this token (nothing without a remote)."""

    def resume(self) -> None:
        """Send what waits for the remote in the background (nothing without a remote, or when nothing waits)."""


# ---------------------------------------------------------------------- without a remote


def _forward(name: str):
    def method(self, *args, **kwargs):
        return getattr(self.local, name)(*args, **kwargs)

    method.__name__ = name
    method.__doc__ = f"`LocalRepo.{name}`: the repo is the source of truth."
    return method


class StandaloneRepo(ProjectRepo):
    """A repo without a remote: its LocalRepo numbers, aliases, and deletes."""

    def __init__(self, local: LocalRepo):
        self.local = local
        self.root = local.root

    def __repr__(self) -> str:
        return f"<cairn.StandaloneRepo {self.root}>"

    @property
    def project(self) -> str | None:
        return self.local.project

    @project.setter
    def project(self, value: str | None) -> None:
        self.local.project = value

    @property
    def uid(self) -> str:
        return self.local.uid

    def resolve(self, ref: Ref | str, offline: bool = False, warn=None) -> tuple[str, str, dict]:
        return self.local.resolve(ref)

    def pull(self, ref: str, record: bool = True, offline: bool = False) -> Path:
        return pull_local(self.local, ref, record)

    # ------------------------------------------------------------------ writing

    def publish_folder(self, project: str, name: str, folder: Path, *, exclude=(), project_root: Path, link: bool = False,
                       label: str | None = None, metadata: dict | None = None, inputs=(), aliases=(), source: dict | None = None,
                       uid: str | None = None) -> dict:
        """Stage the folder and publish it as the next version (none when nothing changed: the latest is returned)."""
        self.local.create_project(project)
        for attempt in range(3):
            entries, _ = self.stage(folder, exclude, project_root, link)
            try:
                number = self.local.publish(project, name, entries, label=label, metadata=metadata, inputs=inputs,
                                            aliases=aliases, source=source, uid=uid)
                break
            except MissingBlobs:  # garbage collection removed a blob since staging: stage it again
                if attempt == 2:
                    raise
        return self.local.get_version(project, name, number)

    def start_version(self, project: str, name: str, label: str | None = None, metadata: dict | None = None,
                      source: dict | None = None, uid: str | None = None) -> dict:
        uid = uid or new_uid()
        self.local.beat(uid)  # before the version exists: a crash right after still leaves one that can be failed
        self.local.create_project(project)
        number = self.local.start_version(project, name, label, metadata, source, uid=uid)
        return self.local.get_version(project, name, number)

    def add_files(self, project: str, name: str, uid: str, entries: list[dict]) -> None:
        if entries:
            self.local.add_files(project, name, uid, entries)

    def set_metadata(self, project: str, name: str, uid: str, metadata: dict) -> None:
        self.local.set_metadata(project, name, uid, metadata)

    def complete_version(self, project: str, name: str, uid: str, inputs=(), aliases=()) -> dict:
        self.local.complete_version(project, name, uid, list(inputs))
        number = self.local.get_version(project, name, uid)["version"]
        for alias in aliases:
            self.local.set_alias(project, name, alias, number)
        return self.local.get_version(project, name, uid)

    def fail_version(self, project: str, name: str, uid: str, error: str | None = None) -> None:
        if self.local.get_version(project, name, uid)["status"] == "running":
            self.local.fail_version(project, name, uid, error)

    def beat(self, uid: str) -> None:
        self.local.beat(uid)

    # ------------------------------------------------------------------ waiting for a remote: nothing

    def sync(self, report=None, refresh: bool = True) -> dict:
        return {"remote": None, "sent": [], "rejected": [], "waiting": [], "failed": [], "adopted": 0, "refreshed": 0, "lines": []}

    def pending(self) -> list[dict]:
        return []

    def status(self) -> dict:
        return {"remote": None, "entries": [], "reaped": list(self.local.reaped)}

    # ------------------------------------------------------------------ maintenance

    def verify(self, ref: str | None = None, repair: bool = False) -> Report:
        if ref:
            project, name, version = self.resolve(ref)
            return verify_version(self.root, exact_ref(project, name, version), version, own=True, repair=False)
        report = verify_store(self.local)  # its blobs too, never repaired: they are the repo's own versions
        return report.merge(verify_folders(self.root, repair))

    def prune(self, older_than: float | None = None, max_size: int | None = None, dry_run: bool = False) -> dict:
        return prune_folders(self.root, older_than, max_size, dry_run, keep=None)


for _name in ("projects", "overviews", "get_project", "create_project", "update_project", "summaries", "summary", "artifacts",
              "versions", "get_version", "aliases", "lineage", "read_file", "blob_file", "blob_path", "set_alias", "delete_version",
              "delete_artifact", "delete_project", "retention", "set_retention", "gc", "export_metadata"):
    setattr(StandaloneRepo, _name, _forward(_name))
del _name


# ---------------------------------------------------------------------- with a remote


class NoCopies:
    """The copies of a repo that has no database (it is on a network filesystem): it keeps none, so what the remote
    does not answer cannot be read here."""

    project = None
    reaped: list = []

    def _none(self, *args, **kwargs):
        raise Unreachable("this repo keeps no copies of the remote's versions (it has no database: it is on a network "
                          "filesystem), so only the remote can answer")

    projects = overviews = get_project = summaries = summary = versions = get_version = aliases = resolve = lineage = _none

    def put_copy(self, project, name, manifest, origin) -> bool:
        return False

    def mirror_aliases(self, project, name, aliases) -> None:
        return None

    def meta(self, key):
        return None

    def set_meta(self, key, value) -> None:
        return None

    def local_versions(self) -> list:
        return []

    def forget_versions(self, uids) -> int:
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
    """A repo with a remote (see the module doc). `copies` is the repo's database (a LocalRepo) or NoCopies."""

    live = True

    def __init__(self, remote: Remote, root: Path | str, copies, project: str | None, heartbeat_timeout: float):
        self.remote, self.copies, self.project = remote, copies, project
        self.root = Path(root)
        self.own_uid = repo_uid(self.root)  # repo.json, also without a database (a network filesystem)
        self.url = remote.url
        self.heartbeat_timeout = heartbeat_timeout
        self.outbox = Outbox(self.root)
        self.engine = Sync(self.outbox, remote, copies, self.root, heartbeat_timeout)
        self._down_until = 0.0  # the remote did not answer: reads and background sends skip it until then
        self._quick: SyncedRepo | None = None  # this repo with short request timeouts (`quick`)
        self._reached = False  # reach() looked at the remote once (and adopted what the repo numbered itself)
        self.ask = _Guarded(self)

    def __repr__(self) -> str:
        return f"<cairn.SyncedRepo {self.root} remote {self.url}>"

    @property
    def uid(self) -> str | None:
        """The remote's uid (the source of truth): the one the repo remembers, else asked (once)."""
        known = self.engine.known_uid
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
        remote._uid = self.remote._uid
        return SyncedRepo(remote, self.root, self.copies, self.project, self.heartbeat_timeout)

    # ------------------------------------------------------------------ copies

    def copy(self, project: str, name: str, manifest: dict) -> None:
        """Record a remote's answer in the repo (a copy). Never fails a read: the repo is a cache."""
        if manifest.get("pending") or manifest.get("status") not in FINAL:
            return
        origin = self.engine.origin()
        if origin is None:
            return
        try:
            self.copies.put_copy(project, name, manifest, origin)
        except (StoreError, ValueError, sqlite3.Error, OSError) as error:
            _warn(f"warning: could not record {exact_ref(project, name, manifest)} in the repo ({error})")

    def mirror(self, project: str, name: str, aliases: dict[str, int] | None = None) -> None:
        """Mirror the remote's aliases of an artifact into the repo (asked now unless given). Never raises."""
        try:
            self.engine.mirror(project, name, aliases)
        except (StoreError, ValueError, sqlite3.Error, OSError):
            pass

    def _copied(self, project: str, name: str, selector: int | str) -> dict | None:
        """The repo's own record of an exact version (a copy by number; by uid also a version it numbered itself,
        shown pending), else None."""
        try:
            state = self.copies.get_version(project, name, selector)
        except (StoreError, ValueError):
            return None
        if isinstance(selector, int) and not state.get("origin"):
            return None  # this repo's own number, not the remote's
        return masked(state)

    # ------------------------------------------------------------------ this repo's pending versions

    def records(self) -> list[dict]:
        """This remote's outbox entries, oldest first."""
        return self.engine.records()

    def _pending_of(self, project: str, name: str) -> list[dict]:
        return [record for record in self.records() if (record["project"], record["name"]) == (project, name)]

    def _pending_match(self, project: str, name: str, selector: str) -> dict | None:
        """The newest usable pending version that `project:name@selector` (an alias, a label, `latest`) names."""
        found = [record for record in self._pending_of(project, name) if usable(record)
                 and (selector == "latest" or selector in record.get("aliases", []) or record.get("label") == selector)]
        return found[-1] if found else None

    def _entry(self, project: str, name: str, uid: str) -> dict | None:
        record = self.outbox.load(uid)
        return record if record and (record["project"], record["name"]) == (project, name) else None

    # ------------------------------------------------------------------ projects and listings

    def _fallback(self, call, local):
        """`call()` against the remote; while it does not answer, `local()` from the repo's copies."""
        try:
            return call()
        except Unreachable:
            return local()

    def _waiting_projects(self) -> dict[str, dict]:
        """The projects this repo's outbox entries name, as project records with their artifact count and last change (a
        project first written offline is known only there until it is sent)."""
        found: dict[str, dict] = {}
        for record in self.records():
            entry = found.setdefault(record["project"], {"name": record["project"], "title": record["project"], "description": "",
                                                         "created": record["created"], "updated": record["created"], "names": set()})
            entry["updated"] = max(entry["updated"], record["created"])
            entry["names"].add(record["name"])
        return found

    def _with_waiting(self, listed, counts: bool) -> list[dict]:
        """A project listing (the remote's or the copies'; None: neither answers) and the projects only the outbox names."""
        listed = list(listed or [])
        known = {item["name"] for item in listed}
        for name, entry in sorted(self._waiting_projects().items()):
            if name not in known:
                record = {key: entry[key] for key in ("name", "title", "description", "created")}
                if counts:
                    record.update(artifacts=len(entry["names"]), updated=entry["updated"])
                listed.append(record)
        return listed

    def _listing(self, call, local):
        try:
            return self._fallback(call, local)
        except Unreachable:  # no copies to answer from (no database): the outbox is still listed
            if not self.records():
                raise
            return None

    def projects(self) -> list[dict]:
        return self._with_waiting(self._listing(self.ask.projects, self.copies.projects), counts=False)

    def overviews(self) -> list[dict]:
        return self._with_waiting(self._listing(self.ask.overviews, self.copies.overviews), counts=True)

    def get_project(self, name: str) -> dict:
        try:
            return self._fallback(lambda: self.ask.get_project(name), lambda: self.copies.get_project(name))
        except (NotFound, Unreachable):
            entry = self._waiting_projects().get(name)
            if entry is None:
                raise
            return {key: entry[key] for key in ("name", "title", "description", "created")}

    def create_project(self, name: str, title: str | None = None, description: str = "") -> dict:
        return self._changes(lambda: self.remote.create_project(name, title, description), f"creating the project {name}")

    def update_project(self, name: str, title: str | None = None, description: str | None = None) -> dict:
        return self._changes(lambda: self.remote.update_project(name, title, description), f"changing the project {name}")

    def _waiting_summary(self, name: str, records: list[dict]) -> dict:
        return {"name": name, "latest": None, "versions": len(records), "aliases": {}, "updated": records[-1]["created"],
                "metadata": records[-1].get("metadata") or {}, "pending": len(records)}

    def summaries(self, project: str) -> list[dict]:
        def local():
            try:
                return self.copies.summaries(project)
            except NotFound:
                return []

        try:
            found = list(self._listing(lambda: self.ask.summaries(project), local) or [])
        except NotFound:  # the remote has no such project yet: what the outbox holds of it, if anything
            if not self._waiting_projects().get(project):
                raise
            found = []
        known = {item["name"] for item in found}
        waiting: dict[str, list[dict]] = {}
        for record in self.records():
            if record["project"] == project and record["name"] not in known:
                waiting.setdefault(record["name"], []).append(record)
        return found + [self._waiting_summary(name, records) for name, records in sorted(waiting.items())]

    def summary(self, project: str, name: str) -> dict:
        try:
            return self._fallback(lambda: self.ask.summary(project, name), lambda: self.copies.summary(project, name))
        except NotFound:
            records = self._pending_of(project, name)
            if not records:
                raise
            return self._waiting_summary(name, records)

    def artifacts(self, project: str) -> list[str]:
        return [item["name"] for item in self.summaries(project)]

    def versions(self, project: str, name: str) -> list[dict]:
        """Every version, oldest first, whatever its status (the remote's, else the repo's copies), then this repo's
        pending ones (number None)."""
        down = None
        try:
            found = self.ask.versions(project, name)
        except NotFound:
            found = []
        except Unreachable:
            try:
                found = sorted((masked(state) for state in self.copies.versions(project, name)),
                               key=lambda state: (state["version"] is None, state["version"] or 0, state["uid"]))
            except NotFound:
                found = []
            except Unreachable as error:
                found, down = [], error
        known = {state["uid"] for state in found}
        found += [manifest_of(record) for record in self._pending_of(project, name) if record["uid"] not in known]
        if not found and down is not None:
            raise down
        return found

    def get_version(self, project: str, name: str, version: int | str) -> dict:
        """One manifest by number or uid, whatever its status: this repo's pending version, its copy, else the remote's
        (copied when final)."""
        if isinstance(version, str) and refs.VERSION.fullmatch(version):
            version = int(version[1:])
        if isinstance(version, str):
            if not UID.fullmatch(version):
                raise ValueError(f"invalid version {version!r}")
            record = self._entry(project, name, version)
            if record is not None:
                return manifest_of(record)
        local = self._copied(project, name, version)
        if local is not None and (local.get("status") in FINAL or local.get("pending")):
            return local
        try:
            manifest = self.ask.get_version(project, name, version)
        except Unreachable:
            if local is not None:
                return local
            raise
        self.copy(project, name, manifest)
        return manifest

    def aliases(self, project: str, name: str) -> dict[str, int]:
        try:
            found = self.ask.aliases(project, name)
        except NotFound:
            return {}
        except Unreachable:
            try:
                return self.copies.aliases(project, name)
            except NotFound:
                return {}
        self.mirror(project, name, found)
        return found

    # ------------------------------------------------------------------ resolving

    def resolve(self, ref: Ref | str, offline: bool = False, warn=None) -> tuple[str, str, dict]:
        """(project, name, manifest) for a reference (no selector: `latest`): this repo's pending version by uid; an exact
        version the repo has; else the remote (and the answer is copied), unless a pending version of this repo that
        the selector names is newer; while the remote does not answer (or with `offline`), the newest of the repo's own
        answer and its pending versions, with a warning."""
        ref = refs.parse(ref) if isinstance(ref, str) else ref
        say = warn or _warn
        project = ref.project or self.project
        if project is None:
            raise StoreError(f"reference {ref} has no project and the repo has no default")
        selector = ref.selector or "latest"
        key = f"{project}:{ref.name}@{selector}"
        if ref.uid:
            record = self._entry(project, ref.name, ref.uid)
            if record is not None:
                if usable(record):
                    say(_pending_warning(key, project, ref.name, record["uid"]))
                    return project, ref.name, manifest_of(record)
                if record["sync"] == "rejected":
                    raise StoreError(f"{key} was refused by the remote: {record.get('error')}")
        if ref.exact:
            local = self._copied(project, ref.name, ref.version if ref.version is not None else ref.uid)
            if local is None and ref.uid:  # a read-only folder a pull made here knows it too
                found = cached(self.root, ref.uid)
                if found and (found.get("project"), found.get("name")) == (project, ref.name) and \
                        (found.get("manifest") or {}).get("version") is not None:
                    local = found["manifest"]
            if local is not None:
                _alive(project, ref.name, local)
                if local.get("pending"):
                    say(_pending_warning(key, project, ref.name, local["uid"]))
                    return project, ref.name, local
                if local.get("status") == "completed":
                    return project, ref.name, local
        pending = None if ref.exact else self._pending_match(project, ref.name, selector)
        if not offline:
            try:
                project, name, version = self.ask.resolve(Ref(project, ref.name, ref.selector))
            except Unreachable:
                if ref.exact:
                    raise Unreachable(f"the remote at {self.url} does not answer and the repo does not have {key}") from None
                reason = f"the remote at {self.url} does not answer"
            except NotFound:
                if pending is None:
                    raise
                say(_pending_warning(key, project, ref.name, pending["uid"]))
                return project, ref.name, manifest_of(pending)
            else:
                self.copy(project, name, version)
                if not ref.exact:
                    self.mirror(project, name)
                if pending is not None and pending["uid"] > version["uid"]:
                    say(_pending_warning(key, project, name, pending["uid"]))
                    return project, name, manifest_of(pending)
                return project, name, version
        else:
            if ref.exact:
                raise NotFound(f"offline: the repo does not have {key}")
            reason = "offline"
        try:
            found = masked(self.copies.resolve(Ref(project, ref.name, ref.selector))[2])
        except StoreError:
            found = None
        if pending is not None and (found is None or pending["uid"] > found["uid"]):
            say(_pending_warning(key, project, ref.name, pending["uid"]))
            return project, ref.name, manifest_of(pending)
        if found is None:
            raise Unreachable(f"{reason}, and the repo has no version for {key}")
        if found.get("pending"):
            say(_pending_warning(key, project, ref.name, found["uid"]))
            return project, ref.name, found
        say(f"warning: {reason}; using the repo's {exact_ref(project, ref.name, found)} (uid {found['uid']}) for {key}")
        return project, ref.name, found

    def lineage(self, ref: Ref | str, marks: bool = False) -> dict[str, list[str]]:
        ref = refs.parse(ref) if isinstance(ref, str) else ref
        project = ref.project or self.project
        record = self._entry(project, ref.name, ref.uid) if ref.uid else None

        def own() -> dict:
            found = {"inputs": list(record.get("inputs") or []), "consumers": []}
            if marks:
                found.update(deleted=[], nodes={})
            return found

        if record is not None and record.get("number") is None:  # the remote does not know it yet
            return own()
        try:
            return self.ask.lineage(Ref(project, ref.name, ref.selector), marks=marks)
        except (Unreachable, NotFound):
            if record is not None:
                return own()
            return self.copies.lineage(Ref(project, ref.name, ref.selector), marks=marks)

    # ------------------------------------------------------------------ files

    def _local_manifest(self, project: str, name: str, selector: str | None) -> dict | None:
        """The manifest of an exact version (`v<n>`, a uid) if the repo has it, else None."""
        if not selector:
            return None
        if UID.fullmatch(selector):
            record = self._entry(project, name, selector)
            if record is not None:
                return manifest_of(record)
            return self._copied(project, name, selector)
        if refs.VERSION.fullmatch(selector):
            return self._copied(project, name, int(selector[1:]))
        return None

    def read_file(self, project: str, name: str, selector: str | None, path: str, range_header: str | None = None) -> FileRead:
        """A file of a version: from the repo's blobs when it has the version and the contents, else from the remote."""
        manifest = self._local_manifest(project, name, selector)
        if manifest is not None and manifest.get("status") != "deleted":
            entry = resolve_entry(manifest["files"], path)
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
                raise NotFound(f"{project}:{name}@{selector} has no file {path} in the repo")
        return self.ask.read_file(project, name, selector, path, range_header)

    def blob_file(self, project: str, name: str, version: int | None, entry: dict, root) -> Path:
        """The entry's contents in the repo's blobs: already there (a pending version's always are), else downloaded."""
        target = blob_path(root, entry["sha256"])
        if target.is_file():
            return target
        if version is None:
            raise StoreError(f"{project}:{name}: the contents of {entry['path']} ({entry['sha256'][:12]}) left the repo {root}")
        return self.ask.blob_file(project, name, version, entry, root)

    def pull(self, ref: str, record: bool = True, offline: bool = False) -> Path:
        """A read-only local copy (or of the file after `//`), its exact version recorded as an input."""
        parsed = refs.parse(ref)
        project, name, version = self.resolve(parsed, offline)
        folder = self.local_folder(project, name, version)
        exact = exact_ref(project, name, version)
        path = inside(folder, parsed, exact)
        if record:
            record_input(exact, version["uid"])
        return path

    # ------------------------------------------------------------------ changes to the source of truth

    def _changes(self, call, what: str):
        """`call()` against the remote; while it does not answer, a clear refusal (it changes the source of truth)."""
        try:
            return call()
        except Unreachable as error:
            raise Unreachable(f"{what} needs the remote (it is the source of truth for numbers, aliases, and deletions), and "
                              f"the remote at {self.url} does not answer; try again when it does ({error})") from None

    def set_alias(self, project: str, name: str, alias: str, version: int) -> None:
        self._changes(lambda: self.remote.set_alias(project, name, alias, version), f"setting the alias {alias}")
        self.mirror(project, name)

    def _mirror_versions(self, project: str, names) -> None:
        for name in names:
            try:
                for manifest in self.remote.versions(project, name):
                    self.copy(project, name, manifest)
            except StoreError:
                continue
            self.mirror(project, name)

    def delete_version(self, project: str, name: str, version: int | str, *, reason: str | None = None, force: bool = False) -> dict:
        result = self._changes(lambda: self.remote.delete_version(project, name, version, reason=reason, force=force),
                               f"deleting {project}:{name}@{version}")
        self._mirror_versions(project, [name])
        return result

    def delete_artifact(self, project: str, name: str, *, reason: str | None = None, force: bool = False) -> dict:
        result = self._changes(lambda: self.remote.delete_artifact(project, name, reason=reason, force=force),
                               f"deleting {project}:{name}")
        self._mirror_versions(project, [name])
        return result

    def delete_project(self, project: str, *, reason: str | None = None, force: bool = False) -> dict:
        result = self._changes(lambda: self.remote.delete_project(project, reason=reason, force=force), f"deleting the project {project}")
        stamp = now()
        for copy_project, name, manifest in self.copies.copies():
            if copy_project == project and manifest["status"] != "deleted":
                self.copy(project, name, {**manifest, "status": "deleted", "files": [], "deleted_at": stamp, "reason": reason})
        return result

    def retention(self, project: str) -> list[dict]:
        return self.remote.retention(project)

    def set_retention(self, project: str, rules: list[dict]) -> list[dict]:
        return self._changes(lambda: self.remote.set_retention(project, rules), "changing retention rules")

    def gc(self, dry_run: bool = True, grace=None) -> dict:
        return self._changes(lambda: self.remote.gc(dry_run=dry_run, grace=grace), "garbage collection")

    # ------------------------------------------------------------------ writing: the outbox first

    def _uid_inputs(self, inputs) -> list[str]:
        """Inputs as the remote should read them: a version this repo numbered itself (before it had a remote) by its
        uid, so the remote resolves it to its own number once it has it."""
        found: list[str] = []
        for item in inputs or []:
            text = str(item)
            try:
                ref = refs.parse(text)
            except ValueError:
                ref = None
            if ref is not None and ref.exact and ref.project:
                try:
                    state = self.copies.get_version(ref.project, ref.name, ref.version if ref.version is not None else ref.uid)
                except (StoreError, ValueError):
                    state = None
                if state is not None and not state.get("origin"):
                    text = str(Ref(ref.project, ref.name, state["uid"]))
                elif state is not None and ref.uid and isinstance(state.get("version"), int):  # a copy: the remote's number
                    text = refs.exact(ref.project, ref.name, state["version"])
            if text not in found:
                found.append(text)
        return found

    def _new_entry(self, kind: str, project: str, name: str, *, files=(), label=None, metadata=None, inputs=(), aliases=(),
                   source=None, uid: str | None = None) -> dict:
        """Commit a new outbox entry (durably, before anything is sent)."""
        import os

        running = kind == "running"
        record = {"uid": uid or new_uid(), "kind": kind, "sync": "running" if running else "pending",
                  "status": "running" if running else "completed", "url": self.url, "store": self.engine.known_uid,
                  "project": project, "name": name, "label": label, "metadata": metadata or {}, "inputs": self._uid_inputs(inputs),
                  "aliases": list(aliases), "source": source, "files": list(files), "created": now(),
                  "owner": {"host": hostname(), "pid": os.getpid()}, "number": None, "pushed": {}, "error": None}
        if not running:
            record["digest"] = manifest_digest(record["files"])  # known before anything is uploaded
            record["completed"] = record["created"]
        self.outbox.create(record, running)
        return record

    def _settled(self, record: dict) -> dict:
        """The manifest of an entry that was just sent (or not): the remote's numbered version, else the pending one."""
        project, name, uid = record["project"], record["name"], record["uid"]
        current = self._entry(project, name, uid)
        if current is not None:
            return manifest_of(current)
        try:
            return self.get_version(project, name, uid)
        except NotFound:  # nothing changed since the latest: the remote kept its own version, not this uid
            if record.get("number") is not None:
                return self.get_version(project, name, record["number"])
            return self.resolve(Ref(project, name, None), warn=lambda line: None)[2]

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
        self._quick.remote._uid = self._quick.remote._uid or self.remote._uid
        self._quick.remote.client.token = self.remote.client.token
        return self._quick

    def reach(self) -> None:
        """Before the first request of this process: a quick look whether the remote answers at all (PROBE seconds), so a
        host that hangs makes one short wait, not one long one per request. Raises Unreachable, also while the remote
        is down (`down`)."""
        if self.down():
            raise Unreachable(f"the remote at {self.url} did not answer a moment ago")
        if self._reached:
            return
        began = time.monotonic()
        try:
            if self.remote._uid is None:
                self.remote._uid = self.with_timeout(PROBE).remote.uid
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

    def _send_now(self, record: dict) -> dict:
        """Send an entry that is final now; when the remote does not answer it stays pending (a warning says so, and a
        background uploader keeps trying). Returns the version's manifest."""
        began = time.monotonic()
        try:
            self.reach()
            self.quick.engine.send(record)
        except Waiting as error:
            if isinstance(error.__cause__, (Unauthorized, Forbidden)):  # the caller should know at once; it stays pending
                raise type(error.__cause__)(f"{error.__cause__}; {record['project']}:{record['name']}@{record['uid']} stays "
                                            "pending in the outbox (`cairn status`) until a token that may write sends it") from None
            _warn(f"pending {record['project']}:{record['name']}@{record['uid']}: {error}")
            start_uploader(self)
        except Unreachable as error:
            if not isinstance(error, NotRecorded):
                self._failed(began)
            what = "failed" if record["status"] == "failed" else "pending"
            _warn(f"{what} {record['project']}:{record['name']}@{record['uid']}: the remote does not answer ({error}); it is "
                  "usable here now and sent when the remote answers (`cairn status` lists it, `cairn sync` sends it now)")
            start_uploader(self)
        return self._settled(record)

    def publish_folder(self, project: str, name: str, folder: Path, *, exclude=(), project_root: Path, link: bool = False,
                       label: str | None = None, metadata: dict | None = None, inputs=(), aliases=(), source: dict | None = None,
                       uid: str | None = None) -> dict:
        """Stage the folder, commit it to the outbox, and send it now if the remote answers (else it is pending)."""
        entries, sources = self.stage(folder, exclude, project_root, link)
        same = None if aliases else self._unchanged(project, name, entries, metadata, inputs)
        if same is not None:  # like a repo without a remote: nothing changed since the latest, so no new version
            return same
        record = self._new_entry("publish", project, name, files=entries, label=label, metadata=metadata, inputs=inputs,
                                 aliases=aliases, source=source, uid=uid)
        self.restage(entries, sources)
        return self._send_now(record)

    def _unchanged(self, project: str, name: str, entries: list[dict], metadata, inputs) -> dict | None:
        """The latest version (the remote's, else what the repo knows) when it has these files, metadata, and inputs."""
        try:
            latest = self.resolve(Ref(project, name, None), warn=lambda line: None)[2]
        except StoreError:
            return None
        same = (latest.get("digest") or manifest_digest(latest.get("files") or [])) == manifest_digest(entries) and \
            (latest.get("metadata") or {}) == (metadata or {}) and sorted(latest.get("inputs") or []) == sorted(self._uid_inputs(inputs))
        return latest if same else None

    def _running(self, project: str, name: str, uid: str) -> dict:
        """The outbox entry of a running version of this process (refused once a sender failed it: its heartbeat
        expired, so the job lost it)."""
        record = self._entry(project, name, uid)
        if record is None:
            raise StoreError(f"{project}:{name}@{uid} is not a running version of this repo")
        if record["status"] != "running":
            what = record.get("error") or record["status"]
            raise StoreError(f"{project}:{name}@{uid} was {record['status']} meanwhile ({what}); publish the folder again")
        return record

    def _try(self, record: dict) -> None:
        """Send what is new of a running entry now; while the remote does not answer, it waits in the outbox."""
        began = time.monotonic()
        try:
            self.reach()
            self.quick.engine.send(record, claim=False)
        except NotRecorded:
            pass
        except Unreachable:
            self._failed(began)

    def start_version(self, project: str, name: str, label: str | None = None, metadata: dict | None = None,
                      source: dict | None = None, uid: str | None = None) -> dict:
        record = self._new_entry("running", project, name, label=label, metadata=metadata, source=source, uid=uid)
        self._try(record)
        return manifest_of(self.outbox.load(record["uid"]) or record)

    def add_files(self, project: str, name: str, uid: str, entries: list[dict]) -> None:
        record = self._running(project, name, uid)
        files = {item["path"]: item for item in record["files"]}
        files.update({entry["path"]: entry for entry in entries})
        record["files"] = sorted(files.values(), key=lambda item: item["path"])
        self.outbox.save(record)
        self.outbox.beat(uid)
        self._try(record)

    def set_metadata(self, project: str, name: str, uid: str, metadata: dict) -> None:
        record = self._running(project, name, uid)
        record["metadata"] = metadata
        self.outbox.save(record)
        self._try(record)

    def complete_version(self, project: str, name: str, uid: str, inputs=(), aliases=()) -> dict:
        record = self._running(project, name, uid)
        record.update(sync="pending", status="completed", inputs=self._uid_inputs(inputs), aliases=list(aliases),
                      digest=manifest_digest(record["files"]), completed=now())
        self.outbox.save(record)
        return self._send_now(record)

    def fail_version(self, project: str, name: str, uid: str, error: str | None = None) -> None:
        record = self._entry(project, name, uid)
        if record is None or record["status"] != "running":
            return
        record.update(sync="pending", status="failed", completed=now(), error=error)
        self.outbox.save(record)
        self._send_now(record)

    def beat(self, uid: str) -> None:
        self.outbox.beat(uid)

    def use_token(self, token: str) -> None:
        self.remote.client.token = token

    def resume(self) -> None:
        if any(record["sync"] == "pending" for record in self.records()):
            start_uploader(self, first_delay=0)

    # ------------------------------------------------------------------ what waits for the remote

    def sync(self, report=None, refresh: bool = True) -> dict:
        """Send the outbox now (the sync engine), then (`refresh`) mirror the remote's aliases of the artifacts the repo
        has versions of (`refreshed`: how many; skipped while the remote does not answer). `report(line)` hears what
        happened."""
        result = self.engine.flush()
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
        entries = [{**record, "heartbeat": self.outbox.last_beat(record)} if record["sync"] == "running" else record
                   for record in self.records()]
        sent = {record["uid"] for record in entries}
        for project, name, state in self.copies.local_versions():  # numbered here before the repo had a remote: not sent yet
            if state["uid"] not in sent and state["status"] != "deleted":
                entries.append({**state, "project": project, "name": name, "number": None, "owner": {},
                                "sync": "running" if state["status"] == "running" else "pending",
                                "error": None if state["status"] != "failed" else state.get("error")})
        return {"remote": self.url, "entries": entries, "reaped": list(getattr(self.copies, "reaped", []))}

    # ------------------------------------------------------------------ maintenance

    def _keep(self) -> set[str]:
        """The blobs only this repo has: the outbox's and those of versions it numbered itself."""
        keep = self.outbox.needed()
        keep |= {entry["sha256"] for _, _, state in self.copies.local_versions() for entry in state.get("files") or [] if "sha256" in entry}
        return keep

    def verify(self, ref: str | None = None, repair: bool = False) -> Report:
        if ref:
            project, name, version = self.resolve(ref)
            return verify_version(self.root, exact_ref(project, name, version), version, own=bool(version.get("pending")), repair=repair)
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
QUICK = 10.0  # seconds a request a process makes while it works may wait for each answer (`SyncedRepo.quick`)
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
                raise Unreachable(f"the remote at {repo.url} did not answer a moment ago")
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


def _pending_warning(key: str, project: str, name: str, uid: str) -> str:
    return (f"warning: {key} is {project}:{name}@{uid}, a version of this repo that the remote does not have yet"
            " (pending: `cairn status` lists it, `cairn sync` sends it)")


# ---------------------------------------------------------------------- the background uploader

_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()
_exit_flush: dict[str, SyncedRepo] = {}
EXIT_TIMEOUT = 5.0  # seconds the last attempt at exit may wait for the remote


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
                    result = repo.sync(report=lambda line: print(f"cairn: {line}", file=sys.stderr, flush=True), refresh=False)
                    left = [record for record in result["waiting"] if record["sync"] != "running"]  # running: its process sends it
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
                repo.with_timeout(EXIT_TIMEOUT).engine.flush()
        except Exception:  # noqa: BLE001 - never a traceback at exit
            pass
