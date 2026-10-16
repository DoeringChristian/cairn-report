"""The object API: the project's repo, its artifacts, and the artifacts' versions.

    import cairn

    repo = cairn.connect()                        # the project's repo (with its remote, if [repo] remote names one)
    crops = repo.artifact("datasets/crops")       # repo.artifacts(prefix="datasets/")
    v3 = crops.version(3)                         # also "v3", an alias, a label, a uid, or a short id; crops.latest
    v3.files, v3.metadata, v3.inputs, v3.consumers, v3.short_id
    with v3.open("tables/cases.csv") as handle:  # streams one file (recorded as an input, like pull)
        ...
    folder = v3.pull()                            # the whole version, read-only, in the repo
    fit = repo.artifact("fit").publish("outputs/fit", metadata={"loss": 0.1}, aliases=["best"])
    fit.ref                                       # "fit@v4"; a pending version: number None, ref by uid
    v3.delete(reason="bad crop")                  # a tombstone; refused for protected versions unless force=True
    repo.retention = [{"prefix": "runs/", "keep_last": 5}]   # applied by repo.gc(dry_run=False)

A repo holds one project, so nothing here names one. Objects are thin and lazy: an `Artifact` reads its summary and
a `Version` its manifest on first access and keep them. The backend is a client/repo.py ProjectRepo, without or with
a remote. With a remote, publishing goes through the outbox (outbox.py, sync.py): the version is committed there
first, and when the remote does not answer, `publish` returns a pending `Version` (status `pending`, number None) that
is usable at once (`pull`, `open`, as an input by its uid), and it is sent once the remote answers (a background
uploader, the next command, `cairn sync`).

Every public method and property raises Cairn's errors from the caller's call (cairn/api.py).
"""

from __future__ import annotations

import io
import socket
from dataclasses import dataclass
from pathlib import Path

from ..api import public, surface
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


@surface
class Repo:
    """The project's repo (`backend`: a client/repo.py ProjectRepo, with or without a remote)."""

    def __init__(self, backend: ProjectRepo):
        if not isinstance(backend, ProjectRepo):  # a repo folder, a remote's URL
            from .open import as_repo

            backend = as_repo(backend)
        self.backend = backend

    def __repr__(self) -> str:
        return f"<cairn.Repo {self.backend.root}{f' remote {self.backend.url}' if self.backend.url else ''}>"

    @property
    def remote(self) -> str | None:
        """The remote's address, `<server>/<project>` (the source of truth for numbers, aliases, and deletions), else None."""
        return self.backend.url

    @property
    def uid(self) -> str | None:
        """The uid of the source of truth: the remote's server, else this repo."""
        return self.backend.uid

    @property
    def root(self) -> Path:
        return self.backend.root

    def artifacts(self, prefix: str | None = None) -> list["Artifact"]:
        """The artifacts (names starting with `prefix`, e.g. "reports/"), sorted by name."""
        found = [Artifact(self, item["name"], item) for item in self.backend.summaries()]
        return sorted((item for item in found if not prefix or item.name.startswith(prefix)), key=lambda item: item.name)

    def artifact(self, name: str) -> "Artifact":
        """An artifact by name (lazy: it need not exist yet)."""
        return Artifact(self, refs.valid_name(name))

    def version(self, ref: str, *, offline: bool = False) -> "Version":
        """The version a reference names (`name[@selector]`, or a bare short id), resolved now (with the offline
        fallbacks). An exact reference (`@v<n>`, `@<uid>`) or a short id also names a running or failed version; a
        deleted one raises Deleted."""
        name, manifest = self.backend.find(refs.parse(ref), offline)
        version = Version(self.artifact(name), manifest)
        if version.deleted:
            raise Deleted(f"{version.ref} was deleted ({(manifest.get('deleted_at') or '')[:19]})"
                          + (f": {manifest['reason']}" if manifest.get("reason") else ""))
        return version

    def pull(self, ref: str, *, record: bool = True, offline: bool = False) -> Path:
        """A read-only local copy of the version (or of the file after `//`) a reference names."""
        return self.backend.pull(ref, record, offline)

    def _exact_version(self, ref: str) -> "Version | None":
        """The version an exact reference names, whatever its status (a deleted one as its tombstone), else None."""
        try:
            name, manifest = self.backend.find(refs.parse(ref))
        except (StoreError, ValueError):
            return None
        return Version(self.artifact(name), manifest)

    def pending(self) -> list["Version"]:
        """The versions of this repo the remote does not have yet: pending, running, rejected (none without a remote)."""
        return [Version.from_record(self, record) for record in self.backend.pending()]

    @property
    def retention(self) -> list[dict]:
        """The retention rules, [{prefix, keep_last, keep_days, keep_aliased}]; they act when `gc` runs."""
        return self.backend.retention()

    @retention.setter
    def retention(self, rules: list[dict]) -> None:
        self.backend.set_retention(rules)

    def set_retention(self, prefix: str = "", *, keep_last: int | None = None, keep_days: float | None = None,
                      keep_aliased: bool = True) -> list[dict]:
        """Add or replace the rule for one artifact-name prefix ("" for every artifact); returns every rule."""
        rules = [rule for rule in self.backend.retention() if rule["prefix"] != prefix]
        rules.append({"prefix": prefix, "keep_last": keep_last, "keep_days": keep_days, "keep_aliased": keep_aliased})
        return self.backend.set_retention(rules)

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


@surface
class Artifact:
    def __init__(self, repo: Repo, name: str, summary: dict | None = None):
        self.repo, self.name, self._summary = repo, name, summary

    def __repr__(self) -> str:
        return f"<cairn.Artifact {self.name}>"

    @property
    def backend(self):
        return self.repo.backend

    @property
    def summary(self) -> dict:
        if self._summary is None:
            self._summary = self.backend.summary(self.name)
        return self._summary

    @property
    def aliases(self) -> dict[str, int]:
        return self.backend.aliases(self.name)

    def versions(self) -> list["Version"]:
        """Every version, oldest first, whatever its status; this repo's versions the remote lacks come last (pending)."""
        try:
            return [Version(self, manifest) for manifest in self.backend.versions(self.name)]
        except NotFound:
            return []

    def version(self, selector: int | str) -> "Version":
        """A version by number (3 or "v3"), uid, short id, alias, or label (completed versions only for the last two).
        Lazy."""
        if isinstance(selector, bool) or not isinstance(selector, (int, str)):
            raise TypeError("a version is a number, 'v<n>', a uid, a short id, an alias, or a label")
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
        self.backend.set_alias(self.name, name, version.number)

    def delete(self, *, force: bool = False, reason: str | None = None) -> dict:
        """Delete every version (all or none, under the rules of `Version.delete`); returns {deleted, skipped}."""
        result = self.backend.delete_artifact(self.name, reason=reason, force=force)
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
            raise StoreError(f"{folder} is not a folder")
        status = run_status(local)
        if status not in (None, "completed"):
            if not allow_incomplete:
                raise IncompleteRun(f"{folder}/run.json says {status}; publish completed runs, or pass --allow-incomplete"
                                    " (allow_incomplete=True)")
            metadata = {**(metadata or {}), "run_status": status}
        config = config or load_config()
        merged = [*read_inputs(local), *(str(item) for item in inputs or [])]
        manifest = self.backend.publish_folder(self.name, local, exclude=config.repo.get("exclude", []),
                                               project_root=config.project, link=_link, label=label or local.name,
                                               metadata=metadata, inputs=merged, aliases=aliases, source=_source(local), uid=_uid)
        return Version(self, manifest)

    def draft(self, *, label: str | None = None, metadata: dict | None = None, aliases=(), watch: bool = False):
        """A fresh, empty folder managed by Cairn that becomes this artifact's next version when it closes (a `Draft`,
        a context manager with the `add`/`add_*` writer; see `cairn.draft`)."""
        from .drafts import Draft

        return Draft(self.name, label=label, metadata=metadata, aliases=aliases, watch=watch, _repo=self.repo)

    def publishing(self, folder: Path | str, *, metadata: dict | None = None, aliases=(), label: str | None = None,
                   inputs=None):
        """A running version that follows a folder while a job writes it (a `Publication`, a context manager)."""
        from .publishing import Publication

        return Publication(self.name, folder, metadata=metadata, aliases=aliases, label=label, inputs=inputs,
                           _repo=self.backend)


@surface
class Version:
    """One version of an artifact. `number` is None while it is pending: on this machine, not yet on the server."""

    def __init__(self, artifact: Artifact, manifest: dict | None, selector: str | None = None):
        self.artifact, self._manifest, self._selector = artifact, manifest, selector

    @classmethod
    def from_record(cls, repo: Repo, record: dict) -> "Version":
        """A version from an outbox entry (one the remote does not have yet)."""
        return cls(repo.artifact(record["name"]), manifest_of(record))

    def __repr__(self) -> str:
        if self._manifest is None:
            return f"<cairn.Version {self.artifact.name}@{self._selector}>"
        return f"<cairn.Version {self.ref} ({self.short_id})>"

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
        backend = artifact.backend
        if refs.VERSION.fullmatch(selector) or UID.fullmatch(selector):  # any status, like versions()
            return backend.get_version(artifact.name, int(selector[1:]) if refs.VERSION.fullmatch(selector) else selector)
        return backend.find(Ref(artifact.name, selector))[1]

    def refresh(self) -> "Version":
        """Read the manifest again (a pending version learns its number once the remote has it)."""
        uid = self.manifest["uid"]
        try:
            self._manifest = self.artifact.backend.get_version(self.artifact.name, uid)
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
    def short_id(self) -> str:
        """The first hex digits of the SHA-256 of the uid (7, more when needed to be unique in the project); any unique
        prefix of 4 or more names the version (`name@<id>`, or a bare `<id>`)."""
        return refs.short_of(self.manifest)

    @property
    def ref(self) -> str:
        """The exact reference: `name@v<n>`, or `name@<uid>` while the version is pending."""
        return exact_ref(self.artifact.name, self.manifest)

    @property
    def pending(self) -> bool:
        return bool(self.manifest.get("pending"))

    @property
    def deleted(self) -> bool:
        """A tombstone: `deleted_at`, `deleted_by`, and `reason` are in its manifest, its files are gone."""
        return self.manifest["status"] == "deleted"

    def delete(self, *, force: bool = False, reason: str | None = None) -> dict:
        """Delete this version: it becomes a tombstone (number and uid never used again), `latest` moves back, and
        its blobs go at the next `Repo.gc` unless other versions name them. Refused (Protected) when an alias
        other than `latest` points at it, a version that is not deleted consumed it, or it is running, unless
        `force` (the owner token on a server)."""
        if self.number is None:
            raise StoreError(f"{self.ref} is pending (the remote does not have it yet): nothing to delete there")
        number = self.number
        result = self.artifact.backend.delete_version(self.artifact.name, number, reason=reason, force=force)
        self._manifest = self.artifact.backend.get_version(self.artifact.name, number)  # the tombstone
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
            found = self.artifact.repo._exact_version(ref)
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
        repo = self.artifact.repo
        found: list[Version] = []
        seen = {self.ref}

        def walk(items: list[str]) -> None:
            for ref in items:
                if ref in seen:
                    continue
                seen.add(ref)
                version = repo._exact_version(ref)
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
        if record and self.artifact.backend.own and (self.number is not None or self.pending):
            record_input(self.ref, self.uid)

    def pull(self, *, record: bool = True) -> Path:
        """The whole version, read-only (`<repo>/versions/<uid>/files/`); recorded as an input."""
        self._usable()
        folder = self.artifact.backend.local_folder(self.artifact.name, self.manifest)
        self._record(record)
        return folder

    def open(self, path: str, *, record: bool = True):
        """A binary stream of one file, without pulling the whole version (recorded as an input, like `pull`)."""
        self._usable()
        entry = resolve_entry(self.manifest["files"], path)
        if entry is None:
            raise NotFound(f"{self.ref} has no file {path}{self.artifact.backend.where}")
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
            read = backend.read_file(self.artifact.name, selector, path)
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


@public("cairn.connect")
def connect(path_or_url: str | Path | None = None, *, token: str | None = None) -> Repo:
    """The project's repo (with its remote, if `[repo] remote` names one); for an admin or a test, a repo folder on this
    machine or a remote project `<server>/<project>` (with the working directory's repo for its outbox and files).
    Another project's repo never feeds this project's runs: its pulls record no inputs. Connecting to a repo whose
    outbox holds pending versions starts the background uploader."""
    from .open import as_repo, open_remote

    if isinstance(path_or_url, Repo):
        return path_or_url
    if isinstance(path_or_url, str) and path_or_url.startswith(("http://", "https://")):
        backend = open_remote(path_or_url, token=token)
    else:
        backend = as_repo(path_or_url)
        if token is not None:
            backend.use_token(token)
    backend.resume()
    return Repo(backend)
