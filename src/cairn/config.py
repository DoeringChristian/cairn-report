"""Project configuration: where reports and build output live, the page style, and the repo's remote.

Everything has a default, so most projects need no configuration. To change
something, add a `cairn.toml` at the project root:

    reports_dir = "reports"            # one folder per report: reports/<slug>/report.md
    output_dir = ".cairn/build"        # build output, one folder per report slug (default: <repo>/build)

    [cairn]
    dir = ".cairn"                     # the repo: where Cairn keeps everything for this project (default: .cairn)

    [style]
    css = ["reports/style.css"]        # extra stylesheets, applied after the default style
    template_dir = "reports/template"  # files here replace the packaged page files by name

    [repo]                             # the repo's remote and what the repo publishes
    remote = "https://cairn.example.org/my-study"   # optional: a project on a `cairn server` (<server>/<project>),
                                       # the source of truth for numbers and aliases
    token_file = "~/.config/cairn/token"   # or CAIRN_TOKEN; never the token itself
    allow_http = false                 # send the token over plain HTTP to a non-local remote (default: refuse)
    interval = 30                      # seconds between live syncs of a running version
    heartbeat_timeout = 600            # seconds without a heartbeat before a running version counts as dead
    allow_network_fs = false           # a repo on NFS/Lustre/...: only if one process on one host ever writes it
    exclude = ["*.pkl"]                # files never published: gitignore-style globs on project paths

Finding the repo is one rule (`locate`), which every caller uses:

    $CAIRN_DIR set        that folder is the repo (the project root is still found as below, for cairn.toml)
    otherwise             walk from the working directory upward; the first folder that holds `.cairn/repo.json`
                          (an existing repo, as git finds `.git`) or a `cairn.toml` is
                          the project root, and its repo is `[cairn] dir` (relative to it), else `<root>/.cairn`
    nothing found         the working directory is the project root and `<cwd>/.cairn` its repo: opening the repo
                          creates it (and says so); read-only commands refuse instead (`cairn init` makes one)

The repo ignores itself in git (a `.gitignore` of `*`; the project's own `.gitignore` is never edited) and holds the
metadata, the blobs, the outbox, drafts, and report builds (store/local.py lists the layout).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "cairn.toml"
ROOT_MARKERS = (CONFIG_FILE,)  # besides `.cairn/repo.json` (an existing repo); nothing else marks a project
BASE_ENV = "CAIRN_DIR"
BASE_NAME = ".cairn"
_DIR_CACHE: dict[tuple, str | None] = {}
REPO_KEYS = {"remote", "token_file", "allow_http", "interval", "heartbeat_timeout", "allow_network_fs", "exclude"}


def config_path(root: Path) -> Path | None:
    """The config file of a project root (`cairn.toml`), or None."""
    file = root / CONFIG_FILE
    return file if file.is_file() else None


@dataclass(frozen=True)
class Config:
    project: Path
    reports_dir: Path
    output_dir: Path
    cairn_dir: Path  # the repo
    extra_css: tuple[Path, ...] = field(default_factory=tuple)
    template_dir: Path | None = None
    repo: dict = field(default_factory=dict)  # the [repo] table
    found: bool = True  # False: nothing was found, so the repo is the working directory's `.cairn`, made when opened
    located: str = ""  # how the repo was found, for messages ("found from the working directory", "named by $CAIRN_DIR")

    @property
    def source(self) -> Path | None:
        return config_path(self.project)


@dataclass(frozen=True)
class Location:
    """Where the repo is: `root` the project root, `repo` the repo folder, `marked` whether the walk found a project
    root (or a repo), `found` whether there is a repo to use without creating one in the working directory (found
    by the walk, or named by $CAIRN_DIR), `how` how it was found (for messages)."""
    root: Path
    repo: Path
    marked: bool
    found: bool
    how: str = ""


def _marked(folder: Path) -> bool:
    return (folder / BASE_NAME / "repo.json").is_file() or any((folder / marker).is_file() for marker in ROOT_MARKERS)


def locate(start: Path | str | None = None, root: Path | str | None = None) -> Location:
    """The one rule for finding the repo (see the module doc), from `start` (default: the working directory), or for
    the project root `root` when it is given (`--project`)."""
    here = start is None
    start = Path(start or Path.cwd()).resolve()
    if root is not None:
        folder, marked, how = Path(root).resolve(), True, "of the project folder given"
    else:
        folder = next((candidate for candidate in [start, *start.parents] if _marked(candidate)), None)
        marked = folder is not None
        how = ("found from the working directory" if here else f"found from {start}") if marked else \
            "the working directory's: no project above it"
        folder = folder or start
    if os.environ.get(BASE_ENV):
        return Location(folder, Path(os.environ[BASE_ENV]).expanduser().resolve(), marked, True, "named by $CAIRN_DIR")
    configured = _configured_dir(folder)
    repo = (folder / Path(configured).expanduser()).resolve() if configured else folder / BASE_NAME
    return Location(folder, repo, marked, marked, how)


def find_project_root(start: Path | None = None) -> Path | None:
    """The project root the walk finds from `start` (default: the working directory), else None (`locate`)."""
    found = locate(start)
    return found.root if found.marked else None


def find_project(start: Path | None = None) -> Path:
    """The project root (`locate`): the one found, else the working directory."""
    return locate(start).root


def _configured_dir(root: Path) -> str | None:
    """`[cairn] dir` of the project's config file (None if absent or the file cannot be read; load_config reports errors)."""
    file = root / CONFIG_FILE
    try:
        info = file.stat()
    except OSError:
        return None
    key = (str(file), info.st_mtime_ns, info.st_size)
    if key not in _DIR_CACHE:
        try:
            value = (tomllib.loads(file.read_text()).get("cairn") or {}).get("dir")
        except (OSError, ValueError, AttributeError):
            value = None
        _DIR_CACHE[key] = value if isinstance(value, str) and value else None
    return _DIR_CACHE[key]


def cairn_base(start: Path | None = None) -> Path | None:
    """The repo for the working directory (`locate`); None when there is none to use yet (nothing found)."""
    found = locate(start)
    return found.repo if found.found else None


def protect(base: Path | None, *paths: Path) -> None:
    """Make the base folder and write its `.gitignore` (`*`) if it lacks one; does nothing unless every given
    path lies inside the base. Called wherever a folder inside the base is about to be used. Never raises."""
    if base is None:
        return
    base = Path(base)
    if any(base not in [Path(path), *Path(path).parents] for path in paths):
        return
    try:
        marker = base / ".gitignore"
        if marker.exists():
            return
        base.mkdir(parents=True, exist_ok=True)
        with open(marker, "x") as handle:
            handle.write("*\n")
    except OSError:  # includes a racing writer: the file exists then
        pass


def load_config(project: Path | str | None = None) -> Config:
    """Read cairn.toml (if any) and resolve every path against the project root."""
    location = locate(root=project) if project else locate()
    root = location.root
    data: dict = {}
    source = config_path(root) or root / CONFIG_FILE
    if source.is_file():
        data = tomllib.loads(source.read_text())
    unknown = set(data) - {"reports_dir", "output_dir", "cairn", "style", "repo"}
    if unknown:
        raise ValueError(f"{source}: unknown keys {sorted(unknown)}")
    section = data.get("cairn", {})
    if not isinstance(section, dict) or set(section) - {"dir"} or not isinstance(section.get("dir", ""), str):
        raise ValueError(f"{source}: [cairn] takes dir (text)")
    style = data.get("style", {})
    unknown_style = set(style) - {"css", "template_dir"}
    if unknown_style:
        raise ValueError(f"{source}: unknown [style] keys {sorted(unknown_style)}")

    repo = data.get("repo", {})
    if not isinstance(repo, dict):
        raise ValueError(f"{source}: [repo] must be a table")
    if "token" in repo:
        raise ValueError(f"{source}: never put the token in the config; use [repo] token_file or CAIRN_TOKEN")
    unknown_repo = set(repo) - REPO_KEYS
    if unknown_repo:
        raise ValueError(f"{source}: unknown [repo] keys {sorted(unknown_repo)} (it takes {', '.join(sorted(REPO_KEYS))})")
    for key in ("remote", "token_file"):
        if not isinstance(repo.get(key, ""), str):
            raise ValueError(f"{source}: [repo] {key} must be text")
    for key in ("allow_http", "allow_network_fs"):
        if not isinstance(repo.get(key, False), bool):
            raise ValueError(f"{source}: [repo] {key} must be true or false")
    if not isinstance(repo.get("exclude", []), list) or not all(isinstance(item, str) for item in repo.get("exclude", [])):
        raise ValueError(f"{source}: [repo] exclude must be a list of glob patterns")
    interval = repo.get("interval", 30)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        raise ValueError(f"{source}: [repo] interval must be a positive number of seconds")
    timeout = repo.get("heartbeat_timeout", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"{source}: [repo] heartbeat_timeout must be a positive number of seconds")

    def path(value: str | None, default: str | None) -> Path | None:
        if value is None and default is None:
            return None
        resolved = (root / (value if value is not None else default)).resolve()
        if root not in [resolved, *resolved.parents]:
            raise ValueError(f"{source}: {value!r} points outside the project")
        return resolved

    base = location.repo

    css = tuple(path(item, None) for item in style.get("css", []))
    for sheet in css:
        if not sheet.is_file() or sheet.suffix != ".css":
            raise ValueError(f"{source}: style css {sheet} is not a .css file")
    template_dir = path(style.get("template_dir"), None)
    if template_dir is not None and not template_dir.is_dir():
        raise ValueError(f"{source}: style template_dir {template_dir} is not a folder")
    return Config(
        project=root,
        reports_dir=path(data.get("reports_dir"), "reports"),
        output_dir=path(data.get("output_dir"), None) or base / "build",
        cairn_dir=base,
        found=location.found,
        located=location.how,
        extra_css=css,
        template_dir=template_dir,
        repo=dict(repo),
    )
