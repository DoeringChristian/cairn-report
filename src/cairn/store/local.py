"""LocalStore: projects, artifacts, and immutable versions in a folder.

    <root>/
      store.json                                       the store's uid (UUIDv7), made once, never changed
      blobs/<aa>/<sha256-rest>                         file contents (blobs.BlobStore)
      projects/<p>/project.json                        name, title, description, created
      projects/<p>/consumers.jsonl                     consumer ref -> input ref, when a version with inputs completes
      projects/<p>/artifacts/<name>/versions.jsonl     one line per version state change; the last line per version wins
      projects/<p>/artifacts/<name>/aliases.jsonl      one line per alias move: alias, version, time

Everything is append-only. A version starts `running` (it may grow), then becomes `completed` or
`failed`; a completed version is never written again. Only completed versions resolve through
`latest`, aliases, and labels, so readers never see half-published data.

Every version has a `uid` (a UUIDv7, see uids.py), made when it starts or supplied by the client
that created it (an outbox); a uid names one version for good. A store written before uids existed
is upgraded as it is read: each version gets a deterministic uid (`uids.legacy`), persisted with one
more state line once the version is final, and the root gets its store.json.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..formats import read_jsonl
from ..ranges import byte_range
from . import refs
from .errors import BadRange, Conflict, Forbidden, NotFound, StoreError, Unauthorized  # noqa: F401
from .blobs import CHUNK, BlobStore, append_lines, clean_entries, now, resolve_entry
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


class LocalStore:
    def __init__(self, root: Path | str, project: str | None = None):
        """`project` is the default for references without one."""
        self.root, self.project = Path(root), project
        self._blobs = BlobStore(self.root)  # creates blobs/, projects/, tmp/
        self.lock = threading.Lock()
        self.uid = store_uid(self.root)

    # ------------------------------------------------------------------ blobs

    def blob_path(self, sha: str) -> Path:
        return self._blobs.blob_path(sha)

    def blob_file(self, project: str, name: str, version: int, entry: dict, cache_root=None) -> Path:
        """A local file with the contents of a version's entry (here: the blob itself)."""
        return self.blob_path(entry["sha256"])

    def read_file(self, project: str, name: str, selector: str | None, path: str, range_header: str | None = None) -> FileRead:
        """A file of the version `selector` names (`v<n>` of any status, else an alias or label of a completed one),
        optionally a byte range (the value of an HTTP Range header). Raises NotFound and BadRange."""
        if selector and refs.VERSION.fullmatch(selector):
            version = self.get_version(project, name, int(selector[1:]))
        elif selector and UID.fullmatch(selector):
            version = self.get_version(project, name, selector)
        else:
            version = self.resolve(Ref(project, name, selector))[2]
        entry = resolve_entry(version["files"], path)
        if entry is None:
            raise NotFound(f"{name} has no file {path}")
        blob = self.blob_path(entry["sha256"])
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

        immutable = version["status"] == "completed" and bool(selector) and bool(refs.VERSION.fullmatch(selector) or UID.fullmatch(selector))
        return FileRead(total, first, last, entry["sha256"], immutable, chunks())

    def has_blob(self, sha: str) -> bool:
        return self._blobs.has_blob(sha)

    def put_blob(self, sha: str, stream, size: int) -> bool:
        return self._blobs.put_blob(sha, stream, size)

    def missing(self, entries: list[dict]) -> list[str]:
        return self._blobs.missing(entries)

    # --------------------------------------------------------------- projects

    def _project_dir(self, project: str) -> Path:
        folder = self._blobs.projects / refs.valid_project(project)
        if not (folder / "project.json").is_file():
            raise NotFound(f"unknown project {project!r}")
        return folder

    def create_project(self, name: str, title: str | None = None, description: str = "") -> dict:
        """Create a project; an existing one is returned unchanged."""
        folder = self._blobs.projects / refs.valid_project(name)
        with self.lock:
            if (folder / "project.json").is_file():
                return json.loads((folder / "project.json").read_text())
            record = {"name": name, "title": title or name, "description": description, "created": now()}
            folder.mkdir(parents=True, exist_ok=True)
            temporary = folder / f"project.json.{os.getpid()}.tmp"
            temporary.write_text(json.dumps(record, indent=1))
            os.replace(temporary, folder / "project.json")
            return record

    def get_project(self, name: str) -> dict:
        return json.loads((self._project_dir(name) / "project.json").read_text())

    def update_project(self, name: str, title: str | None = None, description: str | None = None) -> dict:
        folder = self._project_dir(name)
        with self.lock:
            record = json.loads((folder / "project.json").read_text())
            record.update({key: value for key, value in (("title", title), ("description", description)) if value is not None})
            temporary = folder / f"project.json.{os.getpid()}.tmp"
            temporary.write_text(json.dumps(record, indent=1))
            os.replace(temporary, folder / "project.json")
            return record

    def projects(self) -> list[dict]:
        return [json.loads(file.read_text()) for file in sorted(self._blobs.projects.glob("*/project.json"))]

    def overviews(self) -> list[dict]:
        """Every project record with its artifact count and last update."""
        found = []
        for record in self.projects():
            summaries = self.summaries(record["name"])
            found.append({**record, "artifacts": len(summaries),
                          "updated": max((item["updated"] for item in summaries), default=record["created"])})
        return found

    # --------------------------------------------------------------- artifacts

    def _artifact_dir(self, project: str, name: str) -> Path:
        return self._project_dir(project) / "artifacts" / refs.valid_name(name)

    def _states(self, project: str, name: str) -> dict[int, dict]:
        """The latest recorded state per version number, each with its uid (see the upgrade in the module doc)."""
        states = {row["version"]: row for row in read_jsonl(self._artifact_dir(project, name) / "versions.jsonl")}
        upgraded = []
        for number, state in states.items():
            if not state.get("uid"):
                state["uid"] = legacy(f"{self.uid}/{project}/{name}/{number}", state.get("created"))
                if state["status"] != "running":  # final: no other writer can append after this line
                    upgraded.append(state)
        if upgraded:
            try:
                append_lines(self._artifact_dir(project, name) / "versions.jsonl", upgraded)
            except OSError:  # a read-only store: the same uids are derived again next time
                pass
        return states

    def _by_uid(self, states: dict[int, dict], uid: str) -> dict | None:
        return next((state for state in states.values() if state["uid"] == uid), None)

    def _write(self, project: str, name: str, record: dict) -> None:
        append_lines(self._artifact_dir(project, name) / "versions.jsonl", [record])

    def artifacts(self, project: str) -> list[str]:
        base = self._project_dir(project) / "artifacts"
        return sorted(file.parent.relative_to(base).as_posix() for file in base.rglob("versions.jsonl"))

    def versions(self, project: str, name: str) -> list[dict]:
        return [dict(state) for _, state in sorted(self._states(project, name).items())]

    def get_version(self, project: str, name: str, version: int | str) -> dict:
        """One version's manifest, by number or uid, whatever its status."""
        states = self._states(project, name)
        state = self._by_uid(states, version) if isinstance(version, str) else states.get(version)
        if state is None:
            raise NotFound(f"{project}:{name} has no version {version if isinstance(version, str) else f'v{version}'}")
        return dict(state)

    def summary(self, project: str, name: str) -> dict:
        """The artifact at a glance: latest completed version (number and metadata), version count, aliases, last change."""
        states, aliases = self._states(project, name), self.aliases(project, name)
        stamps = [state.get("completed") or state["created"] for state in states.values()]
        head = states.get(aliases.get("latest"))
        return {"name": name, "latest": aliases.get("latest"), "versions": len(states), "aliases": aliases,
                "updated": max(stamps, default=""), "metadata": head["metadata"] if head else {}}

    def summaries(self, project: str) -> list[dict]:
        return [self.summary(project, name) for name in self.artifacts(project)]

    def aliases(self, project: str, name: str) -> dict[str, int]:
        """Where each alias points now (the last move wins)."""
        return {row["alias"]: row["version"] for row in read_jsonl(self._artifact_dir(project, name) / "aliases.jsonl")}

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

    def _start(self, project, name, label, metadata, source, uid=None) -> dict:
        if label is not None:
            refs.valid_word(label, "label")
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict):
            raise StoreError("metadata must be a dict")
        json.dumps(metadata)
        states = self._states(project, name)
        record = {"version": max(states, default=0) + 1, "uid": new_uid() if uid is None else valid_uid(uid), "label": label,
                  "status": "running", "created": now(), "files": [], "metadata": metadata, "inputs": [], "source": source}
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
        self._move_alias(project, name, "latest", version)
        me = refs.exact(project, name, version)
        for text in exact:
            source = refs.parse(text)
            append_lines(self._project_dir(source.project) / "consumers.jsonl", [{"consumer": me, "input": text, "time": now()}])
        return record

    def _move_alias(self, project, name, alias, version) -> None:
        if self.aliases(project, name).get(alias) != version:
            append_lines(self._artifact_dir(project, name) / "aliases.jsonl", [{"alias": alias, "version": version, "time": now()}])

    def start_version(self, project: str, name: str, label: str | None = None, metadata: dict | None = None,
                      source: dict | None = None, uid: str | None = None) -> int:
        """Start a running version; returns its number. With `uid` (made by the client), starting the same version
        again returns its number; a uid this artifact already has for a different label or metadata is a Conflict."""
        self._artifact_dir(project, name)
        with self.lock:
            if uid is not None:
                known = self._by_uid(self._states(project, name), valid_uid(uid))
                if known is not None:
                    if known["label"] != label or known["metadata"] != (metadata or {}):
                        raise Conflict(f"uid {uid} already names {project}:{name}@v{known['version']} with another label or metadata")
                    return known["version"]
            return self._start(project, name, label, metadata, source, uid)["version"]

    def add_files(self, project: str, name: str, version: int, entries: list[dict]) -> None:
        """Add or replace files in a running version (entries as in `blobs.clean_entries`, blobs already uploaded)."""
        with self.lock:
            self._add(project, name, version, entries)

    def complete_version(self, project: str, name: str, version: int, inputs=None) -> None:
        """Freeze a version and move `latest` to it; `inputs` are the references it consumed."""
        with self.lock:
            self._complete(project, name, version, inputs)

    def fail_version(self, project: str, name: str, version: int) -> None:
        with self.lock:
            state = self._running(project, name, version)
            self._write(project, name, {**state, "status": "failed", "completed": now()})

    def publish(self, project: str, name: str, entries: list[dict], label: str | None = None, metadata: dict | None = None,
                inputs=None, aliases=(), source: dict | None = None, uid: str | None = None) -> int:
        """Start, fill, and complete a version in one step.

        When the latest completed version already has these files, inputs, and metadata, no version
        is created and its number is returned (the aliases are still applied). With `uid`, publishing
        the same version again returns its number; a known uid with other content is a Conflict.
        """
        self._artifact_dir(project, name)
        clean = sorted(clean_entries(entries), key=lambda entry: entry["path"])
        _check_tree(clean)
        with self.lock:
            exact = self._exact_inputs(inputs)
            states = self._states(project, name)
            known = self._by_uid(states, valid_uid(uid)) if uid is not None else None
            head = self.aliases(project, name).get("latest")
            state = states.get(head) if head else None
            if known is not None:
                if known["status"] != "completed" or not _same(known, clean, exact, metadata or {}):
                    raise Conflict(f"uid {uid} already names {project}:{name}@v{known['version']} with other content")
                version = known["version"]
            elif state is not None and _same(state, clean, exact, metadata or {}):
                version = state["version"]
            else:
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
        with self.lock:
            self._set_alias(project, name, alias, version)

    # ------------------------------------------------------------- resolving

    def resolve(self, ref: Ref | str) -> tuple[str, str, dict]:
        """(project, name, version record) for a reference; no selector means `latest`."""
        ref = refs.parse(ref) if isinstance(ref, str) else ref
        project = ref.project or self.project
        if project is None:
            raise StoreError(f"reference {ref} has no project and the store has no default")
        states = self._states(project, ref.name)
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
        number = self.aliases(project, ref.name).get(selector)
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
        consumers: list[str] = []
        for row in read_jsonl(self._project_dir(project) / "consumers.jsonl"):
            if row["input"] == me and row["consumer"] not in consumers:
                consumers.append(row["consumer"])
        return {"inputs": list(version["inputs"]), "consumers": consumers}
