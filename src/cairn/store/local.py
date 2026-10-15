"""LocalStore: projects, artifacts, and immutable versions in a folder.

    <root>/
      store.json                                       the store's uid (UUIDv7), made once, never changed
      blobs/<aa>/<sha256-rest>                         file contents (blobs.BlobStore)
      projects/<p>/project.json                        name, title, description, created
      projects/<p>/consumers.jsonl                     consumer ref -> input ref, when a version with inputs completes
      projects/<p>/artifacts/<name>/versions.jsonl     one line per version state change; the last line per version wins
      projects/<p>/artifacts/<name>/aliases.jsonl      one line per alias move: alias, version, time
      projects/<p>/artifacts/<name>/.numbers/<n>       empty; made with O_EXCL to claim version number n
      projects/<p>/changes.jsonl                       a hint per write: the artifact's name (for the index)

Everything is append-only. A version starts `running` (it may grow), then becomes `completed` or
`failed`; a completed version is never written again. Only completed versions resolve through
`latest`, aliases, and labels, so readers never see half-published data.

Every version has a `uid` (a UUIDv7, see uids.py), made when it starts or supplied by the client
that created it (an outbox); a uid names one version for good. A store written before uids existed
is upgraded as it is read: each version gets a deterministic uid (`uids.legacy`), persisted with one
more state line once the version is final, and the root gets its store.json.

Reads go through an in-memory index (index.py), loaded lazily per project and kept current from the
files' appended lines, which picks up writes by other processes on this host too; see index.py for the
consistency model. Writers to one artifact take that artifact's lock (writers to different artifacts
run in parallel); every append is one write on an O_APPEND descriptor, then fsync. A version number is
claimed by creating `.numbers/<n>` exclusively, so two processes never hand out the same number (a
process that dies right after claiming leaves a gap in the numbers, never a duplicate). Other checks
(a uid sent twice, "nothing changed since latest") hold within one process: run one writer process per
store root, usually `cairn server`, and never write one store root from several hosts (fs.py).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..ranges import byte_range
from . import refs
from .errors import BadRange, Conflict, Forbidden, NotFound, StoreError, Unauthorized  # noqa: F401
from .blobs import CHUNK, BlobStore, BlobWriter, append_bytes, append_lines, clean_entries, now, resolve_entry
from .fs import network_filesystem
from .index import ArtifactIndex, ProjectIndex
from .refs import Ref
from .uids import UID, legacy, new_uid, valid_uid


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


def _same(version: dict, files: list[dict], inputs: list[str], metadata: dict) -> bool:
    def strip(entries):
        return [(entry["path"], entry.get("sha256"), entry.get("link")) for entry in entries]

    return strip(version["files"]) == strip(files) and sorted(version["inputs"]) == sorted(inputs) and version["metadata"] == metadata


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


def _check_tree(entries: list[dict]) -> None:
    """No path may be both a file and a folder."""
    paths = {entry["path"] for entry in entries}
    for path in paths:
        parts = path.split("/")
        for end in range(1, len(parts)):
            if "/".join(parts[:end]) in paths:
                raise StoreError(f"{path}: {'/'.join(parts[:end])} is a file in this version")


_WARNED: set[str] = set()


class LocalStore:
    def __init__(self, root: Path | str, project: str | None = None, network_warning: bool = True):
        """`project` is the default for references without one. A store root on a network filesystem warns once
        per process when it is first written (pass `network_warning=False` when the caller has decided)."""
        self.root, self.project = Path(root), project
        self._blobs = BlobStore(self.root)  # creates blobs/, projects/, tmp/
        self.lock = threading.Lock()  # project records
        self._indexes: dict[str, ProjectIndex] = {}
        self._indexes_lock = threading.Lock()
        self._records: dict[str, tuple[tuple, dict]] = {}
        self._held = threading.local()  # the artifacts this thread holds the write lock of (see _locked)
        self._network_checked = not network_warning
        self.uid = store_uid(self.root)

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

    def _project_dir(self, project: str) -> Path:
        index = self._indexes.get(project)  # a project with an index exists (projects are never removed)
        if index is not None:
            return index.folder
        folder = self._blobs.projects / refs.valid_project(project)
        if not (folder / "project.json").is_file():
            raise NotFound(f"unknown project {project!r}")
        return folder

    def _write_record(self, folder: Path, record: dict) -> None:
        temporary = folder / f"project.json.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        temporary.write_text(json.dumps(record, indent=1))
        os.replace(temporary, folder / "project.json")

    def create_project(self, name: str, title: str | None = None, description: str = "") -> dict:
        """Create a project; an existing one is returned unchanged."""
        folder = self._blobs.projects / refs.valid_project(name)
        self._writing()
        with self.lock:
            if (folder / "project.json").is_file():
                return json.loads((folder / "project.json").read_text())
            record = {"name": name, "title": title or name, "description": description, "created": now()}
            folder.mkdir(parents=True, exist_ok=True)
            self._write_record(folder, record)
            return record

    def _record(self, file: Path) -> dict:
        """A project.json, re-read only when it changed."""
        info = file.stat()
        stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
        cached = self._records.get(str(file))
        if cached is None or cached[0] != stamp:
            cached = (stamp, json.loads(file.read_text()))
            self._records[str(file)] = cached
        return dict(cached[1])

    def get_project(self, name: str) -> dict:
        return self._record(self._project_dir(name) / "project.json")

    def update_project(self, name: str, title: str | None = None, description: str | None = None) -> dict:
        folder = self._project_dir(name)
        self._writing()
        with self.lock:
            record = json.loads((folder / "project.json").read_text())
            record.update({key: value for key, value in (("title", title), ("description", description)) if value is not None})
            self._write_record(folder, record)
            return record

    def projects(self) -> list[dict]:
        found = []
        for file in sorted(self._blobs.projects.glob("*/project.json")):
            try:
                found.append(self._record(file))
            except FileNotFoundError:
                continue
        return found

    def overviews(self) -> list[dict]:
        """Every project record with its artifact count and last update."""
        found = []
        for record in self.projects():
            summaries = self.summaries(record["name"])
            found.append({**record, "artifacts": len(summaries),
                          "updated": max((item["updated"] for item in summaries), default=record["created"])})
        return found

    # --------------------------------------------------------------- the index

    def _index(self, project: str) -> ProjectIndex:
        index = self._indexes.get(project)  # projects are never removed: a known one needs no check
        if index is not None:
            return index
        folder = self._project_dir(project)
        with self._indexes_lock:
            index = self._indexes.get(project)
            if index is None:
                index = self._indexes[project] = ProjectIndex(folder)
            return index

    def _upgrade(self, artifact: ArtifactIndex, states: dict[int, dict], numbers: list[int]) -> dict[int, str]:
        """Give versions stored without a uid their deterministic one (see the module doc); a final version keeps
        it with one more state line (no other writer appends to a final version)."""
        given, persist = {}, []
        for number in numbers:
            state = states[number]
            if state.get("uid"):
                continue
            state["uid"] = legacy(f"{self.uid}/{artifact.project}/{artifact.name}/{number}", state.get("created"))
            given[number] = state["uid"]
            if state["status"] != "running":
                persist.append(state)
        if persist:
            try:
                append_lines(artifact.folder / "versions.jsonl", persist)
            except OSError:  # a read-only store: the same uids are derived again next time
                pass
        return given

    def _artifact(self, project: str, name: str, create: bool = False) -> ArtifactIndex:
        """The artifact's index entry, up to date with its files (see index.py). Inside `_locked` for this artifact
        it is not checked again: the lock holder brought it up to date and reads back its own appends (`_write`)."""
        held = getattr(self._held, "keys", None)
        if held and (project, name) in held:
            return held[(project, name)]
        refs.valid_name(name)
        return self._index(project).artifact(name, self._upgrade, create=create)

    def _states(self, project: str, name: str) -> dict[int, dict]:
        """The latest recorded state per version number (each with its uid): a snapshot, safe to iterate."""
        return self._artifact(project, name).snapshot()[0]

    def _artifact_dir(self, project: str, name: str) -> Path:
        return self._project_dir(project) / "artifacts" / refs.valid_name(name)

    def _by_uid(self, states: dict[int, dict], uid: str) -> dict | None:
        return next((state for state in states.values() if state["uid"] == uid), None)

    def _hint(self, project: str, name: str) -> None:
        """Tell other processes' indexes which artifact changed (index.py); a hint, so no fsync. Inside `_locked`
        the hint is written once, when the lock is released."""
        dirty = getattr(self._held, "dirty", None)
        if dirty is not None and (project, name) in getattr(self._held, "keys", {}):
            dirty.add((project, name))
            return
        try:
            append_bytes(self._index(project).folder / "changes.jsonl", (json.dumps({"artifact": name}) + "\n").encode(), sync=False)
        except OSError:
            pass

    def _write(self, project: str, name: str, record: dict | list[dict]) -> None:
        artifact = self._artifact(project, name, create=True)
        append_lines(artifact.versions_tail.path, record if isinstance(record, list) else [record])
        self._hint(project, name)
        if artifact.refresh(self._upgrade):  # reads back this line (and any other process's)
            self._index(project).bump()

    def _claim(self, project: str, name: str, artifact: ArtifactIndex) -> int:
        """The next version number, claimed by creating `.numbers/<n>` exclusively (unique across processes)."""
        numbers = os.path.join(artifact.folder, ".numbers")
        if not artifact.numbers_ready:
            os.makedirs(numbers, exist_ok=True)
            artifact.numbers_ready = True
        candidate = max(artifact.snapshot()[0], default=0) + 1
        while True:
            try:
                os.close(os.open(os.path.join(numbers, str(candidate)), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                return candidate
            except FileExistsError:  # another process took it: catch up with what it wrote
                if artifact.refresh(self._upgrade):
                    self._index(project).bump()
                candidate = max(candidate + 1, max(artifact.snapshot()[0], default=0) + 1)

    def artifacts(self, project: str) -> list[str]:
        return [name for name, _ in self._index(project).fresh(self._upgrade)]

    def version_numbers(self, project: str, name: str) -> set[int]:
        """The artifact's version numbers (cheaper than `versions` when only they are needed)."""
        artifact = self._artifact(project, name)
        with artifact.lock:
            return set(artifact.states)

    def versions(self, project: str, name: str) -> list[dict]:
        return [dict(state) for _, state in sorted(self._states(project, name).items())]

    def get_version(self, project: str, name: str, version: int | str) -> dict:
        """One version's manifest, by number or uid, whatever its status."""
        artifact = self._artifact(project, name)
        with artifact.lock:
            number = artifact.by_uid.get(version) if isinstance(version, str) else version
            state = artifact.states.get(number) if number is not None else None
            if state is not None and isinstance(version, str) and state.get("uid") != version:
                state = None
            if state is not None:
                return dict(state)
        raise NotFound(f"{project}:{name} has no version {version if isinstance(version, str) else f'v{version}'}")

    def summary(self, project: str, name: str) -> dict:
        """The artifact at a glance: latest completed version (number and metadata), version count, aliases, last change."""
        return dict(self._artifact(project, name).get_summary())

    def summaries(self, project: str) -> list[dict]:
        return list(self.summaries_stamped(project)[1])

    def summaries_stamped(self, project: str) -> tuple[tuple, list[dict]]:
        """(stamp, summaries): the stamp changes whenever the listing may have; callers may cache on it."""
        index = self._index(project)
        items = index.fresh(self._upgrade)
        with index.lock:
            cached, generation = index.listing, index.generation
        if cached is None or cached[0] != generation:
            cached = (generation, [artifact.get_summary() for _, artifact in items])
            with index.lock:
                index.listing = cached
        return (id(index), cached[0]), cached[1]

    def aliases(self, project: str, name: str) -> dict[str, int]:
        """Where each alias points now (the last move wins)."""
        return self._artifact(project, name).snapshot()[1]

    def _running(self, project: str, name: str, version: int) -> dict:
        state = self._states(project, name).get(version)
        if state is None:
            raise NotFound(f"{project}:{name}@v{version} does not exist")
        if state["status"] != "running":
            raise StoreError(f"{project}:{name}@v{version} is {state['status']} and can no longer change")
        return state

    def _exact_inputs(self, inputs) -> list[str]:
        """Exact `project:name@v<n>` strings for input references; each must resolve to a completed version."""
        exact: list[str] = []
        for item in inputs or []:
            project, name, version = self.resolve(item)
            text = refs.exact(project, name, version["version"])
            if text not in exact:
                exact.append(text)
        return exact

    def _start(self, project, name, label, metadata, source, uid=None, write: bool = True) -> dict:
        """A new running version (its number claimed); written unless `write` is False (the caller writes it)."""
        if label is not None:
            refs.valid_word(label, "label")
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict):
            raise StoreError("metadata must be a dict")
        json.dumps(metadata)
        artifact = self._artifact(project, name, create=True)
        record = {"version": self._claim(project, name, artifact), "uid": new_uid() if uid is None else valid_uid(uid), "label": label,
                  "status": "running", "created": now(), "files": [], "metadata": metadata, "inputs": [], "source": source}
        if write:
            self._write(project, name, record)
        return record

    def _add(self, project, name, version, entries) -> dict:
        state = self._running(project, name, version)
        clean = clean_entries(entries)
        missing = self.missing(clean)
        if missing:
            raise StoreError(f"{len(missing)} blobs are not in the store yet, e.g. {missing[0]}")
        merged = {entry["path"]: entry for entry in state["files"]}
        merged.update({entry["path"]: entry for entry in clean})
        files = sorted(merged.values(), key=lambda entry: entry["path"])
        _check_tree(files)
        record = {**state, "files": files}
        self._write(project, name, record)
        return record

    def _complete(self, project, name, version, inputs) -> dict:
        state = self._running(project, name, version)
        exact = self._exact_inputs(inputs)
        record = {**state, "status": "completed", "completed": now(), "inputs": exact}
        self._write(project, name, record)
        self._completed(project, name, version, exact)
        return record

    def _completed(self, project, name, version, exact) -> None:
        """After a version completed: `latest` moves to it, and the versions it consumed learn of it."""
        self._move_alias(project, name, "latest", version)
        me = refs.exact(project, name, version)
        for text in exact:
            source = refs.parse(text)
            append_lines(self._project_dir(source.project) / "consumers.jsonl", [{"consumer": me, "input": text, "time": now()}])

    def _move_alias(self, project, name, alias, version) -> None:
        if self.aliases(project, name).get(alias) != version:
            artifact = self._artifact(project, name, create=True)
            append_lines(artifact.aliases_tail.path, [{"alias": alias, "version": version, "time": now()}])
            self._hint(project, name)
            if artifact.refresh(self._upgrade):
                self._index(project).bump()

    @contextmanager
    def _locked(self, project: str, name: str):
        """Hold this process's write lock of one artifact (index.ArtifactIndex.write_lock), with its index brought up
        to date once; until the lock is released this thread reads the artifact from the index without checking."""
        self._writing()
        artifact = self._artifact(project, name, create=True)
        with artifact.write_lock:
            if artifact.refresh(self._upgrade):
                self._index(project).bump()
            held = getattr(self._held, "keys", None)
            if held is None:
                self._held.keys, self._held.dirty = {}, set()
                held = self._held.keys
            held[(project, name)] = artifact
            try:
                yield artifact
            finally:
                del held[(project, name)]
                if (project, name) in self._held.dirty:
                    self._held.dirty.discard((project, name))
                    self._hint(project, name)

    def start_version(self, project: str, name: str, label: str | None = None, metadata: dict | None = None,
                      source: dict | None = None, uid: str | None = None) -> int:
        """Start a running version; returns its number. With `uid` (made by the client), starting the same version
        again returns its number; a uid this artifact already has for a different label or metadata is a Conflict."""
        with self._locked(project, name):
            if uid is not None:
                known = self._by_uid(self._states(project, name), valid_uid(uid))
                if known is not None:
                    if known["label"] != label or known["metadata"] != (metadata or {}):
                        raise Conflict(f"uid {uid} already names {project}:{name}@v{known['version']} with another label or metadata")
                    return known["version"]
            return self._start(project, name, label, metadata, source, uid)["version"]

    def add_files(self, project: str, name: str, version: int, entries: list[dict]) -> None:
        """Add or replace files in a running version (entries as in `blobs.clean_entries`, blobs already uploaded)."""
        with self._locked(project, name):
            self._add(project, name, version, entries)

    def complete_version(self, project: str, name: str, version: int, inputs=None) -> None:
        """Freeze a version and move `latest` to it; `inputs` are the references it consumed."""
        with self._locked(project, name):
            self._complete(project, name, version, inputs)

    def fail_version(self, project: str, name: str, version: int) -> None:
        with self._locked(project, name):
            state = self._running(project, name, version)
            self._write(project, name, {**state, "status": "failed", "completed": now()})

    def publish(self, project: str, name: str, entries: list[dict], label: str | None = None, metadata: dict | None = None,
                inputs=None, aliases=(), source: dict | None = None, uid: str | None = None) -> int:
        """Start, fill, and complete a version in one step.

        When the latest completed version already has these files, inputs, and metadata, no version
        is created and its number is returned (the aliases are still applied). With `uid`, publishing
        the same version again returns its number; a known uid with other content is a Conflict.
        """
        clean = sorted(clean_entries(entries), key=lambda entry: entry["path"])
        _check_tree(clean)
        with self._locked(project, name):
            exact = self._exact_inputs(inputs)
            states, current = self._artifact(project, name, create=True).snapshot()
            known = self._by_uid(states, valid_uid(uid)) if uid is not None else None
            head = current.get("latest")
            state = states.get(head) if head else None
            if known is not None:
                if known["status"] != "completed" or not _same(known, clean, exact, metadata or {}):
                    raise Conflict(f"uid {uid} already names {project}:{name}@v{known['version']} with other content")
                version = known["version"]
            elif state is not None and _same(state, clean, exact, metadata or {}):
                version = state["version"]
            elif not self.missing(clean):  # the usual case: the version's three state lines in one append (one fsync)
                running = self._start(project, name, label, metadata, source, uid, write=False)
                filled = {**running, "files": clean}
                done = {**filled, "status": "completed", "completed": now(), "inputs": exact}
                self._write(project, name, [running, filled, done])
                version = done["version"]
                self._completed(project, name, version, exact)
            else:  # blobs are missing: the version is started and then fails, as it always did
                version = self._start(project, name, label, metadata, source, uid)["version"]
                try:
                    self._add(project, name, version, clean)
                    self._complete(project, name, version, exact)
                except Exception:
                    self._write(project, name, {**self._states(project, name)[version], "status": "failed", "completed": now()})
                    raise
            for alias in aliases:
                self._set_alias(project, name, alias, version)
        return version

    def _set_alias(self, project, name, alias, version) -> None:
        refs.valid_word(alias, "alias")
        state = self._states(project, name).get(version)
        if state is None or state["status"] != "completed":
            raise StoreError(f"alias {alias!r}: {project}:{name}@v{version} is not a completed version")
        self._move_alias(project, name, alias, version)

    def set_alias(self, project: str, name: str, alias: str, version: int) -> None:
        """Point `alias` at a completed version. `latest` moves by itself and cannot be set."""
        if alias == "latest":
            raise StoreError("'latest' moves automatically when a version completes")
        with self._locked(project, name):
            self._set_alias(project, name, alias, version)

    # ------------------------------------------------------------- resolving

    def resolve(self, ref: Ref | str) -> tuple[str, str, dict]:
        """(project, name, version record) for a reference; no selector means `latest`."""
        ref = refs.parse(ref) if isinstance(ref, str) else ref
        project = ref.project or self.project
        if project is None:
            raise StoreError(f"reference {ref} has no project and the store has no default")
        states, aliases = self._artifact(project, ref.name).snapshot()
        if not states:
            raise NotFound(f"no artifact {project}:{ref.name}")
        if ref.exact:
            state = states.get(ref.version) if ref.version is not None else self._by_uid(states, ref.uid)
            if state is None:
                raise NotFound(f"{project}:{ref.name} has no version {ref.selector}")
            if state["status"] != "completed":
                raise StoreError(f"{project}:{ref.name}@{ref.selector} is {state['status']}, not completed")
            return project, ref.name, dict(state)
        selector = ref.selector or "latest"
        number = aliases.get(selector)
        if number is None:
            labelled = [n for n, state in states.items() if state["label"] == selector and state["status"] == "completed"]
            number = max(labelled, default=None)
        if number is None:
            raise NotFound(f"{project}:{ref.name}@{selector} matches no completed version")
        return project, ref.name, dict(states[number])

    def lineage(self, ref: Ref | str) -> dict[str, list[str]]:
        """The versions this one consumed and the versions that consumed it, as exact references."""
        project, name, version = self.resolve(ref)
        me = refs.exact(project, name, version["version"])
        return {"inputs": list(version["inputs"]), "consumers": self._index(project).lineage_consumers(me)}
