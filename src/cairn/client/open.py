"""The source-of-truth policy: which repo a project gets. The one place that reads `[repo] remote` and
`[repo] allow_network_fs`, looks at the filesystem type, and decides:

    no remote                   StandaloneRepo(LocalRepo)                the repo is the source of truth
    a remote                    SyncedRepo(Remote, LocalRepo copies)     the remote is; the repo is its cache and outbox
    a remote, network fs        SyncedRepo(Remote, NoCopies)             no database: outbox and blobs only
    no remote, network fs       refused, with the cluster setup to use instead

`[repo] allow_network_fs = true` is the owner's word that one process on one host writes the repo: the database
is used as on a local disk. Everything above (the object API, the CLI, reports, runs, the UI) works through the
object this returns (client/repo.py) and never asks which one it is.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from ..config import Config, cairn_base, load_config, protect
from ..store import LocalRepo, StoreError, refs
from ..store.errors import Unreachable
from ..store.fs import network_filesystem, network_fs_message
from .http import TOKEN_ENV, Client
from .remote import Remote
from .repo import NoCopies, ProjectRepo, StandaloneRepo, SyncedRepo

DEFAULT_TIMEOUT = 600.0


def project_name(config: Config) -> str:
    """`[repo] project`, else the project folder's name as a slug."""
    name = config.repo.get("project") or re.sub(r"[^a-z0-9_.-]+", "-", config.project.name.lower()).strip("-.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", name or ""):
        raise SystemExit(f"the project name {name!r} is not a valid slug; set [repo] project in cairn.toml")
    return name


def heartbeat_timeout(config: Config | None) -> float:
    """`[repo] heartbeat_timeout` (seconds; default 600): how long a running version may go without a heartbeat."""
    return float(config.repo.get("heartbeat_timeout", DEFAULT_TIMEOUT)) if config else DEFAULT_TIMEOUT


def remote_url(config: Config | None) -> str | None:
    """`[repo] remote` (None without one)."""
    url = ((config.repo.get("remote") if config else None) or "").strip().rstrip("/")
    return url or None


def repo_root() -> Path:
    """The working directory's repo: `$CAIRN_DIR`, else `[cairn] dir`, else `<project root>/.cairn` (made, with its
    `.gitignore`, on first use). Outside a project, without `$CAIRN_DIR`, there is none (StoreError)."""
    base = cairn_base()
    if base is None:
        raise StoreError("no repo: this folder is not inside a project (no cairn.toml, pixi.toml, or pyproject.toml here or "
                         "above it), and CAIRN_DIR is not set; Cairn keeps everything in the project's .cairn/")
    protect(base)
    base.mkdir(parents=True, exist_ok=True)
    return base


def client(config: Config | None, url: str | None = None, *, token: str | None = None, allow_http: bool = False,
           timeout: float = 120) -> Client:
    """An HTTP client for the remote `url` (default `[repo] remote`); the token is `token`, else `$CAIRN_TOKEN`, else
    `[repo] token_file` (relative to the project folder)."""
    settings = config.repo if config else {}
    url = url or remote_url(config)
    if not url:
        raise SystemExit("no remote: set [repo] remote in cairn.toml")
    if token is None:
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token and settings.get("token_file"):
            token_file = Path(settings["token_file"]).expanduser()
            if not token_file.is_absolute() and config:
                token_file = config.project / token_file
            if not token_file.is_file():
                raise SystemExit(f"the token file {token_file} ([repo] token_file) does not exist")
            if token_file.stat().st_mode & 0o077:
                print(f"warning: {token_file} is readable by other users; run chmod 600 {token_file}", file=sys.stderr)
            token = token_file.read_text().strip()
    # without a token only a server started with --no-auth answers
    return Client(url, token, allow_http=allow_http or bool(settings.get("allow_http")), timeout=timeout)


def _compose(root: Path, remote: Remote | None, config: Config | None, project: str | None, own: bool) -> ProjectRepo:
    """The repo at `root`, with `remote` (None: without one). `own`: the remote is the project's `[repo] remote`, so the
    repo's database holds its copies (a remote named only for this call gets an outbox and blobs, no copies)."""
    protect(root)
    root.mkdir(parents=True, exist_ok=True)
    allowed = bool(config and config.repo.get("allow_network_fs"))
    fstype = None if allowed else network_filesystem(root)
    beat = heartbeat_timeout(config)
    if remote is None:
        if fstype:
            raise StoreError(network_fs_message(root, fstype))
        local = LocalRepo(root, project, allow_network_fs=allowed, heartbeat_timeout=beat)
        if project:
            local.create_project(project)
        return StandaloneRepo(local)
    copies = LocalRepo(root, project, allow_network_fs=allowed, heartbeat_timeout=beat) if own and not fstype else NoCopies()
    return SyncedRepo(remote, root, copies, project, beat)


def open_repo(config: Config | None = None) -> ProjectRepo:
    """The project's repo (`[cairn] dir`, `$CAIRN_DIR`; created if new), with the project as the default for references,
    composed as the module doc says."""
    config = config or load_config()
    project = project_name(config)
    url = remote_url(config)
    remote = Remote(client(config), project) if url else None
    return _compose(Path(config.cairn_dir), remote, config, project, own=True)


def open_remote(url_or_remote: str | Remote, *, token: str | None = None, project: str | None = None) -> ProjectRepo:
    """A remote with the working directory's repo for its outbox and files (and as its cache when it is the project's
    own `[repo] remote`). Needs a repo: inside a project, or `$CAIRN_DIR`."""
    try:
        config = load_config()
        project = project or project_name(config)
    except (SystemExit, ValueError):
        config = None
    remote = url_or_remote if isinstance(url_or_remote, Remote) else Remote(client(config, url_or_remote, token=token), project)
    remote.project = remote.project or project
    return _compose(repo_root(), remote, config, project, own=remote.url == remote_url(config))


def _default_project() -> str | None:
    """The working directory's project name (None outside a project, or with a broken config)."""
    try:
        return project_name(load_config())
    except (SystemExit, ValueError):
        return None


def open_folder(path: Path | str, project: str | None = None) -> ProjectRepo:
    """A repo folder on this machine, opened directly (a server's repo, another project's `.cairn`); the default project
    is the working directory's."""
    return StandaloneRepo(LocalRepo(Path(path).expanduser(), project or _default_project()))


def as_repo(repo) -> ProjectRepo:
    """A project repo from `None` (the project's), a project repo, a LocalRepo or a repo folder (opened directly), or
    a Remote or a remote's URL (with the working directory's repo)."""
    if repo is None:
        return open_repo()
    if isinstance(repo, ProjectRepo):
        return repo
    if isinstance(repo, LocalRepo):
        if repo.project is None:
            repo.project = _default_project()
        return StandaloneRepo(repo)
    if isinstance(repo, Remote) or str(repo).startswith(("http://", "https://")):
        return open_remote(repo)
    return open_folder(repo)


def served_root(config: Config) -> Path:
    """The repo `cairn server` serves when it is given none: the project's own, which must have no remote (it would be
    a copy of another server's)."""
    url = remote_url(config)
    if url:
        raise SystemExit(f"this project's repo syncs with the remote {url}; to serve a repo folder: cairn server <folder>")
    root = Path(config.cairn_dir)
    protect(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def sync_now(config: Config, report=None, timeout: float = 10) -> dict | None:
    """Send what waits for the configured remote, if anything does (the start and end of every repo command). Looks at
    the outbox first, so a command with nothing pending makes no request. Never raises: what is left stays pending;
    a refusal other than not answering is reported."""
    if not remote_url(config):
        return None
    try:
        repo = open_repo(config)
        if not repo.engine.waiting():
            return None
        return repo.with_timeout(timeout).sync(report, refresh=False)
    except Unreachable:
        return None
    except (StoreError, OSError) as error:
        if report:
            report(str(error))
        return None


def versions_of(repo: ProjectRepo, name: str) -> list[dict]:
    """Every version of artifact `name` (project optional), oldest first, whatever its status."""
    parsed = refs.parse(name)
    project = parsed.project or repo.project
    if project is None:
        raise StoreError(f"{name!r} has no project and the repo has no default")
    return repo.versions(project, parsed.name)
