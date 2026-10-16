# Cairn

Cairn is an artifact store for research projects that keep their evidence in plain folders. It
has five parts:

- the **repo**: the project's `.cairn/` folder, which holds named, versioned folders (datasets, run
  outputs, reports) with aliases, metadata, and lineage. Optionally it has a **remote**, a `cairn
  server` that many machines, jobs, and clusters share; the repo then syncs with it by itself;
- the **UI** (`cairn ui`), which explores the repo (or a remote) in a browser;
- **single-page reports**: Markdown pages that read numbers and figures from the artifacts they
  declare as `inputs:`, built into a static site and published as artifacts themselves;
- **runs and drafts**: every process that uses or publishes artifacts is a **run** (`cairn.init()`), a
  version of `runs/<name>` that records its config, inputs, metrics, output, code, packages, and machine;
  what it publishes goes into **drafts** (`with cairn.draft("models/student") as model:`), fresh folders
  Cairn manages that become the artifact's next version, with the run as their input;
- **Hydra helpers**: `hydra.run.dir: ${cairn.run:}` (the job folder is the run's), one run per sweep
  trial plus a summary run, a source snapshot.

Cairn provides mechanisms only. How reports are organised, which sections they have, and what
counts as evidence is up to the project.

## Quick start

```bash
pip install git+https://github.com/DoeringChristian/cairn.git   # see Install for extras
cd my-project                       # the project folder (its repo is .cairn/ here)
cairn init                          # optional: the repo, .cairn/ (any command that writes makes it too)
cairn publish data/crops datasets/crops   # a folder becomes version v1 of "datasets/crops" in the repo, .cairn/
cairn pull datasets/crops@latest    # prints the path of a read-only local copy
cairn ui                            # explore projects, versions, files and lineage in the browser
cairn server                        # or: serve the repo to other machines (the UI starts beside it)
```

Nothing needs to run: the repo is a folder. `cairn ui` creates it if needed, prints a one-time login
link and opens it in the browser. `cairn server` serves the same folder to other machines, whose
projects name it as their remote (`[repo] remote`); the project's own scripts and commands keep
opening the folder directly, whether the server runs or not. In Python a job is a run that uses some
versions and publishes others (see "Runs: the Python API"):

```python
import cairn

run = cairn.init(name="fit", config={"lr": 0.1})   # optional: the first cairn.use() or cairn.draft() starts it
data = cairn.use("datasets/crops@latest")           # an input of the run, at its exact version
run.add({"loss": 0.3, "step": 0})                   # the run's metrics.jsonl
with cairn.draft("fit") as fit:                     # an output of the run: fit@v<n>
    fit.add({"rmse": 0.5})
    fit.add_figure("errors", fig)
```

A report is a folder `reports/<slug>/report.md` that pins what it reads:

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
[`examples/descent/`](examples/descent/README.md), run with four commands from its folder.

## How it works: the repo, a remote, sync

The design is fixed in [`docs/architecture.md`](docs/architecture.md); in short:

- **A repo is a folder on a local disk: the project's `.cairn/`.** It holds the metadata (SQLite), the
  content-addressed blobs, and Cairn's working folders. Every process on the repo's machine opens it
  directly: scripts, CLI commands, `cairn ui`, and `cairn server`, which is just another such process.
  Any number of processes may read and write at once; each write is one SQLite transaction, so a version
  is stored whole or not at all, and numbers are contiguous however many processes publish.
- **A project has one repo and at most one remote** (`[repo] remote = "https://..."`, a `cairn server`).
  Every write goes to the repo first, and the repo sends it to the remote by itself. There is no push or
  pull command to remember and no switching between modes: the same code runs with and without a remote,
  online and offline.
- **One source of truth for numbers, aliases, and deletions:** the remote if there is one, else the repo.
  A version's **uid** (a UUIDv7) is its identity everywhere; `vN` is the source of truth's name for it.
  A version made while the remote does not answer is **pending**: it has its uid and no number, is
  usable at once by its uid (and as `@latest`), waits in the repo's **outbox**, and gets the remote's
  number when the remote takes it. Versions used from the remote are copied into the repo, so the repo
  answers when the remote does not.
- **Sync is automatic:** at the start and end of every `cairn` command, in the background of a process
  that left something pending (and once more as it exits), while a run runs (its live version, every
  `[repo] interval` seconds), and with `cairn sync` (`--watch` keeps going, for a cluster's login node).
  Each attempt fails quickly when the remote does not answer; nothing blocks a run. Sending is
  idempotent by uid: an interrupted or repeated sync never makes a second version. `cairn status` lists
  what waits: pending, running, and rejected versions.
- **On a network filesystem there is no repo database.** On NFS, Lustre, GPFS, SMB and the like (a
  cluster's shared home or project space), SQLite and file locks are unreliable across nodes. There a
  project needs a remote: each version goes through its own outbox folder (one writer per folder), which
  the job itself, or `cairn sync` on the login node, sends. Without a remote, Cairn refuses and explains
  the cluster setup; `[repo] allow_network_fs = true` is the owner's word that one process on one host is
  the repo's only writer.

```text
 laptop                         server machine                       cluster (shared filesystem)
 project/.cairn   ── sync ──▶   cairn server: the remote, a   ◀── sync ──  /work/me/study/.cairn
 database, blobs, ◀── copies ── repo on its local disk         ── copies ─▶ outbox + blobs, no database;
 outbox                         (+ tokens, + its UI)                     jobs write the outbox, the
                                                                         login node sends it
                                                                         (cairn sync --watch)
```

A repo that numbered versions itself (it had no remote) and then gets one sends them at its first sync,
oldest first; the remote numbers them, and the repo takes the numbers over (uids, lineage, and the
reports that recorded them stay valid).

### New in 0.14: the repo layout

0.14 introduces this design. Cairn was not in use before it, so there is no migration and no
compatibility code: a project starts a fresh repo in `.cairn/`. `[store]` in `cairn.toml` is an unknown
key (use `[repo]`), the environment variable `CAIRN_STORE_TOKEN` is now `CAIRN_TOKEN`, and the old
`.cairn/store`, `.cairn/pending`, and `.cairn/cache` folders are not read. The hub importer
(`cairn migrate`) is gone. For the names inherited from cairn-report: the distribution and the command
are `cairn` (also `python -m cairn`), the configuration file is `cairn.toml`, and `cairn server` /
`cairn token` replace `cairn-report store serve` / `store token`. The old Cairn tracker (cairn-track) cannot be installed in the same environment.

## Install

In a pixi project:

```toml
[dependencies]
graphviz = "*"   # only needed for Graphviz diagram fences

[pypi-dependencies]
cairn = { git = "https://github.com/DoeringChristian/cairn.git", tag = "v0.14.0" }

[tasks]
report-build = "cairn build"
report-serve = "cairn serve --host 127.0.0.1 --port 8000"
```

With uv or pip: `uv add git+https://github.com/DoeringChristian/cairn.git`.
The base install is light (`import cairn` loads neither the server, the UI, matplotlib, plotly, nor
hydra). Extras: `cairn[server]` (`cairn server`: Starlette and uvicorn with uvloop and httptools),
`cairn[video]` (`log.video` from frames), `cairn[hydra]` (run bookkeeping for Hydra projects), and
`cairn[ui]`, which adds nothing yet (the UI uses the standard library, and its built files ship with
the package). On the machine that serves a repo (the remote):

```bash
pip install "cairn[server] @ git+https://github.com/DoeringChristian/cairn.git"
# or: uv add "cairn[server] @ git+https://github.com/DoeringChristian/cairn.git"
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
output is a static site: any web server, the project server, or `cairn ui` can
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
for a version of this repo that is still pending; aliases only resolve
completed versions), materialized read-only in the repo,
and exposed in `python exec` blocks as `inputs`, an object whose attributes and
items are `Path`s:

```python
scores = read_scalars(inputs.descent)                         # metrics.jsonl of the input
table = read_csv(inputs["crops"] / "tables" / "cases.csv")
```

Images use `input:descent/figures/trajectory.png`. The lightbox of such a figure
shows the input reference (`descent@v3//figures/trajectory.png`) and the exact
artifact version. The repo is the project's (with its remote, if `[repo] remote`
names one); the build does not touch `.cairn-inputs.jsonl`: a report records its inputs in its
own manifest instead.

**Strict.** A report may read only its own folder and its declared inputs.
The read helpers raise a build error for any other path. While a `python exec`
block (or a `{{ }}` value) runs, an audit hook also fails the build when the
code opens a file (or lists a folder) inside the project or the repo that
is neither in the report folder nor in a declared input, for example
`open("runs/fit/metrics.jsonl")`. The error names the file and the report
line and says to declare it under `inputs:`. Paths elsewhere (the standard
library, site-packages, temporary files, font caches) and writes into the
report's generated output stay allowed. Known gaps: code that reads through a
subprocess or a native library that opens files without Python's `open`, files
reached through a path outside the project and the repo (a repo kept
elsewhere is watched, other locations are not), and imports of project modules
(importing is not reading data).

The manifest (`.cairn/build/<slug>/manifest.json`) and `report-data.json`
record `inputs: {name: {requested, resolved, uid, files}}`; the manifest also lists
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
The frame rate is read from the MP4, so frame steps are exact. `serve`, `cairn server`, and `cairn ui` answer range requests, so browsers can seek.

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

### Copy location

Next to a text selection, a figure, and the lightbox, a copy button puts a one-line location marker on
the clipboard, to paste into a chat with an LLM: `reports/a/report.md:42 "the selected text"`, or for a
figure `reports/a/report.md:42 figure #fig-psnr "PSNR per method" (source: <file>)`.

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
fetched, from jsdelivr at the version the build bundles. The page leaves out the
build manifest. A page above
16 MB fails with its largest figures listed.

Publish the file exactly as written; it says so in its first lines. On
claude.ai, that is the Artifact tool with this `file_path` and nothing else. To
change the page, edit the report and run `share` again.

## Runs: the Python API

```python
import cairn

run = cairn.init(name="train", config=cfg)       # optional; started implicitly by the first cairn.use() or cairn.draft()
data = cairn.use("datasets/crops@latest")         # an input of the run: the exact Version (data.path: its files)
run.add({"loss": loss, "psnr": psnr, "step": step})            # a plain entry -> metrics.jsonl
run.add_figure("samples", fig, {"loss": loss, "step": step})    # every add_* takes optional metadata for that item
with cairn.draft("models/student") as model:      # an output of the run
    model.add({"val_loss": v, "step": step})
    model.add_file("model.pt")
# the run completes when the process exits (failed on an unhandled exception, a non-zero exit, or a crash)
```

- **A run** is a version of the artifact `runs/<name>`, with `metadata.kind = "run"` and
  `metadata.config` (`metadata=` adds more). The name defaults to the script's stem (`python -m pkg.mod`:
  the module's name; an interactive session: `interactive`). There is one run per process:
  `cairn.init()` again returns it (another name raises), `cairn.run()` returns it or None. If
  `cairn.init` was not called, the first `cairn.use`, `cairn.pull`, or `cairn.draft` starts one; call
  `init` first to give it a name and a config, or later: `cairn.init(config=..., metadata=...)` on a
  running run adds them to its metadata (in the repo too), whoever started it. `run.finish()` ends it
  early (a Hydra job does).
- **Its version exists from the start** (running), so the outputs published while it runs can name it
  by its uid. With a remote it is synced every `[repo] interval` seconds with a heartbeat
  (`watch`, the default there), so the remote's UI shows it live; without one it is written when it
  ends (`watch=True` to follow it).
- **It ends with the process:** its open drafts are published, then the run completes. An unhandled
  exception (also Ctrl-C), a non-zero exit (`sys.exit(2)`, `raise SystemExit(2)`; a message such as
  `sys.exit("bad input")` counts as 1, while `sys.exit(0)` and `sys.exit()` complete the run), or
  SIGTERM (when no other handler is installed) fails it: the traceback or "the process exited with
  code N" goes to `logs/error.txt`, a non-zero exit code is also in `metadata.exit_code`, the version
  is marked failed (its outputs keep their edge to it, and the UI shows it failed), and its folder is
  kept (`cairn drafts`). A `SystemExit` that the script catches does not count. (Before Python 3.12
  only `sys.exit` is noticed, not a bare `raise SystemExit`.)
- **A run killed outright** (SIGKILL, the OOM killer, a node failure) leaves no trace of its own, so
  its running version carries a heartbeat (`<repo>/heartbeats/<uid>`, or its outbox entry's with a remote).
  Opening the repo (any `cairn` command, `cairn.connect()`, the UI) and `cairn status` fail every running
  version whose process is gone on this host, or whose heartbeat is older than `[repo] heartbeat_timeout`
  (600 s by default), with the error "the process died (no heartbeat since ...)". With a remote the next
  sync does the same (see "Sync").
- **`cairn.use(ref)`** resolves once, records the exact version as an input of the run, and returns the
  `Version` (its files are listed lazily; `.path` materializes it read-only; `.open(path)` streams one
  file). `cairn.pull(ref)` does the same and returns the folder. A subprocess sees the run's inputs file
  as `$CAIRN_INPUTS` (so `cairn pull` in a shell script is recorded too) and the run as `$CAIRN_RUN`: a
  Python subprocess does not start a run of its own, its pulls and drafts belong to the parent's run.
- **`cairn.draft(name)`** is a fresh, empty folder (`.path`, in `.cairn/drafts/<name>/<uid>/`, git-ignored)
  that becomes the next version of `name` when the block ends: published exactly like `cairn.publish`
  (the unchanged check, the digest, `aliases=`, `label=`, default the start time), then removed (its
  blobs are copy-on-write clones, or hard links where the filesystem cannot clone). An
  exception keeps the folder (state `kept`) and publishes nothing. Without `with`: `publish()`,
  `fail()`, `discard()`; `Artifact.draft(...)` on the object API; `watch=True` keeps a running version
  synced while it is open; `config=`/`metadata=`/`set_metadata()` for its metadata. Its one input is the
  run; there is no other linking: what the process used is the run's input, wherever it was pulled.
- `cairn.current_draft()` returns the innermost open draft (or the one the working directory lies in).

```bash
cairn drafts                                  # open, abandoned, and kept drafts and runs: name, uid, state, age, size, path
cairn drafts publish <uid|path>               # publish a kept one (refused if its run.json says failed,
                                              #   unless --allow-incomplete)
cairn drafts discard <uid|path>               # delete it
```

### The run folder

`init(capture=True, system=True, code=True)` decide what the run records besides what the job adds:

```text
runs/<name>@v<n>/
├── metrics.jsonl          run.add({...}): one entry per call, the dict as given plus time and origin
├── artifacts.jsonl        one entry per add_* file: name, kind, path, time, origin, metadata
├── figures/ images/ videos/ tables/ data/ text/ files/    what add_figure, add_image, ... wrote
├── code/                  the source snapshot: the commit (git-tree.tar.gz), the diff (working-tree.patch),
│                          untracked sources (untracked/), or a copy (tree/) outside git; manifest.json says
│                          what was captured and skipped (secrets, data/, datasets/, run and Cairn folders never)
├── environment/           pip-freeze.txt (the installed distributions), pixi.lock when the project has one
├── logs/                  stdout.txt and stderr.txt (a tee of the file descriptors: still printed, also what
│                          subprocesses write; capture=False turns it off), error.txt when the run failed
├── system/                info.json (host, platform, Python, command line, cwd, pid, the Slurm job and array
│                          task, the variables SLURM_* and CUDA_VISIBLE_DEVICES and no others), metrics.jsonl
│                          (CPU, memory, disk, network, GPUs, every 10 s; system=<seconds> for another interval)
└── .hydra/ provenance/    for a Hydra job whose folder is the run's (see Hydra runs)
```

A run writes no `run.json`: the version's status (running, completed, failed) and its times are the
record. System metrics come from psutil when it is installed, else `/proc` and the standard library
(load average, maximum RSS, disk usage); GPUs from pynvml, else `nvidia-smi`, else none.

### Lineage through runs

A run's inputs are every version the process used or pulled; every draft it published has exactly one
input, the run (named by its uid while the run runs; the repo accepts a running version as an input
only by its exact uid, and still refuses unknown, failed, and deleted ones). So the repo records
`datasets/crops@v3 -> runs/train@v7 -> models/student@v2`. `Version.inputs` and `consumers` are those
direct edges; `Version.run` is the run an output came from; `Version.upstream()` and `downstream()`
look through runs (the data an output came from, the outputs made from a dataset), and so does
`cairn.lineage(ref)` (`through_runs=False` for the direct edges); `cairn show` prints the run and the
data behind it. A run with outputs is protected from deletion as their input, like any input. The UI
draws runs as their own nodes (a `run` pill; failed and running ones marked) and has a "Data only"
toggle that leaves them out.

### Writing files: `add` and `add_*`

A run and a draft share one writer (`cairn.client.logger.Writer`); every `add_*` takes an optional
`metadata` dict for that item, kept with its entry in `artifacts.jsonl`:

```python
w.add({"loss": loss, "psnr": psnr, "step": step})   # metrics.jsonl; non-finite numbers are null, listed in `nonfinite`
w.add_figure("errors", error_histogram, {"step": 3}, errs)   # figures/errors.png; a plot function is called with errs
w.add_image("sample", array, {"step": 3})           # images/sample.png (or a copy of an image file)
w.add_video("rollout", frames, None, fps=30)        # videos/rollout.mp4 (H.264, seekable); or a file path
w.add_plotly("trace", spec)                         # figures/trace.plotly.json
w.add_table("cases", rows)                          # tables/cases.csv
w.add_json("summary", result)                       # data/summary.json
w.add_text("solver", solver_log)                    # text/solver.txt
w.add_file("checkpoints/best.pt")                   # files/best.pt (name="model.pt" for another name)
w.add_dir("checkpoints/", "ckpt")                   # files/ckpt/...
```

Both index files are append-only, and every entry records the code location that wrote it. Reports and
scripts read them with `read_scalars(folder)` (the latest value of every key), `read_series(folder,
key)` (every value with its step and time), `read_jsonl`, and `read_artifacts(folder)`; they also read
the one-scalar lines (`{name, value, step}`) that cairn-report 0.11 wrote.

A job that is not a Cairn run writes its folder with a draft (`cairn.draft(name)` is a managed folder
and the same writer). A folder that already exists is followed with `cairn publish runs/x/latest x --watch -- python run.py`, which
adds new and changed files to one running version of `x` every `[repo] interval` seconds, then
completes it (or marks it failed when the command fails); the command's pulls become the version's
inputs.

## Hydra runs

`cairn.runs.hydra` makes every Hydra job a run (needs the `hydra` extra), so a project's entry point
stays a few lines:

```python
# experiments/run.py
@hydra.main(version_base=None, config_path="../configs", config_name="config")
def main(cfg: DictConfig) -> None:
    run_experiment(cfg, Experiment)   # from cairn.runs.hydra; Experiment is the project's base class
```

```yaml
# configs/config.yaml
run:
  name: feasibility            # a short lowercase slug: the run is a version of runs/feasibility
  description: What the run tests.
hydra:
  run: {dir: "${cairn.run:}"}                                   # the job folder is the run's folder
  sweep: {dir: "${cairn.run:}", subdir: "${hydra.job.num}"}     # one run per trial, plus the sweep's summary run
  callbacks:
    source_snapshot: {_target_: cairn.runs.snapshot.SourceSnapshotCallback}
    study_completion: {_target_: cairn.runs.hydra.StudyCompletion}   # multiruns
```

- `run_experiment` calls `cairn.init(name=cfg.run.name, config=<the resolved job config>,
  metadata={"overrides": [...]})`, requires the source snapshot, instantiates `cfg.experiment`, calls its
  `run(output_dir)`, and finishes the run when it returns (failed, with the traceback, when it raises).
  The experiment gets the run with `cairn.run()` (`run.add(...)`) and publishes its outputs with
  `cairn.draft(...)`.
- `${cairn.run:}` (an OmegaConf resolver, registered when `cairn.runs.hydra` is imported, and cached so
  every resolution in a job and every trial of a sweep gets the same folder) reserves a run folder: the
  job folder is the run's, so `.hydra/`, `provenance/` (the snapshot, which then replaces `code/`), the
  job's files, and the run's logs and system metrics are one version of `runs/<run.name>`.
- **A sweep:** each trial (`<sweep>/<n>`) is a run of its own, a version of `runs/<run.name>` labelled
  `<sweep label>/<n>` with `metadata.sweep` = {uid, label, num, ref}, published when the trial ends (its
  folder then leaves the sweep's; trials.jsonl lists it). `StudyCompletion` (and `cairn finalize-study
  <sweep folder>`, for a launcher that died) publishes the sweep folder (multirun.yaml, trials.jsonl) as
  the summary: a run of its own whose inputs are the trial runs, with `metadata.sweep.trials` and the
  sweep's base config and overrides; it is kept, not published, when a trial did not complete.
- `${cairn.draft:<name>}` still works: the job folder is then an output draft of `<name>` (a sweep's
  trials its subfolders) and the run gets a folder of its own. A single job's draft is published when
  the experiment completes, with the job's run as its input and the job config as its metadata; a sweep
  draft by `StudyCompletion`, with the trial runs as inputs.
- With a plain run dir (`runs/${run.name}/${now:%Y-%m-%d_%H-%M-%S}`), the job folder gets `run.json`
  (`running`, then `completed` or `failed`, and `run`: the run's reference), and a completed single job
  becomes `runs/<name>/latest`, a relative symlink; `StudyCompletion` writes a study's `run.json` and
  moves `latest` once every trial completed. Only such job folders get a `run.json`.
- `SourceSnapshotCallback` stores the project's source files (size-bounded, secrets and data excluded)
  in each job's `provenance/` (`git` mode by default: the commit, the diff, and untracked sources;
  `tree`: a copy) and the runtime environment.

## The repo

The repo holds **artifacts**: named, versioned folders inside a **project**. A name is a
slash path (`descent`, `datasets/crops`); reports are the artifacts `reports/<slug>`. A
version is immutable once completed (until it is deleted: see below) and records its files (by content hash), metadata,
the exact versions it consumed (`inputs`), and where it was published from. Aliases
(`latest` moves by itself; `best`, `paper` by hand) and labels select versions:
`[project:]name[@v3|@uid|@alias|@label][//file]`.

Every version also has a **uid**, a UUIDv7 made where the version is created and kept everywhere:
`datasets/crops@0192f3a4-5b6c-7d8e-9f01-23456789abcd` names that version as exactly as `@v3`. A selector in
the canonical lowercase UUIDv7 form is always a uid, so no alias or label may take that form (nor `v<n>`).
`v<n>` is the source of truth's readable name; the uid is the identity, so it also names a pending version
(one the remote has not numbered yet). Every repo has a uid too (`repo.json`).

The repo's SQLite database (`metadata.sqlite3`: projects, versions, aliases and every alias move, who
consumed which version) and its file contents, named by their SHA-256 (`blobs/`), live side by side. Every
change is one transaction, so a publish is stored whole or not at all, even if the process is killed halfway.
A publish clones files into the repo where the filesystem can (APFS `clonefile`, Btrfs/XFS `FICLONE`), so
the repo's copy takes no extra space until either side changes. `cairn export-metadata <repo> <dir>` writes the
metadata as readable JSON Lines, for backups and inspection.

### The remote: `cairn server`

To share a repo with other machines, serve it over HTTP; other projects name it as their remote. The two
halves are separate:

- the **server** (`cairn server`) serves a repo: a JSON API and file downloads, bearer tokens, no pages;
- the **UI** (`cairn ui`) is a separate process that explores a repo in a browser: the project's (which
  reads through its remote, if it has one), a repo folder, or a remote, so it can run on a laptop against
  a remote on a cluster.

```bash
cairn server                 # the project's repo (.cairn): on 127.0.0.1:8800, its UI on 127.0.0.1:8801
cairn server /srv/cairn      # any repo folder; put an HTTPS reverse proxy in front for other machines
cairn server /srv/cairn --no-ui [--port 8800] [--max-blob-gb 50]     # the server alone
```

Without a folder, `cairn server` serves the project's repo, creating it if needed; a project with
`[repo] remote` is refused (its repo is a copy of that remote's). The server is just one more process
that opens the repo: the project's scripts and commands on the same machine keep opening the folder
directly while it runs, and starting or stopping it changes nothing for them. One server per repo
(`server.lock`). A taken port is an error, never a silent move, because clients' `[repo] remote` names it.

The first `cairn server` run on a new folder creates it with a random owner token (owner-only, in
`<repo>/token`); every launch prints `Repo: <url>`, `UI: <url>`, a one-time `Log in:` link (a random
code that works once within 10 minutes, never the token) and `Token: <owner token>` to the terminal.
Binding to a non-local `--host` warns: the server speaks plain HTTP. By default `cairn server` also starts
`cairn ui` as a child process (own port: `--ui-port`, default `--port` + 1; same `--host`) pointed at this
server. The child gets the owner token only in its environment, never on its command line, and stops with
the server; if the UI dies, the server keeps running and prints a warning. `--no-ui` starts the server alone.

For local testing, `cairn server <folder> --no-auth` needs no token: every request acts as the owner,
clients connect without `CAIRN_TOKEN`, and the UI child runs `--no-auth` too. It is refused on any
`--host` but loopback.

Keep a served repo on a local disk of the server's machine. `cairn server` refuses a folder on NFS,
Lustre, GPFS, SMB, BeeGFS, Ceph and other network filesystems, where SQLite's and the server's locks are
not reliable; `--allow-network-fs` overrides that when nothing else ever touches the folder.

The server is built for many clients at once: it is asynchronous (uvicorn's event loop, with disk
work in worker threads), uploads are hashed and written while they arrive, downloads stream from the
blob files (with `sendfile` on Linux), the project and artifact listings come from summary rows kept
current in the database (and are cached until the project changes), completed versions' files and
manifests are sent as `immutable`, and JSON answers carry an ETag (`If-None-Match` gets 304).
`bench/bench_store.py` measures it (results in `bench/RESULTS.md`):

```bash
taskpolicy -b python bench/bench_store.py --json out.json     # macOS: background priority
nice python bench/bench_store.py --size-mb 1024 --clients 32   # elsewhere; see --help
```

```bash
cairn token create /srv/cairn --project my-study --write --name cluster   # printed once
cairn token list /srv/cairn       # names, projects, access: never token values
cairn token revoke /srv/cairn cluster
```

The owner token can do everything. A scoped token reads one project, and publishes to it only
with `--write`; everything else is 403 and a scoped token only sees its own project. Scoped
tokens are stored hashed (`tokens.jsonl`); revoking one takes effect immediately, also for
UI sessions.

In a project that uses a remote:

```toml
[repo]
remote = "https://cairn.example.org"              # the source of truth (omit: the repo is)
project = "my-study"                              # default: the folder name
token_file = "~/.config/cairn/token"              # chmod 600; or CAIRN_TOKEN
allow_http = false                                # plain HTTP to a non-local remote (default: refused)
exclude = [".submitit/**/*.pkl"]                  # never published: gitignore-style globs on project paths
interval = 30                                     # seconds between live syncs of a running version
heartbeat_timeout = 600                           # seconds without a heartbeat before a running version is failed
allow_network_fs = false                          # see "Clusters"
```

### Commands

```bash
cairn publish data/crops datasets/crops --alias paper      # a folder becomes the next version (none if unchanged)
cairn publish runs/x/latest x --allow-incomplete           # publish although run.json is not completed
cairn publish runs/x/latest x --watch -- python run.py     # sync a running job; complete (or fail) with it
cairn publish reports/alpha                                # build a report and publish reports/alpha
cairn pull datasets/crops@paper                            # print the local, read-only copy's path
cairn pull x@latest --into DIR [--no-record]               # copy it into DIR instead
```

```bash
cairn init [FOLDER]                                        # create a repo (see "Finding the repo")
cairn project list                                         # projects, artifact counts, last update
cairn project create my-study --title "My study" [--description ...]
cairn project show my-study                                # its title, description, and artifacts
cairn project edit my-study --description "..."
cairn artifacts [--prefix reports/] [--project other]      # latest version, version count, aliases
cairn versions datasets/crops                              # number (or pending), status, label, created, uid, size
cairn show datasets/crops@paper                            # metadata, files, inputs, consumers
cairn status                                               # what waits for the remote: pending, running, rejected
cairn sync [--watch]                                       # send it now (and mirror the remote's aliases)
cairn pull datasets/crops@paper --offline                  # never ask the remote: the repo answers
cairn prune [--older-than 30d] [--max-size 20G]            # trim read-only copies by last pull
cairn verify [REF] [--repo FOLDER] [--repair]              # re-hash what the repo holds (see Integrity)
cairn alias datasets/crops@v3 best                         # point an alias at a version (latest moves by itself)
cairn delete datasets/crops@v2 --reason "..."              # a tombstone; see Deleting and garbage collection
cairn gc --dry-run                                         # retention rules and unreferenced blobs
```

All of them work the same with and without a remote. Aliases and deletions change the source of
truth, so with a remote they are made there (and then mirrored into the repo); while the remote does not
answer they fail with a message that says so.

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

`cairn.connect()` opens the project's repo, with its remote if `[repo] remote` names one (or
`cairn.connect("https://...", token=...)` for a remote with the project's repo, or
`cairn.connect("path/to/repo")` for a repo folder on this machine; `project=` sets the default project).
Objects are thin and lazy: they fetch on first access and keep what they fetched.

```python
import cairn

repo = cairn.connect()
project = repo.project()                        # the configured project; repo.project("other"), repo.projects()
project.update(description="Denoising study")   # repo.create_project("new", title="New")
crops = project.artifact("datasets/crops")      # project.artifacts(prefix="datasets/")

v3 = crops.version(3)                           # or "v3", an alias, a label, or a uid; crops.latest, crops.versions()
v3.ref, v3.uid, v3.label, v3.status, v3.created, v3.metadata, v3.inputs, v3.consumers
[(f.path, f.size, f.sha256) for f in v3.files]
with v3.open("tables/cases.csv") as handle:     # streams one file without pulling the version; recorded as an input
    header = handle.readline()
folder = v3.pull()                              # <repo>/versions/<uid>/files/, read-only; recorded as an input

fit = project.artifact("models/fit")
v = fit.publish("runs/fit", metadata={"loss": 0.1}, aliases=["best"], inputs=[v3.ref])   # -> Version
fit.alias("paper", v)
with fit.publishing("runs/live") as running:    # a running version that follows a folder while a job writes it
    ...
    running.sync()
with fit.draft(aliases=["best"]) as model:      # a fresh folder that becomes the next version (see Runs)
    model.add({"loss": 0.1})
model.version                                   # its input is the process's run
model.version.run, model.version.upstream()     # the run, and the data behind it (see Lineage through runs)
repo.pending(), repo.sync()                     # what waits for the remote; send it now
```

`cairn.init`, `cairn.run`, `cairn.use`, `cairn.draft`, and `cairn.current_draft` are the run API above.
The functions `cairn.pull`, `cairn.publish`, `cairn.alias`, `cairn.versions`, `cairn.lineage`, and
`cairn.Publication` stay, as shortcuts on top of these objects (`cairn.publish` returns the version
number, or None while the version is pending). The object API alone (`Version.pull()`, `open()`)
starts no run: inside one, what it reads is the run's input; outside, it is recorded in `$CAIRN_INPUTS`
or the working folder's `.cairn-inputs.jsonl`, which `cairn publish` carries along as the folder's
inputs.

### Deleting and garbage collection

The repo is an artifact store, not a tracker: versions can be deleted, and their files go once
nothing names them any more. With a remote, deletions happen there (the source of truth) and are mirrored
into the repo; they need the remote to answer.

```bash
cairn delete datasets/crops@v3 --reason "wrong crop size"   # one version (also @uid, @alias, @label)
cairn delete --artifact scratch/tmp                          # every version of an artifact (all or none)
cairn project delete old-study [--force]                     # refused while it has versions, unless --force
cairn delete datasets/crops@v1 --force                       # a protected version too (the owner token on a remote)
```

A deleted version becomes a **tombstone**: it stays in the version list (status `deleted`, with
`deleted_at`, `deleted_by`, and the reason; the UI shows it greyed with a `deleted` pill), keeps its
label, metadata, and inputs, and loses its files. Its number and uid are never used again: the next
publish gets the next number, and a repo that sends the same uid again is refused. Any reference to
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
cairn gc --dry-run                                   # the project's repo (or its remote): what would be deleted and freed
cairn gc [--grace 24h]                               # apply the rules, then sweep the blobs
cairn gc /srv/cairn --dry-run                        # a repo folder on this machine
cairn server /srv/cairn --gc-interval 24h [--grace 24h]   # also collect garbage on a schedule (default: never)
```

`cairn gc` first applies the retention rules, then sweeps the blobs: a blob that no version (other
than deleted ones) names in any project, and whose file is older than the **grace period** (24 h by
default), is removed; the output says how many bytes were freed. The grace period protects uploads
in progress: a client uploads the blobs before it commits the version that names them, and blobs an
outbox entry needs are never garbage. The sweep re-checks every candidate inside the same transaction
that removes it, while a publish checks its blobs inside
its own transaction: a publish racing a sweep either finds its blobs kept, or is answered "upload these
blobs first" (409 with `missing`), and the client uploads them and sends the version again, so it ends
complete and downloadable. Stale temporary uploads (untouched for the grace period, at least an hour)
go too. A dry run changes nothing. On a remote, deleting needs a write token for the project,
while `--force` and `gc` need the owner token.

The same in Python: `version.delete(reason=..., force=False)`, `artifact.delete()`, `project.delete()`,
`project.retention = [{"prefix": "runs/", "keep_last": 5}]` (or `project.set_retention("runs/",
keep_last=5)`), and `repo.gc(dry_run=True, grace="24h")`, which returns what was (or would be)
deleted, the protected versions it skipped, and the blobs and bytes freed. The API routes are
`DELETE /api/v2/projects/<p>/artifacts/<name>/versions/<n>` (`?force=1&reason=...`),
`DELETE .../artifacts/<name>`, `DELETE /api/v2/projects/<p>`, `GET`/`PUT /api/v2/projects/<p>/retention`,
and `POST /api/v2/gc {dry_run, grace}`.

After `cairn delete` here, the repo holds the tombstone too, so exact references to it fail here as
well; another machine's repo learns of the deletion the next time it reads that version from the remote.

### Sync: pending versions and the outbox

With a remote, the remote numbers versions; the repo keeps what is on its way there and copies of what
it used:

```text
<repo>/outbox/<uid>/manifest.json      a version the remote does not have yet: everything the remote needs, and its
                                       sync state (pending, running, rejected)
<repo>/outbox/<uid>/heartbeat          a running version's owner: time, host, pid (rewritten while it runs)
<repo>/blobs/<aa>/<sha256>             the contents (shared with everything else in the repo)
<repo>/metadata.sqlite3                copies of the remote's versions this repo used or sent, with the remote's
                                       numbers and its aliases (none on a network filesystem)
```

**Publishing** first commits the version to the repo: the outbox entry (manifest, label, metadata,
inputs, aliases) and the contents it needs, cloned (copy-on-write where the filesystem can) or copied
into `blobs/`, so the upload never reads the source folder again: a file changed after `publish` returns
does not change the version. Then it is sent. If the remote answers, the version gets its number at once
(a running version is created on the remote as soon as it starts). If not, `publish` returns a
**pending** version (its uid is set, its number is None) that is usable at once: `cairn.pull`,
`Version.open`, and a report's `inputs:` resolve it by uid, by an alias it was published with, by its
label, or as `latest` (when it is newer than what the remote has), with a warning that it is pending.
Its uid-exact reference `project:name@<uid>` is what lineage records until the remote numbers it.

**Sending** (any `cairn` command at its start and end, a process that left something pending in the
background and at exit, `cairn sync`): for each outbox entry, in dependency order (an entry after the
pending entries it names as inputs, else oldest first), the blobs the remote lacks, then the manifest;
the remote's answer (the version, with its number) is written into the repo as a copy, the artifact's
aliases are mirrored, and only then is the entry removed. Every request carries the version's uid and
the remote answers a uid it already has with that version, so a sync killed at any point, or two senders
on one outbox, never make a second version; senders claim an entry (`.flushing-<host>-<pid>`) to avoid
double work. The remote turns uid inputs into `@v<n>`; when it finds a pending version unchanged and keeps
its own, the entries that consumed the pending one name the remote's version instead. A version the remote
refuses for good (its own content: an unknown input, a name it does not allow) stays as `rejected` with the
reason (`cairn status` shows it; delete its folder to drop it). Everything else waits: a remote that is down
or errors (5xx), a token that may not write (401/403: `publish` says so, the version stays pending), an
input that is not sent yet, an answer the repo could not record (a busy database). Waiting costs a process
little: a remote that does not answer at all costs it one short look (3 s), after which the process leaves it
alone for 30 s, and so does a remote that answers but slowly. A run's start waits for the remote at most 3 s in
all, its end 4 s, and the last attempt at exit 4 s; a single publish or live sync 3 s; a command (`publish`, `pull`,
`versions`, ...) 10 s in all. These budgets count waiting, not transfers: while a file's bytes flow, the upload (or
download) goes on, also at a run's end (a job does not exit with its output half sent; a line on stderr says what is
being sent), and it gives up only when nothing moved for 10 s. Whatever did not go out is pending: the background
uploader, the next command, or `cairn sync` sends it. A process keeps its connections to the remote alive.

**Reading** asks the remote and copies what it gets into the repo; an exact reference the repo has
(`@v<n>` of a copy, `@<uid>`) never asks the remote. While the remote does not answer, the repo answers
from its copies and mirrored aliases (`latest` is the newest completed version it knows: a copy or a
pending one), with a warning naming what it used; `--offline` (`offline=True`) never asks the remote.

**Running versions** (a run, `cairn publish --watch`) are outbox entries of their own process, synced
every `[repo] interval` seconds while they run; their owner rewrites `heartbeat` every few seconds. A job
that dies leaves its version running only until a sync notices: on the same host a dead pid fails it at
once, and a sync on any host fails it once its heartbeat is older than `[repo] heartbeat_timeout`
(default 600 seconds). The remote itself fails a running version no client has synced for 10 minutes (a
job whose node went away with its outbox). Completion or failure turns it pending like any publish.

`cairn status` lists every outbox entry with its host, age, state, heartbeat, and last error; empty means
the remote has everything. `cairn sync` sends them and mirrors the remote's aliases of every artifact the
repo has versions of; `cairn sync --watch` keeps doing so every `[repo] interval` seconds.

**A remote added later.** A repo that had no remote numbered its versions itself. At its first sync with
a remote, it hands them to the outbox (oldest first, their inputs named by uid, their aliases along) and
they leave its database; the remote numbers them as they are sent, and they come back as copies with the
remote's numbers. Until then they count as pending (uids, no numbers). A repo has one remote: if
`[repo] remote` later names a different one, sync stops with a message.

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
blob is not silently reused (`cairn verify --repair` drops a damaged copy of a remote's blob so the next
pull downloads it again).

**Copy-on-write pulls.** Pulled files are copy-on-write clones of the blobs where the filesystem has them
(APFS `clonefile`, Btrfs/XFS `FICLONE`): they cost no space, are files of their own, and editing one
(after a `chmod`) cannot touch the blob. Where cloning is unsupported, files are hard-linked and always
read-only (`0444`): a hard link shares the blob's inode, so writing through a writable one would corrupt
the blob and every version that shares it. If hard links fail too (another filesystem), they are copied.

**`cairn verify`** re-hashes and exits 1 on any problem it cannot fix:

```bash
cairn verify datasets/crops@v3     # one version: its digest, its read-only folder, the blobs the repo holds for it
cairn verify                       # the whole repo: every blob against its name, every version's blobs and digest
                                   #   (copies: their digests), every read-only folder, every outbox entry
cairn verify --repo /srv/cairn     # a repo folder on this machine (a server's)
cairn verify --repair              # remove what can be made again: broken read-only folders, broken blobs only
                                   #   copies of the remote's versions need; never the only copy of anything
```

The object API returns the same as a report: `cairn.connect().verify(ref=None, repair=False)` gives a
`Report` with `.ok`, `.problems` (kind, path, message), and `.summary()`.

### Clusters (Slurm)

The safe setup:

- run `cairn server` on a machine with a local disk (not on a compute node, and with its repo not on a
  shared filesystem), behind HTTPS, and set `[repo] remote` in the project;
- the project lives on persistent storage, so its repo `.cairn/` is there too. On a network filesystem
  (NFS, Lustre, GPFS, ...) the repo has no database: jobs keep each version in its own outbox folder
  (one writer each) and the blobs it needs (written once and renamed into place, safe on NFS), and read
  version metadata from the remote. Without a remote Cairn refuses such a repo and explains this setup;
  `[repo] allow_network_fs = true` overrides that when one process on one host is the repo's only writer;
- jobs that reach the remote send their versions themselves (a run is synced while it runs); jobs that
  cannot leave outbox entries, which the next command that reaches the remote sends, or `cairn sync
  --watch` on the login node;
- `cairn status` shows what is still pending, from which host, and why.

```bash
#!/bin/bash
#SBATCH ...
cd /work/my-group/me/my-study                                  # the project: its outbox waits here
python fit.py                                                  # a run: synced while it runs, sent when it ends
```

```bash
# on a login node: send what jobs left, as they leave it
cd /work/my-group/me/my-study && cairn sync --watch
```

`exclude` takes gitignore-style globs on project paths: `*` and `?` stay within a folder, `**`
spans folders, a leading `/` anchors to the project root.

The server's API lives under `/api/v2/` (`GET /api/v2/repo` answers its uid and version) and its
files under `/files/<project>/<name>@vN//<path>` (or `@<uid>`)
(ETag, Range, `Content-Security-Policy: sandbox`); the routes are listed in
`cairn/server/__init__.py`. Besides publishing and resolving it answers what a viewer needs:
projects with their artifact count and last update, an artifact's summary with its latest version and
aliases, a version's file list, and lineage. Tokens are bearer tokens only: the server has no
cookies, no login page and no HTML. It never runs pushed code.

### Exploring a repo: `cairn ui`

```bash
cairn ui                                   # the project's repo (with its remote, if it has one), on 127.0.0.1:8801
cairn ui --remote https://cairn.example.org  # a remote, e.g. from a laptop against a cluster-side server
cairn ui --repo /srv/cairn                 # a repo folder on this machine (a server's)
cairn ui --no-browser                      # print the login link without opening it
```

For the project's repo, `cairn ui` creates it (with an owner token) if it is new and opens it directly,
whether a `cairn server` serves it or not; with a remote it reads through the remote and shows the
versions still waiting for it as **pending** (a `pending` badge, their uid instead of `vN`). It prints a
one-time `Log in:` link and opens it in the browser, so there is no token to paste; for a remote the link
logs in with `CAIRN_TOKEN` or `[repo] token_file`, if set. A taken port (a server's own UI on 8801) moves
it to a free one.

The UI opens a repo only through the repo interface, so it does no work in the server. It prints
`UI: <url> (repo: <url or folder>)` and serves a React app (built files in the package, no Node
needed) in the look of the reports (their stylesheet, tokens and class names):

- `/` projects (title, description, artifacts, last update);
- `/p/<project>/` the project: a search box (names, titles, status, aliases), then the tabs
  "Reports" (title, status, summary, latest version, an Open report link), "Artifacts" (latest,
  aliases, versions, updated), and "Runs" (the `runs/` artifacts: latest completed version, config
  keys, versions), sorted by update;
- `/p/<project>/a/<name>[@vN|@alias]` an artifact: its versions (status, label, aliases, source
  host) with a version picker, and for the chosen version a copyable reference and `cairn pull`
  command, the files (download, inline preview of images, video, text and JSON, a file
  reference to copy), the metadata, the inputs and the consumers, and the lineage graph (inputs on
  the left, the version in the middle, consumers on the right; click a node to open it, `+` to
  expand one more level; runs are drawn with a `run` pill, failed and running ones marked, and a run
  in between counts no level, so the data behind it comes along; "Show runs / Data only" toggles
  them). A "Download zip" button streams the whole version as `<name>-v<n>.zip` (a running version:
  what is synced so far). A report artifact has an Open report button. A version of kind `run` has a
  Run panel: a chart of every numeric key of `metrics.jsonl` (over `step`, else time) and of
  `system/metrics.jsonl`, the tails of `logs/stdout.txt`, `stderr.txt`, and `error.txt`, where it ran
  (host, command, directory, Slurm job, Python, sweep), and its config; it refreshes every 5 s while
  the run is running;
- `/p/<project>/r/<slug>[@vN]/` a report version's built page, as before (a report is an artifact
  `reports/<slug>` with `site/` and `source/`); its Data panel links each input to the artifact page
  of the exact version, and its brand link returns to the project in the app;
- `/files/<project>/<name>@vN//<path>` (or `@<uid>`) the repo's files, with Range (videos seek);
  `/files/<project>/<name>@vN.zip` a whole version as a zip (folders kept under `<name>-v<n>/`, a
  stored symlink as its target's file, compressed types stored and the rest deflated, ZIP64), written
  while it is sent: nothing is held in memory or written to disk.

The app talks to the UI process through `/ui-api/` (GET unless noted), a session-scoped passthrough
to the repo interface: `me` (scope: `owner`, `project`, or `remote` when a remote decides),
`projects`, `projects/<p>`, `projects/<p>/artifacts/<name>` (summary, versions, aliases),
`projects/<p>/artifacts/<name>/versions/<n>` (manifest with files, consumers; `<n>` may also be a uid,
an alias, or a label; a pending version has `version: null` and `pending: true`), `projects/<p>/artifacts/<name>/versions/<n>/run` (the Run panel's data: metrics and
system rows, numbers only, thinned to 2000; the last 64 KB of the logs; info.json; config),
`lineage?ref=<project:name@sel>[&depth=1..3]` (inputs and consumers, assembled from
one-level lookups, capped at 200 nodes), and `POST login`. Every call needs the session and the
header `X-Cairn: 1` (a cross-site request cannot send it); a name may be one URL-encoded segment.

**Login.** Besides the one-time link, the UI takes a token on `/login` (the owner token or a scoped one)
and checks it: a remote is asked with it; for a repo on this machine its owner token file and
`tokens.jsonl` are checked. The app's login page posts the token to `/ui-api/login`; it stays in the UI process, in an in-memory session table; the
browser gets only a random session id (HttpOnly, SameSite=Strict cookie; `Secure` when a reverse
proxy says `X-Forwarded-Proto: https` or the UI listens on a public address, not for plain HTTP to
this machine such as an SSH tunnel; `--insecure-cookies` drops it everywhere), and the token is never in a URL or a page. Each
session acts with its own token, so a scoped token sees only its project, and a revoked token ends
its session. Sessions last 30 days and are forgotten when the UI restarts.

`--no-auth` (loopback `--host` only) has no login: the UI uses `CAIRN_TOKEN` or
`[repo] token_file` against a remote (none against a `--no-auth` server), or the owner's full
access to a repo on this machine. `--allow-http` permits plain HTTP to a non-local remote, as for `publish`.
A report page runs in the UI's origin: give write tokens to people whose reports you trust.

#### Developing the UI

The app's source is `web/` (React 18, TypeScript, Vite; it imports
`src/cairn/reports/template/report.css` and `report-extra.css` so the look has one source, plus a small
`web/src/app.css`). The built files in `src/cairn/ui/static/` are committed, so `pip install` needs
no Node.

```bash
cairn ui --repo /srv/cairn --insecure-cookies     # a UI to talk to, on 127.0.0.1:8801
cd web && npm ci && npm run dev                   # http://localhost:5173, proxying /ui-api, /files and reports to it
npm run build                                     # type-checks, then writes src/cairn/ui/static/ (commit it)
```

`CAIRN_UI_URL` points the dev proxy at another UI address. `tests/browser_check.py` walks the UI
in headless Chrome and writes screenshots (`taskpolicy -b uv run --with playwright python
tests/browser_check.py`).

## Where Cairn keeps things

Everything Cairn keeps for a project is its repo, one folder: `.cairn/` in the project root (like
W&B's `./wandb/`). It holds a `.gitignore` containing `*`, so git ignores it; your own `.gitignore` is
never edited. The repo is private to its owner (mode 700).

```text
<project>/.cairn/                the repo
├── .gitignore                   "*"
├── repo.json                    the repo's uid, its layout version, and its remote's uid once learned
├── metadata.sqlite3             projects, versions, aliases, lineage (WAL); not on a network filesystem
├── blobs/<aa>/<sha256>          file contents, written once (temporary file, then rename), shared by all versions
├── outbox/<uid>/                versions on their way to the remote (manifest, state, heartbeat); only with a remote
├── versions/<uid>/files/        read-only folders that pulls return (made from the blobs; `cairn prune` trims them)
├── drafts/                      open and kept drafts and runs: <name>/<uid>/ and <name>/<uid>.draft.json (`cairn drafts`)
├── build/  share/               built report pages, `cairn share` pages
├── token  tokens.jsonl          the owner token and scoped tokens (`cairn server`, `cairn ui`)
├── heartbeats/<uid>             running versions' heartbeats (without a remote)
├── server.lock                  held by the one `cairn server` serving this repo
└── tmp/                         uploads in progress, the hash cache
```

A server's repo (`cairn server /srv/cairn`) has the same layout; it simply has no project files around it.

| What | Resolution |
| --- | --- |
| the repo | `$CAIRN_DIR`, else `[cairn] dir` in `cairn.toml` (relative to the project root), else `<project root>/.cairn` |
| built pages | `output_dir` in `cairn.toml`, else `<repo>/build` |

**Finding the repo** is one rule, the same for every command and script:

1. `$CAIRN_DIR` set: that folder is the repo.
2. Otherwise Cairn walks from the working directory upward. The first folder that holds `.cairn/repo.json`
   (an existing repo, the way git finds `.git`) or a `cairn.toml` is the
   project root; its repo is `[cairn] dir`, else `<root>/.cairn`.
3. Nothing found: whatever opens the repo (`cairn.init`, `use`, `draft`, `publish`, `cairn ui`, `cairn server`
   without a folder) creates `.cairn/` in the working directory and says so in one line on stderr. Commands that
   only look at a repo (`cairn status`, `verify`, `prune`, `sync`) say "no repo here (run `cairn init`)" and
   exit 1 instead.

```bash
cairn init [FOLDER]     # create FOLDER/.cairn (default: here; $CAIRN_DIR if set); an existing repo is left as it is
```

`cairn init` writes only the repo (`repo.json`, its `.gitignore` of `*`, mode 700): never a `cairn.toml`, never
the project's own `.gitignore`. Source snapshots (a run's `code/`, a Hydra job's `provenance/`) skip `.cairn`.

## Configuration

Everything has a default; `cairn.toml` at the project root changes it:

```toml
reports_dir = "reports"            # holds one folder per report
output_dir = ".cairn/build"        # the built site (default: <repo>/build)

[cairn]
dir = ".cairn"                     # the repo (default; or CAIRN_DIR)

[repo]
remote = "https://cairn.example.org"  # optional: the source of truth
project = "my-study"                  # default: the project folder name
token_file = "~/.config/cairn/token"  # or CAIRN_TOKEN
allow_http = false                    # plain HTTP to a non-local remote
interval = 30                         # seconds between live syncs of a running version
heartbeat_timeout = 600               # seconds without a heartbeat before a running version is failed
allow_network_fs = false              # one process on one host writes a repo on a network filesystem
exclude = ["*.pkl"]                   # never published: gitignore-style globs on project paths

[style]
css = ["style.css"]                # extra stylesheets, loaded after the default style
template_dir = "template"          # replaces index.html, report.css, report-extra.css, report.js by name
```

## Development

```bash
uv sync
uv run pytest
```
