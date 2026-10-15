"""The store UI: `cairn ui` explores an artifact store in a browser (see server.py)."""

from __future__ import annotations

from pathlib import Path

from ..config import Config, load_config
from ..push import connect
from .server import LocalBackend, RemoteBackend, make_ui_server, serve_ui

__all__ = ["LocalBackend", "RemoteBackend", "make_ui_server", "open_backend", "serve_ui"]


def open_backend(store: str | None = None, config: Config | None = None, no_auth: bool = False,
                 allow_http: bool = False) -> LocalBackend | RemoteBackend:
    """The store `--store` names (a URL or a folder), else the one `[store]` in cairn.toml names."""
    if store is None or not store.startswith(("http://", "https://")):
        if store is None:
            config = config or load_config()
            if config.store.get("url"):
                store = config.store["url"]
            else:
                root = Path(config.store.get("path", ".store")).expanduser()
                return LocalBackend(root if root.is_absolute() else config.project / root, no_auth)
        else:
            return LocalBackend(Path(store), no_auth)
    token = connect(config, store, allow_http).token if no_auth else ""  # CAIRN_STORE_TOKEN or [store] token_file
    return RemoteBackend(store, allow_http or bool(config and config.store.get("allow_http")), token)
