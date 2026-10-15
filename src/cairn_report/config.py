"""Project configuration: where reports, runs, and build output live, and the page style.

Everything has a default, so most projects need no configuration. To change
something, add a `cairn-report.toml` at the project root:

    reports_dir = "reports"            # the root report: report.md, assets/, child report folders
    output_dir = "generated/reports"   # build output, one folder per report slug
    outputs_dir = "outputs"            # Hydra run directories read by the reports

    [style]
    css = ["reports/style.css"]        # extra stylesheets, applied after the default style
    template_dir = "reports/template"  # files here replace the packaged page files by name

    [page]
    copy_preamble = "..."              # text put before the comments the Copy button copies

    [store]                            # the artifact store `cairn_report.store` reads and writes
    path = ".store"                    # a local store folder (default), or:
    url = "https://store.example.org"  # a store server (`cairn-report store serve`)
    project = "my-study"               # default: the project folder name
    token_file = "~/.config/cairn-report/store-token"  # or CAIRN_STORE_TOKEN; never the token itself
    allow_http = false                 # send the token over plain HTTP to a non-local server (default: refuse)
    exclude = ["*.pkl"]                # files never published: gitignore-style globs on project paths
    interval = 30                      # seconds between syncs of `cairn-report publish --watch`
    publish = [{ folder = "runs/descent/latest", name = "descent" }]  # folders `cairn-report publish` syncs

The project root is the nearest folder, starting at the working directory, that
contains `cairn-report.toml`, `pixi.toml`, or `pyproject.toml`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "cairn-report.toml"
ROOT_MARKERS = (CONFIG_FILE, "pixi.toml", "pyproject.toml")


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
        path = self.project / CONFIG_FILE
        return path if path.is_file() else None


def find_project(start: Path | None = None) -> Path:
    start = Path(start or Path.cwd()).resolve()
    for folder in [start, *start.parents]:
        if any((folder / marker).is_file() for marker in ROOT_MARKERS):
            return folder
    return start


def load_config(project: Path | str | None = None) -> Config:
    """Read cairn-report.toml (if any) and resolve every path against the project root."""
    root = Path(project).resolve() if project else find_project()
    data: dict = {}
    if (root / CONFIG_FILE).is_file():
        data = tomllib.loads((root / CONFIG_FILE).read_text())
    if "logger" in data:
        raise ValueError(f"{root / CONFIG_FILE}: [logger] was removed: the logger only writes files into the run folder "
                         "(sync a running job with `cairn-report publish <folder> <name> --watch`)")
    if "hub" in data:
        raise ValueError(f"{root / CONFIG_FILE}: [hub] was removed: point [store] url at a store server "
                         "(`cairn-report store serve`) and publish with `cairn-report publish`")
    unknown = set(data) - {"reports_dir", "output_dir", "outputs_dir", "style", "store", "page"}
    if unknown:
        raise ValueError(f"{root / CONFIG_FILE}: unknown keys {sorted(unknown)}")
    style = data.get("style", {})
    unknown_style = set(style) - {"css", "template_dir"}
    if unknown_style:
        raise ValueError(f"{root / CONFIG_FILE}: unknown [style] keys {sorted(unknown_style)}")

    store = data.get("store", {})
    if "token" in store:
        raise ValueError(f"{root / CONFIG_FILE}: never put the store token in the config; use token_file or CAIRN_STORE_TOKEN")
    unknown_store = set(store) - {"path", "url", "project", "token_file", "exclude", "publish", "interval", "allow_http"}
    if unknown_store:
        raise ValueError(f"{root / CONFIG_FILE}: unknown [store] keys {sorted(unknown_store)}")
    for key in ("path", "url", "project", "token_file"):
        if not isinstance(store.get(key, ""), str):
            raise ValueError(f"{root / CONFIG_FILE}: [store] {key} must be text")
    if not isinstance(store.get("allow_http", False), bool):
        raise ValueError(f"{root / CONFIG_FILE}: [store] allow_http must be true or false")
    if not isinstance(store.get("exclude", []), list) or not all(isinstance(item, str) for item in store.get("exclude", [])):
        raise ValueError(f"{root / CONFIG_FILE}: [store] exclude must be a list of glob patterns")
    if isinstance(store.get("interval", 30), bool) or not isinstance(store.get("interval", 30), (int, float)):
        raise ValueError(f"{root / CONFIG_FILE}: [store] interval must be a number of seconds")
    mappings = store.get("publish", [])
    if not isinstance(mappings, list) or not all(isinstance(item, dict) and set(item) == {"folder", "name"}
                                                 and all(isinstance(value, str) for value in item.values()) for item in mappings):
        raise ValueError(f"{root / CONFIG_FILE}: [store] publish must be a list of {{ folder = ..., name = ... }} tables")

    page = data.get("page", {})
    if set(page) - {"copy_preamble"} or not isinstance(page.get("copy_preamble", ""), str):
        raise ValueError(f"{root / CONFIG_FILE}: [page] takes copy_preamble (text)")

    def path(value: str | None, default: str | None) -> Path | None:
        if value is None and default is None:
            return None
        resolved = (root / (value if value is not None else default)).resolve()
        if root not in [resolved, *resolved.parents]:
            raise ValueError(f"{root / CONFIG_FILE}: {value!r} points outside the project")
        return resolved

    css = tuple(path(item, None) for item in style.get("css", []))
    for sheet in css:
        if not sheet.is_file() or sheet.suffix != ".css":
            raise ValueError(f"{root / CONFIG_FILE}: style css {sheet} is not a .css file")
    template_dir = path(style.get("template_dir"), None)
    if template_dir is not None and not template_dir.is_dir():
        raise ValueError(f"{root / CONFIG_FILE}: style template_dir {template_dir} is not a folder")
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
