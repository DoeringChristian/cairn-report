"""The outbox: versions published to a store server are committed here first, then uploaded.

    <cache>/outbox/<store-uid>/<version-uid>.json      one version waiting for (or being sent to) the server
    <cache>/blobs/<aa>/<sha256>                        the contents it needs, staged when it was published

(A server never reached before has no known uid yet; its outbox folder is named from its URL instead,
`url-<hash>`, and is flushed together with the uid folder once the uid is known.)

An entry holds everything the server needs: project, name, the manifest entries, label, metadata,
inputs, aliases, source, and its `state`. Its blobs are cloned (copy-on-write, `clonefile` /
`FICLONE`) or copied from the source folder into the blob cache and hashed again there, so the
upload never reads the source folder: a file that changes after publishing does not change the
queued version, and a file that changed between hashing and staging is published as staged. (A hard
link would share the source's inode, so a job rewriting a file in place would change the queued
version; links are therefore not used here.)

Kinds and states:
- `publish` (`state: pending`): one finished version, sent with the publish route.
- `running` (`state: running | completed | failed`): a `Publication` (`cairn publish --watch`). The
  version is created on the server under its uid as soon as the server answers, its files are added
  as they are synced, and completion or failure is queued like everything else. A running entry is
  owned by the process that runs it; `flush` leaves it alone unless that process is gone (same host,
  dead pid), in which case the version is marked failed, so nothing stays running for good.
- `rejected`: the server refused it (an error other than being unreachable); kept for `cairn
  status` and skipped by every flush. Delete the file to drop it.

Sending is idempotent because every request carries the version's uid: the server answers a version
it already has with that version, so an entry sent twice (a crash after the server's answer, two
processes flushing at once) creates one version. Entries are sent in uid order, which is creation
order. A sent entry is removed, its staged blobs are dropped (unless another entry needs them), and
the uid's number is remembered for offline pulls.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

from ..store import INPUTS_FILE
from ..store.blobs import now
from ..store.cache import blob_cache, cache_root, place, scratch
from ..store.errors import StoreError, Unreachable
from ..store.uids import new_uid
from . import state
from .http import Client, HashCache, entries_for, scan
from .remote import RemoteStore

PENDING_STATES = ("pending", "running", "completed", "failed")


# ------------------------------------------------------------------ staging


def _clonefile():
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        function = libc.clonefile
        function.argtypes, function.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int
        return function
    except (OSError, AttributeError):
        return None


_CLONE = _clonefile()
FICLONE = 0x40049409


def clone_or_copy(source: Path, target: Path) -> None:
    """A copy-on-write clone where the filesystem has them (APFS, Btrfs, XFS), else a plain copy."""
    if _CLONE is not None and _CLONE(os.fsencode(source), os.fsencode(target), 0) == 0:
        return
    if sys.platform.startswith("linux"):
        try:
            import fcntl

            with open(source, "rb") as reader, open(target, "wb") as writer:
                fcntl.ioctl(writer.fileno(), FICLONE, reader.fileno())
            return
        except (OSError, ImportError):
            Path(target).unlink(missing_ok=True)
    shutil.copyfile(source, target)


def _digest(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def stage(local: Path, config, cache: HashCache) -> tuple[list[dict], list[str]]:
    """Manifest entries for a folder (its inputs record excluded), each blob kept in the blob cache.

    Returns (entries, the hashes this call put into the blob cache)."""
    files = scan(local, "", config, exclude=config.store.get("exclude", []))
    files.pop(INPUTS_FILE, None)
    entries, _ = entries_for(files, cache)
    root, staged = cache_root(), []
    for entry in entries:
        if "sha256" not in entry or blob_cache(root, entry["sha256"]).is_file():
            continue
        source = files[entry["path"]]["file"]
        target = blob_cache(root, entry["sha256"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = scratch(target)
        try:
            clone_or_copy(source, temporary)
            sha, size = _digest(temporary)
            if sha != entry["sha256"]:  # the file changed after it was hashed: publish what was staged
                cache.forget(source)
                entry.update(sha256=sha, size=size)
                target = blob_cache(root, sha)
                target.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(temporary, 0o444)
            if place(temporary, target):
                staged.append(sha)
        except FileNotFoundError:  # removed since the scan
            entry["drop"] = True
        finally:
            temporary.unlink(missing_ok=True)
    cache.save()
    return [entry for entry in entries if not entry.pop("drop", False)], staged


# ------------------------------------------------------------------ outbox


def outbox_root() -> Path:
    return cache_root() / "outbox"


def _alive(owner: dict | None) -> bool:
    """Whether the process that owns a running entry may still run (another host: assume so)."""
    if not owner or owner.get("host") != socket.gethostname():
        return True
    try:
        os.kill(owner["pid"], 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Outbox:
    """The outbox of one store server (see the module doc)."""

    def __init__(self, remote: RemoteStore):
        self.remote = remote

    # ------------------------------------------------------------ the files

    def folders(self) -> list[Path]:
        names = dict.fromkeys(name for name in (self.remote.key, state.known_uid(self.remote.url), state.store_key(self.remote.url)) if name)
        return [outbox_root() / name for name in names]

    def entries(self) -> list[dict]:
        """Every entry, oldest first (by uid)."""
        found = {}
        for folder in self.folders():
            for file in folder.glob("*.json"):
                entry = state.read_json(file, None)
                if isinstance(entry, dict) and entry.get("uid"):
                    entry["_file"] = str(file)
                    found.setdefault(entry["uid"], entry)
        return [found[uid] for uid in sorted(found)]

    def pending(self) -> list[dict]:
        return [entry for entry in self.entries() if entry["state"] in PENDING_STATES]

    def save(self, entry: dict) -> None:
        file = Path(entry.get("_file") or outbox_root() / self.remote.key / f"{entry['uid']}.json")
        entry["_file"] = str(file)
        state.write_json(file, {key: value for key, value in entry.items() if key != "_file"})

    def _remove(self, entry: dict) -> None:
        Path(entry["_file"]).unlink(missing_ok=True)
        needed = {item["sha256"] for other in self.entries() for item in other.get("files", []) if "sha256" in item}
        for sha in entry.get("staged", []):
            if sha not in needed:
                blob_cache(cache_root(), sha).unlink(missing_ok=True)

    def add(self, kind: str, project: str, name: str, *, files=(), staged=(), label=None, metadata=None, inputs=(),
            aliases=(), source=None) -> dict:
        """Commit a new entry (durably, before anything is sent)."""
        entry = {"uid": new_uid(), "kind": kind, "state": "pending" if kind == "publish" else "running", "url": self.remote.url,
                 "project": project, "name": name, "label": label, "metadata": metadata or {}, "inputs": list(inputs),
                 "aliases": list(aliases), "source": source, "files": list(files), "staged": list(staged), "created": now(),
                 "owner": {"host": socket.gethostname(), "pid": os.getpid()}, "number": None, "pushed": {}, "error": None}
        self.save(entry)
        return entry

    # ------------------------------------------------------------- sending

    def _upload(self, files: list[dict]) -> None:
        """Upload the staged blobs the server lacks."""
        root, sources = cache_root(), {}
        missing = set(self.remote.missing(files)) if any("sha256" in item for item in files) else set()
        for item in files:
            if item.get("sha256") in missing:
                blob = blob_cache(root, item["sha256"])
                if not blob.is_file():
                    raise StoreError(f"the staged contents of {item['path']} ({item['sha256'][:12]}) left the blob cache")
                sources[item["sha256"]] = (blob, item["size"])
        if sources:
            self.remote.upload(sources, None, quiet=True)

    def push(self, entry: dict) -> bool:
        """Send what the server lacks of an entry; True once the version is final there (the entry is then removed).

        Raises Unreachable when the server does not answer, and StoreError when it refuses."""
        remote, project, name = self.remote, entry["project"], entry["name"]
        remote.create_project(project)
        manifest = {key: entry.get(key) for key in ("label", "metadata", "source")}
        if entry["kind"] == "publish":
            self._upload(entry["files"])
            entry["number"] = remote.publish(project, name, entry["files"], inputs=entry["inputs"], aliases=entry["aliases"],
                                             uid=entry["uid"], **manifest)
            return self._done(entry)
        if entry["number"] is None:
            entry["number"] = remote.start_version(project, name, uid=entry["uid"], **manifest)
            self.save(entry)
        fresh = [item for item in entry["files"] if entry["pushed"].get(item["path"]) != (item.get("sha256") or item.get("link"))]
        try:
            if fresh:
                self._upload(fresh)
                remote.add_files(project, name, entry["number"], fresh)
                entry["pushed"].update({item["path"]: item.get("sha256") or item.get("link") for item in fresh})
                self.save(entry)
            if entry["state"] == "running":
                return False
            if entry["state"] == "completed":
                remote.complete_version(project, name, entry["number"], entry["inputs"])
                for alias in entry["aliases"]:
                    remote.set_alias(project, name, alias, entry["number"])
            else:
                remote.fail_version(project, name, entry["number"])
        except StoreError as error:
            if isinstance(error, Unreachable) or "can no longer change" not in str(error):
                raise
            final = remote.get_version(project, name, entry["uid"])  # sent before (by us or another process)
            if final["status"] == "running":
                raise
            if final["status"] == "completed" and entry["state"] == "completed":
                for alias in entry["aliases"]:
                    remote.set_alias(project, name, alias, entry["number"])
        return self._done(entry)

    def _done(self, entry: dict) -> bool:
        if entry["state"] != "failed":
            try:
                state.remember(self.remote.uid, entry["project"], entry["name"], f"v{entry['number']}",
                               {"uid": entry["uid"], "version": entry["number"]})
            except StoreError:
                pass
        self._remove(entry)
        return True

    def send(self, entry: dict) -> bool:
        """Push one entry; a refusal marks it rejected (and raises), being unreachable leaves it queued (and raises)."""
        try:
            return self.push(entry)
        except Unreachable:
            raise
        except StoreError as error:
            entry.update(state="rejected", error=str(error))
            self.save(entry)
            raise

    def flush(self, report=None) -> dict:
        """Send every pending entry in order; stops at the first one the server does not answer.

        Returns {"sent": [entries], "rejected": [entries], "waiting": [entries still queued]}; `report(line)`
        hears about each one."""
        result = {"sent": [], "rejected": [], "waiting": []}
        entries = self.pending()
        for index, entry in enumerate(entries):
            if entry["state"] == "running" and entry["kind"] == "running":
                if _alive(entry.get("owner")):
                    result["waiting"].append(entry)  # its own process sends it
                    continue
                entry["state"] = "failed"  # its job died: fail the version instead of leaving it running
                self.save(entry)
            try:
                self.send(entry)
            except Unreachable:
                result["waiting"].extend(entries[index:])
                break
            except StoreError as error:
                result["rejected"].append(entry)
                if report:
                    report(f"rejected {entry['project']}:{entry['name']}@{entry['uid']}: {error}")
                continue
            result["sent"].append(entry)
            if report:
                what = "failed" if entry["state"] == "failed" else "uploaded"
                report(f"{what} {entry['project']}:{entry['name']}@v{entry['number']} (uid {entry['uid']})")
        return result


# --------------------------------------------------------------- uploader

_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


def start_uploader(remote: RemoteStore, first_delay: float = 1.0, max_delay: float = 60.0) -> threading.Thread:
    """A daemon thread that flushes this server's outbox until nothing is queued (backing off while it is down).
    One per server and process; it stops with the interpreter (what is left is sent by the next flush)."""
    with _threads_lock:
        thread = _threads.get(remote.url)
        if thread is not None and thread.is_alive():
            return thread

        def loop():
            delay = first_delay
            outbox = Outbox(remote)
            while True:
                time.sleep(delay)
                try:
                    result = outbox.flush(report=lambda line: print(f"cairn: {line}", file=sys.stderr, flush=True))
                    left = [entry for entry in result["waiting"] if entry["state"] != "running"]  # running: its process sends it
                except Exception:  # never let the uploader die noisily; the next flush retries
                    left = [True]
                if not left:
                    return
                delay = min(delay * 2, max_delay)

        thread = threading.Thread(target=loop, name=f"cairn-uploader {remote.url}", daemon=True)
        _threads[remote.url] = thread
        thread.start()
        return thread


def has_entries(url: str) -> bool:
    """Whether anything is queued for this server (a look at the folders only, no network)."""
    names = {name for name in (state.known_uid(url), state.store_key(url)) if name}
    return any(any((outbox_root() / name).glob("*.json")) for name in names)


def quick(remote: RemoteStore, timeout: float = 10) -> RemoteStore:
    """The same server with a short timeout, for attempts that must not hold up a command."""
    client = remote.client
    fast = Client.__new__(Client)
    fast.__dict__.update(client.__dict__)
    fast.timeout = timeout
    copy = RemoteStore(fast, remote.project)
    copy._uid = remote._uid
    return copy


def all_entries() -> list[dict]:
    """Every entry of every server (for `cairn status`), oldest first."""
    found = []
    for file in outbox_root().glob("*/*.json"):
        entry = state.read_json(file, None)
        if isinstance(entry, dict) and entry.get("uid"):
            entry["_file"] = str(file)
            found.append(entry)
    return sorted(found, key=lambda entry: entry["uid"])


def flush_configured(config, report=None, timeout: float = 10) -> dict | None:
    """Send what is queued for the configured store server, if anything is (the start of every store command).

    Looks at the outbox folders first, so a command with nothing queued makes no request. Never raises for a
    server that does not answer: what is left stays queued."""
    url = (config.store.get("url") or "").rstrip("/")
    if not url or not has_entries(url):
        return None
    from .open import open_store

    try:
        return Outbox(quick(open_store(config), timeout)).flush(report)
    except (StoreError, OSError):
        return None
