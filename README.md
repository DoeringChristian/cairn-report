# cairn-report

Small pieces for research projects that keep their evidence in plain run
folders:

- a **builder** that turns single-page Markdown reports into a static site, reading
  numbers and figures from the artifacts each report declares as inputs;
- a **run logger** that writes evidence to fixed places in a run folder (files only;
  `cairn-report publish --watch` syncs a running job's folder to the store);
- an **artifact store**: named, versioned folders (datasets, run outputs, reports) in a local
  folder or on a token-protected server, with `publish`, `pull`, and lineage;
- **run bookkeeping** for Hydra projects: each run's `run.json`, the `latest`
  link, study completion, and a source snapshot per run.

cairn-report provides mechanisms only. How reports are organised, which sections
they have, and what counts as evidence is up to the project (for agents, the
`standalone-research-project` skill decides that).

## Install

In a pixi project:

```toml
[dependencies]
graphviz = "*"   # only needed for Graphviz diagram fences

[pypi-dependencies]
cairn-report = { git = "https://github.com/DoeringChristian/cairn-report.git", tag = "v0.13.0" }

[tasks]
report-build = "cairn-report build"
report-serve = "cairn-report serve --host 127.0.0.1 --port 8000"
```

With uv or pip: `uv add git+https://github.com/DoeringChristian/cairn-report.git`.
Optional extras: `cairn-report[video]` (`log.video` from frames),
`cairn-report[hydra]` (run bookkeeping for Hydra projects).

## Reports

A report is one page: a folder `reports/<slug>/` (the slug is lower-case letters,
digits, `-` and `_`) with a `report.md` and, optionally, an `assets/` folder and
a `data/` folder. There is no nesting and no root report: a project holds
several flat reports, one per research direction. A `report.md` anywhere else
(`reports/report.md`, `reports/a/b/report.md`) fails the build with "reports are
single pages: move it to reports/<slug>/".

```text
reports/                       ->  generated/reports/
├── alpha/                     ->  alpha/index.html
│   ├── report.md
│   ├── assets/                    images, Plotly JSON, HTML fragments
│   └── data/                      small data kept with the report
└── beta/report.md             ->  beta/index.html
                                   index.html           (the project index, served at /)
```

```bash
cairn-report build             # build every report into generated/reports/
cairn-report build --report beta
cairn-report serve             # build, then serve at http://127.0.0.1:8000/
```

The build also writes `generated/reports/index.html`, a project index with one
card per report (title, status, summary, update time, number of inputs), and
`site.json`, the flat report list the sidebar uses. `serve` moves to the next
free port when the requested one is taken (`--strict-port` fails instead). The
output is a static site: any web server, the project server, or a store server can
serve it, at any address prefix.

### Writing a report

Front matter is optional: `title` (default: the folder name), `subtitle`,
`status`, `summary`, `eyebrow`, and `inputs`. The body is Markdown with:

- `python exec` fences (`hide` to run without showing the code) that read the
  report's data: `inputs.<name>`, `read_scalars`, `read_json`, `read_csv`,
  `read_jsonl`, `track(path)`, plus `metric(...)` cards.
  Matplotlib figures a block creates become figure cards (`caption(text)` sets
  the current figure's caption), and `graph(source, id=..., title=...,
  caption=...)` turns generated Graphviz source into one, for example a model
  architecture drawn from code;
- `{{ expression }}` values in prose and in the `summary` (which may use inputs
  too);
- figures as Markdown images: a path in the report folder, or
  `input:<name>/<file>` for a file of a declared input; Plotly JSON files become
  interactive figures, videos (`.mp4`, `.m4v`, `.mov`, `.webm`) become video
  cards, Matplotlib figures drawn in `python exec` blocks are embedded, and
  Graphviz fences (`dot`, `neato`, ...) are laid out at build time;
- `!!! <type> "Title"` callouts; `!!! finding <status> "Title"` blocks are
  collected with their status in `report-data.json`;
- `[text](report:<slug>#anchor)` links to other reports, checked at build time;
- `$...$` / `$$...$$` mathematics (MathML), `html raw` and `html include`
  fences, and `<!-- ... -->` comments, which never reach the page.

### Inputs

A report declares the artifacts it reads in its front matter, by local name:

```yaml
---
title: Descent
inputs:
  descent: descent@latest          # [project:]name[@vN|@alias|@label], as in `store.pull`
  crops: datasets/crops@v3
---
```

Local names are lower-case identifiers (`[a-z_][a-z0-9_]*`). At build time each
reference is resolved to an exact version (`project:name@vN`; aliases only
resolve completed versions), materialized read-only through the store's cache,
and exposed in `python exec` blocks as `inputs`, an object whose attributes and
items are `Path`s:

```python
scores = read_scalars(inputs.descent)                         # metrics.jsonl of the input
table = read_csv(inputs["crops"] / "tables" / "cases.csv")
```

Images use `input:descent/figures/trajectory.png`. The lightbox of such a figure
shows the input reference (`descent@v3//figures/trajectory.png`) and the exact
artifact version. The store comes from `[store]` in `cairn-report.toml`; the
build does not touch `.cairn-inputs.jsonl`: a report records its inputs in its
own manifest instead.

**Strict.** A report may read only its own folder and its declared inputs.
The read helpers raise a build error for any other path. While a `python exec`
block (or a `{{ }}` value) runs, an audit hook also fails the build when the
code opens a file (or lists a folder) inside the project or the store cache that
is neither in the report folder nor in a declared input, for example
`open("outputs/run/metrics.jsonl")`. The error names the file and the report
line and says to declare it under `inputs:`. Paths elsewhere (the standard
library, site-packages, temporary files, font caches) and writes into the
report's generated output stay allowed. Known gaps: code that reads through a
subprocess or a native library that opens files without Python's `open`, files
reached through a path outside the project and the cache (a store kept
elsewhere is watched, other locations are not), and imports of project modules
(importing is not reading data).

The manifest (`generated/reports/<slug>/manifest.json`) and `report-data.json`
record `inputs: {name: {requested, resolved, files}}`; the manifest also lists
every file read with its hash, links, typed numbers in prose, and the tooling
versions.

### Pages

Every report page shows a compact **Data** section right after the header: one
row per input with its local name, the exact version (`name@vN`, with the exact
reference in a `data-artifact` attribute) and its file count, and a button that
copies the exact reference. The sidebar lists the project's reports flat, sorted
by title, with the current page highlighted; the brand links to the project
index. A folder whose name is not a valid slug is skipped with a warning. Pages
open figures in a lightbox that shows where they came from. A video card loads
near the viewport and plays muted and looped while it is on screen (not with
reduced motion). In the lightbox it has a player: play/pause (click, Space,
`k`), a scrub bar, frame steps (`,` `.`, or a sideways or Shift mouse wheel),
±1 s (Shift+←/→), speed from 0.1× to 2×, and the same zoom and pan as images.
The frame rate is read from the MP4, so frame steps are exact. `serve` and the
store server answer range requests, so browsers can seek.

### Publishing a report

```bash
cairn-report publish reports/alpha
```

builds the report and stores it as the next version of the artifact
`reports/alpha`: `source/` (the report folder: `report.md`, `assets/`, `data/`)
and `site/` (its built page and the shared `_static` files it needs, so the
version can be viewed on its own), with metadata `{title, status, summary}` and
the exact input versions as its inputs. A report whose source, inputs, and
metadata are unchanged creates no new version. Lineage follows: the report
version lists its inputs, and each input lists the report as a consumer
(`store.lineage`).

### Comments

Readers select text or click a figure's comment icon; comments are kept in that
browser only, per report. **Copy** puts them on the clipboard as JSON (with the
report's source path and URL, after an optional `[page] copy_preamble`), and
**Clear** removes them from the browser.

Next to each comment button (on a text selection, a figure, the lightbox), a
copy button puts a one-line location marker on the clipboard, to paste into a
chat with an LLM: `reports/a/report.md:42 "the selected text"`, or for a figure
`reports/a/report.md:42 figure #fig-psnr "PSNR per method" (source: <file>)`.

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
cairn-report share --report alpha  # only alpha
```

`share` writes the reports as one self-contained page for hosts that take a
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
log.video("rollout", frames, fps=30)         # videos/rollout.mp4 (H.264, seekable); or a file path
log.artifact("model", checkpoint_path)       # artifacts/model.<suffix>
log.text("solver", solver_log)               # logs/solver.txt
```

`metrics.jsonl` and `artifacts.jsonl` are append-only; every event records the
code location that logged it. `read_scalars(run)` and `read_artifacts(run)` fold
them to the latest value per name and the latest entry per file.

The logger only writes these files into the run folder. To follow a running job
from another machine, sync its folder: `cairn-report publish runs/x/latest x --watch -- python run.py`
adds new and changed files to one running version of the artifact `x` every
`[store] interval` seconds, then completes it (or marks it failed when the command fails).

**Watching a job.** `--watch` follows the folder as it exists when it starts. A Hydra run
creates a new timestamped folder (`outputs/<name>/<timestamp>`), so either publish it after it
finishes (`cairn-report publish outputs/x/latest x`) or have the job write to a fixed folder.

## Hydra runs

`cairn_report.runs` and `cairn_report.snapshot` keep the record of each Hydra
run, so a project's entry point stays a few lines (needs the `hydra` extra):

```python
# experiments/run.py
@hydra.main(version_base=None, config_path="../configs", config_name="config")
def main(cfg: DictConfig) -> None:
    run_experiment(cfg, Experiment)   # from cairn_report.runs; Experiment is the project's base class
```

```yaml
# configs/config.yaml
run:
  name: feasibility            # a short lowercase slug
  description: What the run tests.
hydra:
  run: {dir: "outputs/${run.name}/${now:%Y-%m-%d_%H-%M-%S}"}
  sweep: {dir: "outputs/${run.name}/${now:%Y-%m-%d_%H-%M-%S}", subdir: "${hydra.job.num}"}
  callbacks:
    source_snapshot: {_target_: cairn_report.snapshot.SourceSnapshotCallback}
    study_completion: {_target_: cairn_report.runs.StudyCompletion}   # multiruns
```

- `run_experiment` writes `run.json` (`running`, then `completed` or `failed`
  with the error), requires the source snapshot, instantiates `cfg.experiment`,
  and calls its `run(output_dir)`.
- A completed single run becomes `outputs/<name>/latest`, a relative symlink;
  a failed run stays for diagnosis and never moves it.
- `StudyCompletion` writes a multirun study's `run.json` after the last trial
  (completed only when every trial completed) and then moves `latest` to the
  study. Trials never touch `latest`.
- `SourceSnapshotCallback` stores the project's source files (size-bounded,
  secrets and data files excluded) in each run's `provenance/`: in `git` mode
  (default) the commit, the diff, and untracked sources; in `tree` mode a full
  copy. It also records the runtime environment.
- `cairn-report finalize-study outputs/<name>/<timestamp>` completes a study
  whose launcher died, by the same rule, once every trial job has ended.

## Store

The store holds **artifacts**: named, versioned folders inside a **project**. A name is a
slash path (`descent`, `datasets/crops`); reports are the artifacts `reports/<slug>`. A
version is immutable once completed and records its files (by content hash), metadata,
the exact versions it consumed (`inputs`), and where it was published from. Aliases
(`latest` moves by itself; `best`, `paper` by hand) and labels select versions:
`[project:]name[@v3|@alias|@label][//file]`.

A project works offline against a store folder (`.store` by default, `[store] path`). To share
it, serve a folder over HTTP and point projects at the server.

```bash
cairn-report store serve /srv/store      # 127.0.0.1:8800; put an HTTPS reverse proxy in front
```

The first `serve` of a new folder creates it with a random owner token (owner-only, in
`<folder>/token`); every launch prints `Store: <url>` and `Token: <owner token>` to the terminal
(never into a URL). Binding to a non-local `--host` warns: the server speaks plain HTTP.

For local testing, `store serve <folder> --no-auth` needs no login and no token: every request acts
as the owner, and clients connect without `CAIRN_STORE_TOKEN`. It is refused on any `--host` but
loopback.

```bash
cairn-report store token create /srv/store --project my-study --write --name cluster   # printed once
cairn-report store token list /srv/store       # names, projects, access: never token values
cairn-report store token revoke /srv/store cluster
```

The owner token can do everything. A scoped token reads one project, and publishes to it only
with `--write`; everything else is 403 and a scoped token only sees its own project. Scoped
tokens are stored hashed (`tokens.jsonl`); revoking one takes effect immediately, also for
browser sessions (the cookie holds a signed token id, never the token).

In a project:

```toml
[store]
url = "https://store.example.org"                 # omit for a local store
project = "my-study"                              # default: the folder name
token_file = "~/.config/cairn-report/store-token" # chmod 600; or CAIRN_STORE_TOKEN
allow_http = false                                # plain HTTP to a non-local server (default: refused)
exclude = [".submitit/**/*.pkl"]                  # never published: gitignore-style globs on project paths
interval = 30                                     # seconds between syncs with --watch
```

```bash
cairn-report publish data/crops datasets/crops --alias paper      # a folder becomes the next version (none if unchanged)
cairn-report publish runs/x/latest x --watch -- python run.py     # sync a running job; complete (or fail) with it
cairn-report publish reports/alpha                                # build a report and publish reports/alpha
cairn-report pull datasets/crops@paper                            # print the local, read-only copy's path
cairn-report pull x@latest --into DIR [--no-record]               # copy it into DIR instead
```

```python
from cairn_report import store
crops = store.pull("datasets/crops@v3")      # works with a local and a remote store alike
```

`exclude` takes gitignore-style globs on project paths: `*` and `?` stay within a folder, `**`
spans folders, a leading `/` anchors to the project root. Pulled copies live in
`~/.cache/cairn-report/artifacts/`, with a blob cache beside them, so a file is downloaded once.

The server serves the pages for people, with the same login (any valid token, kept as an
HttpOnly, SameSite=Strict cookie; `--insecure-cookies` for plain-HTTP tunnels):

- `/` projects (reports, artifacts, last update);
- `/p/<project>/` its reports (title, status, summary, latest version), then its other artifacts;
- `/p/<project>/r/<slug>[@vN]/` a report version's built page; its Data panel links each input
  to the artifact page of the exact version;
- `/p/<project>/a/<name>[@vN]` an artifact: versions, the files of one version, its metadata,
  its inputs and its consumers (the back-trace view).

The API lives under `/api/v2/` and files under `/files/<project>/<name>@vN//<path>` (ETag, Range);
the routes are listed in `cairn_report/store/server.py`. The server never runs pushed code, but it
serves pushed HTML: give write tokens to people whose reports you trust.

### Migrating from the hub

Before 0.13 the store was the "hub" (`[hub]`, `hub init`, `push`, MLflow, `run:` references),
which is gone without aliases. Convert an old hub folder once:

```bash
cairn-report store migrate /srv/hub /srv/store      # the old folder is only read; blobs are hard-linked
```

Each old project becomes a project. Each `<run>/<timestamp>` becomes a version of the artifact
`<run>` (label: the timestamp; `failed` unless its `run.json` says `completed`). Only the latest
old report version is migrated, split into one `reports/<slug>` artifact per report folder
(`a/b` becomes `a-b`, the root report `overview`), stored as it was and not rebuilt. Reports that
read runs with `run:` must be rewritten by hand to declare `inputs:`. Running it again adds
nothing. `store serve /srv/store` then gives the new folder an owner token if it has none.

## Configuration

Everything has a default; `cairn-report.toml` at the project root changes it:

```toml
reports_dir = "reports"            # holds one folder per report
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
uv sync
uv run pytest
```
