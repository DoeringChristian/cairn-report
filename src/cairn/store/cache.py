"""The local replica's two roots, and the cache half of it: read-only copies of versions, keyed by version uid.

    <cache>/versions/<uid>/manifest.json   the record: {"uid", "sync": "synced", "store", "project", "name",
                                           "version", "manifest", "accessed"}
    <cache>/versions/<uid>/files/          the version's files, read-only: what `pull` returns
    <cache>/blobs/<aa>/<sha256>            file contents downloaded from a store server
    <data>/...                             versions not on the server yet (cairn.client.replica)

`<cache>` is throwaway: `$CAIRN_CACHE_DIR`, else `$XDG_CACHE_HOME/cairn`, else `~/.cache/cairn`; on a
cluster it may be node-local scratch, and `cairn prune` trims it. `<data>` is durable (the data root):
`$CAIRN_DATA_DIR`, else `$XDG_DATA_HOME/cairn`, else `~/.local/share/cairn`. It holds what only this
machine has (pending versions and their blobs; real data, not a cache) and must sit on persistent
storage with room for it: on a cluster with a small home quota, point `CAIRN_DATA_DIR` at a project or
work filesystem. A version's folder is named by its uid, never by project, name, and number, so two
stores never collide.

Either root may sit on NFS and be filled by many jobs at once, so nothing locks: a folder, a blob, or a
record is written under a scratch name (`<target>.tmp-<host>-<pid>-<random>`) and renamed into place in
one step. If a version folder appeared meanwhile, another process won the race and its copy is used;
ours is discarded. Only renamed folders are ever visible, so a reader never sees a half-written version.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import sys
import time
from pathlib import Path

from . import refs
from .blobs import now
from .errors import NotFound, StoreError
from .inputs import record_input

CACHE_ENV = "CAIRN_CACHE_DIR"
DATA_ENV = "CAIRN_DATA_DIR"
LEGACY_STATE_ENV = "CAIRN_STATE_DIR"  # read for one release, with a warning
RECORD = "manifest.json"  # a version folder's record; the folder counts only once it exists
FILES = "files"
ACCESS_EVERY = 60.0  # seconds: how stale `accessed` may get before a pull rewrites it

_override: dict[str, Path | None] = {"data": None}
_warned: set[str] = set()


def use_data_dir(path: Path | str | None) -> None:
    """Use this folder as the data root, whatever the environment says (`cairn sync --data-dir`); None resets."""
    _override["data"] = Path(path).expanduser().resolve() if path else None


def cache_root() -> Path:
    """`$CAIRN_CACHE_DIR`, else `$XDG_CACHE_HOME/cairn`, else `~/.cache/cairn`."""
    if os.environ.get(CACHE_ENV):
        return Path(os.environ[CACHE_ENV]).expanduser()
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "cairn"


def data_root() -> Path:
    """`--data-dir`, else `$CAIRN_DATA_DIR`, else `$XDG_DATA_HOME/cairn`, else `~/.local/share/cairn`.

    The deprecated `$CAIRN_STATE_DIR` is read (with a warning) when `$CAIRN_DATA_DIR` is not set."""
    if _override["data"] is not None:
        return _override["data"]
    if os.environ.get(DATA_ENV):
        return Path(os.environ[DATA_ENV]).expanduser()
    if os.environ.get(LEGACY_STATE_ENV):
        if LEGACY_STATE_ENV not in _warned:
            _warned.add(LEGACY_STATE_ENV)
            print(f"warning: {LEGACY_STATE_ENV} is deprecated: rename it to {DATA_ENV}", file=sys.stderr)
        return Path(os.environ[LEGACY_STATE_ENV]).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "cairn"


def old_state_root() -> Path:
    """Where earlier releases kept pending versions: `$XDG_STATE_HOME/cairn`, else `~/.local/state/cairn`."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "cairn"


def default_root() -> Path:
    """The cache root (kept for callers of the earlier name)."""
    return cache_root()


def hostname() -> str:
    return socket.gethostname()


def scratch(target: Path) -> Path:
    """A private name beside `target` to build it under: host, pid, and a random suffix (unique on NFS too)."""
    host = "".join(char if char.isalnum() or char in "-_" else "_" for char in hostname())[:40] or "host"
    return target.with_name(f"{target.name}.tmp-{host}-{os.getpid()}-{secrets.token_hex(4)}")


def blob_cache(root: Path | str, sha: str) -> Path:
    return Path(root) / "blobs" / sha[:2] / sha


def place(temporary: Path, target: Path) -> bool:
    """Rename a finished file into place; False (and the copy discarded) if another process placed it first."""
    if target.exists():
        temporary.unlink(missing_ok=True)
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(temporary, target)  # atomic; the same contents either way, so overwriting a racing winner is harmless
    return True


def version_folder(root: Path | str, uid: str) -> Path:
    """`<root>/versions/<uid>`: the record and, under `files/`, the files."""
    return Path(root) / "versions" / uid


def files_folder(root: Path | str, uid: str) -> Path:
    """`<root>/versions/<uid>/files`: the read-only folder `pull` returns."""
    return version_folder(root, uid) / FILES


# ------------------------------------------------------------------ records


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    """Write-temp-and-rename: a reader sees the old file or the new one, never a partial one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = scratch(path)
    temporary.write_text(json.dumps(value, indent=1))
    os.replace(temporary, path)


def read_record(folder: Path) -> dict | None:
    """A version folder's record, or None if the folder is not a finished one."""
    record = read_json(Path(folder) / RECORD)
    return record if isinstance(record, dict) and record.get("uid") else None


def cached(uid: str | None) -> dict | None:
    """The record of a synced version in the cache (with `manifest`), or None."""
    if not uid:
        return None
    folder = version_folder(cache_root(), uid)
    record = read_record(folder)
    return record if record and record.get("sync") == "synced" and (folder / FILES).is_dir() else None


def touch(folder: Path, record: dict | None = None) -> None:
    """Note that a cached version was used now (`accessed`, what `cairn prune` goes by); throttled, never fails."""
    record = record or read_record(folder)
    if not record or record.get("sync") != "synced":
        return
    if time.time() - record.get("accessed_at", 0) < ACCESS_EVERY:
        return
    try:
        write_json(Path(folder) / RECORD, {**record, "accessed": now(), "accessed_at": time.time()})
    except OSError:
        pass


def link_files(entries: list[dict], target: Path, blob) -> None:
    """Fill `target` with the entries' files: `blob(entry)` gives each content's path, hard-linked when it can be."""
    target.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        path = target / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if "link" in entry:
            os.symlink(entry["link"], path)
            continue
        source = blob(entry)
        try:
            os.link(source, path)
        except OSError:
            shutil.copyfile(source, path)
        os.chmod(path, 0o444)


def install(build: Path, folder: Path) -> None:
    """Rename a finished version folder into place; a finished folder already there wins (ours is discarded)."""
    if folder.exists() and read_record(folder) is None:  # not a finished folder (an interrupted one): move it aside
        aside = scratch(folder)
        try:
            os.rename(folder, aside)
            remove(aside)
        except OSError:
            pass
    try:
        os.rename(build, folder)
    except OSError:
        if read_record(folder) is None:  # we lost the race only if the winner finished
            raise


def materialize_version(store, project: str, name: str, version: dict, cache_root: Path | str) -> Path:
    """Write a resolved version's files into the cache (once) and return its `files/` folder.

    Files are hard-linked from the store's blobs (a remote store keeps them in the blob cache) when both
    are on one filesystem, else copied, and made read-only (a hard link shares its blob, so the blob is
    protected too). The folder is built under a scratch name and renamed into place (see the module doc).
    """
    uid = version.get("uid")
    if not uid:
        raise StoreError(f"{project}:{name}@v{version['version']} has no uid: the store server is older than this client")
    folder = version_folder(cache_root, uid)
    record = read_record(folder)
    if record and (folder / FILES).is_dir():
        touch(folder, record)
        return folder / FILES
    folder.parent.mkdir(parents=True, exist_ok=True)
    build = scratch(folder)
    try:
        build.mkdir()
        link_files(version["files"], build / FILES, lambda entry: store.blob_file(project, name, version["version"], entry, cache_root))
        write_json(build / RECORD, {"uid": uid, "sync": "synced", "store": getattr(store, "uid", None), "project": project,
                                    "name": name, "version": version["version"], "manifest": version, "accessed": now(),
                                    "accessed_at": time.time()})
        install(build, folder)
    finally:
        if build.exists():
            remove(build)
    return folder / FILES


def remove(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)  # read-only files go too: removing needs a writable folder, not file


def inside(folder: Path, ref: refs.Ref, label: str) -> Path:
    """The folder, or the file `ref` names after `//` inside it."""
    if not ref.path:
        return folder
    target = folder / ref.path
    if not os.path.lexists(target):
        raise NotFound(f"{label} has no {ref.path}")
    return target


def materialize(store, ref, cache_root: Path | str) -> Path:
    """Resolve `ref`, materialize its version, and return the `files/` folder (or the file after `//`)."""
    ref = refs.parse(ref) if isinstance(ref, str) else ref
    project, name, version = store.resolve(ref)
    folder = materialize_version(store, project, name, version, cache_root)
    return inside(folder, ref, refs.exact(project, name, version["version"]))


def pull(store, ref: str, record: bool = True) -> Path:
    """A read-only local copy of an artifact version (or of the file after `//`) from an open store.

    `record=False` leaves the inputs file alone: the report builder records a report's inputs in the
    report's own manifest instead. (`cairn.pull` adds the replica's pending versions and offline fallbacks.)
    """
    parsed = refs.parse(ref)
    project, name, version = store.resolve(parsed)
    folder = materialize_version(store, project, name, version, cache_root())
    path = inside(folder, parsed, refs.exact(project, name, version["version"]))
    if record:
        record_input(refs.exact(project, name, version["version"]))
    return path
