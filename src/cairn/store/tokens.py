"""The one access token of a repo, for `cairn server` and `cairn ui`: `<root>/token`, the owner's, created on the
first launch (owner-only). It changes only when the owner deletes the file and the server makes a new one."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

MIN_TOKEN = 32


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
