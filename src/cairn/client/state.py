"""What the client remembers about store servers, in the cache, so it can work while they are down.

    <cache>/stores.json                     {url: store uid} for every server this machine has reached
    <cache>/resolved/<store-uid>.json       {"project:name@selector": {"uid", "version"}}: the last answer per reference

Both are hints, rewritten whole (scratch name, then rename) and safe to lose: a lost entry is learned
again on the next answer. Numbers and uids never change their version, so `@v<n>` and `@<uid>` hints
are exact; an alias or label hint is only the last known resolution (offline pulls say so).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..store.cache import cache_root, scratch


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = scratch(path)
    temporary.write_text(json.dumps(value, indent=1))
    temporary.replace(path)


def store_key(url: str) -> str:
    """The outbox and hint folder name of a server whose uid is not known yet."""
    return "url-" + hashlib.sha256(url.rstrip("/").encode()).hexdigest()[:16]


def known_uid(url: str) -> str | None:
    return read_json(cache_root() / "stores.json", {}).get(url.rstrip("/"))


def remember_uid(url: str, uid: str) -> None:
    path = cache_root() / "stores.json"
    known = read_json(path, {})
    if known.get(url.rstrip("/")) != uid:
        known[url.rstrip("/")] = uid
        write_json(path, known)


def _resolved_path(store_id: str) -> Path:
    return cache_root() / "resolved" / f"{store_id}.json"


def resolved(store_id: str | None, ref: str) -> dict | None:
    """The last answer for `project:name@selector` from this store: {uid, version}."""
    if not store_id:
        return None
    return read_json(_resolved_path(store_id), {}).get(ref)


def remember(store_id: str | None, project: str, name: str, selector: str | None, version: dict) -> None:
    """Record that `project:name@selector` (and `@v<n>`, `@<uid>`) resolved to `version`."""
    if not store_id or not version.get("uid"):
        return
    path = _resolved_path(store_id)
    known = read_json(path, {})
    hint = {"uid": version["uid"], "version": version["version"]}
    keys = {f"{project}:{name}@{selector or 'latest'}", f"{project}:{name}@v{version['version']}", f"{project}:{name}@{version['uid']}"}
    if any(known.get(key) != hint for key in keys):
        known.update({key: hint for key in keys})
        write_json(path, known)
