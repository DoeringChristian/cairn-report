"""The source-of-truth policy: which repo a project gets. The one place that reads `[repo] remote` and
`[repo] allow_network_fs`, looks at the filesystem type, and decides:

    no remote                   StandaloneRepo(LocalProject)                the repo is the source of truth
    a remote                    SyncedRepo(Remote, LocalProject copies)     the remote is; the repo is its cache and outbox
    a remote, network fs        SyncedRepo(Remote, NoCopies)                no database: outbox and blobs only
    no remote, network fs       refused, with the cluster setup to use instead

The repo holds the project's one project (store/local.py OWN). The remote is a project on a server:
`[repo] remote = "<server>/<project>"`, whose last path segment is the project (`http.split_remote`); a remote
without one is a configuration error. `[repo] allow_network_fs = true` is the owner's word that one process on one
host writes the repo: the database is used as on a local disk. Everything above (the object API, the CLI, reports,
runs, the UI) works through the object this returns (client/repo.py) and never asks which one it is.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from ..config import Config, load_config, locate, protect
from ..store import LocalProject, LocalRepo, StoreError, refs
from ..store.errors import Unreachable
from ..store.fs import network_filesystem, network_fs_message
from .http import TOKEN_ENV, Client, split_remote
from .remote import Remote, Server
from .repo import NoCopies, ProjectRepo, StandaloneRepo, SyncedRepo

DEFAULT_TIMEOUT = 600.0


def slug(text: str) -> str:
    """A folder name as a project name (lowercase letters, digits, '_', '.', '-')."""
    return re.sub(r"[^a-z0-9_.-]+", "-", text.lower()).strip("-.") or "project"


def heartbeat_timeout(config: Config | None) -> float:
    """`[repo] heartbeat_timeout` (seconds; default 600): how long a running version may go without a heartbeat."""
    return float(config.repo.get("heartbeat_timeout", DEFAULT_TIMEOUT)) if config else DEFAULT_TIMEOUT


def remote_url(config: Config | None) -> str | None:
    """`[repo] remote` (None without one): `<server>/<project>`."""
    url = ((config.repo.get("remote") if config else None) or "").strip().rstrip("/")
    return url or None


def remote_parts(url: str, what: str = "[repo] remote") -> tuple[str, str]:
    """(server URL, project) of a remote URL; SystemExit when it names no project (or not a valid one)."""
    server, project = split_remote(url)
    if project is None:
        raise SystemExit(f"cairn: {what} = {url!r} names no project: a remote is <server>/<project>, e.g. "
                         f"{url.rstrip('/')}/my-study")
    if not refs.PROJECT.fullmatch(project):
        raise SystemExit(f"cairn: {what} = {url!r}: its last path segment {project!r} is not a project name (lowercase "
                         "letters, digits, '_', '.', '-')")
    return server, project


def project_label(config: Config) -> str:
    """The name the project's one project is shown under (`cairn ui`, `cairn server` serving the project's repo): the
    remote's project, else the project folder's name."""
    url = remote_url(config)
    return remote_parts(url)[1] if url else slug(config.project.name)


def make_repo_folder(repo: Path, found: bool) -> Path:
    """Make the repo folder (with its `.gitignore`) if it is new. When nothing was found (`config.locate`: no project
    or repo above the working directory, no $CAIRN_DIR), the new repo is the working directory's: one line on stderr
    says so."""
    repo = Path(repo)
    new = not repo.exists()
    protect(repo)
    repo.mkdir(parents=True, exist_ok=True)
    if new and not found:
        print(f"cairn: created the repo {repo} (no repo or project above {Path.cwd()}; `cairn init` makes one on purpose)",
              file=sys.stderr, flush=True)
    return repo


def repo_root() -> Path:
    """The working directory's repo (`config.locate`), made if it is new (see `make_repo_folder`)."""
    found = locate()
    return make_repo_folder(found.repo, found.found)


def existing_repo(config: Config) -> Path:
    """The project's repo for a command that only looks at it (`cairn status`, `verify`, `prune`, `sync`): it must exist
    already (SystemExit otherwise: nothing is created)."""
    repo = Path(config.cairn_dir)
    if not (repo / "repo.json").is_file():
        raise SystemExit(f"cairn: no repo here ({repo} does not exist; run `cairn init`)")
    return repo


def client(config: Config | None, url: str | None = None, *, token: str | None = None, allow_http: bool = False,
           timeout: float = 120) -> Client:
    """An HTTP client for the server `url` (default: the server of `[repo] remote`); the token is `token`, else
    `$CAIRN_TOKEN`, else `[repo] token_file` (relative to the project folder)."""
    settings = config.repo if config else {}
    url = url or (remote_parts(remote_url(config))[0] if remote_url(config) else None)
    if not url:
        raise SystemExit("no remote: set [repo] remote in cairn.toml")
    source = "the caller"
    if token is None:
        token, source = os.environ.get(TOKEN_ENV, "").strip(), f"${TOKEN_ENV}"
        if not token and settings.get("token_file"):
            token_file = Path(settings["token_file"]).expanduser()
            if not token_file.is_absolute() and config:
                token_file = config.project / token_file
            if not token_file.is_file():
                raise SystemExit(f"the token file {token_file} ([repo] token_file) does not exist")
            if token_file.stat().st_mode & 0o077:
                print(f"warning: {token_file} is readable by other users; run chmod 600 {token_file}", file=sys.stderr)
            token, source = token_file.read_text().strip(), str(token_file)
    # without a token only a server started with --no-auth answers
    return Client(url, token, allow_http=allow_http or bool(settings.get("allow_http")), timeout=timeout,
                  token_source=source if token else None, token_places=f"set ${TOKEN_ENV} or [repo] token_file")


def where(config: Config | None, root: Path) -> str:
    """What messages say about where the repo's versions were looked for: `the repo <root> (how it was found)`."""
    how = config.located if config and config.located and Path(config.cairn_dir).resolve() == Path(root).resolve() else ""
    return f"the repo {root}" + (f" ({how})" if how else "")


def _compose(root: Path, remote: Remote | None, config: Config | None, own: bool) -> ProjectRepo:
    """The repo at `root`, with `remote` (None: without one). `own`: the repo is the project's (`[repo] remote` is its
    remote), so its database holds the remote's copies and its pulls are the run's inputs; a remote named only for
    this call gets an outbox and blobs, no copies."""
    make_repo_folder(root, config.found if config else True)
    allowed = bool(config and config.repo.get("allow_network_fs"))
    fstype = None if allowed else network_filesystem(root)
    beat = heartbeat_timeout(config)
    if remote is None:
        if fstype:
            raise StoreError(network_fs_message(root, fstype))
        repo = StandaloneRepo(LocalProject(LocalRepo(root, allow_network_fs=allowed, heartbeat_timeout=beat,
                                                     where=where(config, root))))
        repo.own = own
        return repo
    copies = LocalProject(LocalRepo(root, allow_network_fs=allowed, heartbeat_timeout=beat, where=where(config, root))) \
        if own and not fstype else NoCopies()
    repo = SyncedRepo(remote, root, copies, beat)
    repo.own = own
    return repo


def open_repo(config: Config | None = None) -> ProjectRepo:
    """The project's repo (`[cairn] dir`, `$CAIRN_DIR`; created if new), composed as the module doc says."""
    config = config or load_config()
    url = remote_url(config)
    remote = None
    if url:
        server, project = remote_parts(url)
        remote = Remote(client(config, server), project)
    return _compose(Path(config.cairn_dir), remote, config, own=True)


def _config() -> Config | None:
    try:
        return load_config()
    except (SystemExit, ValueError):
        return None


def open_server(url: str, *, token: str | None = None, allow_http: bool = False) -> Server:
    """A server (`https://host[/prefix]`): its projects, for an admin or `cairn ui --remote <server>`."""
    return Server(client(_config(), url, token=token, allow_http=allow_http))


def open_remote(url: str, *, token: str | None = None) -> ProjectRepo:
    """The remote project `url` (`<server>/<project>`) with the working directory's repo for its outbox and files:
    the project's own repo when `url` is its `[repo] remote`, else a repo that keeps no copies and records no inputs
    (another project's versions never become inputs of this project's runs)."""
    config = _config()
    own = config is not None and url.rstrip("/") == remote_url(config)
    if own and token is None:
        return open_repo(config)
    server, project = remote_parts(url, "the remote")
    remote = Remote(client(config, server, token=token), project)
    return _compose(Path(config.cairn_dir) if config else repo_root(), remote, config, own=own)


def open_folder(path: Path | str) -> ProjectRepo:
    """A repo folder on this machine, opened directly (another project's `.cairn`, in tests or for an admin). Unless it
    is the project's own repo, its pulls record no inputs (another project's versions never become inputs here)."""
    folder = Path(path).expanduser()
    config = _config()
    own = config is not None and folder.resolve() == Path(config.cairn_dir).resolve()
    repo = StandaloneRepo(LocalProject(LocalRepo(folder, where=where(config, folder) if own else None)))
    repo.own = own
    return repo


def as_repo(repo) -> ProjectRepo:
    """A project repo from `None` (the project's), a project repo, a repo folder (opened directly), or a remote's URL
    `<server>/<project>` (with the working directory's repo)."""
    if repo is None:
        return open_repo()
    if isinstance(repo, ProjectRepo):
        return repo
    if str(repo).startswith(("http://", "https://")):
        return open_remote(str(repo))
    return open_folder(repo)


def served_root(config: Config) -> tuple[Path, str]:
    """The repo `cairn server` serves when it is given none: the project's own, which must have no remote (it would be
    a copy of another server's), and the name its one project is served under (the project folder's)."""
    url = remote_url(config)
    if url:
        raise SystemExit(f"this project's repo syncs with the remote {url}; to serve a repo folder: cairn server <folder>")
    return make_repo_folder(Path(config.cairn_dir), config.found), project_label(config)


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
        repo.reach()  # a remote that does not answer costs one short look (and none at the end of the command)
        return repo.with_timeout(timeout).sync(report, refresh=False, deadline=time.monotonic() + timeout)
    except Unreachable:
        return None
    except (StoreError, OSError) as error:
        if report:
            report(str(error))
        return None
