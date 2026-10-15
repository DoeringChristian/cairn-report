"""Build single-page reports from Markdown into a static site.

A report is a folder `reports/<slug>/` with a `report.md` (front matter + Markdown), an optional
`assets/` folder, and optional `data/`. There is no nesting:

    reports/<slug>/report.md   ->  .cairn/build/<slug>/index.html
                                     .cairn/build/index.html   (the project index)

The data a report reads is declared in its front matter (`inputs:`), pinned to exact artifact
versions at build time, and enforced: reading anything else in the project fails the build. Pages
link to each other with `[text](report:<slug>#anchor)`, checked at build time. The output is
static: any web server, the project server, or a store server can serve it.
"""

from __future__ import annotations

import gzip
import contextlib
import contextvars
import csv
import hashlib
import html
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
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

from .._version import __version__
from ..client.open import open_store
from ..config import Config, load_config, protect
from ..formats import METRICS, fold_scalars, fold_series, read_jsonl
from ..client.offline import local_folder, resolve_version
from ..client.replica import exact_ref
from ..store import StoreError, default_root, refs, data_root

PACKAGE_TEMPLATE = Path(__file__).resolve().parent / "template"
# Set by configure() from the project's Config before any report is built.
CONFIG: Config | None = None
PROJECT = REPORTS_ROOT = OUTPUT_ROOT = Path()
SEGMENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
INPUT_NAME = re.compile(r"[a-z_][a-z0-9_]*")
INPUT_IMAGE = re.compile(r"input:([a-z_][a-z0-9_]*)/(.+)")
REPORT_LINK = re.compile(r"report:([a-z0-9][a-z0-9_-]*)(?:#([A-Za-z0-9_.:-]+))?$")
PAGE_FILES = ("index.html", "report.css", "report-extra.css", "report.js")
ASSETS = "assets"


def configure(config: Config) -> None:
    global CONFIG, PROJECT, REPORTS_ROOT, OUTPUT_ROOT
    CONFIG = config
    PROJECT, REPORTS_ROOT = config.project, config.reports_dir
    OUTPUT_ROOT = config.output_dir


def page_file(name: str) -> Path:
    """A page file from the project's template_dir when it provides one, else the packaged default."""
    if CONFIG and CONFIG.template_dir and (CONFIG.template_dir / name).is_file():
        return CONFIG.template_dir / name
    return PACKAGE_TEMPLATE / name


def tooling_info() -> dict:
    """Which cairn produced the build: version, install source, and style overrides."""
    from importlib import metadata

    info = {"package": "cairn", "version": __version__}
    try:
        direct = metadata.distribution("cairn").read_text("direct_url.json")
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

    Results must be {{ }} values computed from input files; design constants such as a
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


class BuildError(ValueError):
    """A report cannot be built as written: undeclared data, a bad input, a misplaced report."""


def discover_reports() -> dict[str, Path]:
    """Every report, keyed by its slug (the folder name below the report root), in slug order.

    Reports are single pages: `reports/<slug>/report.md` and nothing deeper.
    """
    if not REPORTS_ROOT.is_dir():
        return {}
    misplaced = [REPORTS_ROOT / "report.md"] if (REPORTS_ROOT / "report.md").is_file() else []
    found: dict[str, Path] = {}
    for child in sorted(REPORTS_ROOT.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        misplaced += sorted(file for file in child.rglob("report.md") if file != child / "report.md")
        if not (child / "report.md").is_file():
            continue
        if SEGMENT.fullmatch(child.name):
            found[child.name] = child
        else:
            print(f"warning: {child.relative_to(REPORTS_ROOT.parent)}: skipped; report folder names "
                  f"are lower-case letters, digits, '-' and '_'", file=sys.stderr)
    if misplaced:
        shown = ", ".join(file.relative_to(REPORTS_ROOT.parent).as_posix() for file in misplaced)
        raise BuildError(f"{shown}: reports are single pages: move it to {REPORTS_ROOT.name}/<slug>/report.md "
                         f"(one folder per report, no nesting and no root report)")
    return found


def output_dir(key: str) -> Path:
    return OUTPUT_ROOT / key


def report_href(key: str) -> str:
    """A report's address relative to the site root, so a site works under any prefix (e.g. a store server)."""
    return f"{key}/"


def page_href(target: str, current: str, anchor: str | None = None) -> str:
    """A link from one report page to another (or to itself), relative to the current page."""
    fragment = f"#{anchor}" if anchor else ""
    return (fragment or "./") if target == current else f"../{report_href(target)}{fragment}"


FRONT_MATTER = re.compile(r"---\n(.*?)\n---", re.S)


def front_matter(folder: Path) -> dict:
    """A report's front matter, without building it."""
    match = FRONT_MATTER.match((folder / "report.md").read_text())
    meta = yaml.safe_load(match.group(1)) if match else None
    return meta if isinstance(meta, dict) else {}


def front_matter_title(folder: Path) -> str:
    """A report's title from its front matter (its folder name if it has none)."""
    return str(front_matter(folder).get("title") or folder.name)


# ------------------------------------------------------------------ strict data access

AUDITED = ("open", "os.listdir", "os.scandir")
_GUARD: contextvars.ContextVar = contextvars.ContextVar("cairn_guard", default=None)
_HOOKED = False


def real(path) -> str:
    return os.path.realpath(path)


def inside(path: str, roots) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def environment_roots() -> list[str]:
    """The Python environment (standard library, site-packages): code and data of the tools, not of the project."""
    found = {sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix, *sysconfig.get_paths().values()}
    return sorted({real(path) for path in found if path})


def source_line(markdown: Path, fallback):
    """The report line of the `python exec` code that is running now (the block's own line if unknown)."""
    frame = sys._getframe(1)
    pattern = re.escape(f"{markdown}:") + r"line-(\d+)"
    while frame is not None:
        match = re.fullmatch(pattern, frame.f_code.co_filename)
        if match:
            return int(match.group(1)) + frame.f_lineno
        frame = frame.f_back
    return fallback


class Guard:
    """What report code may open while a block runs: the report folder, its generated output, its inputs.

    Anything else inside the project or the store (its cache and blobs) is undeclared data. Paths
    elsewhere (the standard library, site-packages, temporary files, font caches) are not watched.
    """

    def __init__(self, build: "Build", line):
        self.build, self.line, self.busy = build, line, False
        self.allowed = build.allowed_roots()
        self.watched = [real(PROJECT), real(default_root()), real(data_root()),
                        *([real(build.store_root)] if build.store_root else [])]
        self.environment = environment_roots()
        self.violations: list[str] = []

    def check(self, event: str, target) -> None:
        if isinstance(target, int):
            return
        try:
            file = os.fspath(target)
        except TypeError:
            return
        path = real(os.fsdecode(file))
        if not inside(path, self.watched) or inside(path, self.allowed) or inside(path, self.environment):
            return
        if {"site-packages", "__pycache__"} & set(path.split(os.sep)):
            return
        frame = sys._getframe(2)
        while frame is not None:
            if frame.f_code.co_filename.startswith("<frozen importlib"):
                return  # importing code is not reading data
            frame = frame.f_back
        line = source_line(self.build.markdown, self.line)
        message = (f"{self.build.markdown}:{line}: the code reads {os.fsdecode(file)}, which is not part of this report's data; "
                   f"declare it under inputs: in the front matter (or keep it in the report folder)")
        self.violations.append(message)
        raise BuildError(message)


def _audit(event: str, args: tuple) -> None:
    if event not in AUDITED:
        return
    guard = _GUARD.get()
    if guard is None or guard.busy:
        return
    guard.busy = True
    try:
        guard.check(event, args[0])
    finally:
        guard.busy = False


@contextlib.contextmanager
def guarded(build: "Build", line):
    """Enforce the report's declared inputs on everything the code in the block opens.

    The audit hook is installed once per process and does nothing outside this block. A violation
    raises where it happens and is raised again when the block ends, in case the code swallowed it.
    """
    global _HOOKED
    if not _HOOKED:
        sys.addaudithook(_audit)
        _HOOKED = True
    guard = Guard(build, line)
    token = _GUARD.set(guard)
    try:
        yield guard
    finally:
        _GUARD.reset(token)
    if guard.violations:
        raise BuildError(guard.violations[0])


class Inputs:
    """The `inputs` object of `python exec` blocks: `inputs.name` or `inputs["name"]` is the folder of a declared input."""

    def __init__(self, paths: dict[str, Path]):
        self.__dict__["_paths"] = dict(paths)

    def _missing(self, name: str) -> str:
        declared = ", ".join(self.__dict__["_paths"]) or "none"
        return f"no input {name!r}: declare it under inputs: in the front matter (declared: {declared})"

    def __getattr__(self, name: str) -> Path:
        try:
            return self.__dict__["_paths"][name]
        except KeyError:
            raise AttributeError(self._missing(name)) from None

    def __getitem__(self, name: str) -> Path:
        try:
            return self.__dict__["_paths"][name]
        except KeyError:
            raise KeyError(self._missing(name)) from None

    def __iter__(self):
        return iter(self.__dict__["_paths"])

    def __len__(self) -> int:
        return len(self.__dict__["_paths"])

    def __contains__(self, name) -> bool:
        return name in self.__dict__["_paths"]

    def __repr__(self) -> str:
        return f"Inputs({', '.join(self.__dict__['_paths'])})"


class Build:
    """State gathered while rendering one report: sources, inputs, ids, findings, links."""

    def __init__(self, key: str, folder: Path):
        self.key = key
        self.markdown = folder / "report.md"
        self.content_root = folder
        self.generated = output_dir(key)
        self.meta: dict = {}
        self.inputs: dict[str, dict] = {}  # local name -> requested, resolved (exact ref), files, path (read-only folder)
        self.store_root: Path | None = None
        self.line = None  # the report line being evaluated, for messages
        self.links: list[dict] = []
        self.typed: list[dict] = []
        self.sources: dict[str, str] = {}
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

    def resolve_inputs(self) -> None:
        """Pin each declared input to an exact version and materialize it (not recorded in .cairn-inputs.jsonl)."""
        declared = self.meta.get("inputs") or {}
        if not isinstance(declared, dict):
            raise BuildError(f"{self.markdown}: inputs: must map local names to artifact references, e.g. descent: descent@latest")
        if not declared:
            return
        store = open_store(CONFIG)
        self.store_root = getattr(store, "root", None)  # a remote store has no folder
        for name, text in declared.items():
            where = f"{self.markdown}: inputs.{name}"
            if not isinstance(name, str) or not INPUT_NAME.fullmatch(name):
                raise BuildError(f"{where}: input names are lower-case identifiers ([a-z_][a-z0-9_]*)")
            try:
                ref = refs.parse(text)
            except ValueError as error:
                raise BuildError(f"{where}: {error}") from error
            if ref.path:
                raise BuildError(f"{where}: {text!r} names a file; an input is a whole artifact version (use {name}/<file> in the report)")
            try:  # through the replica: this machine's pending versions; with the server down, the cache and last answers
                project, artifact, version = resolve_version(store, ref)
                exact = exact_ref(project, artifact, version)
                folder = local_folder(store, project, artifact, version)
            except StoreError as error:
                raise BuildError(f"{where}: {error}") from error
            self.inputs[name] = {"requested": str(text), "resolved": exact, "files": len(version["files"]), "path": folder}

    def allowed_roots(self) -> list[str]:
        """Where this report may read: its folder, its generated output, and its inputs."""
        return [real(self.content_root), real(self.generated), *(real(item["path"]) for item in self.inputs.values())]

    def source_key(self, resolved: Path) -> str | None:
        """How the manifest names a file the report may read (None if it may not)."""
        path = str(resolved)
        if inside(path, [real(self.content_root)]):
            return resolved.relative_to(PROJECT.resolve()).as_posix()
        for name, item in self.inputs.items():
            if inside(path, [real(item["path"])]):
                return f"input:{name}/{resolved.relative_to(real(item['path'])).as_posix()}"
        return None

    def track(self, path: Path) -> Path:
        """Record a file the report depends on, by its resolved (immutable) path; only the report's own data is allowed."""
        resolved = Path(real(path))
        key = self.source_key(resolved)
        if key is None:
            raise BuildError(f"{self.markdown}:{source_line(self.markdown, self.line)}: {path} is outside this report's folder and "
                             f"its declared inputs; declare it under inputs: in the front matter")
        resolved = Path(path).resolve(strict=True)
        self.sources[key] = digest(resolved)
        return resolved

    # ------------------------------------------------------------------ figures

    def asset(self, src: str, line: int) -> tuple[str, str, dict]:
        """Copy a figure into generated/assets/: a path relative to the report, or `input:<name>/<file>`."""
        where = f"{self.markdown}:{line}"
        match = INPUT_IMAGE.fullmatch(src)
        if match:
            name, inside_input = match.groups()
            if name not in self.inputs:
                raise BuildError(f"{where}: figure {src!r} uses input {name!r}, which is not declared under inputs: in the front matter")
            file = self.inputs[name]["path"] / inside_input
        elif re.match(r"^[a-z]+:", src) or src.startswith("//"):
            raise ValueError(f"{where}: figures must be local files, or input:<name>/<file>; got {src!r}")
        else:
            file = self.content_root / src
        if not file.exists():
            raise ValueError(f"{where}: missing figure {src!r} (paths are relative to the report, or input:<name>/<file>)")
        self.line = line
        resolved = self.track(file)
        key = self.source_key(resolved)
        relative = "inputs/" + key[len("input:"):] if key.startswith("input:") else key
        target = self.generated / ASSETS / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, target)
        if match:
            item = self.inputs[match.group(1)]
            number = item["resolved"].rsplit("@", 1)[1]
            label = f"{match.group(1)}@{number}//{resolved.relative_to(real(item['path'])).as_posix()}"
            trace = {"input": label, "artifact": item["resolved"], "label": label}
        else:
            trace = {"path": relative, "label": relative}
        self.sizes[f"{ASSETS}/{relative}"] = image_size(resolved)
        if resolved.suffix.lower() in (".mp4", ".m4v", ".mov"):
            self.rates[f"{ASSETS}/{relative}"] = (mp4_info(resolved) or {}).get("fps")
        version = self.sources[key][:12]  # content hash: the URL changes when the file does
        return f"{ASSETS}/{relative}?v={version}", trace["label"], trace

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
        style = f' style="--figure-ratio:{min(max(ratio, 0.75), 10):.3f};--image-ratio:{ratio:.4f}"'
        dims = f' width="{size[0]}" height="{size[1]}"'
    attrs = (
        f'class="{classes}"{style} id="{esc(fid)}" '
        f'data-figure-id="{esc(fid)}" data-figure-type="{kind}" data-src="{esc(src)}" '
        f'data-line="{line}" data-trace="{esc(json.dumps(trace))}"'
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

    def read_json(file):
        return json.loads(build.track(file).read_text())

    def read_csv(file):
        with build.track(file).open(newline="") as handle:
            return list(csv.DictReader(handle))

    def read_events(file):
        return read_jsonl(build.track(file))

    def read_scalars(folder):
        """Latest value per key from the append-only metrics.jsonl in a folder (e.g. an input)."""
        return fold_scalars(read_events(Path(folder) / METRICS))

    def read_series(folder, key):
        """Every logged value of one key from a folder's metrics.jsonl, as {"value", "step", "time"}."""
        return fold_series(read_events(Path(folder) / METRICS), key)

    def metric(label: str, value, detail: str = "", fmt: str | None = None):
        shown = format(value, fmt) if fmt else str(value)
        metrics.append(
            f'<article class="metric">'
            f"<span>{esc(label)}</span><strong>{esc(shown)}</strong><small>{esc(detail)}</small></article>"
        )

    namespace = {
        "REPORT_ROOT": build.content_root,
        "PROJECT_ROOT": PROJECT,
        "META": build.meta,
        "inputs": Inputs({name: item["path"] for name, item in build.inputs.items()}),
        "read_json": read_json,
        "read_csv": read_csv,
        "read_jsonl": read_events,
        "read_scalars": read_scalars,
        "read_series": read_series,
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
        line = build.line = tokens[idx].meta.get("line", "?")
        try:
            with guarded(build, line):
                shown = evaluate_value(expression, namespace, f"{path}:value")
        except Exception as error:
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
            raise BuildError(f"{path}:{line}: the reports fence was removed: reports are single pages; "
                             f"link another report with [text](report:<slug>)")
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
        exec_line[0] = build.line = line
        read_before = set(build.sources)
        with contextlib.redirect_stdout(stdout), guarded(build, line):
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
            read = sorted(source for source in set(build.sources) - read_before)
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
                        **({"inputs": read} if read else {}),
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
        """`report:<slug>#anchor` links another report; targets are checked after the build."""
        token = tokens[idx]
        href = token.attrGet("href") or ""
        if href.startswith("report:"):
            line = token.meta.get("line", 0)
            match = REPORT_LINK.match(href)
            if not match:
                raise ValueError(f"{path}:{line}: malformed report link {href!r}; use report:<slug>#anchor")
            target, anchor = match.groups()
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
                with guarded(build, 1):
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
    root = "../"
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
                        f'data-root="{root}" data-report="{esc(key)}" '
                        f'data-narrative="narrative.html?v={versions["narrative.html"]}" '
                        f'data-report-data="report-data.json?v={versions["report-data.json"]}"', 1)
    extra = [f'  <link rel="stylesheet" href="{prefix}{hashed}">' for name, hashed in names.items() if name.startswith("custom-")]
    if extra:  # project styles load after the default ones, so they override them
        page = page.replace("</head>", "\n".join(extra) + "\n</head>", 1)
    return page


OWN_FILES = ("index.html", "narrative.html", "report-data.json", "manifest.json")


def input_record(build: Build) -> dict:
    """The declared inputs as the manifest and report-data.json record them."""
    return {name: {field: item[field] for field in ("requested", "resolved", "files")} for name, item in build.inputs.items()}


def build_report(key: str, folder: Path) -> Build:
    build = Build(key, folder)
    build.meta = front_matter(folder)
    protect(CONFIG.cairn_dir, build.generated)
    build.generated.mkdir(parents=True, exist_ok=True)
    for name in OWN_FILES:
        (build.generated / name).unlink(missing_ok=True)
    if (build.generated / ASSETS).exists():
        shutil.rmtree(build.generated / ASSETS)
    names = STATIC_NAMES or write_static()
    build.resolve_inputs()
    narrative = render(build)
    (build.generated / "narrative.html").write_text(narrative)
    data = {
        "key": key,
        "title": str(build.meta.get("title") or folder.name),
        "subtitle": build.meta.get("subtitle", ""),
        "status": build.meta.get("status", ""),
        "summary": str(build.meta.get("summary", "")).strip(),
        "eyebrow": build.meta.get("eyebrow", ""),
        "href": report_href(key),
        "source": build.markdown.relative_to(PROJECT).as_posix(),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "inputs": input_record(build),
        "findings": build.findings,
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
        "inputs": input_record(build),
        "report_links": build.links,
        "typed_numbers": build.typed,
        "sources": dict(sorted(build.sources.items())),
        "builder": {
            (str(file.relative_to(PROJECT)) if PROJECT in file.parents else f"cairn/{file.relative_to(PACKAGE_TEMPLATE.parent)}"): digest(file)
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


def site_entries(reports: dict[str, Path]) -> list[dict]:
    """Every report (title, status, summary, address, update time, inputs), flat and sorted by title.

    A report not built yet (a single-report build) is listed from its front matter, so the navigation
    and the index always show every report.
    """
    entries = []
    for key, folder in reports.items():
        data_file = output_dir(key) / "report-data.json"
        if data_file.is_file():
            data = json.loads(data_file.read_text())
        else:
            meta = front_matter(folder)
            declared = meta.get("inputs") if isinstance(meta.get("inputs"), dict) else {}
            data = {"key": key, "title": str(meta.get("title") or folder.name), "subtitle": meta.get("subtitle", ""),
                    "status": meta.get("status", ""), "summary": str(meta.get("summary", "")).strip(), "href": report_href(key),
                    "inputs": {name: {"requested": str(ref)} for name, ref in declared.items()}}
        entries.append({field: data.get(field) for field in ("key", "title", "subtitle", "status", "summary", "href", "updated", "inputs")})
    return sorted(entries, key=lambda entry: (entry["title"].lower(), entry["key"]))


def index_html(entries: list[dict], names: dict[str, str]) -> str:
    """The project index page: a card per report, in the report styles, without scripts."""
    sheets = [names["report.css"], names["report-extra.css"], *(hashed for name, hashed in names.items() if name.startswith("custom-"))]
    cards = []
    for entry in entries:
        count = len(entry["inputs"] or {})
        details = " · ".join(part for part in (f"updated {entry['updated']}" if entry["updated"] else "not built yet",
                                               f"{count} input{'' if count == 1 else 's'}") if part)
        status = f'<span class="direction-status">{esc(entry["status"])}</span>' if entry["status"] else ""
        cards.append(f'<article class="direction-card">{status}<h3><a href="{esc(entry["href"])}">{esc(entry["title"])}</a></h3>'
                     f'<p>{esc(entry["summary"] or entry["subtitle"] or "")}</p><small>{esc(details)}</small></article>')
    body = "".join(cards) or '<p class="subtitle">No reports yet.</p>'
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width,initial-scale=1">\n  <title>Reports</title>\n'
        + "".join(f'  <link rel="stylesheet" href="{STATIC}/{sheet}">\n' for sheet in sheets)
        + '</head>\n<body>\n  <aside class="rail"><a class="brand" href="./"><span>R</span> <span class="site-title">Reports</span></a></aside>\n'
        f'  <main id="top">\n    <header class="hero"><div><h1>Reports</h1><p class="subtitle">{esc(PROJECT.name)}</p></div></header>\n'
        f'    <div class="direction-grid">{body}</div>\n  </main>\n</body>\n</html>\n'
    )


def write_site(reports: dict[str, Path], names: dict[str, str]) -> None:
    """site.json (the flat report list for each page's navigation) and the project index page."""
    entries = site_entries(reports)
    page = CONFIG.page if CONFIG else {}
    site = {"title": "Reports", "reports": entries, "copy_preamble": page.get("copy_preamble", "")}
    (OUTPUT_ROOT / "site.json").write_text(json.dumps(site, indent=2) + "\n")
    (OUTPUT_ROOT / "index.html").write_text(index_html(entries, names))


def check_links(builds: list[Build], reports: dict[str, Path]) -> None:
    """Every report: link must name an existing report and, if given, an existing anchor."""
    problems = []
    names = ", ".join(reports)
    for build in builds:
        for link in build.links:
            where = f"{build.markdown}:{link['line']}"
            if link["key"] not in reports:
                problems.append(f"{where}: no report {link['key']!r} (reports: {names})")
                continue
            if link["anchor"]:
                ids = page_ids(link["key"])
                if ids is None:
                    problems.append(f"{where}: report {link['key']!r} is not built; build all reports")
                elif link["anchor"] not in ids:
                    problems.append(f"{where}: report {link['key']!r} has no anchor #{link['anchor']}")
    if problems:
        raise ValueError("broken report links:\n  " + "\n  ".join(problems))


def build_all(config: Config | None = None, report: str | None = None) -> list[Build]:
    """Build every report (or one, by its slug) and check report links; print one line per report."""
    configure(config or load_config())
    reports = discover_reports()
    if not reports:
        raise SystemExit(f"no report found: expected {REPORTS_ROOT / '<slug>' / 'report.md'}")
    key = None if report is None else report.strip("/")
    if key is not None and key not in reports:
        raise SystemExit(f"unknown report {report!r}; reports: {', '.join(reports)}")
    global STATIC_NAMES
    if key is None and OUTPUT_ROOT.exists():  # a full build starts clean, keeping the shared files
        for child in OUTPUT_ROOT.iterdir():
            if child.name != STATIC:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    STATIC_NAMES = write_static()
    selected = {key: reports[key]} if key is not None else reports
    builds = [build_report(key_, folder) for key_, folder in selected.items()]
    if key is None:
        prune_static(STATIC_NAMES)
    write_site(reports, STATIC_NAMES)
    check_links(builds, reports)
    for build in builds:
        print(f"Built {build.key}: {build.generated.relative_to(PROJECT) if PROJECT in build.generated.parents else build.generated}/index.html "
              f"({build.figures} figures, {len(build.findings)} findings, {len(build.typed)} typed numbers)")
    return builds
