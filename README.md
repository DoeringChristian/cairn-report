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

## Development

```bash
uv sync
uv run pytest
```
