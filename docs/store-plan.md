> **Historical design note, superseded by [cairn-plan.md](cairn-plan.md) and, for storage, by
> [architecture.md](architecture.md).** This is the plan the artifact
> store was first built from, written while the project was still called cairn-report. It is kept for the
> reasoning behind the design; where it differs from the code or from `cairn-plan.md` and the README, those
> win. Read `cairn_report` as `cairn`, `cairn-report store serve|token|migrate` as `cairn server|token|migrate`,
> and `cairn-report.toml` as `cairn.toml`; file names below are the old ones.

# cairn-report as an artifact store

## Context

cairn-report today is a report builder, a run logger, and a "hub". The hub stores one
report tree and a flat list of run files per project. The owner does not want a tracker. They want
an artifact store tailored to their research, where the main way to look at data is through
reports:

- reports are artifacts, written in our Markdown dialect;
- run outputs are artifacts;
- runs on other machines and clusters download artifacts through a light Python API;
- a report declares the artifacts it consumes in its front matter, so its data can always be traced
  back.

Decisions already made with the owner:

- **Home:** evolve cairn-report; the hub becomes the store server.
- **Granularity:** only named artifacts. Nothing becomes an artifact automatically. The user
  publishes (syncs) a folder, such as a run's outputs, under a name. The run layout depends on the
  Hydra config, and cairn-report must stay standalone without needing Hydra.
- **Report inputs:** declared, pinned at build, and strictly enforced.
- **Local first:** a project works offline against a store on disk; the server is the same store
  shared over HTTP.
- **Reports:** a report is a single page, with no sub-reports. It is versioned as its whole folder
  (`report.md` plus any data kept with it). A project holds several flat reports, one per research
  direction.
- **Access:** an owner token plus project-scoped tokens (read or write).
- **MLflow:** dropped. The old hub data is migrated.
- **Lineage:** recorded for runs too. Code that pulls artifacts records the versions it read, and
  publishing that code's output folder carries them along. No Hydra hook is involved.

## The model

- **Project**: a first-class record with name, title, description, creation time, and a scoped token
  list. Everything else belongs to a project.
- **Artifact**: `(project, name)`. A name is a slash path chosen by the user (`descent`,
  `datasets/crops`, `models/denoiser`). The only kind the store treats specially is a report, which
  cairn-report builds; it is named `reports/<slug>`.
- **Version**: immutable once completed. A version manifest holds:
  - `version` (an integer per artifact);
  - `label` (optional, for example a run's timestamp, given at publish);
  - `status` (`running`, `completed`, or `failed`);
  - `created`;
  - `files` (`{path, sha256, size}` or `{path, link}`);
  - `metadata`;
  - `inputs` (the exact versions it consumed);
  - `source` (the host and folder it was published from).
  Only completed versions are resolvable by aliases. A running version may grow, which keeps live
  syncing from clusters.
- **Aliases**: movable names per artifact. `latest` is moved automatically when a version completes;
  `best`, `paper`, … are set by hand. Every alias move is recorded in an append-only log.
- **Reference syntax** (one parser, used everywhere): `[project:]name[@v3|@alias|@label]`, with an
  optional `//path/in/artifact` after it, since names themselves contain slashes. Examples:
  `descent@latest//figures/trajectory.png`, `datasets/crops@v3`,
  `other-project:models/denoiser@best`.
- **Storage**: the hub's content-addressed blobs (`blobs/<aa>/<sha>`, written once, verified) are
  kept as they are. They are reused from `hub.py` (`Store.put_blob`, `has_blob`, `missing`) and
  `push.py` (`HashCache`, `upload_missing`).
  - Per project: `projects/<p>/project.json` and `projects/<p>/artifacts/<name>/versions.jsonl`
    (one line per version state).
  - Per artifact: an `aliases.jsonl` log.
  - A derived `consumers` index for lineage lookups.

## Python API (`cairn_report.store`)

```python
from cairn_report import store

crops = store.pull("datasets/crops@v3")           # Path to a local read-only copy; the version is recorded as an input
v = store.publish("models/denoiser", "checkpoints/best", metadata={"psnr": 31.2}, aliases=["best"])
store.alias("models/denoiser@v4", "paper")
store.versions("descent")                          # [{version, label, status, created, inputs, ...}]
store.lineage("reports/boundary@v2")               # upstream and downstream versions
```

- **Store choice:** the store comes from `[store]` in `cairn-report.toml`: `path = ".store"`
  (local, the default) or `url = "https://…"` (server). The token comes from `CAIRN_STORE_TOKEN` or
  `token_file`. One `Store` interface has two implementations: `LocalStore` (a folder) and
  `RemoteStore` (HTTP, reusing `push.Hub`'s request, auth, and HTTP-vs-HTTPS rules).
- **Pulling:** `pull` materializes a version into a cache
  (`~/.cache/cairn-report/artifacts/<project>/<name>/v<n>/`), hard-linking from a local blob
  cache. It is idempotent and read-only.
- **Recording inputs without Hydra:**
  - Every `pull` appends the exact version it read to an inputs file. That file is
    `$CAIRN_INPUTS` if set, else `.cairn-inputs.jsonl` in the current working directory.
  - With Hydra's `chdir: true` the working directory is the run folder, so the record lands in the
    run's outputs. A plain script gets the same result by setting `CAIRN_INPUTS`.
  - Publishing a folder reads its `.cairn-inputs.jsonl` into the version's `inputs`. `publish(...,
    inputs=[...])` adds inputs explicitly.

## Publishing folders (the user syncs runs)

The store knows nothing about Hydra or run layouts. The user decides what becomes an artifact:

```bash
cairn-report publish runs/descent/2026-10-15_07-49-37 descent --label 2026-10-15_07-49-37
cairn-report publish runs/descent/latest descent               # follows the link; the label defaults to its target's name
cairn-report publish data/crops datasets/crops --alias v-paper
cairn-report publish runs/sweep/latest sweep --watch -- python experiments/run.py -m ...   # sync while running
```

- **A new version only when something changed.** It follows `HashCache` and the push's unchanged
  check.
- **Watching:** `--watch` (and `-- <command>`) syncs one running version while the command runs,
  then completes it, or marks it failed if the command fails. This reuses `push --watch` and
  `RunPusher`'s incremental upload.
- **Python:** `store.publish(name, folder, ...)` does the same.
- **Optional `[store.publish]` mappings** in `cairn-report.toml` (e.g.
  `{ folder = "runs/descent/latest", name = "descent" }`) let `cairn-report publish` with no
  arguments sync a project's usual folders. These mappings are the user's choice, not a convention
  of the store.
- **Hydra stays optional.** `cairn_report.runs` (run.json, latest, StudyCompletion) remains as the
  `[hydra]` extra, independent of the store. A project may call `store.publish` at the end of its
  experiment if it wants automatic publishing.
- **The logger stays local.** The writer (`Writer`) only writes files; live upload of a running job is
  `publish --watch`. The hub backend goes, and so does MLflow: `mlflow_backend.py`, `mlflow:`
  references, the `[logger.mlflow]` keys, the `cairn-report mlflow` command, and
  `tests/test_mlflow.py`.

## Reports (`build.py`, `template/`)

- **Layout:** each report is one folder, `reports/<slug>/` with `report.md` plus `assets/` and
  optional `data/`. It is versioned as a whole. There is no nesting.
- **Removed nesting code:**
  - in `build.py`: `discover_reports` nesting, `parent_of`, `depth`, `card_id`, `child_cards` (the
    ```` ```reports ```` fence), the parent back link, and the tree in `site.json`;
  - in `report.js`: `reportTree` and `reportBranch`.
- **Inputs:** the front matter declares them by local name:
  ```yaml
  inputs:
    descent: descent@latest
    crops: datasets/crops@v3
  ```
  The build resolves each one to an exact version, pulls it, and exposes `inputs.descent` (a Path) in
  the `python exec` namespace. Images use `input:descent/figures/trajectory.png`, which replaces
  `run:`. The `run_dir()` helper is replaced by `inputs.<name>`.
- **Strict enforcement:**
  - the read helpers (`read_json`, `read_csv`, …) and `Build.track` refuse paths outside the report
    folder and the declared inputs;
  - a `sys.addaudithook` on `open` during `python exec` fails the build when code opens a project
    file or a store-cache file that isn't declared;
  - stdlib, site-packages, and temp paths stay allowed.
- **Recording:** the resolved inputs are written to `manifest.json` and to the report's version
  manifest (`inputs`).
- **Publishing:** `cairn-report publish reports/<slug>` recognizes a report folder. It builds the
  report and stores a new `reports/<slug>` version containing the source folder, the built page, and
  the resolved inputs. `push` becomes `publish`.
- **Navigation:** the sidebar lists the project's reports (flat) from the store or the local
  folder. Every page gets a **Data** panel listing each input with its exact version, linked to its
  artifact page. The lightbox trace shows the artifact reference instead of the run path.
- **Kept unchanged:** comments, copy-location, video, layout, and `share` (now one page).

## Server (`hub.py` → `store_server.py`; `cairn-report store serve`)

- **API v2:**
  - projects: list, create, and update;
  - artifacts and versions: list and get;
  - a version is created in three steps: start, upload the missing blobs (the existing
    `blobs/missing` and `PUT` protocol), and commit the manifest;
  - set an alias;
  - lineage (upstream and downstream);
  - file download with Range (the existing `ranges.byte_range`).
- **Auth:** the owner token works as today, including the automatic token on first launch. Scoped
  tokens are created with `cairn-report store token create --project P [--write]`; they are stored
  hashed and limited to one project.
- **Pages (served by the separate `cairn-report ui` process, not by the store server, which is only the
  API and downloads):**
  - `/`: projects;
  - `/p/<project>/`: the project's reports and artifacts;
  - `/p/<project>/r/<slug>[@v]/`: a report;
  - `/p/<project>/a/<name>[@v]/`: an artifact's versions, files, inputs, and consumers, which is
    the back-trace view.
- **The server never executes pushed code.** Reports are built where they are written.

## Migration and naming

- `cairn-report store migrate <old-hub-root> <new-root>`:
  - each old project becomes a project;
  - each old report version is split into one `reports/<slug>` version per report folder;
  - nested keys are flattened (`a/b` becomes `a-b`) and noted in the version metadata;
  - each `<name>/<timestamp>` run in `runs.jsonl` becomes a version of the artifact `<name>`,
    labelled by its timestamp, from its latest file states.
- **Naming:** "hub" becomes "store" in the CLI, config, and docs (`[store]` replaces `[hub]`), with
  no aliases. This is a breaking release, **v0.13.0**. The skill switches over in a separate step
  afterwards.

## Build order (each step ends with tests passing)

1. **Core:**
   - `store/` with the reference parser, manifests, `LocalStore` (blobs, versions, aliases, the
     consumers index), the pull cache, and `pull`/`publish`/`alias`/`versions`/`lineage`;
   - tests for immutability, alias moves, idempotent pulls, and reference parsing.
2. **Publishing:**
   - `cairn-report publish <folder> <name>` (also `--watch`, `-- cmd`, and `[store.publish]`
     mappings) and `store.publish`;
   - `.cairn-inputs.jsonl` recording;
   - the hub logger backend and MLflow are removed.
3. **Reports:**
   - flat single-page reports;
   - the `inputs` front matter and the `input:` reference;
   - strict enforcement with the audit hook;
   - the Data panel;
   - `publish`;
   - the nesting code is removed.
4. **Server:**
   - `RemoteStore`;
   - API v2;
   - scoped tokens;
   - the project, report, and artifact pages;
   - the existing hub tests are ported.
5. **Migration, README, and release v0.13.0.** Then update the `standalone-research-project` skill
   (single-page reports, `inputs:`, `store.pull`/`publish`, `runs/`).

## Critical files

- **New:** `src/cairn_report/store/{__init__,refs,local,remote,server,cache}.py`.
- **Changed:**
  - `build.py` (run resolution → inputs; nesting removed);
  - `logger.py` (backends removed; local files only);
  - `hub_backend.py` (removed; its sync loop moves into `publish --watch`);
  - `push.py` (client and upload helpers reused by `RemoteStore`);
  - `hub.py` (→ the server; blob store kept);
  - `config.py` (`[store]`; MLflow keys removed);
  - `cli.py`;
  - `template/report.js` (flat navigation, Data panel).
- **Removed:** `mlflow_backend.py`, `tests/test_mlflow.py`, and the nesting tests in
  `test_reports.py`.

## Verification

- **Unit tests per step.** These cover immutability (a completed version is never rewritten), alias
  semantics, reference parsing, strict-input failures (an undeclared read fails the build, including
  a raw `open()`), input recording through `.cairn-inputs.jsonl`, unchanged folders not creating versions, and scoped-token denial.
- **End-to-end test** (pytest, with a server in a thread and two temporary "machines"):
  1. Machine A runs a plain script (no Hydra) against the server. The script pulls
     `datasets/toy@v1` and writes outputs.
  2. Machine A publishes the output folder as `toy-fit`.
  3. Machine B builds a report with `inputs: {fit: toy-fit@latest}` and publishes it.
  4. The test asserts the lineage chain `datasets/toy` → `toy-fit` → `reports/<slug>`, through the
     API and on the artifact page.
  5. A second case does the same from a Hydra run folder (`chdir: true`), to check that inputs land
     in the run's outputs.
- **Migration test:** the existing `conftest` tree and run are pushed to an old hub, migrated, and
  the reports and runs are checked as resolvable artifacts.
- **Headless Chrome (Playwright with the installed Chrome, at background priority):** the project
  page, a report with its Data panel and links, an artifact page with lineage, and video and comments
  still working.
- **Real use:** run the demo project from this session against `cairn-report store serve /tmp/store`.
