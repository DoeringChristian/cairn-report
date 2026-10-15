"""Where the UI's store lives: a folder (`LocalBackend`) or a store server (`RemoteBackend`), both through the
store interface. A backend also decides which tokens may log in."""

from __future__ import annotations

import hmac
from pathlib import Path

from ..client.http import Client, connect
from ..client.remote import RemoteStore
from ..config import Config, load_config, migrate_store, protect
from ..store import LocalStore, Unauthorized
from ..store.tokens import OWNER, Tokens, read_token
from .sessions import Session


class LocalBackend:
    """A store folder. Login tokens are the folder's owner token and its scoped tokens."""

    def __init__(self, root: Path, no_auth: bool):
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"no store folder {root}")
        self.root, self.describe = root, str(root)
        self.store = LocalStore(root)
        if not no_auth:
            if not (root / "token").is_file():
                raise SystemExit(f"{root} has no owner token (not a store root?); use --no-auth to browse it without a login")
            self.owner, self.tokens = read_token(root / "token"), Tokens(root)

    def anonymous(self) -> Session:
        return Session(self.store, None)

    def login(self, token: str) -> Session | None:
        if hmac.compare_digest(token.encode(), self.owner.encode()):
            return Session(self.store, OWNER)
        principal = self.tokens.find(token)
        return Session(self.store, principal) if principal else None

    def valid(self, session: Session) -> bool:
        return session.principal is OWNER or self.tokens.by_id(session.principal.id) is not None


class RemoteBackend:
    """A store server. Login tokens are checked by the server itself."""

    def __init__(self, url: str, allow_http: bool, token: str = ""):
        self.client = Client(url, token, allow_http=allow_http)  # refuses plain HTTP to a non-local host
        self.url, self.describe, self.allow_http, self.token = self.client.url, self.client.url, allow_http, token

    def _store(self, token: str) -> RemoteStore:
        return RemoteStore(Client(self.url, token, allow_http=self.allow_http))

    def anonymous(self) -> Session:
        return Session(self._store(self.token), None)

    def login(self, token: str) -> Session | None:
        store = self._store(token)
        try:
            store.projects()
        except Unauthorized:
            return None
        return Session(store, None)

    def valid(self, session: Session) -> bool:
        return True  # a revoked token fails on its next request (401)


def open_backend(store: str | None = None, config: Config | None = None, no_auth: bool = False,
                 allow_http: bool = False) -> LocalBackend | RemoteBackend:
    """The store `--store` names (a URL or a folder), else the one `[store]` in cairn.toml names."""
    if store is None or not store.startswith(("http://", "https://")):
        if store is None:
            config = config or load_config()
            if config.store.get("url"):
                store = config.store["url"]
            else:
                migrate_store(config)
                protect(config.cairn_dir, config.store_path)
                return LocalBackend(config.store_path, no_auth)
        else:
            return LocalBackend(Path(store), no_auth)
    token = connect(config, store, allow_http).token if no_auth else ""  # CAIRN_STORE_TOKEN or [store] token_file
    return RemoteBackend(store, allow_http or bool(config and config.store.get("allow_http")), token)
