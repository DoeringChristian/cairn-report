"""Build the project's reports from Markdown.

    reports/report.md          the overview (super-report), served at /
    reports/<slug>/report.md   one report per research direction or idea
    generated/reports/<slug>/  build output (the overview's slug is "overview")

Paths and style come from `cairn_report.config` (an optional cairn-report.toml).
Directions are built first; the overview's ```reports``` fence then lists them
with the short `summary` from each direction's front matter. Reports link to
each other with `[text](report:<slug>#anchor)` (the overview is
`report:overview`), and the build checks that every target exists.
"""

from __future__ import annotations

import base64
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
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
OVERVIEW = "overview"  # slug of the super-report at reports/report.md
RESERVED = {OVERVIEW, "knowledge", "fragments", "media"}
REPORT_LINK = re.compile(r"report:([a-z0-9][a-z0-9-]*)(?:#([A-Za-z0-9_.:-]+))?$")
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

FINDING_STATES = ("tentative", "supported", "contradicted", "superseded")
GRAPHVIZ_ENGINES = ("dot", "neato", "fdp", "sfdp", "circo", "twopi")
SUMMARY_MAX_CHARS = 400
RAW_HTML_MAX_LINES = 20
RAW_HTML_MAX_CHARS = 1_500
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


def discover_reports() -> dict[str, Path]:
    """Research directions (reports/<slug>/report.md) first, then the overview (reports/report.md)."""
    if not REPORTS_ROOT.is_dir():
        return {}
    reports = {
        folder.name: folder
        for folder in sorted(REPORTS_ROOT.iterdir())
        if folder.is_dir() and folder.name not in RESERVED and SLUG.fullmatch(folder.name)
        and (folder / "report.md").is_file()
    }
    if (REPORTS_ROOT / "report.md").is_file():
        reports[OVERVIEW] = REPORTS_ROOT
    return reports


def output_dir(slug: str) -> Path:
    return OUTPUT_ROOT / slug


def report_href(slug: str) -> str:
    """A report's address relative to the site root, so a site works under any prefix (e.g. a hub)."""
    return "" if slug == OVERVIEW else f"reports/{slug}/"


def page_href(target: str, current: str, anchor: str | None = None) -> str:
    """A link from one report page to another (or to itself), relative to the current page."""
    fragment = f"#{anchor}" if anchor else ""
    if target == current:
        return fragment or "./"
    up = "" if current == OVERVIEW else "../../"
    return (up + report_href(target) or "./") + fragment


def overview_backlink(slug: str) -> dict | None:
    """Every direction page links back to its card in the overview."""
    overview = REPORTS_ROOT / "report.md"
    if slug == OVERVIEW or not overview.is_file():
        return None
    match = re.match(r"---\n(.*?)\n---", overview.read_text(), re.S)
    title = (yaml.safe_load(match.group(1)) or {}).get("title") if match else None
    return {"title": title or "Project overview", "href": f"#direction-{slug}"}  # relative to the site root


class Build:
    """State gathered while rendering one report: sources, runs, ids, findings, links."""

    def __init__(self, slug: str, folder: Path):
        self.slug = slug
        self.markdown = folder / "report.md"
        self.content_root = folder
        self.generated = output_dir(slug)
        self.meta: dict = {}
        self.links: list[dict] = []
        self.typed: list[dict] = []
        self.sources: dict[str, str] = {}
        self.runs: set[str] = set()
        self.mlflow_runs: dict[str, dict] = {}
        self.mirrors: dict[str, dict | None] = {}
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
        """The one completion contract: report evidence needs run.json with status completed.

        Records run.json and the run's provenance manifest and patch in the manifest.
        """
        status_file = run_root / "run.json"
        if not status_file.is_file():
            raise ValueError(
                f"{where}: {run_root} has no run.json, so it cannot count as completed"
            )
        status = json.loads(status_file.read_text())
        if status.get("status") != "completed":
            raise ValueError(
                f"{where}: {run_root} is {status.get('status')!r}, not completed"
            )
        self.track(status_file)
        for name in ("manifest.json", "working-tree.patch"):
            if (run_root / "provenance" / name).is_file():
                self.track(run_root / "provenance" / name)
        self.runs.add(str(run_root.resolve().relative_to(PROJECT.resolve())))
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
        trace["run"] = (mlflow or {}).get("run_dir") or run_root.relative_to(project).as_posix()
        trace["path"] = inside if mlflow else trace["path"]
        trace["label"] = f"{trace['run']}/{inside}" if mlflow else relative.as_posix()
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

    def resolve_run(self, name: str, timestamp: str, where: str) -> Path:
        """outputs/<name>/<timestamp>, or its MLflow run when the evidence is not local."""
        local = OUTPUTS / name / timestamp
        if self.uses_mlflow() and not (local.exists() and self.has_evidence(local.resolve())):
            if local.exists():
                return self.mlflow_run(where, run_dir=local.resolve().relative_to(PROJECT.resolve()).as_posix())
            return self.mlflow_run(where, name=name)
        resolved = local.resolve(strict=True)
        self.require_run(resolved, where)
        return resolved

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
        return f"{ASSETS}/{relative.as_posix()}", trace.get("label", relative.as_posix()), trace


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
) -> str:
    """One gallery card. `trace` says where the figure came from and is shown in the lightbox."""
    build.figures += 1
    trace = {
        "report": f"{build.markdown.relative_to(PROJECT).as_posix()}:{line}",
        **(trace or {}),
    }
    attrs = (
        f'class="figure-card{" plotly-card" if kind == "plotly" else ""}" id="{esc(fid)}" '
        f'data-figure-id="{esc(fid)}" data-figure-type="{kind}" data-src="{esc(src)}" '
        f'data-line="{line}" data-comment-target="figure:{esc(fid)}" data-trace="{esc(json.dumps(trace))}"'
    )
    media = (
        '<div class="plotly-preview"></div>'
        if kind == "plotly"
        else (f'<img src="{esc(src)}" alt="{esc(title)}">')
    )
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

    def run_dir(name: str, timestamp: str = "latest") -> Path:
        """Resolve outputs/<name>/<timestamp> once (locally or from MLflow); refuse runs that did not complete."""
        return build.resolve_run(name, timestamp, f"{path}: run_dir({name!r})")

    def mlflow_run(run_id: str | None = None, name: str | None = None, run_dir: str | None = None) -> Path:
        """A finished MLflow run by id, run directory, or run name, read like a run directory."""
        return build.mlflow_run(f"{path}: mlflow_run({run_id or run_dir or name!r})", run_id=run_id, name=name, run_dir=run_dir)

    def run_trials(study: Path) -> list[dict]:
        """Every trial of a completed Hydra multirun study, in trial order.

        Each entry has `trial`, `dir`, `overrides` (from .hydra/overrides.yaml), and
        `status`; the study's run.json must say completed (StudyCompletion writes it).
        """
        study = Path(study).resolve(strict=True)
        status = build.require_run(study, f"{path}: run_trials({study.name!r})")
        if status.get("kind") != "study":
            raise ValueError(f"{path}: {study} is a single run, not a multirun study")
        trials = []
        for entry in status.get("trials", []):
            folder = study / entry["trial"]
            overrides_file = folder / ".hydra" / "overrides.yaml"
            overrides = yaml.safe_load(build.track(overrides_file).read_text()) if overrides_file.is_file() else []
            trial_status = json.loads(build.track(folder / "run.json").read_text()).get("status")
            trials.append({"trial": entry["trial"], "dir": folder, "overrides": overrides or [], "status": trial_status})
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
                state_name = words[1] if len(words) > 1 else ""
                if state_name not in FINDING_STATES:
                    raise ValueError(
                        f"{path}:{line_of(token)}: finding needs a status, one of {', '.join(FINDING_STATES)}"
                    )
                title = tokens[index + 2].content
                if TYPED_NUMBER.search(title) or "{{" in title:
                    raise ValueError(
                        f"{path}:{line_of(token)}: finding title {title!r} contains a result number; "
                        "state the claim qualitatively and give the numbers in the body as {{ }} values "
                        "(the finding id is derived from the title)"
                    )
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
            token.attrSet("src", src)
            return default_image(tokens, idx, options, env)
        title = token.content or Path(source).stem
        kind = "plotly" if source.endswith(".plotly.json") else "image"
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
            return direction_cards(token, line)
        return (
            f'<div data-source-line="{line}">'
            + default_fence(tokens, idx, options, env)
            + "</div>"
        )

    def python_exec(token, flags, line):
        stdout = io.StringIO()
        emitted.clear()
        metrics.clear()
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
        cards = []
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
                    src="data:image/png;base64,"
                    + base64.b64encode(payload.getvalue()).decode(),
                    title=title,
                    caption=f"Generated by report.md line {line}",
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
        if markup.count("\n") > RAW_HTML_MAX_LINES or len(markup) > RAW_HTML_MAX_CHARS:
            raise ValueError(
                f"{path}:{line}: html raw block exceeds {RAW_HTML_MAX_LINES} lines or "
                f"{RAW_HTML_MAX_CHARS} characters; move it to reports/fragments/<name>.html "
                "and reference it with an `html include` fence"
            )
        check_html(markup, f"{path}:{line}")
        return f'<div class="raw-html" data-source-line="{line}">{markup}</div>'

    def include_html(token, line):
        names = token.content.split()
        if len(names) != 1:
            raise ValueError(
                f"{path}:{line}: html include takes exactly one fragment path"
            )
        fragments = build.content_root / "fragments"
        fragment = (build.content_root / names[0]).resolve()
        if fragments.resolve() not in fragment.parents:
            raise ValueError(
                f"{path}:{line}: fragments must live under the report's fragments/"
            )
        if fragment.suffix != ".html" or not fragment.is_file():
            raise ValueError(f"{path}:{line}: missing HTML fragment {names[0]}")
        build.track(fragment)
        markup = check_html(fragment.read_text(), str(fragment))
        source = esc(fragment.relative_to(PROJECT.resolve()))
        return f'<div class="raw-html" data-source-line="{line}" data-source-file="{source}">{markup}</div>'

    def direction_cards(token, line):
        """The overview's index of research directions: one card per direction report."""
        if build.slug != OVERVIEW:
            raise ValueError(f"{path}:{line}: a reports fence belongs in the overview, reports/report.md")
        directions = {slug: folder for slug, folder in discover_reports().items() if slug != OVERVIEW}
        wanted = token.content.split() or list(directions)
        cards = []
        for slug_ in wanted:
            if slug_ not in directions:
                raise ValueError(f"{path}:{line}: no research direction {slug_!r} (have: {', '.join(directions)})")
            data_file = output_dir(slug_) / "report-data.json"
            if not data_file.is_file():
                raise ValueError(f"{path}:{line}: direction {slug_!r} is not built; build all reports")
            data = json.loads(data_file.read_text())
            build.track(directions[slug_] / "report.md")
            build.links.append({"slug": slug_, "anchor": None, "line": line})
            counts = {}
            for finding in data.get("findings", []):
                counts[finding["state"]] = counts.get(finding["state"], 0) + 1
            tally = " · ".join(f"{count} {state}" for state, count in sorted(counts.items())) or "no findings yet"
            status = f'<span class="direction-status">{esc(data["status"])}</span>' if data.get("status") else ""
            cards.append(
                f'<article class="direction-card" id="direction-{esc(slug_)}" data-comment-target="direction:{esc(slug_)}">'
                f'{status}<h3><a href="{esc(page_href(slug_, build.slug))}">{esc(data["title"])}</a></h3>'
                f'<p>{esc(data.get("summary", ""))}</p>'
                f'<small>{esc(tally)} · updated {esc(data.get("updated", ""))}</small></article>'
            )
        return f'<div class="direction-grid" data-source-line="{line}">{"".join(cards)}</div>'

    def graphviz(token, line):
        """Lay out a Graphviz diagram at build time; the page receives a static SVG."""
        words = shlex.split(token.info)
        engine, options_ = words[0], dict(
            word.split("=", 1) for word in words[1:] if "=" in word
        )
        if not shutil.which(engine):
            raise ValueError(
                f"{path}:{line}: Graphviz `{engine}` not found; add graphviz to pixi [dependencies]"
            )
        source = token.content
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
        title = options_.get("title", "Diagram")
        fid = build.unique(options_.get("id") or f"fig-{slug(title)}", line)
        svg = (
            "data:image/svg+xml;base64,"
            + base64.b64encode(result.stdout.encode()).decode()
        )
        card = figure_card(
            build,
            fid=fid,
            src=svg,
            title=title,
            caption=options_.get("caption", ""),
            line=line,
            trace={"generated": f"Graphviz {engine}"},
        )
        return (
            f'<div class="figure-grid diagram" data-source-line="{line}">{card}</div>'
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
        """`report:<slug>#anchor` links another report; targets are checked after the build."""
        token = tokens[idx]
        href = token.attrGet("href") or ""
        if href.startswith("report:"):
            line = token.meta.get("line", 0)
            match = REPORT_LINK.match(href)
            if not match:
                raise ValueError(f"{path}:{line}: malformed report link {href!r}; use report:<slug>#anchor")
            target, anchor = match.groups()
            build.links.append({"slug": target, "anchor": anchor, "line": line})
            token.attrSet("href", page_href(target, build.slug, anchor))
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


def page_ids(slug: str) -> set[str] | None:
    """Anchors of a built report (headings, findings, figures), or None if never built."""
    narrative = output_dir(slug) / "narrative.html"
    if not narrative.is_file():
        return None
    return set(re.findall(r'\sid="([^"]+)"', narrative.read_text()))


def build_report(slug: str, folder: Path) -> Build:
    build = Build(slug, folder)
    if build.generated.exists():
        shutil.rmtree(build.generated)
    build.generated.mkdir(parents=True)
    for name in PAGE_FILES:
        shutil.copy2(page_file(name), build.generated / name)
    extra = []
    for index, sheet in enumerate(CONFIG.extra_css if CONFIG else ()):
        shutil.copy2(sheet, build.generated / f"custom-{index}.css")
        extra.append(f'  <link rel="stylesheet" href="custom-{index}.css">')
    if extra:  # project styles load after the default ones, so they override them
        page = (build.generated / "index.html").read_text()
        (build.generated / "index.html").write_text(page.replace("</head>", "\n".join(extra) + "\n</head>", 1))
    (build.generated / "plotly.min.js").write_text(get_plotlyjs())
    narrative = render(build)
    if not build.meta.get("title"):
        raise ValueError(f"{build.markdown}: front matter needs a title")
    if slug != OVERVIEW and not str(build.meta.get("summary", "")).strip():
        raise ValueError(
            f"{build.markdown}: a research direction needs a front matter `summary:` "
            "(one to three sentences, shown in the overview)"
        )
    if len(str(build.meta.get("summary", ""))) > SUMMARY_MAX_CHARS:
        raise ValueError(
            f"{build.markdown}: summary is {len(str(build.meta['summary']))} characters; keep it to one to "
            f"three sentences (at most {SUMMARY_MAX_CHARS}) and move detail into the Abstract"
        )
    (build.generated / "narrative.html").write_text(narrative)
    data = {
        "slug": slug,
        "title": build.meta["title"],
        "subtitle": build.meta.get("subtitle", ""),
        "status": build.meta.get("status", ""),
        "summary": str(build.meta.get("summary", "")).strip(),
        "href": report_href(slug),
        "overview": overview_backlink(slug),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "findings": build.findings,
        "source_runs": sorted(build.runs),
        "links_to": sorted({link["slug"] for link in build.links}),
    }
    (build.generated / "report-data.json").write_text(json.dumps(data, indent=2) + "\n")
    builder = [Path(__file__).resolve(), *(page_file(name) for name in PAGE_FILES), *(CONFIG.extra_css if CONFIG else ())]
    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "report": slug,
        "code_revision": code_revision(),
        "markdown_source": str(build.markdown.relative_to(PROJECT)),
        "markdown_sha256": digest(build.markdown),
        "selection": "outputs/<run-name>/latest resolved once at build time (or the matching MLflow run when the "
                     "evidence is in MLflow); runs need run.json status completed",
        "source_runs": sorted(build.runs),
        "mlflow_runs": sorted(build.mlflow_runs.values(), key=lambda run: run["run_id"]),
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
            "plotly.js": {"version": get_plotlyjs_version(), "sha256": digest(build.generated / "plotly.min.js")},
            "graphviz": graphviz_version(),
        },
    }
    (build.generated / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return build


def check_links(builds: list[Build], reports: dict[str, Path]) -> None:
    """Every report: link must name an existing report and, if given, an existing anchor."""
    problems = []
    for build in builds:
        for link in build.links:
            where = f"{build.markdown}:{link['line']}"
            if link["slug"] not in reports:
                problems.append(f"{where}: no report named {link['slug']!r} (reports: {', '.join(reports)})")
                continue
            if link["anchor"]:
                ids = page_ids(link["slug"])
                if ids is None:
                    problems.append(f"{where}: report {link['slug']!r} is not built; build all reports")
                elif link["anchor"] not in ids:
                    problems.append(f"{where}: report {link['slug']!r} has no anchor #{link['anchor']}")
    if problems:
        raise ValueError("broken report links:\n  " + "\n  ".join(problems))


def build_all(config: Config | None = None, report: str | None = None) -> list[Build]:
    """Build every report (or one) and check cross-report links; print a short summary."""
    configure(config or load_config())
    reports = discover_reports()
    if not reports:
        raise SystemExit(f"no reports found: expected {REPORTS_ROOT}/report.md and/or {REPORTS_ROOT}/<slug>/report.md")
    if report and report not in reports:
        raise SystemExit(f"unknown report {report!r}; reports: {', '.join(reports)}")
    selected = {report: reports[report]} if report else reports
    builds = [build_report(slug, folder) for slug, folder in selected.items()]
    check_links(builds, reports)
    for build in builds:
        print(f"Built {build.slug}: {build.generated.relative_to(PROJECT)}/index.html ({build.figures} figures, {len(build.findings)} findings)")
        if build.typed:
            shown = ", ".join(f"{hit['line']}:{hit['text']}" for hit in build.typed[:12])
            more = f" (+{len(build.typed) - 12} more)" if len(build.typed) > 12 else ""
            print(f"  typed numbers in {build.markdown.relative_to(PROJECT)} (line:text): {shown}{more}")
            print("  results must be {{ }} values from run files; leave only design constants typed")
    return builds
