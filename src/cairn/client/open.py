"""Open the store `[store]` in cairn.toml names: a store server (`Remote`) or a local folder (`LocalRepo`)."""

from __future__ import annotations

from ..config import Config, load_config, protect
from ..store import LocalRepo, StoreError, refs
from .http import connect, project_name
from .replica import heartbeat_timeout
from .remote import Remote


def open_repo(config: Config | None = None) -> LocalRepo | Remote:
    """The store `[store]` in cairn.toml names, with the project as the default for references."""
    config = config or load_config()
    settings = config.store
    project = project_name(config)
    if settings.get("url"):
        return Remote(connect(config), project)
    protect(config.cairn_dir, config.store_path)
    store = LocalRepo(config.store_path, project, allow_network_fs=bool(settings.get("allow_network_fs")),
                       heartbeat_timeout=heartbeat_timeout(config))
    store.create_project(store.project)
    return store


def as_repo(store) -> LocalRepo | Remote:
    """A store handle from `None` (the configured store), a store, or a folder path."""
    if store is None:
        return open_repo()
    return store if isinstance(store, (LocalRepo, Remote)) else LocalRepo(store)


def versions_of(store: LocalRepo | Remote, name: str) -> list[dict]:
    """Every version of artifact `name` (project optional), oldest first, whatever its status."""
    parsed = refs.parse(name)
    project = parsed.project or store.project
    if project is None:
        raise StoreError(f"{name!r} has no project and the store has no default")
    return store.versions(project, parsed.name)
