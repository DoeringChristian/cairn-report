"""The HTTP client and folder helpers behind `Remote` and publishing.

`Client` talks to a remote (bearer token, HTTPS unless the host is local or `allow_http`); `scan` and `entries_for`
turn a folder into manifest entries (hashes from a store.hashes.HashCache); `upload_missing` sends only the blobs
the remote lacks. Nothing here reads the configuration or knows about repos.
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

TOKEN_ENV = "CAIRN_TOKEN"
SKIP_NAMES = {"__pycache__", ".DS_Store"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
BATCH = 5_000
BLOCK = 1 << 20  # uploads are sent in 1 MB blocks (http.client's default is 8 KB)
CONNECT_TIMEOUT = 10.0  # seconds to reach the remote at all: a host that does not answer fails fast


class _Body:
    """The chunks of a streamed response. Closing it (also before the first chunk, which a generator's `finally`
    would miss) closes the connection."""

    def __init__(self, response, connection):
        self.response, self.connection, self.closed = response, connection, False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        chunk = b"" if self.closed else self.response.read(1 << 20)
        if not chunk:
            self.close()
            raise StopIteration
        return chunk

    def close(self) -> None:
        self.closed = True
        self.response.close()
        self.connection.close()


class ClientError(Exception):
    """An HTTP answer the client did not expect; `status` None means there was no answer at all."""

    def __init__(self, message: str, status: int | None = None, headers: dict | None = None, body: bytes = b""):
        super().__init__(message)
        self.status, self.headers, self.body = status, headers or {}, body

    def answer(self) -> dict:
        """The server's JSON answer ({} if it sent none)."""
        try:
            value = json.loads(self.body)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    @property
    def unreachable(self) -> bool:
        """No answer, or a proxy saying the store is down: worth retrying later."""
        return self.status is None or self.status in (502, 503, 504)


def _no_answer(method: str, path: str, url: str, error: BaseException) -> ClientError:
    return ClientError(f"{method} {path}: the remote at {url} did not answer ({error.__class__.__name__}: {error})", None)


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
        return str(json.loads(data)["error"])  # the remote explains itself; the request is on the error (CAIRN_DEBUG=1)
    except (ValueError, KeyError, TypeError):
        return f"{method} {path}: HTTP {status} {data[:300].decode(errors='replace')}"


class Client:
    def __init__(self, url: str, token: str, allow_http: bool = False, timeout: float = 120):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise SystemExit(f"remote URL {url!r} must look like https://host[:port]")
        if parsed.path not in ("", "/"):
            raise SystemExit(f"remote URL {url!r}: serve the remote at the root of its host, without a path")
        if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS and not allow_http:
            raise SystemExit(f"refusing to send the token over plain HTTP to {parsed.hostname}; "
                             "use https://, an SSH tunnel to localhost, or [repo] allow_http (--allow-http)")
        self.url, self.parsed, self.token, self.timeout = url.rstrip("/"), parsed, token, timeout

    def _connection(self):
        """A connection, connected within CONNECT_TIMEOUT (or the timeout, if shorter); each later read or write may
        then take `timeout`. Raises OSError (or an HTTPException) when the remote cannot be reached."""
        reach = min(CONNECT_TIMEOUT, self.timeout)
        if self.parsed.scheme == "https":
            connection = http.client.HTTPSConnection(self.parsed.hostname, self.parsed.port, timeout=reach,
                                                     context=ssl.create_default_context(), blocksize=BLOCK)
        else:
            connection = http.client.HTTPConnection(self.parsed.hostname, self.parsed.port, timeout=reach, blocksize=BLOCK)
        try:
            connection.connect()
            connection.sock.settimeout(self.timeout)
        except BaseException:
            connection.close()
            raise
        return connection

    def request(self, method: str, path: str, body=None, headers: dict | None = None,
                expect=(200,), stream_to: Path | None = None):
        try:
            connection = self._connection()
        except (OSError, http.client.HTTPException) as error:
            raise _no_answer(method, path, self.url, error) from error
        try:
            connection.request(method, path, body=body, headers={**({"Authorization": f"Bearer {self.token}"} if self.token else {}), **(headers or {})})
            response = connection.getresponse()
            if response.status in expect and stream_to is not None:
                with stream_to.open("wb") as handle:
                    while chunk := response.read(1 << 20):
                        handle.write(chunk)
                return response.status, b""
            data = response.read()
        except (ConnectionError, TimeoutError, http.client.HTTPException) as error:
            raise _no_answer(method, path, self.url, error) from error
        except OSError as error:
            if stream_to is not None and isinstance(error, (PermissionError, FileNotFoundError, IsADirectoryError)):
                raise  # a local file problem, not the network
            raise _no_answer(method, path, self.url, error) from error
        finally:
            connection.close()
        if response.status == 401:
            raise ClientError("the remote rejected the token" if self.token else
                              "the remote needs a token: set CAIRN_TOKEN or [repo] token_file", 401)
        if response.status not in expect:
            raise ClientError(_message(method, path, response.status, data), response.status, body=data)
        return response.status, data

    def stream(self, path: str, headers: dict | None = None, expect=(200, 206)):
        """GET as (status, response headers, chunks); the connection closes when the chunks end or are closed."""
        try:
            connection = self._connection()
        except (OSError, http.client.HTTPException) as error:
            raise _no_answer("GET", path, self.url, error) from error
        try:
            try:
                connection.request("GET", path, headers={**({"Authorization": f"Bearer {self.token}"} if self.token else {}), **(headers or {})})
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                raise _no_answer("GET", path, self.url, error) from error
            found = {key.lower(): value for key, value in response.getheaders()}
            if response.status == 401:
                data = response.read()
                raise ClientError("the remote rejected the token" if self.token else
                                  "the remote needs a token: set CAIRN_TOKEN or [repo] token_file", 401, found)
            if response.status not in expect:
                raise ClientError(_message("GET", path, response.status, response.read()), response.status, found)
        except BaseException:
            connection.close()
            raise

        return response.status, found, _Body(response, connection)

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


def scan(local: Path, prefix: str, project: Path, exclude: list[str] = (), skip_folder=None) -> dict[str, dict]:
    """Every file and relative symlink under `local`, keyed `<prefix>/<path inside local>` (prefix may be "").

    `exclude` are gitignore-style globs on paths relative to the project folder `project` (`[repo] exclude`)."""
    found: dict[str, dict] = {}
    if not local.is_dir():
        return found
    excluded = exclusion(list(exclude))
    project = Path(project).resolve()
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


def entries_for(files: dict[str, dict], cache) -> tuple[list[dict], dict[str, Path]]:
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


def upload_missing(client: Client, sources: dict[str, tuple[Path, int]], cache=None, workers: int = 4,
                   errors: list | None = None, quiet: bool = False) -> tuple[set[str], int]:
    """Upload the contents the remote lacks. Returns (hashes that failed, bytes sent); `errors` collects why."""
    missing = client.missing(sorted(sources))
    failed: set[str] = set()

    def send(sha: str) -> int:
        file, size = sources[sha]
        try:
            client.upload(sha, file, size)
            return size
        except (ClientError, OSError) as error:  # changed while uploading, or a network error: retry next time
            if cache is not None:
                cache.forget(file)
            failed.add(sha)
            if errors is not None:
                errors.append(error)
            if not quiet:
                print(f"warning: {file} not uploaded ({error}); retrying next time", file=sys.stderr)
            return 0

    with ThreadPoolExecutor(workers) as pool:
        sent = sum(pool.map(send, sorted(missing)))
    return failed, sent
