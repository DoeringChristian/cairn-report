"""Drafts: a folder managed by Cairn that becomes an artifact's next version when it closes.

    with cairn.draft("descent") as run:          # a fresh, empty folder: run.path
        x = cairn.pull("datasets/quadratic@latest")   # recorded as an input of the draft
        run.scalar("loss", 0.1, step=0)          # a draft is a run logger on its folder (client/logger.py)
        (run.path / "notes.txt").write_text("...")
    run.version                                  # descent@v<n> (a pending Version while the server is down)

A draft lives at `<base>/drafts/<name>/<uid>/` (the project's `.cairn/`, git-ignored; outside a project the
data root's `drafts/`), with its record beside it, `<uid>.draft.json` (name, project, label, metadata,
aliases, owner, state). While it is open, pulls made in the process record their exact versions in the
draft's own `.cairn-inputs.jsonl` (`$CAIRN_INPUTS` points there and is restored when the draft closes; nested
drafts restore in order). Leaving the block publishes the folder exactly like `cairn.publish(name, folder)`
(inputs, the unchanged check, digest, aliases; the label defaults to the draft's start time) and removes it:
its files now live in the store (blobs are cloned, or hard-linked where the filesystem cannot clone, never
copied when avoidable), the replica, or the cache. An exception keeps the folder (state `kept`, printed on
stderr) and marks a running version failed; `cairn drafts` lists kept and open drafts, `cairn drafts publish
<uid>` publishes one later, `cairn drafts discard <uid>` removes it.

Several drafts may be open at once (a model and its evaluation): a pull is recorded in every open draft, and
a pull made while none is open is carried into the drafts opened next (store/inputs.py). `run.use(other)`
declares another draft, a Version, or a reference as an input; when both drafts are published in this process
the other goes first (a draft closed before its input waits for it) and the input names its exact version.
A draft's version gets the draft's uid. `config=` (and, for a Hydra run dir, the resolved job config and its
overrides) becomes `metadata["config"]` (`metadata["overrides"]`).

`watch=True` starts a running version at once and keeps it synced while the block runs (a `Publication`,
like `cairn publish --watch`: every `[store] interval` seconds when something changed, a heartbeat on a store
server, `sync()` by hand); closing completes it, an exception fails it.

Hydra points `hydra.run.dir` at `${cairn.draft:<name>}` (cairn/runs/hydra.py); `current_draft()` returns the
draft the process writes into (the innermost open one, else the one `$CAIRN_INPUTS` or the working directory
lies in), as a view on a trial's subfolder in a multirun.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import cairn_base, find_project, protect
from ..store import INPUTS_ENV, INPUTS_FILE, StoreError, refs
from ..store.inputs import close_sink, open_sink
from ..store.cache import data_root, read_json, write_json
from ..store.uids import UID, new_uid
from .logger import RunLogger

DRAFTS = "drafts"
SUFFIX = ".draft.json"
_UNSET = object()
_ACTIVE: list["Draft"] = []  # the drafts open in this process, innermost last
_PUBLISHED: dict[str, str] = {}  # draft uid -> the exact ref it was published as, in this process


def drafts_root() -> Path:
    """`<base>/drafts` (the project's `.cairn/`), else `<data root>/drafts` outside a project."""
    base = cairn_base()
    if base is not None:
        protect(base)
        return base / DRAFTS
    return data_root() / DRAFTS


def _record_path(folder: Path) -> Path:
    return folder.parent / f"{folder.name}{SUFFIX}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True
    return True


class Draft(RunLogger):
    """A fresh folder (`path`) that becomes the next version of artifact `name` when it closes.

    A context manager: normal exit publishes it (`publish`), an exception keeps it (`fail`). Without `with`,
    call `publish()`, `fail()`, or `discard()` yourself. It is also a run logger on its folder (`scalar`,
    `scalars`, `table`, `json`, `figure`, `plotly`, `image`, `video`, `artifact`, `text`)."""

    def __init__(self, name: str, *, label: str | None = None, metadata: dict | None = None, aliases=(), watch: bool = False,
                 store=None, config: dict | None = None, interval: float | None = None, activate: bool = True):
        target = refs.parse(name)
        if target.selector or target.path:
            raise ValueError(f"a draft takes an artifact name, not {name!r}")
        refs.valid_name(target.name)
        if target.project:
            refs.valid_project(target.project)
        if config is not None:
            metadata = {**(metadata or {}), "config": config}
        uid = new_uid()
        folder = drafts_root() / target.name / uid
        folder.mkdir(parents=True)
        record = {"uid": uid, "project": target.project, "name": target.name,
                  "label": label or datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
                  "metadata": metadata, "aliases": list(aliases), "watch": bool(watch), "uses": [],
                  "created": datetime.now(timezone.utc).isoformat(), "owner": {"host": socket.gethostname(), "pid": os.getpid()},
                  "state": "open"}
        write_json(_record_path(folder), record)
        self._setup(folder, folder, record, store, interval)
        if activate:
            self.activate()

    @classmethod
    def _at(cls, folder: Path, path: Path, record: dict, store=None) -> "Draft":
        draft = cls.__new__(cls)
        draft._setup(folder, path, record, store, None)
        return draft

    def _setup(self, folder: Path, path: Path, record: dict, store, interval: float | None) -> None:
        RunLogger.__init__(self, path)
        self.project = find_project()  # origins are relative to the project the job runs in
        self.folder, self.record = folder, record
        self.uid, self.name = record["uid"], record["name"]
        self.trial = path != folder  # a view on a subfolder (a multirun trial): it never publishes itself
        self.version = None
        self._store_arg, self._store = store, None
        self._interval = interval
        self._previous = _UNSET
        self._pub = None
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._watcher: threading.Thread | None = None
        self._closed = False
        self._waiting: list["Draft"] = []  # drafts this one uses that were still open when it was published
        self._dependents: list["Draft"] = []  # drafts waiting for this one to be published first
        self._uses: list["Draft"] = []  # the open drafts `use` named, in this process

    def __repr__(self) -> str:
        return f"<cairn.Draft {self.name} {self.uid} {self.path}>"

    # ------------------------------------------------------------ properties

    @property
    def path(self) -> Path:
        """The folder this draft writes into (a trial's subfolder for a view)."""
        return self.root

    @property
    def label(self) -> str:
        return self.record.get("label") or self.uid

    @property
    def watch(self) -> bool:
        return bool(self.record.get("watch"))

    @property
    def state(self) -> str:
        """`open` (its owner may still write), `abandoned` (open, its process on this host is gone), or `kept`."""
        state = self.record.get("state", "open")
        owner = self.record.get("owner") or {}
        if state == "open" and owner.get("host") == socket.gethostname() and owner.get("pid") and not _pid_alive(owner["pid"]):
            return "abandoned"
        return state

    @property
    def created(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.record["created"])
        except (KeyError, ValueError):
            return None

    @property
    def size(self) -> int:
        total = 0
        for directory, _, files in os.walk(self.folder):
            for file in files:
                try:
                    total += os.lstat(os.path.join(directory, file)).st_size
                except OSError:
                    pass
        return total

    @property
    def store(self):
        """The object API's Store this draft publishes to (opened on first use; None: the configured store)."""
        if self._store is None:
            from .objects import Store
            from .open import as_store

            arg = self._store_arg
            self._store = arg if isinstance(arg, Store) else Store(as_store(arg))
        return self._store

    @property
    def target(self):
        """The Artifact this draft becomes a version of."""
        return self.store.project(self.record.get("project")).artifact(self.name)

    # ------------------------------------------------------------ activation

    def activate(self) -> "Draft":
        """Record this process's pulls in the draft (`$CAIRN_INPUTS`) and, with `watch`, start the running version."""
        if self._previous is not _UNSET:
            return self
        self._previous = os.environ.get(INPUTS_ENV)
        os.environ[INPUTS_ENV] = str(self.path / INPUTS_FILE)
        _ACTIVE.append(self)
        open_sink(self.path / INPUTS_FILE)
        if self.watch and not self.trial and self._pub is None:
            self._start_watch()
        return self

    def deactivate(self) -> None:
        """Give `$CAIRN_INPUTS` back (to the enclosing draft, or what it was before)."""
        if self._previous is _UNSET:
            return
        previous, self._previous = self._previous, _UNSET
        close_sink(self.path / INPUTS_FILE)
        index = next((i for i, item in enumerate(_ACTIVE) if item is self), None)
        if index is not None:
            del _ACTIVE[index]
            if index < len(_ACTIVE):  # a draft opened inside this one is still open: it restores to what we had
                _ACTIVE[index]._previous = previous
                return
        if previous is None:
            os.environ.pop(INPUTS_ENV, None)
        else:
            os.environ[INPUTS_ENV] = previous

    def __enter__(self) -> "Draft":
        return self.activate()

    def __exit__(self, kind, error, trace) -> bool:
        if kind is None:
            self.publish()
        else:
            self.fail(error)
        return False

    # ------------------------------------------------------------ inputs and config

    def use(self, other) -> str:
        """Declare an input explicitly: another draft (open or published; when both are published in this process,
        the other one goes first and this one names its exact version), a `Version`, or a reference. Returns the
        reference recorded (a draft's is `project:name@<uid>` until it is published)."""
        if isinstance(other, Draft):
            if other is self or other.folder == self.folder:
                raise ValueError("a draft cannot use itself")
            target = other.target
            ref = f"{target.project.name}:{target.name}@{other.uid}"
            if not other._closed:
                self._uses.append(other)
            entry = {"draft": other.uid, "ref": ref}
        else:
            ref = getattr(other, "ref", None) or str(other)
            refs.parse(ref)
            entry = {"ref": ref}
        if entry not in self.record.setdefault("uses", []):
            self.record["uses"].append(entry)
            self._save()
        return ref

    def _inputs(self) -> list[str]:
        """The explicit inputs (`use`), with each draft published in this process by its exact version."""
        found = []
        for entry in self.record.get("uses") or []:
            ref = _PUBLISHED.get(entry.get("draft"), entry["ref"])
            if ref not in found:
                found.append(ref)
        return found

    @property
    def config(self) -> dict | None:
        """The run's configuration (`metadata["config"]`; Hydra drafts get the resolved job config)."""
        return (self.record.get("metadata") or {}).get("config")

    def set_metadata(self, **values) -> None:
        """Add metadata (for example `config=`, `overrides=`) before the draft is published."""
        self.record["metadata"] = {**(self.record.get("metadata") or {}), **values}
        self._save()

    # ------------------------------------------------------------ watching

    def _start_watch(self) -> None:
        from .publishing import Publication

        target = self.target
        self._pub = Publication(f"{target.project.name}:{target.name}", self.folder, metadata=self.record.get("metadata"),
                                aliases=self.record.get("aliases") or (), label=self.label, store=self.store.backend,
                                uid=self.uid)
        self._pub.start()
        self.version = target.version(self._pub.uid)
        self.record["running"] = {"uid": self._pub.uid, "number": self._pub.version}
        self._save()
        interval = float(self._interval if self._interval is not None else self._pub.config.store.get("interval", 30))
        self._stop = threading.Event()
        self._watcher = threading.Thread(target=self._follow, args=(self._stop, interval), name=f"cairn-draft {self.uid}",
                                         daemon=True)
        self._watcher.start()

    def _follow(self, stop: threading.Event, interval: float) -> None:
        from .publishing import signature

        seen = None
        while not stop.wait(interval):
            try:
                current = signature(self.folder, self._pub.config)
                if current != seen:
                    self.sync()
                    seen = current
            except (StoreError, OSError) as error:
                print(f"warning: syncing the draft {self.folder} failed ({error}); retrying", file=sys.stderr, flush=True)

    def _stop_watch(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._watcher.join(timeout=60)
            self._stop = self._watcher = None

    def sync(self) -> int:
        """Add new and changed files to the running version now (watch mode; 0 otherwise); returns how many."""
        if self._pub is None:
            return 0
        with self._lock:
            return self._pub.sync()

    # ------------------------------------------------------------ closing

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreError(f"the draft {self.uid} was already published or discarded")
        if self.trial:
            raise StoreError(f"{self.path} is a trial of the draft {self.folder}: the whole draft is published (the study"
                             " completes), not one trial")

    def _save(self) -> None:
        write_json(_record_path(self.folder), self.record)

    def publish(self, *, allow_incomplete: bool = False):
        """Publish the folder as the next version (like `cairn.publish(name, path)`), remove the draft, and return the
        `Version` (also `self.version`; pending while the store server does not answer). If publishing fails, the
        folder is kept (`fail`) and the error raised. A folder whose `run.json` is not `completed` is refused unless
        `allow_incomplete`."""
        self._ensure_open()
        self.deactivate()
        waiting = [other for other in self._uses if not other._closed]
        if waiting:  # its inputs go first: published right after the last of them
            self._waiting = waiting
            for other in waiting:
                if self not in other._dependents:
                    other._dependents.append(self)
            return None
        self._stop_watch()
        try:
            if self._pub is not None:
                with self._lock:
                    self._pub.inputs = self._inputs()
                    self._pub.complete()
                version = self.target.version(self._pub.uid)
                version.manifest  # noqa: B018  (load it now)
            else:
                record = self.record
                # the version takes the draft's uid, unless a running version (watch) or an earlier attempt took it
                uid = None if record.get("running") or record.get("attempted") else self.uid
                record["attempted"] = True
                self._save()
                version = self.target.publish(self.folder, metadata=record.get("metadata"), aliases=record.get("aliases") or (),
                                              label=self.label, inputs=self._inputs(), allow_incomplete=allow_incomplete,
                                              _link=True, _uid=uid)
        except BaseException as error:
            self._keep(error)
            self._release(error)
            raise
        self.version = version
        _PUBLISHED[self.uid] = version.ref
        self._remove()
        self._release(None)
        return version

    def _release(self, failed) -> None:
        """This draft is published (`failed` None) or will not be: publish (or keep) the drafts that waited for it."""
        dependents, self._dependents = self._dependents, []
        for other in dependents:
            if other._closed or self not in other._waiting:
                continue
            other._waiting.remove(self)
            if failed is not None:
                other._waiting = []
                reason = f"its input {self.name}@{self.uid} was not published ({failed})"
                other._keep(reason)
                other._release(reason)
            elif not other._waiting:
                other.publish()

    def fail(self, error: BaseException | str | None = None) -> None:
        """Keep the folder for inspection (state `kept`, printed on stderr) and mark a running version failed."""
        if self.trial:
            self.deactivate()
            return
        self._ensure_open()
        self._stop_watch()
        self.deactivate()
        self._keep(error)
        self._release(error or "failed")

    def _keep(self, error) -> None:
        if self._pub is not None:
            try:
                with self._lock:
                    try:
                        self._pub.sync()  # what the failed run wrote, for inspection
                    finally:
                        self._pub.fail()
            except (StoreError, OSError) as failure:
                print(f"warning: could not mark {self._pub.ref} failed ({failure})", file=sys.stderr, flush=True)
            self._pub = None
        _unshare(self.folder)
        reason = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error
        self.record.update(state="kept", error=reason, ended=datetime.now(timezone.utc).isoformat())
        self._save()
        print(f"cairn: kept the draft {self.folder}{f' ({reason})' if reason else ''}; `cairn drafts publish {self.uid}`"
              f" publishes it, `cairn drafts discard {self.uid}` removes it", file=sys.stderr, flush=True)

    def discard(self) -> None:
        """Remove the folder without publishing it (a running version is marked failed)."""
        self._ensure_open()
        self._stop_watch()
        self.deactivate()
        if self._pub is not None:
            with self._lock:
                self._pub.fail()
            self._pub = None
        elif (self.record.get("running") or {}).get("number") is not None:
            self._fail_running()
        self._remove()
        self._release("discarded")

    def _fail_running(self) -> None:
        """A kept or abandoned watch draft's running version on a local store: fail it (a server fails it by heartbeat)."""
        from ..store import LocalStore

        running = self.record["running"]
        backend = self.store.backend
        if isinstance(backend, LocalStore):
            target = self.target
            try:
                if backend.get_version(target.project.name, target.name, running["number"])["status"] == "running":
                    backend.fail_version(target.project.name, target.name, running["number"])
            except StoreError:
                pass

    def _remove(self) -> None:
        self._closed = True
        shutil.rmtree(self.folder, ignore_errors=True)
        _record_path(self.folder).unlink(missing_ok=True)
        root = drafts_root().resolve()
        parent = self.folder.parent.resolve()
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _unshare(folder: Path) -> None:
    """Give hard-linked files (shared with a blob by a publish that failed later) their own writable copy."""
    for directory, _, files in os.walk(folder):
        for file in files:
            path = Path(directory) / file
            try:
                info = path.lstat()
                if path.is_symlink() or info.st_nlink < 2:
                    continue
                temporary = path.with_name(f".{file}.unshare-{os.getpid()}")
                shutil.copyfile(path, temporary)
                os.chmod(temporary, 0o644)
                os.replace(temporary, path)
            except OSError:
                pass


def draft(name: str, *, label: str | None = None, metadata: dict | None = None, aliases=(), watch: bool = False,
          store=None, config: dict | None = None) -> Draft:
    """A fresh, empty folder managed by Cairn (`.path`) that becomes the next version of `name` when it closes.

        with cairn.draft("descent") as run:
            run.scalar("loss", 0.1)            # or write files into run.path
        run.version                            # the published Version

    `label` defaults to the start time (`YYYY-MM-DD_HH-MM-SS`); `watch=True` keeps a running version synced while
    the block runs. `store` as for `cairn.publish` (None: the configured store). `config` (a plain dict) becomes
    `metadata["config"]`. The version's uid is the draft's uid."""
    return Draft(name, label=label, metadata=metadata, aliases=aliases, watch=watch, store=store, config=config)


def find(path: Path | str, store=None) -> Draft | None:
    """The draft whose folder is `path` or holds it (a view on the subfolder when it is deeper), else None."""
    path = Path(path).resolve()
    for folder in [path, *path.parents]:
        if UID.fullmatch(folder.name):
            record = read_json(_record_path(folder))
            if isinstance(record, dict) and record.get("uid") == folder.name:
                return Draft._at(folder, path, record, store)
    return None


def drafts(root: Path | None = None) -> list[Draft]:
    """Every draft under the drafts root (open, abandoned, or kept), oldest first."""
    root = Path(root) if root else drafts_root()
    found = []
    if not root.is_dir():
        return found
    for directory, folders, files in os.walk(root):
        keep = []
        for name in folders:
            if UID.fullmatch(name) and f"{name}{SUFFIX}" in files:
                record = read_json(Path(directory) / f"{name}{SUFFIX}")
                if isinstance(record, dict) and record.get("uid") == name:
                    folder = Path(directory) / name
                    found.append(Draft._at(folder, folder, record))
                    continue  # never descend into a draft's own files
            keep.append(name)
        folders[:] = keep
    return sorted(found, key=lambda item: item.uid)


def load(where: Path | str, store=None) -> Draft:
    """A draft by its folder (or a path inside it) or by its uid (or a unique start of it)."""
    text = str(where)
    if not Path(text).exists():
        matches = [item for item in drafts() if item.uid.startswith(text)]
        if len(matches) == 1:
            matches[0]._store_arg = store
            return matches[0]
        raise StoreError(f"no draft {text!r}" if not matches else f"{text!r} names {len(matches)} drafts: give more of the uid")
    found = find(text, store)
    if found is None:
        raise StoreError(f"{text} is not inside a draft folder (`cairn drafts` lists them)")
    return found


def current_draft() -> Draft | None:
    """The draft this process writes into: the innermost open one, else the one `$CAIRN_INPUTS` or the working
    directory lies in (a Hydra job whose run dir is `${cairn.draft:...}`, or a subprocess of one), else None."""
    if _ACTIVE:
        return _ACTIVE[-1]
    if os.environ.get(INPUTS_ENV):
        found = find(Path(os.environ[INPUTS_ENV]).parent)
        if found is not None:
            return found
    return find(Path.cwd())


def age(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.0f}{unit}"
    return f"{seconds:.0f}s"


def describe(item: Draft) -> str:
    created = item.created
    elapsed = age(time.time() - created.timestamp()) if created else "?"
    project = item.record.get("project")
    name = f"{project}:{item.name}" if project else item.name
    return f"{name}\t{item.uid}\t{item.state}\t{elapsed}\t{item.size / 1e6:.1f} MB\t{item.folder}"
