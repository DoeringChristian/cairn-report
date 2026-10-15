# Changelog

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
  `CAIRN_DIR` or `[cairn] dir`. Earlier layouts (`.store/`, `generated/reports/`, the global pending
  folder) move on first use and are announced; `outputs_dir` is ignored.
- **SQLite metadata, an asynchronous server.** A store folder keeps its metadata in one SQLite
  database (`metadata.sqlite3`) and its files as content-addressed blobs; every change is one
  transaction, so a publish is stored whole or not at all. An older JSON Lines store is imported the
  first time it is opened, and `cairn export-metadata` writes the metadata back out as readable JSON
  Lines. The server is asynchronous (uvicorn), streams uploads and downloads, and answers listings
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
    `log.video`.
  - One reading column for text and figures; figures keep their own shape; small images stay
    pixel-sharp.
  - Copy-location buttons give LLM-ready markers.
- **Runs as artifacts.** Every process that uses or publishes artifacts is a run: `cairn.init(name=...,
  config=...)` (or, implicitly, the first `cairn.use`, `cairn.pull`, or `cairn.draft`) makes it a version of
  `runs/<name>` (`metadata.kind = "run"`, `metadata.config`), running from the start and completed when the
  process exits (failed on an unhandled exception or SIGTERM, with `logs/error.txt`). One run per process;
  `cairn.run()`; `run.finish()`.
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
  lines of earlier logs. `cairn.runs.logger.RunLogger` keeps the old names (`scalar`, `figure`, ...) for
  one release with a DeprecationWarning; drafts have only the new ones.
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
- **Package layout.** `store`, `client`, `server`, `ui`, `reports` and `runs`, with import
  boundaries enforced by a test. `import cairn` loads no server, UI, report or plotting code.

### Removed

- The hub (`cairn-report hub`, `push`, `pull` of report trees), MLflow mirroring, and the logger's hub
  and MLflow backends (`[logger]`).
- Nested reports, the ```` ```reports ```` fence, the overview report, and `run:` references.

### Migrating

- Install `cairn` instead of `cairn-report`. Commands become `cairn ...`, imports `import cairn` /
  `cairn.runs...` / `cairn.reports...`, and the config file `cairn.toml`. `cairn-report.toml` is
  read for this release, with a warning.
- Old hub data: `cairn migrate <old-hub-root> <new-root>`.
- `cairn.reports.build()` is `cairn.reports.build_all()` (a package function must not shadow the
  `cairn.reports.build` module).
- Reports: move each report to `reports/<slug>/report.md`, publish the runs it reads, declare them
  under `inputs:`, and replace `run:` with `input:`.
- Logging: `run.scalar(name, v, step=s)` is `run.add({name: v, "step": s})`; `figure(name, f, *args)` is
  `add_figure(name, f, None, *args)`; `table`, `json`, `plotly`, `image`, `video` gain the `add_` prefix;
  `text` writes `text/<name>.txt` (was `logs/`) and `artifact` is `add_file` (`files/`, was `artifacts/`).
- Drafts no longer record pulls themselves and `Draft.use()` is gone: what a process pulls is its run's
  input, and its drafts' one input is the run. Code that read `version.inputs` of an output for the data
  reads `version.upstream()` (or `cairn.lineage(ref)`, which now looks through runs and also returns `runs`).
