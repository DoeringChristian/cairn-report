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

## Layout after the rename

```text
src/cairn/
├── __init__.py        the light API: pull, publish, alias, versions, lineage, Publication
├── cli.py             `cairn ...`
├── refs.py, local.py, remote.py, cache.py, blobs.py, tokens.py, migrate.py   (from store/)
├── build.py, share.py, serve.py, template/                                   (reports)
├── logger.py, runs.py, snapshot.py, ranges.py, push.py (client)
├── server/            the artifact store server (from store/server.py)
└── ui/                the UI server (from ui/) + static/ (the built web app)
web/                   React + TS + Vite source of the UI (builds into src/cairn/ui/static/)
```

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
