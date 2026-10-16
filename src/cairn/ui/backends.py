"""Where the UI's data comes from, as projects: the project's repo on this machine (one project, named after the
project folder or the remote's project: mode `local`), a repo folder on this machine or a server (its projects: mode
`server`), or one project on a server (`--remote <server>/<project>`: mode `local`). Every project is reached through
the repo interface without a project in its calls (store/interface.py). A backend also decides which token may log in.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from ..client.http import Client, ClientError, split_remote
from ..client.remote import Remote, Server
from ..config import Config, load_config
from ..store import OWN, LocalProject, LocalRepo, NotFound, Unauthorized, refs
from ..store.tokens import read_token
from ..term import short_path
from .sessions import Session


def _overview(name: str, repo) -> dict:
    """A project record with its artifact count and last update, from its repo."""
    try:
        record = dict(repo.record())
    except NotFound:
        record = {"title": name, "description": "", "created": ""}
    summaries = repo.summaries()
    record.update(name=name, title=record.get("title") or name, artifacts=sum(1 for item in summaries if item.get("versions")))
    record["updated"] = max([item.get("updated") or "" for item in summaries] + [record.get("created") or ""])
    return record


class OneProject:
    """One project, under the name `name`: the project's repo (a client/repo.py ProjectRepo) or a Remote."""

    mode = "local"

    def __init__(self, name: str, repo):
        self.name, self.repo = name, repo

    def overviews(self) -> list[dict]:
        return [_overview(self.name, self.repo)]

    def get(self, project: str):
        if project != self.name:
            raise NotFound(f"no project {project!r}: this UI shows the project {self.name!r}")
        return self.repo

    def record(self, project: str) -> dict:
        return {**self.get(project).record(), "name": self.name, "title": self.get(project).record().get("title") or self.name}


class FolderProjects:
    """The projects of a repo folder on this machine (a server's repo): each as a LocalProject; the folder's own
    project (OWN) under `own`, if it has one."""

    mode = "server"

    def __init__(self, local: LocalRepo, own: str | None = None):
        self.local, self.own = local, own

    def _key(self, project: str) -> str:
        return OWN if self.own is not None and project == self.own else refs.valid_project(project)

    def overviews(self) -> list[dict]:
        found = []
        for item in self.local.overviews():
            if item["name"] == OWN:
                if self.own is None or not item.get("artifacts"):
                    continue
                item = {**item, "name": self.own, "title": item.get("title") or self.own}
            found.append(item)
        return found

    def get(self, project: str) -> LocalProject:
        view = LocalProject(self.local, self._key(project))
        view.record()  # NotFound for a project the folder does not have
        return view

    def record(self, project: str) -> dict:
        record = self.get(project).record()
        return {**record, "name": project, "title": record.get("title") or project}


class ServerProjects:
    """The projects of a server, each as a Remote."""

    mode = "server"

    def __init__(self, server: Server):
        self.server = server

    def overviews(self) -> list[dict]:
        return self.server.projects()

    def get(self, project: str) -> Remote:
        return self.server.project(project)

    def record(self, project: str) -> dict:
        return self.get(project).record()


class LocalBackend:
    """A repo on this machine. The login token is the repo's token (`owner`); `store` is what every session reads
    through: the project's repo as its one project, or a repo folder's projects."""

    def __init__(self, root: Path, no_auth: bool, store=None, owner: str | None = None):
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"cairn: no repo folder {short_path(root)}")
        self.root, self.describe = root, short_path(root)
        self.store = store if store is not None else FolderProjects(LocalRepo(root, where=""), _own_name(root))
        self.owner = ""
        if not no_auth and owner is not None:  # a project's own UI (see open_backend)
            self.owner = owner
        elif not no_auth:
            if not (root / "token").is_file():
                raise SystemExit(f"cairn: {short_path(root)} has no owner token (not a repo?); --no-auth browses it without a login")
            self.owner = read_token(root / "token")

    def anonymous(self) -> Session:
        return Session(self.store)

    def login(self, token: str) -> Session | None:
        return Session(self.store) if self.owner and hmac.compare_digest(token.encode(), self.owner.encode()) else None


def _own_name(root: Path) -> str | None:
    """The name a repo folder's own project is shown under: the folder around a `.cairn` (a project's repo)."""
    from ..client.open import slug

    return slug(root.parent.name) if root.name == ".cairn" else None


class RemoteBackend:
    """A server (`url`: its projects) or one project on it (`url` = `<server>/<project>`). Login tokens are checked by
    the server itself."""

    def __init__(self, url: str, token: str = ""):
        self.server_url, self.project = _server_or_project(url)
        self.client = Client(self.server_url, token)
        self.url = self.describe = url.rstrip("/")
        self.token = token

    def _store(self, token: str):
        server = Server(Client(self.server_url, token, token_source="the login"))
        return OneProject(self.project, server.project(self.project)) if self.project else ServerProjects(server)

    def anonymous(self) -> Session:
        return Session(self._store(self.token))

    def login(self, token: str) -> Session | None:
        store = self._store(token)
        try:
            store.overviews()
        except Unauthorized:
            return None
        return Session(store)


def _server_or_project(url: str) -> tuple[str, str | None]:
    """(server URL, project or None) of `--remote URL`: a URL with a path is a project on a server
    (`<server>/<project>`) unless the server answers at the URL itself (a server behind a path prefix)."""
    server, project = split_remote(url)
    if project is None:
        return server, None
    try:
        Client(url, "", timeout=10).json("GET", "/api/v2/repo")
        return url.rstrip("/"), None  # the API answers here: a server behind a path prefix
    except ClientError as error:
        if error.status == 401:
            return url.rstrip("/"), None
    except SystemExit:
        raise
    return server, refs.valid_project(project)


def open_backend(remote: str | None = None, repo: str | Path | None = None, config: Config | None = None,
                 no_auth: bool = False) -> LocalBackend | RemoteBackend:
    """`--remote URL`, else `--repo FOLDER`, else the project's repo (created, with an owner token, if it is new; it
    reads through the project's remote when it has one, and shows its one project under the project folder's name, or
    the remote's project)."""
    from ..client.open import client, open_repo, project_label

    if remote:
        token = client(config, remote).token if no_auth else ""  # the first token open.find_token finds
        return RemoteBackend(remote, token)
    if repo is not None:
        return LocalBackend(Path(repo), no_auth)
    config = config or load_config()
    root = Path(config.cairn_dir)
    store = open_repo(config)  # makes the repo if it is new
    # The project's own UI never writes <repo>/token: that file is a client's token for the remote (and a server's
    # owner token), so a token made here would be sent to the remote. Without one, it logs in with <repo>/ui-token,
    # made once (owner-only) and never sent anywhere, so a browser's session outlives a restart of the UI.
    owner = None if (root / "token").is_file() else ui_token(root)
    return LocalBackend(root, no_auth, OneProject(project_label(config), store), owner=owner)


def ui_token(root: Path) -> str:
    """`<repo>/ui-token`: the login token of a project's own UI when the repo has no `token`, made on first use."""
    path = Path(root) / "ui-token"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return read_token(path)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(secrets.token_urlsafe(32) + "\n")
    return read_token(path)
