"""Build a tree of reports from Markdown into a static site.

A report is a folder with a `report.md` (front matter + Markdown), an optional
`assets/` folder, and optional child reports (sub-folders with their own
`report.md`). The configured report folder is the root of the tree:

    reports/report.md            ->  generated/reports/index.html
    reports/<a>/report.md        ->  generated/reports/<a>/index.html
    reports/<a>/<b>/report.md    ->  generated/reports/<a>/<b>/index.html

Pages link to each other with `[text](report:<path>#anchor)` (`report:/` is the
root), checked at build time. The output is static: any web server, the
project server, or a hub can serve it.
"""

from __future__ import annotations

import gzip
import contextlib
import csv
import hashlib
import html
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yaml
from latex2mathml.converter import convert as latex_to_mathml
from markdown_it import MarkdownIt
from mdit_py_plugins.admon import admon_plugin
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.attrs import attrs_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from plotly.offline import get_plotlyjs, get_plotlyjs_version

from . import __version__
from .config import Config, load_config
from .logger import ARTIFACTS, METRICS, fold_artifacts, fold_scalars, read_jsonl

PACKAGE_TEMPLATE = Path(__file__).resolve().parent / "template"
# Set by configure() from the project's Config before any report is built.
CONFIG: Config | None = None
PROJECT = REPORTS_ROOT = OUTPUT_ROOT = OUTPUTS = Path()
SEGMENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
REPORT_LINK = re.compile(r"report:(/|[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*)(?:#([A-Za-z0-9_.:-]+))?$")
SKIPPED = {"assets"}
  # folder names that are never reports (also anything starting with "_" or ".")
PAGE_FILES = ("index.html", "report.css", "report-extra.css", "report.js")
ASSETS = "assets"


def configure(config: Config) -> None:
    global CONFIG, PROJECT, REPORTS_ROOT, OUTPUT_ROOT, OUTPUTS
    CONFIG = config
    PROJECT, REPORTS_ROOT = config.project, config.reports_dir
    OUTPUT_ROOT, OUTPUTS = config.output_dir, config.outputs_dir


def page_file(name: str) -> Path:
    """A page file from the project's template_dir when it provides one, else the packaged default."""
    if CONFIG and CONFIG.template_dir and (CONFIG.template_dir / name).is_file():
        return CONFIG.template_dir / name
    return PACKAGE_TEMPLATE / name


def tooling_info() -> dict:
    """Which cairn-report produced the build: version, install source, and style overrides."""
    from importlib import metadata

    info = {"package": "cairn-report", "version": __version__}
    try:
        direct = metadata.distribution("cairn-report").read_text("direct_url.json")
        if direct:
            url = json.loads(direct)
            info["source"] = url.get("url")
            if url.get("vcs_info"):
                info["commit"] = url["vcs_info"].get("commit_id")
    except metadata.PackageNotFoundError:
        pass
    info["page_files"] = {name: str(page_file(name)) for name in PAGE_FILES if page_file(name).parent != PACKAGE_TEMPLATE}
    info["extra_css"] = [str(path.relative_to(PROJECT)) for path in (CONFIG.extra_css if CONFIG else ())]
    return info

GRAPHVIZ_ENGINES = ("dot", "neato", "fdp", "sfdp", "circo", "twopi")
UNSAFE_HTML = re.compile(
    r"<(?:script|iframe|object|embed|link|base|meta)\b"
    r"|\son[a-z]+\s*="
    r"|javascript:"
    r"|\b(?:src|srcset|poster|data)\s*=\s*[\"']?(?:https?:)?//",
    re.IGNORECASE,
)
DELIMITERS = set("|‖()[]{}⟨⟩⌈⌉⌊⌋/∣∥")
BARE_MO = re.compile(r"<mo>(&#x([0-9A-Fa-f]+);|[^<])</mo>")
VALUE = re.compile(r"\{\{(.+?)\}\}")
# Graphviz defaults in the report palette; attributes in the diagram override them.
GRAPHVIZ_DEFAULTS = (
    'graph [bgcolor="transparent" fontname="Helvetica" fontsize=12 pad=0.2 nodesep=0.35 ranksep=0.45]; '
    'node [shape=box style="rounded,filled" fillcolor="#fffef9" color="#8bb5a8" fontcolor="#202621" '
    'fontname="Helvetica" fontsize=12 penwidth=1.2 margin="0.18,0.08"]; '
    'edge [color="#697169" fontcolor="#697169" fontname="Helvetica" fontsize=10 arrowsize=0.7]; '
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def check_html(markup: str, origin: str) -> str:
    """Author HTML is trusted but self-contained: no scripts, handlers, or external resources."""
    match = UNSAFE_HTML.search(markup)
    if match:
        raise ValueError(f"{origin}: disallowed HTML {match.group(0)!r}")
    return markup


def unstretch_delimiters(mathml: str) -> str:
    """Mark bare delimiters non-stretchy: in LaTeX only \\left/\\right delimiters grow.

    latex2mathml emits e.g. \\lVert as a plain <mo>, which MathML renderers stretch
    as a fence, so the two sides of a norm render at different heights.
    """

    def fix(match):
        char = chr(int(match.group(2), 16)) if match.group(2) else match.group(1)
        return (
            f'<mo stretchy="false">{match.group(1)}</mo>'
            if char in DELIMITERS
            else match.group(0)
        )

    return BARE_MO.sub(fix, mathml)


RENDER_PACKAGES = (
    "markdown-it-py",
    "mdit-py-plugins",
    "latex2mathml",
    "matplotlib",
    "plotly",
    "pyyaml",
)


def package_version(name: str) -> str | None:
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def graphviz_version() -> str | None:
    if not shutil.which("dot"):
        return None
    result = subprocess.run(["dot", "-V"], capture_output=True, text=True)
    return (result.stderr or result.stdout).strip() or None


def code_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unversioned"


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
COMMENT = re.compile(r"<!--.*?-->", re.S)


def evaluate_value(expression: str, namespace: dict, origin: str):
    """`{{ expr }}` or `{{ expr:spec }}`: plain Python first, f-string format spec when needed."""
    try:
        return eval(compile(expression, origin, "eval"), namespace)
    except SyntaxError:
        if ":" not in expression:
            raise
    head, spec = expression.rsplit(":", 1)  # the format spec never contains a colon
    return format(eval(compile(head, origin, "eval"), namespace), spec)


def strip_comments(markdown: str) -> str:
    """Drop <!-- ... --> author/agent comments outside code fences.

    Section guidance and `@agent` directives stay in the source only. Each comment is
    replaced by the same number of newlines, so source-line anchors do not shift.
    """
    out, chunk, fence = [], [], None
    for line in markdown.splitlines(keepends=True):
        match = FENCE.match(line)
        if fence is None and match:
            out.append(COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), "".join(chunk)))
            chunk, fence = [], match.group(1)[0] * len(match.group(1))
            out.append(line)
        elif fence is not None:
            out.append(line)
            if line.strip().startswith(fence) and set(line.strip()) <= set(fence[0]):
                fence = None
        else:
            chunk.append(line)
    out.append(COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), "".join(chunk)))
    return "".join(out)


TYPED_NUMBER = re.compile(r"(?<![\w.#/-])\d+(?:\.\d+)?\s?%|(?<![\w.#/-])\d+\.\d+(?![\w.])")
NOT_PROSE = re.compile(r"`[^`]*`|\$[^$]*\$|\{\{.*?\}\}|\]\([^)]*\)|\{#[^}]*\}|https?://\S+")


def typed_numbers(markdown: str) -> list[dict]:
    """Decimals and percentages typed in prose (outside code, math, {{ }}, links).

    Results must be {{ }} values computed from run files; design constants such as a
    gate threshold may stay typed. The build lists every hit so the author can decide.
    """
    hits, fence = [], None
    for number, line in enumerate(strip_comments(markdown).splitlines(), 1):
        match = FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)[0] * len(match.group(1))
            continue
        if fence is not None:
            if line.strip().startswith(fence) and set(line.strip()) <= set(fence[0]):
                fence = None
            continue
        for token in TYPED_NUMBER.findall(NOT_PROSE.sub(" ", line)):
            hits.append({"line": number, "text": token.replace(" ", "")})
    return hits


def HUB_RUNS_DIR(project: Path) -> Path:
    """Where runs downloaded from a hub are kept for reports."""
    return Path(project) / "generated" / "hub-runs"


def discover_reports() -> dict[str, Path]:
    """Every report in the tree, keyed by its path below the root ("" is the root), children first.

    The whole folder tree is searched: a folder without a report.md still holds the reports below it.
    """
    if not (REPORTS_ROOT / "report.md").is_file():
        return {}
    found: dict[str, Path] = {}

    def walk(folder: Path, key: str) -> None:
        for child in sorted(folder.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")) or child.name in SKIPPED:
                continue
            if not SEGMENT.fullmatch(child.name):
                if any(child.rglob("report.md")):
                    print(f"warning: {child.relative_to(REPORTS_ROOT.parent)}: skipped; report folder names "
                          f"are lower-case letters, digits, '-' and '_'", file=sys.stderr)
                continue
            walk(child, f"{key}/{child.name}" if key else child.name)
        if (folder / "report.md").is_file():
            found[key] = folder

    walk(REPORTS_ROOT, "")
    return found


def depth(key: str) -> int:
    return len(key.split("/")) if key else 0


def parent_of(key: str, reports: dict[str, Path] | None = None) -> str | None:
    """The nearest enclosing report ("" is the root); folders without a report.md are passed over."""
    reports = discover_reports() if reports is None else reports
    while key:
        key = key.rsplit("/", 1)[0] if "/" in key else ""
        if key in reports:
            return key
    return None


def output_dir(key: str) -> Path:
    return OUTPUT_ROOT / key if key else OUTPUT_ROOT


def report_href(key: str) -> str:
    """A report's address relative to the site root, so a site works under any prefix (e.g. a hub)."""
    return f"{key}/" if key else ""


def page_href(target: str, current: str, anchor: str | None = None) -> str:
    """A link from one report page to another (or to itself), relative to the current page."""
    fragment = f"#{anchor}" if anchor else ""
    if target == current:
        return fragment or "./"
    return ("../" * depth(current) + report_href(target) or "./") + fragment


def front_matter_title(folder: Path | None) -> str:
    """A report's title from its front matter (its folder name if it has none), without building it."""
    if folder is None:
        return ""
    match = re.match(r"---\n(.*?)\n---", (folder / "report.md").read_text(), re.S)
    title = (yaml.safe_load(match.group(1)) or {}).get("title") if match else None
    return str(title or folder.name)


def root_title() -> str:
    """The root report's title from its front matter, without building it ("Report" if it has none)."""
    title = front_matter_title(REPORTS_ROOT)
    return "Report" if title == REPORTS_ROOT.name else title


def card_id(key: str) -> str:
    """The id of a report's card on its parent page."""
    return "report-" + key.replace("/", "-")


class Build:
    """State gathered while rendering one report: sources, runs, ids, findings, links."""

    def __init__(self, key: str, folder: Path):
        self.key = key
        self.markdown = folder / "report.md"
        self.content_root = folder
        self.generated = output_dir(key)
        self.meta: dict = {}
        self.links: list[dict] = []
        self.typed: list[dict] = []
        self.sources: dict[str, str] = {}
        self.runs: dict[str, str | None] = {}  # run path -> status from its run.json (None if it has none)
        self.mlflow_runs: dict[str, dict] = {}
        self.mirrors: dict[str, dict | None] = {}
        self.hub_index = None
        self.hub_runs: dict[str, dict] = {}
        self.sizes: dict[str, tuple[int, int] | None] = {}
        self.rates: dict[str, float | None] = {}  # frame rates of MP4/MOV videos
        self.ids: set[str] = set()
        self.findings: list[dict] = []
        self.figures = 0

    def unique(self, wanted: str, line: int) -> str:
        if wanted in self.ids:
            raise ValueError(f"{self.markdown}:{line}: duplicate id {wanted!r}")
        self.ids.add(wanted)
        return wanted

    def track(self, path: Path) -> Path:
        """Record a file the report depends on, by its resolved (immutable) path."""
        resolved = Path(path).resolve(strict=True)
        if PROJECT.resolve() not in resolved.parents:
            raise ValueError(f"{path}: report sources must live inside the project")
        relative = resolved.relative_to(PROJECT.resolve())
        self.sources[str(relative)] = digest(resolved)
        return resolved

    def require_run(self, run_root: Path, where: str) -> dict:
        """Record a run the report reads: its run.json (if any), provenance files, and status in the manifest."""
        status_file = run_root / "run.json"
        status = json.loads(self.track(status_file).read_text()) if status_file.is_file() else {}
        for name in ("manifest.json", "working-tree.patch"):
            if (run_root / "provenance" / name).is_file():
                self.track(run_root / "provenance" / name)
        self.runs[run_root.resolve().relative_to(PROJECT.resolve()).as_posix()] = status.get("status")
        return status

    def artifact_index(self, run_root: Path) -> dict[str, dict]:
        """Latest artifacts.jsonl entry per path (append-only index written by RunLogger)."""
        index = run_root / ARTIFACTS
        if index.is_file():
            return fold_artifacts(read_jsonl(self.track(index)))
        legacy = run_root / "artifacts.json"  # older runs: one rewritten JSON document
        if legacy.is_file():
            return {entry["path"]: entry for entry in json.loads(self.track(legacy).read_text()).get("artifacts", [])}
        return {}

    def run_trace(self, relative: Path, line: int) -> dict:
        """Where a run file came from: its run, the code that wrote it, the commit, and its MLflow run.

        Refuses files from runs that did not complete. Reads the run logger's
        artifacts.jsonl (origin and producer) and the run's provenance commit.
        """
        trace = {"path": relative.as_posix()}
        project = PROJECT.resolve()
        file = project / relative
        run_root = self.run_root(file)
        if run_root is None:
            return trace
        status = self.require_run(run_root, f"{self.markdown}:{line}")
        inside = file.relative_to(run_root).as_posix()
        mlflow = json.loads((run_root / "mlflow-run.json").read_text()) if (run_root / "mlflow-run.json").is_file() else None
        hub = json.loads((run_root / "hub-run.json").read_text()) if (run_root / "hub-run.json").is_file() else None
        trace["run"] = (mlflow or {}).get("run_dir") or (hub or {}).get("run") or run_root.relative_to(project).as_posix()
        snapshot = bool(mlflow or hub)
        trace["path"] = inside if snapshot else trace["path"]
        trace["label"] = f"{trace['run']}/{inside}" if snapshot else relative.as_posix()
        if hub:
            trace["hub"] = {"run": hub["run"], "url": hub["url"]}
        entry = self.artifact_index(run_root).get(inside)
        if entry:
            trace["origin"] = entry.get("origin")
            if entry.get("producer"):
                trace["producer"] = entry["producer"]
        manifest = run_root / "provenance" / "manifest.json"
        commit = json.loads(manifest.read_text()).get("commit") if manifest.exists() else None
        commit = commit or status.get("code_revision")
        if commit:
            trace["revision"] = commit
            patch = run_root / "provenance" / "working-tree.patch"
            if patch.exists() and patch.stat().st_size:
                trace["uncommitted"] = patch.relative_to(project).as_posix()
        if mlflow is None and self.uses_mlflow():
            mlflow = self.mirrored_run(run_root)
        if mlflow:
            trace["mlflow"] = mlflow_link(mlflow, inside)
        return trace

    def run_root(self, file: Path) -> Path | None:
        """The completed run (or study trial) a file belongs to: under outputs/ or an MLflow snapshot."""
        project = PROJECT.resolve()
        relative = file.relative_to(project)
        cache = (project / self.mlflow_settings().get("cache_dir", "generated/mlflow")).resolve()
        if cache in file.parents:
            return cache / file.relative_to(cache).parts[0]
        hub_cache = HUB_RUNS_DIR(project).resolve()
        if hub_cache in file.parents and len(file.relative_to(hub_cache).parts) > 2:
            return hub_cache / Path(*file.relative_to(hub_cache).parts[:2])
        parts = relative.parts
        if parts[0] != OUTPUTS.resolve().relative_to(project).parts[0] or len(parts) < 4:
            return None
        study = project / Path(*parts[:3])
        return next(
            (folder for folder in file.parents if (folder / "run.json").exists() and (folder == study or study in folder.parents)),
            study,
        )

    def label(self, source: str) -> str:
        """A read file as readers know it: MLflow snapshot files by their run, not by cache path."""
        cache = (PROJECT.resolve() / self.mlflow_settings().get("cache_dir", "generated/mlflow")).resolve()
        file = PROJECT.resolve() / source
        hub_cache = HUB_RUNS_DIR(PROJECT).resolve()
        if hub_cache in file.parents:
            name, stamp, *inside = file.relative_to(hub_cache).parts
            return f"{name}/{stamp} (hub)/{'/'.join(inside)}"
        if cache not in file.parents:
            return source
        run_id, *inside = file.relative_to(cache).parts
        record = self.mlflow_runs.get(run_id, {})
        return f"{record.get('run_dir') or 'mlflow:' + run_id} (MLflow)/{'/'.join(inside)}"

    def mirrored_run(self, run_root: Path) -> dict | None:
        """The MLflow copy of a local run (both backends), found by its run directory tag; best effort."""
        key = run_root.relative_to(PROJECT.resolve()).as_posix()
        if key not in self.mirrors:
            try:
                from .mlflow_backend import find_run

                _, uri, run = find_run(self.mlflow_settings(), PROJECT, run_dir=key)
                self.mirrors[key] = {"run_id": run.info.run_id, "tracking_uri": uri, "experiment_id": run.info.experiment_id}
            except (Exception, SystemExit):
                self.mirrors[key] = None  # the local run is the record; a missing MLflow copy only loses the link
        return self.mirrors[key]

    # ------------------------------------------------------------ run selection

    def uses_mlflow(self) -> bool:
        return CONFIG is not None and "mlflow" in CONFIG.logger["backends"]

    def mlflow_settings(self) -> dict:
        return CONFIG.logger["mlflow"] if CONFIG else {}

    def uses_hub(self) -> bool:
        return CONFIG is not None and "hub" in CONFIG.logger["backends"]

    def resolve_run(self, name: str, timestamp: str, where: str) -> Path:
        """outputs/<name>/<timestamp>; when its evidence is not local, the run from the hub, then from MLflow."""
        local = OUTPUTS / name / timestamp
        local_evidence = local.exists() and self.has_evidence(local.resolve())
        if self.uses_hub() and not local_evidence:
            try:
                return self.hub_run(name, timestamp, where)
            except LookupError:
                if not self.uses_mlflow() and not local.exists():
                    raise ValueError(f"{where}: no run {name}/{timestamp} locally or on the hub") from None
        if self.uses_mlflow() and not local_evidence:
            if local.exists():
                return self.mlflow_run(where, run_dir=local.resolve().relative_to(PROJECT.resolve()).as_posix())
            return self.mlflow_run(where, name=name)
        resolved = local.resolve(strict=True)
        self.require_run(resolved, where)
        return resolved

    def hub_run(self, name: str, timestamp: str, where: str) -> Path:
        """A run from the hub's run index, downloaded (verified) into generated/hub-runs/<name>/<timestamp>/."""
        from urllib.parse import quote

        from .push import _download, connect, project_name

        if self.hub_index is None:
            hub = connect(CONFIG)
            status, entries = hub.json("GET", f"/api/v1/projects/{project_name(CONFIG)}/runs", expect=(200, 404))
            self.hub_index = (hub, {entry["path"]: entry for entry in entries} if status == 200 else {})
        hub, index = self.hub_index
        stamp = timestamp
        if timestamp == "latest":
            link = index.get(f"{name}/latest", {}).get("link")
            stamps = sorted(path.split("/")[1] for path in index if path.startswith(f"{name}/") and path.count("/") == 2
                            and path.endswith("/run.json"))
            stamp = link.rstrip("/") if link else (stamps[-1] if stamps else None)
        prefix = f"{name}/{stamp}/"
        files = {path: entry for path, entry in index.items() if stamp and path.startswith(prefix)}
        if not files:
            raise LookupError(f"{name}/{timestamp}")
        target = HUB_RUNS_DIR(PROJECT) / name / stamp
        project = project_name(CONFIG)
        for path, entry in files.items():
            local = target / path[len(prefix):]
            if "link" in entry:
                if not local.is_symlink():
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.symlink_to(entry["link"])
            else:
                _download(hub, f"/files/{project}/runs/{quote(path)}", local, entry["sha256"])
        record = {"run": f"{name}/{stamp}", "url": f"{hub.url}/runs/{project}/{name}/{stamp}/"}
        (target / "hub-run.json").write_text(json.dumps(record) + "\n")
        self.require_run(target, where)
        self.hub_runs[record["run"]] = {**record, "snapshot": target.relative_to(PROJECT.resolve()).as_posix()}
        return target

    @staticmethod
    def has_evidence(folder: Path) -> bool:
        return (folder / METRICS).is_file() or (folder / ARTIFACTS).is_file()

    def mlflow_run(self, where: str, **query) -> Path:
        from .mlflow_backend import snapshot

        cache = (PROJECT / self.mlflow_settings().get("cache_dir", "generated/mlflow")).resolve()
        folder, record = snapshot(self.mlflow_settings(), PROJECT, cache, **query)
        self.require_run(folder, where)
        self.mlflow_runs[record["run_id"]] = {**record, "snapshot": folder.relative_to(PROJECT.resolve()).as_posix()}
        return folder

    # ------------------------------------------------------------------ figures

    RUN_REFERENCE = re.compile(r"run:([a-z0-9][a-z0-9_.-]*)(?:@([^/]+))?/(.+)")
    MLFLOW_REFERENCE = re.compile(r"mlflow:([0-9a-zA-Z]+)/(.+)")

    def asset(self, src: str, line: int) -> tuple[str, str, dict]:
        """Copy a figure into generated/assets/: a path relative to the report, `run:<name>[@<timestamp>]/<file>`
        (local or MLflow, per the logger backends), or `mlflow:<run-id>/<file>`."""
        where = f"{self.markdown}:{line}"
        run_match, mlflow_match = self.RUN_REFERENCE.fullmatch(src), self.MLFLOW_REFERENCE.fullmatch(src)
        if run_match:
            name, timestamp, inside = run_match.groups()
            file = self.resolve_run(name, timestamp or "latest", where) / inside
        elif mlflow_match:
            run_id, inside = mlflow_match.groups()
            file = self.mlflow_run(where, run_id=run_id) / inside
        elif re.match(r"^[a-z]+:", src) or src.startswith("//"):
            raise ValueError(f"{where}: figures must be local files, run:<name>/<file>, or mlflow:<run-id>/<file>; got {src!r}")
        else:
            file = self.content_root / src
        if not file.exists():
            raise ValueError(f"{where}: missing figure {src!r} (paths are relative to the report)")
        resolved = self.track(file)
        relative = resolved.relative_to(PROJECT.resolve())
        target = self.generated / ASSETS / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, target)
        trace = self.run_trace(relative, line)
        self.sizes[f"{ASSETS}/{relative.as_posix()}"] = image_size(resolved)
        if resolved.suffix.lower() in (".mp4", ".m4v", ".mov"):
            self.rates[f"{ASSETS}/{relative.as_posix()}"] = (mp4_info(resolved) or {}).get("fps")
        version = self.sources[relative.as_posix()][:12]  # content hash: the URL changes when the file does
        return f"{ASSETS}/{relative.as_posix()}?v={version}", trace.get("label", relative.as_posix()), trace

    def generated_asset(self, data: bytes, suffix: str) -> str:
        """Store build-made figure bytes (executed Matplotlib, Graphviz) as a file named by its hash."""
        name = f"{hashlib.sha256(data).hexdigest()[:16]}{suffix}"
        target = self.generated / ASSETS / "generated" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.sizes[f"{ASSETS}/generated/{name}"] = image_size(target)
        return f"{ASSETS}/generated/{name}?v={name.split('.')[0][:12]}"  # named by content: cache for good


VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm")


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    """The (type, payload start, payload end) of each ISO-BMFF box in data[start:end]."""
    end = len(data) if end is None else end
    while start + 8 <= end:
        size, kind, header = int.from_bytes(data[start:start + 4], "big"), data[start + 4:start + 8], 8
        if size == 1:
            size, header = int.from_bytes(data[start + 8:start + 16], "big"), 16
        elif size == 0:
            size = end - start
        if size < header:
            return
        yield kind, start + header, min(start + size, end)
        start += size


def mp4_info(path: Path) -> dict | None:
    """Frame size and frame rate of the first video track of an MP4/MOV, read from its moov box."""
    try:
        with path.open("rb") as handle:
            end, position = path.stat().st_size, 0
            while position + 8 <= end:  # top-level boxes, to find moov without reading the media
                handle.seek(position)
                header = handle.read(16)
                size, kind = int.from_bytes(header[:4], "big"), header[4:8]
                size = int.from_bytes(header[8:16], "big") if size == 1 else end - position if size == 0 else size
                if size < 8:
                    return None
                if kind == b"moov":
                    handle.seek(position)
                    moov = handle.read(min(size, 64 * 1024 * 1024))
                    break
                position += size
            else:
                return None
    except OSError:
        return None
    for kind, start, stop in _boxes(moov, 8):
        if kind != b"trak":
            continue
        info: dict = {}
        stack = [(start, stop)]
        while stack:  # walk trak > mdia > minf > stbl for tkhd, mdhd, and stts
            first, last = stack.pop()
            for inner, body, finish in _boxes(moov, first, last):
                if inner in (b"mdia", b"minf", b"stbl"):
                    stack.append((body, finish))
                elif inner == b"tkhd":
                    info["width"] = int.from_bytes(moov[finish - 8:finish - 4], "big") >> 16
                    info["height"] = int.from_bytes(moov[finish - 4:finish], "big") >> 16
                elif inner == b"mdhd":
                    offset = body + (20 if moov[body] == 1 else 12)
                    info["timescale"] = int.from_bytes(moov[offset:offset + 4], "big")
                elif inner == b"stts" and int.from_bytes(moov[body + 4:body + 8], "big"):
                    info["delta"] = int.from_bytes(moov[body + 12:body + 16], "big")  # first entry's sample duration
        if info.get("width") and info.get("height"):  # a video track (audio has no size)
            fps = info["timescale"] / info["delta"] if info.get("timescale") and info.get("delta") else None
            return {"size": (info["width"], info["height"]), "fps": round(fps, 3) if fps else None}
    return None


def mp4_size(path: Path) -> tuple[int, int] | None:
    info = mp4_info(path)
    return info["size"] if info else None


def image_size(path: Path) -> tuple[int, int] | None:
    """Pixel size of a PNG, GIF, JPEG, or SVG, or the frame size of an MP4/MOV video (None if unknown)."""
    if path.suffix.lower() in (".mp4", ".m4v", ".mov"):
        return mp4_size(path)
    try:
        with path.open("rb") as handle:
            head = handle.read(64 * 1024)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")
    if head[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(head):
            if head[index] != 0xFF:
                index += 1
                continue
            marker, length = head[index + 1], int.from_bytes(head[index + 2:index + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return int.from_bytes(head[index + 7:index + 9], "big"), int.from_bytes(head[index + 5:index + 7], "big")
            index += 2 + length
        return None
    text = head.decode("utf-8", errors="ignore")
    match = re.search(r"<svg\b[^>]*>", text, re.S)
    if match:
        tag = match.group(0)
        box = re.search(r'viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', tag)
        width, height = (re.search(rf'\b{key}="([\d.]+)(?:px|pt)?"', tag) for key in ("width", "height"))
        if width and height:
            return round(float(width.group(1))), round(float(height.group(1)))
        if box:
            return round(float(box.group(1))), round(float(box.group(2)))
    return None


def mlflow_link(record: dict, inside: str) -> dict:
    """An MLflow run for the lightbox: its id and, for an HTTP(S) tracking server, the run's page in the UI."""
    link = {"run_id": record["run_id"], "file": inside}
    uri = str(record.get("tracking_uri", ""))
    if uri.startswith(("http://", "https://")):
        link["url"] = f"{uri.rstrip('/')}/#/experiments/{record['experiment_id']}/runs/{record['run_id']}/artifacts"
    return link


def figure_card(
    build: Build,
    *,
    fid: str,
    src: str,
    title: str,
    caption: str,
    line: int,
    source: str = "",
    kind: str = "image",
    trace: dict | None = None,
    size: tuple[int, int] | None = None,
    fps: float | None = None,
) -> str:
    """One gallery card. `trace` says where the figure came from and is shown in the lightbox."""
    build.figures += 1
    trace = {
        "report": f"{build.markdown.relative_to(PROJECT).as_posix()}:{line}",
        **(trace or {}),
    }
    classes, style, dims = "figure-card", "", ""
    if kind == "plotly":
        classes += " plotly-card"
    elif size and size[0] > 0 and size[1] > 0:  # the card's final shape, before the image arrives
        ratio = size[0] / size[1]
        classes += " is-wide" if ratio > 2 else " is-tall" if ratio < 1 else ""
        style = f' style="--figure-ratio:{min(max(ratio, 0.75), 10):.3f}"'
        dims = f' width="{size[0]}" height="{size[1]}"'
    attrs = (
        f'class="{classes}"{style} id="{esc(fid)}" '
        f'data-figure-id="{esc(fid)}" data-figure-type="{kind}" data-src="{esc(src)}" '
        f'data-line="{line}" data-comment-target="figure:{esc(fid)}" data-trace="{esc(json.dumps(trace))}"'
    )
    if kind == "plotly":
        media = '<div class="plotly-preview"></div>'
    elif kind == "video":  # loaded near the viewport; plays muted while on screen
        rate = f' data-fps="{fps:g}"' if fps else ""
        media = (f'<video data-src="{esc(src)}" aria-label="{esc(title)}"{dims}{rate} muted loop playsinline '
                 f'preload="none" disablepictureinpicture></video>')
    else:
        media = f'<img data-src="{esc(src)}" alt="{esc(title)}"{dims} decoding="async">'  # loaded near the viewport
    copy = f"<h3>{esc(title)}</h3>"
    if caption:
        copy += f"<p>{esc(caption)}</p>"
    if source:
        copy += f'<small class="figure-source">Source · {esc(source)}</small>'
    return f'<article {attrs}>{media}<div class="figure-copy">{copy}</div></article>'


def render(build: Build) -> str:
    path = build.markdown
    emitted: list[str] = []
    metrics: list[str] = []
    graphs: list[str] = []  # figure cards from graph(...) in the current exec block
    exec_line = [0]  # source line of the exec block that is running

    def run_dir(name: str, timestamp: str = "latest") -> Path:
        """Resolve outputs/<name>/<timestamp> once (locally or from MLflow); refuse runs that did not complete."""
        return build.resolve_run(name, timestamp, f"{path}: run_dir({name!r})")

    def mlflow_run(run_id: str | None = None, name: str | None = None, run_dir: str | None = None) -> Path:
        """A finished MLflow run by id, run directory, or run name, read like a run directory."""
        return build.mlflow_run(f"{path}: mlflow_run({run_id or run_dir or name!r})", run_id=run_id, name=name, run_dir=run_dir)

    def run_trials(study: Path) -> list[dict]:
        """The trials of a Hydra multirun folder (numbered sub-folders), in trial order.

        Each entry has `trial`, `dir`, `overrides` (from .hydra/overrides.yaml), and
        `status` (from the trial's run.json, or None).
        """
        study = Path(study).resolve(strict=True)
        build.require_run(study, f"{path}: run_trials({study.name!r})")
        trials = []
        for folder in sorted((child for child in study.iterdir() if child.is_dir() and child.name.isdigit()), key=lambda f: int(f.name)):
            overrides_file = folder / ".hydra" / "overrides.yaml"
            overrides = yaml.safe_load(build.track(overrides_file).read_text()) if overrides_file.is_file() else []
            status = build.require_run(folder, f"{path}: run_trials({study.name!r})").get("status")
            trials.append({"trial": folder.name, "dir": folder, "overrides": overrides or [], "status": status})
        return trials

    def read_json(file):
        return json.loads(build.track(file).read_text())

    def read_csv(file):
        with build.track(file).open(newline="") as handle:
            return list(csv.DictReader(handle))

    def read_events(file):
        return read_jsonl(build.track(file))

    def read_scalars(run):
        """Latest value per scalar name from a run's append-only metrics.jsonl."""
        return fold_scalars(read_events(Path(run) / METRICS))

    def metric(label: str, value, detail: str = "", fmt: str | None = None):
        shown = format(value, fmt) if fmt else str(value)
        target = f"metric:{slug(label)}"
        metrics.append(
            f'<article class="metric" data-comment-target="{esc(target)}">'
            f"<span>{esc(label)}</span><strong>{esc(shown)}</strong><small>{esc(detail)}</small></article>"
        )

    namespace = {
        "REPORT_ROOT": build.content_root,
        "PROJECT_ROOT": PROJECT,
        "META": build.meta,
        "run_dir": run_dir,
        "mlflow_run": mlflow_run,
        "run_trials": run_trials,
        "read_json": read_json,
        "read_csv": read_csv,
        "read_jsonl": read_events,
        "read_scalars": read_scalars,
        "track": build.track,
        "metric": metric,
        "report_html": lambda markup: emitted.append(str(markup)),
        "caption": lambda text, figure=None: setattr(figure or plt.gcf(), "_cairn_caption", str(text)),
        "graph": lambda source, *, id=None, title="Diagram", caption="", engine="dot": graphs.append(
            render_graph(str(source), engine, title, caption, id, exec_line[0])
        ),
    }

    def render_math(content, options):
        if "{{" in content:
            raise ValueError(f"{path}: {{{{ }}}} values are not evaluated inside $...$ formulas; write the value next to the formula: ${content.strip()[:60]}$")
        display = "block" if options.get("display_mode") else "inline"
        mathml = unstretch_delimiters(latex_to_mathml(content, display=display))
        return f'<span class="math-tex" data-latex="{esc(content.strip())}">{mathml}</span>'

    md = (
        MarkdownIt("commonmark", {"html": False})
        .enable("table")
        .use(front_matter_plugin)
        .use(dollarmath_plugin, renderer=render_math, allow_labels=True)
        .use(admon_plugin)
        .use(attrs_plugin)
        .use(anchors_plugin, min_level=2, max_level=3)
    )
    default_fence = md.renderer.rules["fence"]
    default_image = md.renderer.rules["image"]
    line_of = lambda token: (token.map or [0])[0] + 1

    # --- inline `{{ expression }}` values, evaluated where they appear ---
    def value_rule(state, silent):
        if not state.src.startswith("{{", state.pos):
            return False
        match = VALUE.match(state.src, state.pos)
        if not match:
            return False
        if not silent:
            token = state.push("report_value", "", 0)
            token.content = match.group(1).strip()
        state.pos = match.end()
        return True

    md.inline.ruler.push("report_value", value_rule)

    def render_value(tokens, idx, options, env):
        expression = tokens[idx].content
        try:
            shown = evaluate_value(expression, namespace, f"{path}:value")
        except Exception as error:
            line = tokens[idx].meta.get("line", "?")
            raise ValueError(
                f"{path}:{line}: cannot evaluate {{{{ {expression} }}}}: {error}"
            ) from error
        return (
            f'<span class="report-value" title="{esc(expression)}">{esc(shown)}</span>'
        )

    md.renderer.rules["report_value"] = render_value

    # --- block structure: source lines, findings, figure grids ---
    def structure(state):
        tokens = state.tokens
        for index, token in enumerate(tokens):
            if token.type == "front_matter":
                build.meta.update(yaml.safe_load(token.content) or {})
            if token.map and token.nesting == 1:
                token.attrSet("data-source-line", str(line_of(token)))
            if token.type == "inline":
                for child in token.children or []:
                    child.meta["line"] = line_of(token)
            if token.type == "admonition_open" and token.meta.get("tag") == "finding":
                words = token.info.split()
                state_name = words[1] if len(words) > 1 else ""  # any status word the author uses
                title = tokens[index + 2].content
                fid = build.unique(f"finding-{slug(title)}", line_of(token))
                token.attrSet("id", fid)
                token.attrSet("data-comment-target", f"finding:{fid}")
                tokens[index + 1].attrSet("data-state", state_name)
                build.findings.append(
                    {
                        "id": fid,
                        "state": state_name,
                        "title": title,
                        "line": line_of(token),
                    }
                )
            if token.type == "paragraph_open" and index + 1 < len(tokens):
                children = tokens[index + 1].children or []
                visible = [
                    c
                    for c in children
                    if not (
                        c.type in ("softbreak", "hardbreak")
                        or (c.type == "text" and not c.content.strip())
                    )
                ]
                if visible and all(c.type == "image" for c in visible):
                    token.tag = tokens[index + 2].tag = "div"
                    token.attrSet("class", "figure-grid")
                    tokens[index + 1].meta["figure_grid"] = True
                    for child in visible:
                        child.meta["card"] = True

    md.core.ruler.push("report_structure", structure)

    def image(tokens, idx, options, env):
        token = tokens[idx]
        line = token.meta.get("line", 0)
        src, source, trace = build.asset(token.attrGet("src"), line)
        if not token.meta.get("card"):
            if source.lower().endswith(VIDEO_SUFFIXES):  # a video inside prose: the browser's own player
                return (f'<video src="{esc(src)}" aria-label="{esc(token.content)}" controls muted loop playsinline '
                        f'preload="metadata"></video>')
            token.attrSet("src", src)
            return default_image(tokens, idx, options, env)
        title = token.content or Path(source).stem
        kind = ("plotly" if source.endswith(".plotly.json")
                else "video" if source.lower().endswith(VIDEO_SUFFIXES) else "image")
        fid = build.unique(
            token.attrGet("id") or f"fig-{slug(Path(source).name.split('.')[0])}", line
        )
        return figure_card(
            build,
            fid=fid,
            src=src,
            title=title,
            caption=token.attrGet("title") or "",
            line=line,
            source=source,
            size=build.sizes.get(src.split("?")[0]),
            fps=build.rates.get(src.split("?")[0]),
            kind=kind,
            trace=trace,
        )

    md.renderer.rules["image"] = image

    # --- fences: python exec, html raw/include, Graphviz, display code ---
    def fence(tokens, idx, options, env):
        token = tokens[idx]
        info = token.info.split()
        line = line_of(token)
        if info[:2] == ["python", "exec"]:
            return python_exec(token, set(info[2:]), line)
        if info[:2] == ["html", "raw"]:
            return raw_html(token, line)
        if info[:2] == ["html", "include"]:
            return include_html(token, line)
        if info and info[0] in GRAPHVIZ_ENGINES:
            return graphviz(token, line)
        if info[:1] == ["reports"]:
            return child_cards(token, line)
        return (
            f'<div data-source-line="{line}">'
            + default_fence(tokens, idx, options, env)
            + "</div>"
        )

    def python_exec(token, flags, line):
        stdout = io.StringIO()
        emitted.clear()
        metrics.clear()
        graphs.clear()
        exec_line[0] = line
        read_before = set(build.sources)
        with contextlib.redirect_stdout(stdout):
            exec(compile(token.content, f"{path}:line-{line}", "exec"), namespace)
        result = f'<div class="exec-block" data-source-line="{line}">'
        if "hide" not in flags:
            result += f'<pre class="src"><code>{esc(token.content)}</code></pre>'
        if stdout.getvalue():
            result += f'<pre class="out">{esc(stdout.getvalue())}</pre>'
        if metrics:
            result += f'<section class="metric-grid" aria-label="Summary metrics">{"".join(metrics)}</section>'
        if emitted:
            markup = "".join(
                check_html(item, f"{path}:{line} report_html") for item in emitted
            )
            result += f'<div class="raw-html">{markup}</div>'
        cards = list(graphs)  # graph(...) diagrams first, in call order, then Matplotlib figures
        for position, number in enumerate(plt.get_fignums()):
            figure = plt.figure(number)
            payload = io.BytesIO()
            figure.savefig(payload, format="png", dpi=160, bbox_inches="tight")
            label = figure.get_label()
            suptitle = figure._suptitle.get_text() if figure._suptitle else ""
            axes_title = figure.axes[0].get_title() if figure.axes else ""
            title = suptitle or axes_title or label or f"Figure from line {line}"
            fid = build.unique(
                f"fig-{slug(label)}" if label else f"fig-line-{line}-{position}", line
            )
            inputs = sorted(build.label(source) for source in set(build.sources) - read_before)
            cards.append(
                figure_card(
                    build,
                    fid=fid,
                    src=(generated := build.generated_asset(payload.getvalue(), ".png")),
                    size=build.sizes.get(generated.split("?")[0]),
                    title=title,
                    caption=getattr(figure, "_cairn_caption", None) or f"Generated by report.md line {line}",
                    line=line,
                    trace={
                        "generated": "python exec block",
                        **({"inputs": inputs} if inputs else {}),
                    },
                )
            )
        plt.close("all")
        if cards:
            result += f'<div class="figure-grid">{"".join(cards)}</div>'
        return result + "</div>"

    def raw_html(token, line):
        markup = token.content
        check_html(markup, f"{path}:{line}")
        return f'<div class="raw-html" data-source-line="{line}">{markup}</div>'

    def include_html(token, line):
        names = token.content.split()
        if len(names) != 1:
            raise ValueError(
                f"{path}:{line}: html include takes exactly one fragment path"
            )
        fragment = (build.content_root / names[0]).resolve()
        if build.content_root.resolve() not in fragment.parents:
            raise ValueError(f"{path}:{line}: included HTML must live inside the report's folder")
        if fragment.suffix != ".html" or not fragment.is_file():
            raise ValueError(f"{path}:{line}: missing HTML fragment {names[0]}")
        build.track(fragment)
        markup = check_html(fragment.read_text(), str(fragment))
        source = esc(fragment.relative_to(PROJECT.resolve()))
        return f'<div class="raw-html" data-source-line="{line}" data-source-file="{source}">{markup}</div>'

    def child_cards(token, line):
        """A card per child report (or the named children): status, title, summary, findings, update time."""
        reports = discover_reports()
        children = {key[len(build.key):].strip("/"): key for key in reports if parent_of(key, reports) == build.key}
        wanted = token.content.split() or list(children)
        cards = []
        for name in wanted:
            if name not in children:
                raise ValueError(f"{path}:{line}: no child report {name!r} (have: {', '.join(children) or 'none'})")
            key = children[name]
            data_file = output_dir(key) / "report-data.json"
            if not data_file.is_file():
                raise ValueError(f"{path}:{line}: child report {name!r} is not built; build all reports")
            data = json.loads(data_file.read_text())
            build.track(reports[key] / "report.md")
            build.links.append({"key": key, "anchor": None, "line": line})
            counts = {}
            for finding in data.get("findings", []):
                if finding["state"]:
                    counts[finding["state"]] = counts.get(finding["state"], 0) + 1
            tally = " · ".join(f"{count} {state}" for state, count in sorted(counts.items()))
            details = " · ".join(part for part in (tally, f"updated {data.get('updated', '')}") if part)
            status = f'<span class="direction-status">{esc(data["status"])}</span>' if data.get("status") else ""
            cards.append(
                f'<article class="direction-card" id="{card_id(key)}" data-comment-target="report:{esc(key)}">'
                f'{status}<h3><a href="{esc(page_href(key, build.key))}">{esc(data["title"])}</a></h3>'
                f'<p>{esc(data.get("summary", ""))}</p><small>{esc(details)}</small></article>'
            )
        return f'<div class="direction-grid" data-source-line="{line}">{"".join(cards)}</div>'

    def graphviz(token, line):
        """Lay out a Graphviz diagram at build time; the page receives a static SVG."""
        words = shlex.split(token.info)
        engine, options_ = words[0], dict(
            word.split("=", 1) for word in words[1:] if "=" in word
        )
        card = render_graph(token.content, engine, options_.get("title", "Diagram"),
                            options_.get("caption", ""), options_.get("id"), line)
        return (
            f'<div class="figure-grid diagram" data-source-line="{line}">{card}</div>'
        )

    def render_graph(source, engine, title, caption, fid, line):
        """A Graphviz figure card: from a `dot` fence, or from graph(...) in a python exec block."""
        if engine not in ("dot", "neato", "fdp", "sfdp", "circo", "twopi"):
            raise ValueError(f"{path}:{line}: unknown Graphviz engine {engine!r}")
        if not shutil.which(engine):
            raise ValueError(
                f"{path}:{line}: Graphviz `{engine}` not found; add graphviz to pixi [dependencies]"
            )
        brace = source.find("{")
        if brace < 0:
            raise ValueError(
                f"{path}:{line}: Graphviz source needs a graph body {{ … }}"
            )
        source = source[: brace + 1] + " " + GRAPHVIZ_DEFAULTS + source[brace + 1 :]
        result = subprocess.run(
            [engine, "-Tsvg"], input=source, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0 and "No formats found" in result.stderr:
            # conda-forge Graphviz registers its plugins in a post-link step that pixi skips;
            # `dot -c` writes the plugin index into the environment once.
            subprocess.run(["dot", "-c"], capture_output=True, check=True, timeout=30)
            result = subprocess.run(
                [engine, "-Tsvg"],
                input=source,
                capture_output=True,
                text=True,
                timeout=30,
            )
        if result.returncode != 0:
            raise ValueError(f"{path}:{line}: Graphviz failed: {result.stderr.strip()}")
        fid = build.unique(fid or f"fig-{slug(title)}", line)
        svg = build.generated_asset(result.stdout.encode(), ".svg")
        return figure_card(
            build,
            fid=fid,
            src=svg,
            size=build.sizes.get(svg.split("?")[0]),
            title=title,
            caption=caption,
            line=line,
            trace={"generated": f"Graphviz {engine}"},
        )

    md.renderer.rules["fence"] = fence

    for name in ("math_block", "math_block_label"):
        if name in md.renderer.rules:
            rule = md.renderer.rules[name]
            md.renderer.rules[name] = (
                lambda rule: lambda tokens, idx, options, env: (
                    f'<div data-source-line="{line_of(tokens[idx])}">'
                    + rule(tokens, idx, options, env)
                    + "</div>"
                )
            )(rule)

    md.renderer.rules["table_open"] = lambda tokens, idx, options, env: (
        '<div class="table-wrap">' + md.renderer.renderToken(tokens, idx, options, env)
    )
    md.renderer.rules["table_close"] = lambda tokens, idx, options, env: (
        md.renderer.renderToken(tokens, idx, options, env) + "</div>"
    )
    default_admonition = md.renderer.rules["admonition_open"]

    def admonition_open(tokens, idx, options, env):
        opened = default_admonition(tokens, idx, options, env)
        state_name = tokens[idx + 1].attrGet("data-state")
        return opened + (
            f'<span class="finding-state">{esc(state_name)}</span>'
            if state_name
            else ""
        )

    md.renderer.rules["admonition_open"] = admonition_open

    default_link = md.renderer.rules.get("link_open") or (
        lambda tokens, idx, options, env: md.renderer.renderToken(tokens, idx, options, env)
    )

    def link_open(tokens, idx, options, env):
        """`report:<path>#anchor` links another report; targets are checked after the build."""
        token = tokens[idx]
        href = token.attrGet("href") or ""
        if href.startswith("report:"):
            line = token.meta.get("line", 0)
            match = REPORT_LINK.match(href)
            if not match:
                raise ValueError(f"{path}:{line}: malformed report link {href!r}; use report:<path>#anchor (report:/ is the root)")
            target, anchor = match.groups()
            target = "" if target == "/" else target
            build.links.append({"key": target, "anchor": anchor, "line": line})
            token.attrSet("href", page_href(target, build.key, anchor))
            token.attrSet("class", "report-link")
        return default_link(tokens, idx, options, env)

    md.renderer.rules["link_open"] = link_open
    md.renderer.rules["front_matter"] = lambda *args: ""
    rendered = md.render(strip_comments(path.read_text()))
    build.typed = typed_numbers(path.read_text())
    # The summary may quote results as {{ }} values; they are evaluated after every block ran.
    summary = str(build.meta.get("summary", "")).strip()
    if summary:
        def summary_value(match):
            try:
                return str(evaluate_value(match.group(1).strip(), namespace, f"{path}:summary"))
            except Exception as error:
                raise ValueError(f"{path}: cannot evaluate summary value {{{{ {match.group(1).strip()} }}}}: {error}") from error
        build.meta["summary"] = VALUE.sub(summary_value, summary)
    return rendered


def page_ids(key: str) -> set[str] | None:
    """Anchors of a built report (headings, findings, figures), or None if never built."""
    narrative = output_dir(key) / "narrative.html"
    if not narrative.is_file():
        return None
    return set(re.findall(r'\sid="([^"]+)"', narrative.read_text()))


STATIC = "_static"  # page files shared by every report, named by content hash so browsers cache them for good
STATIC_NAMES: dict[str, str] | None = None


def write_static() -> dict[str, str]:
    """Write the shared page files (and gzip copies) once; returns original name -> hashed name."""
    folder = OUTPUT_ROOT / STATIC
    folder.mkdir(parents=True, exist_ok=True)
    files = {name: page_file(name).read_bytes() for name in PAGE_FILES if name != "index.html"}
    files["plotly.min.js"] = get_plotlyjs().encode()
    for index, sheet in enumerate(CONFIG.extra_css if CONFIG else ()):
        files[f"custom-{index}.css"] = sheet.read_bytes()
    names = {}
    for name, data in files.items():
        stem, extension = name.split(".", 1)
        hashed = f"{stem}.{hashlib.sha256(data).hexdigest()[:12]}.{extension}"
        target = folder / hashed
        if not target.is_file():
            target.write_bytes(data)
            (folder / f"{hashed}.gz").write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
        names[name] = hashed
    return names


def prune_static(names: dict[str, str]) -> None:
    """After a full build, drop shared files no report refers to any more."""
    keep = set(names.values()) | {f"{name}.gz" for name in names.values()}
    for file in (OUTPUT_ROOT / STATIC).iterdir():
        if file.name not in keep:
            file.unlink()


def page_html(key: str, names: dict[str, str], versions: dict[str, str]) -> str:
    """The report's index.html: shared hashed page files, and versioned URLs of this report's data,
    so everything but index.html itself can be cached for good."""
    root = "../" * depth(key)
    prefix = root + STATIC + "/"
    page = page_file("index.html").read_text()
    page = re.sub(r'\s*<script src="plotly\.min\.js"></script>', "", page)  # loaded on demand by report.js
    page = re.sub(
        r'(href|src)="(report\.css|report-extra\.css|report\.js)"',
        lambda match: f'{match.group(1)}="{prefix}{names[match.group(2)]}"',
        page,
    )
    page = page.replace(f'<script src="{prefix}{names["report.js"]}"',
                        f'<script src="{prefix}{names["report.js"]}" data-plotly="{prefix}{names["plotly.min.js"]}" '
                        f'data-root="{root or "./"}" data-report="{esc(key)}" '
                        f'data-narrative="narrative.html?v={versions["narrative.html"]}" '
                        f'data-report-data="report-data.json?v={versions["report-data.json"]}"', 1)
    extra = [f'  <link rel="stylesheet" href="{prefix}{hashed}">' for name, hashed in names.items() if name.startswith("custom-")]
    if extra:  # project styles load after the default ones, so they override them
        page = page.replace("</head>", "\n".join(extra) + "\n</head>", 1)
    return page


OWN_FILES = ("index.html", "narrative.html", "report-data.json", "manifest.json")


def build_report(key: str, folder: Path) -> Build:
    build = Build(key, folder)
    build.generated.mkdir(parents=True, exist_ok=True)
    for name in OWN_FILES:  # only this report's files: child reports live in sub-folders of its output
        (build.generated / name).unlink(missing_ok=True)
    if (build.generated / ASSETS).exists():
        shutil.rmtree(build.generated / ASSETS)
    names = STATIC_NAMES or write_static()
    narrative = render(build)
    (build.generated / "narrative.html").write_text(narrative)
    parent = parent_of(key)
    data = {
        "key": key,
        "title": str(build.meta.get("title") or (folder.name if key else "Report")),
        "subtitle": build.meta.get("subtitle", ""),
        "status": build.meta.get("status", ""),
        "summary": str(build.meta.get("summary", "")).strip(),
        "eyebrow": build.meta.get("eyebrow", ""),
        "href": report_href(key),
        "parent": None if parent is None else {"key": parent, "title": front_matter_title(discover_reports().get(parent)),
                                               "href": report_href(parent) + f"#{card_id(key)}"},
        "source": build.markdown.relative_to(PROJECT).as_posix(),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "findings": build.findings,
        "source_runs": sorted(build.runs),
        "links_to": sorted({link["key"] for link in build.links}),
    }
    (build.generated / "report-data.json").write_text(json.dumps(data, indent=2) + "\n")
    versions = {name: digest(build.generated / name)[:12] for name in ("narrative.html", "report-data.json")}
    (build.generated / "index.html").write_text(page_html(key, names, versions))  # the one file browsers revalidate
    builder = [Path(__file__).resolve(), *(page_file(name) for name in PAGE_FILES), *(CONFIG.extra_css if CONFIG else ())]
    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "report": key,
        "code_revision": code_revision(),
        "markdown_source": str(build.markdown.relative_to(PROJECT)),
        "markdown_sha256": digest(build.markdown),
        "selection": "run:<name> resolves outputs/<name>/latest once at build time (or the matching MLflow run)",
        "source_runs": dict(sorted(build.runs.items())),
        "mlflow_runs": sorted(build.mlflow_runs.values(), key=lambda run: run["run_id"]),
        "hub_runs": sorted(build.hub_runs.values(), key=lambda run: run["run"]),
        "report_links": build.links,
        "typed_numbers": build.typed,
        "sources": dict(sorted(build.sources.items())),
        "builder": {
            (str(file.relative_to(PROJECT)) if PROJECT in file.parents else f"cairn_report/{file.relative_to(PACKAGE_TEMPLATE.parent)}"): digest(file)
            for file in builder if file.is_file()
        },
        "tooling": tooling_info(),
        "runtime": {
            "python": sys.version.split()[0],
            "packages": {name: package_version(name) for name in RENDER_PACKAGES},
            "plotly.js": {"version": get_plotlyjs_version(), "sha256": digest(OUTPUT_ROOT / STATIC / names["plotly.min.js"])},
            "graphviz": graphviz_version(),
        },
    }
    (build.generated / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return build


def write_site(reports: dict[str, Path]) -> None:
    """site.json: every report of the tree (title, status, summary, path), for each page's navigation.

    A report not built yet (a single-report build) is listed from its front matter, so the navigation
    always shows the whole tree.
    """
    entries = []
    for key in sorted(reports, key=lambda key: (key != "", key)):
        data_file = output_dir(key) / "report-data.json"
        data = json.loads(data_file.read_text()) if data_file.is_file() else {
            "key": key, "title": front_matter_title(reports[key]) if key else root_title(), "href": report_href(key)}
        entries.append({field: data.get(field) for field in ("key", "title", "subtitle", "status", "summary", "href")}
                       | {"parent": parent_of(key, reports)})
    page = CONFIG.page if CONFIG else {}
    site = {"title": entries[0]["title"] if entries else "Reports", "reports": entries,
            "copy_preamble": page.get("copy_preamble", "")}
    (OUTPUT_ROOT / "site.json").write_text(json.dumps(site, indent=2) + "\n")


def check_links(builds: list[Build], reports: dict[str, Path]) -> None:
    """Every report: link must name an existing report and, if given, an existing anchor."""
    problems = []
    names = ", ".join(key or "/" for key in reports)
    for build in builds:
        for link in build.links:
            where = f"{build.markdown}:{link['line']}"
            shown = link["key"] or "/"
            if link["key"] not in reports:
                problems.append(f"{where}: no report {shown!r} (reports: {names})")
                continue
            if link["anchor"]:
                ids = page_ids(link["key"])
                if ids is None:
                    problems.append(f"{where}: report {shown!r} is not built; build all reports")
                elif link["anchor"] not in ids:
                    problems.append(f"{where}: report {shown!r} has no anchor #{link['anchor']}")
    if problems:
        raise ValueError("broken report links:\n  " + "\n  ".join(problems))


def build_all(config: Config | None = None, report: str | None = None) -> list[Build]:
    """Build every report (or one, by its path) and check report links; print one line per report."""
    configure(config or load_config())
    reports = discover_reports()
    if not reports:
        raise SystemExit(f"no report found: expected {REPORTS_ROOT / 'report.md'}")
    key = None if report is None else report.strip("/")
    if key is not None and key not in reports:
        raise SystemExit(f"unknown report {report!r}; reports: {', '.join(k or '/' for k in reports)}")
    global STATIC_NAMES
    if key is None and OUTPUT_ROOT.exists():  # a full build starts clean, keeping the shared files
        for child in OUTPUT_ROOT.iterdir():
            if child.name != STATIC:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    STATIC_NAMES = write_static()
    selected = {key: reports[key]} if key is not None else reports  # children before parents
    builds = [build_report(key_, folder) for key_, folder in selected.items()]
    if key is None:
        prune_static(STATIC_NAMES)
    write_site(reports)
    check_links(builds, reports)
    for build in builds:
        print(f"Built {build.key or '/'}: {build.generated.relative_to(PROJECT)}/index.html "
              f"({build.figures} figures, {len(build.findings)} findings, {len(build.typed)} typed numbers)")
    return builds
