"""Upgrading a repo made by Cairn 0.14 (`cairn upgrade [repo]`): the one piece of compatibility code, kept until 0.16.

A 0.14 repo named every version by a uid (a UUIDv7) and kept a short id beside it, the first digits of the SHA-256 of
that uid. 0.15 names a version by its id alone. The upgrade gives every version of 0.14 the id `legacy_id(uid)`: the
SHA-256 of its uid, so

- a server and each of its clients, upgraded one by one, give every version the same id (a client's copies, its outbox
  entries, and its read-only folders keep matching the server's versions), and
- every short id 0.14 showed is still a prefix of the version's id, so it names the same version.

A repo (a project's `.cairn/` or a server's repo folder with many projects) is converted in place, in these steps, each
safe to run again when an earlier attempt stopped half way (`upgrade` picks up where it ended):

1. refuse while it is in use: a `cairn server` holds `server.lock`, a running version has a live heartbeat, a draft is
   open in a live process (`in_use`);
2. back up the database to `metadata.sqlite3.0.14.bak` (SQLite's backup API, so the WAL comes along);
3. one transaction: every table that named a version (versions, aliases and their log, consumers, the remote's
   binding) is rewritten by id, every manifest (inputs, `metadata.sweep`, the copies' `origin`) too; the schema
   becomes 2. A copy of the remote's version whose inputs this repo cannot tell (they were never copied here) is
   dropped: it is a cache, fetched again from the remote when it is used;
4. the folders named by uid are renamed to the id: `outbox/<uid>/` (with its manifest), `versions/<uid>/`,
   `heartbeats/<uid>`, and the drafts in `drafts/` (`<uid>/`, `<uid>.draft.json`);
5. `repo.json` gets its id and layout 2: from now on the repo is a 0.15 one;
6. `cairn verify` checks the result.

Numbers, labels, aliases, files, and blobs stay as they were.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import time
from pathlib import Path

from .errors import StoreError

UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")  # 0.14's version and repo uids
NUMBERED = re.compile(r"(.+)@v(0|[1-9][0-9]*)")  # 0.14's exact input references, `name@v<n>`
BY_UID = re.compile(r"(.+)@(" + UID.pattern + r")")  # and `name@<uid>`
BACKUP = "metadata.sqlite3.0.14.bak"
OLD_LAYOUT, LAYOUT = 1, 2
HEARTBEAT_TIMEOUT = 600.0


def legacy_id(uid: str) -> str:
    """The id a version (or a repo) of Cairn 0.14 gets: the SHA-256 of its uid, so a server and its clients, each
    upgraded on its own, give a version the same id, and the short ids 0.14 showed (prefixes of that hash) still name
    it."""
    return hashlib.sha256(uid.encode()).hexdigest()


def layout(root: Path | str) -> int | None:
    """The layout of the repo at `root` (1: Cairn 0.14, 2: 0.15), None without a readable `repo.json`."""
    try:
        record = json.loads((Path(root) / "repo.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if "uid" in record or record.get("layout") in (None, OLD_LAYOUT):
        return OLD_LAYOUT
    return record.get("layout")


# ------------------------------------------------------------------ is anything using the repo?


def _alive(beat: dict, timeout: float) -> bool:
    """Whether a heartbeat {time, host, pid} may still come from a live process."""
    try:
        stamp = float(beat.get("time", 0))
    except (TypeError, ValueError):
        return False
    if beat.get("host") == socket.gethostname() and isinstance(beat.get("pid"), int):
        from .folders import pid_alive

        return pid_alive(beat["pid"]) and time.time() - stamp <= timeout
    return time.time() - stamp <= timeout


def _read(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def in_use(root: Path, timeout: float = HEARTBEAT_TIMEOUT) -> list[str]:
    """Why the repo is in use now (empty: it is not): a server holds its lock, a running version has a live heartbeat
    (in `heartbeats/` or a running outbox entry), a draft is open in a live process."""
    from .db import ServerLock
    from .folders import pid_alive

    root = Path(root)
    found = []
    if ServerLock.held(root):
        found.append(f"a `cairn server` serves it (it holds {root / 'server.lock'}): stop the server first")
    for path in sorted((root / "heartbeats").glob("*")) if (root / "heartbeats").is_dir() else []:
        beat = _read(path)
        if not path.name.startswith(".") and isinstance(beat, dict) and _alive(beat, timeout):
            found.append(f"a running version has a live heartbeat ({path}): let it end, or stop its process")
    for folder in sorted((root / "outbox").iterdir()) if (root / "outbox").is_dir() else []:
        record = _read(folder / "manifest.json")
        if isinstance(record, dict) and record.get("sync") == "running":
            beat = _read(folder / "heartbeat") or {"time": 0}
            if _alive({**(record.get("owner") or {}), **beat}, timeout):
                found.append(f"a running version is being synced ({folder}): let it end, or stop its process")
    for record_file in _draft_records(root):
        record = _read(record_file)
        if not isinstance(record, dict) or record.get("state", "open") != "open":
            continue
        owner = record.get("owner") or {}
        mine = owner.get("host") == socket.gethostname()
        if not mine or (isinstance(owner.get("pid"), int) and pid_alive(owner["pid"])):
            where = "on this host" if mine else f"on {owner.get('host')}"
            found.append(
                f"the draft {record_file.parent / record_file.name[: -len('.draft.json')]} is open (its process "
                f"{owner.get('pid')} {where} may write to it): let it end, or discard it with Cairn 0.14"
            )
    return found


def _draft_records(root: Path) -> list[Path]:
    """The `<uid>.draft.json` records under `<root>/drafts` (a draft's own folder is never entered)."""
    found = []
    base = Path(root) / "drafts"
    if not base.is_dir():
        return found
    for directory, folders, files in os.walk(base):
        keep = []
        for name in folders:
            if UID.fullmatch(name) and f"{name}.draft.json" in files:
                found.append(Path(directory) / f"{name}.draft.json")
            else:
                keep.append(name)
        folders[:] = keep
    return found


# ------------------------------------------------------------------ the database


def _backup(root: Path) -> Path | None:
    """Copy the database (with its WAL: SQLite's backup API) to BACKUP, once: a backup that exists is the 0.14 one."""
    database, target = root / "metadata.sqlite3", root / BACKUP
    if not database.is_file():
        return None
    if target.exists():
        return target
    temporary = target.with_name(target.name + ".tmp")
    temporary.unlink(missing_ok=True)
    source = sqlite3.connect(database, timeout=60)
    try:
        copy = sqlite3.connect(temporary)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    os.replace(temporary, target)
    return target


class _Mapping:
    """Version ids by 0.14 identity: (project, name, number) and uid (always `legacy_id(uid)`)."""

    def __init__(self, rows):
        self.by_number = {(project, name, number): legacy_id(uid) for project, name, number, uid, *_ in rows}

    def input_id(self, project: str, text) -> str | None:
        """The id a 0.14 input reference names (`name@v<n>` of the project, `name@<uid>`), None if it is not known."""
        if not isinstance(text, str):
            return None
        match = BY_UID.fullmatch(text)
        if match:
            return legacy_id(match.group(2))
        match = NUMBERED.fullmatch(text)
        if match:
            return self.by_number.get((project, match.group(1), int(match.group(2))))
        if UID.fullmatch(text):
            return legacy_id(text)
        return None


def _sweep(metadata):
    """`metadata.sweep` of a Hydra trial (0.14: {uid, ref: `name@<uid>`, ...}) as 0.15 writes it ({id, ref})."""
    if not isinstance(metadata, dict) or not isinstance(metadata.get("sweep"), dict):
        return metadata
    sweep = dict(metadata["sweep"])
    if isinstance(sweep.get("uid"), str) and UID.fullmatch(sweep["uid"]):
        sweep["id"] = legacy_id(sweep.pop("uid"))
    if isinstance(sweep.get("ref"), str) and BY_UID.fullmatch(sweep["ref"]):
        sweep["ref"] = legacy_id(BY_UID.fullmatch(sweep["ref"]).group(2))
    if isinstance(sweep.get("trials"), list):
        sweep["trials"] = [_trial_row(row) for row in sweep["trials"]]
    return {**metadata, "sweep": sweep}


def _trial_row(row):
    """A trial row of a sweep (`trials.jsonl`, `metadata.sweep.trials`): its uid becomes the id."""
    if not isinstance(row, dict):
        return row
    row = dict(row)
    if isinstance(row.get("uid"), str) and UID.fullmatch(row["uid"]):
        row["id"] = legacy_id(row.pop("uid"))
    if isinstance(row.get("ref"), str) and BY_UID.fullmatch(row["ref"]):
        row["ref"] = legacy_id(BY_UID.fullmatch(row["ref"]).group(2))
    return row


def _origin(origin):
    """A copy's `origin`, `<server uid>/<project>`, as `<server id>/<project>`."""
    if isinstance(origin, str):
        head, slash, project = origin.partition("/")
        if UID.fullmatch(head):
            return legacy_id(head) + slash + project
    return origin


def convert_manifest(manifest: dict, project: str, mapping: _Mapping) -> tuple[dict, list[str]]:
    """A 0.14 version manifest as 0.15 stores it, and the inputs it could not convert (references to versions this
    repo does not know)."""
    record = {key: value for key, value in manifest.items() if key not in ("uid", "short_id")}
    record = {"id": legacy_id(manifest["uid"]), **record} if manifest.get("uid") else record
    inputs, unknown = [], []
    for text in manifest.get("inputs") or []:
        id = mapping.input_id(project, text)
        if id is None:
            unknown.append(str(text))
        elif id not in inputs:
            inputs.append(id)
    record["inputs"] = inputs
    if "metadata" in record:
        record["metadata"] = _sweep(record["metadata"])
    if "origin" in record:
        record["origin"] = _origin(record["origin"])
    return record, unknown


NEW_TABLES = """
CREATE TABLE versions (
    project TEXT NOT NULL, name TEXT NOT NULL, number INTEGER NOT NULL, id TEXT NOT NULL, label TEXT,
    status TEXT NOT NULL, manifest TEXT NOT NULL, PRIMARY KEY (project, name, number)) WITHOUT ROWID;
CREATE UNIQUE INDEX versions_by_id ON versions (id);
CREATE INDEX versions_by_project_id ON versions (project, id);
CREATE INDEX versions_by_label ON versions (project, name, label, status);
CREATE TABLE aliases (
    project TEXT NOT NULL, name TEXT NOT NULL, alias TEXT NOT NULL, id TEXT NOT NULL, position INTEGER NOT NULL,
    PRIMARY KEY (project, name, alias)) WITHOUT ROWID;
CREATE INDEX aliases_by_id ON aliases (id);
CREATE TABLE alias_log (
    seq INTEGER PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL, alias TEXT NOT NULL, id TEXT NOT NULL,
    time TEXT NOT NULL);
CREATE INDEX alias_log_by_artifact ON alias_log (project, name, seq);
CREATE TABLE consumers (
    seq INTEGER PRIMARY KEY, project TEXT NOT NULL, input TEXT NOT NULL, consumer TEXT NOT NULL, time TEXT NOT NULL,
    UNIQUE (project, input, consumer));
CREATE INDEX consumers_by_consumer ON consumers (project, consumer);
"""


def _convert_database(root: Path, counts: dict, notes: list[str]) -> _Mapping | None:
    """Step 3 (see the module doc): one transaction. Returns the mapping of the versions (None without a database)."""
    database = root / "metadata.sqlite3"
    if not database.is_file():
        return None
    connection = sqlite3.connect(database, timeout=60, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            schema = connection.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if schema is not None and schema[0] == str(LAYOUT):  # converted by an earlier attempt
                rows = connection.execute("SELECT project, name, number, id, manifest FROM versions").fetchall()
                connection.execute("ROLLBACK")
                mapping = _Mapping([])
                mapping.by_number = {(project, name, number): id for project, name, number, id, _ in rows}
                return mapping
            rows = connection.execute(
                "SELECT project, name, number, uid, label, status, manifest FROM versions"
            ).fetchall()
            mapping = _Mapping(rows)
            converted, dropped = [], set()
            for project, name, number, uid, label, status, text in rows:
                manifest = json.loads(text)
                manifest.setdefault("uid", uid)
                record, unknown = convert_manifest(manifest, project, mapping)
                if unknown and record.get("origin"):  # a copy: the remote has it; it is fetched again when used
                    dropped.add(legacy_id(uid))
                    notes.append(
                        f"dropped the copy of {name}@v{number}{_in(project)}: its inputs {', '.join(unknown)} were never "
                        "copied here (the remote has it; it is copied again when it is used)"
                    )
                    continue
                if unknown:
                    raise StoreError(
                        f"{name}@v{number}{_in(project)} names inputs this repo does not have ({', '.join(unknown)}): "
                        f"nothing was changed; the backup is {root / BACKUP}"
                    )
                converted.append((project, name, number, record["id"], label, status, json.dumps(record)))
            aliases = connection.execute("SELECT project, name, alias, number, position FROM aliases").fetchall()
            log = connection.execute("SELECT seq, project, name, alias, number, time FROM alias_log").fetchall()
            consumers = connection.execute("SELECT seq, project, input, consumer, time FROM consumers").fetchall()
            for table in ("versions", "aliases", "alias_log", "consumers"):
                connection.execute(f"DROP TABLE {table}")
            for statement in NEW_TABLES.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.executemany(
                "INSERT INTO versions (project, name, number, id, label, status, manifest) VALUES (?, ?, ?, ?, ?, ?, ?)",
                converted,
            )
            kept = {(row[0], row[1], row[2]): row[3] for row in converted}
            for project, name, alias, number, position in aliases:
                id = kept.get((project, name, number))
                if id is None:
                    notes.append(f"dropped the alias {name}@{alias}{_in(project)}: its version v{number} is not here")
                    continue
                connection.execute("INSERT INTO aliases VALUES (?, ?, ?, ?, ?)", (project, name, alias, id, position))
                counts["aliases"] += 1
            for seq, project, name, alias, number, stamp in log:
                id = "" if not number else mapping.by_number.get((project, name, number), "")
                connection.execute(
                    "INSERT INTO alias_log VALUES (?, ?, ?, ?, ?, ?)", (seq, project, name, alias, id, stamp)
                )
            known = set(kept.values())
            for seq, project, input_ref, consumer_ref, stamp in consumers:
                input_id, consumer_id = mapping.input_id(project, input_ref), mapping.input_id(project, consumer_ref)
                if input_id is None or consumer_id not in known:
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO consumers VALUES (?, ?, ?, ?, ?)",
                    (seq, project, input_id, consumer_id, stamp),
                )
            for project, name, number, id, label, status, text in converted:  # every edge the inputs record
                for input_id in json.loads(text).get("inputs") or []:
                    connection.execute(
                        "INSERT OR IGNORE INTO consumers (project, input, consumer, time) VALUES (?, ?, ?, ?)",
                        (project, input_id, id, json.loads(text).get("completed") or ""),
                    )
            counts["consumers"] = connection.execute("SELECT count(*) FROM consumers").fetchone()[0]
            _summaries(connection)
            remote = connection.execute("SELECT value FROM meta WHERE key = 'remote'").fetchone()
            if remote is not None:
                connection.execute("UPDATE meta SET value = ? WHERE key = 'remote'", (_origin(remote[0]),))
            connection.execute(
                "INSERT INTO meta VALUES ('schema', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(LAYOUT),),
            )
            connection.execute("UPDATE projects SET generation = generation + 1")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        counts["versions"] = len(converted)
        counts["projects"] = len({row[0] for row in converted})
        counts["dropped copies"] = len(dropped)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    mapping.by_number = {(row[0], row[1], row[2]): row[3] for row in converted}
    return mapping


def _in(project: str) -> str:
    return f" (project {project})" if project else ""


def _summaries(connection) -> None:
    """Every artifact's summary row from the converted tables: latest, its metadata, the aliases by number, the count."""
    for project, name in connection.execute("SELECT project, name FROM artifacts").fetchall():
        aliases = {
            alias: number
            for alias, number in connection.execute(
                "SELECT a.alias, v.number FROM aliases a JOIN versions v ON v.id = a.id "
                "WHERE a.project = ? AND a.name = ? ORDER BY a.position",
                (project, name),
            )
        }
        head = aliases.get("latest")
        metadata = {}
        if head is not None:
            row = connection.execute(
                "SELECT manifest FROM versions WHERE project = ? AND name = ? AND number = ?", (project, name, head)
            ).fetchone()
            metadata = json.loads(row[0]).get("metadata") or {} if row else {}
        count = connection.execute(
            "SELECT count(*) FROM versions WHERE project = ? AND name = ? AND status != 'deleted'", (project, name)
        ).fetchone()[0]
        connection.execute(
            "UPDATE artifacts SET latest = ?, metadata = ?, aliases = ?, versions = ? WHERE project = ? AND name = ?",
            (head, json.dumps(metadata), json.dumps(aliases), count, project, name),
        )


# ------------------------------------------------------------------ the folders


def _write(path: Path, value) -> None:
    temporary = path.with_name(f".{path.name}.upgrade.tmp")
    temporary.write_text(json.dumps(value, indent=1))
    os.replace(temporary, path)


def _outbox(root: Path, mapping: _Mapping | None, counts: dict, notes: list[str]) -> None:
    """`outbox/<uid>/` -> `outbox/<id>/`, its manifest by id (inputs `name@<uid>` -> the id; `name@v<n>` of a copy here
    -> its id; one this repo cannot tell stays a reference, which the remote resolves when it gets the entry)."""
    base = root / "outbox"
    if not base.is_dir():
        return
    for folder in sorted(base.iterdir()):
        if not UID.fullmatch(folder.name):
            continue
        record = _read(folder / "manifest.json")
        target = base / legacy_id(folder.name)
        if not isinstance(record, dict):
            notes.append(f"left {folder} alone: its manifest is not readable")
            continue
        if record.get("uid"):
            inputs = []
            for text in record.get("inputs") or []:
                id = mapping.input_id("", text) if mapping else None
                if id is None and isinstance(text, str) and BY_UID.fullmatch(text):
                    id = legacy_id(BY_UID.fullmatch(text).group(2))
                if id is None and isinstance(text, str):
                    id = _folder_id(root, text)
                inputs.append(id or text)
            record = {"id": legacy_id(record.pop("uid")), **record, "inputs": list(dict.fromkeys(inputs))}
            if isinstance(record.get("store"), str) and UID.fullmatch(record["store"]):
                record["store"] = legacy_id(record["store"])
            record["metadata"] = _sweep(record.get("metadata") or {})
            for claim in folder.glob(".flushing-*"):
                claim.unlink(missing_ok=True)
            _write(folder / "manifest.json", record)
        os.rename(folder, target)
        counts["outbox entries"] += 1


def _folder_id(root: Path, text: str) -> str | None:
    """The id of `name@v<n>` from a read-only folder a pull made (its record has the name, the number, and the uid)."""
    match = NUMBERED.fullmatch(text)
    base = root / "versions"
    if not match or not base.is_dir():
        return None
    for folder in base.iterdir():
        record = _read(folder / "manifest.json")
        if isinstance(record, dict) and record.get("name") == match.group(1):
            if record.get("version") == int(match.group(2)):
                uid = record.get("uid") or ""
                return legacy_id(uid) if UID.fullmatch(uid) else record.get("id")
    return None


def _versions(root: Path, mapping: _Mapping | None, counts: dict) -> None:
    """`versions/<uid>/` -> `versions/<id>/` with its record by id; a folder whose manifest names inputs this repo
    cannot tell is removed (it is made again by the next pull)."""
    from .folders import remove

    base = root / "versions"
    if not base.is_dir():
        return
    for folder in sorted(base.iterdir()):
        if not UID.fullmatch(folder.name):
            continue
        record = _read(folder / "manifest.json")
        if not isinstance(record, dict) or not isinstance(record.get("manifest"), dict):
            remove(folder)
            continue
        manifest, unknown = convert_manifest(record["manifest"], "", mapping or _Mapping([]))
        if unknown:
            remove(folder)
            counts["read-only folders removed"] += 1
            continue
        repo = record.get("repo")
        converted = {
            "id": legacy_id(folder.name),
            **{key: value for key, value in record.items() if key not in ("uid", "manifest")},
            "repo": legacy_id(repo) if isinstance(repo, str) and UID.fullmatch(repo) else repo,
            "manifest": manifest,
        }
        _write(folder / "manifest.json", converted)
        os.rename(folder, base / legacy_id(folder.name))
        counts["read-only folders"] += 1


def _heartbeats(root: Path, counts: dict) -> None:
    base = root / "heartbeats"
    for path in sorted(base.iterdir()) if base.is_dir() else []:
        if UID.fullmatch(path.name):
            os.rename(path, base / legacy_id(path.name))
            counts["heartbeats"] += 1


def _drafts(root: Path, counts: dict) -> None:
    """Every draft: `<uid>/` and `<uid>.draft.json` -> the id; its record's uid, run, running version, and inputs by
    id; inside it, a Hydra job's `run.json` (`run`) and a sweep's `trials.jsonl` too."""
    for record_file in _draft_records(root):
        record = _read(record_file)
        uid = record_file.name[: -len(".draft.json")]
        folder = record_file.parent / uid
        if not isinstance(record, dict):
            continue
        record = {"id": legacy_id(uid), **{key: value for key, value in record.items() if key != "uid"}}
        run = record.get("run")
        if isinstance(run, str) and BY_UID.fullmatch(run):
            record["run"] = legacy_id(BY_UID.fullmatch(run).group(2))
        if isinstance(record.get("running"), dict) and record["running"].get("uid"):
            running = dict(record["running"])
            running["id"] = legacy_id(running.pop("uid"))
            record["running"] = running
        record["inputs"] = [
            legacy_id(BY_UID.fullmatch(text).group(2)) if isinstance(text, str) and BY_UID.fullmatch(text) else text
            for text in record.get("inputs") or []
        ] or record.get("inputs")
        if record.get("inputs") is None:
            record.pop("inputs")
        record["metadata"] = _sweep(record.get("metadata"))
        for path in folder.rglob("run.json") if folder.is_dir() else []:
            status = _read(path)
            if isinstance(status, dict) and isinstance(status.get("run"), str) and BY_UID.fullmatch(status["run"]):
                _write(path, {**status, "run": legacy_id(BY_UID.fullmatch(status["run"]).group(2))})
        for path in folder.rglob("trials.jsonl") if folder.is_dir() else []:
            rows = []
            for line in path.read_text().splitlines():
                try:
                    rows.append(json.dumps(_trial_row(json.loads(line))))
                except ValueError:
                    rows.append(line)
            path.write_text("".join(row + "\n" for row in rows))
        target = record_file.parent / legacy_id(uid)
        if folder.is_dir():
            os.rename(folder, target)
        _write(record_file, record)
        os.rename(record_file, record_file.with_name(f"{legacy_id(uid)}.draft.json"))
        counts["drafts"] += 1


def _repo_file(root: Path) -> str:
    """Step 5: `repo.json` with the repo's id and layout 2 (its remote's id too); returns the id."""
    path = root / "repo.json"
    record = _read(path) or {}
    uid = record.pop("uid", None)
    id = record.get("id") or (legacy_id(uid) if uid else None)
    if id is None:
        raise StoreError(f"{path} names no repo")
    remote = record.get("remote")
    if isinstance(remote, dict) and remote.get("uid"):
        remote = {**{key: value for key, value in remote.items() if key != "uid"}, "id": legacy_id(remote["uid"])}
    record = {"id": id, **record, "layout": LAYOUT, "upgraded": {"from": "0.14", "time": time.time()}}
    if remote is not None:
        record["remote"] = remote
    _write(path, record)
    return id


def upgrade(root: Path | str, timeout: float = HEARTBEAT_TIMEOUT) -> dict:
    """Convert the 0.14 repo at `root` in place (see the module doc). Returns {counts, notes, backup, report} (report:
    the verify.Report). StoreError when it is not a 0.14 repo, is in use, or the result does not verify."""
    root = Path(root)
    found = layout(root)
    if found is None:
        raise StoreError(f"{root} is not a repo (no readable repo.json)")
    if found == LAYOUT:
        raise StoreError(f"{root} is a Cairn 0.15 repo already (layout {LAYOUT}): nothing to upgrade")
    if found != OLD_LAYOUT:
        raise StoreError(f"{root} has layout {found!r}, which this Cairn cannot upgrade")
    busy = in_use(root, timeout)
    if busy:
        raise StoreError(f"{root} is in use, so it was not upgraded: " + "; ".join(busy))
    counts = {
        key: 0
        for key in (
            "projects",
            "versions",
            "aliases",
            "consumers",
            "outbox entries",
            "read-only folders",
            "read-only folders removed",
            "heartbeats",
            "drafts",
            "dropped copies",
        )
    }
    notes: list[str] = []
    backup = _backup(root)
    mapping = _convert_database(root, counts, notes)
    _outbox(root, mapping, counts, notes)
    _versions(root, mapping, counts)
    _heartbeats(root, counts)
    _drafts(root, counts)
    _repo_file(root)
    from .local import LocalRepo
    from .outbox import Outbox
    from .verify import Report, verify_folders, verify_outbox, verify_store

    report = Report()
    if (root / "metadata.sqlite3").is_file():
        report.merge(verify_store(LocalRepo(root, allow_network_fs=True, where="")))
    report.merge(verify_folders(root, repair=False)).merge(verify_outbox(Outbox(root), root))
    if not report.ok:
        raise StoreError(
            f"{root} was upgraded, but `cairn verify` finds problems ({report.summary()}): "
            + "; ".join(report.lines()[:5])
            + (f"; the 0.14 database is kept in {backup}" if backup else "")
        )
    return {"counts": counts, "notes": notes, "backup": backup, "report": report}
