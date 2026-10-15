"""Resolving and pulling with a store server that may be down.

- An exact reference (`name@v<n>` whose uid this machine has seen, or `name@<uid>`) whose version is in the
  cache is answered from the cache without asking the server at all.
- Anything else asks the server and remembers the answer (`state.remember`).
- When the server does not answer, or `offline=True`, an alias, label, or `latest` falls back to the
  last answer this machine got for it, if that version is in the cache, with a warning that names the
  version; that exact version is what a pull records as the input.

A local store (`LocalStore`) is always reachable, so it is asked directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..store import refs
from ..store.cache import MARKER, cache_root, inside, materialize_version, read_marker, version_folder
from ..store.errors import NotFound, StoreError, Unreachable
from ..store.inputs import record_input
from ..store.refs import Ref
from . import state
from .remote import RemoteStore


def cached(uid: str | None) -> dict | None:
    """The marker of a version in the cache (store, project, name, version, uid, manifest), or None."""
    if not uid:
        return None
    folder = version_folder(cache_root(), uid)
    return read_marker(folder) if (folder / MARKER).is_file() else None


def resolve_version(store, ref: Ref | str, offline: bool = False, warn=None) -> tuple[str, str, dict]:
    """(project, name, manifest) for a reference, falling back to the cache as the module doc says."""
    ref = refs.parse(ref) if isinstance(ref, str) else ref
    if not isinstance(store, RemoteStore):
        return store.resolve(ref)
    project = ref.project or store.project
    if project is None:
        raise StoreError(f"reference {ref} has no project and the store has no default")
    selector = ref.selector or "latest"
    key = f"{project}:{ref.name}@{selector}"
    if ref.exact:
        hint = {"uid": ref.uid} if ref.uid else state.resolved(store.key, key)
        found = cached(hint and hint["uid"])
        if found and found["project"] == project and found["name"] == ref.name:
            return project, ref.name, found["manifest"]
    if not offline:
        try:
            project, name, version = store.resolve(Ref(project, ref.name, ref.selector))
        except Unreachable:
            if ref.exact:
                raise Unreachable(f"the store at {store.url} does not answer and {key} is not in the cache") from None
            reason = f"the store at {store.url} does not answer"
        else:
            try:
                state.remember(store.uid, project, name, ref.selector, version)
            except StoreError:
                pass
            return project, name, version
    else:
        if ref.exact:
            raise NotFound(f"offline: {key} is not in the cache")
        reason = "offline"
    hint = state.resolved(store.key, key)
    found = cached(hint and hint["uid"])
    if not found:
        raise Unreachable(f"{reason}, and this machine has no cached version for {key}")
    exact = refs.exact(project, ref.name, found["version"])
    (warn or _warn)(f"warning: {reason}; using the last known resolution of {key}: {exact} (uid {found['uid']})")
    return project, ref.name, found["manifest"]


def _warn(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def pull(store, ref: str, record: bool = True, offline: bool = False) -> Path:
    """`store.cache.pull` with the offline fallbacks: a read-only local copy, its exact version recorded as an input."""
    parsed = refs.parse(ref)
    project, name, version = resolve_version(store, parsed, offline)
    folder = materialize_version(store, project, name, version, cache_root())
    path = inside(folder, parsed, refs.exact(project, name, version["version"]))
    if record:
        record_input(refs.exact(project, name, version["version"]))
    return path
