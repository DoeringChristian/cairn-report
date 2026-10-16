"""Content-addressed blobs and the entry format every version manifest uses.

<repo>/blobs/<aa>/<sha256>     file contents, written once (a temporary file renamed into place) and verified;
                               shared by every version, the outbox, and the read-only folders `pull` makes;
                               removed only by `gc` (gc.py) and `cairn prune` (client/prune.py)
<repo>/tmp/                    uploads in progress
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .fs import clone_or_copy

SHA256 = re.compile(r"[0-9a-f]{64}")
CONTROL = re.compile(r"[\x00-\x1f]")
CHUNK = 4 << 20  # blobs are read, hashed and written in 4 MB pieces


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_path(path) -> bool:
    """A relative POSIX path without `.`/`..`, empty parts, or control characters."""
    if not isinstance(path, str) or not 0 < len(path) <= 1024 or "\\" in path:
        return False
    return not CONTROL.search(path) and all(part not in ("", ".", "..") for part in path.split("/"))


def link_target(path: str, target) -> str | None:
    """Where a relative symlink at `path` points, if it stays inside the artifact."""
    if not isinstance(target, str) or not target or target.startswith("/") or "\\" in target or len(target) > 1024:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return resolved if valid_path(resolved) else None


def clean_entries(entries, removals: bool = False) -> list[dict]:
    """Validated {path, sha256, size[, mtime]} or {path, link} entries; with `removals` (the files of a running
    version) also {path, removed: true}: that path leaves the version."""
    if not isinstance(entries, list) or len(entries) > 200_000:
        raise ValueError("entries must be a list")
    clean = []
    for entry in entries:
        if not isinstance(entry, dict) or not valid_path(entry.get("path")):
            raise ValueError(f"invalid entry path: {str(entry)[:200]}")
        if "removed" in entry:
            if not removals:
                raise ValueError(f"{entry['path']}: only a running version's files can be removed")
            if entry["removed"] is not True or set(entry) != {"path", "removed"}:
                raise ValueError(f"{entry['path']}: a removal is {{path, removed: true}}, nothing else")
            clean.append({"path": entry["path"], "removed": True})
            continue
        if "link" in entry:
            if link_target(entry["path"], entry["link"]) is None:
                raise ValueError(f"{entry['path']}: link must stay inside its artifact")
            clean.append({"path": entry["path"], "link": entry["link"]})
            continue
        sha, size = entry.get("sha256"), entry.get("size")
        if not isinstance(sha, str) or not SHA256.fullmatch(sha) or not isinstance(size, int) or size < 0:
            raise ValueError(f"{entry['path']}: needs sha256 and size")
        mtime = entry.get("mtime")
        clean.append(
            {
                "path": entry["path"],
                "sha256": sha,
                "size": size,
                "mtime": mtime if isinstance(mtime, (int, float)) else None,
            }
        )
    return clean


def merged_files(files: list[dict], entries: list[dict]) -> list[dict]:
    """A running version's files after `entries`, in order: a removal drops its path (a path not there is no change),
    any other entry adds or replaces the file at its path. Sorted by path."""
    merged = {entry["path"]: entry for entry in files}
    for entry in entries:
        if entry.get("removed"):
            merged.pop(entry["path"], None)
        else:
            merged[entry["path"]] = entry
    return sorted(merged.values(), key=lambda entry: entry["path"])


def resolve_entry(files: list[dict], path: str) -> dict | None:
    """The file entry at `path` in a version's files, following stored symlinks (for example `latest`) on the way."""
    tree = {entry["path"]: entry for entry in files}
    for _ in range(16):
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            entry = tree.get("/".join(parts[:index]))
            if entry and "link" in entry:
                base = link_target("/".join(parts[:index]), entry["link"])
                if base is None:
                    return None
                path = "/".join([base, *parts[index:]])
                break
        else:
            entry = tree.get(path)
            return entry if entry and "sha256" in entry else None
    return None


def append_lines(path: Path, rows: list[dict]) -> None:
    """Append rows to a JSON Lines file in one `write` on an O_APPEND descriptor, then fsync.

    One write keeps concurrent appenders (threads or processes on this host) from interleaving inside a
    line. A file whose last line was torn (a writer died mid-line) first gets a newline, so the new rows
    never fuse with the torn one."""
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    append_bytes(path, data, sync=True)


def append_bytes(path: Path, data: bytes, sync: bool = True) -> None:
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileNotFoundError:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, flags, 0o644)
    try:
        size = os.fstat(descriptor).st_size
        if size and os.pread(descriptor, 1, size - 1) != b"\n":
            data = b"\n" + data
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        if sync:
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


class BlobWriter:
    """One blob being written: hashed while it arrives, kept in a temporary file, fsynced, verified, renamed.

        writer = store.blob_writer(sha)
        for chunk in body: writer.write(chunk)   # chunks of any size; write in 1-8 MB pieces for speed
        created = writer.finish()                 # ValueError if the bytes do not hash to `sha`

    When the blob is already stored, nothing is written to disk: the bytes are only hashed (an upload that
    does not match its name is still refused). `abort()` drops a half-written blob."""

    def __init__(self, blobs: "BlobStore", sha: str, size: int | None = None):
        self.blobs, self.sha, self.size = blobs, sha, size
        self.digest, self.received = hashlib.sha256(), 0
        self.target = blobs.blob_name(sha)  # plain strings: this runs once per upload, pathlib costs show
        self.exists = os.path.isfile(self.target)
        self.temporary = None if self.exists else f"{blobs.tmp_name}/{sha}.{secrets.token_hex(6)}"
        self.handle = None if self.exists else open(self.temporary, "wb", buffering=0)

    def write(self, chunk) -> None:
        self.hash_part(chunk)
        self.write_part(chunk)

    def hash_part(self, chunk) -> None:
        """Hash the next piece (hashlib releases the GIL for large pieces, so this may run beside `write_part` on
        the same piece in another thread; pieces must arrive in order)."""
        self.digest.update(chunk)
        self.received += len(chunk)

    def write_part(self, chunk) -> None:
        """Write the next piece to the temporary file (nothing when the blob is already stored)."""
        if self.handle is not None:
            view = memoryview(chunk)
            while view:
                view = view[self.handle.write(view) :]

    def finish(self) -> bool:
        """Verify and place the blob; False if it was already stored."""
        try:
            if self.size is not None and self.received != self.size:
                raise ValueError("upload ended before Content-Length bytes arrived")
            if self.digest.hexdigest() != self.sha:
                raise ValueError("content does not match its SHA-256 (did the file change during upload?)")
            if self.handle is None:
                return False
            os.fsync(self.handle.fileno())
            self.handle.close()
            self.handle = None
            if os.path.isfile(self.target):
                return False
            try:
                os.replace(self.temporary, self.target)
            except FileNotFoundError:  # the first blob of its folder
                os.makedirs(os.path.dirname(self.target), exist_ok=True)
                os.replace(self.temporary, self.target)
            return True
        finally:
            self.abort()

    def abort(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        if self.temporary is not None:
            try:
                os.unlink(self.temporary)
            except FileNotFoundError:
                pass
            self.temporary = None


def blob_path(root: Path | str, sha: str) -> Path:
    """`<root>/blobs/<aa>/<sha256>`: where a repo keeps the contents with this hash."""
    return Path(root) / "blobs" / sha[:2] / sha


class BlobStore:
    """The blob folder of a repo; safe for concurrent requests and processes."""

    def __init__(self, root: Path):
        self.root = root
        self.blobs, self.tmp = root / "blobs", root / "tmp"
        for folder in (self.blobs, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        self.blobs_name, self.tmp_name = os.fspath(self.blobs), os.fspath(self.tmp)

    def blob_name(self, sha: str) -> str:
        return f"{self.blobs_name}/{sha[:2]}/{sha}"

    def blob_path(self, sha: str) -> Path:
        return Path(self.blob_name(sha))

    def has_blob(self, sha: str) -> bool:
        return os.path.isfile(self.blob_name(sha))

    def writer(self, sha: str, size: int | None = None) -> BlobWriter:
        return BlobWriter(self, sha, size)

    def put_blob(self, sha: str, stream, size: int) -> bool:
        """Store `size` bytes from `stream` if they hash to `sha`; False if the blob already existed."""
        writer = self.writer(sha, size)
        try:
            remaining = size
            while remaining:
                chunk = stream.read(min(CHUNK, remaining))
                if not chunk:
                    raise ValueError("upload ended before Content-Length bytes arrived")
                writer.write(chunk)
                remaining -= len(chunk)
            return writer.finish()
        finally:
            writer.abort()

    def put_file(self, sha: str, source: Path, link: bool = False) -> bool:
        """Store a local file as blob `sha`: a copy-on-write clone where the filesystem allows (no extra space),
        else a copy; the copy is hashed again, so a file that changed since it was hashed is refused (ValueError).

        `link`: the source is about to be deleted (a published draft), so where no clone is possible a hard link
        (same filesystem) replaces the copy; the caller must not let anything write the source in place after it."""
        target = self.blob_path(sha)
        if target.is_file():
            return False
        temporary = self.tmp / f"{sha}.{secrets.token_hex(6)}"
        try:
            if not (link and _hard_link(source, temporary)):
                clone_or_copy(source, temporary)
            os.utime(temporary)  # a clone keeps the source's times; the blob's age (gc.py's grace period) starts now
            digest = hashlib.sha256()
            with open(temporary, "rb", buffering=0) as handle:
                while chunk := handle.read(CHUNK):
                    digest.update(chunk)
                os.fsync(handle.fileno())
            if digest.hexdigest() != sha:
                raise ValueError(
                    f"{source}: content does not match its SHA-256 (did the file change during publishing?)"
                )
            if target.is_file():
                return False
            target.parent.mkdir(exist_ok=True)
            os.replace(temporary, target)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def stage_file(self, entry: dict, source: Path, link: bool = False) -> bool:
        """Keep the contents of a file about to be published as a blob, for the manifest entry `entry` ({path, sha256,
        size} as hashed a moment ago): a copy-on-write clone where the filesystem allows, else a copy (`link`: a hard
        link, for a source deleted right after: a published draft). The copy is hashed again; when the file changed in
        between, `entry` is updated to what was kept (the version has what was staged). Blobs are read-only. Returns
        False when the file is gone. Safe on a network filesystem: written under a temporary name, renamed into place.
        """
        target = self.blob_path(entry["sha256"])
        try:
            entry["size"] = (
                target.stat().st_size
            )  # kept already (a file that grew while it was hashed: the hashed bytes)
            return True
        except FileNotFoundError:
            pass
        temporary = self.tmp / f"{entry['sha256']}.{secrets.token_hex(6)}"
        try:
            if not (link and _hard_link(source, temporary)):
                clone_or_copy(source, temporary)
            os.utime(temporary)  # a clone keeps the source's times; the blob's age (gc.py's grace period) starts now
            digest, size = hashlib.sha256(), 0
            with open(temporary, "rb", buffering=0) as handle:
                while chunk := handle.read(CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                os.fsync(handle.fileno())
            if digest.hexdigest() != entry["sha256"] or size != entry.get("size"):  # changed since it was hashed (or
                entry.update(sha256=digest.hexdigest(), size=size)  # while): publish what was kept
                target = self.blob_path(entry["sha256"])
            os.chmod(temporary, 0o444)
            if not target.is_file():
                target.parent.mkdir(exist_ok=True)
                os.replace(temporary, target)
            return True
        except FileNotFoundError:  # removed since it was hashed
            return False
        finally:
            temporary.unlink(missing_ok=True)

    def missing_hashes(self, hashes) -> list[str]:
        """The hashes (sorted, unique) that are not stored, in one call. A batch dense enough (many hashes per
        blob folder) lists each folder it touches once instead of checking every blob on its own."""
        wanted = set(hashes)
        prefixes = {sha[:2] for sha in wanted}
        if len(wanted) < 64 * len(prefixes):  # few per folder: one stat each is cheaper than listing the folders
            return sorted(sha for sha in wanted if not self.has_blob(sha))
        present: set[str] = set()
        for prefix in prefixes:
            try:
                with os.scandir(self.blobs / prefix) as entries:
                    present.update(entry.name for entry in entries)
            except FileNotFoundError:
                continue
        return sorted(wanted - present)

    def missing(self, entries: list[dict]) -> list[str]:
        return self.missing_hashes(entry["sha256"] for entry in entries if "sha256" in entry)


def _hard_link(source: Path, target: Path) -> bool:
    """A clone where possible, else a hard link; False (nothing left behind) when neither works."""
    from .fs import clone

    if clone(source, target):
        return True
    try:
        os.link(source, target)
        return True
    except OSError:
        return False
