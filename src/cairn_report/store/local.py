"""LocalStore: projects, artifacts, and immutable versions in a folder.

    <root>/
      blobs/<aa>/<sha256-rest>                         file contents (blobs.BlobStore)
      projects/<p>/project.json                        name, title, description, created
      projects/<p>/consumers.jsonl                     consumer ref -> input ref, when a version with inputs completes
      projects/<p>/artifacts/<name>/versions.jsonl     one line per version state change; the last line per version wins
      projects/<p>/artifacts/<name>/aliases.jsonl      one line per alias move: alias, version, time

Everything is append-only. A version starts `running` (it may grow), then becomes `completed` or
`failed`; a completed version is never written again. Only completed versions resolve through
`latest`, aliases, and labels, so readers never see half-published data.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ..logger import read_jsonl
from . import refs
from .blobs import BlobStore, append_lines, clean_entries, now
from .refs import Ref


class StoreError(ValueError):
    pass


class NotFound(StoreError, LookupError):
    pass


def _same(version: dict, files: list[dict], inputs: list[str], metadata: dict) -> bool:
    def strip(entries):
        return [(entry["path"], entry.get("sha256"), entry.get("link")) for entry in entries]

    return strip(version["files"]) == strip(files) and sorted(version["inputs"]) == sorted(inputs) and version["metadata"] == metadata


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

    # ------------------------------------------------------------------ blobs

    def blob_path(self, sha: str) -> Path:
        return self._blobs.blob_path(sha)

    def blob_file(self, project: str, name: str, version: int, entry: dict, cache_root=None) -> Path:
        """A local file with the contents of a version's entry (here: the blob itself)."""
        return self.blob_path(entry["sha256"])

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

    # --------------------------------------------------------------- artifacts

    def _artifact_dir(self, project: str, name: str) -> Path:
        return self._project_dir(project) / "artifacts" / refs.valid_name(name)

    def _states(self, project: str, name: str) -> dict[int, dict]:
        """The latest recorded state per version number."""
        return {row["version"]: row for row in read_jsonl(self._artifact_dir(project, name) / "versions.jsonl")}

    def _write(self, project: str, name: str, record: dict) -> None:
        append_lines(self._artifact_dir(project, name) / "versions.jsonl", [record])

    def artifacts(self, project: str) -> list[str]:
        base = self._project_dir(project) / "artifacts"
        return sorted(file.parent.relative_to(base).as_posix() for file in base.rglob("versions.jsonl"))

    def versions(self, project: str, name: str) -> list[dict]:
        return [dict(state) for _, state in sorted(self._states(project, name).items())]

    def get_version(self, project: str, name: str, version: int) -> dict:
        """One version's manifest, whatever its status."""
        state = self._states(project, name).get(version)
        if state is None:
            raise NotFound(f"{project}:{name} has no version v{version}")
        return dict(state)

    def summary(self, project: str, name: str) -> dict:
        """The artifact at a glance: latest completed version, version count, aliases, last change."""
        states, aliases = self._states(project, name), self.aliases(project, name)
        stamps = [state.get("completed") or state["created"] for state in states.values()]
        return {"name": name, "latest": aliases.get("latest"), "versions": len(states), "aliases": aliases,
                "updated": max(stamps, default="")}

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

    def _start(self, project, name, label, metadata, source) -> dict:
        if label is not None:
            refs.valid_word(label, "label")
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict):
            raise StoreError("metadata must be a dict")
        json.dumps(metadata)
        states = self._states(project, name)
        record = {"version": max(states, default=0) + 1, "label": label, "status": "running", "created": now(),
                  "files": [], "metadata": metadata, "inputs": [], "source": source}
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
                      source: dict | None = None) -> int:
        self._artifact_dir(project, name)
        with self.lock:
            return self._start(project, name, label, metadata, source)["version"]

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
                inputs=None, aliases=(), source: dict | None = None) -> int:
        """Start, fill, and complete a version in one step.

        When the latest completed version already has these files, inputs, and metadata, no version
        is created and its number is returned (the aliases are still applied).
        """
        self._artifact_dir(project, name)
        clean = sorted(clean_entries(entries), key=lambda entry: entry["path"])
        _check_tree(clean)
        with self.lock:
            exact = self._exact_inputs(inputs)
            head = self.aliases(project, name).get("latest")
            state = self._states(project, name).get(head) if head else None
            if state is not None and _same(state, clean, exact, metadata or {}):
                version = state["version"]
            else:
                version = self._start(project, name, label, metadata, source)["version"]
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
        if ref.version is not None:
            state = states.get(ref.version)
            if state is None:
                raise NotFound(f"{project}:{ref.name} has no version v{ref.version}")
            if state["status"] != "completed":
                raise StoreError(f"{project}:{ref.name}@v{ref.version} is {state['status']}, not completed")
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
