"""Runs: every process that reads or writes artifacts is a run, and the run is an artifact version too.

    run = cairn.init(name="train", config=cfg)       # optional: the first cairn.use(), pull(), or draft() starts it
    data = cairn.use("datasets/crops@latest")         # an input of the run (a Version; data.path is its folder)
    run.add({"loss": loss, "step": step})             # the run is a writer too: its metrics.jsonl
    with cairn.draft("models/student") as model:      # an output of the run: its one input is the run's version
        model.add_file("model.pt")
    # the run completes when the process exits (failed on an unhandled exception, a non-zero exit, a crash, or SIGTERM)

A `Run` is a draft (cairn/client/drafts.py) of the artifact `runs/<name>`, with `metadata.kind = "run"` and
`metadata.config`. The name defaults to the script's stem (`python -m pkg.mod`: the module's name; an interactive
session: `interactive`). There is one current run per process: a second `init()` returns it (adding its `config` and `metadata`, and refusing another
name), `cairn.run()` returns it or None. Its version is created at once (running), so the outputs published while
it runs name it by its uid; with a store server it is synced every `[store] interval` seconds (`watch`, the
default there) with a heartbeat, so the UI shows it live; with a local store it is synced when it ends (`watch=True`
to follow it).

Its folder gets, besides what the job writes with `run.add(...)` and `run.add_*`:

    code/                  the source snapshot: commit, diff, untracked sources (cairn/client/code.py); manifest.json
    environment/           pip-freeze.txt (the installed distributions), pixi.lock when the project has one
    logs/stdout.txt        the process's output (still printed; capture=False turns it off)
    logs/stderr.txt
    logs/error.txt         the traceback, when the run failed
    system/info.json       host, platform, Python, command line, cwd, Slurm job, SLURM_* and CUDA_VISIBLE_DEVICES
    system/metrics.jsonl   CPU, memory, disk, network, GPUs, every 10 s (system=<seconds> for another interval)

Lineage: what the process pulls (`cairn.use`, `cairn.pull`, `Version.pull`/`open`, and plain subprocesses through
`$CAIRN_INPUTS`) are the run's inputs; every draft it publishes has the run as its one input. A subprocess of a run
(it inherits `$CAIRN_RUN`) does not start a run of its own: what it pulls is recorded in the parent's run, and its
drafts are outputs of that run; `cairn.init()` there starts a separate run.

Completion: at exit (`atexit`), the open drafts of the run are published, then the run; an unhandled exception
(`sys.excepthook`), a non-zero exit (`sys.exit(2)`, `raise SystemExit("message")`: the exit code goes to
logs/error.txt and `metadata.exit_code`; `sys.exit(0)` and `sys.exit()` complete it) or SIGTERM (when no other
handler is installed) fails it, keeping its folder and writing logs/error.txt. A process killed without a chance to
run any of this (SIGKILL, the OOM killer) leaves the version `running` until the store notices the missing
heartbeat: it fails it the next time anything opens the store (local store: `LocalRepo.reap`; store server: a
flusher, see sync.py). `run.finish()` ends it earlier (a Hydra job does, cairn/runs/hydra.py), after which the process may
start another one.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import signal
import sys
import threading
import traceback
from pathlib import Path

from ..store import INPUTS_ENV, INPUTS_FILE, StoreError
from ..store.inputs import close_sink, open_sink
from . import logger as _logger
from .drafts import _ACTIVE, Draft, reserve

RUN_ENV = "CAIRN_RUN"  # the exact uid reference of the run a process belongs to, for its subprocesses
RUNS = "runs"
SYSTEM_INTERVAL = 10.0
CAPTURE_DEFAULT = True  # tests turn it off for the runs pytest itself starts
_CURRENT: "Run | None" = None
_LOCK = threading.RLock()
_HOOKS = {"installed": False, "excepthook": None}
_EXIT: dict = {"code": None, "error": None}  # the exit code of a SystemExit that left the main module (see _watch_exit)
_logger._OWN_FILES.add(Path(__file__).resolve())


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", str(text).lower()).strip("-._")
    return slug[:96] or "run"


def default_name() -> str:
    """The script's stem; for `python -m pkg.mod` the module's name; for an interactive session `interactive`."""
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    if spec is not None and getattr(spec, "name", None) and spec.name != "__main__":
        name = spec.name[: -len(".__main__")] if spec.name.endswith(".__main__") else spec.name
        return _slug(name)
    file = getattr(main, "__file__", None)
    if hasattr(sys, "ps1") or not file or not sys.argv or not sys.argv[0] or sys.argv[0] in ("-c", "-"):
        return "interactive"
    return _slug(Path(sys.argv[0]).stem)


def _interactive() -> bool:
    return hasattr(sys, "ps1") or bool(sys.flags.interactive) or "ipykernel" in sys.modules


def _same_store(one, other) -> bool:
    a, b = one.backend, other.backend
    if type(a) is not type(b):
        return False
    if getattr(a, "url", None) or getattr(b, "url", None):
        return getattr(a, "url", None) == getattr(b, "url", None)
    return Path(a.root).resolve() == Path(b.root).resolve() and a.project == b.project


class Run(Draft):
    """The process's run: a draft of `runs/<name>` that records the process (see the module doc). Make one with
    `cairn.init()`; it is also a writer on its folder (`add`, `add_figure`, ...)."""

    is_run = True

    def __init__(self, name: str | None = None, *, config=None, metadata: dict | None = None, watch: bool | None = None,
                 capture: bool = True, system: bool | float = True, code: bool = True, store=None, label: str | None = None,
                 folder: Path | None = None, adopt: Draft | None = None, inputs=()):
        self.run_name = _slug(name or default_name())
        meta = {"kind": "run", **(metadata or {})}
        if config is not None:
            meta["config"] = config
        if adopt is not None:  # a folder reserved earlier (Hydra's ${cairn.run:}): its uid and label stay
            record = adopt.record
            record.update(metadata={**(record.get("metadata") or {}), **meta}, name=f"{RUNS}/{self.run_name}",
                          label=label or record.get("label"))
            folder = adopt.folder
            self._setup(folder, folder, record, store, None)
            self._save()
        else:
            folder, record = reserve(f"{RUNS}/{self.run_name}", label=label, metadata=meta, folder=folder, kind="run")
            self._setup(folder, folder, record, store, None)
        if inputs:
            self.record["inputs"] = list(inputs)
            self._save()
        self.options = {"watch": watch, "capture": capture, "system": system, "code": code}
        self._captures: list = []
        self._sampler = None
        self._code_thread: threading.Thread | None = None
        self._error = None
        self._previous: dict = {}
        self._sink: Path | None = None
        self._finished = False

    @property
    def config(self):
        return (self.record.get("metadata") or {}).get("config")

    # ------------------------------------------------------------ starting

    def start(self) -> "Run":
        """Create the running version and start recording (env, output, system, code); see the module doc."""
        global _CURRENT
        watch = self.options["watch"]
        watch = self.store.remote if watch is None else bool(watch)
        self.record["watch"] = watch
        self._save()
        self._start_watch(follow=watch)
        ours = str(self.folder / INPUTS_FILE)
        self._previous = {INPUTS_ENV: os.environ.get(INPUTS_ENV), RUN_ENV: os.environ.get(RUN_ENV)}
        inherited = self._previous[INPUTS_ENV]
        if inherited and os.path.abspath(inherited) != os.path.abspath(ours):
            self._sink = Path(inherited)  # a parent that is not a run (cairn publish --watch) still hears of every pull
            open_sink(self._sink)
        os.environ[INPUTS_ENV] = ours
        os.environ[RUN_ENV] = self.ref
        _CURRENT = self
        _install_hooks()
        if self.options["capture"]:
            from .system import Capture

            fd_level = not _interactive()
            for which in ("stdout", "stderr"):
                try:
                    self._captures.append(Capture(which, self.folder / "logs" / f"{which}.txt", fd_level=fd_level))
                except OSError as error:
                    print(f"warning: cairn could not capture {which} ({error})", file=sys.stderr, flush=True)
        system = self.options["system"]
        if system:
            from .system import Sampler, info

            (self.folder / "system").mkdir(parents=True, exist_ok=True)
            facts = {**info(), "run": self.ref}
            (self.folder / "system" / "info.json").write_text(json.dumps(facts, indent=2, default=str) + "\n", encoding="utf-8")
            interval = SYSTEM_INTERVAL if system is True else float(system)
            self._sampler = Sampler(self.folder, interval).start()
        if self.options["code"]:
            self._code_thread = threading.Thread(target=self._snapshot, name="cairn-code", daemon=True)
            self._code_thread.start()
        return self

    def _snapshot(self) -> None:
        """code/ and environment/ (in a thread, so a large repository does not hold up the job)."""
        from ..config import find_project_root
        from .code import create_snapshot, snapshot_mode, write_environment

        root = find_project_root()
        try:
            write_environment(root or Path.cwd(), self.folder / "environment")
        except OSError as error:
            print(f"warning: cairn could not record the environment ({error})", file=sys.stderr, flush=True)
        code = self.folder / "code"
        if root is None:
            code.mkdir(parents=True, exist_ok=True)
            (code / "manifest.json").write_text(json.dumps({"skipped": "no project root (cairn.toml, pyproject.toml, ...)"},
                                                           indent=2) + "\n")
            return
        try:
            create_snapshot(root, code, snapshot_mode(root), 2_000_000, 50_000_000)
        except (OSError, RuntimeError, ValueError) as error:
            import shutil

            shutil.rmtree(code, ignore_errors=True)
            code.mkdir(parents=True, exist_ok=True)
            (code / "manifest.json").write_text(json.dumps({"source_root": str(root), "error": str(error)}, indent=2) + "\n")

    # ------------------------------------------------------------ ending

    def fail_with(self, error) -> None:
        """Mark the run failed when it ends (an exception, or a reason as text)."""
        if self._error is None:
            self._error = error

    def finish(self, error=None):
        """End the run now: publish its open drafts and then the run (or, with `error`, keep them and fail the run);
        returns the run's Version (None when it failed). Called at exit when not called before."""
        global _CURRENT
        with _LOCK:
            if self._finished:
                return self.version
            self._finished = True
        error = error if error is not None else self._error
        self._stop_recording(error)
        for other in list(_ACTIVE):
            if other is self or other._closed or other.trial or other.record.get("run") != self.ref:
                continue
            try:
                other.fail(error) if error is not None else other.publish()
            except Exception as failure:  # noqa: BLE001  (one draft must not keep the run from ending)
                print(f"warning: could not close the draft {other.folder} ({failure})", file=sys.stderr, flush=True)
        self._release()
        if _CURRENT is self:
            _CURRENT = None
        if error is None:
            return self.publish()
        self.fail(error)
        return None

    def _stop_recording(self, error) -> None:
        if self._sampler is not None:
            self._sampler.stop()
            self._sampler = None
        if self._code_thread is not None:
            self._code_thread.join(timeout=120)
            self._code_thread = None
        if error is not None:
            if isinstance(error, BaseException):
                text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            else:
                text = f"{error}\n"
            try:
                (self.folder / "logs").mkdir(parents=True, exist_ok=True)
                (self.folder / "logs" / "error.txt").write_text(text, encoding="utf-8")
            except OSError:
                pass
        for capture in reversed(self._captures):
            try:
                capture.stop()
            except OSError:
                pass
        self._captures = []

    def _release(self) -> None:
        """Give the environment back (`$CAIRN_INPUTS`, `$CAIRN_RUN`) and stop the extra sink."""
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._previous = {}
        if self._sink is not None:
            close_sink(self._sink)
            self._sink = None

    def _abandon(self) -> None:
        """Stop everything this run started without ending it (tests): it stays running, its folder stays."""
        global _CURRENT
        self._finished = True
        self._stop_recording(None)
        self._stop_watch()
        if self._pub is not None:
            self._pub._stop_beating()
        self._release()
        self.deactivate()
        if _CURRENT is self:
            _CURRENT = None


# ------------------------------------------------------------------ the process's run


def current() -> Run | None:
    """The run of this process, or None (`cairn.run()`)."""
    return _CURRENT


def init(name: str | None = None, *, config=None, metadata: dict | None = None, watch: bool | None = None, capture: bool | None = None,
         system: bool | float = True, code: bool = True, store=None, label: str | None = None, _adopt: Draft | None = None,
         _folder: Path | None = None, _inputs=()) -> Run:
    """Start this process's run (see the module doc), or return it when it already runs.

    `name` (default: the script's stem) makes it a version of `runs/<name>`; `config` (plain values) becomes
    `metadata.config`, `metadata` is added to the version's metadata. `watch` (default: True for a store server)
    keeps the running version synced while it runs; `capture` tees stdout and stderr into logs/; `system` samples
    the machine every 10 s (or every `system` seconds); `code` snapshots the source and the installed packages.
    `store` as for `cairn.publish` (None: the configured store). A second call returns the current run; with
    another name it raises, and a `config` or `metadata` given then is added to the running run (also when the
    first `use`/`pull`/`draft` started it implicitly)."""
    with _LOCK:
        if _CURRENT is not None:
            run = _CURRENT
            if name is not None and _slug(name) != run.run_name:
                raise StoreError(f"this process's run is {run.name}@{run.uid}: one run per process (finish() it first)")
            if config is not None or metadata:  # e.g. the run was started by the first cairn.use(): complete it now
                run.set_metadata(**({"config": config} if config is not None else {}), **(metadata or {}))
            return run
        run = Run(name, config=config, metadata=metadata, watch=watch,
                  capture=CAPTURE_DEFAULT if capture is None else capture, system=system, code=code, store=store,
                  label=label, folder=_folder, adopt=_adopt, inputs=_inputs)
        try:
            run.start()
        except BaseException:
            run._abandon()
            if run._pub is None:
                import shutil

                shutil.rmtree(run.folder, ignore_errors=True)
                from .drafts import _record_path

                _record_path(run.folder).unlink(missing_ok=True)
            raise
        return run


def ensure(store=None) -> Run | None:
    """The current run, else a new one (implicitly, named after the script); None in a subprocess of a run (it
    inherited `$CAIRN_RUN`): that run records what this process reads."""
    if _CURRENT is not None:
        return _CURRENT
    if os.environ.get(RUN_ENV):
        return None
    return init(store=store)


def input_for(draft: Draft) -> str | None:
    """The run reference a draft opened now gets as its input (starting the run if needed)."""
    run = ensure(draft._store_arg)
    if run is None:
        return os.environ.get(RUN_ENV)
    if not _same_store(run.store, draft.store):
        print(f"warning: {draft.name}@{draft.uid} goes to another store than the run {run.ref}: it gets no run input",
              file=sys.stderr, flush=True)
        return None
    return run.ref


def _reset() -> None:
    """Forget the current run without ending it (tests)."""
    run = _CURRENT
    if run is not None:
        run._abandon()


# ------------------------------------------------------------------ process hooks


def _install_hooks() -> None:
    if _HOOKS["installed"]:
        return
    _HOOKS["installed"] = True
    # Finish before the interpreter stops thread pools (concurrent.futures refuses new work in plain atexit handlers):
    # threading's exit callbacks run first, the last registered first, so register after concurrent.futures has.
    import concurrent.futures.thread  # noqa: F401

    register = getattr(threading, "_register_atexit", None)
    try:
        register(_at_exit) if register is not None else atexit.register(_at_exit)
    except RuntimeError:
        atexit.register(_at_exit)
    _HOOKS["excepthook"] = sys.excepthook
    sys.excepthook = _excepthook
    _watch_exit()
    if threading.current_thread() is threading.main_thread():
        try:
            if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None):
                signal.signal(signal.SIGTERM, _on_signal)
        except (ValueError, OSError):
            pass


def _exit_code(error: SystemExit) -> int:
    """The process exit code of a SystemExit: None is 0, an int is itself, anything else (a message) is 1."""
    code = error.code
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def _watch_exit() -> None:
    """Learn the exit code of a SystemExit that leaves the main module (`sys.exit(n)` or `raise SystemExit(n)`).

    Python calls no hook for it: `sys.excepthook` does not see SystemExit, and the code is gone when the exit callbacks
    run. From Python 3.12 `sys.monitoring` reports every frame an exception leaves; the one hook that matters is the main
    module's own frame, which a caught SystemExit never leaves. Before 3.12, `sys.exit` is wrapped (a `raise SystemExit`
    there goes unnoticed) and the exception's traceback tells at exit whether it reached the main module."""
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is not None:
        for tool in (3, 4):  # 0 debugger, 1 coverage, 2 profiler, 5 optimizer
            try:
                monitoring.use_tool_id(tool, "cairn")
            except ValueError:
                continue

            def unwind(code, offset, exception):
                if isinstance(exception, SystemExit) and code.co_name == "<module>" and \
                        sys._getframe(1).f_globals.get("__name__") == "__main__":
                    _EXIT["code"] = _exit_code(exception)

            monitoring.register_callback(tool, monitoring.events.PY_UNWIND, unwind)
            monitoring.set_events(tool, monitoring.events.PY_UNWIND)
            _HOOKS["tool"] = tool
            return
        return
    original = sys.exit

    def exit(status=None):
        try:
            original(status)
        except SystemExit as error:
            _EXIT["error"] = error
            raise

    sys.exit = exit


def _exit_status() -> int:
    """The exit code recorded for the process (0 when it did not exit through a SystemExit)."""
    error = _EXIT["error"]
    if error is not None and _EXIT["code"] is None:  # Python before 3.12: did the exception reach the main module?
        trace = error.__traceback__
        if trace is not None and trace.tb_frame.f_code.co_name == "<module>" and trace.tb_frame.f_globals.get("__name__") == "__main__":
            return _exit_code(error)
    return _EXIT["code"] or 0


def _excepthook(kind, value, trace) -> None:
    run = _CURRENT
    if run is not None and not issubclass(kind, SystemExit):
        run.fail_with(value)
    (_HOOKS["excepthook"] or sys.__excepthook__)(kind, value, trace)


def _on_signal(signum, frame) -> None:
    run = _CURRENT
    if run is not None:
        run.fail_with(f"terminated by signal {signal.Signals(signum).name}")
    raise SystemExit(128 + signum)


def _at_exit() -> None:
    run = _CURRENT
    if run is None:
        return
    try:
        code = _exit_status()
        if code:
            run.fail_with(f"the process exited with code {code}")
            try:
                run.set_metadata(exit_code=code)
            except Exception:  # noqa: BLE001  (the failure itself matters more than its metadata)
                pass
        run.finish()
    except BaseException as error:  # noqa: BLE001  (never a traceback at interpreter exit)
        print(f"cairn: could not finish the run {run.name}@{run.uid} ({error}); its folder is {run.folder}",
              file=sys.__stderr__, flush=True)
