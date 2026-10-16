# Cairn architecture: a repo, an optional remote, automatic sync

Decided 2026-10-15 with the owner; projects, short ids and errors added 2026-10-16. Fixed: later work fits this design; it does not change it.

## The rules

1. **A repo is a folder on a local disk: the project's `.cairn/`.** It holds the metadata (SQLite), the
   content-addressed blobs, and Cairn's working folders. Every process on the repo's machine opens it
   directly: scripts, CLI commands, `cairn ui`, and `cairn server`, which is just another such process.
   Any number of processes may read and write at once (each write is one SQLite transaction).
2. **A project has one repo and at most one remote.** Every write goes to the repo first; the repo sends
   it to the remote automatically. There is no push or pull command to remember and no switching between
   modes: the same code runs with and without a remote, online and offline.
3. **On a network filesystem there is no repo database.** On NFS, Lustre, GPFS, SMB and the like (a
   cluster's shared home or project space), SQLite and file locks are unreliable across nodes. There a
   project needs a remote, and each version goes through its own outbox folder (one writer per folder), which any
   node, including the login node, sends to the remote.
4. **One source of truth for numbers, aliases and deletions.** With a remote, the remote's server; without
   one, the repo. A version's **uid** is its identity everywhere; `vN` is the source of truth's name for it.

## Vocabulary

| term | meaning |
|---|---|
| repo | the folder `.cairn/` (`[cairn] dir`, `CAIRN_DIR`): metadata, blobs, outbox, drafts, report builds |
| remote | a project on a `cairn server`: `[repo] remote = "https://host/<project>"`, like a git remote URL |
| project | what a repo holds: locally exactly one (unnamed), on a server many, each at `<server>/<project>` |
| source of truth | the remote if configured, else the repo |
| outbox | `<repo>/outbox/<uid>/`: one version on its way to the remote (manifest + state); one writer |
| synced | the remote has the version and assigned its number |

## Layout

```text
.cairn/                      the repo (self-ignoring: .gitignore = "*")
├── repo.json                uid of this repo, layout version
├── metadata.sqlite3         versions, aliases, lineage (WAL)   — not on a network filesystem
├── blobs/<aa>/<sha256>      content-addressed files, written once (temp + rename), shared by all versions
├── outbox/<uid>/            versions not yet on the remote (manifest, state); only with a remote
├── drafts/                  folders that become versions (runs, `cairn.draft`)
├── build/  share/  reviews/ report builds, shared pages, review state
├── token  tokens.jsonl      owner token and scoped tokens, for `cairn server`
├── heartbeats/<uid>         running versions' heartbeats
├── server.lock              held by the one `cairn server` serving this repo
└── tmp/
```

A server's repo (`cairn server /srv/cairn`) has the same layout; it simply has no project files around it.

## Projects

- **Locally there are no projects.** A project's `.cairn/` holds that project only; references, the CLI,
  the Python API and `cairn ui` name artifacts without a project (`descent@v3`, `datasets/crops@best`).
- **A server hosts many projects in one repo folder**, each exposed at `<server>/<project>` as if it were
  its own repo (as a git host serves many repositories). They share the server's one database and one
  content-addressed `blobs/`, so a file published by several projects is stored once.
- **The remote URL names the project:** its last path segment (`https://host/my-study`, or
  `https://host/prefix/my-study` behind a path prefix). Syncing to a project the server does not have
  creates it when the token is the owner's; a project-scoped token only reaches its existing project.
- **There is no cross-project access.** A reference has no project prefix, a run's inputs and a report's
  inputs come from its own project, and the public functions take no `store=` pointing elsewhere. A dataset
  several projects need is published in each of them; the server stores its files once (content-addressed
  blobs), and so does a repo for a file in several versions.

## Short ids

- Every version has a **short id**: the first 7 hex digits of the SHA-256 of its uid (longer when needed
  to be unique), fixed when the version is created and the same in the repo and on the remote.
- `name@<id>` and a bare `<id>` (as in `cairn show 3f9a2c1`) name a version, by any unique prefix of
  4 or more digits. Names, numbers, aliases and labels are matched first; an ambiguous prefix is an error
  that lists the candidates.

## Versions and numbers

- **Without a remote** the repo assigns `vN` when a version starts, as a store does today.
- **With a remote** the remote assigns `vN`. A version made while the remote answers gets its number at
  once (the version is started on the remote too). A version made offline is created in the repo with its
  uid and no number ("pending"); it is written to the outbox, and when the remote takes it, the repo records
  the remote's number. References by uid, by `@latest`, and the version objects work before that.
- **Versions from the remote** that a process uses (`cairn.use`, `pull`, a report input) are copied into
  the repo with the remote's number and uid: their blobs into `blobs/`, their metadata into the database.
  The repo is the cache; there is no separate cache folder. `cairn prune` may drop copies of versions the
  remote has; it never drops a version that is not synced.
- **`@latest` in the repo** is the newest completed version the repo knows: synced, pending, or copied.
  On the remote it is the remote's latest. A report records the uid and number it resolved.
- **Aliases and deletions with a remote** need the remote (they change the source of truth): set or
  removed there, then mirrored into the repo. Offline they fail with a clear message.
- **Adding a remote to a repo** that already has numbered versions: the first sync sends them, oldest
  first; the remote assigns its numbers and the repo takes them over (uids unchanged, so lineage and
  reports, which record uids, stay valid).

## Sync

- **When:** at the start and end of every Cairn command and process that touches the repo, while a run
  runs (its live version, every `[repo] interval` seconds), and with `cairn sync` (`--watch` keeps going,
  for a login node). Each attempt is quick to fail when the remote does not answer; nothing blocks a run.
- **What:** each outbox entry (a version, and for a running version its newest state): missing blobs first,
  then the manifest; the remote's answer (number) is written to the repo, and the outbox entry is removed
  only after that. Idempotent by uid: an interrupted or repeated sync never makes a second version.
- **Refusals** (the remote rejects a version for good, e.g. a name it does not allow): the entry stays as
  `rejected` with the reason, shown by `cairn status`; never silently dropped.
- **`cairn status`** lists pending, running, and rejected entries; empty means the remote has everything.

## Network filesystems (clusters)

- A repo on a network filesystem has no `metadata.sqlite3`: opening it for writing without a remote is
  an error that explains the cluster setup. With a remote, runs write their drafts and outbox entries
  (per-version folders, one writer each) and copy used versions' blobs into `blobs/` (write-once, atomic
  rename, safe on NFS). Version metadata comes from the remote.
- Jobs that reach the remote sync themselves; jobs that do not leave outbox entries for `cairn sync` on
  the login node (or any later command that reaches the remote).
- `[repo] allow_network_fs = true` is the owner's word that one process on one host is the repo's only
  writer; then the repo database is used as on a local disk.

## Processes and failures

- **Crash or `kill -9` during a run:** the draft folder stays; the heartbeat stops; the next open of the
  repo (any command) or the remote's timeout marks the version failed. Nothing a run already finished is lost.
- **Crash during a publish:** the repo transaction is all or nothing; blobs without a version are removed by
  gc after the grace period.
- **Remote down:** versions wait in the outbox; the run is not slowed; the next command that reaches the
  remote sends them.
- **Server start or stop:** changes nothing for processes on its machine (rule 1).
- **Two servers on one repo:** refused (`server.lock`). A taken port is an error, never a silent move,
  because clients' `remote` names it; only the UI takes a free port.

## Access

- On the repo's machine: file permissions (the repo is private to its owner, mode 700).
- Over HTTP: the owner token or project-scoped tokens (`cairn token`), never in a URL. The UI logs in with
  a one-time link (a random code valid once for 10 minutes).

## Configuration

```toml
[cairn]
dir = ".cairn"                        # the repo (default; or CAIRN_DIR)

[repo]
remote = "https://cairn.example.org/my-study"  # optional: the source of truth (<server>/<project>)
token_file = "~/.config/cairn/token"  # or CAIRN_TOKEN
allow_http = false                    # plain HTTP to a non-local remote
interval = 30                         # seconds between live syncs of a running version
heartbeat_timeout = 600
allow_network_fs = false
exclude = ["*.pkl"]
```

There is no migration and no compatibility code: Cairn was not in use before this design, so 0.14 starts
with fresh repos (`[store]` is an unknown key like any other).

## Commands

- `cairn server [repo]` serves a repo (default: the project's) to other machines; `cairn ui` explores the
  project's repo (or `--remote URL`: a server lists its projects, `<server>/<project>` opens one); both print
  a one-time login link.

## Errors

Cairn's own errors (an unknown artifact, a refused alias, a remote that does not answer, ...) are raised
from the public function or method the caller called, so the traceback ends at the caller's line and that
one call, followed by a message that names what was looked for and where (the repo folder or remote).
Unexpected failures keep their full traceback.
- `cairn sync [--watch]`, `cairn status`, `cairn prune`, `cairn verify` work on the repo and its remote.
