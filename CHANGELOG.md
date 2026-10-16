# Changelog

## 0.14.0: the repo, an optional remote, automatic sync

This release introduces the design in `docs/architecture.md`. It is the new layout: Cairn was not in use
before it, so there is no migration and no compatibility code, and a project starts a fresh repo.

- **The repo is `.cairn/` itself**: `repo.json`, `metadata.sqlite3`, `blobs/<aa>/<sha256>`, `outbox/<uid>/`,
  read-only folders under `versions/<uid>/files/`, `drafts/`, `build/`, `share/`, `token`, `server.lock`, `tmp/`.
  There is no `.cairn/store`, `.cairn/pending`, or `.cairn/cache` any more, and a server's repo has the same
  layout (`store.json` is now `repo.json`). Every process on the repo's machine opens it directly, also while
  `cairn server` serves it.
- **A remote, optionally** (`[repo] remote`): the source of truth for numbers, aliases, and deletions.
  Every write goes to the repo first and is sent automatically: at the start and end of every command, in the
  background of a process that left something pending (and once more at exit), live while a run runs, and
  with `cairn sync` (`--watch` keeps going, for a login node). A version made while the remote does not
  answer is pending (a uid, no number), usable at once, and gets the remote's number when it is sent; sending
  is idempotent by uid, in dependency order. Versions used from the remote are copied into the repo, which
  answers while the remote is away. Aliases and deletions go to the remote and fail clearly offline.
- **A remote added later:** the versions the repo numbered itself are sent at the first sync, oldest first;
  the remote numbers them and the repo takes the numbers over (uids, lineage, and reports stay valid).
- **Network filesystems:** a repo on NFS, Lustre, GPFS, SMB and the like has no database; with a remote, jobs
  keep an outbox and blobs only (metadata from the remote) and a login node sends what they left; without a
  remote, Cairn refuses with the cluster setup to use. `[repo] allow_network_fs = true` overrides it.
- **Configuration:** `[repo]` (remote, project, token_file, allow_http, interval, heartbeat_timeout,
  allow_network_fs, exclude) replaces `[store]` (now an unknown key); `[store] path` and `publish` mappings are
  gone (the repo is `[cairn] dir` / `CAIRN_DIR`). `CAIRN_TOKEN` replaces `CAIRN_STORE_TOKEN`; `CAIRN_DATA_DIR`,
  `CAIRN_CACHE_DIR`, and `--data-dir` are gone.
- **Commands:** `cairn server [repo]` serves the project's repo by default (a taken port is an error);
  `cairn ui` opens the project's repo directly (`--remote URL`, `--repo FOLDER`); `cairn status` lists the
  pending, running, and rejected versions; `cairn verify [REF] [--repo FOLDER] [--repair]`; `cairn prune` trims
  read-only folders and contents only copies need. `cairn migrate` (the hub importer) is removed.
- **Finding the repo** is one rule: `$CAIRN_DIR`, else the first folder upward from the working directory that
  holds `.cairn/repo.json` or `cairn.toml` (`pixi.toml` and `pyproject.toml` do not count; its repo is `[cairn] dir`, else
  `.cairn`); with nothing found, opening the repo creates `.cairn/` in the working directory (and says so), while
  `cairn status`, `verify`, `prune`, `sync`, and the commands that read versions (`artifacts`, `versions`, `show`,
  `pull`, `alias`, `delete`, `drafts`) refuse. `cairn init [folder]` creates a repo (idempotent). A
  `cairn-report.toml` is no longer recognized at all.
- **UI:** pending versions show a `pending` badge and their uid instead of `vN`.
- **API:** `cairn.connect()` returns a `cairn.Repo` (was `Store`); `LocalRepo` (was `LocalStore`) and
  `Remote` (was `RemoteStore`) both implement the repo interface (`cairn.store.interface`); `cairn.open_repo`
  (was `open_store`). The server's `GET /api/v2/store` is `GET /api/v2/repo`.
- **Layers:** `store/` (the repo core) changes a repo folder and knows nothing of HTTP or configuration;
  `client/remote.py` is the repo interface over HTTP; `client/sync.py` is the sync engine; `client/open.py`
  alone decides between no remote, a remote, and a network filesystem. `tests/test_boundaries.py` enforces it.

## 0.13.1

- `cairn server` with no folder serves the project's store folder (`.cairn/store`), creating it if
  needed. While it runs (it writes `<folder>/server.json`), the project's scripts and commands send
  their versions through it instead of being refused as a second writer.
- `cairn ui` with the project's store folder creates it if it is new and browses it through a running
  `cairn server` if there is one. It prints a one-time login link (a random code, valid once for 10
  minutes; never the token) and opens it in the browser (`--no-browser` to skip); `cairn server`
  prints one too. A taken port moves the UI to a free one.
- The UI's session cookie is `Secure` behind an HTTPS reverse proxy (`X-Forwarded-Proto: https`) and on
  public addresses, and plain for HTTP to this machine, where browsers refuse a `Secure` cookie.
- The repository is github.com/DoeringChristian/cairn (formerly cairn-report).

## 0.13.0: the first Cairn release

cairn-report becomes **Cairn**: an artifact store with a server and a UI, single-page reports built
from Markdown and published artifacts, and a run logger. This release breaks compatibility. Projects
that pin an older cairn-report tag keep working as they are; moving to 0.13 needs the changes under
"Migrating".

### New

- **The artifact store.** Named artifacts (`descent`, `datasets/crops`, `reports/<slug>`) with:
  - immutable versions, each with a UUIDv7 uid, a number (`v3`) and an optional label;
  - aliases, with `latest` moved automatically, and an alias log;
  - free-form metadata;
  - lineage: inputs, and the consumers derived from them.

  Projects are first-class, and blobs are content-addressed and stored once.
- **Publishing.**
  - `cairn publish <folder> <name>` stores a folder, adding nothing new when it is unchanged.
  - `--watch -- <command>` keeps a running version in sync while a job runs.
  - Folder-to-name mappings in `cairn.toml`.
  - Nothing depends on Hydra.
- **Pulling.** `cairn pull <ref>` and `cairn.pull(...)` return a read-only local copy. The pull
  records the exact version as an input in the working folder's `.cairn-inputs.jsonl`, and that is
  how lineage is captured.
- **Python API.**
  - `cairn.connect()` returns a store; `Project`, `Artifact` and `Version` objects give
    `versions()`, `version(alias | label | n | uid)`, `publish()`, `publishing()`, `alias()`,
    `files`, `open(path)` and `pull()`.
  - The shortcuts `cairn.pull`, `publish`, `alias`, `versions`, `lineage` and `Publication` remain.
- **Working offline.**
  - Publishing while the server is unreachable queues the version locally; `cairn sync` uploads it
    and `cairn status` shows what is pending.
  - `--watch` jobs start and finish even with the server down.
  - Exact versions pull from the cache, and aliases fall back to their last known version with a
    warning (`--offline`).
- **A local replica, with a data root and a cache root.** On this machine, versions the server does
  not have yet (pending, running, rejected) live in the data root; synced and pulled ones live in the
  cache root. Both are keyed by version uid, so two stores never collide. Folders and blobs are
  written under a scratch name and renamed into place, with no locks, so both roots are safe on NFS.
  `CAIRN_CACHE_DIR` selects node-local scratch on clusters, `CAIRN_DATA_DIR` the pending root.
  `cairn prune` trims the cache by last access and never touches pending or running versions.
- **One `.cairn/` folder per project.** The local store, pending versions, cache, built report pages
  and shared pages all go to `<project>/.cairn/` (with a `*` `.gitignore`), configurable with
  `CAIRN_DIR` or `[cairn] dir`.
- **SQLite metadata, an asynchronous server.** A store folder keeps its metadata in one SQLite
  database (`metadata.sqlite3`) and its files as content-addressed blobs; every change is one
  transaction, so a publish is stored whole or not at all. `cairn export-metadata` writes the
  metadata out as readable JSON Lines. The server is asynchronous (uvicorn), streams uploads and downloads, and answers listings
  from summary rows; publishes clone files into the store where the filesystem can. Measured
  numbers (against the first server) are in `bench/RESULTS.md`.
- **Network filesystems are refused for a store folder.** `cairn server` stops on NFS, Lustre, GPFS,
  SMB, BeeGFS, Ceph and similar (`--allow-network-fs` overrides); a local store on one warns once.
  Cluster jobs talk to the server over HTTP, and their pull caches may live on NFS.
- **Integrity.**
  - Every completed version has a `digest`, the SHA-256 of its canonical file list, checked by the
    server on upload and shown by `cairn show` and the UI.
  - Pulls are verified: every file is hashed as it is placed, and a mismatch fails the pull and
    removes the half-built folder.
  - Pulled files are copy-on-write clones where the filesystem supports it, else read-only hard
    links, so editing a pulled file cannot damage the store.
  - `cairn verify` re-hashes a version, the cache, pending versions, or a local store (`--repair`
    fixes the cache); it exits 1 on any problem.
- **Deleting, retention, and garbage collection.** `cairn delete` removes a version or an artifact
  as a tombstone (its number and uid are never reused). `cairn retention set` keeps the last N
  versions and/or the last N days per project or artifact prefix, and `cairn gc` applies the rules
  and removes blobs no version names (`--dry-run` shows what would go). Versions that carry an alias
  other than `latest`, are inputs of a live version, or are running are protected: deleting one
  needs `--force`, and retention skips it.
- **Study lineage.** Publishing a folder merges every `.cairn-inputs.jsonl` found anywhere inside it
  (for example one per Hydra multirun trial) into the version's inputs, deduplicated; none of them
  becomes a file of the version.
- **Incomplete-run check.** `cairn publish` refuses a folder whose `run.json` says the run is not
  `completed`, unless `--allow-incomplete` (`allow_incomplete=True`) is given, which records
  `metadata.run_status`. `--watch` is exempt.
- **The store server** (`cairn server <root>`).
  - A JSON API (`/api/v2`) and file downloads with Range and ETag.
  - An owner token, created and printed at first launch, plus project-scoped tokens
    (`cairn token create|list|revoke`), stored hashed.
  - `--no-auth`, on loopback only.
  - It knows nothing about reports, and it starts the UI as a child process (`--no-ui` turns that
    off).
- **The UI** (`cairn ui [--store URL|PATH]`).
  - A React app in the look of the reports: projects, a project's reports and artifacts with search,
    and artifact pages with versions, files and previews, metadata, inputs, consumers and a lineage
    graph.
  - The report viewer.
  - It works against a local folder or a remote server. The store token stays server-side in the UI
    process.
- **Reports are single pages.**
  - Each report is `reports/<slug>/report.md`. Front matter `inputs:` pins exactly what the report
    reads, and reads are strictly enforced (helpers plus an audit hook).
  - `input:<name>/<file>` figures, and a Data panel listing every input's exact version.
  - `cairn publish reports/<slug>` stores the report (source, built page, pinned inputs) as an
    artifact.
- **Reports, other additions.**
  - Videos: cards that play on screen and a lightbox player with frame steps, scrubbing and speed;
    `add_video`.
  - One reading column for text and figures; figures keep their own shape; small images stay
    pixel-sharp.
  - Copy-location buttons give LLM-ready markers.
- **Runs as artifacts.** Every process that uses or publishes artifacts is a run: `cairn.init(name=...,
  config=...)` (or, implicitly, the first `cairn.use`, `cairn.pull`, or `cairn.draft`) makes it a version of
  `runs/<name>` (`metadata.kind = "run"`, `metadata.config`), running from the start and completed when the
  process exits. One run per process; `cairn.run()`; `run.finish()`.
  - It fails, with `logs/error.txt` and its folder kept, on an unhandled exception, on SIGTERM, and on a
    non-zero exit: `sys.exit(n)` and `raise SystemExit(n)` that leave the script record "the process exited
    with code n" and `metadata.exit_code` (a message counts as 1; `sys.exit(0)` and `sys.exit()` complete
    the run). The exit code is read from the main module's frame with `sys.monitoring` (Python 3.12+);
    on 3.11 only `sys.exit` is noticed.
  - A run killed without a chance to clean up (SIGKILL, the OOM killer) does not stay `running`: a running
    version in a local store has a heartbeat too, and opening the store, any `cairn` command, and
    `cairn status` fail the versions whose process is gone on this host or whose heartbeat is older than
    `[store] heartbeat_timeout`, with the error "the process died (no heartbeat since ...)".
  - `cairn.init(config=..., metadata=...)` after the run started (for example by the first `cairn.use`)
    adds them to the running run's metadata, in the store too; only a different name is refused.
  - Its folder records `code/` (the source snapshot), `environment/` (installed packages, pixi.lock),
    `logs/stdout.txt` and `stderr.txt` (a tee; still printed), and `system/info.json` and
    `system/metrics.jsonl` (CPU, memory, disk, network, GPUs every 10 s; psutil and pynvml when installed).
    It writes no `run.json`: the version's status is the record.
  - With a store server it is synced while it runs (`watch`, with a heartbeat), so the UI shows it live.
  - `cairn.use(ref)` returns the exact `Version` (lazy files, `.path` materializes) and records it as an
    input of the run; a subprocess of a run (`$CAIRN_RUN`) records into that run.
- **Lineage through runs.** A run's inputs are what the process used; every draft it publishes has exactly
  one input, its run (by uid while the run is running: the store now accepts a running version as an input
  when named by its exact uid, and still refuses unknown, failed, and deleted ones). `Version.run`,
  `Version.upstream()` / `downstream()`, and `cairn.lineage(ref, through_runs=True)` look through runs;
  `Version.inputs` stays the direct edges; `cairn show` prints the run and the data behind it. The store's
  lineage answer has `nodes` (status and kind of each reference).
- **Drafts: writing a version directly.** `with cairn.draft("models/student") as model:` (also
  `Artifact.draft(...)`) gives a fresh, empty folder in `.cairn/drafts/<name>/<uid>/` that becomes the
  artifact's next version when the block ends, exactly as `cairn.publish` would store it (unchanged check,
  digest, aliases; the label defaults to the start time; the version gets the draft's uid), and is then
  removed; blobs are cloned or hard-linked, not copied, where possible. An exception keeps the folder for
  inspection and publishes nothing. `config=` becomes `metadata["config"]`; `watch=True` keeps a running
  version synced; `cairn.current_draft()`; `cairn drafts`, `cairn drafts publish <uid>`,
  `cairn drafts discard <uid>`.
- **One writer API for runs and drafts.** `add({...})` appends one entry (any keys; `step` is just a key)
  to `metrics.jsonl`; `add_figure(name, fig_or_plot_function, metadata, *args)`, `add_image`,
  `add_video`, `add_plotly`, `add_table`, `add_json`, `add_text` (text/), `add_file` and `add_dir`
  (files/) each take an optional `metadata` dict for the item, kept in `artifacts.jsonl`. The readers
  (`read_scalars`, `read_jsonl`, the new `read_series`) take entries with several keys and the one-scalar
  lines of cairn-report 0.11 logs.
- **Hydra jobs are runs** (`cairn.runs.hydra`, `cairn.runs.snapshot`): `run_experiment` calls
  `cairn.init(name=cfg.run.name, config=<resolved config>, metadata={"overrides": ...})`;
  `hydra.run.dir: ${cairn.run:}` makes the job folder the run's folder (`.hydra/` and `provenance/`
  included). A sweep (`hydra.sweep.dir: ${cairn.run:}`) is one run per trial (label `<sweep>/<n>`,
  `metadata.sweep`) plus the summary run `StudyCompletion` publishes, whose inputs are the trial runs.
  `${cairn.draft:<name>}` keeps working as an output draft. `cairn finalize-study` covers both.
  Labels may be words joined by `/`.
- **UI: runs.** A Run panel for versions of kind `run` (charts of `metrics.jsonl` and the system metrics,
  the tails of stdout and stderr, refreshed while running, the config, where it ran); run nodes in the
  lineage graph (a `run` pill, failed and running marked) and a "Data only" toggle; a Runs tab and rail
  section; "Download zip" for a whole version (`/files/<p>/<name>@<sel>.zip`, streamed).
- **UI: the reports' look.** The UI uses the report stylesheets unchanged (no separate theme, so no dark
  mode). The rail lists the projects and the current project's reports, artifacts and runs, on app
  pages and on reports opened in the UI, which also get a back link to where the reader came from.
  Selects are a listbox in the report style instead of the system menu. At phone width the rail folds
  into a "Browse" bar, tables and the lineage graph scroll in their own box, and the page never
  scrolls sideways.
- **Package layout.** `store`, `client`, `server`, `ui`, `reports` and `runs`, with import
  boundaries enforced by a test. `import cairn` loads no server, UI, report or plotting code.

### Removed

- The hub (`cairn-report hub`, `push`, `pull` of report trees), MLflow mirroring, and the logger's hub
  and MLflow backends (`[logger]`).
- Nested reports, the ```` ```reports ```` fence, the overview report, and `run:` references.
- Report comments (the comment popover, Copy and Clear, `[page] copy_preamble`). The copy-location
  buttons stay: on a text selection, a figure, and in the lightbox.

### Migrating

- Install `cairn` instead of `cairn-report`. Commands become `cairn ...`, imports `import cairn` /
  `cairn.runs...` / `cairn.reports...`, and the config file `cairn.toml` (a `cairn-report.toml` is
  not read; Cairn asks you to rename it).
- Old hub data: `cairn migrate <old-hub-root> <new-root>`.
- `cairn.reports.build()` is `cairn.reports.build_all()` (a package function must not shadow the
  `cairn.reports.build` module).
- Reports: move each report to `reports/<slug>/report.md`, publish the runs it reads, declare them
  under `inputs:`, and replace `run:` with `input:`.
- Logging: cairn-report's `RunLogger` methods become the writer's: `scalar(name, v, step=s)` is
  `add({name: v, "step": s})`; `figure(name, f, *args)` is `add_figure(name, f, None, *args)`; `table`,
  `json`, `plotly`, `image`, `video` gain the `add_` prefix; `text` writes `text/<name>.txt` (was `logs/`)
  and `artifact` is `add_file` (`files/`, was `artifacts/`). The writer lives on runs and drafts
  (`cairn.init()`, `cairn.draft(name)`), not on a bare folder.
