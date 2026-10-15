"""Project configuration: where reports, runs, and build output live, and the page style.

Everything has a default, so most projects need no configuration. To change
something, add a `cairn.toml` at the project root:

    reports_dir = "reports"            # the root report: report.md, assets/, child report folders
    output_dir = "generated/reports"   # build output, one folder per report slug
    outputs_dir = "outputs"            # Hydra run directories read by the reports

    [style]
    css = ["reports/style.css"]        # extra stylesheets, applied after the default style
    template_dir = "reports/template"  # files here replace the packaged page files by name

    [page]
    copy_preamble = "..."              # text put before the comments the Copy button copies

    [store]                            # the artifact store `cairn` reads and writes
    path = ".store"                    # a local store folder (default), or:
    url = "https://store.example.org"  # a store server (`cairn server`)
    project = "my-study"               # default: the project folder name
    token_file = "~/.config/cairn/store-token"  # or CAIRN_STORE_TOKEN; never the token itself
    allow_http = false                 # send the token over plain HTTP to a non-local server (default: refuse)
    exclude = ["*.pkl"]                # files never published: gitignore-style globs on project paths
    interval = 30                      # seconds between syncs of `cairn publish --watch`
    publish = [{ folder = "runs/descent/latest", name = "descent" }]  # folders `cairn publish` syncs

The project root is the nearest folder, starting at the working directory, that
contains `cairn.toml` (or the deprecated `cairn-report.toml`), `pixi.toml`, or `pyproject.toml`.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "cairn.toml"
LEGACY_CONFIG_FILE = "cairn-report.toml"  # read when cairn.toml is absent (deprecated)
ROOT_MARKERS = (CONFIG_FILE, LEGACY_CONFIG_FILE, "pixi.toml", "pyproject.toml")
_WARNED: set[Path] = set()


def config_path(root: Path) -> Path | None:
    """The config file of a project root: cairn.toml, else the deprecated cairn-report.toml (with a
    warning on stderr, once per root); both present is an error."""
    new, old = root / CONFIG_FILE, root / LEGACY_CONFIG_FILE
    if new.is_file() and old.is_file():
        raise ValueError(f"{root} has both {CONFIG_FILE} and {LEGACY_CONFIG_FILE}: keep {CONFIG_FILE} and delete {LEGACY_CONFIG_FILE}")
    if new.is_file():
        return new
    if old.is_file():
        if root not in _WARNED:
            _WARNED.add(root)
            print(f"warning: {old} is deprecated: rename it to {CONFIG_FILE}", file=sys.stderr)
        return old
    return None


@dataclass(frozen=True)
class Config:
    project: Path
    reports_dir: Path
    output_dir: Path
    outputs_dir: Path
    extra_css: tuple[Path, ...] = field(default_factory=tuple)
    template_dir: Path | None = None
    store: dict = field(default_factory=dict)
    page: dict = field(default_factory=dict)

    @property
    def source(self) -> Path | None:
        return config_path(self.project)


def find_project(start: Path | None = None) -> Path:
    start = Path(start or Path.cwd()).resolve()
    for folder in [start, *start.parents]:
        if any((folder / marker).is_file() for marker in ROOT_MARKERS):
            return folder
    return start


def load_config(project: Path | str | None = None) -> Config:
    """Read cairn.toml (or the deprecated cairn-report.toml, if any) and resolve every path against the project root."""
    root = Path(project).resolve() if project else find_project()
    data: dict = {}
    source = config_path(root) or root / CONFIG_FILE
    if source.is_file():
        data = tomllib.loads(source.read_text())
    if "logger" in data:
        raise ValueError(f"{source}: [logger] was removed: the logger only writes files into the run folder "
                         "(sync a running job with `cairn publish <folder> <name> --watch`)")
    if "hub" in data:
        raise ValueError(f"{source}: [hub] was removed: point [store] url at a store server "
                         "(`cairn server`) and publish with `cairn publish`")
    unknown = set(data) - {"reports_dir", "output_dir", "outputs_dir", "style", "store", "page"}
    if unknown:
        raise ValueError(f"{source}: unknown keys {sorted(unknown)}")
    style = data.get("style", {})
    unknown_style = set(style) - {"css", "template_dir"}
    if unknown_style:
        raise ValueError(f"{source}: unknown [style] keys {sorted(unknown_style)}")

    store = data.get("store", {})
    if "token" in store:
        raise ValueError(f"{source}: never put the store token in the config; use token_file or CAIRN_STORE_TOKEN")
    unknown_store = set(store) - {"path", "url", "project", "token_file", "exclude", "publish", "interval", "allow_http"}
    if unknown_store:
        raise ValueError(f"{source}: unknown [store] keys {sorted(unknown_store)}")
    for key in ("path", "url", "project", "token_file"):
        if not isinstance(store.get(key, ""), str):
            raise ValueError(f"{source}: [store] {key} must be text")
    if not isinstance(store.get("allow_http", False), bool):
        raise ValueError(f"{source}: [store] allow_http must be true or false")
    if not isinstance(store.get("exclude", []), list) or not all(isinstance(item, str) for item in store.get("exclude", [])):
        raise ValueError(f"{source}: [store] exclude must be a list of glob patterns")
    if isinstance(store.get("interval", 30), bool) or not isinstance(store.get("interval", 30), (int, float)):
        raise ValueError(f"{source}: [store] interval must be a number of seconds")
    mappings = store.get("publish", [])
    if not isinstance(mappings, list) or not all(isinstance(item, dict) and set(item) == {"folder", "name"}
                                                 and all(isinstance(value, str) for value in item.values()) for item in mappings):
        raise ValueError(f"{source}: [store] publish must be a list of {{ folder = ..., name = ... }} tables")

    page = data.get("page", {})
    if set(page) - {"copy_preamble"} or not isinstance(page.get("copy_preamble", ""), str):
        raise ValueError(f"{source}: [page] takes copy_preamble (text)")

    def path(value: str | None, default: str | None) -> Path | None:
        if value is None and default is None:
            return None
        resolved = (root / (value if value is not None else default)).resolve()
        if root not in [resolved, *resolved.parents]:
            raise ValueError(f"{source}: {value!r} points outside the project")
        return resolved

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
        output_dir=path(data.get("output_dir"), "generated/reports"),
        outputs_dir=path(data.get("outputs_dir"), "outputs"),
        extra_css=css,
        template_dir=template_dir,
        store=dict(store),
        page=dict(page),
    )
