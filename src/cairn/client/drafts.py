"""Drafts: a folder managed by Cairn that becomes an artifact's next version when it closes.

    with cairn.draft("models/student") as model:  # a fresh, empty folder: model.path
        model.add({"val_loss": 0.1, "step": 0})   # a draft is a writer on its folder (client/logger.py)
        model.add_file("model.pt")
    model.version                                # models/student@v<n> (a pending Version while the remote is down)

A draft lives at `<repo>/drafts/<name>/<uid>/` (the project's `.cairn/`, git-ignored), with its record beside it, `<uid>.draft.json` (name, project, label, metadata,
aliases, owner, state, run). Every draft is an output of the process's run (cairn/client/runs.py): opening one
starts the run if `cairn.init()` was not called, and the draft's one input is that run's version, named by its
uid while the run is still running. What the process pulls is an input of the run, not of the draft. Leaving the
block publishes the folder exactly like `cairn.publish(name, folder)` (the unchanged check, digest, aliases; the
label defaults to the draft's start time) and removes it: its files now live in the repo's blobs (cloned, or
hard-linked where the filesystem cannot clone, never copied when avoidable). An
exception keeps the folder (state `kept`, printed on stderr) and marks a running version failed; `cairn drafts`
lists kept and open drafts, `cairn drafts publish <uid>` publishes one later, `cairn drafts discard <uid>`
removes it. A draft's version gets the draft's uid. `config=` (and, for a Hydra run dir, the resolved job config
and its overrides) becomes `metadata["config"]` (`metadata["overrides"]`).

`watch=True` starts a running version at once and keeps it synced while the block runs (a `Publication`,
like `cairn publish --watch`: every `[repo] interval` seconds when something changed, with a heartbeat,
`sync()` by hand); closing completes it, an exception fails it.

Hydra may point `hydra.run.dir` at `${cairn.draft:<name>}` (cairn/runs/hydra.py) to make the job folder an output
draft; `current_draft()` returns the draft the process writes into (the innermost open one, else the one the
working directory lies in), as a view on a trial's subfolder in a multirun.
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

from ..store import StoreError, refs
from ..store.folders import pid_alive as _pid_alive, read_json, write_json
from .http import credit_transfer, transferring
from .open import repo_root
from ..store.uids import UID, new_uid
from . import logger as _logger
from .logger import Writer

DRAFTS = "drafts"
SUFFIX = ".draft.json"
_ACTIVE: list["Draft"] = []  # the drafts open in this process, innermost last
_logger._OWN_FILES.add(Path(__file__).resolve())


def drafts_root() -> Path:
    """`<repo>/drafts` (the project's `.cairn/`; StoreError outside a project without `$CAIRN_DIR`)."""
    return repo_root() / DRAFTS


def _record_path(folder: Path) -> Path:
    return folder.parent / f"{folder.name}{SUFFIX}"


def _label() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def reserve(name: str, *, label: str | None = None, metadata: dict | None = None, aliases=(), watch: bool = False,
            folder: Path | None = None, uid: str | None = None, **extra) -> tuple[Path, dict]:
    """Make a draft's folder (`folder`, default `<drafts root>/<name>/<uid>/`, fresh) and write its record."""
    target = refs.parse(name)
    if target.selector or target.path:
        raise ValueError(f"a draft takes an artifact name, not {name!r}")
    refs.valid_name(target.name)
    if target.project:
        refs.valid_project(target.project)
    label = label or _label()
    refs.valid_label(label)
    uid = uid or new_uid()
    given = folder is not None
    folder = Path(folder) if given else drafts_root() / target.name / uid
    folder.mkdir(parents=True, exist_ok=given)  # a given folder is a Hydra job's: Hydra made it
    record = {"uid": uid, "project": target.project, "name": target.name, "label": label,
              "metadata": metadata, "aliases": list(aliases), "watch": bool(watch),
              "created": datetime.now(timezone.utc).isoformat(), "owner": {"host": socket.gethostname(), "pid": os.getpid()},
              "state": "open", **extra}
    write_json(_record_path(folder), record)
    return folder.resolve(), record


class Draft(Writer):
    """A fresh folder (`path`) that becomes the next version of artifact `name` when it closes.

    A context manager: normal exit publishes it (`publish`), an exception keeps it (`fail`). Without `with`,
    call `publish()`, `fail()`, or `discard()` yourself (a draft still open when the process's run ends is
    published with it, or kept when the run fails). It is also a writer on its folder (`add`, `add_figure`,
    `add_image`, `add_video`, `add_plotly`, `add_table`, `add_json`, `add_text`, `add_file`, `add_dir`)."""

    is_run = False

    def __init__(self, name: str, *, label: str | None = None, metadata: dict | None = None, aliases=(), watch: bool = False,
                 store=None, config: dict | None = None, interval: float | None = None, activate: bool = True):
        if config is not None:
            metadata = {**(metadata or {}), "config": config}
        folder, record = reserve(name, label=label, metadata=metadata, aliases=aliases, watch=watch)
        self._setup(folder, folder, record, store, interval)
        if activate:
            self.activate()

    @classmethod
    def _at(cls, folder: Path, path: Path, record: dict, store=None) -> "Draft":
        draft = cls.__new__(cls)
        draft._setup(folder, path, record, store, None)
        return draft

    def _setup(self, folder: Path, path: Path, record: dict, store, interval: float | None) -> None:
        from ..config import find_project

        Writer.__init__(self, path)
        self.project = find_project()  # origins are relative to the project the job runs in
        self.folder, self.record = folder, record
        self.uid, self.name = record["uid"], record["name"]
        self.trial = path != folder  # a view on a subfolder (a multirun trial): it never publishes itself
        self.version = None
        self._store_arg, self._store = store, None
        self._interval = interval
        self._active = False
        self._pub = None
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._watcher: threading.Thread | None = None
        self._moved = 0.0  # seconds the watcher spent transferring blobs (`_stop_watch`)
        self._closed = False

    def __repr__(self) -> str:
        return f"<cairn.{type(self).__name__} {self.name} {self.uid} {self.path}>"

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
        """The object API's Repo this draft publishes to (opened on first use; None: the project's repo)."""
        if self._store is None:
            from .objects import Repo
            from .open import as_repo

            arg = self._store_arg
            if isinstance(arg, Repo):
                self._store = arg
            else:
                self._store = Repo(as_repo(arg))
                self._store.backend.resume()  # what earlier processes left pending goes too
        return self._store

    @property
    def target(self):
        """The Artifact this draft becomes a version of."""
        return self.store.project(self.record.get("project")).artifact(self.name)

    @property
    def ref(self) -> str:
        """`project:name@<uid>`: this draft's version, exactly, before and after it is published."""
        target = self.target
        return f"{target.project.name}:{target.name}@{self.uid}"

    @property
    def run_ref(self) -> str | None:
        """The run this draft is an output of (`project:runs/<name>@<uid>`), once it is open."""
        return self.record.get("run")

    # ------------------------------------------------------------ activation

    def activate(self) -> "Draft":
        """Make this the draft the process writes into, as an output of the process's run (started now if it is
        not yet), and, with `watch`, start the running version."""
        if self._active:
            return self
        self._active = True
        if not self.is_run and not self.trial:
            from . import runs

            run = runs.input_for(self)
            if run and run != self.record.get("run"):
                self.record["run"] = run
                self._save()
        _ACTIVE.append(self)
        if self.watch and not self.trial and self._pub is None:
            self._start_watch()
        return self

    def deactivate(self) -> None:
        if not self._active:
            return
        self._active = False
        index = next((i for i, item in enumerate(_ACTIVE) if item is self), None)
        if index is not None:
            del _ACTIVE[index]

    def __enter__(self) -> "Draft":
        return self.activate()

    def __exit__(self, kind, error, trace) -> bool:
        if kind is None:
            self.publish()
        else:
            self.fail(error)
        return False

    # ------------------------------------------------------------ inputs and config

    def _inputs(self) -> list[str]:
        """The version's inputs: its run (while that is running or completed), and any listed in the record."""
        found = [*(self.record.get("inputs") or [])]
        run = self.record.get("run")
        if run and run not in found and self._run_usable(run):
            found.insert(0, run)
        return found

    def _run_usable(self, ref: str) -> bool:
        from . import runs

        current = runs.current()
        if current is not None and current.uid == refs.parse(ref).selector:
            return True
        if os.environ.get(runs.RUN_ENV) == ref:
            return True
        try:
            status = self.store.version(ref).status
        except StoreError as error:
            print(f"warning: {self.name}@{self.uid}: its run {ref} is not available ({error}); publishing it without",
                  file=sys.stderr, flush=True)
            return False
        if status in ("running", "completed"):
            return True
        print(f"warning: {self.name}@{self.uid}: its run {ref} is {status}; publishing it without that input",
              file=sys.stderr, flush=True)
        return False

    @property
    def config(self) -> dict | None:
        """The configuration (`metadata["config"]`; Hydra drafts get the resolved job config)."""
        return (self.record.get("metadata") or {}).get("config")

    def set_metadata(self, **values) -> None:
        """Add metadata (for example `config=`, `overrides=`) until the draft is published; a running version in the
        store gets it too."""
        self._ensure_open()
        self.record["metadata"] = {**(self.record.get("metadata") or {}), **values}
        self._save()
        if self._pub is not None:
            with self._lock:
                self._pub.set_metadata(self.record["metadata"])

    # ------------------------------------------------------------ watching

    def _start_watch(self, follow: bool = True) -> None:
        from .publishing import Publication

        target = self.target
        self._pub = Publication(f"{target.project.name}:{target.name}", self.folder, metadata=self.record.get("metadata"),
                                aliases=self.record.get("aliases") or (), label=self.label, store=self.store.backend,
                                uid=self.uid)
        self._pub.start()
        self.version = target.version(self._pub.uid)
        self.record["running"] = {"uid": self._pub.uid, "number": self._pub.version}
        self._save()
        if not follow:
            return
        interval = float(self._interval if self._interval is not None else self._pub.config.repo.get("interval", 30))
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
                    moved = transferring()
                    try:
                        self.sync()
                    finally:
                        self._moved += transferring() - moved
                    seen = current
            except (StoreError, OSError) as error:
                print(f"warning: syncing the draft {self.folder} failed ({error}); retrying", file=sys.stderr, flush=True)

    def _stop_watch(self) -> None:
        """Stop following the folder, once the sync in progress ended (its waits for the remote are bounded; a transfer
        goes on while bytes flow, and the time it took is not charged to this thread's budget for waiting)."""
        if self._stop is not None:
            moved = self._moved
            self._stop.set()
            self._watcher.join()
            credit_transfer(self._moved - moved)
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
        `Version` (also `self.version`; pending while the remote does not answer). If publishing fails, the
        folder is kept (`fail`) and the error raised. A folder whose `run.json` is not `completed` is refused unless
        `allow_incomplete`."""
        self._ensure_open()
        self.deactivate()
        self._stop_watch()
        try:
            inputs = self._inputs()
            if self._pub is not None:
                with self._lock:
                    self._pub.inputs = inputs
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
                                              label=self.label, inputs=inputs, allow_incomplete=allow_incomplete,
                                              _link=True, _uid=uid)
        except BaseException as error:
            self._keep(error)
            raise
        self.version = version
        self._remove()
        return version

    def fail(self, error: BaseException | str | None = None) -> None:
        """Keep the folder for inspection (state `kept`, printed on stderr) and mark a running version failed."""
        if self.trial:
            self.deactivate()
            return
        self._ensure_open()
        self._stop_watch()
        self.deactivate()
        self._keep(error)

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
        self._closed = True
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
        elif (self.record.get("running") or {}).get("uid"):
            self._fail_running()
        self._remove()

    def _fail_running(self) -> None:
        """A kept or abandoned watch draft's running version: fail it (with a remote a sender would also fail it once its
        heartbeat expires)."""
        running = self.record["running"]
        target = self.target
        try:
            if self.store.backend.get_version(target.project.name, target.name, running["uid"])["status"] == "running":
                self.store.backend.fail_version(target.project.name, target.name, running["uid"])
        except StoreError:
            pass

    def _remove(self) -> None:
        self._closed = True
        try:  # before the folder goes: it may be the working directory, which the repo is found from
            root = drafts_root().resolve()
        except (OSError, StoreError):
            root = None
        shutil.rmtree(self.folder, ignore_errors=True)
        _record_path(self.folder).unlink(missing_ok=True)
        if root is None or find(self.folder.parent) is not None:  # a trial's folder inside its sweep's: the sweep's stays
            return
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

        with cairn.draft("models/student") as model:
            model.add_file("model.pt")         # or write files into model.path
        model.version                          # the published Version; its input is the process's run

    `label` defaults to the start time (`YYYY-MM-DD_HH-MM-SS`); `watch=True` keeps a running version synced while
    the block runs. `store` as for `cairn.publish` (None: the project's repo). `config` (a plain dict) becomes
    `metadata["config"]`. The version's uid is the draft's uid. The process's run starts now if it has not."""
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
    """The draft this process writes into: the innermost open one, else the one the working directory lies in (a
    Hydra job whose run dir is `${cairn.draft:...}`, or a subprocess of one), else None. The run is `cairn.run()`."""
    if _ACTIVE:
        return _ACTIVE[-1]
    found = find(Path.cwd())
    return None if found is None or found.record.get("kind") == "run" else found


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
