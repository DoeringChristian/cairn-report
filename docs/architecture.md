# Cairn architecture: a repo, an optional remote, automatic sync

Decided 2026-10-15 with the owner; projects, short ids and errors added 2026-10-16; simplified for its one user the
same day (settings in `.cairn/config.toml` and `cairn remote`, one token, logins without timeouts, plain HTTP with a
warning). The version identity and the reference syntax were made foundational on 2026-10-16 (0.15.0: one id per
version, git-like, and one parser). Fixed: later work fits this design; it does not change it.

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
   one, the repo. A version's **id** is its identity everywhere; `vN` is the source of truth's name for it.

## Vocabulary

| term | meaning |
|---|---|
| repo | the folder `.cairn/` (or `CAIRN_DIR`): settings, metadata, blobs, outbox, drafts, report builds |
| project root | the nearest folder upward that holds `.cairn/repo.json`, as git finds `.git` |
| remote | a project on a `cairn server`: `[remote] url = "https://host/<project>"` (`cairn remote set`), like a git remote |
| project | what a repo holds: locally exactly one (unnamed), on a server many, each at `<server>/<project>` |
| source of truth | the remote if configured, else the repo |
| outbox | `<repo>/outbox/<id>/`: one version on its way to the remote (manifest + state); one writer |
| synced | the remote has the version and assigned its number |

## Layout

```text
.cairn/                      the repo (self-ignoring: .gitignore = "*")
├── repo.json                id of this repo, layout version
├── config.toml              the project's settings (see Configuration)
├── metadata.sqlite3         versions, aliases, lineage (WAL)   — not on a network filesystem
├── blobs/<aa>/<sha256>      content-addressed files, written once (temp + rename), shared by all versions
├── outbox/<id>/            versions not yet on the remote (manifest, state); only with a remote
├── drafts/                  folders that become versions (runs, `cairn.draft`)
├── build/  share/  reviews/ report builds, shared pages, review state
├── token                    the one token: `cairn server`'s, or a client's for the remote
├── ui-token                 the login of the project's own `cairn ui` when there is no `token`
├── heartbeats/<id>         running versions' heartbeats
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
  creates it.
- **There is no cross-project access.** A reference has no project prefix, a run's inputs and a report's
  inputs come from its own project, and the public functions take no `store=` pointing elsewhere. A dataset
  several projects need is published in each of them; the server stores its files once (content-addressed
  blobs), and so does a repo for a file in several versions.

## Identity and references

- **A version's id is its only identity**, as a commit's hash is in git: 64 lowercase hex digits, random
  (256 bits from the operating system's generator), made by whoever creates the version (a script offline, a
  CLI command, the server), never changed and never reused. The database, manifests, inputs and lineage, the
  outbox and read-only folders (`outbox/<id>/`, `versions/<id>/`), heartbeats, the HTTP API, the UI's URLs and
  reports all name a version by its id. There is no second identifier.
- **A short id is a prefix of the id.** Cairn shows the shortest prefix of at least 7 digits that is unique in
  the project; any unique prefix of 4 or more digits names the version; a prefix that matches several versions
  is an error that lists them. The full id is shown by `cairn show` and on the version's page.
- **Order never comes from ids.** Listings, `@latest`, and the order in which the outbox is sent come from the
  version number and `created` (and a run's outputs follow their run), never from the id's digits.
- **A reference has exactly one of four forms**, told apart by its shape alone, before anything is looked up:

  | form | meaning |
  |---|---|
  | `<id>` | one version: 4 to 64 hex digits (a prefix of its id) |
  | `<name>` | the artifact's newest completed version (`@latest`) |
  | `<name>@v<n>` | version n of the artifact |
  | `<name>@<word>` | the version an alias (`best`, `latest`) or a label (a run's timestamp) names |

  A file inside a version follows after `//` (`descent@v3//figures/a.png`, `3f9a2c1//figures/a.png`).
- **The forms cannot overlap:** an id has no `@`; `v<n>` is never a word, nor is anything else of a version's shape (`V3`, `v-1`, `v01`, `v2-final`: `v` or `V`, then a sign or a digit), and a Cairn 0.14 uid (a UUID) is neither a name nor a word; an artifact name, an alias and a
  label are never 4 or more hex digits alone (`cafe`, `1234` and `deadbeef` are refused when a name, alias or
  label is created), so a bare word is an id or a name by its shape, and a word after `@` is a number or a
  word. An alias and a label cannot share a word (creating the second is refused).
- **One parser** (`store/refs.py`) reads every reference, wherever it comes from: the CLI, the Python API,
  report `inputs:`, `.cairn-inputs.jsonl`, the server, and the UI's URLs (`/p/<project>/a/<name>@v3`,
  `/p/<project>/v/<id>`). Nothing rewrites a reference before it; no layer has a shortcut of its own.

## Invariants (each with its reason; each has a test of its own, with constructed ids)

1. **Ids never collide.** Each is 256 random bits made where the version is made; two machines would have to
   draw the same number (probability about 2^-256). No coordination is needed, offline or online.
2. **Only the source of truth assigns numbers.** With a remote, its server numbers versions in the order they
   arrive; a repo without a remote numbers its own. So two machines publishing at once get consecutive numbers,
   and a number never names two versions.
3. **`@latest` is the highest completed number:** on a remote, the version that arrived last, not the one whose
   creating machine's clock says it is newest. It needs no clock, and a report records exactly what it resolved.
4. **Stored references are full ids, never short ids or numbers alone:** inputs, `.cairn-inputs.jsonl`, report
   manifests, lineage, aliases. A short id is only for typing and showing; it may become ambiguous as a project
   grows or on a remote that combines machines, and then it is refused with the candidates, never resolved wrong.
5. **The four reference forms are disjoint by their characters** (the table above); nothing resolves by
   precedence or by trying one reading after another.
6. **One parser.** Only `store/refs.py` (and one shared module in the web code) takes references apart; a
   boundary test fails on any other selector parsing.
7. **Aliases and deletions change only on the source of truth;** offline they are refused, so two machines
   cannot set an alias differently.

**Before any change to identity, numbering or the reference syntax:** a table of the forms with their character
sets showing they stay disjoint, a note on how git handles the same thing, and a test for every invariant it
touches. A rule of the form "X is matched first" is a failed review.

## Upgrading a 0.14 repo

`cairn upgrade [repo]` converts a repo made by 0.14 (a project's `.cairn/`, or a server's repo folder) in place,
once: every version gets its id, the SHA-256 of its 0.14 uid (so a server and each of its clients, upgraded one
by one, agree on every id, and every short id 0.14 printed still names its version), and every place that named its uid (inputs, lineage, aliases, outbox entries,
read-only folders, heartbeats) is rewritten to it; numbers, labels, aliases, files and blobs stay. It refuses to
run while another process uses the repo (a running `cairn server`, an open run), writes a backup of the
database first (`metadata.sqlite3.0.14.bak`), works in one transaction, and checks the result with
`cairn verify` before it reports success. Every other command refuses a 0.14 repo with a message naming
`cairn upgrade`. A server and its clients upgrade together (a 0.15 client refuses a 0.14 server and the other
way round, with a message saying so). This is the one piece of compatibility code, kept until 0.16.

## Versions and numbers

- **Without a remote** the repo assigns `vN` when a version starts, as a store does today.
- **With a remote** the remote assigns `vN`. A version made while the remote answers gets its number at
  once (the version is started on the remote too). A version made offline is created in the repo with its
  id and no number ("pending"); it is written to the outbox, and when the remote takes it, the repo records
  the remote's number. References by id, by `@latest`, and the version objects work before that.
- **Versions from the remote** that a process uses (`cairn.use`, `pull`, a report input) are copied into
  the repo with the remote's number and id: their blobs into `blobs/`, their metadata into the database.
  The repo is the cache; there is no separate cache folder. `cairn prune` may drop copies of versions the
  remote has; it never drops a version that is not synced.
- **`@latest` in the repo** is the newest completed version the repo knows: synced, pending, or copied.
  On the remote it is the remote's latest. A report records the id and number it resolved.
- **Aliases and deletions with a remote** need the remote (they change the source of truth): set or
  removed there, then mirrored into the repo. Offline they fail with a clear message.
- **Adding a remote to a repo** that already has numbered versions: the first sync sends them, oldest
  first; the remote assigns its numbers and the repo takes them over (ids unchanged, so lineage and
  reports, which record ids, stay valid).

## Sync

- **When:** at the start and end of every Cairn command and process that touches the repo, while a run
  runs (its live version, every `[repo] interval` seconds), and with `cairn sync` (`--watch` keeps going,
  for a login node). Each attempt is quick to fail when the remote does not answer; nothing blocks a run.
- **What:** each outbox entry (a version, and for a running version its newest state): missing blobs first,
  then the manifest; the remote's answer (number) is written to the repo, and the outbox entry is removed
  only after that. Idempotent by id: an interrupted or repeated sync never makes a second version.
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
- **Two servers on one repo:** refused (`server.lock`). A taken port moves the server to the next free one; its startup output
  shows the port, and clients name it in their remote (`http://fermat:8805/<project>`).

## Access

- On the repo's machine: file permissions (the repo is private to its owner, mode 700).
- Over HTTP: one token, the owner's, in the server's `<repo>/token` (made on the first launch), never in a URL.
  It reaches every project. A client takes the first of `$CAIRN_TOKEN`, `<repo>/token` (the project's
  `.cairn/token`), `~/.config/cairn/token`; a refusal names where the token came from, and no token lists the
  three places. The token is never in the config.
- Plain HTTP to a non-local host works; each process warns once on stderr that the token is sent unencrypted
  (fine over a VPN such as Tailscale, or an SSH tunnel). For anything else, an HTTPS reverse proxy.
- The UI logs in with a one-time link (a random code that works once and does not expire; every launch prints
  a new one) or the token on its login page. The browser's cookie holds only the session id, an HMAC of the
  token with a fixed label (HttpOnly, SameSite=Strict, Secure behind HTTPS, Max-Age ten years): a session
  survives restarts of the UI and ends exactly when the token changes; logging out clears the cookie.

## SSH access (planned for the release after 0.14.0)

As on GitHub: SSH carries sync, the CLI and the Python API, with the SSH key as the credential (no token);
a browser talking to a server's UI stays on HTTP(S) with a login.

- **Remote:** `ssh://[user@]host[:port]/<project>` (or scp-like `[user@]host:<project>`). The URL names
  only the project; which repo folder serves it is the server's business, fixed by the key's command.
- **Keys:** on the server's account, `cairn key add <name> <public key>` adds the key to that account's
  `~/.ssh/authorized_keys`, inside a block Cairn marks and owns, as
  `command="<absolute path of this cairn> ssh-serve <repo> --key <name>",restrict <key>`. `cairn key list`
  and `cairn key remove` manage it; lines outside the block are never touched. A key is the owner's
  credential, as the token is: it reaches every project and creates one on first sync.
- **A forced command applies to its key only**, so the server's account is either dedicated (like GitHub's
  `git` user) or the key is one made for Cairn; a key used for a normal shell login stays unregistered.
- **The connection:** the client runs `ssh -T` with that key (batch mode, no password prompts) and speaks
  the same HTTP API over it; `ssh-serve` opens the repo directly (rule 1), answers, and ends with the connection. No `cairn server` needs to run; one may run beside it.
- **`cairn ui --remote ssh://...`** runs the UI on the client and reads through SSH, with no token.

## Configuration

Every setting lives in the repo's `config.toml` (`.cairn/config.toml`; with `CAIRN_DIR`, that folder's). All keys
are optional; paths are relative to the project root, the folder containing `.cairn/`:

```toml
[remote]
url = "https://cairn.example.org/my-study"   # the source of truth (<server>/<project>); `cairn remote set|remove`

[repo]
exclude = ["*.pkl"]                   # files never published (gitignore-style)
interval = 30                         # seconds between live syncs of a running version
heartbeat_timeout = 600
allow_network_fs = false

[reports]
dir = "reports"
output_dir = ".cairn/build"           # default: <repo>/build

[style]
css = ["reports/style.css"]
template_dir = "reports/template"
```

`cairn init` writes it as a commented template. There is no other config file, no `[cairn] dir` (the repo is
`.cairn/` or `CAIRN_DIR`), and no token in it. There is no migration and no compatibility code: Cairn was not
in use before this design, so 0.14 starts with fresh repos (unknown keys are errors).

## Commands

- `cairn init [path]` makes `.cairn/` (with its config template); other writing commands make it in the working
  directory when no repo is found, and read-only ones refuse.
- `cairn remote` prints the remote (or "no remote"); `cairn remote set <url>` checks and writes it (the next
  command syncs, and versions the repo numbered itself are adopted); `cairn remote remove` drops it (versions
  keep their numbers; pending ones stay pending).
- `cairn sync [--watch]`, `cairn status`, `cairn prune`, `cairn verify` work on the repo and its remote.
- `cairn server [repo]` serves a repo (default: the project's) to other machines; `cairn ui` explores the
  project's repo (or `--remote URL`: a server lists its projects, `<server>/<project>` opens one); both print
  a one-time login link.

## Errors

Cairn's own errors (an unknown artifact, a refused alias, a remote that does not answer, ...) are raised
from the public function or method the caller called, so the traceback ends at the caller's line and that
one call, followed by a message that names what was looked for and where (the repo folder or remote).
Unexpected failures keep their full traceback.
