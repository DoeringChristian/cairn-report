"""`cairn-report publish`: store folders as artifact versions, or follow a running job's folder.

Without `--watch` or a command a folder becomes one version (and none when nothing changed). With
either, one running version is started and the folder is synced into it while the job runs, the
way `push --watch` follows a project: every `interval` seconds, and shortly after files stop
changing (a burst of writes goes out as one sync). A store that errors only warns and backs off;
the final sync must succeed. The version completes when the command exits 0, or when watching is
stopped without a command, and fails otherwise, so `latest` never moves to a broken run.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import store as artifacts
from .config import Config
from .push import scan
from .store import Publication, StoreError

SETTLE = 2.0  # seconds without new changes before a changed folder is synced


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
    store = artifacts.open_store(config)
    try:
        before = max((v["version"] for v in artifacts.versions(name, store=store)), default=0)
    except StoreError:
        before = 0
    number = artifacts.publish(name, folder, metadata=meta, aliases=aliases, label=label, store=store)
    target = artifacts.refs.parse(name)
    if number <= before:
        print(f"{target.name}@v{number} unchanged", flush=True)
    else:
        state = store.resolve(artifacts.refs.exact(target.project or store.project, target.name, number))[2]
        print(f"published {target.name}@v{number} ({describe(state)})", flush=True)


def publish_mappings(config: Config, label: str | None, aliases: list[str], meta: dict) -> None:
    mappings = config.store.get("publish", [])
    if not mappings:
        raise SystemExit("nothing to publish: give FOLDER NAME or add [store] publish = [{ folder = ..., name = ... }] to cairn-report.toml")
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
    pub = Publication(name, folder, metadata=meta, aliases=aliases, label=label, store=artifacts.open_store(config))
    pub.start()
    print(f"publishing {folder} as {pub.name}@v{pub.version} (running) every {interval:g} s", flush=True)
    process = subprocess.Popen(command) if command else None
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
                state = pub.store.resolve(artifacts.refs.exact(pub.project, pub.name, number))[2]
                print(f"published {pub.name}@v{number} ({describe(state)})", flush=True)
            return code
        except (StoreError, OSError) as error:
            if "can no longer change" in str(error):
                return code
            print(f"warning: final publish failed ({error}); retrying", file=sys.stderr, flush=True)
            time.sleep(min(2 ** attempt, 10))
    print(f"error: {pub.name}@v{pub.version} was left running; publish the folder again", file=sys.stderr)
    return code or 1
