"""Push reports and runs to a hub, and pull them back.

    cairn-report push [report|runs]            send the report (a new version) and/or the runs
    cairn-report push --watch [-- <command>]   keep sending every [hub] interval seconds; with a
                                               command, while it runs and once more after it exits
    cairn-report pull [--runs] [--version N]   bring the report (and runs) back into the project
    cairn-report pull --name P --into DIR      restore project P into an empty folder

The report is the configured report folder (`reports_dir`: sources, `source/...` on the
hub) plus its built pages (`output_dir`, `site/...`), versioned as a whole. Runs are
the files under `outputs_dir`, sent file by file. Only content the hub lacks is
uploaded (addressed by SHA-256, verified by the hub); hashes are cached by size and
modification time, so repeated pushes reread only changed files.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlparse

from .config import Config

TOKEN_ENV, URL_ENV = "CAIRN_HUB_TOKEN", "CAIRN_HUB_URL"
SKIP_NAMES = {"__pycache__", ".DS_Store"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
BATCH = 5_000


class HubError(Exception):
    pass


class Limited:
    """A file read as exactly `size` bytes, even if it grows while it is being sent."""

    def __init__(self, handle, size: int):
        self.handle, self.remaining = handle, size

    def read(self, amount: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        chunk = self.handle.read(self.remaining if amount < 0 else min(amount, self.remaining))
        self.remaining -= len(chunk)
        return chunk


class Hub:
    def __init__(self, url: str, token: str, allow_http: bool = False, timeout: float = 120):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise SystemExit(f"hub URL {url!r} must look like https://host[:port]")
        if parsed.path not in ("", "/"):
            raise SystemExit(f"hub URL {url!r}: serve the hub at the root of its host, without a path")
        if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS and not allow_http:
            raise SystemExit(f"refusing to send the hub token over plain HTTP to {parsed.hostname}; "
                             "use https://, an SSH tunnel to localhost, or --allow-http")
        self.url, self.parsed, self.token, self.timeout = url.rstrip("/"), parsed, token, timeout

    def _connection(self):
        if self.parsed.scheme == "https":
            return http.client.HTTPSConnection(self.parsed.hostname, self.parsed.port, timeout=self.timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self.parsed.hostname, self.parsed.port, timeout=self.timeout)

    def request(self, method: str, path: str, body=None, headers: dict | None = None,
                expect=(200,), stream_to: Path | None = None):
        connection = self._connection()
        try:
            connection.request(method, path, body=body, headers={"Authorization": f"Bearer {self.token}", **(headers or {})})
            response = connection.getresponse()
            if response.status in expect and stream_to is not None:
                with stream_to.open("wb") as handle:
                    while chunk := response.read(1 << 20):
                        handle.write(chunk)
                return response.status, b""
            data = response.read()
        finally:
            connection.close()
        if response.status == 401:
            raise HubError("the hub rejected the token")
        if response.status not in expect:
            raise HubError(f"{method} {path}: HTTP {response.status} {data[:300].decode(errors='replace')}")
        return response.status, data

    def json(self, method: str, path: str, value=None, expect=(200,)):
        body = None if value is None else json.dumps(value).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        status, data = self.request(method, path, body, headers, expect)
        return status, (json.loads(data) if data else None)

    def upload(self, sha: str, file: Path, size: int) -> None:
        with file.open("rb") as handle:
            self.request("PUT", f"/api/v1/blobs/{sha}", body=Limited(handle, size),
                         headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
                         expect=(200, 201))

    def missing(self, hashes: list[str]) -> set[str]:
        missing: set[str] = set()
        for start in range(0, len(hashes), BATCH):
            _, answer = self.json("POST", "/api/v1/blobs/missing", {"sha256": hashes[start:start + BATCH]})
            missing.update(answer["missing"])
        return missing


# ----------------------------------------------------------------- settings


def connect(config: Config | None, url: str | None = None, allow_http: bool = False) -> Hub:
    settings = config.hub if config else {}
    url = url or os.environ.get(URL_ENV) or settings.get("url")
    if not url:
        raise SystemExit("no hub URL: set [hub] url in cairn-report.toml, CAIRN_HUB_URL, or --url")
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token and settings.get("token_file"):
        token_file = Path(settings["token_file"]).expanduser()
        if not token_file.is_absolute() and config:
            token_file = config.project / token_file
        if not token_file.is_file():
            raise SystemExit(f"hub token file {token_file} does not exist")
        if token_file.stat().st_mode & 0o077:
            print(f"warning: {token_file} is readable by other users; run chmod 600 {token_file}", file=sys.stderr)
        token = token_file.read_text().strip()
    if not token:
        raise SystemExit("no hub token: set CAIRN_HUB_TOKEN or [hub] token_file in cairn-report.toml")
    return Hub(url, token, allow_http=allow_http)


def project_name(config: Config) -> str:
    name = config.hub.get("project") or re.sub(r"[^a-z0-9_.-]+", "-", config.project.name.lower()).strip("-.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", name or ""):
        raise SystemExit(f"hub project name {name!r} is not a valid slug; set [hub] project in cairn-report.toml")
    return name


# --------------------------------------------------------------------- scan


def exclusion(patterns: list[str]):
    """A matcher for gitignore-style globs on project-relative paths.

    `*` and `?` stay within one folder, `**` spans folders; a pattern without a leading `/`
    matches at any depth (".submitit/**/*.pkl" matches outputs/a/1/.submitit/0/x.pkl), and a
    pattern ending in `/` (or naming a folder) excludes everything inside it.
    """
    expressions = []
    for pattern in patterns:
        anchored = pattern.startswith("/")
        body = pattern.strip("/")
        regex, index = "", 0
        while index < len(body):
            if body.startswith("**/", index):
                regex, index = regex + "(?:.*/)?", index + 3
            elif body.startswith("**", index):
                regex, index = regex + ".*", index + 2
            elif body[index] == "*":
                regex, index = regex + "[^/]*", index + 1
            elif body[index] == "?":
                regex, index = regex + "[^/]", index + 1
            else:
                regex, index = regex + re.escape(body[index]), index + 1
        expressions.append(re.compile(("" if anchored else "(?:.*/)?") + regex + "(?:/.*)?"))
    return lambda path: any(expression.fullmatch(path) for expression in expressions)


def skipped(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(".tmp")


def scan(local: Path, prefix: str, config: Config, skip_folder=None, exclude: list[str] | None = None) -> dict[str, dict]:
    """Every file and relative symlink under `local`, keyed `<prefix>/<path inside local>` (prefix may be "").

    `exclude` replaces the `[hub]` exclude globs (the store passes its own)."""
    found: dict[str, dict] = {}
    if not local.is_dir():
        return found
    excluded = exclusion(config.hub.get("exclude", []) if exclude is None else exclude)
    project = config.project.resolve()
    try:  # patterns match project paths (outputs/..., reports/..., generated/reports/...)
        base = local.resolve().relative_to(project).as_posix()
    except ValueError:
        base = prefix or local.name
    for directory, folders, files in os.walk(local):
        here = Path(directory)
        keep = []
        for name in folders:
            path = here / name
            if skipped(name) or (skip_folder and skip_folder(path)):
                continue
            if path.is_symlink():
                files.append(name)  # recorded as a link, never followed
            else:
                keep.append(name)
        folders[:] = keep
        for name in files:
            if skipped(name):
                continue
            path = here / name
            inside = path.relative_to(local).as_posix()
            if excluded(f"{base}/{inside}"):
                continue
            key = f"{prefix}/{inside}" if prefix else inside
            if path.is_symlink():
                target = os.readlink(path)
                if not os.path.isabs(target):
                    found[key] = {"link": target}
                continue
            try:
                info = path.stat()
            except FileNotFoundError:  # removed while scanning
                continue
            found[key] = {"file": path, "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    return found


class HashCache:
    """SHA-256 per file, reused while size and modification time are unchanged."""

    def __init__(self, project: Path):
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cairn-report"
        self.path = base / f"push-{hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:16]}.json"
        try:
            self.entries = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.entries = {}
        self.dirty = False
        self.lock = threading.Lock()

    def sha(self, file: Path, info: dict) -> str:
        key = str(file)
        cached = self.entries.get(key)
        if cached and cached[0] == info["size"] and cached[1] == info["mtime_ns"]:
            return cached[2]
        digest = hashlib.sha256()
        with file.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        with self.lock:
            self.entries[key] = [info["size"], info["mtime_ns"], digest.hexdigest()]
            self.dirty = True
        return digest.hexdigest()

    def forget(self, file: Path) -> None:
        with self.lock:
            self.entries.pop(str(file), None)
            self.dirty = True

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(self.entries))
            os.replace(temporary, self.path)
            self.dirty = False


def entries_for(files: dict[str, dict], cache: HashCache) -> tuple[list[dict], dict[str, Path]]:
    """Hub entries for scanned files, and a local source file for each content hash."""
    entries, sources = [], {}
    for key, info in files.items():
        if "link" in info:
            entries.append({"path": key, "link": info["link"]})
            continue
        try:
            sha = cache.sha(info["file"], info)
        except FileNotFoundError:
            continue
        entries.append({"path": key, "sha256": sha, "size": info["size"], "mtime": info["mtime_ns"] / 1e9})
        sources.setdefault(sha, (info["file"], info["size"]))
    return entries, sources


def upload_missing(hub: Hub, sources: dict[str, tuple[Path, int]], cache: HashCache, workers: int = 4) -> tuple[set[str], int]:
    """Upload the contents the hub lacks. Returns (hashes that failed, bytes sent)."""
    missing = hub.missing(sorted(sources))
    failed: set[str] = set()

    def send(sha: str) -> int:
        file, size = sources[sha]
        try:
            hub.upload(sha, file, size)
            return size
        except (HubError, OSError) as error:  # changed while uploading, or a network error: retry next time
            cache.forget(file)
            failed.add(sha)
            print(f"warning: {file} not uploaded ({error}); retrying next push", file=sys.stderr)
            return 0

    with ThreadPoolExecutor(workers) as pool:
        sent = sum(pool.map(send, sorted(missing)))
    return failed, sent


class RunPusher:
    """Sends run files under `outputs_dir` (or one run folder below it) as they change."""

    def __init__(self, hub: Hub, config: Config, name: str, folder: Path | None = None):
        self.hub, self.config, self.name = hub, config, name
        self.outputs = config.outputs_dir.resolve()
        self.folder = (folder or self.outputs).resolve()
        self.cache = HashCache(config.project)
        self.remote: dict[str, dict] | None = None

    def once(self) -> tuple[int, int]:
        if self.remote is None:
            status, entries = self.hub.json("GET", f"/api/v1/projects/{self.name}/runs", expect=(200, 404))
            self.remote = {entry["path"]: entry for entry in entries} if status == 200 else {}
        prefix = self.folder.relative_to(self.outputs).as_posix() if self.folder != self.outputs else ""
        files = scan(self.folder, "" if prefix == "." else prefix, self.config)
        entries, sources = entries_for(files, self.cache)
        changed = [entry for entry in entries if _changed(self.remote.get(entry["path"]), entry)]
        self.cache.save()
        if not changed:
            return 0, 0
        failed, sent = upload_missing(self.hub, {e["sha256"]: sources[e["sha256"]] for e in changed if "sha256" in e}, self.cache)
        ready = [entry for entry in changed if entry.get("sha256") not in failed]
        updated = 0
        for start in range(0, len(ready), BATCH):
            _, answer = self.hub.json("POST", f"/api/v1/projects/{self.name}/runs", {"entries": ready[start:start + BATCH]})
            updated += answer["updated"]
        for entry in ready:
            self.remote[entry["path"]] = entry
        self.cache.save()
        return updated, sent


def unbuilt_reports(config: Config) -> list[str]:
    """Reports in the source tree whose pages the last build did not write."""
    from . import build as builder

    builder.configure(config)
    return [key for key in builder.discover_reports()
            if not (builder.output_dir(key) / "index.html").is_file()
            or not (builder.output_dir(key) / "report-data.json").is_file()]


class ReportPusher:
    """Sends the report folder and its built pages as one new version, when anything changed."""

    def __init__(self, hub: Hub, config: Config, name: str):
        self.hub, self.config, self.name = hub, config, name
        self.cache = HashCache(config.project)

    def once(self) -> tuple[int | None, bool, int]:
        """Returns (version, created, bytes sent)."""
        source = self.config.reports_dir.resolve()
        site = self.config.output_dir.resolve()
        nested = site if source in site.parents else None  # never push the build output twice
        files = scan(source, "source", self.config, skip_folder=(lambda path: path.resolve() == nested) if nested else None)
        files.update(scan(site, "site", self.config))
        if not any(key.startswith("source/") for key in files):
            return None, False, 0
        unbuilt = unbuilt_reports(self.config)
        if unbuilt:  # a failed or partial build must not become a report version
            raise HubError("these reports have no built pages: " + ", ".join(key or "/" for key in unbuilt)
                           + "; run `cairn-report build` (and fix its errors) before pushing")
        entries, sources = entries_for(files, self.cache)
        failed, sent = upload_missing(self.hub, sources, self.cache)
        self.cache.save()
        if failed:
            raise HubError(f"{len(failed)} report files were not uploaded; push again")
        status, answer = self.hub.json("POST", f"/api/v1/projects/{self.name}/report", {"entries": entries}, expect=(200, 201))
        return answer["version"], answer["created"], sent


def _changed(old: dict | None, new: dict) -> bool:
    if old is None:
        return True
    if "link" in new:
        return old.get("link") != new["link"]
    return old.get("sha256") != new["sha256"]


# -------------------------------------------------------------------- push


def push(config: Config, what: str = "all", watch: bool = False, interval: float | None = None,
         command: list[str] | None = None, url: str | None = None, allow_http: bool = False) -> int:
    hub = connect(config, url, allow_http)
    name = project_name(config)
    runs = RunPusher(hub, config, name) if what in ("all", "runs") else None
    report = ReportPusher(hub, config, name) if what in ("all", "report") else None
    interval = float(interval or config.hub.get("interval", 30))
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    process = subprocess.Popen(command) if command else None
    watching = watch or process is not None
    failures = 0

    def sync(final: bool = False) -> bool:
        nonlocal failures
        try:
            messages = []
            if runs:
                updated, sent = runs.once()
                if updated or final:
                    messages.append(f"{updated} run files ({sent / 1e6:.1f} MB)")
            if report:
                version, created, sent = report.once()
                if version is not None and (created or final):
                    messages.append(f"report v{version}{'' if created else ' (unchanged)'} ({sent / 1e6:.1f} MB)")
            failures = 0
            if messages:
                print(f"pushed {', '.join(messages)} to {hub.url}/p/{name}/", flush=True)
            return True
        except (HubError, OSError) as error:
            failures += 1
            print(f"{'warning: ' if watching else ''}push failed ({error}){'; retrying' if watching else ''}",
                  file=sys.stderr, flush=True)
            return False

    if not watching:
        return 0 if sync(final=True) else 1
    print(f"pushing {config.project} to {hub.url} as {name!r} every {interval:.0f} s", flush=True)
    while True:
        sync()
        wait = min(interval * 2 ** min(failures, 4), 600)  # back off while the hub is unreachable
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not stop.is_set() and not (process and process.poll() is not None):
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        if stop.is_set() or (process and process.poll() is not None):
            break
    if process and process.poll() is None:
        process.send_signal(signal.SIGTERM)  # we were stopped: stop the command too, then sync its final state
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for attempt in range(5):  # the final state must reach the hub
        if sync(final=True):
            break
        time.sleep(min(2 ** attempt * 5, 60))
    else:
        print("error: the final push did not reach the hub; run `cairn-report push` again", file=sys.stderr)
        return process.returncode if process and process.returncode else 1
    return process.returncode if process else 0


# -------------------------------------------------------------------- pull


def _download(hub: Hub, url_path: str, local: Path, sha: str) -> bool:
    """Fetch one file into place (verified); False if it was already there."""
    if local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == sha:
        return False
    local.parent.mkdir(parents=True, exist_ok=True)
    temporary = local.with_name(f".{local.name}.pull.tmp")
    hub.request("GET", url_path, stream_to=temporary)
    digest = hashlib.sha256()
    with temporary.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != sha:
        temporary.unlink()
        raise HubError(f"{url_path}: downloaded content does not match its SHA-256")
    os.replace(temporary, local)
    return True


def _place(entries: list[dict], target_of, force: bool) -> list[tuple[dict, Path]]:
    """Map entries to local paths; refuse to replace differing local files unless forced."""
    from .hub import valid_path

    placed, conflicts = [], []
    for entry in entries:
        if not valid_path(entry["path"]):
            continue
        local = target_of(entry["path"])
        if local is None:
            continue
        if not force and "sha256" in entry and local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() != entry["sha256"]:
            conflicts.append(str(local))
        placed.append((entry, local))
    if conflicts:
        shown = "\n  ".join(conflicts[:20]) + (f"\n  (+{len(conflicts) - 20} more)" if len(conflicts) > 20 else "")
        raise SystemExit(f"these local files differ from the hub and would be replaced (pass --force):\n  {shown}")
    return placed


def _write(hub: Hub, placed: list[tuple[dict, Path]], url_of) -> int:
    count = 0
    for entry, local in sorted(placed, key=lambda item: "link" in item[0]):  # files first, then links
        if "link" in entry:
            if not local.exists() and not local.is_symlink():
                local.parent.mkdir(parents=True, exist_ok=True)
                local.symlink_to(entry["link"])
            continue
        count += _download(hub, url_of(entry["path"]), local, entry["sha256"])
    return count


def pull(hub: Hub, name: str, config: Config, version: int | None = None, runs: list[str] | None = None,
         with_report: bool = True, force: bool = False) -> None:
    """Bring a project's report (sources into reports_dir, pages into output_dir) and optionally runs."""
    if with_report:
        query = f"?version={version}" if version else ""
        status, record = hub.json("GET", f"/api/v1/projects/{name}/report{query}", expect=(200, 404))
        if status == 404:
            print(f"{name}: no report on the hub")
        else:
            roots = {"source": config.reports_dir, "site": config.output_dir}

            def report_target(path: str) -> Path | None:
                tree, _, inside = path.partition("/")
                return roots[tree] / inside if tree in roots else None

            placed = _place(record["entries"], report_target, force)
            count = _write(hub, placed, lambda path: f"/files/{name}/report/{quote(path)}?version={record['version']}")
            print(f"pulled report v{record['version']} of {name!r}: {count} files changed")
    if runs is not None:
        status, entries = hub.json("GET", f"/api/v1/projects/{name}/runs", expect=(200, 404))
        entries = entries if status == 200 else []
        wanted = [entry for entry in entries
                  if not runs or any(entry["path"] == run or entry["path"].startswith(run.rstrip("/") + "/") for run in runs)]
        placed = _place(wanted, lambda path: config.outputs_dir / path, force)
        count = _write(hub, placed, lambda path: f"/files/{name}/runs/{quote(path)}")
        print(f"pulled {count} run files of {name!r} into {config.outputs_dir}")
