"""Open the store `[store]` in cairn.toml names: a store server (`RemoteStore`) or a local folder (`LocalStore`)."""

from __future__ import annotations

from ..config import Config, load_config, protect
from ..store import LocalStore, StoreError, refs
from .http import connect, project_name, served
from .replica import heartbeat_timeout
from .remote import RemoteStore


def open_store(config: Config | None = None) -> LocalStore | RemoteStore:
    """The store `[store]` in cairn.toml names, with the project as the default for references."""
    config = config or load_config()
    settings = config.store
    project = project_name(config)
    if settings.get("url") or served(config):  # a running `cairn server` on the local folder takes its writes
        return RemoteStore(connect(config), project)
    protect(config.cairn_dir, config.store_path)
    store = LocalStore(config.store_path, project, heartbeat_timeout=heartbeat_timeout(config))
    store.create_project(store.project)
    return store


def as_store(store) -> LocalStore | RemoteStore:
    """A store handle from `None` (the configured store), a store, or a folder path."""
    if store is None:
        return open_store()
    return store if isinstance(store, (LocalStore, RemoteStore)) else LocalStore(store)


def versions_of(store: LocalStore | RemoteStore, name: str) -> list[dict]:
    """Every version of artifact `name` (project optional), oldest first, whatever its status."""
    parsed = refs.parse(name)
    project = parsed.project or store.project
    if project is None:
        raise StoreError(f"{name!r} has no project and the store has no default")
    return store.versions(project, parsed.name)
