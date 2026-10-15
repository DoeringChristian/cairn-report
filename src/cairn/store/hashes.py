"""The hashes of a project's files, reused while a file's size and modification time are unchanged, so repeated
publishes reread only changed files. Kept in `<repo>/tmp/hashes/<project key>.json` (throwaway: losing it only costs
hashing again)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path


class HashCache:
    """SHA-256 per file, reused while size and modification time are unchanged."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.entries = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.entries = {}
        if not isinstance(self.entries, dict):
            self.entries = {}
        self.dirty = False
        self.lock = threading.Lock()

    @classmethod
    def of(cls, repo: Path, project: Path) -> "HashCache":
        """The cache of the project folder `project`, kept in the repo `repo`."""
        key = hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()[:16]
        return cls(Path(repo) / "tmp" / "hashes" / f"{key}.json")

    def sha(self, file: Path, info: dict) -> str:
        key = str(file)
        cached = self.entries.get(key)
        if cached and cached[0] == info["size"] and cached[1] == info["mtime_ns"]:
            return cached[2]
        digest = hashlib.sha256()
        with file.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        with self.lock:
            self.entries[key] = [info["size"], info["mtime_ns"], digest.hexdigest()]
            self.dirty = True
        return digest.hexdigest()

    def forget(self, file: Path) -> None:
        with self.lock:
            self.entries.pop(str(file), None)
            self.dirty = True

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                temporary.write_text(json.dumps(self.entries))
                os.replace(temporary, self.path)
            except OSError:  # a cache: losing it costs only hashing again
                return
            self.dirty = False
