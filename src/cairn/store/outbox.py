"""The outbox: versions on their way to the remote, one folder per version, in the repo.

    <repo>/outbox/<uid>/manifest.json            the entry: everything the remote needs, and its sync state
    <repo>/outbox/<uid>/heartbeat                a running version's owner: {"time", "host", "pid"}, rewritten while it runs
    <repo>/outbox/<uid>/.flushing-<host>-<pid>   a sender's claim on it: {"time", "host", "pid"}
    <repo>/blobs/<aa>/<sha256>                   the contents the entries need (shared with the rest of the repo)

An entry holds project, name, the manifest entries, label, metadata, inputs, aliases, and source, plus `sync`
(pending | running | rejected), `status` (the version's own: completed | running | failed), `owner` (host, pid),
`number` (None until the remote assigns it), and `error`. A version is pending from the moment its entry exists
until the remote has it: then its number is written into the repo (a copy, store/local.py) and only after that is
the entry removed (client/sync.py). Every entry update is write-temp-and-rename; there are no locks: one process
writes an entry while it is running (its owner), and once it is final any sender may send it, after claiming it.

Folders, records, and claims are written under scratch names and renamed into place, so the outbox works on a
network filesystem too (a cluster's shared project space): jobs on many nodes each write their own entries, and a
login node sends them.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from .folders import RECORD, hostname, pid_alive, read_json, read_record, remove, scratch, write_json
from .refs import Ref

OUTBOX = "outbox"
HEARTBEAT = "heartbeat"
CLAIM = ".flushing-"
DEFAULT_TIMEOUT = 600.0


def epoch(stamp: str | None) -> float:
    try:
        return datetime.fromisoformat(stamp).timestamp() if stamp else 0.0
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ what an entry says


def belongs(record: dict, url: str, uid: str | None) -> bool:
    """Whether an entry is for the remote at `url` (or with repo uid `uid`)."""
    return record.get("url") == url.rstrip("/") or bool(uid and record.get("store") == uid)


def manifest_of(record: dict) -> dict:
    """A version-style manifest for an entry (what `Version`, the UI, and `pull` read): number None, `pending` set."""
    status = "pending" if record["status"] == "completed" else record["status"]
    return {"version": record.get("number"), "uid": record["uid"], "label": record.get("label"), "status": status,
            "created": record["created"], "completed": record.get("completed"), "files": record["files"],
            "metadata": record.get("metadata") or {}, "inputs": record.get("inputs") or [], "source": record.get("source"),
            "digest": record.get("digest"), "pending": True, "sync": record["sync"], "aliases": record.get("aliases") or [],
            "error": record.get("error")}


def usable(record: dict | None) -> bool:
    """A pending version this repo can read and use as an input before the remote has it."""
    return bool(record) and record.get("sync") == "pending" and record.get("status") == "completed"


def exact_ref(project: str, name: str, manifest: dict) -> str:
    """`project:name@v<n>`, or `project:name@<uid>` while the remote has not numbered the version."""
    number = manifest.get("version")
    return str(Ref(project, name, f"v{number}" if number is not None else manifest["uid"]))


class Outbox:
    """The outbox folder of one repo (see the module doc)."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.folder = self.root / OUTBOX

    # ------------------------------------------------------------------ entries

    def folder_of(self, uid: str) -> Path:
        return self.folder / uid

    def load(self, uid: str) -> dict | None:
        """The entry of a version uid (None if there is none: never made here, or sent)."""
        record = read_record(self.folder_of(uid))
        return record if record and record.get("sync") in ("pending", "running", "rejected") else None

    def save(self, record: dict) -> None:
        write_json(self.folder_of(record["uid"]) / RECORD, {key: value for key, value in record.items() if not key.startswith("_")})

    def create(self, record: dict, running: bool) -> None:
        """Put a new entry in place in one step (built under a scratch name, then renamed): it exists whole or not at all."""
        folder = self.folder_of(record["uid"])
        folder.parent.mkdir(parents=True, exist_ok=True)
        build = scratch(folder)
        build.mkdir()
        try:
            write_json(build / RECORD, record)
            if running:
                write_json(build / HEARTBEAT, {"time": time.time(), "host": hostname(), "pid": os.getpid()})
            os.rename(build, folder)
        finally:
            if build.exists():
                remove(build)

    def records(self) -> list[dict]:
        """Every entry, oldest first (by uid, which is creation order)."""
        found = []
        try:
            folders = list(self.folder.iterdir())
        except FileNotFoundError:
            return found
        for folder in folders:
            if ".tmp-" in folder.name:
                continue
            record = read_record(folder)
            if record and record.get("sync") in ("pending", "running", "rejected"):
                found.append(record)
        return sorted(found, key=lambda record: record["uid"])

    def needed(self) -> set[str]:
        """The contents some entry still needs (never garbage, never pruned)."""
        return {item["sha256"] for record in self.records() for item in record.get("files", []) if "sha256" in item}

    def remove(self, record: dict) -> None:
        """Forget an entry (the remote has the version, and the repo knows its number): marked done first, so a sender
        that reads it meanwhile skips it, then removed."""
        folder = self.folder_of(record["uid"])
        if not folder.is_dir():
            return
        try:
            write_json(folder / RECORD, {**{key: value for key, value in record.items() if not key.startswith("_")}, "sync": "synced"})
        except OSError:
            pass
        remove(folder)

    # --------------------------------------------------------------- heartbeats

    def beat(self, uid: str) -> None:
        """The owner of a running version says it is alive (write-temp-and-rename; never fails, never makes the folder)."""
        if not self.folder_of(uid).is_dir():
            return
        try:
            write_json(self.folder_of(uid) / HEARTBEAT, {"time": time.time(), "host": hostname(), "pid": os.getpid()})
        except OSError:
            pass

    def last_beat(self, record: dict) -> float:
        beat = read_json(self.folder_of(record["uid"]) / HEARTBEAT) or {}
        return float(beat.get("time") or epoch(record.get("created")))

    def dead(self, record: dict, timeout: float) -> str | None:
        """Why a running version's owner is gone (None while it may still run).

        The same host checks the pid (fast); any host fails it once its heartbeat is older than `timeout`."""
        owner = record.get("owner") or {}
        if owner.get("host") == hostname() and owner.get("pid") and not pid_alive(owner["pid"]):
            return f"its process ({owner.get('host')} pid {owner['pid']}) is gone"
        age = time.time() - self.last_beat(record)
        if age > timeout:
            return f"no heartbeat from {owner.get('host')} pid {owner.get('pid')} for {age:.0f} s (timeout {timeout:g} s)"
        return None

    # ------------------------------------------------------------------ claims

    @staticmethod
    def _claim_name() -> str:
        host = "".join(char if char.isalnum() or char in "-_." else "_" for char in hostname())[:60] or "host"
        return f"{CLAIM}{host}-{os.getpid()}"

    @staticmethod
    def _stale(path: Path, timeout: float) -> bool:
        claim = read_json(path)
        if not isinstance(claim, dict):
            try:
                return time.time() - path.stat().st_mtime > timeout
            except OSError:
                return True
        if claim.get("host") == hostname() and claim.get("pid") and not pid_alive(claim["pid"]):
            return True
        return time.time() - float(claim.get("time", 0)) > timeout

    def claim(self, uid: str, timeout: float) -> Path | None:
        """Claim an entry for sending: exclusively create `.flushing-<host>-<pid>` (a timestamp in it).

        None when another live claim exists (stale ones, older than `timeout` or of a dead pid on this host, are
        ignored) or the entry left. Two senders that claim at the same moment both see both claims and back off;
        the next sync sends it. A claim only saves double work: sending is idempotent anyway."""
        folder = self.folder_of(uid)
        mine = folder / self._claim_name()
        if not folder.is_dir():
            return None
        others = [path for path in folder.glob(f"{CLAIM}*") if path != mine and ".tmp-" not in path.name]
        if any(not self._stale(path, timeout) for path in others):
            return None
        temporary = scratch(mine)
        stamp = json.dumps({"time": time.time(), "host": hostname(), "pid": os.getpid()})
        try:
            temporary.write_text(stamp)
            try:
                os.link(temporary, mine)  # exclusive: fails if it exists
            except FileExistsError:
                if not self._stale(mine, timeout):  # this process holds it already (another thread)
                    return None
                os.replace(temporary, mine)
            except OSError:  # a filesystem without hard links (SMB, some FUSE): an exclusive create instead
                if not folder.is_dir():
                    return None
                try:
                    descriptor = os.open(mine, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                except FileExistsError:
                    if not self._stale(mine, timeout):
                        return None
                    os.replace(temporary, mine)
                else:
                    with os.fdopen(descriptor, "w") as handle:
                        handle.write(stamp)
        except OSError:  # the folder left (sent by another sender)
            return None
        finally:
            temporary.unlink(missing_ok=True)
        others = [path for path in folder.glob(f"{CLAIM}*") if path != mine and ".tmp-" not in path.name]
        if any(not self._stale(path, timeout) for path in others):
            self.release(mine)
            return None
        return mine

    @staticmethod
    def refresh_claim(path: Path | None) -> None:
        if path is not None:
            try:
                write_json(path, {"time": time.time(), "host": hostname(), "pid": os.getpid()})
            except OSError:
                pass

    @staticmethod
    def release(path: Path | None) -> None:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
