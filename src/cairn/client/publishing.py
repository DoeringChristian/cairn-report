"""Publishing: store a folder as an artifact version (`publish`), follow a running job's folder (`Publication`,
`cairn publish --watch`).

Without `--watch` or a command a folder becomes one version (and none when nothing changed). With
either, one running version is started and the folder is synced into it while the job runs: every
`interval` seconds, and shortly after files stop changing (a burst of writes goes out as one sync). A
repo that errors only warns and backs off; the final sync must succeed. The version completes when the
command exits 0, or when watching is stopped without a command, and fails otherwise, so `latest` never
moves to a broken run.

Without a remote both write the repo directly: a running version is a row of its database, with a heartbeat
(`<repo>/heartbeats/<uid>`); opening the repo fails it once its process is dead or the heartbeat is old
(LocalRepo.reap), so a killed job never stays `running`. With a remote both go through the outbox (outbox.py,
sync.py): a running version is an outbox entry first (sync `running`) and is created on the remote (under its uid)
as soon as the remote answers, so a job starts and finishes while the remote is down; its completion or failure
turns it pending and it is sent like any publish. While it runs, a heartbeat thread rewrites its outbox
`heartbeat` (time, host, pid) every few seconds; a sender on any host fails the version once the heartbeat is
older than `[repo] heartbeat_timeout`.
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

from ..api import public, surface
from ..config import Config, load_config
from ..store import INPUTS_ENV, INPUTS_FILE, MissingBlobs, StoreError, read_inputs, refs
from ..term import files as files_text, hint, note, plural, short_path, warn
from .http import scan
from .open import open_repo

SETTLE = 2.0  # seconds without new changes before a changed folder is synced


def _target(name: str) -> str:
    """The artifact name of a publish target."""
    target = refs.parse(name)
    if target.selector or target.path:
        raise refs.InvalidRef(f"publish takes an artifact name, not {name!r}")
    return target.name


def publish_version(
    repo,
    name: str,
    folder: Path | str,
    *,
    metadata: dict | None = None,
    aliases=(),
    label: str | None = None,
    inputs=None,
    config: Config | None = None,
    allow_incomplete: bool = False,
):
    """`Artifact.publish` in the repo `repo` (a client/repo.py ProjectRepo): the Version."""
    from .objects import Repo

    return (
        Repo(repo)
        .artifact(_target(name))
        .publish(
            folder,
            metadata=metadata,
            aliases=aliases,
            label=label,
            inputs=inputs,
            config=config,
            allow_incomplete=allow_incomplete,
        )
    )


@public("cairn.publish")
def publish(
    name: str,
    folder: Path | str,
    *,
    metadata: dict | None = None,
    aliases=(),
    label: str | None = None,
    inputs=None,
    config: Config | None = None,
    allow_incomplete: bool = False,
) -> int | None:
    """Store a folder as the next version of artifact `name` in the project's repo; returns the version number
    (`Artifact.publish`).

    A symlinked folder is followed (the label defaults to the resolved folder's name). A folder whose
    `run.json` is not `completed` is refused unless `allow_incomplete` (then `metadata.run_status` records it). When nothing
    changed since the latest version, no version is created and its number is returned. None means the
    remote did not answer: the version is pending in the repo's outbox (`cairn status`), usable here
    by its uid, and sent later.
    """
    repo = open_repo(config)
    repo.resume()
    return publish_version(
        repo,
        name,
        folder,
        metadata=metadata,
        aliases=aliases,
        label=label,
        inputs=inputs,
        config=config,
        allow_incomplete=allow_incomplete,
    ).number


@surface
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

    def __init__(
        self,
        name: str,
        folder: Path | str,
        *,
        metadata: dict | None = None,
        aliases=(),
        label: str | None = None,
        inputs=None,
        uid: str | None = None,
        _repo=None,
    ):
        from .open import heartbeat_timeout

        self.local = Path(folder).resolve()
        self.store = _repo if _repo is not None else open_repo()  # the project's repo (a ProjectRepo)
        self.name = _target(name)
        self.config = load_config()
        self.exclude = self.config.repo.get("exclude", [])
        self.label, self.metadata, self.aliases, self.inputs = (
            label or self.local.name,
            metadata,
            tuple(aliases),
            inputs,
        )
        self.version: int | None = None  # the number; None while the source of truth has not numbered the version
        self.uid: str | None = uid  # the version's uid (default: a new one, made when it starts)
        self.queued = False  # pending: the remote has not taken it yet
        self.started = False
        self._sent: dict[str, tuple] = {}
        self._beating: threading.Event | None = None
        self.timeout = heartbeat_timeout(self.config)

    @property
    def source(self) -> dict:
        return {"host": socket.gethostname(), "folder": str(self.local)}

    @property
    def ref(self) -> str:
        """`name@v<n>`, or `name@<uid>` while it has no number yet."""
        return refs.exact(self.name, self.version) if self.version is not None else f"{self.name}@{self.uid}"

    def _apply(self, manifest: dict) -> None:
        self.version, self.uid = manifest.get("version"), manifest["uid"]
        self.queued = bool(manifest.get("pending"))

    def _start_beating(self) -> None:
        """A daemon thread that rewrites the heartbeat while the version runs (it stops with the process)."""
        stop = self._beating = threading.Event()
        every = max(0.05, min(60.0, self.timeout / 5))
        uid, beat = self.uid, self.store.beat

        def loop():
            while not stop.wait(every):
                beat(uid)

        threading.Thread(target=loop, name=f"cairn-heartbeat {uid}", daemon=True).start()

    def _stop_beating(self) -> None:
        if self._beating is not None:
            self._beating.set()
            self._beating = None

    def start(self) -> int | None:
        """Start the running version (in the repo first; with a remote it is created there too as soon as it answers)."""
        if not self.started:
            from ..store.uids import new_uid

            self.uid = self.uid or new_uid()
            manifest = self.store.start_version(self.name, self.label, self.metadata, self.source, uid=self.uid)
            self.started = True
            self._apply(manifest)
            self._start_beating()
        return self.version

    def sync(self) -> int:
        """Add the files that are new or changed since the last sync; returns how many."""
        self.start()
        for attempt in range(3):
            entries, sources = self.store.stage(self.local, self.exclude, self.config.project)
            fresh = [
                entry for entry in entries if self._sent.get(entry["path"]) != (entry.get("sha256"), entry.get("link"))
            ]
            try:
                self.store.add_files(self.name, self.uid, fresh)
                break
            except (
                MissingBlobs
            ):  # garbage collection removed a staged blob meanwhile: stage again (the version is fine)
                if attempt == 2:
                    raise
            except StoreError:
                self._stop_beating()  # failed meanwhile (its heartbeat expired, or the repo failed it): this job lost it
                raise
        self.store.restage(entries, sources)
        self._sent.update({entry["path"]: (entry.get("sha256"), entry.get("link")) for entry in fresh})
        self._apply(self.store.get_version(self.name, self.uid))
        return len(fresh)

    def set_metadata(self, metadata: dict) -> None:
        """Replace the metadata of the running version (not once it completed or failed)."""
        self.metadata = metadata
        if self.started:
            self.store.set_metadata(self.name, self.uid, metadata)

    def complete(self) -> int | None:
        """Sync a last time, freeze the version, move `latest`, and apply the aliases. While the remote does not answer,
        the completed version is pending (`queued` is True) and the number is None until it is sent."""
        self.sync()
        merged = [*read_inputs(self.local), *(str(item) for item in self.inputs or [])]
        self._stop_beating()
        self._apply(self.store.complete_version(self.name, self.uid, merged, self.aliases))
        return self.version

    def fail(self) -> None:
        if self.started:
            self._stop_beating()
            self.store.fail_version(self.name, self.uid)

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
            raise SystemExit(f"cairn: --meta takes KEY=VALUE, got {item!r}")
        try:
            meta[key] = json.loads(value)
        except ValueError:
            meta[key] = value
    return meta


def describe(version: dict) -> str:
    return files_text(version["files"])


def published(name: str, version: dict, before: int = 0, why: str | None = None) -> str:
    """The line a publish prints: `published descent@v3 (3f9a2c1, 2 files, 12 KB)`, `unchanged descent@v3 (3f9a2c1)`,
    or `pending descent (3f9a2c1): remote not reachable` while the remote does not answer."""
    short = refs.short_of(version)
    if version.get("version") is None:
        return f"pending {name} ({short}): {why or 'remote not reachable'}\n" + hint("run `cairn sync` to send it now")
    if version["version"] <= before:
        return f"unchanged {name}@v{version['version']} ({short})"
    return f"published {name}@v{version['version']} ({short}, {describe(version)})"


def publish_folder(
    config: Config,
    folder: Path,
    name: str,
    label: str | None,
    aliases: list[str],
    meta: dict,
    allow_incomplete: bool = False,
) -> None:
    """Publish one folder and print what happened."""
    store = open_repo(config)
    store.quiet_pending = True  # the line below says why
    target = _target(name)
    try:
        before = max((v["version"] or 0 for v in store.versions(target)), default=0)
    except StoreError:  # also a remote that does not answer: the version stays pending below
        before = 0
    version = publish_version(
        store,
        target,
        folder,
        metadata=meta,
        aliases=aliases,
        label=label,
        config=config,
        allow_incomplete=allow_incomplete,
    )
    why = getattr(store, "pending_reasons", {}).get(version.manifest.get("uid"))
    print(published(target, version.manifest, before, why), flush=True)


def signature(folder: Path, config: Config) -> dict:
    """Path -> (size, mtime) of the folder's files: a cheap way to see that something changed."""
    files = scan(folder, "", config.project, config.repo.get("exclude", []))
    return {path: (info.get("size"), info.get("mtime_ns"), info.get("link")) for path, info in files.items()}


def watch_folder(
    config: Config,
    folder: Path,
    name: str,
    label: str | None,
    aliases: list[str],
    meta: dict,
    command: list[str] | None = None,
    interval: float | None = None,
) -> int:
    """Follow `folder` into one running version of `name` while `command` runs; returns the exit code."""
    interval = float(interval if interval is not None else config.repo.get("interval", 30))
    settle, tick = min(SETTLE, interval / 2), max(0.05, min(1.0, interval / 4))
    stopped: list[int] = []
    previous = {
        signum: signal.signal(signum, lambda number, _frame: stopped.append(number))
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        return _follow(config, folder, name, label, aliases, meta, command, interval, settle, tick, stopped)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _follow(config, folder, name, label, aliases, meta, command, interval, settle, tick, stopped) -> int:
    pub = Publication(name, folder, metadata=meta, aliases=aliases, label=label, _repo=open_repo(config))
    for attempt in range(5):  # a repo that errors; a remote that does not answer is no reason to wait
        try:
            pub.start()
            break
        except (StoreError, OSError) as error:
            if attempt == 4:
                raise
            warn(f"could not start the version ({error}); retrying", once=False)
            time.sleep(1)
    short = refs.default_short_id(pub.uid)
    if pub.version is None:
        print(
            f"following {short_path(folder)} as {pub.name} ({short}, running, pending) every {interval:g} s", flush=True
        )
    else:
        print(
            f"following {short_path(folder)} as {pub.name}@v{pub.version} ({short}, running) every {interval:g} s",
            flush=True,
        )
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
                where = f"{pub.name}@v{pub.version}" if pub.version is not None else f"{pub.name} ({short}, pending)"
                print(f"synced {plural(added, 'file')} to {where}", flush=True)
            return True
        except (StoreError, OSError) as error:
            failures += 1
            warn(f"sync failed ({error}); retrying", once=False)
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
        wait = min(interval * 2 ** min(failures, 4), 600) if failures else 0  # back off while the repo errors
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
    for attempt in range(5):  # the final state must reach the repo
        try:
            if code:
                pub.sync()  # keep what the failed run wrote for inspection
                pub.fail()
                note(f"failed {pub.ref} (command exited {code}); latest did not move")
            else:
                number = pub.complete()
                if number is None or pub.queued:
                    print(
                        f"pending {pub.name} ({short}, completed): remote not reachable\n"
                        + hint("run `cairn sync` to send it now"),
                        flush=True,
                    )
                else:
                    print(published(pub.name, pub.store.get_version(pub.name, pub.uid)), flush=True)
            return code
        except (StoreError, OSError) as error:
            if "can no longer change" in str(error):
                return code
            warn(f"final publish failed ({error}); retrying", once=False)
            time.sleep(min(2**attempt, 10))
    note(f"cairn: {pub.name}@v{pub.version} was left running; publish the folder again")
    return code or 1
