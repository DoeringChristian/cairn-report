"""The source-of-truth policy: which repo a project gets. The one place that reads `[remote] url` and
`[repo] allow_network_fs`, looks at the filesystem type, and decides:

    no remote                   StandaloneRepo(LocalProject)                the repo is the source of truth
    a remote                    SyncedRepo(Remote, LocalProject copies)     the remote is; the repo is its cache and outbox
    a remote, network fs        SyncedRepo(Remote, NoCopies)                no database: outbox and blobs only
    no remote, network fs       refused, with the cluster setup to use instead

The repo holds the project's one project (store/local.py OWN). The remote is a project on a server:
`[remote] url = "<server>/<project>"` (`cairn remote set`), whose last path segment is the project (`http.split_remote`); a remote
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

from ..config import Config, load_config, locate, protect, write_template
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
    """`[remote] url` (None without one): `<server>/<project>`."""
    url = ((config.repo.get("remote") if config else None) or "").strip().rstrip("/")
    return url or None


def remote_parts(url: str, what: str = "[remote] url") -> tuple[str, str]:
    """(server URL, project) of a remote URL; SystemExit when it names no project (or not a valid one)."""
    server, project = split_remote(url)
    if project is None:
        raise SystemExit(f"cairn: {what} {url!r} names no project: a remote is <server>/<project>, e.g. "
                         f"{url.rstrip('/')}/my-study")
    if not refs.PROJECT.fullmatch(project):
        raise SystemExit(f"cairn: {what} {url!r}: its last path segment {project!r} is not a project name (lowercase "
                         "letters, digits, '_', '.', '-')")
    return server, project


def valid_remote(url: str) -> str:
    """A remote URL as `cairn remote set` stores it: `http(s)://host[:port][/prefix]/<project>` (SystemExit otherwise)."""
    from urllib.parse import urlparse

    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.params:
        raise SystemExit(f"cairn: {url!r} is not a remote: it looks like http(s)://host[:port][/prefix]/<project>")
    try:
        parsed.port
    except ValueError:
        raise SystemExit(f"cairn: {url!r} has an invalid port") from None
    remote_parts(url, "the remote")
    return url


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
    if new:
        write_template(repo)
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


def home_token() -> Path:
    """`~/.config/cairn/token`: the token of every project of this user (the last place looked)."""
    return Path.home() / ".config" / "cairn" / "token"


def _read_token(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        print(f"warning: {path} is readable by other users; run chmod 600 {path}", file=sys.stderr)
    return path.read_text().strip()


def find_token(config: Config | None) -> tuple[str, str | None, str]:
    """(token, where it came from, where it was looked for) for a client of the project's remote; the first found wins:

        1. $CAIRN_TOKEN
        2. the repo's `token` file (`<repo>/token`, normally .cairn/token)
        3. ~/.config/cairn/token

    So copying the server's token file into the project's `.cairn/` or into `~/.config/cairn/` is all a machine needs.
    A token file readable by other users is used with a warning. ("", None, places) when there is none. (`<repo>/token`
    is also the owner token when `cairn server` serves that repo; the server itself only ever reads its own repo's.)"""
    files = ([Path(config.cairn_dir) / "token"] if config is not None else []) + [home_token()]
    places = f"looked in ${TOKEN_ENV}, {', '.join(str(path) for path in files)}"
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token, f"${TOKEN_ENV}", places
    for path in files:
        if path.is_file():
            token = _read_token(path)
            if token:
                return token, str(path), places
    return "", None, places


def client(config: Config | None, url: str | None = None, *, token: str | None = None, timeout: float = 120) -> Client:
    """An HTTP client for the server `url` (default: the server of `[remote] url`); the token is `token`, else the
    first found of `find_token`. Without one only a server started with --no-auth answers, and a refusal lists where
    a token was looked for."""
    url = url or (remote_parts(remote_url(config))[0] if remote_url(config) else None)
    if not url:
        raise SystemExit("no remote: `cairn remote set <server>/<project>` sets one")
    source, places = "the caller", None
    if token is None:
        token, source, places = find_token(config)
    return Client(url, token, timeout=timeout, token_source=source if token else None, token_places=places)


def where(config: Config | None, root: Path) -> str:
    """What messages say about where the repo's versions were looked for: `the repo <root> (how it was found)`."""
    how = config.located if config and config.located and Path(config.cairn_dir).resolve() == Path(root).resolve() else ""
    return f"the repo {root}" + (f" ({how})" if how else "")


def _compose(root: Path, remote: Remote | None, config: Config | None, own: bool) -> ProjectRepo:
    """The repo at `root`, with `remote` (None: without one). `own`: the repo is the project's (`[remote] url` is its
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
    """The project's repo (`.cairn`, or `$CAIRN_DIR`; created if new), composed as the module doc says."""
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


def open_server(url: str, *, token: str | None = None) -> Server:
    """A server (`https://host[/prefix]`): its projects, for an admin or `cairn ui --remote <server>`."""
    return Server(client(_config(), url, token=token))


def open_remote(url: str, *, token: str | None = None) -> ProjectRepo:
    """The remote project `url` (`<server>/<project>`) with the working directory's repo for its outbox and files:
    the project's own repo when `url` is its `[remote] url`, else a repo that keeps no copies and records no inputs
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


def own_project(directory: Path | None, root: Path) -> str | None:
    """The name a repo folder's own project is served under when the folder is the project's repo (`cairn server
    <root>`): the project folder's name; None for any other folder (a server's repo)."""
    try:
        config = load_config(directory)
    except (SystemExit, ValueError):
        return None
    if Path(config.cairn_dir).resolve() != Path(root).resolve() or remote_url(config):
        return None
    return project_label(config)


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
