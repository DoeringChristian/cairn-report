"""Cairn: an artifact store for run outputs and reports, with a server and a UI, plus static reports and a run logger.

This is the light API (no server, UI, reports, matplotlib, plotly, or hydra is imported):

    import cairn

    crops = cairn.pull("datasets/crops@v3")        # read-only local copy; the exact version is recorded as an input
    cairn.publish("models/denoiser", "checkpoints/best", metadata={"psnr": 31.2}, aliases=["best"])
    cairn.alias("models/denoiser@v4", "paper")
    cairn.versions("descent")
    cairn.lineage("reports/boundary@v2")

`store=None` means the configured store (`[store]` in cairn.toml): a store server when `url`
is set (`RemoteStore`; the token comes from CAIRN_STORE_TOKEN or `token_file`), else a local folder,
`path` (default `.store` in the project root). `project` names the default project (default: the
project folder's slug). Every `pull` appends the exact version to `$CAIRN_INPUTS`, else to
`.cairn-inputs.jsonl` in the working directory; `publish` carries a folder's `.cairn-inputs.jsonl`
along as the version's `inputs`, which is how lineage is recorded without any framework hook.

The parts are packages: `cairn.store` (the core), `cairn.client` (HTTP client, publishing, `RemoteStore`),
`cairn.server`, `cairn.ui`, `cairn.reports`, and `cairn.runs`; each is imported only when used.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ._version import __version__
from .store import (BadRange, FileRead, Forbidden, LocalStore, NotFound, Ref, StoreError, Unauthorized, pull as _pull)

if TYPE_CHECKING:
    from .client.remote import RemoteStore

__all__ = ["__version__", "BadRange", "FileRead", "Forbidden", "LocalStore", "NotFound", "Ref", "RemoteStore", "StoreError",
           "Unauthorized", "alias", "lineage", "Publication", "open_store", "publish", "pull", "versions"]

_CLIENT = {"RemoteStore": "remote", "open_store": "open", "Publication": "publishing", "publish": "publishing"}


def __getattr__(name: str):
    """The names that need the HTTP client load it on first use (`import cairn` stays light)."""
    if name in _CLIENT:
        from importlib import import_module

        return getattr(import_module(f".client.{_CLIENT[name]}", __name__), name)
    raise AttributeError(f"module 'cairn' has no attribute {name!r}")


def _store(store):
    from .client.open import as_store

    return as_store(store)


def pull(ref: str, *, store: "LocalStore | RemoteStore | Path | str | None" = None, record: bool = True) -> Path:
    """A read-only local copy of an artifact version (or of the file after `//`), recorded as an input.

    `record=False` leaves the inputs file alone: the report builder records a report's inputs in the
    report's own manifest instead.
    """
    return _pull(_store(store), ref, record)


def alias(ref: str, alias: str, *, store: "LocalStore | RemoteStore | Path | str | None" = None) -> None:
    """Point `alias` at the version `ref` names (which must be completed)."""
    store = _store(store)
    project, name, version = store.resolve(ref)
    store.set_alias(project, name, alias, version["version"])


def versions(name: str, *, store: "LocalStore | RemoteStore | Path | str | None" = None) -> list[dict]:
    """Every version of an artifact, oldest first, whatever its status."""
    from .client.open import versions_of

    return versions_of(_store(store), name)


def lineage(ref: str, *, store: "LocalStore | RemoteStore | Path | str | None" = None) -> dict[str, list[str]]:
    """One level up (`inputs`) and down (`consumers`) from a version, as exact references."""
    return _store(store).lineage(ref)
