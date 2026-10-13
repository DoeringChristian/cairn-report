"""Push a project's runs and reports to a hub, pull hub comments back, or restore from a hub.

    cairn-report push                          one sync of outputs/, reports/, and the built reports
    cairn-report push --watch                  sync every [hub] interval seconds until stopped
    cairn-report push --watch -- <command...>  run a command (e.g. an experiment), sync while it
                                               runs and once more after it exits; returns its exit code
    cairn-report pull-comments                 append comments written on the hub to reports/
    cairn-report fetch <project> <dest>        download a project's latest files from the hub

Only changed files are sent: contents are addressed by SHA-256, the hub reports
which ones it lacks, and every upload is verified by the hub. Hashes are cached
by size and modification time, so a watch loop rereads only files that changed.
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

from . import comments
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
            detail = data[:300].decode(errors="replace")
            raise HubError(f"{method} {path}: HTTP {response.status} {detail}")
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


def trees(config: Config) -> list[tuple[Path, str]]:
    return [(config.outputs_dir, "outputs"), (config.reports_dir, "reports"), (config.output_dir, "site")]


def skipped(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(".tmp")


def scan(config: Config) -> dict[str, dict]:
    """Every pushable file and relative symlink, keyed by its path on the hub."""
    found: dict[str, dict] = {}
    for local, prefix in trees(config):
        if not local.is_dir():
            continue
        for directory, folders, files in os.walk(local):
            here = Path(directory)
            keep = []
            for name in folders:
                if skipped(name):
                    continue
                if (here / name).is_symlink():
                    files.append(name)  # recorded as a link, never followed
                else:
                    keep.append(name)
            folders[:] = keep
            for name in files:
                if skipped(name):
                    continue
                path = here / name
                key = f"{prefix}/{path.relative_to(local).as_posix()}"
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

    def sha(self, key: str, info: dict) -> str:
        cached = self.entries.get(key)
        if cached and cached[0] == info["size"] and cached[1] == info["mtime_ns"]:
            return cached[2]
        digest = hashlib.sha256()
        with info["file"].open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        self.entries[key] = [info["size"], info["mtime_ns"], digest.hexdigest()]
        self.dirty = True
        return self.entries[key][2]

    def forget(self, key: str) -> None:
        self.entries.pop(key, None)
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.entries))
        os.replace(temporary, self.path)
        self.dirty = False


# --------------------------------------------------------------------- push


class Pusher:
    def __init__(self, hub: Hub, config: Config, name: str, workers: int = 4):
        self.hub, self.config, self.name, self.workers = hub, config, name, workers
        self.cache = HashCache(config.project)
        self.remote: dict[str, dict] | None = None

    def _remote(self) -> dict[str, dict]:
        if self.remote is None:
            status, entries = self.hub.json("GET", f"/api/v1/projects/{self.name}/tree", expect=(200, 404))
            self.remote = {entry["path"]: entry for entry in entries} if status == 200 else {}
        return self.remote

    def once(self) -> tuple[int, int]:
        """One sync. Returns (files updated on the hub, bytes uploaded)."""
        remote, local = self._remote(), scan(self.config)
        changed, sources = [], {}
        for key, info in local.items():
            if "link" in info:
                if remote.get(key, {}).get("link") != info["link"]:
                    changed.append({"path": key, "link": info["link"]})
                continue
            try:
                sha = self.cache.sha(key, info)
            except FileNotFoundError:
                continue
            if remote.get(key, {}).get("sha256") != sha:
                changed.append({"path": key, "sha256": sha, "size": info["size"], "mtime": info["mtime_ns"] / 1e9})
                sources.setdefault(sha, (key, info))
        self.cache.save()
        if not changed:
            return 0, 0
        missing = []
        hashes = sorted(sources)
        for start in range(0, len(hashes), BATCH):
            _, answer = self.hub.json("POST", f"/api/v1/projects/{self.name}/missing", {"sha256": hashes[start:start + BATCH]})
            missing += answer["missing"]
        failed, sent = set(), 0

        def send(sha: str) -> int:
            key, info = sources[sha]
            try:
                self.hub.upload(sha, info["file"], info["size"])
                return info["size"]
            except (HubError, OSError) as error:  # changed while uploading, or a network error: retry next sync
                self.cache.forget(key)
                failed.add(sha)
                print(f"warning: {key} not uploaded ({error}); retrying next sync", file=sys.stderr)
                return 0

        with ThreadPoolExecutor(self.workers) as pool:
            sent = sum(pool.map(send, missing))
        ready = [entry for entry in changed if entry.get("sha256") not in failed]
        updated = 0
        for start in range(0, len(ready), BATCH):
            _, answer = self.hub.json("POST", f"/api/v1/projects/{self.name}/tree", {"entries": ready[start:start + BATCH]})
            updated += answer["updated"]
        for entry in ready:
            remote[entry["path"]] = entry
        self.cache.save()
        return updated, sent


def push(config: Config, watch: bool = False, interval: float | None = None, command: list[str] | None = None,
         url: str | None = None, allow_http: bool = False) -> int:
    hub = connect(config, url, allow_http)
    name = project_name(config)
    pusher = Pusher(hub, config, name)
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
            updated, sent = pusher.once()
            failures = 0
            if updated or final:
                print(f"pushed {updated} files ({sent / 1e6:.1f} MB) to {hub.url}/p/{name}/", flush=True)
            return True
        except (HubError, OSError) as error:
            failures += 1
            print(f"warning: push failed ({error}); retrying", file=sys.stderr, flush=True)
            return False

    if not watching:
        if not sync(final=True):
            return 1
        return 0
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


# ------------------------------------------------------------ pull and fetch


def local_reports(config: Config) -> dict[str, Path]:
    reports = {"overview": config.reports_dir} if (config.reports_dir / "report.md").is_file() else {}
    if config.reports_dir.is_dir():
        for folder in sorted(config.reports_dir.iterdir()):
            if folder.is_dir() and (folder / "report.md").is_file() and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", folder.name):
                reports.setdefault(folder.name, folder)
    return reports


def pull_comments(config: Config, url: str | None = None, allow_http: bool = False) -> int:
    """Append comments written on the hub to each report's comments.jsonl (no duplicates)."""
    hub = connect(config, url, allow_http)
    name = project_name(config)
    total = 0
    for slug, folder in local_reports(config).items():
        status, remote = hub.json("GET", f"/api/v1/projects/{name}/comments/{slug}", expect=(200, 404))
        if status != 200 or not remote:
            continue
        log = folder / "knowledge" / "comments.jsonl"
        existing = {comments.canonical(row) for row in comments.read_log(log)}
        new = [row for row in remote if comments.canonical(row) not in existing]
        comments.append(log, new)
        if new:
            print(f"{slug}: {len(new)} new comment records -> {log.relative_to(config.project)}")
        total += len(new)
    print(f"pulled {total} comment records from {hub.url}")
    return total


def fetch(hub: Hub, name: str, destination: Path, site_dir: str = "generated/reports") -> int:
    """Download a project's latest files into destination (outputs/, reports/, generated/reports/)."""
    from .hub import link_target, valid_path

    _, entries = hub.json("GET", f"/api/v1/projects/{name}/tree")
    destination = destination.resolve()
    count = 0
    for entry in sorted(entries, key=lambda item: "link" in item):  # files first, then links
        path = entry["path"]
        if not valid_path(path):
            continue
        local = destination / (site_dir + path[len("site"):] if path.startswith("site/") else path)
        local.parent.mkdir(parents=True, exist_ok=True)
        if "link" in entry:
            if link_target(path, entry["link"]) and not local.exists() and not local.is_symlink():
                local.symlink_to(entry["link"])
            continue
        if local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == entry["sha256"]:
            continue
        temporary = local.with_name(f".{local.name}.fetch.tmp")
        hub.request("GET", f"/files/{name}/{quote(path)}", stream_to=temporary)
        digest = hashlib.sha256()
        with temporary.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        if digest.hexdigest() != entry["sha256"]:
            temporary.unlink()
            raise HubError(f"{path}: downloaded content does not match its SHA-256")
        os.replace(temporary, local)
        count += 1
    print(f"fetched {count} files of {name!r} into {destination}")
    return count
