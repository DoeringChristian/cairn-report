"""The local replica: every version this machine knows, one record per version uid, in two roots.

    <data>/versions/<uid>/manifest.json            a version the server does not have yet (sync pending | running | rejected)
    <data>/versions/<uid>/files/                   its files, read-only (made on first use: what `pull` returns)
    <data>/versions/<uid>/heartbeat                a running version's owner: {"time", "host", "pid"}, rewritten every sync
    <data>/versions/<uid>/.flushing-<host>-<pid>   a flusher's claim on it: {"time", "host", "pid"}
    <data>/blobs/<aa>/<sha256>                     the contents those versions need
    <data>/stores/<store-uid>.json                 one store server: {"url", "uid", "contact", "artifacts":
                                                    {"project:name": {selector: {"uid", "version"}}}} (last known answers)
    <cache>/versions/<uid>/manifest.json, files/    a synced version (store/cache.py), with its last access time
    <cache>/blobs/<aa>/<sha256>                     downloaded contents

`<data>` (`$CAIRN_DATA_DIR`, else `<project>/.cairn/pending`, else `~/.local/share/cairn`; see config.py) is durable:
what only this machine has lives there, never only in the cache. `<cache>` (`$CAIRN_CACHE_DIR`, else `<project>/.cairn/cache`) is
throwaway: it can be node-local scratch, deleted at any time, or trimmed by `cairn prune`.

A record holds everything the server needs: project, name, the manifest entries, label, metadata,
inputs, aliases, source, plus `sync` (synced | pending | rejected | running), `status` (the version's own:
completed | running | failed), `owner` (host, pid), `number` (None until the server assigns it), and
`error`. Every record update is write-temp-and-rename; there are no locks (see sync.py for claims).
A server never reached has no known uid yet: its store file is `url-<hash>.json` until it answers.

The layout before the replica (`<cache>/outbox/`, `<cache>/resolved/`, `<cache>/stores.json`, version
folders holding their files beside `.cairn-complete`) is converted on first use and then removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from ..store.blobs import now
from ..config import find_project_root
from ..store.cache import (FILES, RECORD, blob_cache, cache_root, data_root, data_root_at, data_roots, files_folder,
                           global_data_root, hostname, install, link_files, old_state_root, place, project_pending_default,
                           read_json, read_record, remove, scratch, version_folder, write_json)
from ..store.errors import StoreError
from ..store.refs import Ref

HEARTBEAT = "heartbeat"
CLAIM = ".flushing-"
LAYOUT = "layout"  # <cache>/layout: "2" once the cache uses this layout
DEFAULT_TIMEOUT = 600.0


def heartbeat_timeout(config=None) -> float:
    """`[store] heartbeat_timeout` (seconds; default 600): how long a running version may go without a heartbeat."""
    if config is None:
        try:
            from ..config import load_config

            config = load_config()
        except (SystemExit, ValueError, OSError):
            return DEFAULT_TIMEOUT
    return float(config.store.get("heartbeat_timeout", DEFAULT_TIMEOUT))


def epoch(stamp: str | None) -> float:
    try:
        return datetime.fromisoformat(stamp).timestamp() if stamp else 0.0
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ records


def folder_of(uid: str) -> Path:
    return version_folder(data_root(), uid)


def load(uid: str) -> dict | None:
    """The local record of a version uid (None if this machine has none: never made here, or synced)."""
    ensure_layout()
    record = read_record(folder_of(uid))
    return record if record and record.get("sync") != "synced" else None


def save(record: dict) -> None:
    write_json(folder_of(record["uid"]) / RECORD, {key: value for key, value in record.items() if not key.startswith("_")})


def records(all_roots: bool = False) -> list[dict]:
    """Every local record (any store), oldest first (by uid); with `all_roots`, those of the global data root too."""
    if all_roots:
        extra = data_roots()[1:]
        found = records()
        for root in extra:
            with data_root_at(root):
                found += records()
        return sorted(found, key=lambda record: record["uid"])
    ensure_layout()
    found = []
    base = data_root() / "versions"
    if not base.is_dir():
        return found
    for folder in base.iterdir():
        if ".tmp-" in folder.name:
            continue
        record = read_record(folder)
        if record and record.get("sync") != "synced":
            found.append(record)
    return sorted(found, key=lambda record: record["uid"])


def synced_records() -> list[dict]:
    """Every synced version in the cache, oldest first (by uid)."""
    ensure_layout()
    base = cache_root() / "versions"
    found = []
    if base.is_dir():
        for folder in base.iterdir():
            record = read_record(folder) if ".tmp-" not in folder.name else None
            if record and record.get("sync") == "synced":
                found.append(record)
    return sorted(found, key=lambda record: record["uid"])


def belongs(record: dict, url: str, uid: str | None) -> bool:
    """Whether a record is for the store server at `url` (or with store uid `uid`)."""
    return record.get("url") == url.rstrip("/") or bool(uid and record.get("store") == uid)


def manifest_of(record: dict) -> dict:
    """A version-style manifest for a record (what `Version` and `pull` read)."""
    if record.get("sync") == "synced":
        return record["manifest"]
    status = "pending" if record["status"] == "completed" else record["status"]
    return {"version": record.get("number"), "uid": record["uid"], "label": record.get("label"), "status": status,
            "created": record["created"], "files": record["files"], "metadata": record.get("metadata") or {},
            "inputs": record.get("inputs") or [], "source": record.get("source"), "pending": True, "sync": record["sync"],
            "aliases": record.get("aliases") or []}


def usable(record: dict | None) -> bool:
    """A pending version this machine can read and use as an input before the server has it."""
    return bool(record) and record.get("sync") == "pending" and record.get("status") == "completed"


def exact_ref(project: str, name: str, manifest: dict) -> str:
    """`project:name@v<n>`, or `project:name@<uid>` while the server has not numbered the version."""
    number = manifest.get("version")
    return str(Ref(project, name, f"v{number}" if number is not None else manifest["uid"]))


def data_blob(sha: str) -> Path:
    return blob_cache(data_root(), sha)


def blob_source(sha: str) -> Path | None:
    """A local copy of a content: the data root's, else the cache's."""
    for root in (data_root(), cache_root()):
        path = blob_cache(root, sha)
        if path.is_file():
            return path
    return None


def pending_files(uid: str) -> Path | None:
    """The read-only `files/` folder of a local record, made from the data root's blobs on first use (None if the record left)."""
    record = load(uid)
    if record is None:
        return None
    target = files_folder(data_root(), uid)
    if target.is_dir():
        return target
    build = scratch(target)

    def blob(entry):
        source = blob_source(entry["sha256"])
        if source is None:
            raise StoreError(f"{record['project']}:{record['name']}@{uid}: the contents of {entry['path']} left {data_root()}")
        return source

    try:
        link_files(record["files"], build, blob)
        try:
            os.rename(build, target)
        except OSError:
            if not target.is_dir():
                raise
    finally:
        if build.exists():
            remove(build)
    return target


def needed() -> set[str]:
    """The contents some local record still needs."""
    return {item["sha256"] for record in records() for item in record.get("files", []) if "sha256" in item}


def drop_blobs(shas, keep_in_cache: bool) -> None:
    """Remove data-root blobs no record needs any more (moved to the cache when it shares the filesystem)."""
    keep = needed()
    for sha in set(shas) - keep:
        blob = data_blob(sha)
        if not blob.is_file():
            continue
        if keep_in_cache:
            target = blob_cache(cache_root(), sha)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(blob, target)
                except OSError:
                    pass
        blob.unlink(missing_ok=True)


def same_filesystem() -> bool:
    try:
        (cache_root() / "versions").mkdir(parents=True, exist_ok=True)
        (data_root() / "versions").mkdir(parents=True, exist_ok=True)
        return os.stat(cache_root() / "versions").st_dev == os.stat(data_root() / "versions").st_dev
    except OSError:
        return False


def same_root() -> bool:
    return os.path.realpath(cache_root()) == os.path.realpath(data_root())


def promote(record: dict, manifest: dict, store_uid: str | None) -> None:
    """A version the server now has: from the data root into the cache (the record keeps its uid).

    The record is marked synced first, so a concurrent flusher skips it. When both roots share a
    filesystem, the version folder is renamed into the cache (its files and blobs stay hard links, no
    copy); otherwise the data-root copy is dropped and the cache downloads the version on its next pull.
    Blobs no other local record needs leave the data root either way."""
    uid = record["uid"]
    folder = folder_of(uid)
    synced = {"uid": uid, "sync": "synced", "store": store_uid, "project": record["project"], "name": record["name"],
              "version": manifest["version"], "manifest": manifest, "accessed": now(), "accessed_at": time.time()}
    shas = [item["sha256"] for item in record.get("files", []) if "sha256" in item]
    if same_root():
        try:
            pending_files(uid)
        except StoreError:
            pass
        write_json(folder / RECORD, synced)
        _clean(folder)
        return
    write_json(folder / RECORD, {**record, "sync": "synced"})  # others skip it from now on
    _clean(folder)
    if same_filesystem():
        if not (folder / FILES).is_dir():
            try:
                record_files(record, folder / FILES)
            except (StoreError, OSError):
                pass
        if (folder / FILES).is_dir():
            write_json(folder / RECORD, synced)
            target = version_folder(cache_root(), uid)
            try:
                install(folder, target)
            except OSError:
                pass
        drop_blobs(shas, keep_in_cache=True)
    else:
        drop_blobs(shas, keep_in_cache=False)
    if folder.exists():
        remove(folder)


def record_files(record: dict, target: Path) -> None:
    build = scratch(target)
    try:
        link_files(record["files"], build, lambda entry: blob_source(entry["sha256"]) or _gone(record, entry))
        os.rename(build, target)
    finally:
        if build.exists():
            remove(build)


def _gone(record, entry):
    raise StoreError(f"{record['project']}:{record['name']}@{record['uid']}: the contents of {entry['path']} are gone")


def drop(record: dict) -> None:
    """Forget a local record (a failed version the server now has, or one it already had)."""
    folder = folder_of(record["uid"])
    write_json(folder / RECORD, {**record, "sync": "synced"})
    remove(folder)
    drop_blobs([item["sha256"] for item in record.get("files", []) if "sha256" in item], keep_in_cache=same_filesystem())


def _clean(folder: Path) -> None:
    for path in folder.glob(f"{CLAIM}*"):
        path.unlink(missing_ok=True)
    (folder / HEARTBEAT).unlink(missing_ok=True)


# --------------------------------------------------------------- heartbeats


def beat(uid: str) -> None:
    """The owner of a running version says it is alive (write-temp-and-rename; never fails, never makes the folder)."""
    if not folder_of(uid).is_dir():
        return
    try:
        write_json(folder_of(uid) / HEARTBEAT, {"time": time.time(), "host": hostname(), "pid": os.getpid()})
    except OSError:
        pass


def last_beat(record: dict) -> float:
    beat_ = read_json(folder_of(record["uid"]) / HEARTBEAT) or {}
    return float(beat_.get("time") or epoch(record.get("created")))


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True
    return True


def dead(record: dict, timeout: float) -> str | None:
    """Why a running version's owner is gone (None while it may still run).

    The same host checks the pid (fast); any host fails it once its heartbeat is older than `timeout`."""
    owner = record.get("owner") or {}
    if owner.get("host") == hostname() and owner.get("pid") and not pid_alive(owner["pid"]):
        return f"its process ({owner.get('host')} pid {owner['pid']}) is gone"
    age = time.time() - last_beat(record)
    if age > timeout:
        return f"no heartbeat from {owner.get('host')} pid {owner.get('pid')} for {age:.0f} s (timeout {timeout:g} s)"
    return None


# ------------------------------------------------------------------ claims


def _claim_name() -> str:
    host = "".join(char if char.isalnum() or char in "-_." else "_" for char in hostname())[:60] or "host"
    return f"{CLAIM}{host}-{os.getpid()}"


def _stale(path: Path, timeout: float) -> bool:
    claim = read_json(path)
    if not isinstance(claim, dict):
        try:
            return time.time() - path.stat().st_mtime > timeout
        except OSError:
            return True
    if claim.get("host") == hostname() and claim.get("pid") and not pid_alive(claim["pid"]):
        return True
    return time.time() - float(claim.get("time", 0)) > timeout


def claim(uid: str, timeout: float) -> Path | None:
    """Claim a local record for sending: exclusively create `.flushing-<host>-<pid>` (a timestamp in it).

    None when another live claim exists (stale ones, older than `timeout` or of a dead pid on this host, are
    ignored) or the record left. Two flushers that claim at the same moment both see both claims and back
    off; the next flush sends it. A claim only saves double work: sending is idempotent anyway."""
    folder = folder_of(uid)
    mine = folder / _claim_name()
    if not folder.is_dir():
        return None
    others = [path for path in folder.glob(f"{CLAIM}*") if path != mine and ".tmp-" not in path.name]
    if any(not _stale(path, timeout) for path in others):
        return None
    temporary = scratch(mine)
    try:
        temporary.write_text(json.dumps({"time": time.time(), "host": hostname(), "pid": os.getpid()}))
        try:
            os.link(temporary, mine)  # exclusive: fails if it exists
        except FileExistsError:
            if not _stale(mine, timeout):  # this process holds it already (another thread)
                return None
            os.replace(temporary, mine)
    except OSError:  # the folder left (sent and promoted by another flusher)
        return None
    finally:
        temporary.unlink(missing_ok=True)
    others = [path for path in folder.glob(f"{CLAIM}*") if path != mine and ".tmp-" not in path.name]
    if any(not _stale(path, timeout) for path in others):
        release(mine)
        return None
    return mine


def refresh_claim(path: Path | None) -> None:
    if path is not None:
        try:
            write_json(path, {"time": time.time(), "host": hostname(), "pid": os.getpid()})
        except OSError:
            pass


def release(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


# -------------------------------------------------------------- store files


def store_key(url: str) -> str:
    """The store file name of a server whose uid is not known yet."""
    return "url-" + hashlib.sha256(url.rstrip("/").encode()).hexdigest()[:16]


def _store_path(key: str) -> Path:
    return data_root() / "stores" / f"{key}.json"


def known_uid(url: str) -> str | None:
    """The uid of the server at `url`, if it ever answered."""
    ensure_layout()
    url = url.rstrip("/")
    folder = data_root() / "stores"
    if not folder.is_dir():
        return None
    for path in folder.glob("*.json"):
        if path.name.startswith("url-"):
            continue
        known = read_json(path) or {}
        if known.get("url") == url and known.get("uid"):
            return known["uid"]
    return None


def store_file(key: str) -> dict:
    return read_json(_store_path(key)) or {}


def remember_uid(url: str, uid: str) -> None:
    """Record the server's uid; what was kept under its URL's key moves into its uid's file."""
    url = url.rstrip("/")
    path, old = _store_path(uid), _store_path(store_key(url))
    known = read_json(path) or {}
    if old.is_file():
        earlier = read_json(old) or {}
        for artifact, hints in (earlier.get("artifacts") or {}).items():
            known.setdefault("artifacts", {}).setdefault(artifact, {}).update(hints)
    if known.get("url") != url or known.get("uid") != uid or old.is_file():
        write_json(path, {**known, "url": url, "uid": uid, "contact": now()})
        old.unlink(missing_ok=True)


def hint(key: str | None, project: str, name: str, selector: str | None) -> dict | None:
    """The last answer for `project:name@selector` from this store: {uid, version}."""
    if not key:
        return None
    return ((store_file(key).get("artifacts") or {}).get(f"{project}:{name}") or {}).get(selector or "latest")


def remember(key: str | None, project: str, name: str, selectors, version: dict, url: str | None = None) -> None:
    """Record that `project:name@selector` (for each selector, and `@v<n>`, `@<uid>`) resolved to `version`."""
    if not key or not version.get("uid") or version.get("version") is None:
        return
    path = _store_path(key)
    known = read_json(path) or {}
    hints = known.setdefault("artifacts", {}).setdefault(f"{project}:{name}", {})
    value = {"uid": version["uid"], "version": version["version"]}
    keys = {*(selector or "latest" for selector in selectors), f"v{version['version']}", version["uid"]}
    if any(hints.get(selector) != value for selector in keys) or time.time() - epoch(known.get("contact")) > 60:
        hints.update({selector: value for selector in keys})
        if url:
            known["url"] = url.rstrip("/")
        known["contact"] = now()
        write_json(path, known)


def remember_aliases(key: str | None, project: str, name: str, aliases: dict[str, dict]) -> None:
    """Replace what is known about an artifact's aliases ({alias: {uid, version}}) in one write."""
    if not key:
        return
    path = _store_path(key)
    known = read_json(path) or {}
    hints = known.setdefault("artifacts", {}).setdefault(f"{project}:{name}", {})
    for alias, value in aliases.items():
        hints[alias] = value
        hints[f"v{value['version']}"] = hints[value["uid"]] = value
    known["contact"] = now()
    write_json(path, known)


def stores() -> list[dict]:
    folder = data_root() / "stores"
    return [read_json(path) or {} for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []


# ---------------------------------------------------------------- migration

_checked: set[tuple[str, str]] = set()


def ensure_layout() -> None:
    """Convert the layout before the replica, once per process and pair of roots (see the module doc)."""
    roots = (str(cache_root()), str(data_root()))
    if roots in _checked:
        return
    _checked.add(roots)
    try:
        move_old_state()
        move_global_pending()
    except OSError:
        pass
    cache = cache_root()
    if (cache / LAYOUT).is_file() or not cache.is_dir():
        return
    try:
        migrate(cache)
        write_json(cache / LAYOUT, 2)
    except OSError:
        _checked.discard(roots)


def move_old_state() -> list[str]:
    """Move pending records from the data root's predecessor (`$XDG_STATE_HOME/cairn`, else
    `~/.local/state/cairn`) into the data root, with their blobs and store files; returns what was moved.
    The old folder is removed once empty. (Nothing moves when `$CAIRN_STATE_DIR` named the old folder: it
    is the data root then.)"""
    old = old_state_root()
    new = global_data_root() if project_pending_default() is not None else data_root()
    if not (old / "versions").is_dir() or os.path.realpath(old) == os.path.realpath(new):
        return []
    moved = []
    for folder in sorted((old / "versions").iterdir()):
        record = read_record(folder)
        if ".tmp-" in folder.name or not record:
            continue
        target = version_folder(new, record["uid"])
        if target.exists():
            remove(folder)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(folder, target)
        except OSError:
            shutil.copytree(folder, target, symlinks=True)
            remove(folder)
        for item in record.get("files", []):
            if "sha256" in item and blob_cache(old, item["sha256"]).is_file() and not data_blob(item["sha256"]).is_file():
                _copy_blob(blob_cache(old, item["sha256"]), data_blob(item["sha256"]))
        moved.append(record["uid"])
    for path in sorted((old / "stores").glob("*.json")) if (old / "stores").is_dir() else []:
        if not (new / "stores" / path.name).exists():
            (new / "stores").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, new / "stores" / path.name)
    for name in ("versions", "blobs", "stores"):
        shutil.rmtree(old / name, ignore_errors=True)
    try:
        old.rmdir()
    except OSError:
        pass
    if moved:
        print(f"cairn: moved {len(moved)} pending versions from {old} to {new}", file=sys.stderr)
    return moved


def move_global_pending() -> list[str]:
    """Move pending records from the global data root whose `source.folder` lies inside this project into the
    project's pending root (used only when that is the data root); returns the uids moved. Running versions
    stay (their job writes to the old place); so do records of other projects. Never overwrites."""
    if project_pending_default() is None:
        return []
    old, new, project = global_data_root(), data_root(), find_project_root()
    if project is None or not (old / "versions").is_dir() or os.path.realpath(old) == os.path.realpath(new):
        return []
    moved = []
    for folder in sorted((old / "versions").iterdir()):
        record = None if ".tmp-" in folder.name else read_record(folder)
        if not record or record.get("sync") in ("synced", "running"):
            continue
        where = (record.get("source") or {}).get("folder")
        if not where or not _inside(Path(where), project):
            continue
        target = version_folder(new, record["uid"])
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(folder, target)
        except OSError:
            shutil.copytree(folder, target, symlinks=True)
            remove(folder)
        for item in record.get("files", []):
            if "sha256" in item and blob_cache(old, item["sha256"]).is_file() and not data_blob(item["sha256"]).is_file():
                _copy_blob(blob_cache(old, item["sha256"]), data_blob(item["sha256"]))
        moved.append(record["uid"])
    if moved:
        left = set()
        for folder in (old / "versions").iterdir():
            record = None if ".tmp-" in folder.name else read_record(folder)
            left |= {item["sha256"] for item in (record or {}).get("files", []) if "sha256" in item}
        for uid in moved:
            for item in (read_record(version_folder(new, uid)) or {}).get("files", []):
                if "sha256" in item and item["sha256"] not in left:
                    blob_cache(old, item["sha256"]).unlink(missing_ok=True)
        print(f"cairn: moved {len(moved)} pending versions of this project from {old} to {new}", file=sys.stderr)
    return moved


def _inside(path: Path, folder: Path) -> bool:
    try:
        path = path.resolve()
    except OSError:
        return False
    return folder in [path, *path.parents]


def migrate(cache: Path) -> list[str]:
    """Convert `<cache>/outbox/`, `resolved/`, `stores.json`, and old version folders; returns what was done."""
    done = []
    old_stores = read_json(cache / "stores.json") or {}
    urls = {store_key(url): url for url in old_stores} | {uid: url for url, uid in old_stores.items()}
    for key in {*old_stores.values()}:
        if key:
            remember_uid(urls[key], key)
    resolved = cache / "resolved"
    if resolved.is_dir():
        for file in resolved.glob("*.json"):
            key = file.stem
            hints: dict = {}
            for text, value in (read_json(file) or {}).items():
                head, _, selector = text.rpartition("@")
                if head and isinstance(value, dict) and value.get("uid"):
                    hints.setdefault(head, {})[selector] = value
            path = _store_path(key)
            known = read_json(path) or {}
            for artifact, values in hints.items():
                known.setdefault("artifacts", {}).setdefault(artifact, {}).update(values)
            if urls.get(key):
                known.setdefault("url", urls[key])
            if key in old_stores.values():
                known["uid"] = key
            write_json(path, known)
            file.unlink(missing_ok=True)
            done.append(f"hints of {key}")
        shutil.rmtree(resolved, ignore_errors=True)
    outbox = cache / "outbox"
    if outbox.is_dir():
        for file in sorted(outbox.glob("*/*.json")):
            entry = read_json(file)
            if isinstance(entry, dict) and entry.get("uid") and not load(entry["uid"]):
                save(_converted(entry, file.parent.name))
                for item in entry.get("files", []):
                    if "sha256" in item and not data_blob(item["sha256"]).is_file():
                        source = blob_cache(cache, item["sha256"])
                        if source.is_file():
                            _copy_blob(source, data_blob(item["sha256"]))
                done.append(f"queued {entry['uid']}")
            file.unlink(missing_ok=True)
        shutil.rmtree(outbox, ignore_errors=True)
    (cache / "stores.json").unlink(missing_ok=True)
    versions = cache / "versions"
    if versions.is_dir():
        for folder in versions.iterdir():
            marker = folder / ".cairn-complete"
            if ".tmp-" in folder.name or not marker.is_file():
                continue
            old = read_json(marker) or {}
            build = scratch(folder)
            try:
                build.mkdir()
                os.rename(folder, build / FILES)
                os.chmod(build / FILES / ".cairn-complete", 0o644)
                (build / FILES / ".cairn-complete").unlink()
                write_json(build / RECORD, {"uid": old.get("uid", folder.name), "sync": "synced", "store": old.get("store"),
                                            "project": old.get("project"), "name": old.get("name"), "version": old.get("version"),
                                            "manifest": old.get("manifest"), "accessed": now(), "accessed_at": time.time()})
                install(build, folder)
                done.append(f"cached {folder.name}")
            except OSError:
                pass
            finally:
                if build.exists():
                    remove(build)
    return done


def _copy_blob(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = scratch(target)
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o444)
        place(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _converted(entry: dict, key: str) -> dict:
    """An outbox entry as a local record."""
    old = entry.get("state")
    sync = {"pending": "pending", "completed": "pending", "failed": "pending", "running": "running", "rejected": "rejected"}.get(old, "pending")
    status = {"failed": "failed", "running": "running"}.get(old, "completed")
    if old == "rejected" and entry.get("kind") == "running" and entry.get("number") is None:
        status = "completed"
    record = {key_: entry.get(key_) for key_ in ("uid", "kind", "url", "project", "name", "label", "metadata", "inputs", "aliases",
                                                  "source", "files", "created", "owner", "number", "pushed", "error")}
    record.update(sync=sync, status=status, store=key, pushed=entry.get("pushed") or {}, files=entry.get("files") or [],
                  inputs=entry.get("inputs") or [], aliases=entry.get("aliases") or [])
    return record
