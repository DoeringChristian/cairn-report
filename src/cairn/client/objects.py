"""The object API: a repo, its projects, their artifacts, and the artifacts' versions.

    import cairn

    repo = cairn.connect()                        # the project's repo (with its remote, if [repo] remote names one)
    project = repo.project()                      # the configured project; repo.project("other") for another
    crops = project.artifact("datasets/crops")
    v3 = crops.version(3)                         # also "v3", an alias, a label, or a uid; crops.latest
    v3.files, v3.metadata, v3.inputs, v3.consumers
    with v3.open("tables/cases.csv") as handle:  # streams one file (recorded as an input, like pull)
        ...
    folder = v3.pull()                            # the whole version, read-only, in the repo
    fit = project.artifact("fit").publish("outputs/fit", metadata={"loss": 0.1}, aliases=["best"])
    fit.ref                                       # "study:fit@v4"; a pending version: number None, ref by uid
    v3.delete(reason="bad crop")                  # a tombstone; refused for protected versions unless force=True
    project.retention = [{"prefix": "runs/", "keep_last": 5}]   # applied by repo.gc(dry_run=False)

Objects are thin and lazy: a `Project` reads its record, an `Artifact` its summary, and a `Version` its
manifest on first access and keep them. The backend is a LocalRepo (no remote) or a SyncedRepo (repo.py). With a
remote, publishing goes through the outbox (outbox.py, sync.py): the version is committed there first, and when
the remote does not answer, `publish` returns a pending `Version` (status `pending`, number None) that is usable at
once (`pull`, `open`, as an input by its uid), and it is sent once the remote answers (a background uploader, the
next command, `cairn sync`).
"""

from __future__ import annotations

import io
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import Config, load_config
from ..store import IncompleteRun, StoreError, read_inputs, record_input, refs, run_status
from ..store.blobs import resolve_entry
from ..store.errors import Deleted, NotFound, Unreachable
from ..store.gc import GRACE
from ..store.outbox import exact_ref, manifest_of
from ..store.refs import Ref
from ..store.uids import UID
from .repo import ProjectRepo


@dataclass(frozen=True)
class File:
    """One file of a version (`sha256` None and `link` set for a stored symlink)."""
    path: str
    size: int
    sha256: str | None
    link: str | None = None


def _source(local: Path) -> dict:
    return {"host": socket.gethostname(), "folder": str(local)}


class Repo:
    """The project's repo (`backend`: a client/repo.py ProjectRepo, with or without a remote)."""

    def __init__(self, backend: ProjectRepo):
        if not isinstance(backend, ProjectRepo):  # a LocalRepo, a repo folder, a Remote, a remote's URL
            from .open import as_repo

            backend = as_repo(backend)
        self.backend = backend

    def __repr__(self) -> str:
        return f"<cairn.Repo {self.backend.root}{f' remote {self.backend.url}' if self.backend.url else ''}>"

    @property
    def remote(self) -> str | None:
        """The remote's address (the source of truth for numbers, aliases, and deletions), else None."""
        return self.backend.url

    @property
    def uid(self) -> str | None:
        """The uid of the source of truth: the remote's repo, else this repo."""
        return self.backend.uid

    @property
    def root(self) -> Path:
        return self.backend.root

    # ------------------------------------------------------------- projects

    def projects(self) -> list["Project"]:
        return [Project(self, item["name"], item) for item in self.backend.projects()]

    def project(self, name: str | None = None) -> "Project":
        """A project by name; None means the configured default (`[repo] project`)."""
        name = name or self.backend.project
        if name is None:
            raise StoreError("no project named and the repo has no default")
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
        """The version a reference names (`[project:]name[@selector]`), resolved now (with the offline fallbacks). An
        exact reference (`@v<n>`, `@<uid>`) also names a running or failed version."""
        parsed = refs.parse(ref)
        try:
            project, name, manifest = self.backend.resolve(parsed, offline)
        except (Deleted, NotFound, Unreachable):
            raise
        except StoreError:
            found = self._exact_version(ref) if parsed.exact else None
            if found is None:
                raise
            return found
        return Version(self.project(project).artifact(name), manifest)

    def pull(self, ref: str, *, record: bool = True, offline: bool = False) -> Path:
        """A read-only local copy of the version (or of the file after `//`) a reference names."""
        return self.backend.pull(ref, record, offline)

    def _exact_version(self, ref: str) -> "Version | None":
        """The version an exact reference names, whatever its status (a deleted one as its tombstone), else None."""
        try:
            project, name, manifest = self.backend.resolve(refs.parse(ref))
            return Version(self.project(project).artifact(name), manifest)
        except (StoreError, ValueError):
            pass
        try:
            parsed = refs.parse(ref)
            project = parsed.project or self.backend.project
            selector = parsed.version if parsed.version is not None else parsed.uid
            if selector is None:
                return None
            return Version(self.project(project).artifact(parsed.name), self.backend.get_version(project, parsed.name, selector))
        except (StoreError, ValueError):
            return None

    def pending(self) -> list["Version"]:
        """The versions of this repo the remote does not have yet: pending, running, rejected (none without a remote)."""
        return [Version.from_record(self, record) for record in self.backend.pending()]

    def verify(self, ref: str | None = None, *, repair: bool = False):
        """Re-hash what this repo holds and return a `Report` (`.ok`, `.problems`, `.summary()`; `cairn verify`).

        `ref`: one version (its digest, its read-only folder, its blobs). Without: every blob against its name,
        every version of the database against its blobs and digest, every read-only folder, every outbox entry.
        `repair`: remove what can be made again (a broken read-only folder; a broken blob only a copy of the
        remote's version needs), so the next pull makes or downloads it again; never the only copy of anything."""
        return self.backend.verify(ref, repair=repair)

    def sync(self, report=None) -> dict:
        """Send the pending versions now (`Sync.flush`), then mirror the remote's aliases of the artifacts this repo
        has versions of (`refreshed`: how many; skipped while the remote does not answer)."""
        return self.backend.sync(report)

    def gc(self, dry_run: bool = True, grace: float | str = GRACE) -> dict:
        """Apply the retention rules and remove unreferenced blobs older than `grace` (LocalRepo.gc; with a remote, on
        the remote: the owner token only). `dry_run` (the default) only says what would go."""
        return self.backend.gc(dry_run=dry_run, grace=grace)


class Project:
    def __init__(self, store: Repo, name: str, record: dict | None = None):
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

    @property
    def retention(self) -> list[dict]:
        """The retention rules, [{prefix, keep_last, keep_days, keep_aliased}]; they act when `Store.gc` runs."""
        return self.store.backend.retention(self.name)

    @retention.setter
    def retention(self, rules: list[dict]) -> None:
        self.store.backend.set_retention(self.name, rules)

    def set_retention(self, prefix: str = "", *, keep_last: int | None = None, keep_days: float | None = None,
                      keep_aliased: bool = True) -> list[dict]:
        """Add or replace the rule for one artifact-name prefix ("" for the whole project); returns every rule."""
        rules = [rule for rule in self.retention if rule["prefix"] != prefix]
        rules.append({"prefix": prefix, "keep_last": keep_last, "keep_days": keep_days, "keep_aliased": keep_aliased})
        return self.store.backend.set_retention(self.name, rules)

    def delete(self, *, force: bool = False, reason: str | None = None) -> dict:
        """Delete the project: refused while it has versions that are not deleted, unless `force` (owner only on a server)."""
        return self.store.backend.delete_project(self.name, reason=reason, force=force)


class Artifact:
    def __init__(self, project: Project, name: str, summary: dict | None = None):
        self.project, self.name, self._summary = project, name, summary

    def __repr__(self) -> str:
        return f"<cairn.Artifact {self.project.name}:{self.name}>"

    @property
    def store(self) -> Repo:
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
        """Every version, oldest first, whatever its status; this repo's versions the remote lacks come last (pending)."""
        try:
            return [Version(self, manifest) for manifest in self.backend.versions(self.project.name, self.name)]
        except NotFound:
            return []

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
            raise StoreError(f"{version.ref} is pending (the remote does not have it yet): alias it once it is sent"
                             " (or pass aliases= to publish)")
        self.backend.set_alias(self.project.name, self.name, name, version.number)

    def delete(self, *, force: bool = False, reason: str | None = None) -> dict:
        """Delete every version (all or none, under the rules of `Version.delete`); returns {deleted, skipped}."""
        result = self.backend.delete_artifact(self.project.name, self.name, reason=reason, force=force)
        self._summary = None
        return result

    def lineage(self, version: "Version | int | str") -> dict[str, list[str]]:
        version = version if isinstance(version, Version) else self.version(version)
        return {"inputs": version.inputs, "consumers": version.consumers}

    # ------------------------------------------------------------ publishing

    def publish(self, folder: Path | str, *, metadata: dict | None = None, aliases=(), label: str | None = None,
                inputs=None, config: Config | None = None, allow_incomplete: bool = False, _link: bool = False,
                _uid: str | None = None) -> "Version":
        """Store a folder as the next version; when nothing changed since the latest, that version is returned.

        A symlinked folder is followed (the label defaults to the resolved folder's name). The folder's
        `.cairn-inputs.jsonl` and `inputs` become the version's inputs. With a remote the version is committed
        to the outbox first; if the remote does not answer, a pending Version is returned (usable at once) and
        sent in the background (and by `cairn sync` or the next cairn command).

        A folder whose `run.json` says the run is not `completed` is refused unless `allow_incomplete`;
        then the status is recorded as `metadata.run_status`.
        """
        local = Path(folder).resolve()
        if not local.is_dir():
            raise ValueError(f"{folder} is not a folder")
        status = run_status(local)
        if status not in (None, "completed"):
            if not allow_incomplete:
                raise IncompleteRun(f"{folder}/run.json says {status}; publish completed runs, or pass --allow-incomplete"
                                 " (allow_incomplete=True)")
            metadata = {**(metadata or {}), "run_status": status}
        config = config or load_config()
        merged = [*read_inputs(local), *(str(item) for item in inputs or [])]
        manifest = self.backend.publish_folder(self.project.name, self.name, local, exclude=config.repo.get("exclude", []),
                                               project_root=config.project, link=_link, label=label or local.name,
                                               metadata=metadata, inputs=merged, aliases=aliases, source=_source(local), uid=_uid)
        return Version(self, manifest)

    def draft(self, *, label: str | None = None, metadata: dict | None = None, aliases=(), watch: bool = False):
        """A fresh, empty folder managed by Cairn that becomes this artifact's next version when it closes (a `Draft`,
        a context manager with the `add`/`add_*` writer; see `cairn.draft`)."""
        from .drafts import Draft

        return Draft(f"{self.project.name}:{self.name}", label=label, metadata=metadata, aliases=aliases, watch=watch,
                     store=self.store)

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
    def from_record(cls, store: Repo, record: dict) -> "Version":
        """A version from an outbox entry (one the remote does not have yet)."""
        return cls(store.project(record["project"]).artifact(record["name"]), manifest_of(record))

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
            return backend.get_version(project, name, int(selector[1:]) if refs.VERSION.fullmatch(selector) else selector)
        return backend.resolve(Ref(project, name, selector))[2]

    def refresh(self) -> "Version":
        """Read the manifest again (a pending version learns its number once the remote has it)."""
        uid = self.manifest["uid"]
        try:
            self._manifest = self.artifact.backend.get_version(self.artifact.project.name, self.artifact.name, uid)
        except (NotFound, Unreachable):
            if not self.manifest.get("pending"):
                raise
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
        return exact_ref(self.artifact.project.name, self.artifact.name, self.manifest)

    @property
    def pending(self) -> bool:
        return bool(self.manifest.get("pending"))

    @property
    def deleted(self) -> bool:
        """A tombstone: `deleted_at`, `deleted_by`, and `reason` are in its manifest, its files are gone."""
        return self.manifest["status"] == "deleted"

    def delete(self, *, force: bool = False, reason: str | None = None) -> dict:
        """Delete this version: it becomes a tombstone (number and uid never used again), `latest` moves back, and
        its blobs go at the next `Store.gc` unless other versions name them. Refused (Protected) when an alias
        other than `latest` points at it, a version that is not deleted consumed it, or it is running, unless
        `force` (the owner token on a server)."""
        if self.number is None:
            raise StoreError(f"{self.ref} is pending (the remote does not have it yet): nothing to delete there")
        number = self.number
        result = self.artifact.backend.delete_version(self.artifact.project.name, self.artifact.name, number, reason=reason, force=force)
        self._manifest = self.artifact.backend.get_version(self.artifact.project.name, self.artifact.name, number)  # the tombstone
        return result

    label = property(lambda self: self.manifest.get("label"))
    status = property(lambda self: self.manifest["status"])
    created = property(lambda self: self.manifest.get("created"))
    completed = property(lambda self: self.manifest.get("completed"))
    metadata = property(lambda self: dict(self.manifest.get("metadata") or {}))
    inputs = property(lambda self: list(self.manifest.get("inputs") or []))
    source = property(lambda self: self.manifest.get("source"))
    digest = property(lambda self: self.manifest.get("digest"))  # `sha256:<hex>` of the file list (store/digest.py); None while running

    @property
    def files(self) -> list[File]:
        return [File(entry["path"], entry.get("size", 0), entry.get("sha256"), entry.get("link")) for entry in self.manifest["files"]]

    @property
    def consumers(self) -> list[str]:
        """The versions that consumed this one, as exact references."""
        if self.number is None:
            return []
        return self.artifact.backend.lineage(self.ref)["consumers"]

    # ------------------------------------------------------------- runs and lineage

    @property
    def is_run(self) -> bool:
        """Whether this version is a run (`metadata.kind == "run"`, cairn/client/runs.py)."""
        return (self.manifest.get("metadata") or {}).get("kind") == "run"

    @property
    def run(self) -> "Version | None":
        """The run this version is an output of (the run among its inputs), else None."""
        for ref in self.inputs:
            found = self.artifact.store._exact_version(ref)
            if found is not None and found.is_run:
                return found
        return None

    def upstream(self, *, _runs: list | None = None) -> list["Version"]:
        """The versions this one comes from, looking through runs: every input that is a run is replaced by that
        run's own inputs (a sweep's summary run by its trials' inputs). `inputs` stays the direct edges."""
        return self._through("inputs", _runs)

    def downstream(self, *, _runs: list | None = None) -> list["Version"]:
        """The versions made from this one, looking through runs: every consumer that is a run is replaced by what
        that run published (its consumers). `consumers` stays the direct edges."""
        return self._through("consumers", _runs)

    def _through(self, side: str, runs: list | None) -> list["Version"]:
        store = self.artifact.store
        found: list[Version] = []
        seen = {self.ref}

        def walk(items: list[str]) -> None:
            for ref in items:
                if ref in seen:
                    continue
                seen.add(ref)
                version = store._exact_version(ref)
                if version is None:
                    continue
                if version.is_run:
                    if runs is not None and version.ref not in runs:
                        runs.append(version.ref)
                    try:
                        walk(version.inputs if side == "inputs" else version.consumers)
                    except StoreError:
                        pass
                elif version not in found:
                    found.append(version)

        walk(self.inputs if side == "inputs" else self.consumers)
        return found

    @property
    def path(self) -> Path:
        """The whole version, read-only, as a local folder (materialized on first use; not recorded again)."""
        return self.pull(record=False)

    # ------------------------------------------------------------- reading

    def _usable(self) -> None:
        if self.deleted:
            raise Deleted(f"{self.ref} was deleted: its files are gone")
        if self.pending and (self.status != "pending" or self.manifest.get("sync") != "pending"):
            what = {"running": "still running", "failed": "failed"}.get(self.status, self.manifest.get("sync"))
            raise StoreError(f"{self.ref} is {what} in this repo and the remote does not have it: it cannot be read")

    def _record(self, record: bool) -> None:
        if record and (self.number is not None or self.pending):
            record_input(self.ref, self.uid)

    def pull(self, *, record: bool = True) -> Path:
        """The whole version, read-only (`<repo>/versions/<uid>/files/`); recorded as an input."""
        self._usable()
        folder = self.artifact.backend.local_folder(self.artifact.project.name, self.artifact.name, self.manifest)
        self._record(record)
        return folder

    def open(self, path: str, *, record: bool = True):
        """A binary stream of one file, without pulling the whole version (recorded as an input, like `pull`)."""
        self._usable()
        entry = resolve_entry(self.manifest["files"], path)
        if entry is None:
            raise NotFound(f"{self.ref} has no file {path}")
        from ..store.folders import cached, files_folder, touch, version_folder

        backend = self.artifact.backend
        root = backend.root
        local = files_folder(root, self.uid) / path
        if cached(root, self.uid) is not None:
            touch(version_folder(root, self.uid))
        if local.is_file():
            handle = local.open("rb")
        else:
            selector = f"v{self.number}" if self.number is not None else self.uid
            read = backend.read_file(self.artifact.project.name, self.artifact.name, selector, path)
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


def connect(url_or_path: str | Path | None = None, *, token: str | None = None, project: str | None = None) -> Repo:
    """A repo: None for the project's (with its remote, if `[repo] remote` names one), a remote's URL (with the project's
    repo for its outbox and files, and as its cache when it is the project's own remote), or a repo folder on this
    machine. `project` is the default project (default: the configured one). Connecting to a repo whose outbox holds
    pending versions starts the background uploader."""
    from .open import as_repo, open_remote

    if isinstance(url_or_path, Repo):
        return url_or_path
    if isinstance(url_or_path, str) and url_or_path.startswith(("http://", "https://")):
        backend = open_remote(url_or_path, token=token, project=project)
    else:
        backend = as_repo(url_or_path)
        if project:
            backend.project = refs.valid_project(project)
        if token is not None:
            backend.use_token(token)
    backend.resume()
    return Repo(backend)
