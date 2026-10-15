"""The HTTP client and folder helpers behind `RemoteStore` and `publish`.

`Client` talks to a store server (bearer token, HTTPS unless the host is local or `allow_http`);
`scan`, `HashCache`, `entries_for` and `upload_missing` turn a folder into manifest entries and
send only the blobs the server lacks. Hashes are cached by size and modification time, so
repeated publishes reread only changed files.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from .config import Config

TOKEN_ENV = "CAIRN_STORE_TOKEN"
SKIP_NAMES = {"__pycache__", ".DS_Store"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
BATCH = 5_000


class ClientError(Exception):
    def __init__(self, message: str, status: int | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status, self.headers = status, headers or {}


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


def _message(method: str, path: str, status: int, data: bytes) -> str:
    """The server's own error text if it sent one (`{"error": ...}`), else the start of the body."""
    try:
        text = json.loads(data)["error"]
    except (ValueError, KeyError, TypeError):
        text = data[:300].decode(errors="replace")
    return f"{method} {path}: HTTP {status} {text}"


class Client:
    def __init__(self, url: str, token: str, allow_http: bool = False, timeout: float = 120):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise SystemExit(f"store URL {url!r} must look like https://host[:port]")
        if parsed.path not in ("", "/"):
            raise SystemExit(f"store URL {url!r}: serve the store at the root of its host, without a path")
        if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS and not allow_http:
            raise SystemExit(f"refusing to send the store token over plain HTTP to {parsed.hostname}; "
                             "use https://, an SSH tunnel to localhost, or allow_http (--allow-http)")
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
            connection.request(method, path, body=body, headers={**({"Authorization": f"Bearer {self.token}"} if self.token else {}), **(headers or {})})
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
            raise ClientError("the store rejected the token" if self.token else
                              "the store needs a token: set CAIRN_STORE_TOKEN or [store] token_file", 401)
        if response.status not in expect:
            raise ClientError(_message(method, path, response.status, data), response.status)
        return response.status, data

    def stream(self, path: str, headers: dict | None = None, expect=(200, 206)):
        """GET as (status, response headers, chunks); the connection closes when the chunks end or are closed."""
        connection = self._connection()
        try:
            connection.request("GET", path, headers={**({"Authorization": f"Bearer {self.token}"} if self.token else {}), **(headers or {})})
            response = connection.getresponse()
            found = {key.lower(): value for key, value in response.getheaders()}
            if response.status == 401:
                data = response.read()
                raise ClientError("the store rejected the token" if self.token else
                                  "the store needs a token: set CAIRN_STORE_TOKEN or [store] token_file", 401, found)
            if response.status not in expect:
                raise ClientError(_message("GET", path, response.status, response.read()), response.status, found)
        except BaseException:
            connection.close()
            raise

        def chunks():
            try:
                while chunk := response.read(1 << 20):
                    yield chunk
            finally:
                connection.close()

        return response.status, found, chunks()

    def json(self, method: str, path: str, value=None, expect=(200,)):
        body = None if value is None else json.dumps(value).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        status, data = self.request(method, path, body, headers, expect)
        return status, (json.loads(data) if data else None)

    def upload(self, sha: str, file: Path, size: int) -> None:
        with file.open("rb") as handle:
            self.request("PUT", f"/api/v2/blobs/{sha}", body=Limited(handle, size),
                         headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
                         expect=(200, 201))

    def missing(self, hashes: list[str]) -> set[str]:
        missing: set[str] = set()
        for start in range(0, len(hashes), BATCH):
            _, answer = self.json("POST", "/api/v2/blobs/missing", {"sha256": hashes[start:start + BATCH]})
            missing.update(answer["missing"])
        return missing


# ----------------------------------------------------------------- settings


def connect(config: Config | None, url: str | None = None, allow_http: bool = False) -> Client:
    """A client for the store `[store] url` names; the token comes from CAIRN_STORE_TOKEN or `[store] token_file`."""
    settings = config.store if config else {}
    url = url or settings.get("url")
    if not url:
        raise SystemExit("no store URL: set [store] url in cairn-report.toml")
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token and settings.get("token_file"):
        token_file = Path(settings["token_file"]).expanduser()
        if not token_file.is_absolute() and config:
            token_file = config.project / token_file
        if not token_file.is_file():
            raise SystemExit(f"store token file {token_file} does not exist")
        if token_file.stat().st_mode & 0o077:
            print(f"warning: {token_file} is readable by other users; run chmod 600 {token_file}", file=sys.stderr)
        token = token_file.read_text().strip()
    # without a token only a server started with --no-auth answers
    return Client(url, token, allow_http=allow_http or bool(settings.get("allow_http")))


def project_name(config: Config) -> str:
    name = config.store.get("project") or re.sub(r"[^a-z0-9_.-]+", "-", config.project.name.lower()).strip("-.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", name or ""):
        raise SystemExit(f"store project name {name!r} is not a valid slug; set [store] project in cairn-report.toml")
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

    `exclude` defaults to the `[store]` exclude globs."""
    found: dict[str, dict] = {}
    if not local.is_dir():
        return found
    excluded = exclusion(config.store.get("exclude", []) if exclude is None else exclude)
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
    """Manifest entries for scanned files, and a local source file for each content hash."""
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


def upload_missing(client: Client, sources: dict[str, tuple[Path, int]], cache: HashCache, workers: int = 4) -> tuple[set[str], int]:
    """Upload the contents the store lacks. Returns (hashes that failed, bytes sent)."""
    missing = client.missing(sorted(sources))
    failed: set[str] = set()

    def send(sha: str) -> int:
        file, size = sources[sha]
        try:
            client.upload(sha, file, size)
            return size
        except (ClientError, OSError) as error:  # changed while uploading, or a network error: retry next time
            cache.forget(file)
            failed.add(sha)
            print(f"warning: {file} not uploaded ({error}); retrying next time", file=sys.stderr)
            return 0

    with ThreadPoolExecutor(workers) as pool:
        sent = sum(pool.map(send, sorted(missing)))
    return failed, sent
