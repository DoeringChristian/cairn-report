# Cairn: the product

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
├── __init__.py   the light API: pull, publish, alias, versions, lineage, Publication, open_store (client names load lazily)
├── cli.py        argument parsing only; every command imports its part lazily
├── __main__.py
├── config.py     cairn.toml (leaf: imports nothing from cairn)
├── ranges.py     HTTP byte ranges (leaf)
├── formats.py    readers for the logger's JSON Lines files (leaf)
├── _version.py   __version__ (leaf)
├── store/        core, no HTTP: refs, local (LocalStore), blobs, tokens, cache (pull), migrate, errors, inputs
├── client/       http (Client, scan/hash/upload), remote (RemoteStore), publishing (publish, Publication, --watch), open (open_store)
├── server/       app (handler + routes), auth, files (/files, Range, ETag), spawn (UI child); exports make_server, serve_store
├── ui/           server (UI HTTP server + /ui-api), sessions, backends (Local/RemoteBackend), static/ (built app)
├── reports/      build, serve, share, publishing (report -> artifact), template/
└── runs/         logger (RunLogger), hydra (run_experiment, StudyCompletion, finalize_study), snapshot
web/              React + TS + Vite source of the UI (builds into src/cairn/ui/static/)
```

Allowed imports (besides the leaves `config`, `ranges`, `formats`, `_version`, which import nothing from cairn):

| part | may import |
|---|---|
| `store` | leaves only |
| `client` | `store`, `config`, `ranges` |
| `server` | `store`, `config`, `ranges` (it starts the UI as `python -m cairn ui`, never by import) |
| `ui` | `store`, `client`, `config`, `ranges` |
| `reports` | `store`, `client`, `config`, `ranges`, `formats` |
| `runs` | `store`, `client`, `config`, `formats` |
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

The config file becomes `cairn.toml`. `cairn-report.toml` is still read, with a warning, for one
release. The environment variables `CAIRN_STORE_TOKEN` and `CAIRN_INPUTS` keep their names.

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
3. **Release v0.13.0 / the first Cairn release** (merged to main after the owner's OK).
4. **Repo renames**, each confirmed by the owner:
   1. rename `DoeringChristian/cairn` to `cairn-track`;
   2. update every pin;
   3. rename this repo to `cairn`.
5. **Skills:** move `standalone-research-project` and `research-project` to Cairn: single-page
   reports, `inputs:`, `cairn.pull`/`publish`, `runs/`, `cairn server`/`ui`.
