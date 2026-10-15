"""Where the UI's data comes from: a repo on this machine (`LocalBackend`: the project's repo, with its remote if it
has one, or a repo folder) or a remote (`RemoteBackend`), both through the repo interface. A backend also decides
which tokens may log in."""

from __future__ import annotations

import hmac
from pathlib import Path

from ..client.http import Client
from ..client.remote import Remote
from ..config import Config, load_config
from ..store import Unauthorized
from ..store.tokens import OWNER, Tokens, init_root, read_token
from .sessions import Session


class LocalBackend:
    """A repo on this machine. Login tokens are the repo's owner token and its scoped tokens; `store` is what every
    session reads through (the project's repo, composed by client/open.py, or the folder opened directly)."""

    def __init__(self, root: Path, no_auth: bool, store=None):
        from ..client.open import open_folder

        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"no repo folder {root}")
        self.root, self.describe = root, str(root)
        self.store = store if store is not None else open_folder(root)
        if not no_auth:
            if not (root / "token").is_file():
                raise SystemExit(f"{root} has no owner token (not a repo?); use --no-auth to browse it without a login")
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
    """A remote (`cairn server`). Login tokens are checked by the remote itself."""

    def __init__(self, url: str, allow_http: bool, token: str = ""):
        self.client = Client(url, token, allow_http=allow_http)  # refuses plain HTTP to a non-local host
        self.url, self.describe, self.allow_http, self.token = self.client.url, self.client.url, allow_http, token

    def _store(self, token: str) -> Remote:
        return Remote(Client(self.url, token, allow_http=self.allow_http))

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


def open_backend(remote: str | None = None, repo: str | Path | None = None, config: Config | None = None, no_auth: bool = False,
                 allow_http: bool = False) -> LocalBackend | RemoteBackend:
    """`--remote URL`, else `--repo FOLDER`, else the project's repo (created, with an owner token, if it is new; it
    reads through the project's remote when it has one)."""
    from ..client.open import client, open_repo

    if remote:
        token = client(config, remote, allow_http=allow_http).token if no_auth else ""  # CAIRN_TOKEN or [repo] token_file
        return RemoteBackend(remote, allow_http or bool(config and config.repo.get("allow_http")), token)
    if repo is not None:
        return LocalBackend(Path(repo), no_auth)
    config = config or load_config()
    root = Path(config.cairn_dir)
    store = open_repo(config)  # makes the repo if it is new
    if not no_auth and not (root / "token").is_file():
        print(f"Created an owner token for the repo {root} ({init_root(root)}, owner-only)", flush=True)
    return LocalBackend(root, no_auth, store)
