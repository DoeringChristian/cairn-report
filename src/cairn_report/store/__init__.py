"""The artifact store, light API.

    from cairn_report import store

    crops = store.pull("datasets/crops@v3")        # read-only local copy; the exact version is recorded as an input
    store.publish("models/denoiser", "checkpoints/best", metadata={"psnr": 31.2}, aliases=["best"])
    store.alias("models/denoiser@v4", "paper")
    store.versions("descent")
    store.lineage("reports/boundary@v2")

`store=None` means the configured store (`[store]` in cairn-report.toml): for now a local folder,
`path` (default `.store` in the project root) with the project named by `project` (default: the
project folder's slug). Every `pull` appends the exact version to `$CAIRN_INPUTS`, else to
`.cairn-inputs.jsonl` in the working directory; `publish` carries a folder's `.cairn-inputs.jsonl`
along as the version's `inputs`, which is how lineage is recorded without any framework hook.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
from pathlib import Path

from ..config import Config, load_config
from ..hub import now
from ..logger import read_jsonl
from ..push import HashCache, Limited, entries_for, project_name, scan
from . import refs
from .cache import default_root, materialize
from .local import LocalStore, NotFound, StoreError
from .refs import Ref

__all__ = ["LocalStore", "NotFound", "Ref", "StoreError", "alias", "lineage", "Publication", "open_store", "publish", "pull", "versions"]

INPUTS_FILE = ".cairn-inputs.jsonl"
INPUTS_ENV = "CAIRN_INPUTS"


def open_store(config: Config | None = None) -> LocalStore:
    """The store `[store]` in cairn-report.toml names, with the project as the default for references."""
    config = config or load_config()
    settings = config.store
    if settings.get("url"):
        raise NotImplementedError("remote stores arrive in step 4")
    root = Path(settings.get("path", ".store")).expanduser()
    store = LocalStore(root if root.is_absolute() else config.project / root, project_name(_named(config)))
    store.create_project(store.project)
    return store


def _named(config: Config) -> Config:
    """project_name reads the hub's project key; hand it the store's."""
    return dataclasses.replace(config, hub={"project": config.store.get("project")})


def _store(store) -> LocalStore:
    if store is None:
        return open_store()
    return store if isinstance(store, LocalStore) else LocalStore(store)


def pull(ref: str, *, store: LocalStore | Path | str | None = None, record: bool = True) -> Path:
    """A read-only local copy of an artifact version (or of the file after `//`), recorded as an input.

    `record=False` leaves the inputs file alone: the report builder records a report's inputs in the
    report's own manifest instead.
    """
    store = _store(store)
    parsed = refs.parse(ref)
    project, name, version = store.resolve(parsed)
    path = materialize(store, parsed, default_root())
    if record:
        target = Path(os.environ.get(INPUTS_ENV) or INPUTS_FILE)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ref": refs.exact(project, name, version["version"]), "time": now()}) + "\n")
    return path


def read_inputs(folder: Path) -> list[str]:
    """The distinct references a folder's `.cairn-inputs.jsonl` recorded, in order."""
    found: list[str] = []
    for row in read_jsonl(folder / INPUTS_FILE):
        if row.get("ref") and row["ref"] not in found:
            found.append(row["ref"])
    return found


def _target(name: str, store: LocalStore) -> tuple[str, str]:
    """(project, artifact name) for a publish target, creating the project."""
    target = refs.parse(name)
    if target.selector or target.path:
        raise ValueError(f"publish takes an artifact name, not {name!r}")
    project = target.project or store.project
    if project is None:
        raise StoreError(f"{name!r} has no project and the store has no default")
    store.create_project(project)
    return project, target.name


def _stage(store: LocalStore, local: Path, config: Config, cache: HashCache) -> list[dict]:
    """Entries for a folder's files (its inputs record excluded) with their blobs put in the store."""
    files = scan(local, "", config, exclude=config.store.get("exclude", []))
    files.pop(INPUTS_FILE, None)
    entries, sources = entries_for(files, cache)
    cache.save()
    for sha in store.missing(entries):
        file, size = sources[sha]
        with file.open("rb") as handle:
            store.put_blob(sha, Limited(handle, size), size)
    return entries


def publish(name: str, folder: Path | str, *, metadata: dict | None = None, aliases=(), label: str | None = None,
            inputs=None, store: LocalStore | Path | str | None = None, config: Config | None = None) -> int:
    """Store a folder as the next version of artifact `name`; returns the version number.

    A symlinked folder is followed (the label defaults to the resolved folder's name). When nothing
    changed since the latest version, no version is created and its number is returned.
    """
    local = Path(folder).resolve()
    if not local.is_dir():
        raise ValueError(f"{folder} is not a folder")
    store = _store(store)
    project, artifact = _target(name, store)
    config = config or load_config()
    entries = _stage(store, local, config, HashCache(config.project))
    merged = [*read_inputs(local), *(str(item) for item in inputs or [])]
    return store.publish(project, artifact, entries, label=label or local.name, metadata=metadata, inputs=merged,
                         aliases=aliases, source={"host": socket.gethostname(), "folder": str(local)})


class Publication:
    """One running version that follows a folder while a job writes it.

        with store.Publication("sweep", "runs/sweep/latest") as pub:
            ...                    # the job runs and writes into the folder
            pub.sync()             # add new and changed files now (call it as often as you like)

    Leaving the block syncs once more and completes the version (inputs come from the folder's
    `.cairn-inputs.jsonl` plus `inputs`); an exception marks it failed instead. The version is
    invisible to `latest` and aliases until it completes, and a file that is written again replaces
    its earlier entry. Files deleted from the folder stay in the running version.
    """

    def __init__(self, name: str, folder: Path | str, *, metadata: dict | None = None, aliases=(), label: str | None = None,
                 inputs=None, store: LocalStore | Path | str | None = None):
        self.local = Path(folder).resolve()
        self.store = _store(store)
        self.project, self.name = _target(name, self.store)
        self.config = load_config()
        self.cache = HashCache(self.config.project)
        self.label, self.metadata, self.aliases, self.inputs = label or self.local.name, metadata, tuple(aliases), inputs
        self.version: int | None = None
        self._sent: dict[str, tuple] = {}

    def start(self) -> int:
        if self.version is None:
            self.version = self.store.start_version(self.project, self.name, self.label, self.metadata,
                                                    {"host": socket.gethostname(), "folder": str(self.local)})
        return self.version

    def sync(self) -> int:
        """Add the files that are new or changed since the last sync; returns how many."""
        self.start()
        entries = _stage(self.store, self.local, self.config, self.cache)
        fresh = [entry for entry in entries if self._sent.get(entry["path"]) != (entry.get("sha256"), entry.get("link"))]
        if fresh:
            self.store.add_files(self.project, self.name, self.version, fresh)
            self._sent.update({entry["path"]: (entry.get("sha256"), entry.get("link")) for entry in fresh})
        return len(fresh)

    def complete(self) -> int:
        """Sync a last time, freeze the version, move `latest`, and apply the aliases."""
        self.sync()
        merged = [*read_inputs(self.local), *(str(item) for item in self.inputs or [])]
        self.store.complete_version(self.project, self.name, self.version, merged)
        for alias in self.aliases:
            self.store.set_alias(self.project, self.name, alias, self.version)
        return self.version

    def fail(self) -> None:
        if self.version is not None:
            self.store.fail_version(self.project, self.name, self.version)

    def __enter__(self) -> "Publication":
        self.start()
        return self

    def __exit__(self, kind, error, trace) -> None:
        if kind is None:
            self.complete()
        else:
            self.fail()


def alias(ref: str, alias: str, *, store: LocalStore | Path | str | None = None) -> None:
    """Point `alias` at the version `ref` names (which must be completed)."""
    store = _store(store)
    project, name, version = store.resolve(ref)
    store.set_alias(project, name, alias, version["version"])


def versions(name: str, *, store: LocalStore | Path | str | None = None) -> list[dict]:
    """Every version of an artifact, oldest first, whatever its status."""
    store = _store(store)
    parsed = refs.parse(name)
    project = parsed.project or store.project
    if project is None:
        raise StoreError(f"{name!r} has no project and the store has no default")
    return store.versions(project, parsed.name)


def lineage(ref: str, *, store: LocalStore | Path | str | None = None) -> dict[str, list[str]]:
    """One level up (`inputs`) and down (`consumers`) from a version, as exact references."""
    return _store(store).lineage(ref)
