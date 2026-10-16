"""The read-only folders of versions that `pull` returns, in a repo, keyed by version uid.

    <repo>/versions/<uid>/manifest.json   the record: {"uid", "sync": "synced", "repo" (the uid of the repo or remote
                                          the version is from), "name", "version", "manifest",
                                          "accessed", "accessed_at"}
    <repo>/versions/<uid>/files/          the version's files, read-only: what `pull` returns
    <repo>/blobs/<aa>/<sha256>            the contents they are made from (blobs.py)

A version folder is named by its uid, never by name and number, so versions never collide. The folders are
derived from the blobs: deleting one loses nothing (the next pull makes it again), and `cairn prune` trims them.

The repo may sit on a network filesystem (a cluster's shared project space, with an outbox and no database) and be
filled by many jobs at once, so nothing here locks: a folder, a blob, or a record is written under a scratch name
(`<target>.tmp-<host>-<pid>-<random>`) and renamed into place in one step. If a version folder appeared meanwhile,
another process won the race and its copy is used; ours is discarded. Only renamed folders are ever visible, so a
reader never sees a half-written version.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import time
from pathlib import Path

from . import refs
from .blobs import now
from .digest import manifest_digest, sha256_file
from .errors import NotFound, StoreError
from .fs import clone
from .inputs import record_input

RECORD = "manifest.json"  # a version folder's record; the folder counts only once it exists
FILES = "files"
ACCESS_EVERY = 60.0  # seconds: how stale `accessed` may get before a pull rewrites it


def hostname() -> str:
    return socket.gethostname()


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists on this host."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True
    return True


def scratch(target: Path) -> Path:
    """A private name beside `target` to build it under: host, pid, and a random suffix (unique on NFS too)."""
    host = "".join(char if char.isalnum() or char in "-_" else "_" for char in hostname())[:40] or "host"
    return target.with_name(f"{target.name}.tmp-{host}-{os.getpid()}-{secrets.token_hex(4)}")


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


def update_record(root: Path | str, manifest: dict, repo: str | None = None) -> None:
    """The version a folder was made from got its final manifest (the remote numbered it): the folder's record says so
    from now on. Never raises (the record only describes the folder)."""
    folder = version_folder(root, manifest["uid"])
    record = read_record(folder)
    if record is None or record.get("manifest") == manifest:
        return
    try:
        write_json(folder / RECORD, {**record, "version": manifest.get("version"), "manifest": manifest,
                                     **({"repo": repo} if repo else {})})
    except OSError:
        pass


def cached(root: Path | str, uid: str | None) -> dict | None:
    """The record of a version folder `pull` made in the repo `root` (with `manifest`), or None."""
    if not uid:
        return None
    folder = version_folder(root, uid)
    record = read_record(folder)
    return record if record and record.get("sync") == "synced" and (folder / FILES).is_dir() else None


def touch(folder: Path, record: dict | None = None) -> None:
    """Note that a version folder was used now (`accessed`, what `cairn prune` goes by); throttled, never fails."""
    record = record or read_record(folder)
    if not record or record.get("sync") != "synced":
        return
    if time.time() - record.get("accessed_at", 0) < ACCESS_EVERY:
        return
    try:
        write_json(Path(folder) / RECORD, {**record, "accessed": now(), "accessed_at": time.time()})
    except OSError:
        pass


def place_file(source: Path, path: Path) -> str:
    """Make `path` (new) a read-only file with `source`'s contents, sharing storage where that is safe: a
    copy-on-write clone (APFS `clonefile`, Btrfs/XFS `FICLONE`) when the filesystem has them; else a hard link;
    else a copy. Returns "clone", "link" or "copy".

    A clone or a copy is a file of its own: editing it (after a `chmod`) never changes the blob. A hard link
    shares the blob's inode, so writing through it WOULD corrupt the blob and every other version using it;
    that is why a hard-linked file is always made read-only (0444) and why links are only the fallback for
    filesystems that cannot clone. (The blob itself becomes read-only too: it is the same inode.)"""
    if clone(source, path):
        how = "clone"
    else:
        try:
            os.link(source, path)
            how = "link"
        except OSError:
            shutil.copyfile(source, path)
            how = "copy"
    os.chmod(path, 0o444)
    return how


def link_files(entries: list[dict], target: Path, blob, digest: str | None = None, label: str = "version") -> None:
    """Fill `target` with the entries' files: `blob(entry)` gives each content's path; the file is placed with
    `place_file` (clone, else read-only hard link, else copy).

    The pull is verified: each placed file is hashed and must match its entry's sha256 and size, and the
    manifest digest (digest.py) of what was placed must equal `digest` (when the version has one). A mismatch
    raises a StoreError naming the file; the caller removes the partial folder."""
    target.mkdir(parents=True, exist_ok=True)
    placed = []
    for entry in entries:
        path = target / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if "link" in entry:
            os.symlink(entry["link"], path)
            placed.append(entry)
            continue
        source = blob(entry)
        place_file(source, path)
        sha, size = sha256_file(path)
        if sha != entry["sha256"] or size != entry["size"]:
            raise StoreError(f"{label}: {entry['path']} does not match its manifest (expected sha256 {entry['sha256'][:16]}..., "
                             f"{entry['size']} bytes; the stored copy {source} has {sha[:16]}..., {size} bytes): the stored copy is "
                             "damaged; `cairn verify --repair` removes it so the next pull downloads it again")
        placed.append({"path": entry["path"], "sha256": sha, "size": size})
    if digest is not None and manifest_digest(placed) != digest:
        raise StoreError(f"{label}: the files pulled hash to {manifest_digest(placed)}, not the manifest digest {digest}")


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


def materialize_version(store, name: str, version: dict, root: Path | str) -> Path:
    """Write a resolved version's files into the repo `root` (once) and return its `files/` folder.

    Files are cloned (copy-on-write) from the repo's blobs (`store.blob_file` downloads a remote's into them
    first), else hard-linked, else copied, and always made read-only (see `place_file`: a hard link shares its
    blob). Each file and the manifest digest are verified (`link_files`); a mismatch fails the pull and removes
    the partial folder. The folder is built under a scratch name and renamed into place (see the module doc).
    """
    uid = version.get("uid")
    if not uid:
        raise StoreError(f"{name}@v{version['version']} has no uid")
    folder = version_folder(root, uid)
    record = read_record(folder)
    if record and (folder / FILES).is_dir():
        touch(folder, record)
        return folder / FILES
    folder.parent.mkdir(parents=True, exist_ok=True)
    build = scratch(folder)
    try:
        build.mkdir()
        label = refs.exact(name, version["version"]) if version.get("version") is not None else f"{name}@{uid}"
        link_files(version["files"], build / FILES, lambda entry: store.blob_file(name, version["version"], entry, root),
                   version.get("digest"), label)
        write_json(build / RECORD, {"uid": uid, "sync": "synced", "repo": getattr(store, "uid", None), "name": name,
                                    "version": version["version"], "manifest": version, "accessed": now(),
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


def materialize(store, ref, root: Path | str) -> Path:
    """Resolve `ref`, materialize its version in the repo `root`, and return the `files/` folder (or the file after `//`)."""
    ref = refs.parse(ref) if isinstance(ref, str) else ref
    name, version = store.resolve(ref)
    folder = materialize_version(store, name, version, root)
    return inside(folder, ref, refs.exact(name, version["version"]))


def pull(store, ref: str, record: bool = True) -> Path:
    """A read-only local copy of an artifact version (or of the file after `//`) from a project of an open repo
    (a LocalProject).

    `record=False` leaves the inputs file alone: the report builder records a report's inputs in the
    report's own manifest instead. (`cairn.pull` adds the outbox's pending versions and the remote.)
    """
    parsed = refs.parse(ref)
    name, version = store.resolve(parsed)
    folder = materialize_version(store, name, version, store.root)
    path = inside(folder, parsed, refs.exact(name, version["version"]))
    if record:
        record_input(refs.exact(name, version["version"]), version["uid"])
    return path
