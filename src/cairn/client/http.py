"""The HTTP client and folder helpers behind `Remote` and publishing.

`Client` talks to a server (bearer token, HTTPS unless the host is local or `allow_http`; the server may sit behind
a path prefix, `https://host/prefix`); `split_remote` is the one place that reads a remote URL `<server>/<project>`;
`scan` and `entries_for` turn a folder into manifest entries (hashes from a store.hashes.HashCache);
`upload_missing` sends only the blobs the remote lacks. Nothing here reads the configuration or knows about repos.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import posixpath
import re
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urlunparse

TOKEN_ENV = "CAIRN_TOKEN"
SKIP_NAMES = {"__pycache__", ".DS_Store"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
BATCH = 5_000
BLOCK = 1 << 20  # uploads are sent in 1 MB blocks (http.client's default is 8 KB)
CONNECT_TIMEOUT = 10.0  # seconds to reach the remote at all: a host that does not answer fails fast
IDLE = 20.0  # seconds an idle kept-alive connection is used again (a server keeps it 30 s)
FRESH = 1.0  # seconds idle after which a request whose body cannot be sent twice takes a new connection
KEEP = 4  # idle connections kept per remote (the uploader sends blobs in parallel)
REPLAYABLE = (bytes, bytearray, str, type(None))
SEND_BUFFER = 4 << 20  # bytes a connection's kernel send buffer may hold (see `_connection`)


class _Pool:
    """Kept-alive connections per remote (scheme, host, port), shared by the threads of the process: a request takes an
    idle one (or opens one) and gives it back when its answer was read whole, so a process pays the connection (and TLS
    handshake) once, not once per request."""

    def __init__(self):
        self.lock, self.idle = threading.Lock(), {}

    def take(self, key: tuple, fresh_only: bool) -> tuple[object | None, bool]:
        """(connection, reused); None when there is no idle one to use (then the caller opens one)."""
        now = time.monotonic()
        with self.lock:
            found = self.idle.get(key) or []
            while found:
                connection, since = found.pop()
                if now - since < (FRESH if fresh_only else IDLE):
                    return connection, True
                connection.close()
        return None, False

    def give(self, key: tuple, connection) -> None:
        with self.lock:
            found = self.idle.setdefault(key, [])
            if len(found) < KEEP:
                found.append((connection, time.monotonic()))
                return
        connection.close()


_POOL = _Pool()


# ------------------------------------------------------------------ waiting budgets

_BUDGET = threading.local()


@contextmanager
def budget(seconds: float, *, uploads: bool = False, default: bool = False):
    """Within the block, this thread's requests together wait for the remote at most `seconds` (nested budgets: the
    shorter wins); a request once the time is up fails at once, as a remote that does not answer. Blob transfers
    (uploads, downloads) are exempt unless `uploads`: they go on while bytes flow (each read or write still has its
    timeout, which is the stall timeout), and the time they take is added to the budget (`credit_transfer`), so it caps
    only the waiting. `default`: only when no budget is set already (an operation's own budget, which a command's or a
    run's phase overrides)."""
    outer = getattr(_BUDGET, "deadline", None), getattr(_BUDGET, "uploads", True)
    if default and outer[0] is not None:
        yield
        return
    deadline = time.monotonic() + seconds
    _BUDGET.deadline = deadline if outer[0] is None else min(outer[0], deadline)
    _BUDGET.uploads = uploads if outer[0] is None else outer[1] and uploads
    try:
        yield
    finally:
        _BUDGET.deadline, _BUDGET.uploads = outer


def credit_transfer(seconds: float) -> None:
    """A blob transfer that the budget exempts took `seconds`: the budget's deadline moves by as much."""
    _BUDGET.moved = transferring() + seconds
    if getattr(_BUDGET, "deadline", None) is not None and not getattr(_BUDGET, "uploads", True):
        _BUDGET.deadline += seconds


def transferring() -> float:
    """Seconds this thread spent transferring blobs so far (to tell a slow remote from a large transfer)."""
    return getattr(_BUDGET, "moved", 0.0)


def remaining(upload: bool = False) -> float | None:
    """Seconds this thread's budget leaves (None: no budget, or an upload it exempts)."""
    deadline = getattr(_BUDGET, "deadline", None)
    if deadline is None or (upload and not getattr(_BUDGET, "uploads", True)):
        return None
    return deadline - time.monotonic()


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


def split_remote(url: str) -> tuple[str, str | None]:
    """(server URL, project) of a remote URL: its last path segment is the project (`https://host/my-study`:
    `https://host` and `my-study`; `https://host/prefix/my-study`: `https://host/prefix`). Without a path the project
    is None (a server, not one of its projects)."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise SystemExit(f"remote URL {url!r} must look like https://host[:port][/prefix]/<project>")
    path = parsed.path.rstrip("/")
    if not path:
        return urlunparse(parsed._replace(path="")).rstrip("/"), None
    head, _, project = path.rpartition("/")
    return urlunparse(parsed._replace(path=head)).rstrip("/"), project


class Client:
    """HTTP to one server, `url` (with a path prefix if it sits behind one: requests go to `<prefix>/api/...`)."""

    def __init__(self, url: str, token: str, allow_http: bool = False, timeout: float = 120, token_source: str | None = None,
                 token_places: str | None = None):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
            raise SystemExit(f"server URL {url!r} must look like https://host[:port][/prefix]")
        if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS and not allow_http:
            raise SystemExit(f"refusing to send the token over plain HTTP to {parsed.hostname}; "
                             "use https://, an SSH tunnel to localhost, or [repo] allow_http (--allow-http)")
        self.url, self.parsed, self.token, self.timeout = url.rstrip("/"), parsed, token, timeout
        self.base = parsed.path.rstrip("/")  # the server's path prefix ("" at the root of its host)
        self.token_source = token_source  # where the token came from, for messages (never the token itself)
        self.token_places = token_places  # where a token was looked for, when none was found

    def refused(self, where: str | None = None) -> str:
        """What a 401 means, naming where the token came from (never the token): `where` is the remote's address."""
        where = where or self.url
        if self.token:
            return f"the token from {self.token_source or 'the caller'} was refused by {where}"
        return f"{where} needs a token, and none was found ({self.token_places or 'set CAIRN_TOKEN'})"

    def _headers(self, headers: dict | None) -> dict:
        return {**({"Authorization": f"Bearer {self.token}"} if self.token else {}), **(headers or {})}

    @property
    def _key(self) -> tuple:
        return self.parsed.scheme, self.parsed.hostname, self.parsed.port

    def _limit(self, method: str, path: str, upload: bool = False) -> float:
        """The timeout of the next read or write: `timeout`, or less when this thread's budget is nearly spent
        (ClientError, as a remote that does not answer, once it is spent)."""
        left = remaining(upload)
        if left is None:
            return self.timeout
        if left <= 0:
            raise ClientError(f"{method} {path}: the remote at {self.url} did not answer in time (out of time)", None)
        return min(self.timeout, left)

    def _connection(self, limit: float | None = None):
        """A connection, connected within CONNECT_TIMEOUT (or the timeout, if shorter); each later read or write may
        then take `timeout`. Raises OSError (or an HTTPException) when the remote cannot be reached."""
        limit = self.timeout if limit is None else limit
        reach = min(CONNECT_TIMEOUT, limit)
        if self.parsed.scheme == "https":
            connection = http.client.HTTPSConnection(self.parsed.hostname, self.parsed.port, timeout=reach,
                                                     context=ssl.create_default_context(), blocksize=BLOCK)
        else:
            connection = http.client.HTTPConnection(self.parsed.hostname, self.parsed.port, timeout=reach, blocksize=BLOCK)
        try:
            connection.connect()
            connection.sock.settimeout(limit)
            # bytes the kernel holds unsent count as sent: a bounded send buffer keeps "no answer within the timeout
            # after the body" the stall it means (the drain of a few MB), not the drain of a buffer that grew to many
            connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)
        except BaseException:
            connection.close()
            raise
        return connection

    def request(self, method: str, path: str, body=None, headers: dict | None = None,
                expect=(200,), stream_to: Path | None = None, upload: bool = False):
        """One request on a kept-alive connection (see `_Pool`). A kept-alive connection the remote closed meanwhile is
        noticed when the request fails on it: the request is sent once more on a new one (every request of the API is
        idempotent; a body that cannot be sent twice always goes on a connection used a moment ago, or a new one)."""
        replayable = isinstance(body, REPLAYABLE)
        transfer, began = upload or stream_to is not None, time.monotonic()
        try:
            return self._request(method, path, body, headers, expect, stream_to, transfer, replayable)
        finally:
            if transfer:
                credit_transfer(time.monotonic() - began)

    def _request(self, method, path, body, headers, expect, stream_to, transfer, replayable):
        for attempt in range(2):
            limit = self._limit(method, path, transfer)
            connection, reused = _POOL.take(self._key, fresh_only=not replayable) if attempt == 0 else (None, False)
            try:
                if connection is None:
                    connection = self._connection(limit)
                else:
                    connection.sock.settimeout(limit)
            except (OSError, http.client.HTTPException) as error:
                raise _no_answer(method, path, self.url, error) from error
            keep = False
            try:
                connection.request(method, self.base + path, body=body, headers=self._headers(headers))
                response = connection.getresponse()
                if response.status in expect and stream_to is not None:
                    with stream_to.open("wb") as handle:
                        while chunk := response.read(1 << 20):
                            handle.write(chunk)
                    data = b""
                else:
                    data = response.read()
                keep = not response.will_close
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as error:
                if reused and replayable and attempt == 0:  # the remote closed the idle connection: once more, anew
                    continue
                raise _no_answer(method, path, self.url, error) from error
            except (ConnectionError, TimeoutError, http.client.HTTPException) as error:
                raise _no_answer(method, path, self.url, error) from error
            except OSError as error:
                if stream_to is not None and isinstance(error, (PermissionError, FileNotFoundError, IsADirectoryError)):
                    raise  # a local file problem, not the network
                raise _no_answer(method, path, self.url, error) from error
            finally:
                if keep:
                    _POOL.give(self._key, connection)
                else:
                    connection.close()
            break
        if stream_to is not None and response.status in expect:
            return response.status, b""
        if response.status == 401:
            raise ClientError(self.refused(), 401)
        if response.status not in expect:
            raise ClientError(_message(method, path, response.status, data), response.status, body=data)
        return response.status, data

    def stream(self, path: str, headers: dict | None = None, expect=(200, 206)):
        """GET as (status, response headers, chunks); the connection closes when the chunks end or are closed."""
        try:
            connection = self._connection(self._limit("GET", path))  # its own: the chunks are read later, maybe never whole
        except (OSError, http.client.HTTPException) as error:
            raise _no_answer("GET", path, self.url, error) from error
        try:
            try:
                connection.request("GET", self.base + path, headers=self._headers(headers))
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                raise _no_answer("GET", path, self.url, error) from error
            found = {key.lower(): value for key, value in response.getheaders()}
            if response.status == 401:
                response.read()
                raise ClientError(self.refused(), 401, found)
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
                         expect=(200, 201), upload=True)

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


def _points_inside(path: str, target: str) -> bool:
    """Whether a symlink at `path` (inside the folder) with this target stays inside the folder (the rule the repo
    applies to stored links, store.blobs.link_target)."""
    if not target or target.startswith("/") or "\\" in target:
        return False
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return all(part not in ("", ".", "..") for part in resolved.split("/"))


def skipped(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(".tmp")


def scan(local: Path, prefix: str, project: Path, exclude: list[str] = (), skip_folder=None) -> dict[str, dict]:
    """Every file and symlink under `local` that points inside it (others are left out), keyed `<prefix>/<path inside
    local>` (prefix may be "").

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
                if _points_inside(key, target):  # a link out of the folder (absolute or not) is left out
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

    began = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:  # the workers have no budget: their timeout is the stall timeout
        sent = sum(pool.map(send, sorted(missing)))
    credit_transfer(time.monotonic() - began)  # the caller's budget caps waiting, not the transfer
    return failed, sent
