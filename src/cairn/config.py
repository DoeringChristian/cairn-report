"""Project configuration: where reports, runs, and build output live, and the page style.

Everything has a default, so most projects need no configuration. To change
something, add a `cairn.toml` at the project root:

    reports_dir = "reports"            # the root report: report.md, assets/, child report folders
    output_dir = ".cairn/build"        # build output, one folder per report slug (default: <cairn dir>/build)

    [cairn]
    dir = ".cairn"                     # where Cairn keeps its files for this project (default: .cairn)

    [style]
    css = ["reports/style.css"]        # extra stylesheets, applied after the default style
    template_dir = "reports/template"  # files here replace the packaged page files by name

    [store]                            # the artifact store `cairn` reads and writes
    path = ".cairn/store"              # a local store folder (default: <cairn dir>/store), or:
    url = "https://store.example.org"  # a store server (`cairn server`)
    project = "my-study"               # default: the project folder name
    token_file = "~/.config/cairn/store-token"  # or CAIRN_STORE_TOKEN; never the token itself
    allow_http = false                 # send the token over plain HTTP to a non-local server (default: refuse)
    exclude = ["*.pkl"]                # files never published: gitignore-style globs on project paths
    interval = 30                      # seconds between syncs of `cairn publish --watch`
    heartbeat_timeout = 600            # seconds without a heartbeat before a running version counts as dead
    allow_network_fs = false           # a path on NFS/Lustre/...: only if one process on one host ever writes it
    publish = [{ folder = "runs/descent/latest", name = "descent" }]  # folders `cairn publish` syncs

The project root is the nearest folder, starting at the working directory, that
contains `cairn.toml`, `pixi.toml`, or `pyproject.toml`. (A `cairn-report.toml` is an error: rename it to `cairn.toml`.)

Where Cairn keeps things. Everything Cairn writes for a project goes into one folder, the base:

    <base>/.gitignore   "*" (written whenever the folder is created; the project's own .gitignore is never edited)
    <base>/store/       the local store (when [store] has no url)
    <base>/pending/     the data root: versions not on the store server yet, their blobs, the known servers
    <base>/cache/       the cache root: pulled versions, downloaded blobs, hash caches (throwaway)
    <base>/build/       built report pages

Resolution, first match wins:

    base          $CAIRN_DIR, else `[cairn] dir` in cairn.toml (relative to the project root), else
                  `<project root>/.cairn` when a project root is found, else none
    data root     `--data-dir`, else $CAIRN_DATA_DIR, else
                  `<base>/pending`, else `$XDG_DATA_HOME/cairn` (~/.local/share/cairn)
    cache root    $CAIRN_CACHE_DIR, else `<base>/cache`, else `$XDG_CACHE_HOME/cairn` (~/.cache/cairn)
    local store   `[store] path` if set, else `<base>/store`
    build output  `output_dir` if set, else `<base>/build`

The data and cache roots are looked up from the working directory (its project root); `load_config(project)`
resolves the store and build folders for that project.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "cairn.toml"
OLD_CONFIG_FILE = "cairn-report.toml"  # the name before Cairn: never read, only recognized to say what to do
ROOT_MARKERS = (CONFIG_FILE, OLD_CONFIG_FILE, "pixi.toml", "pyproject.toml")
BASE_ENV = "CAIRN_DIR"
BASE_NAME = ".cairn"
_DIR_CACHE: dict[tuple, str | None] = {}


def config_path(root: Path) -> Path | None:
    """The config file of a project root (`cairn.toml`), or None. A `cairn-report.toml` without it is an error."""
    file = root / CONFIG_FILE
    if file.is_file():
        return file
    if (root / OLD_CONFIG_FILE).is_file():
        raise ValueError(f"{root / OLD_CONFIG_FILE} is the config file of cairn-report: rename it to {CONFIG_FILE}")
    return None


@dataclass(frozen=True)
class Config:
    project: Path
    reports_dir: Path
    output_dir: Path
    cairn_dir: Path
    store_path: Path
    extra_css: tuple[Path, ...] = field(default_factory=tuple)
    template_dir: Path | None = None
    store: dict = field(default_factory=dict)

    @property
    def source(self) -> Path | None:
        return config_path(self.project)


def find_project_root(start: Path | None = None) -> Path | None:
    """The nearest folder from `start` (default: the working directory) up that holds a root marker, else None."""
    start = Path(start or Path.cwd()).resolve()
    for folder in [start, *start.parents]:
        if any((folder / marker).is_file() for marker in ROOT_MARKERS):
            return folder
    return None


def find_project(start: Path | None = None) -> Path:
    return find_project_root(start) or Path(start or Path.cwd()).resolve()


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


def base_for(root: Path) -> Path:
    """The base folder of a project root: $CAIRN_DIR, else `[cairn] dir` (relative to the root), else `<root>/.cairn`."""
    if os.environ.get(BASE_ENV):
        return Path(os.environ[BASE_ENV]).expanduser().resolve()
    configured = _configured_dir(root)
    return (root / Path(configured).expanduser()).resolve() if configured else root / BASE_NAME


def cairn_base(start: Path | None = None) -> Path | None:
    """The base folder for the working directory's project (see the module doc); None outside any project
    unless $CAIRN_DIR is set."""
    root = find_project_root(start)
    if root is not None:
        return base_for(root)
    return Path(os.environ[BASE_ENV]).expanduser().resolve() if os.environ.get(BASE_ENV) else None


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
    root = Path(project).resolve() if project else find_project()
    data: dict = {}
    source = config_path(root) or root / CONFIG_FILE
    if source.is_file():
        data = tomllib.loads(source.read_text())
    unknown = set(data) - {"reports_dir", "output_dir", "cairn", "style", "store"}
    if unknown:
        raise ValueError(f"{source}: unknown keys {sorted(unknown)}")
    section = data.get("cairn", {})
    if not isinstance(section, dict) or set(section) - {"dir"} or not isinstance(section.get("dir", ""), str):
        raise ValueError(f"{source}: [cairn] takes dir (text)")
    style = data.get("style", {})
    unknown_style = set(style) - {"css", "template_dir"}
    if unknown_style:
        raise ValueError(f"{source}: unknown [style] keys {sorted(unknown_style)}")

    store = data.get("store", {})
    if "token" in store:
        raise ValueError(f"{source}: never put the store token in the config; use token_file or CAIRN_STORE_TOKEN")
    unknown_store = set(store) - {"path", "url", "project", "token_file", "exclude", "publish", "interval", "allow_http",
                                     "heartbeat_timeout", "allow_network_fs"}
    if unknown_store:
        raise ValueError(f"{source}: unknown [store] keys {sorted(unknown_store)}")
    for key in ("path", "url", "project", "token_file"):
        if not isinstance(store.get(key, ""), str):
            raise ValueError(f"{source}: [store] {key} must be text")
    for key in ("allow_http", "allow_network_fs"):
        if not isinstance(store.get(key, False), bool):
            raise ValueError(f"{source}: [store] {key} must be true or false")
    if not isinstance(store.get("exclude", []), list) or not all(isinstance(item, str) for item in store.get("exclude", [])):
        raise ValueError(f"{source}: [store] exclude must be a list of glob patterns")
    if isinstance(store.get("interval", 30), bool) or not isinstance(store.get("interval", 30), (int, float)):
        raise ValueError(f"{source}: [store] interval must be a number of seconds")
    timeout = store.get("heartbeat_timeout", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"{source}: [store] heartbeat_timeout must be a positive number of seconds")
    mappings = store.get("publish", [])
    if not isinstance(mappings, list) or not all(isinstance(item, dict) and set(item) == {"folder", "name"}
                                                 and all(isinstance(value, str) for value in item.values()) for item in mappings):
        raise ValueError(f"{source}: [store] publish must be a list of {{ folder = ..., name = ... }} tables")

    def path(value: str | None, default: str | None) -> Path | None:
        if value is None and default is None:
            return None
        resolved = (root / (value if value is not None else default)).resolve()
        if root not in [resolved, *resolved.parents]:
            raise ValueError(f"{source}: {value!r} points outside the project")
        return resolved

    base = base_for(root)

    css = tuple(path(item, None) for item in style.get("css", []))
    for sheet in css:
        if not sheet.is_file() or sheet.suffix != ".css":
            raise ValueError(f"{source}: style css {sheet} is not a .css file")
    template_dir = path(style.get("template_dir"), None)
    if template_dir is not None and not template_dir.is_dir():
        raise ValueError(f"{source}: style template_dir {template_dir} is not a folder")
    stored = Path(store["path"]).expanduser() if store.get("path") else None
    return Config(
        project=root,
        reports_dir=path(data.get("reports_dir"), "reports"),
        output_dir=path(data.get("output_dir"), None) or base / "build",
        cairn_dir=base,
        store_path=(stored if stored.is_absolute() else root / stored) if stored else base / "store",
        extra_css=css,
        template_dir=template_dir,
        store=dict(store),
    )
