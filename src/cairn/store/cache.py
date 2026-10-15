"""The local cache: read-only copies of artifact versions, keyed by version uid, shared by every run.

    <cache>/versions/<uid>/        one version's files (read-only), with `.cairn-complete` (its manifest, JSON)
    <cache>/blobs/<aa>/<sha256>    file contents downloaded from a store server, or staged by the outbox
    <cache>/outbox/, resolved/     the client's offline state (cairn.client.outbox)

`<cache>` is `$CAIRN_CACHE_DIR`, else `$XDG_CACHE_HOME/cairn`, else `~/.cache/cairn`; on a cluster,
point it at node-local scratch. A version's folder is named by its uid, never by project, name, and
number, so two stores never collide in one cache.

The cache may sit on NFS and be filled by many jobs at once, so nothing locks: a folder or a blob is
written under a scratch name (`<target>.tmp-<host>-<pid>-<random>`) and renamed into place in one
step. If the target appeared meanwhile, another process won the race and its copy is used; ours is
discarded. Only renamed folders are ever visible, so a reader never sees a half-written version.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
from pathlib import Path

from . import refs
from .errors import NotFound, StoreError
from .inputs import record_input

MARKER = ".cairn-complete"
CACHE_ENV = "CAIRN_CACHE_DIR"


def cache_root() -> Path:
    """`$CAIRN_CACHE_DIR`, else `$XDG_CACHE_HOME/cairn`, else `~/.cache/cairn`."""
    if os.environ.get(CACHE_ENV):
        return Path(os.environ[CACHE_ENV]).expanduser()
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "cairn"


def default_root() -> Path:
    """The cache root (kept for callers of the earlier name)."""
    return cache_root()


def scratch(target: Path) -> Path:
    """A private name beside `target` to build it under: host, pid, and a random suffix (unique on NFS too)."""
    host = "".join(char if char.isalnum() or char in "-_" else "_" for char in socket.gethostname())[:40] or "host"
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
    return Path(root) / "versions" / uid


def read_marker(folder: Path) -> dict | None:
    """What a materialized folder holds (store, project, name, version, uid, manifest), or None if it is not one."""
    try:
        return json.loads((folder / MARKER).read_text() or "{}")
    except (OSError, ValueError):
        return None


def materialize_version(store, project: str, name: str, version: dict, cache_root: Path | str) -> Path:
    """Write a resolved version's files into the cache (once) and return its folder.

    Files are hard-linked from the store's blobs (a remote store keeps them in the blob cache) when both
    are on one filesystem, else copied, and made read-only (a hard link shares its blob, so the blob is
    protected too). The folder is built under a scratch name and renamed into place (see the module doc).
    """
    uid = version.get("uid")
    if not uid:
        raise StoreError(f"{project}:{name}@v{version['version']} has no uid: the store server is older than this client")
    folder = version_folder(cache_root, uid)
    if (folder / MARKER).is_file():
        return folder
    folder.parent.mkdir(parents=True, exist_ok=True)
    build = scratch(folder)
    try:
        build.mkdir()
        for entry in version["files"]:
            target = build / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if "link" in entry:
                os.symlink(entry["link"], target)
                continue
            source = store.blob_file(project, name, version["version"], entry, cache_root)
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
            os.chmod(target, 0o444)
        record = {"store": getattr(store, "uid", None), "project": project, "name": name, "version": version["version"],
                  "uid": uid, "manifest": version}
        (build / MARKER).write_text(json.dumps(record))
        os.chmod(build / MARKER, 0o444)
        if folder.exists() and not (folder / MARKER).is_file():  # not a finished folder: move it out of the way
            aside = scratch(folder)
            try:
                os.rename(folder, aside)
                _remove(aside)
            except OSError:
                pass
        try:
            os.rename(build, folder)
        except OSError:
            if not (folder / MARKER).is_file():  # we lost the race only if the winner finished
                raise
    finally:
        if build.exists():
            _remove(build)
    return folder


def _remove(folder: Path) -> None:
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
    """Resolve `ref`, materialize its version, and return the folder (or the file after `//`)."""
    ref = refs.parse(ref) if isinstance(ref, str) else ref
    project, name, version = store.resolve(ref)
    folder = materialize_version(store, project, name, version, cache_root)
    return inside(folder, ref, refs.exact(project, name, version["version"]))


def pull(store, ref: str, record: bool = True) -> Path:
    """A read-only local copy of an artifact version (or of the file after `//`) from an open store.

    `record=False` leaves the inputs file alone: the report builder records a report's inputs in the
    report's own manifest instead. (`cairn.pull` adds offline fallbacks for store servers.)
    """
    parsed = refs.parse(ref)
    project, name, version = store.resolve(parsed)
    folder = materialize_version(store, project, name, version, cache_root())
    path = inside(folder, parsed, refs.exact(project, name, version["version"]))
    if record:
        record_input(refs.exact(project, name, version["version"]))
    return path
