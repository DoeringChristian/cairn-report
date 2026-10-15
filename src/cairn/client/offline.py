"""Resolving and pulling through the local replica, with a store server that may be down.

- A version published on this machine and still pending is usable at once: `name@<uid>` names it,
  and an alias it was published with, its label, or `latest` resolve to it when it is newer than what
  the server (or the last known answer) has; a warning says it is pending. Its read-only folder is in
  the data root, and a pull records `project:name@<uid>` as the input (the server turns that into
  `@v<n>` when the consumer is sent, after the input: records go in uid order).
- An exact reference (`name@v<n>` whose uid this machine has seen, or `name@<uid>`) whose version is in the
  cache is answered from the cache without asking the server at all.
- Anything else asks the server and remembers the answer (`replica.remember`).
- When the server does not answer, or `offline=True`, an alias, label, or `latest` falls back to the
  last answer this machine got for it, if that version is in the cache, with a warning that names the
  version; that exact version is what a pull records as the input.

A local store (`LocalStore`) is always reachable, so it is asked directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..store import refs
from ..store.cache import cache_root, cached as cached_record, inside, materialize_version
from ..store.errors import NotFound, StoreError, Unreachable
from ..store.inputs import record_input
from ..store.refs import Ref
from . import replica
from .remote import RemoteStore


def cached(uid: str | None) -> dict | None:
    """The record of a synced version in the cache (store, project, name, version, uid, manifest), or None."""
    return cached_record(uid)


def pending_match(store: RemoteStore, project: str, name: str, selector: str) -> dict | None:
    """The newest pending version of this machine that `project:name@selector` (an alias, a label, `latest`) names."""
    uid = replica.known_uid(store.url) or store._uid
    found = [record for record in replica.records() if replica.usable(record) and replica.belongs(record, store.url, uid)
             and (record["project"], record["name"]) == (project, name)
             and (selector == "latest" or selector in record.get("aliases", []) or record.get("label") == selector)]
    return found[-1] if found else None


def _pending_warning(key: str, record: dict) -> str:
    return (f"warning: {key} is {record['project']}:{record['name']}@{record['uid']}, published on this machine and still pending"
            " (not on the store server yet; `cairn status` lists it, `cairn sync` sends it)")


def resolve_version(store, ref: Ref | str, offline: bool = False, warn=None) -> tuple[str, str, dict]:
    """(project, name, manifest) for a reference, through the replica as the module doc says."""
    ref = refs.parse(ref) if isinstance(ref, str) else ref
    if not isinstance(store, RemoteStore):
        return store.resolve(ref)
    say = warn or _warn
    project = ref.project or store.project
    if project is None:
        raise StoreError(f"reference {ref} has no project and the store has no default")
    selector = ref.selector or "latest"
    key = f"{project}:{ref.name}@{selector}"
    if ref.uid:
        record = replica.load(ref.uid)
        if record and (record["project"], record["name"]) == (project, ref.name):
            if replica.usable(record):
                say(_pending_warning(key, record))
                return project, ref.name, replica.manifest_of(record)
            if record["sync"] == "rejected":
                raise StoreError(f"{key} was refused by the store server: {record.get('error')}")
    if ref.exact:
        hint = {"uid": ref.uid} if ref.uid else replica.hint(store.key, project, ref.name, selector)
        found = cached(hint and hint["uid"])
        if found and found["project"] == project and found["name"] == ref.name:
            return project, ref.name, found["manifest"]
    pending = None if ref.exact else pending_match(store, project, ref.name, selector)
    if not offline:
        try:
            project, name, version = store.resolve(Ref(project, ref.name, ref.selector))
        except Unreachable:
            if ref.exact:
                raise Unreachable(f"the store at {store.url} does not answer and {key} is not in the cache") from None
            reason = f"the store at {store.url} does not answer"
        except NotFound:
            if pending is None:
                raise
            say(_pending_warning(key, pending))
            return project, ref.name, replica.manifest_of(pending)
        else:
            try:
                replica.remember(store.uid, project, name, [ref.selector], version, store.url)
            except StoreError:
                pass
            if pending is not None and pending["uid"] > version["uid"]:
                say(_pending_warning(key, pending))
                return project, name, replica.manifest_of(pending)
            return project, name, version
    else:
        if ref.exact:
            raise NotFound(f"offline: {key} is not in the cache")
        reason = "offline"
    hint = replica.hint(store.key, project, ref.name, selector)
    found = cached(hint and hint["uid"])
    if pending is not None and (not found or pending["uid"] > found["uid"]):
        say(_pending_warning(key, pending))
        return project, ref.name, replica.manifest_of(pending)
    if not found:
        raise Unreachable(f"{reason}, and this machine has no cached version for {key}")
    exact = refs.exact(project, ref.name, found["version"])
    say(f"warning: {reason}; using the last known resolution of {key}: {exact} (uid {found['uid']})")
    return project, ref.name, found["manifest"]


def _warn(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def local_folder(store, project: str, name: str, version: dict) -> Path:
    """The read-only folder of a resolved version: a pending one's in the data root, else the cache's (made once)."""
    if version.get("pending"):
        folder = replica.pending_files(version["uid"])
        if folder is not None:
            return folder
        found = cached(version["uid"])  # sent meanwhile: promoted into the cache, or on the server
        if found is not None:
            version = found["manifest"]
        else:
            project, name, version = store.resolve(Ref(project, name, version["uid"]))
    return materialize_version(store, project, name, version, cache_root())


def pull(store, ref: str, record: bool = True, offline: bool = False) -> Path:
    """`store.cache.pull` through the replica: a read-only local copy, its exact version recorded as an input."""
    parsed = refs.parse(ref)
    project, name, version = resolve_version(store, parsed, offline)
    folder = local_folder(store, project, name, version)
    exact = replica.exact_ref(project, name, version)
    path = inside(folder, parsed, exact)
    if record:
        record_input(exact)
    return path
