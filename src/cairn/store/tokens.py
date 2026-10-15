"""Access tokens of a repo, for `cairn server` and `cairn ui`.

    <root>/token          the owner token (full access), created on the first launch
    <root>/tokens.jsonl   scoped tokens, append-only: a row per token (id, name, project, write, created,
                          sha256 of the token) and a row per revocation. The token itself is shown once,
                          when it is created, and never stored.

A scoped token reads one project, and writes it too only when created with `--write`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

from ..formats import read_jsonl
from . import refs
from .blobs import append_lines, now

MIN_TOKEN = 32


@dataclass(frozen=True)
class Principal:
    """Who a request acts as: `project` None means every project (the owner)."""
    id: str
    name: str
    project: str | None
    write: bool

    def can(self, project: str | None, write: bool = False) -> bool:
        return (self.project is None or self.project == project) and (self.write or not write)


OWNER = Principal("owner", "owner", None, True)


def read_token(path: Path) -> str:
    token = path.read_text().strip()
    if len(token) < MIN_TOKEN:
        raise SystemExit(f"{path}: the owner token must have at least {MIN_TOKEN} characters")
    return token


def init_root(root: Path) -> Path:
    """Create a store root with a fresh random owner token, readable only by its owner."""
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    token_file = root / "token"
    if token_file.exists():
        raise SystemExit(f"{token_file} already exists; delete it first to rotate the token")
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)  # explicit: default ACLs may ignore the creation mode
        handle.write(secrets.token_urlsafe(32) + "\n")
    return token_file


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Tokens:
    """The scoped tokens of a root, re-read whenever tokens.jsonl changes (the CLI edits it while the server runs)."""

    def __init__(self, root: Path):
        self.path = Path(root) / "tokens.jsonl"
        self.lock = threading.Lock()
        self._stamp: tuple | None = None
        self._active: list[dict] = []

    def active(self) -> list[dict]:
        try:
            info = self.path.stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            return []
        with self.lock:
            if stamp != self._stamp:
                rows = read_jsonl(self.path)
                revoked = {row["revoked"] for row in rows if "revoked" in row}
                self._active = [row for row in rows if "sha256" in row and row["id"] not in revoked]
                self._stamp = stamp
            return self._active

    @staticmethod
    def _principal(row: dict) -> Principal:
        return Principal(row["id"], row["name"], row["project"], bool(row["write"]))

    def find(self, token: str) -> Principal | None:
        digest = _digest(token)
        found = None
        for row in self.active():  # no early exit: the time does not depend on which token matched
            if hmac.compare_digest(digest, row["sha256"]):
                found = row
        return self._principal(found) if found else None

    def by_id(self, identifier: str) -> Principal | None:
        return next((self._principal(row) for row in self.active() if row["id"] == identifier), None)


def create_token(root: Path, project: str, write: bool = False, name: str | None = None) -> str:
    """A new scoped token for `project` (created if it does not exist); returns the token, which is shown only now."""
    from .local import LocalRepo

    root = Path(root)
    if not (root / "token").is_file():
        raise SystemExit(f"{root} is not a repo with a token; `cairn server {root}` creates one")
    refs.valid_project(project)
    LocalRepo(root).create_project(project)
    tokens = Tokens(root)
    identifier = secrets.token_hex(4)
    name = name or f"{project}-{'write' if write else 'read'}-{identifier}"
    if any(row["name"] == name for row in tokens.active()):
        raise SystemExit(f"a token named {name!r} already exists; revoke it first or pick another name")
    token = secrets.token_urlsafe(32)
    append_lines(tokens.path, [{"id": identifier, "name": name, "project": project, "write": write,
                                "created": now(), "sha256": _digest(token)}])
    os.chmod(tokens.path, 0o600)
    return token


def list_tokens(root: Path) -> list[dict]:
    """Active tokens without their hashes."""
    return [{key: row[key] for key in ("name", "project", "write", "created")} for row in Tokens(root).active()]


def revoke_token(root: Path, name: str) -> None:
    tokens = Tokens(root)
    row = next((row for row in tokens.active() if row["name"] == name), None)
    if row is None:
        raise SystemExit(f"no active token named {name!r} (see `cairn token list {root}`)")
    append_lines(tokens.path, [{"revoked": row["id"], "name": name, "time": now()}])
