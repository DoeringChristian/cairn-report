"""Cairn: an artifact store for run outputs and reports, with a server and a UI, plus static reports and a run logger.

This is the light API (no server, UI, reports, matplotlib, plotly, or hydra is imported):

    import cairn

    store = cairn.connect()                        # the store [store] in cairn.toml names (or a URL, or a folder)
    crops = store.project().artifact("datasets/crops")
    v3 = crops.version(3)                          # or "v3", an alias, a label, a uid; crops.latest
    v3.files, v3.metadata, v3.inputs, v3.consumers, v3.open("a.txt"), v3.pull()
    crops.publish("data/crops", metadata={"n": 3}, aliases=["paper"])   # -> Version

and the shortcuts, on top of it:

    crops = cairn.pull("datasets/crops@v3")        # read-only local copy; the exact version is recorded as an input
    cairn.publish("models/denoiser", "checkpoints/best", metadata={"psnr": 31.2}, aliases=["best"])
    cairn.alias("models/denoiser@v4", "paper")
    cairn.versions("descent")
    cairn.lineage("reports/boundary@v2")

`store=None` means the configured store (`[store]` in cairn.toml): a store server when `url`
is set (`RemoteStore`; the token comes from CAIRN_STORE_TOKEN or `token_file`), else a local folder,
`path` (default `.cairn/store` in the project root). `project` names the default project (default: the
project folder's slug). Every `pull` appends the exact version to `$CAIRN_INPUTS`, else to
`.cairn-inputs.jsonl` in the working directory; `publish` carries a folder's `.cairn-inputs.jsonl`
along as the version's `inputs`, which is how lineage is recorded without any framework hook.

Every version has a uid (UUIDv7), and `name@<uid>` names it as exactly as `name@v<n>`. Publishing to a
store server goes through a local replica, so it works while the server is down: the version is pending
in the durable data root (`$CAIRN_DATA_DIR`, else `.cairn/pending` in the project), usable at once by its uid,
and sent later (`cairn status`, `cairn sync`). Pulls of exact versions, or of aliases by their last known
resolution, work from the throwaway cache root (`$CAIRN_CACHE_DIR`, else `.cairn/cache` in the project; `$CAIRN_DIR` moves the whole `.cairn` folder).

The parts are packages: `cairn.store` (the core), `cairn.client` (HTTP client, the object API, publishing,
the replica, `RemoteStore`), `cairn.server`, `cairn.ui`, `cairn.reports`, and `cairn.runs`; each is
imported only when used.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ._version import __version__
from .store import (BadRange, Conflict, Deleted, FileRead, Forbidden, LocalStore, MissingBlobs, NotFound, Protected, Ref, StoreError,
                    Unauthorized, Unreachable)

if TYPE_CHECKING:
    from .client.objects import Artifact, File, Project, Store, Version
    from .client.remote import RemoteStore

__all__ = ["__version__", "Artifact", "BadRange", "Conflict", "Deleted", "File", "FileRead", "Forbidden", "LocalStore", "MissingBlobs",
           "NotFound", "Project", "Protected",
           "Ref", "RemoteStore", "Store", "StoreError", "Unauthorized", "Unreachable", "Version", "alias", "connect", "lineage",
           "Publication", "open_store", "publish", "pull", "versions"]

_CLIENT = {"RemoteStore": "remote", "open_store": "open", "Publication": "publishing", "publish": "publishing",
           "connect": "objects", "Store": "objects", "Project": "objects", "Artifact": "objects", "Version": "objects",
           "File": "objects"}


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


def pull(ref: str, *, store: "Store | LocalStore | RemoteStore | Path | str | None" = None, record: bool = True,
         offline: bool = False) -> Path:
    """A read-only local copy of an artifact version (or of the file after `//`), recorded as an input.

    `record=False` leaves the inputs file alone: the report builder records a report's inputs in the
    report's own manifest instead. An exact version in the cache needs no server; when the server does
    not answer (or `offline=True`), an alias falls back to its last known resolution, with a warning.
    """
    return _store(store).pull(ref, record=record, offline=offline)


def alias(ref: str, alias: str, *, store: "Store | LocalStore | RemoteStore | Path | str | None" = None) -> None:
    """Point `alias` at the version `ref` names (which must be completed)."""
    version = _store(store).version(ref)
    version.artifact.alias(alias, version)


def versions(name: str, *, store: "Store | LocalStore | RemoteStore | Path | str | None" = None) -> list[dict]:
    """Every version of an artifact (its manifest), oldest first, whatever its status; this machine's pending ones last."""
    return [version.manifest for version in _store(store).artifact(name).versions()]


def lineage(ref: str, *, store: "Store | LocalStore | RemoteStore | Path | str | None" = None) -> dict[str, list[str]]:
    """One level up (`inputs`) and down (`consumers`) from a version, as exact references."""
    version = _store(store).version(ref)
    return {"inputs": version.inputs, "consumers": version.consumers}
