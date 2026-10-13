"""Project configuration: where reports, runs, and build output live, and the page style.

Everything has a default, so most projects need no configuration. To change
something, add a `cairn-report.toml` at the project root:

    reports_dir = "reports"            # report content: Markdown, fragments, media, comments
    output_dir = "generated/reports"   # build output, one folder per report slug
    outputs_dir = "outputs"            # Hydra run directories read by the reports

    [style]
    css = ["reports/style.css"]        # extra stylesheets, applied after the default style
    template_dir = "reports/template"  # files here replace the packaged page files by name

    [hub]                              # where `cairn-report push` sends runs and reports
    url = "https://hub.example.org"    # or CAIRN_HUB_URL
    project = "my-study"               # default: the project folder name
    token_file = "~/.config/cairn-report/hub-token"  # or CAIRN_HUB_TOKEN; never the token itself
    interval = 30                      # seconds between pushes with --watch
    exclude = [".submitit/**/*.pkl"]   # files never pushed: gitignore-style globs (*, ?, **) on project paths

    [logger]
    backends = ["local"]               # "local", "mlflow", or both
    [logger.mlflow]
    tracking_uri = "https://mlflow.example.org"  # default: MLFLOW_TRACKING_URI; credentials via MLflow's env vars
    experiment = "my-study"            # default: the project folder name
    cache_dir = "generated/mlflow"     # where reports keep downloaded MLflow runs
    serve = true                       # run the project's own tracking server (instead of tracking_uri)
    port = 5000                        # its port; `cairn-report serve` starts it next to the reports
    store = "mlflow"                   # its data folder in the project

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
    hub: dict = field(default_factory=dict)
    logger: dict = field(default_factory=lambda: {"backends": ["local"], "mlflow": {}})

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
    unknown = set(data) - {"reports_dir", "output_dir", "outputs_dir", "style", "hub", "logger"}
    if unknown:
        raise ValueError(f"{root / CONFIG_FILE}: unknown keys {sorted(unknown)}")
    style = data.get("style", {})
    unknown_style = set(style) - {"css", "template_dir"}
    if unknown_style:
        raise ValueError(f"{root / CONFIG_FILE}: unknown [style] keys {sorted(unknown_style)}")

    hub = data.get("hub", {})
    if "token" in hub:
        raise ValueError(f"{root / CONFIG_FILE}: never put the hub token in the config; use token_file or CAIRN_HUB_TOKEN")
    unknown_hub = set(hub) - {"url", "project", "token_file", "interval", "exclude"}
    if not isinstance(hub.get("exclude", []), list) or not all(isinstance(item, str) for item in hub.get("exclude", [])):
        raise ValueError(f"{root / CONFIG_FILE}: [hub] exclude must be a list of glob patterns")
    if unknown_hub:
        raise ValueError(f"{root / CONFIG_FILE}: unknown [hub] keys {sorted(unknown_hub)}")

    logger = data.get("logger", {})
    mlflow = logger.get("mlflow", {})
    unknown_logger = (set(logger) - {"backends", "mlflow"}) | (set(mlflow) - {"tracking_uri", "experiment", "cache_dir", "serve", "port", "store"})
    if unknown_logger:
        raise ValueError(f"{root / CONFIG_FILE}: unknown [logger] keys {sorted(unknown_logger)} "
                         "(MLflow credentials belong in MLFLOW_TRACKING_TOKEN or MLFLOW_TRACKING_USERNAME/PASSWORD)")
    backends = logger.get("backends", ["local"])
    if not isinstance(backends, list) or not backends or set(backends) - {"local", "mlflow"}:
        raise ValueError(f"{root / CONFIG_FILE}: [logger] backends must list 'local' and/or 'mlflow'")

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
        hub=dict(hub),
        logger={"backends": backends, "mlflow": dict(mlflow)},
    )
