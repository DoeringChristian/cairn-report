# cairn-report

Three small pieces for research projects that keep their evidence in plain run
folders:

- a **builder** that turns a tree of Markdown reports into a static site, reading
  numbers and figures straight from run outputs;
- a **run logger** that writes evidence to fixed places in a run folder (and,
  optionally, to MLflow or a hub while the run executes);
- a **hub**: a token-protected artifact store for reports and runs, with push
  and pull.

cairn-report provides mechanisms only. How reports are organised, which sections
they have, and what counts as evidence is up to the project (for agents, the
`standalone-research-project` skill decides that).

## Install

In a pixi project:

```toml
[dependencies]
graphviz = "*"   # only needed for Graphviz diagram fences

[pypi-dependencies]
cairn-report = { git = "https://github.com/DoeringChristian/cairn-report.git", tag = "v0.8.0" }

[tasks]
report-build = "cairn-report build"
report-serve = "cairn-report serve --host 127.0.0.1 --port 8000"
```

With uv or pip: `uv add git+https://github.com/DoeringChristian/cairn-report.git`.
Optional extras: `cairn-report[mlflow]` (MLflow backend), `cairn-report[mlflow-server]`
(a project-managed MLflow server).

## Reports

A report is a folder with a `report.md` and, optionally, an `assets/` folder and
child reports (sub-folders with their own `report.md`). The configured report
folder (default `reports/`) is the root of the tree:

```text
reports/                       ->  generated/reports/
├── report.md                  ->  index.html            (the root page, served at /)
├── assets/                    ->  images, Plotly JSON, HTML fragments, data
└── alpha/                     ->  alpha/index.html
    ├── report.md
    ├── assets/
    └── beta/report.md         ->  alpha/beta/index.html
```

```bash
cairn-report build             # build the whole tree into generated/reports/
cairn-report build --report alpha/beta
cairn-report serve             # build, then serve at http://127.0.0.1:8000/
```

`serve` moves to the next free port when the requested one is taken
(`--strict-port` fails instead). The output is a static site: any web server,
the project server, or a hub can serve it, at any address prefix.

### Writing a report

Front matter is optional: `title` (default: the folder name), `subtitle`,
`status`, `summary`, `eyebrow`. The body is Markdown with:

- `python exec` fences (`hide` to run without showing the code) that read runs:
  `run_dir(name)`, `run_trials(study)`, `read_scalars`, `read_json`, `read_csv`,
  `read_jsonl`, `mlflow_run(...)`, `track(path)`, plus `metric(...)` cards.
  Matplotlib figures a block creates become figure cards (`caption(text)` sets
  the current figure's caption), and `graph(source, id=..., title=...,
  caption=...)` turns generated Graphviz source into one, for example a model
  architecture drawn from code;
- `{{ expression }}` values in prose and in the `summary`;
- figures as Markdown images: a path in the report folder, `run:<name>/<file>`
  (the run's latest folder, `run:<name>@<timestamp>/<file>` for a fixed one), or
  `mlflow:<run-id>/<file>`; Plotly JSON files become interactive figures,
  Matplotlib figures drawn in `python exec` blocks are embedded, and Graphviz
  fences (`dot`, `neato`, ...) are laid out at build time;
- `!!! <type> "Title"` callouts; `!!! finding <status> "Title"` blocks are
  collected with their status and counted on the parent's child cards;
- `[text](report:<path>#anchor)` links to other reports (`report:/` is the root),
  checked at build time;
- a ```` ```reports ```` fence: a card per child report (title, status, summary,
  finding counts), or only the children it names;
- `$...$` / `$$...$$` mathematics (MathML), `html raw` and `html include`
  fences, and `<!-- ... -->` comments, which never reach the page.

Each page links to its parent, lists the whole tree in its sidebar, and opens
figures in a lightbox that shows where they came from (run, the code that drew
and logged them, the commit, the MLflow or hub run). Each build writes a
`manifest.json` per report: every file read with its hash, the runs used and
their status, links, typed numbers in prose, and the tooling versions.

### Comments

Readers select text or click a figure's comment icon; comments are kept in that
browser only, per report. **Copy** puts them on the clipboard as JSON (with the
report's source path and URL, after an optional `[page] copy_preamble`), and
**Clear** removes them from the browser.

### Loading and caching

The page files and Plotly are shared in `generated/reports/_static/`, named by
content hash and stored gzipped too, so browsers download them once. Each
`index.html` points at versioned URLs of its report data, so switching between
reports costs only `index.html`. Figures and plots load as they approach the
screen, image sizes are recorded at build time (no layout jumps), and each
report remembers the reader's position in the browser.

### Sharing as one page

```bash
cairn-report share                 # build, then write generated/share/index.html
cairn-report share --report alpha  # only alpha and its children
```

`share` writes the whole tree as one self-contained page for hosts that take a
single file, such as claude.ai artifacts: every report's text, its figures as
data URIs, and its Plotly data, with the styles and script inline. The sidebar
and `report:` links switch reports inside the page, and a plain `#anchor` in
the page's address opens the report that has it. Only Plotly's runtime is
fetched, from jsdelivr at the version the build bundles. The page keeps the
comments (Copy and Clear) and leaves out the build manifest. A page above
16 MB fails with its largest figures listed.

Publish the file exactly as written; it says so in its first lines. On
claude.ai, that is the Artifact tool with this `file_path` and nothing else. To
change the page, edit the report and run `share` again.

## Logging runs

```python
from cairn_report.logger import RunLogger

log = RunLogger(output_dir)                  # one logger per run folder
log.scalar("rmse", rmse)                     # metrics.jsonl; step=... for a series
log.table("cases", rows)                     # tables/cases.csv
log.figure("errors", error_histogram, errs)  # figures/errors.png, records the plot function
log.plotly("trace", spec)                    # figures/trace.plotly.json
log.json("summary", result)                  # data/summary.json
log.image("sample", array)                   # images/sample.png
log.artifact("model", checkpoint_path)       # artifacts/model.<suffix>
log.text("solver", solver_log)               # logs/solver.txt
```

`metrics.jsonl` and `artifacts.jsonl` are append-only; every event records the
code location that logged it. `read_scalars(run)` and `read_artifacts(run)` fold
them to the latest value per name and the latest entry per file.

Where evidence goes is set in `cairn-report.toml`:

```toml
[logger]
backends = ["local"]   # "local" (default), "mlflow", "hub" (implies local); combine freely
```

### MLflow

```toml
[logger]
backends = ["local", "mlflow"]   # or ["mlflow"] alone

[logger.mlflow]
tracking_uri = "https://mlflow.example.org"  # or MLFLOW_TRACKING_URI; credentials via MLflow's env vars
experiment = "my-study"                      # default: the project folder name
```

One MLflow run per run folder: scalars become metrics, files become artifacts at
the same paths, the Hydra config becomes parameters, and the run's indexes,
`.hydra/`, `provenance/`, and `run.json` are uploaded at the end, so an MLflow run
has the layout of a run folder; its status follows `run.json`. With MLflow
alone, an MLflow error fails the logging call. Reports resolve `run:` references
and `run_dir()` through MLflow when the evidence is not local.

With `serve = true` (plus `port`, `store`), the project runs its own MLflow
server: `cairn-report serve` starts or reuses it next to the reports, and
`cairn-report mlflow` runs it alone.

## Hub

A hub stores each project's **report** (every push that changes it is a new,
complete version of its sources and built pages) and its **runs** (files under
`outputs/`, version by version). Nothing stored is deleted or overwritten, and
the hub never runs pushed code.

```bash
cairn-report hub init /srv/hub     # data folder + random token (owner-only)
cairn-report hub serve /srv/hub    # 127.0.0.1:8800; put an HTTPS reverse proxy in front
```

In a project:

```toml
[hub]
url = "https://hub.example.org"
project = "my-study"                              # default: the folder name
token_file = "~/.config/cairn-report/hub-token"   # chmod 600; or CAIRN_HUB_TOKEN
exclude = [".submitit/**/*.pkl"]                  # never pushed
interval = 30                                     # seconds between pushes with --watch / the hub backend
```

```bash
cairn-report push                  # a new report version (if changed) and new run files
cairn-report push report|runs      # only one of them
cairn-report push --watch -- python experiments/run.py   # push while a command runs, and after
cairn-report pull                  # the latest report back (refuses to overwrite local changes; --force)
cairn-report pull --runs [NAME]    # also runs; --version N for an older report
cairn-report pull --name P --into DIR   # restore another project into a folder
```

With `[logger] backends = ["hub"]`, the logger itself sends a run's files while
the run executes (every `interval` seconds, shortly after each logged file, and
once more at exit); a hub that cannot be reached only produces warnings. Reports
resolve `run:` references from the hub when the runs are not local.

The hub serves `/` (every project), `/p/<project>/` (the latest report),
`/p/<project>@<n>/` (version n), and `/runs/<project>/` (runs and their files).
Clients authenticate with the token as a bearer header; browsers log in once and
keep an HttpOnly, SameSite=Strict cookie (`--insecure-cookies` for plain-HTTP
tunnels). The client refuses plain HTTP to a non-local hub unless `--allow-http`.
`exclude` takes gitignore-style globs on project paths: `*` and `?` stay within
a folder, `**` spans folders, a leading `/` anchors to the project root.

## Configuration

Everything has a default; `cairn-report.toml` at the project root changes it:

```toml
reports_dir = "reports"            # the root report folder
output_dir = "generated/reports"   # the built site
outputs_dir = "outputs"            # run folders

[style]
css = ["style.css"]                # extra stylesheets, loaded after the default style
template_dir = "template"          # replaces index.html, report.css, report-extra.css, report.js by name

[page]
copy_preamble = "..."              # text put before copied comments
```

## Development

```bash
uv sync --extra mlflow
uv run pytest
```
