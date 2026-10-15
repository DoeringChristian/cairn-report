# Cairn: the product

> **Since 0.14 the storage design is [architecture.md](architecture.md)** (a repo, an optional remote,
> automatic sync; fixed). Where this plan describes the store folder, the data and cache roots, the
> replica, or `[store]`, the architecture document and the README win: the repo is `.cairn/` itself, the
> replica's pending versions became the repo's outbox, its cache became the repo's blobs and copies, and
> `[repo]` replaced `[store]`.

The artifact store built in [store-plan.md](store-plan.md) becomes the product **Cairn** and will
most likely replace the earlier Cairn tracker (cairn-track). Decided with the owner on 2026-10-15.

## Decisions

- **Names:** it takes over the `cairn` name: `import cairn`, the `cairn` command, distribution
  `cairn`. cairn-track is renamed (not archived) and cannot share an environment with it.
- **Packaging:** one distribution with extras. The base install is light, for cluster jobs: the
  Python API, the CLI, the report builder, and the logger. The extras are `[server]`, `[ui]`,
  `[hydra]`, and `[video]`.
- **Three parts in one repo:**
  - **client:** the Python API and CLI, plus reports, logger, and runs;
  - **server:** the artifact store, API only, and very general;
  - **ui:** explores a store, local or remote.
- **UI stack:** React + TypeScript + Vite. The built files ship inside the package, so running it
  needs no Node.
- **UI style:** the current look (report.css and report-extra.css: tokens, dark mode, rail, cards,
  pills, tables, copy and comment buttons). The app reuses that stylesheet and its class names; it
  does not introduce a second style. Published reports keep their own page and report.js.
- **Repo:** the work continues here. Later this repo is renamed to `cairn` on GitHub, after
  cairn-track is renamed and every pin is updated. The owner confirms each rename.

## Layout

The parts are separate packages; an `ast` test (`tests/test_boundaries.py`) fails on any import that
crosses the arrows below.

```text
src/cairn/
├── __init__.py   the light API: init, run, use, draft, connect (Store, Project, Artifact, Version), pull, publish, alias, versions, lineage, Publication, open_store (client names load lazily)
├── cli.py        argument parsing only; every command imports its part lazily
├── __main__.py
├── config.py     cairn.toml (leaf: imports nothing from cairn)
├── ranges.py     HTTP byte ranges (leaf)
├── formats.py    readers for the logger's JSON Lines files (leaf)
├── _version.py   __version__ (leaf)
├── migrate.py    the one-off conversion of an old hub (knows the old report layout)
├── store/        core, no HTTP: refs, uids (UUIDv7), local (LocalStore), blobs, tokens, cache (the two roots, pull, uid-keyed, NFS-safe), errors, inputs
├── client/       http (Client, scan/hash/upload), remote (RemoteStore), objects (connect, Store, Project, Artifact, Version),
│                 publishing (publish, Publication, --watch), drafts (draft, Draft, current_draft), runs (init, Run, the process's run),
│                 system (info, system metrics, output capture), code (source snapshot, packages), logger (Writer, shared by runs and drafts), replica (the local replica: data and cache roots,
│                 records, claims, heartbeats, store files), sync (sending pending versions), prune,
│                 offline (resolution through the replica), open (open_store)
├── server/       app (handler + routes), auth, files (/files, Range, ETag), spawn (UI child); exports make_server, serve_store
├── ui/           server (UI HTTP server + /ui-api), sessions, backends (Local/RemoteBackend), static/ (built app)
├── reports/      build, serve, share, publishing (report -> artifact), template/
└── runs/         hydra (run_experiment, StudyCompletion, finalize_study, the cairn.run and cairn.draft resolvers), snapshot
web/              React + TS + Vite source of the UI (builds into src/cairn/ui/static/)
```

Allowed imports (besides the leaves `config`, `ranges`, `formats`, `_version`, which import nothing from cairn):

| part | may import |
|---|---|
| `store` | leaves only |
| `client` | `store`, `config`, `ranges`, `formats` (the run logger, client/logger.py, writes the files formats.py reads) |
| `server` | `store`, `config`, `ranges` (it starts the UI as `python -m cairn ui`, never by import) |
| `ui` | `store`, `client`, `config`, `ranges` |
| `reports` | `store`, `client`, `config`, `ranges`, `formats` |
| `runs` | `store`, `client`, `config`, `formats` |
| `migrate` | `store`, `formats` |
| `cairn/__init__` | `store`, `client` (lazily); `import cairn` loads no server, UI, reports, matplotlib, plotly, or hydra |

## Commands

- `cairn server <root>`: was `cairn-report store serve`. It starts the UI child by default, and
  `--no-ui` turns that off.
- `cairn ui [--store URL|PATH]`
- `cairn token create|list|revoke <root>`
- `cairn migrate <old-hub> <new-root>`
- `cairn publish`, `cairn pull`
- `cairn build`, `cairn serve`, `cairn share` (reports)
- `cairn finalize-study`

The config file becomes `cairn.toml`; a `cairn-report.toml` is an error that asks for the rename. The environment variables `CAIRN_STORE_TOKEN` and `CAIRN_INPUTS` keep their names.

## Steps

1. **Rename.** Package, CLI, config file, docs, and tests move to the new names and layout.
   Behaviour is unchanged. All tests pass.
2. **React UI.**
   - `web/` builds into `src/cairn/ui/static/`; the UI server serves it plus a small JSON API for it
     (a session-scoped passthrough to the store), and keeps `/files` and the report viewer.
   - Pages:
     - projects;
     - a project's reports and artifacts, with search and filters;
     - an artifact: versions, files, metadata, aliases, and an interactive lineage graph of inputs
       and consumers across versions;
     - the report viewer: the published site, with a link back to the artifact.
   - Login works as now; the token never reaches the browser.
3. **UI fixes** from the screenshots: the clipped image preview, and the sidebar position on long
   pages.
4. **Client: the object API and working offline.**
   - **The object API:** `store = cairn.connect()` (from `cairn.toml`) or
     `cairn.connect(url, token=...)`.
     - Projects: `store.projects()` and `store.project(name)` return `Project` objects, which have
       `update(title=..., description=...)`, `artifacts(prefix=...)`, and `artifact(name)`.
     - `Artifact`: `versions()`, `version(alias | label | n)`, `latest`, `publish(folder, ...)`
       returning a `Version`, `publishing(folder)` (a running version), and `alias(name, version)`.
     - `Version`: `ref`, `metadata`, `inputs`, `consumers`, `label`, `status`, `created`, `files`
       (`File(path, size, sha256)`), `open(path)` (streams one file and records the input), and
       `pull()`.
     - The module functions (`cairn.pull`, `publish`, `alias`, `versions`, `lineage`) stay as
       shortcuts on `store.project()`.
   - **CLI:** `cairn project list|create|show|edit`, `cairn artifacts [--prefix]`,
     `cairn versions <name>`, `cairn show <ref>` (metadata, files, lineage). They work the same way
     against local and remote stores.
   - **Offline publishing:**
     - every publish commits first to the local replica (pending, in the data root
       `<project>/.cairn/pending/`, or `$CAIRN_DATA_DIR`): manifest, blob references, inputs;
     - a background uploader sends it when the server answers; the server assigns the version
       number at upload, and until then the version is pending;
     - `cairn sync` sends the pending versions by hand, and `cairn status` shows what is pending;
     - `--watch` jobs start and finish even while the server is down: the running version is
       created on the server when it can be reached, and completion or failure is queued;
     - a file that changes before upload is re-hashed, so a version is uploaded exactly as
       published or not at all.
   - **Drafts** (owner request, on `artifact-store`): "a cairn managed artifact folder instead of the run
     folders, so we can also point hydra at those". `cairn.draft(name)` / `Artifact.draft()` is a fresh folder in
     `<base>/drafts/<name>/<uid>/` that is published (like `cairn.publish`, the version taking the draft's uid) and
     removed when it closes, kept on an exception; it is a writer on its folder (client/logger.py, so
     `client` may import the leaf `formats`); pulls go into every open draft (and loose pulls into the next ones),
     `use()` declares draft-to-draft inputs (published inputs first; both replaced by runs, below); `watch=True` reuses `Publication`. Hydra:
     the `${cairn.draft:name}` resolver (cached, so one folder per job and per sweep), `run_experiment` publishes a
     single run's draft with the job config as metadata, `StudyCompletion` a sweep's. `cairn drafts [publish|discard]`.
   - **Runs as artifacts** (owner-approved, on `artifact-store`, replacing "inputs go directly onto outputs"):
     every process that uses or publishes artifacts is a run, `cairn.init(name, config=...)` or implicitly the
     first `use`/`pull`/`draft`: a draft of `runs/<name>` (`metadata.kind = "run"`) whose version is running
     from the start and completes at exit (atexit via threading's exit callbacks, so thread pools still work;
     failed on an unhandled exception or SIGTERM, `logs/error.txt`). One run per process (`cairn.run()`,
     `run.finish()`). Its folder: the writer's files, `code/` (the source snapshot, client/code.py, moved out of
     runs/snapshot.py so it needs no Hydra), `environment/` (pip-freeze.txt, pixi.lock), `logs/` (an fd-level tee of
     stdout and stderr), `system/info.json` and `system/metrics.jsonl` (client/system.py); no run.json. With a
     store server it is watched (synced with a heartbeat). Lineage: a run's inputs are what the process used
     (`$CAIRN_INPUTS` points at the run's folder; an inherited one stays a sink; `$CAIRN_RUN` lets subprocesses
     record into the parent's run); every draft has exactly one input, its run, by uid while it runs. The one
     store rule change: `_exact_inputs` accepts a running version named by its exact uid (unknown, failed, and
     deleted are still refused; the server inherits it); lineage of an exact version works in any status and
     answers `nodes` (status, kind). The replica creates a running input version on the server before sending
     an output that names it. `Version.run`, `upstream()`, `downstream()`, `cairn.lineage(through_runs=True)`.
     Writer API: `add({...})` and `add_*` with per-item metadata (runs and drafts share the `Writer`; there is no
     writer for a bare folder); `formats.py` folds multi-key entries and cairn-report 0.11 lines. Hydra: `${cairn.run:}`
     reserves the run folder (the job folder), a sweep is a run per trial (label `<sweep>/<n>`) plus a summary run
     whose inputs are the trials. UI: Run panel, run nodes and a data-only toggle, a Runs tab, and (owner request)
     `GET /files/<p>/<name>@<sel>.zip` in the UI server (streamed, ZIP64; the store server is unchanged).
   - *Done (on `artifact-store`):* the ref syntax for uids is a format check, not a prefix: a selector in
     the canonical lowercase UUIDv7 form is a uid, and aliases and labels may not take that form.
   - **Version identity:**
     - every version gets a UID (ULID/UUIDv7) where it is created, on the client or in the store,
       and the server keeps it; every store gets a store UID;
     - `v<n>` stays the readable name; references also accept the UID (`name@01JA…`);
     - the pull cache is keyed by version UID (`cache/versions/<uid>/`), never by name and
       number, so two stores never collide;
     - materializing and blob downloads are safe on NFS (many Slurm jobs pulling at once): write
       into a temporary folder named by host, pid, and a random suffix, then rename into place;
       the loser of a race discards its copy; no file locks;
     - `CAIRN_CACHE_DIR` points cluster jobs at node-local scratch;
     - tests cover concurrent pulls from several processes into one cache.
   - **Offline pulls:**
     - an exact `name@vN` comes from the cache without asking the server;
     - an alias falls back to its last known resolution, with a warning naming the version, and
       records that version as the input;
     - `--offline` forces the fallback.
   - **Layout** (owner-approved, on `artifact-store`): everything Cairn writes for a project goes into one
     relocatable folder, `<project>/.cairn/` (`CAIRN_DIR`, or `[cairn] dir`), with its own `.gitignore` of `*`:
     `store/` (was `.store/`), `pending/` (the data root), `cache/` (the cache root), `build/` (was
     `generated/reports/`). `CAIRN_DATA_DIR` and `CAIRN_CACHE_DIR` still override single roots; outside a
     project the XDG folders apply.
   - **The local replica** (owner-approved, replaces the outbox; on branch `replica`):
     - two roots: `CAIRN_DATA_DIR` (durable, default `<project>/.cairn/pending`, `~/.local/share/cairn` outside a project;
       the project sits on persistent storage on a cluster) holds pending and running versions and
       their blobs, like W&B's offline runs; `cairn sync --data-dir PATH` flushes
       a given data root from a login node; `CAIRN_CACHE_DIR` (throwaway, may be node-local scratch) holds synced
       versions and downloads; pending data never lives only in the cache;
     - one record per version, `versions/<uid>/manifest.json` (sync `synced | pending | rejected |
       running`), files under `versions/<uid>/files/` (what `pull` returns); `stores/<store-uid>.json`
       keeps each server's URL, last contact, and last known aliases;
     - pending versions are usable at once (`pull`, `open`, report inputs; by uid, alias, label,
       latest), lineage records `project:name@<uid>` until the server numbers it;
     - running versions write a heartbeat; any flusher fails one whose heartbeat is older than
       `[store] heartbeat_timeout` (600 s); flushers claim with `.flushing-<host>-<pid>`, no locks;
     - `cairn sync` (also refreshes aliases), `cairn status [--all]`, `cairn prune`; a README section
       on clusters (Slurm).
5. **Performance.**
   - Measure first: a benchmark script for concurrent small publishes, large-file pull throughput,
     Range requests, and the UI's metadata queries.
   - **An async server:** the `[server]` extra brings uvicorn (its fast event loop and HTTP parser)
     and Starlette. Uploads and downloads stream, and blob downloads use `sendfile`.
   - **A faster store core:**
     - the metadata (projects, versions, aliases and their log, consumers, a summary row per artifact) in
       one SQLite database per root (WAL, short transactions, the server as its single writer); blobs stay
       files; an old JSON Lines root is imported once (decided with the owner on 2026-10-15, replacing the
       planned in-memory index and per-artifact locks);
     - store roots on network filesystems refused by `cairn server` (`--allow-network-fs` for a single
       writer);
     - upload hashing in large chunks while the data streams;
     - batched missing-blob checks;
     - caching headers on everything immutable;
     - copy-on-write clones (`clonefile` / `FICLONE`) for local publishes, so the store's copy costs
       no extra space until either side changes.
   - A performance test in the suite catches regressions, and the benchmark script gives the real
     numbers.
   - **Deleting and garbage collection** (owner-approved: an artifact store, not a tracker; on branch `gc`):
     - a deleted version is a tombstone (status `deleted`, `deleted_at`/`deleted_by`/reason, its file list
       dropped): its number and uid are never reused, references fail with "deleted" (410), lineage keeps the edge
       and marks the node; `latest` moves back;
     - protected versions (an alias other than `latest`, an input of any version that is not deleted, in any
       project, running) are refused (409) unless forced (owner token); an artifact deletes all or nothing; a
       project only when empty, or forced;
     - retention rules per project or artifact prefix (`keep_last`, `keep_days`, `keep_aliased`) in the metadata,
       applied by `cairn gc` (and `cairn server --gc-interval`), never to protected versions (skipped, reported);
     - the blob sweep (store/gc.py): unreferenced in every project and older than the grace period (24 h), re-checked
       inside the transaction that removes it; publishes check their blobs inside their own transaction after a
       sweep, so a racing publish gets 409 + `missing` and the client uploads again;
     - `cairn delete`, `cairn project delete`, `cairn retention show|set|clear`, `cairn gc`; the UI shows tombstones.
6. **Release v0.13.0 / the first Cairn release** (merged to main after the owner's OK).
7. **Repo renames**, each confirmed by the owner:
   1. rename `DoeringChristian/cairn` to `cairn-track`;
   2. update every pin;
   3. rename this repo to `cairn`.
8. **Skills:** move `standalone-research-project` and `research-project` to Cairn: single-page
   reports, `inputs:`, `cairn.pull`/`publish`, `runs/`, `cairn server`/`ui`.
