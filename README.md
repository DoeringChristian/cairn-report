# Cairn

Cairn is an artifact store for research projects that keep their evidence in plain folders. It
has five parts:

- the **store**: named, versioned folders (datasets, run outputs, reports) with aliases, metadata,
  and lineage, in a local folder or on a token-protected **server** (`cairn server`) that many jobs
  and clusters share;
- the **UI** (`cairn ui`), which explores a store, local or remote, in a browser;
- **single-page reports**: Markdown pages that read numbers and figures from the artifacts they
  declare as `inputs:`, built into a static site and published as artifacts themselves;
- **drafts** (`with cairn.draft("fit") as run:`): a fresh folder Cairn manages, which is also a run
  logger, and which becomes the artifact's next version when the block ends;
- **Hydra helpers**: `hydra.run.dir: ${cairn.draft:fit}`, each run's `run.json`, study completion, a
  source snapshot.

Cairn provides mechanisms only. How reports are organised, which sections they have, and what
counts as evidence is up to the project.

## Quick start

```bash
pip install git+https://github.com/DoeringChristian/cairn-report.git   # see Install for extras
cd my-project                       # any folder with a pixi.toml, pyproject.toml or cairn.toml
cairn publish runs/fit/latest fit   # a folder becomes version v1 of the artifact "fit" in .cairn/store
cairn pull fit@latest               # prints the path of a read-only local copy
cairn ui                            # explore projects, versions, files and lineage in the browser
```

With no `[store] url` the store is a local folder, `.cairn/store`; nothing needs to run. A job
that pulls an artifact records the exact version it read, and `cairn publish` carries that along
as the new version's `inputs`. A report is a folder `reports/<slug>/report.md` that pins what it
reads:

````markdown
---
title: Fit quality
inputs:
  fit: fit@latest
---

```python exec hide
error = read_scalars(inputs.fit)["rmse"]
```

The final error is {{ error }}.

![errors](input:fit/figures/errors.png)
````

`cairn build` makes the page, `cairn publish reports/<slug>` stores it with its pinned inputs.

A complete small project (dataset → run → report, with lineage) is in
[`examples/descent/`](examples/descent/README.md); `python run_example.py` runs it end to end.

### Coming from cairn-report

Cairn is the next version of cairn-report (0.13.0 is the first release under the new names):

| cairn-report | Cairn |
| --- | --- |
| distribution `cairn-report` | `cairn` |
| `import cairn_report` / `cairn_report.store` | `import cairn` (`cairn.pull`, `cairn.publish`, ...) |
| `cairn_report.store.server` | `cairn.server` |
| command `cairn-report` | `cairn` (also `python -m cairn`) |
| `cairn-report store serve` | `cairn server` |
| `cairn-report store token ...` | `cairn token ...` |
| `cairn-report store migrate` | `cairn migrate` |
| `cairn-report.toml` | `cairn.toml` (the old name is still read, with a warning, when `cairn.toml` is absent) |
| `~/.cache/cairn-report/` | `~/.cache/cairn/` |

The environment variables `CAIRN_STORE_TOKEN` and `CAIRN_INPUTS` keep their names. Everything Cairn writes for a project now lives in `.cairn/` (see Where Cairn keeps things); an old `.store/` is moved there on first use.
The old Cairn tracker (cairn-track) cannot be installed in the same environment.

## Install

In a pixi project:

```toml
[dependencies]
graphviz = "*"   # only needed for Graphviz diagram fences

[pypi-dependencies]
cairn = { git = "https://github.com/DoeringChristian/cairn-report.git", tag = "v0.13.0" }

[tasks]
report-build = "cairn build"
report-serve = "cairn serve --host 127.0.0.1 --port 8000"
```

With uv or pip: `uv add git+https://github.com/DoeringChristian/cairn-report.git`.
The base install is light (`import cairn` loads neither the server, the UI, matplotlib, plotly, nor
hydra). Extras: `cairn[server]` (`cairn server`: Starlette and uvicorn with uvloop and httptools),
`cairn[video]` (`log.video` from frames), `cairn[hydra]` (run bookkeeping for Hydra projects), and
`cairn[ui]`, which adds nothing yet (the UI uses the standard library, and its built files ship with
the package). On the machine that serves a store:

```bash
pip install "cairn[server] @ git+https://github.com/DoeringChristian/cairn-report.git"
# or: uv add "cairn[server] @ git+https://github.com/DoeringChristian/cairn-report.git"
```

Without the extra, `cairn server` stops with a message saying to install it.

## Reports

A report is one page: a folder `reports/<slug>/` (the slug is lower-case letters,
digits, `-` and `_`) with a `report.md` and, optionally, an `assets/` folder and
a `data/` folder. Reports are flat: a project holds
several, one per research direction. A `report.md` anywhere else
(`reports/report.md`, `reports/a/b/report.md`) fails the build with "reports are
single pages: move it to reports/<slug>/".

```text
reports/                       ->  .cairn/build/
├── alpha/                     ->  alpha/index.html
│   ├── report.md
│   ├── assets/                    images, Plotly JSON, HTML fragments
│   └── data/                      small data kept with the report
└── beta/report.md             ->  beta/index.html
                                   index.html           (the project index, served at /)
```

```bash
cairn build             # build every report into .cairn/build/
cairn build --report beta
cairn serve             # build, then serve at http://127.0.0.1:8000/
```

The build also writes `.cairn/build/index.html`, a project index with one
card per report (title, status, summary, update time, number of inputs), and
`site.json`, the flat report list the sidebar uses. `serve` moves to the next
free port when the requested one is taken (`--strict-port` fails instead). The
output is a static site: any web server, the project server, or the store UI can
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
  descent: descent@latest          # [project:]name[@vN|@alias|@label], as in `cairn.pull`
  crops: datasets/crops@v3
---
```

Local names are lower-case identifiers (`[a-z_][a-z0-9_]*`). At build time each
reference is resolved to an exact version (`project:name@vN`, or `project:name@<uid>`
for a version this machine published that is still pending; aliases only resolve
completed versions), materialized read-only through the local replica,
and exposed in `python exec` blocks as `inputs`, an object whose attributes and
items are `Path`s:

```python
scores = read_scalars(inputs.descent)                         # metrics.jsonl of the input
table = read_csv(inputs["crops"] / "tables" / "cases.csv")
```

Images use `input:descent/figures/trajectory.png`. The lightbox of such a figure
shows the input reference (`descent@v3//figures/trajectory.png`) and the exact
artifact version. The store comes from `[store]` in `cairn.toml`; the
build does not touch `.cairn-inputs.jsonl`: a report records its inputs in its
own manifest instead.

**Strict.** A report may read only its own folder and its declared inputs.
The read helpers raise a build error for any other path. While a `python exec`
block (or a `{{ }}` value) runs, an audit hook also fails the build when the
code opens a file (or lists a folder) inside the project or the store cache that
is neither in the report folder nor in a declared input, for example
`open("runs/fit/metrics.jsonl")`. The error names the file and the report
line and says to declare it under `inputs:`. Paths elsewhere (the standard
library, site-packages, temporary files, font caches) and writes into the
report's generated output stay allowed. Known gaps: code that reads through a
subprocess or a native library that opens files without Python's `open`, files
reached through a path outside the project and the cache (a store kept
elsewhere is watched, other locations are not), and imports of project modules
(importing is not reading data).

The manifest (`.cairn/build/<slug>/manifest.json`) and `report-data.json`
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
The frame rate is read from the MP4, so frame steps are exact. `serve`, the
store server and the store UI answer range requests, so browsers can seek.

### Publishing a report

```bash
cairn publish reports/alpha
```

builds the report and stores it as the next version of the artifact
`reports/alpha`: `source/` (the report folder: `report.md`, `assets/`, `data/`)
and `site/` (its built page and the shared `_static` files it needs, so the
version can be viewed on its own), with metadata `{title, status, summary}` and
the exact input versions as its inputs. A report whose source, inputs, and
metadata are unchanged creates no new version. Lineage follows: the report
version lists its inputs, and each input lists the report as a consumer
(`cairn.lineage`).

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

The page files and Plotly are shared in `.cairn/build/_static/`, named by
content hash and stored gzipped too, so browsers download them once. Each
`index.html` points at versioned URLs of its report data, so switching between
reports costs only `index.html`. Figures and plots load as they approach the
screen, image sizes are recorded at build time (no layout jumps), and each
report remembers the reader's position in the browser.

### Sharing as one page

```bash
cairn share                 # build, then write .cairn/share/index.html
cairn share --report alpha  # only alpha
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

## Drafts: writing a version directly

A draft is a fresh, empty folder that Cairn manages and that becomes the next version of an artifact
when it closes. A job writes into it instead of into a `runs/` folder, and nothing needs to be
published afterwards:

```python
import shutil

import cairn

data = cairn.pull("datasets/quadratic@latest")         # recorded as an input of the draft below
with cairn.draft("descent", aliases=["best"]) as run:  # a new version: its own uid folder
    run.scalars({"loss": loss}, step=step)             # the draft is a run logger (see Logging runs)
    run.figure("trajectory", fig)
    shutil.copy("model.pt", run.path)                  # or write files there directly
run.version                                            # descent@v<n>, a cairn.Version
```

- **Where.** `run.path` is `.cairn/drafts/<name>/<uid>/` (git-ignored with the rest of `.cairn/`;
  outside a project, `drafts/` in the data root), with its record beside it (`<uid>.draft.json`).
  The version gets the draft's uid.
- **Closing.** Leaving the block publishes the folder exactly like `cairn.publish(name, run.path)`:
  its inputs, the unchanged check (an unchanged folder makes no new version), the digest, the
  aliases, and `label` (default: the start time, `YYYY-MM-DD_HH-MM-SS`). The folder is then removed:
  its files live in the store (on a local store the blobs are copy-on-write clones, or hard links where
  the filesystem cannot clone; never a copy when avoidable), or in the replica while the server is
  down (`run.version` is pending then). Without `with`, call `run.publish()`, `run.fail()`, or
  `run.discard()`; `Artifact.draft(...)` does the same on the object API.
- **Failing.** An exception keeps the folder for inspection (its path is printed on stderr, its
  state is `kept`), publishes nothing, and re-raises.
- **Inputs.** While a draft is open, `$CAIRN_INPUTS` points at its `.cairn-inputs.jsonl` (restored
  when it closes; nested drafts restore in order), so its pulls, and its subprocesses' pulls, are its
  inputs. Several drafts may be open at once (a model and its evaluation): a pull is recorded in
  every open one, and a pull made while none is open goes into the drafts opened next.
  `evaluation.use(model)` declares another draft (or a `Version`, or a reference) as an input; when
  both are published in one process the input goes first (a draft that closes before its input waits
  for it) and is named by its exact version.
- **Metadata.** `metadata=`, and `config=` (a plain dict) for `metadata["config"]`; `run.set_metadata(...)`
  adds more before it closes. `cairn show` prints the config as `key.sub = value` lines and the UI
  shows it as a table.
- **Watching.** `cairn.draft(name, watch=True)` starts a running version at once and keeps it synced
  while the block runs (like `cairn publish --watch`: every `[store] interval` seconds when something
  changed, with a heartbeat on a store server; `run.sync()` syncs now); closing completes it, an
  exception fails it.
- **`cairn.current_draft()`** returns the draft the process writes into: the innermost open one, or the
  one `$CAIRN_INPUTS` or the working directory lies in (a Hydra job, a subprocess).

```bash
cairn drafts                                  # open, abandoned, and kept drafts: name, uid, state, age, size, path
cairn drafts publish <uid|path>               # publish a kept one (refused if its run.json says failed,
                                              #   unless --allow-incomplete)
cairn drafts discard <uid|path>               # delete it
```

## Logging runs

```python
from cairn.runs.logger import RunLogger      # the same writer a draft is, for a plain folder

log = RunLogger(output_dir)                  # one logger per run folder; with a draft: run.scalar(...), ...
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

The logger only writes these files into the run folder (it lives in `cairn.client.logger`;
`cairn.runs.logger` re-exports it). A draft is a logger on its own folder, so a run that logs into a
draft needs no publishing step; `cairn.draft(name, watch=True)` follows it from another machine
while it runs. A plain folder is followed with `cairn publish runs/x/latest x --watch -- python
run.py`, which adds new and changed files to one running version of the artifact `x` every
`[store] interval` seconds, then completes it (or marks it failed when the command fails).

**Watching a job.** `--watch` follows the folder as it exists when it starts. A Hydra run that
writes into a timestamped folder (`runs/<name>/<timestamp>`) is either published after it finishes
(`cairn publish runs/x/latest x`) or, simpler, given a draft as its run dir (see Hydra runs).

## Hydra runs

`cairn.runs.hydra` and `cairn.runs.snapshot` keep the record of each Hydra
run, so a project's entry point stays a few lines (needs the `hydra` extra):

```python
# experiments/run.py
@hydra.main(version_base=None, config_path="../configs", config_name="config")
def main(cfg: DictConfig) -> None:
    run_experiment(cfg, Experiment)   # from cairn.runs; Experiment is the project's base class
```

```yaml
# configs/config.yaml
run:
  name: feasibility            # a short lowercase slug
  description: What the run tests.
hydra:
  run: {dir: "${cairn.draft:feasibility}"}                                  # a draft: published when the run completes
  sweep: {dir: "${cairn.draft:feasibility-sweep}", subdir: "${hydra.job.num}"}   # one draft, trials as subfolders
  callbacks:
    source_snapshot: {_target_: cairn.runs.snapshot.SourceSnapshotCallback}
    study_completion: {_target_: cairn.runs.hydra.StudyCompletion}   # multiruns
```

- `${cairn.draft:<name>}` is an OmegaConf resolver, registered when `cairn.runs.hydra` is imported
  (the entry point's `from cairn.runs.hydra import run_experiment` does it). It is cached, so every
  resolution in a job, and every trial of a sweep, gets the same draft folder.
- `run_experiment` writes `run.json` (`running`, then `completed` or `failed`
  with the error), requires the source snapshot, instantiates `cfg.experiment`,
  and calls its `run(output_dir)`. In a draft it also records the job's pulls in the run
  folder (a trial's own subfolder in a sweep), and `cairn.current_draft()` gives the experiment
  the draft to log into (`draft.scalar(...)`; a view on its trial folder in a sweep).
- A completed single run in a draft is published as the next version of `<name>`, with the
  resolved job config as `metadata["config"]` and its overrides as `metadata["overrides"]`, and the
  folder is removed; a failed one keeps its draft (`cairn drafts`) and publishes nothing.
- With a plain run dir (`runs/${run.name}/${now:%Y-%m-%d_%H-%M-%S}`), a completed single run becomes
  `runs/<name>/latest`, a relative symlink; a failed run stays for diagnosis and never moves it.
- `StudyCompletion` writes a multirun study's `run.json` after the last trial
  (completed only when every trial completed). A sweep draft is then published once, with every
  trial folder and the inputs of all of them, the base config and the sweep's overrides as
  metadata; a study with a failed trial keeps its draft. A plain sweep dir moves `latest` to the
  study instead. Trials never publish and never touch `latest`.
- `SourceSnapshotCallback` stores the project's source files (size-bounded,
  secrets and data files excluded) in each run's `provenance/`: in `git` mode
  (default) the commit, the diff, and untracked sources; in `tree` mode a full
  copy. It also records the runtime environment.
- `cairn finalize-study <study folder>` completes a study (a sweep draft too)
  whose launcher died, by the same rule, once every trial job has ended.

## Store

The store holds **artifacts**: named, versioned folders inside a **project**. A name is a
slash path (`descent`, `datasets/crops`); reports are the artifacts `reports/<slug>`. A
version is immutable once completed (until it is deleted: see below) and records its files (by content hash), metadata,
the exact versions it consumed (`inputs`), and where it was published from. Aliases
(`latest` moves by itself; `best`, `paper` by hand) and labels select versions:
`[project:]name[@v3|@uid|@alias|@label][//file]`.

Every version also has a **uid**, a UUIDv7 made where the version is created (by the store, or by
the client's replica, and kept by the server): `datasets/crops@0192f3a4-5b6c-7d8e-9f01-23456789abcd`
names that version as exactly as `@v3`. A selector in the canonical lowercase UUIDv7 form is always a
uid, so no alias or label may take that form (nor `v<n>`). `v<n>` stays the readable name; the uid is
what the replica (its data and cache roots) and offline pulls key on. Every store has a uid too (`store.json` at
its root). A store made before uids existed is upgraded as it is read: each version gets a uid
derived from the store and the version (the same in every process), and the root gets its
`store.json`.

A store folder holds a SQLite database with the metadata (`metadata.sqlite3`: projects, versions,
aliases and every alias move, who consumed which version) and the file contents, named by their
SHA-256 (`blobs/`). Every change is one transaction, so a publish is stored whole or not at all,
even if the process is killed halfway. A folder written by an earlier cairn (JSON Lines files) is
imported the first time it is opened; the old files stay beside it as `*.migrated`.
`cairn export-metadata <folder> <dir>` writes the metadata as readable JSON Lines, for backups and
inspection.

A project works offline against a store folder (`.cairn/store` by default, `[store] path`). To share
it, serve a folder over HTTP and point projects at the server. The two halves are separate:

- the **store server** (`cairn server`) is only an artifact store: a JSON API and file
  downloads, bearer tokens, no pages;
- the **UI** (`cairn ui`) is a separate process that explores a store in a browser, either a
  folder or a store server, so it can run on a laptop against a store on a cluster.

```bash
cairn server /srv/store      # store on 127.0.0.1:8800, UI on 127.0.0.1:8801; HTTPS reverse proxy in front
cairn server /srv/store --no-ui [--port 8800] [--max-blob-gb 50]     # the store alone
```

The first `cairn server` run on a new folder creates it with a random owner token (owner-only, in
`<folder>/token`); every launch prints `Store: <url>`, `UI: <url>` and `Token: <owner token>` to
the terminal (never into a URL). Binding to a non-local `--host` warns: the server speaks plain
HTTP. By default `cairn server` also starts `cairn ui` as a child process (own port: `--ui-port`,
default `--port` + 1; same `--host`) pointed at this store. The child gets the owner token only in
its environment, never on its command line, and stops with the server; if the UI dies, the store
keeps running and prints a warning. `--no-ui` starts the store alone.

For local testing, `cairn server <folder> --no-auth` needs no token: every request acts as the owner,
clients connect without `CAIRN_STORE_TOKEN`, and the UI child runs `--no-auth` too. It is refused on
any `--host` but loopback.

One server per store folder, and it is the folder's only writer: while it runs, other processes may
read the folder but publish through the server's URL (`cairn token` commands on the same machine
still work). Keep the folder on a local disk of the server's machine. `cairn server` refuses a folder
on NFS, Lustre, GPFS, SMB, BeeGFS, Ceph and other network filesystems, where SQLite's and the
server's locks are not reliable; `--allow-network-fs` overrides that when nothing else ever touches
the folder. A local store folder (a project's `.cairn/store`) on a network filesystem warns once when it
is written. Cluster jobs talk to a store server over HTTP; their pull cache may live on NFS.

The server is built for many clients at once: it is asynchronous (uvicorn's event loop, with disk
work in worker threads), uploads are hashed and written while they arrive, downloads stream from the
blob files (with `sendfile` on Linux), the project and artifact listings come from summary rows kept
current in the database (and are cached until the project changes), completed versions' files and
manifests are sent as `immutable`, and JSON answers carry an ETag (`If-None-Match` gets 304). A
local publish clones files into the store where the filesystem can (APFS `clonefile`, Btrfs/XFS
`FICLONE`), so the store's copy takes no extra space until either side changes.
`bench/bench_store.py` measures it (results in `bench/RESULTS.md`):

```bash
taskpolicy -b python bench/bench_store.py --json out.json     # macOS: background priority
nice python bench/bench_store.py --size-mb 1024 --clients 32   # elsewhere; see --help
```

```bash
cairn token create /srv/store --project my-study --write --name cluster   # printed once
cairn token list /srv/store       # names, projects, access: never token values
cairn token revoke /srv/store cluster
```

The owner token can do everything. A scoped token reads one project, and publishes to it only
with `--write`; everything else is 403 and a scoped token only sees its own project. Scoped
tokens are stored hashed (`tokens.jsonl`); revoking one takes effect immediately, also for
UI sessions.

In a project:

```toml
[store]
url = "https://store.example.org"                 # omit for a local store
project = "my-study"                              # default: the folder name
token_file = "~/.config/cairn/store-token" # chmod 600; or CAIRN_STORE_TOKEN
allow_http = false                                # plain HTTP to a non-local server (default: refused)
exclude = [".submitit/**/*.pkl"]                  # never published: gitignore-style globs on project paths
interval = 30                                     # seconds between syncs with --watch
heartbeat_timeout = 600                           # seconds without a heartbeat before a running version is failed
```

```bash
cairn publish data/crops datasets/crops --alias paper      # a folder becomes the next version (none if unchanged)
cairn publish runs/x/latest x --allow-incomplete           # publish although run.json is not completed
cairn publish runs/x/latest x --watch -- python run.py     # sync a running job; complete (or fail) with it
cairn publish reports/alpha                                # build a report and publish reports/alpha
cairn pull datasets/crops@paper                            # print the local, read-only copy's path
cairn pull x@latest --into DIR [--no-record]               # copy it into DIR instead
```

```bash
cairn project list                                         # projects, artifact counts, last update
cairn project create my-study --title "My study" [--description ...]
cairn project show my-study                                # its title, description, and artifacts
cairn project edit my-study --description "..."
cairn artifacts [--prefix reports/] [--project other]      # latest version, version count, aliases
cairn versions datasets/crops                              # number, status, label, created, uid, size
cairn show datasets/crops@paper                            # metadata, files, inputs, consumers
cairn status [--all]                                       # this machine's versions the server lacks (--all: every one)
cairn sync                                                 # send them now, and refresh the known aliases
cairn pull datasets/crops@paper --offline                  # never ask the server (see below)
cairn prune [--older-than 30d] [--max-size 20G]            # trim the cache by last pull
cairn verify [REF | --cache | --store [ROOT] | --all] [--repair]   # re-hash what is stored (see Integrity)
cairn alias datasets/crops@v3 best                       # point an alias at a version (latest moves by itself)
cairn delete datasets/crops@v2 --reason "..."             # a tombstone; see Deleting and garbage collection
cairn gc --dry-run                                         # retention rules and unreferenced blobs
```

All of them work the same against a store folder and a store server.

**Lineage.** `pull` appends the exact version it returned to `.cairn-inputs.jsonl` in the working
directory (or the file `$CAIRN_INPUTS` names). Publishing a folder merges every `.cairn-inputs.jsonl`
found anywhere inside it, one per Hydra multirun trial for example, into the new version's `inputs`
(deduplicated); none of them becomes a file of the version.

**Incomplete runs.** If the published folder (or the folder `latest` resolves to) has a `run.json`
whose `status` is not `completed`, `cairn publish` refuses: "run.json says running/failed; publish
completed runs, or pass --allow-incomplete". `--allow-incomplete` (`allow_incomplete=True` in
`publish()` and `Artifact.publish()`) publishes anyway and records `metadata.run_status`. `--watch`
is exempt: it publishes a running job by design.

### The Python API

`cairn.connect()` opens the store `[store]` names (or `cairn.connect("https://...", token=...)`, or
`cairn.connect("path/to/store")`; `project=` sets the default project). Objects are thin and lazy:
they fetch on first access and keep what they fetched.

```python
import cairn

store = cairn.connect()
project = store.project()                       # the configured project; store.project("other"), store.projects()
project.update(description="Denoising study")   # store.create_project("new", title="New")
crops = project.artifact("datasets/crops")      # project.artifacts(prefix="datasets/")

v3 = crops.version(3)                           # or "v3", an alias, a label, or a uid; crops.latest, crops.versions()
v3.ref, v3.uid, v3.label, v3.status, v3.created, v3.metadata, v3.inputs, v3.consumers
[(f.path, f.size, f.sha256) for f in v3.files]
with v3.open("tables/cases.csv") as handle:     # streams one file without pulling the version; recorded as an input
    header = handle.readline()
folder = v3.pull()                              # <cache>/versions/<uid>/files/, read-only; recorded as an input

fit = project.artifact("models/fit")
v = fit.publish("runs/fit", metadata={"loss": 0.1}, aliases=["best"], inputs=[v3.ref])   # -> Version
fit.alias("paper", v)
with fit.publishing("runs/live") as running:    # a running version that follows a folder while a job writes it
    ...
    running.sync()
with fit.draft(aliases=["best"]) as run:        # a fresh folder that becomes the next version (see Drafts)
    run.scalar("loss", 0.1)
run.version
```

The functions `cairn.pull`, `cairn.publish`, `cairn.alias`, `cairn.versions`, `cairn.lineage`, and
`cairn.Publication` stay, as shortcuts on top of these objects (and `cairn.draft`, `cairn.current_draft`) (`cairn.publish` returns the
version number, or None while the version is pending).

### Deleting and garbage collection

The store is an artifact store, not a tracker: versions can be deleted, and their files go once
nothing names them any more.

```bash
cairn delete datasets/crops@v3 --reason "wrong crop size"   # one version (also @uid, @alias, @label)
cairn delete --artifact scratch/tmp                          # every version of an artifact (all or none)
cairn project delete old-study [--force]                     # refused while it has versions, unless --force
cairn delete datasets/crops@v1 --force                       # a protected version too (the owner token on a server)
```

A deleted version becomes a **tombstone**: it stays in the version list (status `deleted`, with
`deleted_at`, `deleted_by`, and the reason; the UI shows it greyed with a `deleted` pill), keeps its
label, metadata, and inputs, and loses its files. Its number and uid are never used again: the next
publish gets the next number, and a replica that sends the same uid again is refused. Any reference to
it (`@v3`, `@<uid>`, as an input, a file URL) fails with "was deleted" (410 on the server). Lineage
keeps its edges, and marks the node deleted. `latest` moves back to the newest completed version left.

Some versions are **protected**, and a deletion that touches one is refused (409) unless forced:

- an alias other than `latest` points at it (forcing removes the alias);
- it is an input of a version that is not deleted, in any project: a report's pinned input, a model's
  dataset (forcing keeps the consumer's input edge, marked deleted);
- it is running.

**Retention rules** say what `cairn gc` deletes by itself, per project or per artifact-name prefix
(the longest matching prefix wins). A version is kept if it is among the `keep_last` newest of its
artifact, or younger than `keep_days`, or the artifact's `latest`; aliased versions are kept and take
no `keep_last` place (`--no-keep-aliased`: they take a place, and are still never deleted). Rules never
delete protected versions: those are skipped and reported.

```bash
cairn retention set my-study --prefix runs/ --keep-last 5 --keep-days 30
cairn retention set my-study --keep-last 20          # every other artifact
cairn retention show my-study
cairn retention clear my-study --prefix runs/
cairn gc --dry-run                                   # the configured store: what would be deleted and freed
cairn gc [--grace 24h]                               # apply the rules, then sweep the blobs
cairn gc /srv/store --dry-run                        # a store root on this machine (a served one: only --dry-run)
cairn server /srv/store --gc-interval 24h [--grace 24h]   # also collect garbage on a schedule (default: never)
```

`cairn gc` first applies the retention rules, then sweeps the blobs: a blob that no version (other
than deleted ones) names in any project, and whose file is older than the **grace period** (24 h by
default), is removed; the output says how many bytes were freed. The grace period protects uploads
in progress: a client uploads the blobs before it commits the version that names them. The sweep runs
in the store's single writer (the server, or the process that owns a store folder) and re-checks
every candidate inside the same transaction that removes it, while a publish checks its blobs inside
its own transaction: a publish racing a sweep either finds its blobs kept, or is answered "upload these
blobs first" (409 with `missing`), and the client uploads them and sends the version again, so it ends
complete and downloadable. Stale temporary uploads (untouched for the grace period, at least an hour)
go too. A dry run changes nothing. On a store server, deleting needs a write token for the project,
while `--force` and `gc` need the owner token.

The same in Python: `version.delete(reason=..., force=False)`, `artifact.delete()`, `project.delete()`,
`project.retention = [{"prefix": "runs/", "keep_last": 5}]` (or `project.set_retention("runs/",
keep_last=5)`), and `store.gc(dry_run=True, grace="24h")`, which returns what was (or would be)
deleted, the protected versions it skipped, and the blobs and bytes freed. The API routes are
`DELETE /api/v2/projects/<p>/artifacts/<name>/versions/<n>` (`?force=1&reason=...`),
`DELETE .../artifacts/<name>`, `DELETE /api/v2/projects/<p>`, `GET`/`PUT /api/v2/projects/<p>/retention`,
and `POST /api/v2/gc {dry_run, grace}`.

A copy of a deleted version that this machine already pulled stays in its cache until `cairn prune`;
after `cairn delete` here, exact references to it fail on this machine too, but other machines'
caches still answer exact references to it offline.

### The local replica

Every version this machine knows is one record in the **replica**, `versions/<uid>/manifest.json`,
with a sync state (`synced`, `pending`, `rejected`, or `running` for a `--watch` version) and its
files, read-only, under `versions/<uid>/files/`: that folder is what `pull` returns. The replica has
two roots:

```text
<data>/versions/<uid>/manifest.json, files/   versions the server does not have yet: pending, running, rejected
<data>/versions/<uid>/heartbeat               a running version's owner: time, host, pid (rewritten while it runs)
<data>/blobs/<aa>/<sha256>                    the contents they need
<data>/stores/<store-uid>.json                each server: URL, last contact, last known aliases and labels per artifact
<cache>/versions/<uid>/manifest.json, files/   synced versions, with their last pull time
<cache>/blobs/<aa>/<sha256>                    downloaded contents (a file is downloaded once)
```

`<data>` (the data root) is durable: `--data-dir`, else `$CAIRN_DATA_DIR`, else `.cairn/pending`
in the project (`~/.local/share/cairn` outside a project; see "Where Cairn keeps things"). It holds what only this machine has: pending versions are real data, not
cache, so the root belongs on persistent storage with room for them (`CAIRN_STATE_DIR`, its old name,
is still read for one release, with a warning; pending versions in the old `~/.local/state/cairn` or
`$XDG_STATE_HOME/cairn` are moved into the data root on first use). It is the counterpart of
W&B's offline runs: the work is kept locally until `cairn sync` sends it. `<cache>` is throwaway:
`$CAIRN_CACHE_DIR`, else `.cairn/cache` in the project (`~/.cache/cairn` outside one); deleting it loses nothing
the server does not have, and `cairn prune` trims it by last pull (recorded in the manifest, not the
filesystem's atime), never touching pending or running versions or the blobs they need.

Publishing to a store server first commits the version to the data root: the record (manifest,
label, metadata, inputs, aliases) and the contents it needs, cloned (copy-on-write where the
filesystem can) or copied, so the upload never reads the source folder again: a file changed after
`publish` returns does not change the pending version. Then it uploads. If the server does not
answer, `publish` returns a **pending** version (its uid is set, its number is None until the server
assigns it) that is usable at once: `cairn.pull`, `Version.open`, and a report's `inputs:` resolve it
by uid, by an alias it was published with, by its label, or as `latest` (when it is newer than what
the server has), with a warning that it is pending. Its uid-exact reference `project:name@<uid>` is
what lineage records until the server numbers it; pending versions are sent in uid order, so an
input goes before its consumers, and the server turns that input into `@v<n>`.

Pending versions are sent by a background thread (in long-lived processes, once something is pending
or `connect()` finds something pending), by `cairn sync`, and at the start of every `cairn` command
that uses the store. Sending is idempotent: every request carries the version's uid, and the server
answers a uid it already has with that version. A sent version is promoted to the cache under the same
uid (its folder is renamed when both roots share a filesystem; otherwise the data-root copy is dropped
and the cache downloads it on its next pull). A version the server refuses stays as `rejected`
(shown by `cairn status`, skipped by every flush; delete its folder to drop it). `cairn sync` also
refreshes what this machine knows about the aliases of every artifact it has versions of.
`cairn status` lists the versions the server does not have yet, with their host, age, state, and last
error; `cairn status --all` lists every version in the replica.

`cairn publish --watch -- cmd` always starts the command: the running version is created in the
data root and, as soon as the server answers, on the server under the same uid; its files follow
as they are synced, and completion or failure turns it pending like any publish. While it runs, its
owner rewrites `heartbeat` every few seconds. A job that dies leaves its version running only until
a flush notices: on the same host a dead pid fails it at once, and a flush on any host fails it once
its heartbeat is older than `[store] heartbeat_timeout` (default 600 seconds).

Several hosts may share one data root (a folder on NFS). Nothing locks: every record is written
under a scratch name and renamed into place, and a flusher claims a version by exclusively creating
`versions/<uid>/.flushing-<host>-<pid>` with a timestamp (claims older than the timeout, or of a dead
pid on this host, are ignored). The claim only avoids double work; sending twice would be harmless.

A local store (`[store] path`) is written directly, with no replica.

Pulls work offline too. An exact reference (`name@vN` this machine has seen, or `name@<uid>`)
whose version is in the cache comes from the cache without asking the server at all. When the
server does not answer, an alias, label, or `latest` falls back to its last known resolution (kept in
`<data>/stores/<store-uid>.json`) with a warning naming the version, and that exact version is
recorded as the input. `cairn pull --offline` and `cairn.pull(..., offline=True)` force the
fallback; a report build resolves its `inputs:` the same way.

Versions are keyed by uid, never by project, name, and number, so two stores never collide in one
replica. Either root may be shared by many jobs, also on NFS: a version folder or a blob is written
under a scratch name (`<target>.tmp-<host>-<pid>-<random>`) and renamed into place, and whoever
loses a race discards its copy.

### Integrity

**Manifest digest.** Every completed version has a `digest`: `sha256:` plus the hex SHA-256 of its
canonical file list, and nothing else. The list has one object per entry, `{"path", "sha256", "size"}`
for a file or `{"link", "path"}` for a symlink, sorted by `path` (code point order, which equals UTF-8
byte order), each object with its keys sorted. It is serialized as compact JSON (no whitespace, `,` and
`:` only, non-ASCII written as UTF-8 rather than `\uXXXX`) and hashed as UTF-8 bytes. Metadata, label,
inputs, source, timestamps, and `mtime` are not part of it, so two versions with the same files have the
same digest. In Python:

```python
json.dumps(sorted(items, key=lambda item: item["path"]), sort_keys=True,
           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

`cairn.store.digest` documents the form and implements it. The store computes the digest when a version
completes and keeps it in the manifest (stores written before digests existed get theirs the first time
they are opened); a client computes it for a version it is about to upload, so a pending version knows it
before the server does, and the server recomputes it and answers 409 if a digest the client sent does not
match the files. `Version.digest`, `cairn show`, and the UI's artifact page (copyable) show it.

**Verified pulls.** Materializing a version hashes every file as it is placed and compares it with the
manifest (hash and size), then compares the digest of what was placed with the version's digest. On any
mismatch the pull fails with an error naming the file, and the half-built folder is removed; a damaged
blob in the cache is not silently reused (`cairn verify --cache --repair` drops it so the next pull
downloads it again).

**Copy-on-write pulls.** Pulled files are copy-on-write clones of the blobs where the filesystem has them
(APFS `clonefile`, Btrfs/XFS `FICLONE`): they cost no space, are files of their own, and editing one
(after a `chmod`) cannot touch the blob. Where cloning is unsupported, files are hard-linked and always
read-only (`0444`): a hard link shares the blob's inode, so writing through a writable one would corrupt
the blob and every version that shares it. If hard links fail too (another filesystem), they are copied.

**`cairn verify`** re-hashes and exits 1 on any problem it cannot fix:

```bash
cairn verify datasets/crops@v3     # one version: its digest, its cached or pending copy, the blobs held for it
cairn verify --cache               # every cached version against its manifest and digest, every cache blob
                                   #   against its name, and this machine's pending versions (the default)
cairn verify --store [ROOT]        # a local store: each blob against its name, every blob a version references
                                   #   exists, every completed version's digest matches its files
cairn verify --all                 # the cache, pending versions, and the configured local store
cairn verify --cache --repair      # remove broken cached versions and blobs; the next pull downloads them
```

`--repair` touches the cache only: pending versions and stores may hold the only copy, so they are
reported, never changed. The object API returns the same as a report: `cairn.connect().verify(ref=None,
cache=False, store=False, all=False, repair=False)` gives a `Report` with `.ok`, `.problems` (kind, path,
message), and `.summary()`.

### Clusters (Slurm)

The safe setup:

- run `cairn server` on a machine with a local disk (not on a compute node, and with its root not
  on a shared filesystem many writers touch), behind HTTPS;
- jobs publish over HTTP (`[store] url`), never into a store folder on NFS;
- the project lives on persistent storage, so the default `.cairn/` folder is already right:
  pending versions in `.cairn/pending` stay there until the server has them, also after the job and
  its node are gone, and every node and the login node see the same folder (set `CAIRN_DIR` to
  put the whole folder elsewhere on persistent storage, e.g. when the project is not shared);
- `CAIRN_CACHE_DIR` points at node-local scratch, so pulls read from a local disk and the cache
  disappears with the job (the default `.cairn/cache` sits on the project filesystem);
- every job script ends with `cairn sync` (it sends what the job left pending; a job killed before
  that is sent by the next sync anywhere, and a killed `--watch` version is failed once its heartbeat
  expires), and a login-node timer can run `cairn sync` too, from the project (or with the same
  `CAIRN_DIR`, `CAIRN_DATA_DIR`, or `cairn sync --data-dir PATH`);
- `cairn status` shows what is still pending, from which host, and why.

```bash
#!/bin/bash
#SBATCH ...
export CAIRN_CACHE_DIR="${TMPDIR:-/tmp}/cairn-$SLURM_JOB_ID"   # node-local, throwaway
cd /work/my-group/me/my-study                                  # the project: .cairn/pending waits here
cairn publish runs/fit/latest fit --watch -- python fit.py
cairn sync                                                    # send anything still pending
```

```bash
# on a login node, e.g. from cron or a systemd user timer
*/15 * * * * cd /work/my-group/me/my-study && cairn sync >/dev/null 2>&1
```

`exclude` takes gitignore-style globs on project paths: `*` and `?` stay within a folder, `**`
spans folders, a leading `/` anchors to the project root.

The server's API lives under `/api/v2/` (`GET /api/v2/store` answers its uid and version) and its
files under `/files/<project>/<name>@vN//<path>` (or `@<uid>`)
(ETag, Range, `Content-Security-Policy: sandbox`); the routes are listed in
`cairn/server/__init__.py`. Besides publishing and resolving it answers what a viewer needs:
projects with their artifact count and last update, an artifact's summary with its latest version and
aliases, a version's file list, and lineage. Tokens are bearer tokens only: the server has no
cookies, no login page and no HTML. It never runs pushed code.

### Exploring a store: `cairn ui`

```bash
cairn ui                                   # the store in cairn.toml ([store] url or path), on 127.0.0.1:8801
cairn ui --store https://store.example.org # a remote store, e.g. from a laptop against a cluster-side store
cairn ui --store /srv/store                # a store folder
```

The UI opens the store only through its interface (a store server's API, or a folder's store
code), so it needs no access to the store's files and does no work in the store server. It prints
`UI: <url> (store: <url or folder>)` and serves a React app (built files in the package, no Node
needed) in the look of the reports (their stylesheet, tokens and class names; dark mode follows the
system):

- `/` projects (title, description, artifacts, last update);
- `/p/<project>/` the project: a search box (names, titles, status, aliases), then the tabs
  "Reports" (title, status, summary, latest version, an Open report link) and "Artifacts" (latest,
  aliases, versions, updated), sorted by update;
- `/p/<project>/a/<name>[@vN|@alias]` an artifact: its versions (status, label, aliases, source
  host) with a version picker, and for the chosen version a copyable reference and `cairn pull`
  command, the files (download, inline preview of images, video, text and JSON, a file
  reference to copy), the metadata, the inputs and the consumers, and the lineage graph (inputs on
  the left, the version in the middle, consumers on the right; click a node to open it, `+` to
  expand one more level). A report artifact has an Open report button;
- `/p/<project>/r/<slug>[@vN]/` a report version's built page, as before (a report is an artifact
  `reports/<slug>` with `site/` and `source/`); its Data panel links each input to the artifact page
  of the exact version, and its brand link returns to the project in the app;
- `/files/<project>/<name>@vN//<path>` the store's files, with Range (videos seek).

The app talks to the UI process through `/ui-api/` (GET unless noted), a session-scoped passthrough
to the store interface: `me` (scope: `owner`, `project`, or `store` when a remote store decides),
`projects`, `projects/<p>`, `projects/<p>/artifacts/<name>` (summary, versions, aliases),
`projects/<p>/artifacts/<name>/versions/<n>` (manifest with files, consumers; `<n>` may also be an
alias or label), `lineage?ref=<project:name@sel>[&depth=1..3]` (inputs and consumers, assembled from
one-level lookups, capped at 200 nodes), and `POST login`. Every call needs the session and the
header `X-Cairn: 1` (a cross-site request cannot send it); a name may be one URL-encoded segment.

**Login.** By default the UI asks for a store token on `/login` (the owner token or a scoped one) and
checks it against the store: a store server is asked with it, a store folder's owner token file and
`tokens.jsonl` are checked. The app's login page posts the token to `/ui-api/login`; it stays in the UI process, in an in-memory session table; the
browser gets only a random session id (HttpOnly, SameSite=Strict cookie, `Secure` unless
`--insecure-cookies`, for plain-HTTP tunnels), and the token is never in a URL or a page. Each
session acts with its own token, so a scoped token sees only its project, and a revoked token ends
its session. Sessions last 30 days and are forgotten when the UI restarts.

`--no-auth` (loopback `--host` only) has no login: the UI uses `CAIRN_STORE_TOKEN` or
`[store] token_file` against a store server (none against a `--no-auth` store), or the owner's full
access to a folder. `--allow-http` permits plain HTTP to a non-local store server, as for `publish`.
A report page runs in the UI's origin: give write tokens to people whose reports you trust.

#### Developing the UI

The app's source is `web/` (React 18, TypeScript, Vite; it imports
`src/cairn/reports/template/report.css` and `report-extra.css` so the look has one source, plus a small
`web/src/app.css`). The built files in `src/cairn/ui/static/` are committed, so `pip install` needs
no Node.

```bash
cairn ui --store /srv/store --insecure-cookies    # a UI to talk to, on 127.0.0.1:8801
cd web && npm ci && npm run dev                   # http://localhost:5173, proxying /ui-api, /files and reports to it
npm run build                                     # type-checks, then writes src/cairn/ui/static/ (commit it)
```

`CAIRN_UI_URL` points the dev proxy at another UI address. `tests/browser_check.py` walks the UI
in headless Chrome and writes screenshots (`taskpolicy -b uv run --with playwright python
tests/browser_check.py`).

### Migrating from the hub

Before 0.13 the store was the "hub" (`[hub]`, `hub init`, `push`, MLflow, `run:` references),
which is gone without aliases. Convert an old hub folder once:

```bash
cairn migrate /srv/hub /srv/store      # the old folder is only read; blobs are hard-linked
```

Each old project becomes a project. Each `<run>/<timestamp>` becomes a version of the artifact
`<run>` (label: the timestamp; `failed` unless its `run.json` says `completed`). Only the latest
old report version is migrated, split into one `reports/<slug>` artifact per report folder
(`a/b` becomes `a-b`; a top-level report becomes `overview`), stored as it was and not rebuilt. Reports that
read runs with `run:` must be rewritten by hand to declare `inputs:`. Running it again adds
nothing. `cairn server /srv/store` then gives the new folder an owner token if it has none.

## Where Cairn keeps things

Everything Cairn writes for a project goes into one folder, `.cairn/` in the project root (like
W&B's `./wandb/`). It creates a `.gitignore` containing `*` inside, so git ignores it; your own
`.gitignore` is never edited.

```text
<project>/.cairn/
├── .gitignore      "*"
├── store/          the local store, when [store] has no url
├── pending/        the data root: versions/<uid>/, blobs/, stores/ (pending, running, rejected versions)
├── cache/          the cache root: versions/<uid>/, blobs/, hash caches (throwaway)
├── build/          built report pages
├── drafts/         open and kept drafts: <name>/<uid>/ and <name>/<uid>.draft.json (`cairn drafts`)
└── share/          `cairn share` pages (next to build/)
```

Each place is resolved separately; the first match wins:

| What | Resolution |
| --- | --- |
| base folder | `$CAIRN_DIR`, else `[cairn] dir` in `cairn.toml` (relative to the project root), else `<project root>/.cairn`; none outside a project |
| pending (data root) | `cairn sync --data-dir`, else `$CAIRN_DATA_DIR` (`$CAIRN_STATE_DIR`, deprecated), else `<base>/pending`, else `$XDG_DATA_HOME/cairn` (`~/.local/share/cairn`) |
| cache root | `$CAIRN_CACHE_DIR`, else `<base>/cache`, else `$XDG_CACHE_HOME/cairn` (`~/.cache/cairn`) |
| local store | `[store] path`, else `<base>/store` |
| built pages | `output_dir` in `cairn.toml`, else `<base>/build` |
| drafts | `<base>/drafts`, else `<data root>/drafts` outside a project |

The project root is the nearest folder above the working directory with a `cairn.toml`,
`pixi.toml`, or `pyproject.toml`. Source snapshots of Hydra runs skip `.cairn`.

Earlier layouts move on first use (once, announced on stderr, never overwriting anything): a
project's `.store/` becomes `.cairn/store/`; pending versions in the global `~/.local/share/cairn`
whose source folder lies inside the project move to `.cairn/pending/` (running versions and other
projects' versions stay there, and `cairn sync` and `cairn status` still cover the global root).
`generated/reports/` is left alone: the next build writes to `.cairn/build/`, and the old folder can
be deleted. `outputs_dir` in `cairn.toml` is no longer used (one warning, then ignored).

On a cluster the project sits on persistent storage, so the default is the right one: pending
versions survive the job. Set `CAIRN_CACHE_DIR` to node-local scratch for fast pulls, and end every
job with `cairn sync` (see "Clusters (Slurm)").

## Configuration

Everything has a default; `cairn.toml` at the project root changes it:

```toml
reports_dir = "reports"            # holds one folder per report
output_dir = ".cairn/build"        # the built site (default: <cairn dir>/build)

[cairn]
dir = ".cairn"                     # where Cairn keeps its files for this project (default)

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
