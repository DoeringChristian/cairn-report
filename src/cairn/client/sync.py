"""Sending the replica's pending versions to their store server, and learning what it knows.

A publish to a store server first commits the version to the replica (replica.py): a local record
with the manifest entries, label, metadata, inputs, aliases, and source, and the contents it needs
cloned (copy-on-write, `clonefile` / `FICLONE`) or copied from the source folder into the data root's
blobs and hashed again there, so the upload never reads the source folder: a file that changes after
publishing does not change the pending version, and a file that changed between hashing and staging
is published as staged. (A hard link would share the source's inode, so a job rewriting a file in
place would change the pending version; links are therefore not used here.)

Kinds and sync states:
- `publish` (`sync: pending`): one finished version, sent with the publish route.
- `running` (`sync: running`, `status: running`): a `Publication` (`cairn publish --watch`). The version
  is created on the server under its uid as soon as the server answers, its files are added as they
  are synced, and its owner rewrites `heartbeat` (time, host, pid) on every sync. Completion or failure
  turns it `pending` (status completed or failed) and is sent like any publish. Any flusher on any host
  fails it (a queued failure) once its heartbeat is older than `[store] heartbeat_timeout` (600 s); on
  the owner's host a dead pid fails it at once.
- `rejected`: the server refused it (an error other than being unreachable); kept for `cairn status`
  and skipped by every flush. Delete its folder to drop it.

Sending is idempotent because every request carries the version's uid: the server answers a version
it already has with that version, so a record sent twice (a crash after the server's answer, two hosts
flushing one shared data root) creates one version. To avoid the double work, a flusher claims a
record first (`replica.claim`: an exclusively created `.flushing-<host>-<pid>` with a timestamp; stale
claims are ignored). Records are sent in uid order, which is creation order, so a pending version that
consumed another pending version (its input is `project:name@<uid>`) follows it, and the server turns
that input into `@v<n>`. A sent version is promoted to the cache (replica.promote) under the same uid.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path

from ..store import drop_inputs
from ..store.blobs import now
from ..store.cache import hostname, place, scratch
from ..store.fs import clone_or_copy
from ..store.digest import manifest_digest
from ..store.errors import MissingBlobs, NotFound, StoreError, Unreachable
from ..store.uids import new_uid
from . import replica
from .http import Client, HashCache, entries_for, scan
from .remote import RemoteStore

RETRIES = 3  # sends of a version whose blobs the server's garbage collection removed meanwhile

# ------------------------------------------------------------------ staging


def _digest(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _stage_one(entry: dict, source: Path, cache: HashCache, link: bool = False) -> bool:
    """Clone one file into the data root's blobs (False if it left meanwhile); fixes the entry if the file changed."""
    target = replica.data_blob(entry["sha256"])
    if target.is_file():
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = scratch(target)
    try:
        if not (link and _clone_or_link(source, temporary)):
            clone_or_copy(source, temporary)
        sha, size = _digest(temporary)
        if sha != entry["sha256"]:  # the file changed after it was hashed: publish what was staged
            cache.forget(source)
            entry.update(sha256=sha, size=size)
            target = replica.data_blob(sha)
            target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(temporary, 0o444)
        place(temporary, target)
        return True
    except FileNotFoundError:  # removed since the scan
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _clone_or_link(source: Path, target: Path) -> bool:
    """A clone where possible, else a hard link (False, nothing left behind, when neither works)."""
    from ..store.fs import clone

    if clone(source, target):
        return True
    try:
        os.link(source, target)
        return True
    except OSError:
        return False


def stage(local: Path, config, cache: HashCache, link: bool = False) -> tuple[list[dict], dict[str, Path]]:
    """Manifest entries for a folder (its inputs record excluded), each blob kept in the data root (`link`: hard
    links where no clone is possible, for a folder deleted right after: a published draft).

    Returns (entries, {sha: source file}) — the sources let `restage` put back a blob that a concurrent
    flush dropped before the record naming it was saved."""
    files = scan(local, "", config, exclude=config.store.get("exclude", []))
    drop_inputs(files)
    entries, _ = entries_for(files, cache)
    kept, sources = [], {}
    for entry in entries:
        if "sha256" in entry:
            source = files[entry["path"]]["file"]
            if not _stage_one(entry, source, cache, link):
                continue
            sources[entry["sha256"]] = source
        kept.append(entry)
    cache.save()
    return kept, sources


def restage(entries: list[dict], sources: dict[str, Path], cache: HashCache) -> None:
    """After the record is saved: any blob a concurrent flush removed in between is staged again."""
    for entry in entries:
        if "sha256" in entry and not replica.data_blob(entry["sha256"]).is_file():
            sha = entry["sha256"]
            if not _stage_one(entry, sources[sha], cache) or entry["sha256"] != sha:
                raise StoreError(f"{sources[sha]} changed while it was published; publish again")


# ------------------------------------------------------------------ sending


class Replica:
    """The replica's records for one store server (see the module doc)."""

    def __init__(self, remote: RemoteStore, timeout: float | None = None):
        self.remote = remote
        self.timeout = replica.heartbeat_timeout() if timeout is None else timeout

    # ------------------------------------------------------------ records

    def records(self) -> list[dict]:
        """This server's local records, oldest first (by uid)."""
        uid = replica.known_uid(self.remote.url) or self.remote._uid
        return [record for record in replica.records() if replica.belongs(record, self.remote.url, uid)]

    def pending(self) -> list[dict]:
        """The records still to send (everything but rejected ones)."""
        return [record for record in self.records() if record["sync"] in ("pending", "running")]

    def add(self, kind: str, project: str, name: str, *, files=(), label=None, metadata=None, inputs=(), aliases=(),
            source=None, uid: str | None = None) -> dict:
        """Commit a new record (durably, before anything is sent); `uid` the version's (default: a new one)."""
        running = kind == "running"
        record = {"uid": uid or new_uid(), "kind": kind, "sync": "running" if running else "pending",
                  "status": "running" if running else "completed", "url": self.remote.url, "store": self.remote.key,
                  "project": project, "name": name, "label": label, "metadata": metadata or {}, "inputs": list(inputs),
                  "aliases": list(aliases), "source": source, "files": list(files), "created": now(),
                  "owner": {"host": hostname(), "pid": os.getpid()}, "number": None, "pushed": {}, "error": None}
        if record["status"] == "completed":
            record["digest"] = manifest_digest(record["files"])  # known before anything is uploaded
        folder = replica.folder_of(record["uid"])
        build = scratch(folder)
        build.mkdir(parents=True)
        replica.write_json(build / replica.RECORD, record)
        if running:
            replica.write_json(build / replica.HEARTBEAT, {"time": time.time(), "host": hostname(), "pid": os.getpid()})
        os.rename(build, folder)
        return record

    def save(self, record: dict) -> None:
        replica.save(record)

    # ------------------------------------------------------------- sending

    def _upload(self, files: list[dict]) -> None:
        """Upload the blobs the server lacks, from the data root (or the cache)."""
        sources = {}
        missing = set(self.remote.missing(files)) if any("sha256" in item for item in files) else set()
        for item in files:
            if item.get("sha256") in missing:
                blob = replica.blob_source(item["sha256"])
                if blob is None:
                    raise StoreError(f"the staged contents of {item['path']} ({item['sha256'][:12]}) left {replica.data_root()}")
                sources[item["sha256"]] = (blob, item["size"])
        if sources:
            self.remote.upload(sources, None, quiet=True)

    def push(self, record: dict, claimed=None) -> bool:
        """Send what the server lacks of a record; True once the version is final there (it is then promoted).

        Raises Unreachable when the server does not answer, and StoreError when it refuses."""
        remote, project, name = self.remote, record["project"], record["name"]
        remote.create_project(project)
        if record["status"] == "completed":
            record["digest"] = manifest_digest(record["files"])  # the server recomputes it from the files and refuses a mismatch
            self._start_inputs(record)
        manifest = {key: record.get(key) for key in ("label", "metadata", "source")}
        if record["kind"] == "publish":
            for attempt in range(RETRIES):
                self._upload(record["files"])
                replica.refresh_claim(claimed)
                try:
                    record["number"] = remote.publish(project, name, record["files"], inputs=record["inputs"],
                                                      aliases=record["aliases"], uid=record["uid"], digest=record["digest"], **manifest)
                    break
                except MissingBlobs:  # the server's garbage collection removed a blob since we checked: send it again
                    if attempt == RETRIES - 1:
                        raise
            return self._done(record)
        if record["number"] is None:
            record["number"] = remote.start_version(project, name, uid=record["uid"], **manifest)
            record["pushed_metadata"] = record.get("metadata")
            self.save(record)
        if record.get("pushed_metadata", record.get("metadata")) != record.get("metadata") and record["status"] == "running":
            remote.set_metadata(project, name, record["number"], record.get("metadata") or {})  # changed while it ran
            record["pushed_metadata"] = record.get("metadata")
            self.save(record)
        fresh = [item for item in record["files"] if record["pushed"].get(item["path"]) != (item.get("sha256") or item.get("link"))]
        try:
            if fresh:
                for attempt in range(RETRIES):
                    self._upload(fresh)
                    replica.refresh_claim(claimed)
                    try:
                        remote.add_files(project, name, record["number"], fresh)
                        break
                    except MissingBlobs:  # removed by garbage collection since we checked: send it again
                        if attempt == RETRIES - 1:
                            raise
                record["pushed"].update({item["path"]: item.get("sha256") or item.get("link") for item in fresh})
                self.save(record)
            if record["status"] == "running":
                return False
            if record["status"] == "completed":
                remote.complete_version(project, name, record["number"], record["inputs"], digest=record["digest"])
                for alias in record["aliases"]:
                    remote.set_alias(project, name, alias, record["number"])
            else:
                remote.fail_version(project, name, record["number"])
        except StoreError as error:
            if isinstance(error, Unreachable) or "can no longer change" not in str(error):
                raise
            final = remote.get_version(project, name, record["uid"])  # sent before (by us or another process)
            if final["status"] == "running":
                raise
            if final["status"] == "completed" and record["status"] == "completed":
                for alias in record["aliases"]:
                    remote.set_alias(project, name, alias, record["number"])
        return self._done(record)

    def _start_inputs(self, record: dict) -> None:
        """An input that is a running version of this machine (the run an output comes from) must exist on the server
        before the output names it: create it there now (idempotent by uid; its owner keeps syncing it)."""
        from ..store import refs

        for item in record.get("inputs") or []:
            try:
                uid = refs.parse(item).uid
            except ValueError:
                continue
            if uid is None:
                continue
            other = replica.load(uid)
            if other is None or other.get("sync") != "running" or other.get("number") is not None:
                continue
            manifest = {key: other.get(key) for key in ("label", "metadata", "source")}
            self.remote.create_project(other["project"])
            number = self.remote.start_version(other["project"], other["name"], uid=other["uid"], **manifest)
            current = replica.load(uid)
            if current is not None and current.get("number") is None:
                current["number"] = number
                self.save(current)

    def _done(self, record: dict) -> bool:
        """The server has the version: remember its number, and promote the record into the cache."""
        remote, project, name = self.remote, record["project"], record["name"]
        if record["status"] == "failed":
            replica.drop(record)
            return True
        try:
            manifest = remote.get_version(project, name, record["number"])
        except NotFound:
            manifest = None
        if manifest is not None:
            replica.remember(remote.uid or remote.key, project, name, record["aliases"], manifest, remote.url)
        if manifest is not None and manifest.get("uid") == record["uid"]:
            replica.promote(record, manifest, remote.uid)
        else:  # nothing changed since the latest: the server kept its version and not this uid
            replica.drop(record)
        return True

    def send(self, record: dict, claim: bool = True) -> bool:
        """Push one record; a refusal marks it rejected (and raises), being unreachable leaves it pending (and raises).

        With `claim`, the record is claimed first (see the module doc); False means another flusher has it or sent it.
        Without, False means the version is still running."""
        claimed = None
        if claim:
            claimed = replica.claim(record["uid"], self.timeout)
            if claimed is None:
                return False
            current = replica.load(record["uid"])  # read again: another flusher may have sent it meanwhile
            if current is None or current["sync"] not in ("pending", "running"):
                replica.release(claimed)
                return False
            record.clear()
            record.update(current)
        try:
            return self.push(record, claimed)
        except Unreachable:
            raise
        except StoreError as error:
            record.update(sync="rejected", error=str(error))
            self.save(record)
            raise
        finally:
            replica.release(claimed)

    def flush(self, report=None) -> dict:
        """Send every pending record of the data root in uid order; stops at the first one the server does not answer.

        A running record whose owner is gone (`replica.dead`) is failed first. Returns {"sent": [records],
        "rejected": [records], "waiting": [records still pending or running]}; `report(line)` hears about each."""
        result = {"sent": [], "rejected": [], "waiting": []}
        records = self.pending()
        for index, record in enumerate(records):
            if record["sync"] == "running":
                why = replica.dead(record, self.timeout)
                if why is None:
                    result["waiting"].append(record)  # its own process sends it
                    continue
                current = replica.load(record["uid"])
                if current is None or current["sync"] != "running":
                    continue
                record = current
                record.update(sync="pending", status="failed", error=f"failed by {hostname()}: {why}")
                self.save(record)  # a queued failure, so nothing stays running for good
                if report:
                    report(f"failing {record['project']}:{record['name']}@{record['uid']}: {why}")
            try:
                sent = self.send(record)
            except Unreachable:
                result["waiting"].extend(records[index:])
                break
            except StoreError as error:
                result["rejected"].append(record)
                if report:
                    report(f"rejected {record['project']}:{record['name']}@{record['uid']}: {error}")
                continue
            if not sent:
                if replica.load(record["uid"]) is not None:
                    result["waiting"].append(record)  # another flusher has it
                continue
            result["sent"].append(record)
            if report:
                what = "failed" if record["status"] == "failed" else "uploaded"
                report(f"{what} {record['project']}:{record['name']}@v{record['number']} (uid {record['uid']})")
        return result

    def refresh(self) -> int:
        """Learn the current aliases of every artifact this machine has versions of (for offline pulls); returns how many."""
        remote = self.remote
        uid = remote.uid
        key = uid or remote.key
        artifacts = {(record["project"], record["name"]) for record in self.records()}
        artifacts |= {(record["project"], record["name"]) for record in replica.synced_records()
                      if record.get("store") == uid and record.get("project")}
        count = 0
        for project, name in sorted(artifacts):
            try:
                aliases = remote.aliases(project, name)
            except NotFound:
                continue
            known = {}
            for alias, number in aliases.items():
                found = replica.hint(key, project, name, f"v{number}")
                if found is None:
                    try:
                        manifest = remote.get_version(project, name, number)
                    except NotFound:
                        continue
                    found = {"uid": manifest["uid"], "version": number}
                known[alias] = found
            replica.remember_aliases(key, project, name, known)
            count += 1
        return count


# --------------------------------------------------------------- uploader

_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


def start_uploader(remote: RemoteStore, first_delay: float = 1.0, max_delay: float = 60.0) -> threading.Thread:
    """A daemon thread that flushes this server's pending versions until none are left (backing off while it is down).
    One per server and process; it stops with the interpreter (what is left is sent by the next flush)."""
    with _threads_lock:
        thread = _threads.get(remote.url)
        if thread is not None and thread.is_alive():
            return thread

        def loop():
            delay = first_delay
            replica_ = Replica(remote)
            while True:
                time.sleep(delay)
                try:
                    result = replica_.flush(report=lambda line: print(f"cairn: {line}", file=sys.stderr, flush=True))
                    left = [record for record in result["waiting"] if record["sync"] != "running"]  # running: its process sends it
                except Exception:  # never let the uploader die noisily; the next flush retries
                    left = [True]
                if not left:
                    return
                delay = min(delay * 2, max_delay)

        thread = threading.Thread(target=loop, name=f"cairn-uploader {remote.url}", daemon=True)
        _threads[remote.url] = thread
        thread.start()
        return thread


def has_pending(url: str) -> bool:
    """Whether anything is pending or running for this server (a look at the data root only, no network)."""
    uid = replica.known_uid(url)
    return any(record["sync"] in ("pending", "running") and replica.belongs(record, url, uid) for record in replica.records())


def quick(remote: RemoteStore, timeout: float = 10) -> RemoteStore:
    """The same server with a short timeout, for attempts that must not hold up a command."""
    client = remote.client
    fast = Client.__new__(Client)
    fast.__dict__.update(client.__dict__)
    fast.timeout = timeout
    copy = RemoteStore(fast, remote.project)
    copy._uid = remote._uid
    return copy


def flush_configured(config, report=None, timeout: float = 10) -> dict | None:
    """Send what is pending for the configured store server, if anything is (the start of every store command).

    Looks at the data root first, so a command with nothing pending makes no request. Never raises for a
    server that does not answer: what is left stays pending."""
    from .http import store_url

    url = store_url(config)
    if not url or not has_pending(url):
        return None
    from .open import open_store

    try:
        return Replica(quick(open_store(config), timeout), replica.heartbeat_timeout(config)).flush(report)
    except (StoreError, OSError):
        return None
