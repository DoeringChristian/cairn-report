"""Content-addressed blobs and the entry format every version manifest uses.

    <root>/blobs/<aa>/<sha256-rest>     file contents, written once and verified
    <root>/projects/                    one folder per project (see local.py)
    <root>/tmp/                         uploads in progress
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

SHA256 = re.compile(r"[0-9a-f]{64}")
CHUNK = 1 << 20


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_path(path) -> bool:
    """A relative POSIX path without `.`/`..`, empty parts, or control characters."""
    if not isinstance(path, str) or not 0 < len(path) <= 1024 or "\\" in path:
        return False
    return all(part not in ("", ".", "..") and not any(ord(char) < 32 for char in part) for part in path.split("/"))


def link_target(path: str, target) -> str | None:
    """Where a relative symlink at `path` points, if it stays inside the artifact."""
    if not isinstance(target, str) or not target or target.startswith("/") or "\\" in target or len(target) > 1024:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return resolved if valid_path(resolved) else None


def clean_entries(entries) -> list[dict]:
    """Validated {path, sha256, size[, mtime]} or {path, link} entries."""
    if not isinstance(entries, list) or len(entries) > 200_000:
        raise ValueError("entries must be a list")
    clean = []
    for entry in entries:
        if not isinstance(entry, dict) or not valid_path(entry.get("path")):
            raise ValueError(f"invalid entry path: {str(entry)[:200]}")
        if "link" in entry:
            if link_target(entry["path"], entry["link"]) is None:
                raise ValueError(f"{entry['path']}: link must stay inside its artifact")
            clean.append({"path": entry["path"], "link": entry["link"]})
            continue
        sha, size = entry.get("sha256"), entry.get("size")
        if not isinstance(sha, str) or not SHA256.fullmatch(sha) or not isinstance(size, int) or size < 0:
            raise ValueError(f"{entry['path']}: needs sha256 and size")
        mtime = entry.get("mtime")
        clean.append({"path": entry["path"], "sha256": sha, "size": size,
                      "mtime": mtime if isinstance(mtime, (int, float)) else None})
    return clean


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class BlobStore:
    """The blob folder of a store root; safe for concurrent requests."""

    def __init__(self, root: Path):
        self.root = root
        self.blobs, self.projects, self.tmp = root / "blobs", root / "projects", root / "tmp"
        for folder in (self.blobs, self.projects, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)

    def blob_path(self, sha: str) -> Path:
        return self.blobs / sha[:2] / sha[2:]

    def has_blob(self, sha: str) -> bool:
        return self.blob_path(sha).is_file()

    def put_blob(self, sha: str, stream, size: int) -> bool:
        """Store `size` bytes from `stream` if they hash to `sha`; False if the blob already existed."""
        target = self.blob_path(sha)
        temporary = self.tmp / f"{sha}.{secrets.token_hex(6)}"
        digest, remaining = hashlib.sha256(), size
        try:
            with temporary.open("wb") as handle:
                while remaining:
                    chunk = stream.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ValueError("upload ended before Content-Length bytes arrived")
                    digest.update(chunk)
                    handle.write(chunk)
                    remaining -= len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if digest.hexdigest() != sha:
                raise ValueError("content does not match its SHA-256 (did the file change during upload?)")
            if target.is_file():
                return False
            target.parent.mkdir(exist_ok=True)
            os.replace(temporary, target)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def missing(self, entries: list[dict]) -> list[str]:
        return sorted({entry["sha256"] for entry in entries if "sha256" in entry and not self.has_blob(entry["sha256"])})
