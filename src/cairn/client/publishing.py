"""Publishing: store a folder as an artifact version (`publish`), follow a running job's folder (`Publication`,
`cairn publish --watch`).

Without `--watch` or a command a folder becomes one version (and none when nothing changed). With
either, one running version is started and the folder is synced into it while the job runs, the
way `push --watch` follows a project: every `interval` seconds, and shortly after files stop
changing (a burst of writes goes out as one sync). A store that errors only warns and backs off;
the final sync must succeed. The version completes when the command exits 0, or when watching is
stopped without a command, and fails otherwise, so `latest` never moves to a broken run.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from ..config import Config, load_config
from ..store import INPUTS_ENV, INPUTS_FILE, LocalStore, StoreError, read_inputs, refs
from .http import HashCache, Limited, entries_for, scan
from .open import as_store, open_store, versions_of
from .remote import RemoteStore

SETTLE = 2.0  # seconds without new changes before a changed folder is synced


def _target(name: str, store: LocalStore | RemoteStore) -> tuple[str, str]:
    """(project, artifact name) for a publish target, creating the project."""
    target = refs.parse(name)
    if target.selector or target.path:
        raise ValueError(f"publish takes an artifact name, not {name!r}")
    project = target.project or store.project
    if project is None:
        raise StoreError(f"{name!r} has no project and the store has no default")
    store.create_project(project)
    return project, target.name


def _stage(store: LocalStore | RemoteStore, local: Path, config: Config, cache: HashCache) -> list[dict]:
    """Entries for a folder's files (its inputs record excluded) with their blobs put in the store."""
    files = scan(local, "", config, exclude=config.store.get("exclude", []))
    files.pop(INPUTS_FILE, None)
    entries, sources = entries_for(files, cache)
    cache.save()
    if isinstance(store, RemoteStore):  # asks the server what it lacks, then uploads in parallel
        store.upload(sources, cache)
        cache.save()
    else:
        for sha in store.missing(entries):
            file, size = sources[sha]
            with file.open("rb") as handle:
                store.put_blob(sha, Limited(handle, size), size)
    return entries


def publish(name: str, folder: Path | str, *, metadata: dict | None = None, aliases=(), label: str | None = None,
            inputs=None, store: LocalStore | RemoteStore | Path | str | None = None, config: Config | None = None) -> int:
    """Store a folder as the next version of artifact `name`; returns the version number.

    A symlinked folder is followed (the label defaults to the resolved folder's name). When nothing
    changed since the latest version, no version is created and its number is returned.
    """
    local = Path(folder).resolve()
    if not local.is_dir():
        raise ValueError(f"{folder} is not a folder")
    store = as_store(store)
    project, artifact = _target(name, store)
    config = config or load_config()
    entries = _stage(store, local, config, HashCache(config.project))
    merged = [*read_inputs(local), *(str(item) for item in inputs or [])]
    return store.publish(project, artifact, entries, label=label or local.name, metadata=metadata, inputs=merged,
                         aliases=aliases, source={"host": socket.gethostname(), "folder": str(local)})


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
                 inputs=None, store: LocalStore | RemoteStore | Path | str | None = None):
        self.local = Path(folder).resolve()
        self.store = as_store(store)
        self.project, self.name = _target(name, self.store)
        self.config = load_config()
        self.cache = HashCache(self.config.project)
        self.label, self.metadata, self.aliases, self.inputs = label or self.local.name, metadata, tuple(aliases), inputs
        self.version: int | None = None
        self._sent: dict[str, tuple] = {}

    def start(self) -> int:
        if self.version is None:
            self.version = self.store.start_version(self.project, self.name, self.label, self.metadata,
                                                    {"host": socket.gethostname(), "folder": str(self.local)})
        return self.version

    def sync(self) -> int:
        """Add the files that are new or changed since the last sync; returns how many."""
        self.start()
        entries = _stage(self.store, self.local, self.config, self.cache)
        fresh = [entry for entry in entries if self._sent.get(entry["path"]) != (entry.get("sha256"), entry.get("link"))]
        if fresh:
            self.store.add_files(self.project, self.name, self.version, fresh)
            self._sent.update({entry["path"]: (entry.get("sha256"), entry.get("link")) for entry in fresh})
        return len(fresh)

    def complete(self) -> int:
        """Sync a last time, freeze the version, move `latest`, and apply the aliases."""
        self.sync()
        merged = [*read_inputs(self.local), *(str(item) for item in self.inputs or [])]
        self.store.complete_version(self.project, self.name, self.version, merged)
        for alias in self.aliases:
            self.store.set_alias(self.project, self.name, alias, self.version)
        return self.version

    def fail(self) -> None:
        if self.version is not None:
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


def publish_folder(config: Config, folder: Path, name: str, label: str | None, aliases: list[str], meta: dict) -> None:
    """Publish one folder and print what happened."""
    store = open_store(config)
    try:
        before = max((v["version"] for v in versions_of(store, name)), default=0)
    except StoreError:
        before = 0
    number = publish(name, folder, metadata=meta, aliases=aliases, label=label, store=store)
    target = refs.parse(name)
    if number <= before:
        print(f"{target.name}@v{number} unchanged", flush=True)
    else:
        state = store.resolve(refs.exact(target.project or store.project, target.name, number))[2]
        print(f"published {target.name}@v{number} ({describe(state)})", flush=True)


def publish_mappings(config: Config, label: str | None, aliases: list[str], meta: dict) -> None:
    mappings = config.store.get("publish", [])
    if not mappings:
        raise SystemExit("nothing to publish: give FOLDER NAME or add [store] publish = [{ folder = ..., name = ... }] to cairn.toml")
    for mapping in mappings:
        folder = config.project / mapping["folder"]
        if not folder.is_dir():
            print(f"skipping {mapping['name']}: {mapping['folder']} does not exist", flush=True)
            continue
        publish_folder(config, folder, mapping["name"], label, aliases, meta)


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
    pub.start()
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
                print(f"synced {added} files to {pub.name}@v{pub.version}", flush=True)
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
                print(f"{pub.name}@v{pub.version} failed (command exited {code}); latest did not move", file=sys.stderr, flush=True)
            else:
                number = pub.complete()
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
