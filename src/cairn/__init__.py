"""Cairn: an artifact store for run outputs and reports, with a server and a UI, plus static reports and run tracking.

This is the light API (no server, UI, reports, matplotlib, plotly, or hydra is imported):

    import cairn

    run = cairn.init(name="train", config=cfg)       # optional; the first cairn.use() or cairn.draft() starts it
    data = cairn.use("datasets/crops@latest")         # an input of the run: the exact Version (data.path: its files)
    run.add({"loss": loss, "psnr": psnr, "step": step})            # one entry in the run's metrics.jsonl
    run.add_figure("samples", fig, {"loss": loss, "step": step})    # every add_* takes optional metadata
    with cairn.draft("models/student") as model:      # an output of the run (its one input is the run's version)
        model.add({"val_loss": v, "step": step})
        model.add_file("model.pt")
    # the run (runs/train@v<n>) completes when the process exits; failed on an unhandled exception or a crash

and the object API and shortcuts:

    repo = cairn.connect()                         # the project's repo (.cairn/), with its remote if [repo] remote names one
    crops = repo.artifact("datasets/crops")
    v3 = crops.version(3)                          # or "v3", an alias, a label, a uid, a short id; crops.latest
    v3.files, v3.metadata, v3.inputs, v3.consumers, v3.upstream(), v3.downstream(), v3.open("a.txt"), v3.pull()
    v3.short_id                                    # "3f9a2c1": `datasets/crops@3f9a2c1`, or a bare `3f9a2c1`, names it
    crops.publish("data/crops", metadata={"n": 3}, aliases=["paper"])   # -> Version
    cairn.pull("datasets/crops@v3")                # a read-only local copy (a Path), recorded like use()
    cairn.publish("models/denoiser", "checkpoints/best", metadata={"psnr": 31.2}, aliases=["best"])
    cairn.alias("models/denoiser@v4", "paper")
    cairn.versions("models/student")
    cairn.lineage("models/student@v2")             # through the run: the data it came from, what consumed it

Everything works on the project's repo: the folder `.cairn/` (`[cairn] dir`, `$CAIRN_DIR`), which holds the project
(one, so references name no project: `descent@v3`). Every write goes to the repo first. Without a remote the repo is
the source of truth and numbers its versions. With `[repo] remote = "<server>/<project>"` (a project on a `cairn
server`; the token from `$CAIRN_TOKEN` or `[repo] token_file`) the remote is: versions wait in the repo's outbox
(pending: a uid, no number) until the remote takes them, which happens at once when it answers and otherwise
automatically later (`cairn status`, `cairn sync`); versions used from the remote are copied into the repo. The run
(cairn/client/runs.py) points `$CAIRN_INPUTS` at its folder, so what the process and its plain subprocesses pull are
its inputs; outside a run every pull appends the exact version to `$CAIRN_INPUTS`, else to `.cairn-inputs.jsonl` in
the working directory, and `publish` carries a folder's `.cairn-inputs.jsonl` along as the version's `inputs`.

Every version has a uid (UUIDv7), and `name@<uid>` names it as exactly as `name@v<n>`; its short id (the first 7 hex
digits of the SHA-256 of the uid) names it too, by any unique prefix of 4 or more digits.

Cairn's own errors (StoreError: NotFound, Conflict, Unreachable, ...) are raised from the function or method called
(cairn/api.py): the traceback ends at the caller's line and that one call, then the message.

The parts are packages: `cairn.store` (the repo core), `cairn.client` (the remote, sync, the object API,
publishing, drafts and runs), `cairn.server`, `cairn.ui`, `cairn.reports`, and `cairn.runs` (Hydra); each is
imported only when used.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ._version import __version__
from .api import public
from .store import (Ambiguous, BadRange, Conflict, Deleted, FileRead, Forbidden, InvalidRef, LocalRepo, MissingBlobs, NoRun, NotFound,
                    Protected, Ref, StoreError, Unauthorized, Unreachable)

if TYPE_CHECKING:
    from .client.drafts import Draft
    from .client.runs import Run
    from .client.objects import Artifact, File, Repo, Version
    from .client.remote import Remote

__all__ = ["__version__", "Ambiguous", "Artifact", "BadRange", "Conflict", "Deleted", "Draft", "File", "FileRead", "Forbidden",
           "InvalidRef", "LocalRepo", "MissingBlobs", "NoRun", "NotFound", "Protected", "Ref", "Remote", "Run", "Repo", "StoreError",
           "Unauthorized", "Unreachable", "Version", "alias", "connect", "lineage", "Publication", "current_draft", "draft", "init",
           "open_repo", "publish", "pull", "run", "use", "versions"]

_CLIENT = {"Remote": "remote", "open_repo": "open", "Publication": "publishing", "publish": "publishing",
           "connect": "objects", "Repo": "objects", "Artifact": "objects", "Version": "objects",
           "File": "objects", "Draft": "drafts", "draft": "drafts", "current_draft": "drafts", "Run": "runs", "init": "runs"}


def __getattr__(name: str):
    """The names that need the HTTP client load it on first use (`import cairn` stays light)."""
    if name in _CLIENT:
        from importlib import import_module

        return getattr(import_module(f".client.{_CLIENT[name]}", __name__), name)
    raise AttributeError(f"module 'cairn' has no attribute {name!r}")


def _repo() -> "Repo":
    """The object API's Repo of the project's repo. A repo with pending versions starts sending them in the background
    (every process that touches the repo syncs)."""
    from .client.objects import Repo
    from .client.open import open_repo

    repo = Repo(open_repo())
    repo.backend.resume()
    return repo


class _CurrentRun:
    """`cairn.run`: this process's run (`cairn.init()`, or the one the first use/pull/draft started), wherever the
    code is, so nothing has to pass the run around: `cairn.run.add({"loss": loss})`. Every attribute is the current
    run's; with no run, using one raises (`bool(cairn.run)` is False then). `from cairn import run` works the same."""

    __slots__ = ()

    @staticmethod
    def _get() -> "Run":
        from .client import runs

        current = runs.current()
        if current is None:
            raise NoRun("no run in this process: call cairn.init() first")
        return current

    @public("cairn.run")
    def __getattr__(self, name: str):
        return getattr(self._get(), name)

    @public("cairn.run")
    def __setattr__(self, name: str, value) -> None:
        setattr(self._get(), name, value)

    def __bool__(self) -> bool:
        from .client import runs

        return runs.current() is not None

    def __eq__(self, other) -> bool:
        from .client import runs

        current = runs.current()
        return current is (other._get() if isinstance(other, _CurrentRun) else other) if current is not None else other is None

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        from .client import runs

        current = runs.current()
        return repr(current) if current is not None else "<cairn.run: no run (cairn.init() starts one)>"


run = _CurrentRun()


def _start_run() -> None:
    from .client import runs

    runs.ensure()


@public("cairn.use")
def use(ref: str, *, offline: bool = False) -> "Version":
    """The exact version a reference names (`name@v3`, `name@best`, `name@3f9a2c1`, a bare short id), resolved now in
    the project's repo, as an input of this process's run (started if needed).

    Returns the `Version` (its files are listed lazily; `.path` is its read-only local folder, materialized on
    first use; `.open(path)` streams one file). A subprocess of a run records it in that run."""
    _start_run()
    version = _repo().version(ref, offline=offline)
    version._usable()
    if version.status not in ("completed", "pending"):  # pending: completed here, not on the remote yet (`_usable`)
        raise StoreError(f"{version.ref} is {version.status}: only a completed version can be used")
    from .store import record_input

    record_input(version.ref, version.uid)
    return version


@public("cairn.pull")
def pull(ref: str, *, record: bool = True, offline: bool = False) -> Path:
    """A read-only local copy of an artifact version (or of the file after `//`), recorded as an input of the run.

    `record=False` leaves the inputs alone (and starts no run): the report builder records a report's inputs in
    the report's own manifest instead. An exact version the repo has needs no remote; when the remote does not
    answer (or `offline=True`), an alias resolves in the repo, with a warning.
    """
    if record:
        _start_run()
    return _repo().pull(ref, record=record, offline=offline)


@public("cairn.alias")
def alias(ref: str, alias: str) -> None:
    """Point `alias` at the version `ref` names (which must be completed)."""
    version = _repo().version(ref)
    version.artifact.alias(alias, version)


@public("cairn.versions")
def versions(name: str) -> list[dict]:
    """Every version of an artifact (its manifest), oldest first, whatever its status; the repo's pending ones last."""
    return [version.manifest for version in _repo().artifact(name).versions()]


@public("cairn.lineage")
def lineage(ref: str, *, through_runs: bool = True) -> dict[str, list[str]]:
    """One level up (`inputs`) and down (`consumers`) from a version, as exact references.

    With `through_runs` (the default) a run in between is looked through: an output's inputs are the data its run
    used, a dataset's consumers are the outputs of the runs that used it, and `runs` lists the runs passed. With
    `through_runs=False` the direct edges, run versions included."""
    version = _repo().version(ref)
    if not through_runs:
        return {"inputs": version.inputs, "consumers": version.consumers}
    passed: list[str] = []
    upstream = [item.ref for item in version.upstream(_runs=passed)]
    downstream = [item.ref for item in version.downstream(_runs=passed)]
    return {"inputs": upstream, "consumers": downstream, "runs": passed}
