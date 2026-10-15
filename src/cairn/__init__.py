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

    store = cairn.connect()                        # the store [store] in cairn.toml names (or a URL, or a folder)
    crops = store.project().artifact("datasets/crops")
    v3 = crops.version(3)                          # or "v3", an alias, a label, a uid; crops.latest
    v3.files, v3.metadata, v3.inputs, v3.consumers, v3.upstream(), v3.downstream(), v3.open("a.txt"), v3.pull()
    crops.publish("data/crops", metadata={"n": 3}, aliases=["paper"])   # -> Version
    cairn.pull("datasets/crops@v3")                # a read-only local copy (a Path), recorded like use()
    cairn.publish("models/denoiser", "checkpoints/best", metadata={"psnr": 31.2}, aliases=["best"])
    cairn.alias("models/denoiser@v4", "paper")
    cairn.versions("models/student")
    cairn.lineage("models/student@v2")             # through the run: the data it came from, what consumed it

`store=None` means the configured store (`[store]` in cairn.toml): a store server when `url`
is set (`RemoteStore`; the token comes from CAIRN_STORE_TOKEN or `token_file`), else a local folder,
`path` (default `.cairn/store` in the project root). `project` names the default project (default: the
project folder's slug). The run (cairn/client/runs.py) points `$CAIRN_INPUTS` at its folder, so what the
process and its plain subprocesses pull are its inputs; outside a run every pull appends the exact version to
`$CAIRN_INPUTS`, else to `.cairn-inputs.jsonl` in the working directory, and `publish` carries a folder's
`.cairn-inputs.jsonl` along as the version's `inputs`.

Every version has a uid (UUIDv7), and `name@<uid>` names it as exactly as `name@v<n>`. Publishing to a
store server goes through a local replica, so it works while the server is down: the version is pending
in the durable data root (`$CAIRN_DATA_DIR`, else `.cairn/pending` in the project), usable at once by its uid,
and sent later (`cairn status`, `cairn sync`). Pulls of exact versions, or of aliases by their last known
resolution, work from the throwaway cache root (`$CAIRN_CACHE_DIR`, else `.cairn/cache` in the project; `$CAIRN_DIR` moves the whole `.cairn` folder).

The parts are packages: `cairn.store` (the core), `cairn.client` (HTTP client, the object API, publishing,
the replica, drafts and runs, `RemoteStore`), `cairn.server`, `cairn.ui`, `cairn.reports`, and `cairn.runs`
(Hydra); each is imported only when used.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ._version import __version__
from .store import (BadRange, Conflict, Deleted, FileRead, Forbidden, LocalStore, MissingBlobs, NotFound, Protected, Ref, StoreError,
                    Unauthorized, Unreachable)

if TYPE_CHECKING:
    from .client.drafts import Draft
    from .client.runs import Run
    from .client.objects import Artifact, File, Project, Store, Version
    from .client.remote import RemoteStore

__all__ = ["__version__", "Artifact", "BadRange", "Conflict", "Deleted", "Draft", "File", "FileRead", "Forbidden", "LocalStore", "MissingBlobs",
           "NotFound", "Project", "Protected",
           "Ref", "RemoteStore", "Run", "Store", "StoreError", "Unauthorized", "Unreachable", "Version", "alias", "connect", "lineage",
           "Publication", "current_draft", "draft", "init", "open_store", "publish", "pull", "run", "use", "versions"]

_CLIENT = {"RemoteStore": "remote", "open_store": "open", "Publication": "publishing", "publish": "publishing",
           "connect": "objects", "Store": "objects", "Project": "objects", "Artifact": "objects", "Version": "objects",
           "File": "objects", "Draft": "drafts", "draft": "drafts", "current_draft": "drafts", "Run": "runs", "init": "runs"}


def __getattr__(name: str):
    """The names that need the HTTP client load it on first use (`import cairn` stays light)."""
    if name in _CLIENT:
        from importlib import import_module

        return getattr(import_module(f".client.{_CLIENT[name]}", __name__), name)
    raise AttributeError(f"module 'cairn' has no attribute {name!r}")


def _store(store) -> "Store":
    """The object API's Store for `store=` (None: the configured store; a store; a folder)."""
    from .client.objects import Store
    from .client.open import as_store

    return store if isinstance(store, Store) else Store(as_store(store))


def run() -> "Run | None":
    """This process's run (`cairn.init()`, or the one the first use/pull/draft started), else None."""
    from .client import runs

    return runs.current()


def _start_run(store) -> None:
    from .client import runs

    runs.ensure(store)


def use(ref: str, *, store: "Store | LocalStore | RemoteStore | Path | str | None" = None, offline: bool = False) -> "Version":
    """The exact version a reference names, resolved now, as an input of this process's run (started if needed).

    Returns the `Version` (its files are listed lazily; `.path` is its read-only local folder, materialized on
    first use; `.open(path)` streams one file). A subprocess of a run records it in that run."""
    _start_run(store)
    version = _store(store).version(ref, offline=offline)
    version._usable()
    if version.status != "completed":
        raise StoreError(f"{version.ref} is {version.status}: only a completed version can be used")
    from .store import record_input

    record_input(version.ref)
    return version


def pull(ref: str, *, store: "Store | LocalStore | RemoteStore | Path | str | None" = None, record: bool = True,
         offline: bool = False) -> Path:
    """A read-only local copy of an artifact version (or of the file after `//`), recorded as an input of the run.

    `record=False` leaves the inputs alone (and starts no run): the report builder records a report's inputs in
    the report's own manifest instead. An exact version in the cache needs no server; when the server does not
    answer (or `offline=True`), an alias falls back to its last known resolution, with a warning.
    """
    if record:
        _start_run(store)
    return _store(store).pull(ref, record=record, offline=offline)


def alias(ref: str, alias: str, *, store: "Store | LocalStore | RemoteStore | Path | str | None" = None) -> None:
    """Point `alias` at the version `ref` names (which must be completed)."""
    version = _store(store).version(ref)
    version.artifact.alias(alias, version)


def versions(name: str, *, store: "Store | LocalStore | RemoteStore | Path | str | None" = None) -> list[dict]:
    """Every version of an artifact (its manifest), oldest first, whatever its status; this machine's pending ones last."""
    return [version.manifest for version in _store(store).artifact(name).versions()]


def lineage(ref: str, *, store: "Store | LocalStore | RemoteStore | Path | str | None" = None,
            through_runs: bool = True) -> dict[str, list[str]]:
    """One level up (`inputs`) and down (`consumers`) from a version, as exact references.

    With `through_runs` (the default) a run in between is looked through: an output's inputs are the data its run
    used, a dataset's consumers are the outputs of the runs that used it, and `runs` lists the runs passed. With
    `through_runs=False` the direct edges, run versions included."""
    version = _store(store).version(ref)
    if not through_runs:
        return {"inputs": version.inputs, "consumers": version.consumers}
    passed: list[str] = []
    upstream = [item.ref for item in version.upstream(_runs=passed)]
    downstream = [item.ref for item in version.downstream(_runs=passed)]
    return {"inputs": upstream, "consumers": downstream, "runs": passed}
