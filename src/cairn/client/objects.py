"""The object API: a store, its projects, their artifacts, and the artifacts' versions.

    import cairn

    store = cairn.connect()                       # [store] in cairn.toml; or connect("https://...", token=...) / connect("path/to/store")
    project = store.project()                     # the configured project; store.project("other") for another
    crops = project.artifact("datasets/crops")
    v3 = crops.version(3)                         # also "v3", an alias, a label, or a uid; crops.latest
    v3.files, v3.metadata, v3.inputs, v3.consumers
    with v3.open("tables/cases.csv") as handle:  # streams one file (recorded as an input, like pull)
        ...
    folder = v3.pull()                            # the whole version, read-only, from the cache
    fit = project.artifact("fit").publish("outputs/fit", metadata={"loss": 0.1}, aliases=["best"])
    fit.ref                                       # "study:fit@v4"; a pending version: number None, ref by uid

Objects are thin and lazy: a `Project` reads its record, an `Artifact` its summary, and a `Version` its
manifest on first access and keep them. Publishing to a store server goes through the local replica
(replica.py, sync.py): the version is committed there first, and when the server does not answer,
`publish` returns a pending `Version` (status `pending`, number None) that is usable at once (`pull`,
`open`, as an input by its uid) and a background uploader sends it once the server answers.
"""

from __future__ import annotations

import io
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import Config, load_config
from ..store import LocalStore, StoreError, read_inputs, record_input, refs
from ..store.blobs import resolve_entry
from ..store.cache import cache_root
from ..store.errors import NotFound, Unreachable
from ..store.refs import Ref
from ..store.uids import UID
from . import offline as fallback, replica
from .http import HashCache
from .remote import RemoteStore


@dataclass(frozen=True)
class File:
    """One file of a version (`sha256` None and `link` set for a stored symlink)."""
    path: str
    size: int
    sha256: str | None
    link: str | None = None


def _source(local: Path) -> dict:
    return {"host": socket.gethostname(), "folder": str(local)}


class Store:
    """A store, local (`LocalStore`) or a server (`RemoteStore`), as `backend`."""

    def __init__(self, backend: LocalStore | RemoteStore):
        self.backend = backend

    def __repr__(self) -> str:
        return f"<cairn.Store {getattr(self.backend, 'url', None) or self.backend.root}>"

    @property
    def remote(self) -> bool:
        return isinstance(self.backend, RemoteStore)

    @property
    def uid(self) -> str | None:
        return self.backend.uid

    @property
    def replica(self):
        """This store server's records in the local replica (`sync.Replica`; None for a local store)."""
        from .sync import Replica

        return Replica(self.backend) if self.remote else None

    # ------------------------------------------------------------- projects

    def projects(self) -> list["Project"]:
        return [Project(self, item["name"], item) for item in self.backend.projects()]

    def project(self, name: str | None = None) -> "Project":
        """A project by name; None means the configured default (`[store] project`)."""
        name = name or self.backend.project
        if name is None:
            raise StoreError("no project named and the store has no default")
        return Project(self, refs.valid_project(name))

    def create_project(self, name: str, title: str | None = None, description: str = "") -> "Project":
        """Create a project (an existing one is returned unchanged)."""
        return Project(self, name, self.backend.create_project(name, title, description))

    # ------------------------------------------------------------ shortcuts

    def artifact(self, name: str) -> "Artifact":
        """`[project:]name`, the project defaulting to the configured one."""
        ref = refs.parse(name)
        if ref.selector or ref.path:
            raise ValueError(f"an artifact name, not a reference: {name!r}")
        return self.project(ref.project).artifact(ref.name)

    def version(self, ref: str, *, offline: bool = False) -> "Version":
        """The version a reference names (`[project:]name[@selector]`), resolved now (with the offline fallbacks)."""
        project, name, manifest = fallback.resolve_version(self.backend, refs.parse(ref), offline)
        return Version(self.project(project).artifact(name), manifest)

    def pull(self, ref: str, *, record: bool = True, offline: bool = False) -> Path:
        """A read-only local copy of the version (or of the file after `//`) a reference names."""
        return fallback.pull(self.backend, ref, record, offline)

    def pending(self) -> list["Version"]:
        """The versions of this machine the server does not have yet: pending, running, rejected (none for a local store)."""
        if not self.remote:
            return []
        return [Version.from_record(self, record) for record in self.replica.records()]

    def sync(self, report=None, refresh: bool = True) -> dict:
        """Send the pending versions now (`Replica.flush`), then learn the current aliases of the artifacts this
        machine has versions of (`refreshed`: how many; skipped while the server does not answer)."""
        if not self.remote:
            return {"sent": [], "rejected": [], "waiting": [], "refreshed": 0}
        result = self.replica.flush(report)
        result["refreshed"] = 0
        if refresh and not [record for record in result["waiting"] if record["sync"] != "running"]:
            try:
                result["refreshed"] = self.replica.refresh()
            except Unreachable:
                pass
        return result


class Project:
    def __init__(self, store: Store, name: str, record: dict | None = None):
        self.store, self.name, self._record = store, name, record

    def __repr__(self) -> str:
        return f"<cairn.Project {self.name}>"

    def __eq__(self, other) -> bool:
        return isinstance(other, Project) and other.store.backend is self.store.backend and other.name == self.name

    __hash__ = object.__hash__

    @property
    def record(self) -> dict:
        if self._record is None or "title" not in self._record:
            self._record = self.store.backend.get_project(self.name)
        return self._record

    @property
    def title(self) -> str:
        return self.record.get("title") or self.name

    @property
    def description(self) -> str:
        return self.record.get("description") or ""

    def update(self, title: str | None = None, description: str | None = None) -> "Project":
        self._record = self.store.backend.update_project(self.name, title, description)
        return self

    def artifacts(self, prefix: str | None = None) -> list["Artifact"]:
        """The project's artifacts (names starting with `prefix`, e.g. "reports/"), sorted by name."""
        found = [Artifact(self, item["name"], item) for item in self.store.backend.summaries(self.name)]
        return sorted((item for item in found if not prefix or item.name.startswith(prefix)), key=lambda item: item.name)

    def artifact(self, name: str) -> "Artifact":
        return Artifact(self, refs.valid_name(name))


class Artifact:
    def __init__(self, project: Project, name: str, summary: dict | None = None):
        self.project, self.name, self._summary = project, name, summary

    def __repr__(self) -> str:
        return f"<cairn.Artifact {self.project.name}:{self.name}>"

    @property
    def store(self) -> Store:
        return self.project.store

    @property
    def backend(self):
        return self.project.store.backend

    @property
    def summary(self) -> dict:
        if self._summary is None:
            self._summary = self.backend.summary(self.project.name, self.name)
        return self._summary

    @property
    def aliases(self) -> dict[str, int]:
        return self.backend.aliases(self.project.name, self.name)

    def versions(self) -> list["Version"]:
        """Every version, oldest first, whatever its status; this machine's versions the server lacks come last (pending)."""
        try:
            found = [Version(self, manifest) for manifest in self.backend.versions(self.project.name, self.name)]
        except NotFound:
            found = []
        except Unreachable:  # the server does not answer: the versions this machine has in its cache
            if not self.store.remote:
                raise
            uid = self.backend.uid
            found = [Version(self, record["manifest"]) for record in replica.synced_records()
                     if (record.get("project"), record.get("name")) == (self.project.name, self.name)
                     and (record.get("store") in (uid, None))]
            found.sort(key=lambda version: version.number or 0)
        if self.store.remote:
            known = {version.uid for version in found}
            found += [Version.from_record(self.store, record) for record in self.store.replica.records()
                      if (record["project"], record["name"]) == (self.project.name, self.name) and record["uid"] not in known]
        return found

    def version(self, selector: int | str) -> "Version":
        """A version by number (3 or "v3"), uid, alias, or label (completed versions only for the last two). Lazy."""
        if isinstance(selector, bool) or not isinstance(selector, (int, str)):
            raise TypeError("a version is a number, 'v<n>', a uid, an alias, or a label")
        text = f"v{selector}" if isinstance(selector, int) else selector
        refs.parse(f"{self.name}@{text}")  # validates it
        return Version(self, None, text)

    @property
    def latest(self) -> "Version":
        return self.version("latest")

    def alias(self, name: str, version: "Version | int | str") -> None:
        """Point alias `name` at a completed version."""
        if not isinstance(version, Version):
            version = self.version(version)
        if version.number is None:
            raise StoreError(f"{version.ref} is pending (not on the store server yet): alias it once it is uploaded"
                             " (or pass aliases= to publish)")
        self.backend.set_alias(self.project.name, self.name, name, version.number)

    def lineage(self, version: "Version | int | str") -> dict[str, list[str]]:
        version = version if isinstance(version, Version) else self.version(version)
        return {"inputs": version.inputs, "consumers": version.consumers}

    # ------------------------------------------------------------ publishing

    def publish(self, folder: Path | str, *, metadata: dict | None = None, aliases=(), label: str | None = None,
                inputs=None, config: Config | None = None) -> "Version":
        """Store a folder as the next version; when nothing changed since the latest, that version is returned.

        A symlinked folder is followed (the label defaults to the resolved folder's name). The folder's
        `.cairn-inputs.jsonl` and `inputs` become the version's inputs. On a store server the version is
        committed to the local replica first; if the server does not answer, a pending Version is returned
        (usable at once) and uploaded in the background (and by `cairn sync` or the next cairn command).
        """
        local = Path(folder).resolve()
        if not local.is_dir():
            raise ValueError(f"{folder} is not a folder")
        config = config or load_config()
        cache = HashCache(config.project)
        merged = [*read_inputs(local), *(str(item) for item in inputs or [])]
        label = label or local.name
        project, backend = self.project.name, self.backend
        if not self.store.remote:
            from .publishing import stage_local

            backend.create_project(project)
            entries = stage_local(backend, local, config, cache)
            number = backend.publish(project, self.name, entries, label=label, metadata=metadata, inputs=merged,
                                     aliases=aliases, source=_source(local))
            return Version(self, backend.get_version(project, self.name, number))
        from .sync import restage, stage, start_uploader

        pending = self.store.replica
        entries, sources = stage(local, config, cache)
        record = pending.add("publish", project, self.name, files=entries, label=label, metadata=metadata,
                             inputs=merged, aliases=aliases, source=_source(local))
        restage(entries, sources, cache)
        try:
            sent = pending.send(record)
        except Unreachable as error:
            print(f"pending {project}:{self.name}@{record['uid']}: the store does not answer ({error}); it is usable here now"
                  " and uploaded when the store answers (`cairn status` lists it, `cairn sync` sends it now)",
                  file=sys.stderr, flush=True)
            start_uploader(backend)
            return Version.from_record(self.store, record)
        if not sent and record.get("number") is None:  # another flusher sent it meanwhile
            return Version(self, backend.get_version(project, self.name, record["uid"]))
        return Version(self, backend.get_version(project, self.name, record["number"]))

    def publishing(self, folder: Path | str, *, metadata: dict | None = None, aliases=(), label: str | None = None,
                   inputs=None):
        """A running version that follows a folder while a job writes it (a `Publication`, a context manager)."""
        from .publishing import Publication

        return Publication(f"{self.project.name}:{self.name}", folder, metadata=metadata, aliases=aliases, label=label,
                           inputs=inputs, store=self.backend)


class Version:
    """One version of an artifact. `number` is None while it is pending: on this machine, not yet on the server."""

    def __init__(self, artifact: Artifact, manifest: dict | None, selector: str | None = None):
        self.artifact, self._manifest, self._selector = artifact, manifest, selector

    @classmethod
    def from_record(cls, store: Store, record: dict) -> "Version":
        """A version from a replica record (one the server does not have yet, or a synced one)."""
        return cls(store.project(record["project"]).artifact(record["name"]), replica.manifest_of(record))

    def __repr__(self) -> str:
        return f"<cairn.Version {self.ref if self._manifest is not None else f'{self.artifact.name}@{self._selector}'}>"

    def __eq__(self, other) -> bool:
        return isinstance(other, Version) and other.uid == self.uid

    def __hash__(self) -> int:
        return hash(self.uid)

    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = self._load()
        return self._manifest

    def _load(self) -> dict:
        artifact, selector = self.artifact, self._selector
        project, name, backend = artifact.project.name, artifact.name, artifact.backend
        if refs.VERSION.fullmatch(selector) or UID.fullmatch(selector):  # any status, like versions()
            remote = isinstance(backend, RemoteStore)
            if remote and UID.fullmatch(selector):
                record = replica.load(selector)
                if record and (record["project"], record["name"]) == (project, name):
                    return replica.manifest_of(record)  # pending, running, or rejected: this machine's record
            hint = fallback.cached(selector if UID.fullmatch(selector) else
                                   (replica.hint(backend.key, project, name, selector) or {}).get("uid")) if remote else None
            if hint and (hint["project"], hint["name"]) == (project, name):
                return hint["manifest"]
            try:
                return backend.get_version(project, name, int(selector[1:]) if selector.startswith("v") else selector)
            except NotFound:
                record = replica.load(selector) if remote and UID.fullmatch(selector) else None
                if record and (record["project"], record["name"]) == (project, name):
                    return replica.manifest_of(record)
                raise
        return fallback.resolve_version(backend, Ref(project, name, selector))[2]

    def refresh(self) -> "Version":
        """Read the manifest again (a pending version learns its number once it is uploaded)."""
        uid = self.manifest["uid"]
        try:
            self._manifest = self.artifact.backend.get_version(self.artifact.project.name, self.artifact.name, uid)
        except (NotFound, Unreachable):
            if not self.manifest.get("pending"):
                raise
            record = replica.load(uid)
            if record is not None:
                self._manifest = replica.manifest_of(record)
        return self

    # ----------------------------------------------------------- the manifest

    @property
    def number(self) -> int | None:
        return self.manifest["version"]

    @property
    def uid(self) -> str:
        return self.manifest["uid"]

    @property
    def ref(self) -> str:
        """The exact reference: `project:name@v<n>`, or `project:name@<uid>` while the version is pending."""
        return replica.exact_ref(self.artifact.project.name, self.artifact.name, self.manifest)

    @property
    def pending(self) -> bool:
        return bool(self.manifest.get("pending"))

    label = property(lambda self: self.manifest.get("label"))
    status = property(lambda self: self.manifest["status"])
    created = property(lambda self: self.manifest.get("created"))
    completed = property(lambda self: self.manifest.get("completed"))
    metadata = property(lambda self: dict(self.manifest.get("metadata") or {}))
    inputs = property(lambda self: list(self.manifest.get("inputs") or []))
    source = property(lambda self: self.manifest.get("source"))

    @property
    def files(self) -> list[File]:
        return [File(entry["path"], entry.get("size", 0), entry.get("sha256"), entry.get("link")) for entry in self.manifest["files"]]

    @property
    def consumers(self) -> list[str]:
        """The versions that consumed this one, as exact references."""
        if self.number is None:
            return []
        return self.artifact.backend.lineage(self.ref)["consumers"]

    # ------------------------------------------------------------- reading

    def _usable(self) -> None:
        if self.pending and (self.status != "pending" or self.manifest.get("sync") != "pending"):
            what = {"running": "still running", "failed": "failed"}.get(self.status, self.manifest.get("sync"))
            raise StoreError(f"{self.ref} is {what} on this machine and not on the store server: it cannot be read")

    def _record(self, record: bool) -> None:
        if record and (self.number is not None or self.pending):
            record_input(self.ref)

    def pull(self, *, record: bool = True) -> Path:
        """The whole version, read-only (`<cache>/versions/<uid>/files/`, or `<state>/versions/<uid>/files/` while it
        is pending); recorded as an input."""
        self._usable()
        folder = fallback.local_folder(self.artifact.backend, self.artifact.project.name, self.artifact.name, self.manifest)
        self._record(record)
        return folder

    def open(self, path: str, *, record: bool = True):
        """A binary stream of one file, without pulling the whole version (recorded as an input, like `pull`)."""
        self._usable()
        entry = resolve_entry(self.manifest["files"], path)
        if entry is None:
            raise NotFound(f"{self.ref} has no file {path}")
        from ..store.cache import cached, files_folder, touch, version_folder

        backend = self.artifact.backend
        local = files_folder(cache_root(), self.uid) / path
        if self.pending:
            local = fallback.local_folder(backend, self.artifact.project.name, self.artifact.name, self.manifest) / path
        elif cached(self.uid) is not None:
            touch(version_folder(cache_root(), self.uid))
        if local.is_file():
            handle = local.open("rb")
        elif isinstance(backend, LocalStore):
            handle = backend.blob_path(entry["sha256"]).open("rb")
        else:
            read = backend.read_file(self.artifact.project.name, self.artifact.name, f"v{self.number}", path)
            handle = io.BufferedReader(_Chunks(read))
        self._record(record)
        return handle


class _Chunks(io.RawIOBase):
    """A raw binary stream over a `FileRead`'s chunks."""

    def __init__(self, read):
        self.read_, self.iterator, self.buffer = read, iter(read.chunks), b""

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:
        while not self.buffer:
            try:
                self.buffer = next(self.iterator)
            except StopIteration:
                return 0
        size = min(len(target), len(self.buffer))
        target[:size], self.buffer = self.buffer[:size], self.buffer[size:]
        return size

    def close(self) -> None:
        if not self.closed:
            self.read_.close()
        super().close()


# -------------------------------------------------------------------- connect


def connect(url_or_path: str | Path | None = None, *, token: str | None = None, project: str | None = None) -> Store:
    """A store: None for the one `[store]` in cairn.toml names, a server URL, or a store folder.

    `project` is the default project (default: the configured one). Connecting to a server for which this
    machine has pending versions starts the background uploader."""
    from .http import Client, connect as client_for, project_name
    from .open import open_store

    if isinstance(url_or_path, Store):
        return url_or_path
    if isinstance(url_or_path, (LocalStore, RemoteStore)):
        backend = url_or_path
    elif url_or_path is None:
        config = load_config()
        backend = open_store(config)
        if project:
            backend.project = refs.valid_project(project)
        if isinstance(backend, RemoteStore) and token is not None:
            backend.client.token = token
    else:
        try:
            config = load_config()
            default = project or project_name(config)
        except (SystemExit, ValueError):
            config, default = None, project
        text = str(url_or_path)
        if text.startswith(("http://", "https://")):
            client = Client(text, token, allow_http=bool(config and config.store.get("allow_http"))) if token is not None \
                else client_for(config, text)
            backend = RemoteStore(client, default)
        else:
            backend = LocalStore(Path(text).expanduser(), default)
    store = Store(backend)
    if store.remote:
        from .sync import has_pending, start_uploader

        if has_pending(backend.url):
            start_uploader(backend, first_delay=0)
    return store
