# cairn-report

Commentable research reports built from Markdown and Hydra run outputs, with no
tracking service. A project has one **overview** (`reports/report.md`, served
at `/`) and one report per **research direction** (`reports/<slug>/report.md`).
Reports read their numbers and figures straight from run directories, show where
every figure came from, link to each other, and collect reader comments in an
append-only log.

This is the report tooling of the `standalone-research-project` agent skill,
packaged so projects depend on it instead of copying it.

## Install

In a pixi project:

```toml
[dependencies]
graphviz = "*"   # only needed for Graphviz diagram fences

[pypi-dependencies]
cairn-report = { git = "https://github.com/DoeringChristian/cairn-report.git" }

[tasks]
report-build = "cairn-report build"
report-serve = "cairn-report serve --host 127.0.0.1 --port 8000"
report-host = "cairn-report serve --host 0.0.0.0 --port 8000"
```

Pin a tag or commit (`rev = "..."`) for reproducible builds. With uv or pip:
`uv add git+https://github.com/DoeringChristian/cairn-report.git`.

## Use

```bash
cairn-report init          # reports/report.md (overview) + a sample direction
cairn-report new <slug>    # add reports/<slug>/report.md
cairn-report build         # build every report into generated/reports/<slug>/
cairn-report serve         # build, then serve at http://127.0.0.1:8000/
```

`serve` moves to the next free port when the requested one is taken and prints
the URL it uses; `--strict-port` fails instead. `--project <path>` selects a
project other than the current one.

## Layout

```text
project/
├── reports/                    # content only
│   ├── report.md               # overview: Introduction, Status, Research directions
│   ├── knowledge/comments.jsonl
│   └── <slug>/                 # one direction: report.md, fragments/, media/, knowledge/
├── outputs/<run>/<timestamp>/  # Hydra runs (run.json, metrics.jsonl, artifacts.jsonl, ...)
└── generated/reports/<slug>/   # build output (overview: overview/), git-ignored
```

## Configuration and style

Nothing needs configuring; the default style is the one in
`src/cairn_report/template/`. To change paths or the look, add
`cairn-report.toml` at the project root:

```toml
reports_dir = "reports"
output_dir = "generated/reports"
outputs_dir = "outputs"

[style]
css = ["reports/style.css"]        # extra stylesheets, loaded after the default style
template_dir = "reports/template"  # replaces index.html, report.css, report-extra.css, report.js by name
```

Extra stylesheets override the default through its CSS variables (`--paper`,
`--ink`, `--accent`, `--card`, ...) or ordinary rules; `template_dir` replaces
whole page files. Every build records the package version, its install commit,
and any style overrides in `generated/reports/<slug>/manifest.json`.

## Logging runs

Experiments write their evidence with `RunLogger`, one per Hydra run directory.
The same module defines the run file format the reports read:

```python
from cairn_report.logger import RunLogger, read_artifacts, read_scalars

log = RunLogger(output_dir)
log.scalar("rmse", rmse)                     # single value; step=... for a series
log.table("cases", rows)                     # tables/cases.csv
log.figure("errors", error_histogram, errs)  # figures/errors.png, records the plot function
log.plotly("trace", spec)                    # figures/trace.plotly.json
log.json("summary", result)                  # data/summary.json
log.image("sample", array)                   # images/sample.png
log.artifact("model", checkpoint_path)       # artifacts/model.<suffix>
log.text("solver", solver_log)               # logs/solver.txt
```

`metrics.jsonl` and `artifacts.jsonl` are append-only; each event records the
code location that logged it. `read_scalars(run)` and `read_artifacts(run)`
return the latest value per name and the latest entry per file.

## Loading and caching

Reports stay fast as they grow:

- The page files and Plotly are shared by every report in `generated/reports/_static/`,
  named by content hash and stored gzipped as well, so the browser downloads them
  once and keeps them; each report's `index.html` points at them and at versioned
  URLs of its own data (`narrative.html?v=...`).
- Figures and Plotly load only as they approach the screen; Plotly itself loads
  with the first interactive figure. Cards keep their final size while loading,
  because the build records each image's dimensions.
- The header appears first, then the text, then figures; switching back to a
  report already seen costs only its small `index.html`.
- Each report remembers the reader's position in this browser and returns to it
  on reload (a `#anchor` in the URL takes precedence).

Both servers send `immutable` caching for hashed and versioned files, ETags with
`no-cache` for everything else, and the gzip copies when the browser accepts them.

## Writing reports

Front matter names a report (`title`, `subtitle`, `status`, and for directions a
`summary` of at most 400 characters). The body is Markdown with:

- `python exec` fences that read runs (`run_dir`, `run_trials`, `read_scalars`,
  `read_json`, `read_csv`, `read_jsonl`, `track`) and add `metric(...)` cards;
- `{{ expression }}` values in prose and summaries instead of typed results;
- `!!! finding <tentative|supported|contradicted|superseded> "Qualitative title"`
  blocks and callouts (`question`, `decision`, `prior-art`, `warning`, `important`);
- figures as Markdown images, Plotly JSON, executed Matplotlib, or Graphviz fences;
- `[text](report:<slug>#anchor)` links between reports, checked at build time;
- `$...$` / `$$...$$` mathematics rendered to MathML;
- `<!-- ... -->` comments, which never reach the page.

## MLflow as the tracking backend

`RunLogger` can write to MLflow instead of (or as well as) the run directory:

```toml
# cairn-report.toml
[logger]
backends = ["mlflow"]          # or ["local", "mlflow"] to mirror

[logger.mlflow]
tracking_uri = "https://mlflow.example.org"  # or MLFLOW_TRACKING_URI; credentials via MLflow's env vars
experiment = "my-study"                      # default: the project folder name
```

Install `cairn-report[mlflow]`. One MLflow run is created per Hydra run
directory (tagged `cairn.run_dir`); scalars become MLflow metrics, logged files
become artifacts at the same paths, the Hydra config becomes parameters, and the
run's indexes, `.hydra/`, `provenance/`, and `run.json` are uploaded when it
ends, so an MLflow run has the layout of a local run. The MLflow status follows
`run.json`. With MLflow alone, an MLflow error fails the logging call; when
mirroring, the run directory stays the record.

Reports read MLflow runs directly; nothing in them is specific to the backend:

```markdown
![Error histogram](run:demo/figures/errors.png)       <!-- local run or its MLflow run -->
![Pinned](run:demo@2026-10-13_10-00-00/figures/errors.png)
![By id](mlflow:<run-id>/plots/curve.png)             <!-- any MLflow run -->
```

`run_dir("demo")` and `read_scalars(...)` resolve the same way, and
`mlflow_run(run_id=..., name=..., run_dir=...)` selects a run explicitly; runs
not written by cairn-report work too. The build downloads each run once into
`generated/mlflow/<run-id>/` (refreshed when the run changes), hashes what it
reads, and lists the MLflow runs in the manifest. In the figure lightbox, the
source panel links to the run in the MLflow UI.

**One command for both servers.** With `serve = true`, the project runs its own
tracking server (needs `cairn-report[mlflow-server]`):

```toml
[logger.mlflow]
serve = true
port = 5000          # experiments log to http://127.0.0.1:5000
store = "mlflow"     # its data, in the project (git-ignore it like outputs/)
```

```bash
cairn-report serve    # starts (or reuses) the MLflow server, builds, serves the reports
cairn-report mlflow   # only the MLflow server, e.g. while experiments run
```

Both stop the MLflow server they started on Ctrl-C or SIGTERM. With
`--host 0.0.0.0`, MLflow binds the same interface and report links point at the
host the page was loaded from.

## Hub: one place for every project

A hub stores the runs and reports of many projects, pushed from wherever they
run (a cluster, a workstation), and serves them behind one token.

**Server**

```bash
cairn-report hub init /srv/cairn-hub     # data folder + a random token in /srv/cairn-hub/token
cairn-report hub serve /srv/cairn-hub    # listens on 127.0.0.1:8800
```

Put an HTTPS reverse proxy in front, for example Caddy (forwards the Host header):

```text
hub.example.org {
    reverse_proxy 127.0.0.1:8800
}
```

Open `https://hub.example.org/` and enter the token once; the browser keeps an
HttpOnly, SameSite=Strict session cookie. With only an SSH tunnel
(`ssh -L 8800:127.0.0.1:8800 server`), start the hub with `--insecure-cookies`.

**Client** (in each project):

```toml
# cairn-report.toml
[hub]
url = "https://hub.example.org"
project = "my-study"                              # default: the folder name
token_file = "~/.config/cairn-report/hub-token"   # chmod 600; or CAIRN_HUB_TOKEN
exclude = [".submitit/**/*.pkl"]                  # never pushed (see below)
```

`exclude` takes gitignore-style globs on project paths (`outputs/...`,
`reports/...`, `generated/reports/...`): `*` and `?` stay within one folder, `**`
spans folders, a pattern without a leading `/` matches at any depth, and a
folder pattern excludes everything inside it.

```bash
cairn-report push                                  # one sync
cairn-report push --watch                          # keep syncing every 30 s
cairn-report push --watch -- python experiments/run.py experiment=main
cairn-report fetch my-study ./restore              # download everything back
```

In a cluster job, wrap the experiment so its data leaves the node while it runs
and once more when it ends (also when the job is cancelled with SIGTERM):

```bash
pixi run cairn-report push --watch -- python experiments/run.py experiment=main
```

`push` sends `outputs/` (Hydra runs, including `.hydra/` and the `latest`
links), `reports/` (sources and comment logs), and the built reports. Build
reports before pushing (`cairn-report build`); the hub only serves them.

**How data is kept safe**

- Contents are addressed by SHA-256: the client uploads only what the hub lacks,
  and the hub verifies every upload before storing it (written with fsync and
  an atomic rename).
- Nothing is overwritten or deleted on the hub: a changed file becomes a new
  version in the project's append-only `files.jsonl`; files deleted on the
  client stay on the hub. Back up the hub folder like any other data.
- Interrupted uploads are retried on the next sync; the watch loop backs off
  while the hub is unreachable and always attempts a final push.
- The hub never builds reports or runs pushed code. Everyone who can push can
  also publish pages on the hub, so give the token only to people you trust.
- The client refuses plain HTTP to a non-local hub unless `--allow-http`, and
  the token is never accepted in `cairn-report.toml` or in URLs.

Comments made on the hub are stored on the hub and shown together with the
project's own pushed comments. To act on them, use the page's Copy button and
paste them to whoever (or whichever agent) updates the project; the hub does not
write comments back into the project.

## Development

```bash
uv sync
uv run pytest
```
