"""Publishing: store a folder as an artifact version (`publish`), follow a running job's folder (`Publication`,
`cairn publish --watch`).

Without `--watch` or a command a folder becomes one version (and none when nothing changed). With
either, one running version is started and the folder is synced into it while the job runs, the
way `push --watch` follows a project: every `interval` seconds, and shortly after files stop
changing (a burst of writes goes out as one sync). A store that errors only warns and backs off;
the final sync must succeed. The version completes when the command exits 0, or when watching is
stopped without a command, and fails otherwise, so `latest` never moves to a broken run.

On a store server both go through the local replica (replica.py, sync.py): a running version is a
local record first (sync `running`) and is created on the server (under its uid) as soon as the server
answers, so a job starts and finishes while the server is down; its completion or failure turns it
pending and is sent like any publish. While it runs, a heartbeat thread rewrites its `heartbeat`
(time, host, pid) every few seconds; a flusher on any host fails the version once the heartbeat is
older than `[store] heartbeat_timeout`. A running version in a local store has a heartbeat too
(`<store>/heartbeats/<uid>`): opening the store fails it once its process is dead or the heartbeat is old
(LocalStore.reap), so a killed job never stays `running`.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..config import Config, load_config
from ..store import INPUTS_ENV, INPUTS_FILE, LocalStore, StoreError, drop_inputs, read_inputs, refs
from ..store.digest import manifest_digest
from ..store.errors import Unreachable
from .http import HashCache, entries_for, scan  # noqa: F401
from .open import as_store, open_store, versions_of
from .remote import RemoteStore

SETTLE = 2.0  # seconds without new changes before a changed folder is synced


def _target(name: str, store: LocalStore | RemoteStore) -> tuple[str, str]:
    """(project, artifact name) for a publish target."""
    target = refs.parse(name)
    if target.selector or target.path:
        raise ValueError(f"publish takes an artifact name, not {name!r}")
    project = target.project or store.project
    if project is None:
        raise StoreError(f"{name!r} has no project and the store has no default")
    return project, target.name


def stage_local(store: LocalStore, local: Path, config: Config, cache: HashCache, link: bool = False) -> list[dict]:
    """Entries for a folder's files (its inputs record excluded) with their blobs put in a local store (`link`: hard
    links where no clone is possible, for a folder deleted right after, see `BlobStore.put_file`)."""
    files = scan(local, "", config, exclude=config.store.get("exclude", []))
    drop_inputs(files)
    entries, sources = entries_for(files, cache)
    cache.save()
    for sha in store.missing(entries):
        store.put_file(sha, sources[sha][0], link=link)  # a copy-on-write clone where the filesystem allows, verified
    return entries


def publish(name: str, folder: Path | str, *, metadata: dict | None = None, aliases=(), label: str | None = None,
            inputs=None, store: LocalStore | RemoteStore | Path | str | None = None, config: Config | None = None,
            allow_incomplete: bool = False) -> int | None:
    """Store a folder as the next version of artifact `name`; returns the version number (`Artifact.publish`).

    A symlinked folder is followed (the label defaults to the resolved folder's name). A folder whose
    `run.json` is not `completed` is refused unless `allow_incomplete` (then `metadata.run_status` records it). When nothing
    changed since the latest version, no version is created and its number is returned. None means the
    store server did not answer: the version is pending in the local replica (`cairn status`), usable here
    by its uid, and uploaded later.
    """
    from .objects import Store

    backend = as_store(store)
    project, artifact = _target(name, backend)
    version = Store(backend).project(project).artifact(artifact).publish(folder, metadata=metadata, aliases=aliases,
                                                                          label=label, inputs=inputs, config=config,
                                                                          allow_incomplete=allow_incomplete)
    return version.number


class Publication:
    """One running version that follows a folder while a job writes it.

        with cairn.Publication("sweep", "runs/sweep/latest") as pub:
            ...                    # the job runs and writes into the folder
            pub.sync()             # add new and changed files now (call it as often as you like)

    Leaving the block syncs once more and completes the version (inputs come from the folder's
    `.cairn-inputs.jsonl` plus `inputs`); an exception marks it failed instead. The version is
    invisible to `latest` and aliases until it completes, and a file that is written again replaces
    its earlier entry. Files deleted from the folder stay in the running version.
    """

    def __init__(self, name: str, folder: Path | str, *, metadata: dict | None = None, aliases=(), label: str | None = None,
                 inputs=None, store: LocalStore | RemoteStore | Path | str | None = None, uid: str | None = None):
        self.local = Path(folder).resolve()
        self.store = as_store(store)
        self.project, self.name = _target(name, self.store)
        self.config = load_config()
        self.cache = HashCache(self.config.project)
        self.label, self.metadata, self.aliases, self.inputs = label or self.local.name, metadata, tuple(aliases), inputs
        self.version: int | None = None  # the number; None until the server has the version
        self.uid: str | None = uid  # the version's uid (default: a new one, made when it starts)
        self.queued = False  # the server did not answer at the last attempt
        self._sent: dict[str, tuple] = {}
        self._entry: dict | None = None
        self._replica = None
        self._beating: threading.Event | None = None
        from .replica import heartbeat_timeout

        self.timeout = heartbeat_timeout(self.config)
        if isinstance(self.store, RemoteStore):
            from .sync import Replica

            self._replica = Replica(self.store, self.timeout)

    @property
    def source(self) -> dict:
        return {"host": socket.gethostname(), "folder": str(self.local)}

    @property
    def ref(self) -> str:
        """`project:name@v<n>`, or `project:name@<uid>` while the server has not answered."""
        return refs.exact(self.project, self.name, self.version) if self.version is not None else f"{self.project}:{self.name}@{self.uid}"

    def _push(self) -> bool:
        """Send the record; False (and `queued`) when the server does not answer. Once it is no longer running,
        other flushers may send it too, so it is claimed first."""
        try:
            done = self._replica.send(self._entry, claim=self._entry["sync"] != "running")
        except Unreachable:
            self.queued = True
            return False
        finally:
            self.version = self._entry["number"]
        self.queued = False
        return done

    def _save(self) -> None:
        """Save the record, unless a flusher failed it meanwhile (its heartbeat expired): then this job lost it."""
        from . import replica

        current = replica.load(self.uid)
        if current is not None and current["status"] == "failed" and self._entry["status"] != "failed":
            self._stop_beating()
            raise StoreError(f"{self.ref} was failed by another flusher ({current.get('error')}); publish the folder again")
        self._replica.save(self._entry)
        if self._entry["sync"] == "running":
            replica.beat(self.uid)

    def _start_beating(self) -> None:
        """A daemon thread that rewrites the heartbeat while the version runs (it stops with the process)."""
        from . import replica

        stop = self._beating = threading.Event()
        every = max(0.05, min(60.0, self.timeout / 5))
        uid = self.uid
        beat = replica.beat if self._replica is not None else self.store.beat

        def loop():
            while not stop.wait(every):
                beat(uid)

        threading.Thread(target=loop, name=f"cairn-heartbeat {uid}", daemon=True).start()

    def _stop_beating(self) -> None:
        if self._beating is not None:
            self._beating.set()
            self._beating = None

    def start(self) -> int | None:
        if self._replica is not None:
            if self._entry is None:
                self._entry = self._replica.add("running", self.project, self.name, label=self.label, metadata=self.metadata,
                                                source=self.source, uid=self.uid)
                self.uid = self._entry["uid"]
                self._start_beating()
                self._push()
            return self.version
        if self.version is None:
            from ..store.uids import new_uid

            self.uid = self.uid or new_uid()
            self.store.beat(self.uid)  # before the version exists: a crash right after still leaves one that can be failed
            self.store.create_project(self.project)
            self.version = self.store.start_version(self.project, self.name, self.label, self.metadata, self.source, uid=self.uid)
            self.uid = self.store.get_version(self.project, self.name, self.version)["uid"]
            self._start_beating()
        return self.version

    def sync(self) -> int:
        """Add the files that are new or changed since the last sync; returns how many."""
        self.start()
        if self._replica is not None:
            from .sync import restage, stage

            entries, sources = stage(self.local, self.config, self.cache)
        else:
            entries = stage_local(self.store, self.local, self.config, self.cache)
        fresh = [entry for entry in entries if self._sent.get(entry["path"]) != (entry.get("sha256"), entry.get("link"))]
        self._sent.update({entry["path"]: (entry.get("sha256"), entry.get("link")) for entry in fresh})
        if self._replica is not None:
            files = {item["path"]: item for item in self._entry["files"]}
            files.update({entry["path"]: entry for entry in fresh})
            self._entry["files"] = sorted(files.values(), key=lambda item: item["path"])
            self._save()
            restage(entries, sources, self.cache)
            self._push()
        elif fresh:
            self.store.add_files(self.project, self.name, self.version, fresh)
        return len(fresh)

    def set_metadata(self, metadata: dict) -> None:
        """Replace the metadata of the running version (not once it completed or failed); sent with the next sync
        on a store server."""
        self.metadata = metadata
        if self._replica is not None:
            if self._entry is not None:
                self._entry["metadata"] = metadata
                self._save()
                self._push()
        elif self.version is not None:
            self.store.set_metadata(self.project, self.name, self.version, metadata)

    def complete(self) -> int | None:
        """Sync a last time, freeze the version, move `latest`, and apply the aliases. On a store server that does
        not answer, the completed version is pending (`queued` is True) and the number is None until it is sent."""
        self.sync()
        merged = [*read_inputs(self.local), *(str(item) for item in self.inputs or [])]
        if self._replica is not None:
            self._entry.update(sync="pending", status="completed", inputs=merged, aliases=list(self.aliases),
                               digest=manifest_digest(self._entry["files"]))
            self._save()
            self._stop_beating()
            if not self._push():
                from .sync import start_uploader

                start_uploader(self.store)
            return self.version
        self._stop_beating()
        self.store.complete_version(self.project, self.name, self.version, merged)
        for alias in self.aliases:
            self.store.set_alias(self.project, self.name, alias, self.version)
        return self.version

    def fail(self) -> None:
        if self._replica is not None:
            if self._entry is not None:
                self._entry.update(sync="pending", status="failed")
                self._save()
                self._stop_beating()
                if not self._push():
                    from .sync import start_uploader

                    start_uploader(self.store)
            return
        if self.version is not None:
            self._stop_beating()
            self.store.fail_version(self.project, self.name, self.version)

    def __enter__(self) -> "Publication":
        self.start()
        return self

    def __exit__(self, kind, error, trace) -> None:
        if kind is None:
            self.complete()
        else:
            self.fail()


def parse_meta(items: list[str]) -> dict:
    """KEY=VALUE pairs; a value that is valid JSON (a number, true, a list) keeps its type."""
    meta: dict = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise SystemExit(f"--meta takes KEY=VALUE, got {item!r}")
        try:
            meta[key] = json.loads(value)
        except ValueError:
            meta[key] = value
    return meta


def describe(version: dict) -> str:
    size = sum(entry.get("size", 0) for entry in version["files"])
    return f"{len(version['files'])} files, {size / 1e6:.1f} MB"


def publish_folder(config: Config, folder: Path, name: str, label: str | None, aliases: list[str], meta: dict,
                   allow_incomplete: bool = False) -> None:
    """Publish one folder and print what happened."""
    store = open_store(config)
    try:
        before = max((v["version"] for v in versions_of(store, name)), default=0)
    except StoreError:  # also a server that does not answer: the version stays pending below
        before = 0
    number = publish(name, folder, metadata=meta, aliases=aliases, label=label, store=store, config=config,
                    allow_incomplete=allow_incomplete)
    target = refs.parse(name)
    if number is None:
        return print(f"pending {target.name}: the store does not answer; `cairn status` lists it, `cairn sync` sends it", flush=True)
    if number <= before:
        print(f"{target.name}@v{number} unchanged", flush=True)
    else:
        state = store.resolve(refs.exact(target.project or store.project, target.name, number))[2]
        print(f"published {target.name}@v{number} ({describe(state)})", flush=True)


def publish_mappings(config: Config, label: str | None, aliases: list[str], meta: dict, allow_incomplete: bool = False) -> None:
    mappings = config.store.get("publish", [])
    if not mappings:
        raise SystemExit("nothing to publish: give FOLDER NAME or add [store] publish = [{ folder = ..., name = ... }] to cairn.toml")
    for mapping in mappings:
        folder = config.project / mapping["folder"]
        if not folder.is_dir():
            print(f"skipping {mapping['name']}: {mapping['folder']} does not exist", flush=True)
            continue
        publish_folder(config, folder, mapping["name"], label, aliases, meta, allow_incomplete)


def signature(folder: Path, config: Config) -> dict:
    """Path -> (size, mtime) of the folder's files: a cheap way to see that something changed."""
    files = scan(folder, "", config, exclude=config.store.get("exclude", []))
    return {path: (info.get("size"), info.get("mtime_ns"), info.get("link")) for path, info in files.items()}


def watch_folder(config: Config, folder: Path, name: str, label: str | None, aliases: list[str], meta: dict,
                 command: list[str] | None = None, interval: float | None = None) -> int:
    """Follow `folder` into one running version of `name` while `command` runs; returns the exit code."""
    interval = float(interval if interval is not None else config.store.get("interval", 30))
    settle, tick = min(SETTLE, interval / 2), max(0.05, min(1.0, interval / 4))
    stopped: list[int] = []
    previous = {signum: signal.signal(signum, lambda number, _frame: stopped.append(number))
                for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        return _follow(config, folder, name, label, aliases, meta, command, interval, settle, tick, stopped)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _follow(config, folder, name, label, aliases, meta, command, interval, settle, tick, stopped) -> int:
    pub = Publication(name, folder, metadata=meta, aliases=aliases, label=label, store=open_store(config))
    for attempt in range(5):  # a local store that errors; a store server that does not answer is no reason to wait
        try:
            pub.start()
            break
        except (StoreError, OSError) as error:
            if attempt == 4:
                raise
            print(f"warning: could not start the version ({error}); retrying", file=sys.stderr, flush=True)
            time.sleep(1)
    if pub.version is None:
        print(f"publishing {folder} as {pub.name}@{pub.uid} (running, pending: the store does not answer yet) every {interval:g} s",
              flush=True)
    else:
        print(f"publishing {folder} as {pub.name}@v{pub.version} (running) every {interval:g} s", flush=True)
    # what the command pulls is recorded in the folder it writes, unless the caller chose a file
    env = os.environ | {INPUTS_ENV: os.environ.get(INPUTS_ENV) or str(Path(folder).resolve() / INPUTS_FILE)}
    process = subprocess.Popen(command, env=env) if command else None
    failures, seen, changed_at = 0, None, 0.0
    last = time.monotonic()
    synced = None  # the signature of the folder at the last good sync

    def sync() -> bool:
        nonlocal failures, synced, last
        try:
            current = signature(folder, config)
            added = pub.sync()
            synced, last, failures = current, time.monotonic(), 0
            if added:
                where = f"{pub.name}@v{pub.version}" if pub.version is not None else f"{pub.name}@{pub.uid} (pending)"
                print(f"synced {added} files to {where}", flush=True)
            return True
        except (StoreError, OSError) as error:
            failures += 1
            print(f"warning: sync failed ({error}); retrying", file=sys.stderr, flush=True)
            last = time.monotonic()
            return False

    def running() -> bool:
        return not stopped and not (process and process.poll() is not None)

    while running():
        time.sleep(tick)
        current = signature(folder, config)
        now = time.monotonic()
        if current != seen:
            seen, changed_at = current, now
        wait = min(interval * 2 ** min(failures, 4), 600) if failures else 0  # back off while the store errors
        if current != synced and (now - changed_at >= settle or now - last >= interval) and now - last >= wait:
            sync()
    if process and process.poll() is None:
        process.send_signal(signal.SIGTERM)  # we were stopped: stop the command too
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    code = 0
    if process:
        code = process.returncode if process.returncode >= 0 else 128 - process.returncode
    for attempt in range(5):  # the final state must reach the store
        try:
            if code:
                pub.sync()  # keep what the failed run wrote for inspection
                pub.fail()
                print(f"{pub.ref} failed (command exited {code}); latest did not move", file=sys.stderr, flush=True)
            else:
                number = pub.complete()
                if number is None or pub.queued:
                    print(f"pending {pub.name}@{pub.uid} (completed): the store does not answer; it is uploaded when it does"
                          " (`cairn status` lists it, `cairn sync` sends it)", flush=True)
                else:
                    state = pub.store.resolve(refs.exact(pub.project, pub.name, number))[2]
                    print(f"published {pub.name}@v{number} ({describe(state)})", flush=True)
            return code
        except (StoreError, OSError) as error:
            if "can no longer change" in str(error):
                return code
            print(f"warning: final publish failed ({error}); retrying", file=sys.stderr, flush=True)
            time.sleep(min(2 ** attempt, 10))
    print(f"error: {pub.name}@v{pub.version} was left running; publish the folder again", file=sys.stderr)
    return code or 1
