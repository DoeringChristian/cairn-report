"""`GET /files/<project>/<name>[@selector]//<path>`: a stored file with ETag, Range, and a sandbox policy.

On Linux the body is sent without copying it through Python: the status line and headers go out through
ASGI, then the kernel copies the blob straight to the socket (`os.sendfile`, in a worker thread, so the
event loop never waits on the disk), and the response is finished through ASGI again. That needs uvicorn's
httptools protocol (its transport and its Content-Length bookkeeping are reached through the ASGI `send` it
hands the app; see `_zero_copy`); neither Starlette's FileResponse (64 KB reads) nor uvicorn (no
`zerocopysend` extension) does it on their own. Elsewhere (and with `CAIRN_SENDFILE=0`; see
`sendfile_enabled`) the body streams in 1 MB pieces read in worker threads, the next one read while the
current one is sent, with the server's backpressure between them. Small bodies
(at most `SMALL` bytes, e.g. most Range requests) are read in one piece, in the same worker-thread trip that
resolves the file.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import selectors
import sys
from urllib.parse import quote, unquote

from ..ranges import byte_range
from ..store import BadRange, refs
from ..store.tokens import Principal
from .auth import HttpError, allow

SMALL = 1 << 20
PIECE = 1 << 20
SEND_TIMEOUT = 300  # seconds a client may stall a download before it is dropped


def sendfile_enabled() -> bool:
    """`CAIRN_SENDFILE=1` or `0` decides; by default only on Linux, where sendfile is zero-copy. On macOS it is
    available but measured slower (bench/RESULTS.md): no faster for one download, and about a quarter slower for
    four parallel downloads of one blob, so the 1 MB pieces are used there."""
    if not hasattr(os, "sendfile"):
        return False
    choice = os.environ.get("CAIRN_SENDFILE", "")
    return choice == "1" if choice in ("0", "1") else sys.platform.startswith("linux")


def opened(store, project: str, name: str, selector: str | None, path: str, range_header: str | None):
    """Resolve, open, and range-check a file (blocking: run it in a worker thread).
    Returns (sha256, immutable, descriptor, first, last, total, data): a body of at most SMALL bytes is read
    right away (`data`, with the descriptor closed), so a small request takes one trip to a worker thread.
    Raises NotFound or BadRange."""
    entry, blob, immutable = store.open_file(project, name, selector, path)
    descriptor = os.open(blob, os.O_RDONLY)
    try:
        total = os.fstat(descriptor).st_size
        wanted = byte_range(range_header, total)
        if wanted is False:
            raise BadRange(total)
    except BaseException:
        os.close(descriptor)
        raise
    first, last = wanted or (0, total - 1)
    size = max(last - first + 1, 0)
    if size <= SMALL:
        try:
            return entry["sha256"], immutable, None, first, last, total, os.pread(descriptor, size, first) if size else b""
        finally:
            os.close(descriptor)
    return entry["sha256"], immutable, descriptor, first, last, total, None


def parse(raw_path: str) -> tuple[str, refs.Ref]:
    rest = unquote(raw_path)[len("/files/"):]
    project, _, remainder = rest.partition("/")
    head, separator, inner = remainder.partition("//")
    if not separator:
        raise HttpError(404, "file URLs look like /files/<project>/<name>@v<n>//<path>")
    return project, refs.parse(f"{project}:{head}//{inner}")


async def serve_file(app, request, principal: Principal, raw_path: str, query: dict):
    """The response for one file request (the app maps HttpError, NotFound, and BadRange to statuses)."""
    project, ref = parse(raw_path)
    allow(principal, project)
    loop = asyncio.get_running_loop()
    sha, immutable, descriptor, first, last, total, data = await loop.run_in_executor(
        app.executor, opened, app.store, project, ref.name, ref.selector, ref.path, request.headers.get("range"))
    etag = f'"{sha}"'
    headers = {"ETag": etag, "X-Content-Type-Options": "nosniff", "Accept-Ranges": "bytes",
               "Cache-Control": "private, max-age=31536000, immutable" if immutable else "no-cache",
               "Content-Security-Policy": "sandbox"}  # a pushed HTML file must not act as the store's origin
    if request.headers.get("if-none-match") == etag:
        if descriptor is not None:
            os.close(descriptor)
        return Body(304, headers, None, 0, 0, app)
    if query.get("download"):
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(ref.path.rsplit('/', 1)[-1])}"
    partial = (first, last) != (0, total - 1)
    if partial:
        headers["Content-Range"] = f"bytes {first}-{last}/{total}"
    headers["Content-Type"] = mimetypes.guess_type(ref.path.rsplit("/", 1)[-1])[0] or "application/octet-stream"
    return Body(206 if partial else 200, headers, descriptor, first, max(last - first + 1, 0), app, data)


def _send_blocking(socket_fd: int, file_fd: int, offset: int, count: int) -> int:
    """`os.sendfile` until `count` bytes are sent, waiting for the (non-blocking) socket when it is full."""
    sent = 0
    with selectors.DefaultSelector() as selector:
        selector.register(socket_fd, selectors.EVENT_WRITE)
        while count > 0:
            try:
                written = os.sendfile(socket_fd, file_fd, offset, min(count, 1 << 30))
            except (BlockingIOError, InterruptedError):
                written = None
            if written is None:
                if not selector.select(SEND_TIMEOUT):
                    raise TimeoutError("the client stopped reading")
                continue
            if written == 0:
                raise EOFError("the blob ended early")
            offset, count, sent = offset + written, count - written, sent + written
    return sent


class Body:
    """An ASGI response for a file: headers, then the bytes `first`..`first+size-1` of `descriptor` (closed after)."""

    def __init__(self, status: int, headers: dict, descriptor: int | None, first: int, size: int, app, data: bytes | None = None):
        self.status, self.headers, self.descriptor, self.first, self.size, self.app = status, headers, descriptor, first, size, app
        self.data = data  # the whole body, already read (small files and ranges)

    async def __call__(self, scope, receive, send) -> None:
        headers = [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in self.headers.items()]
        body = self.data if self.data is not None else b""
        headers.append((b"content-length", str(self.size if self.descriptor is not None else len(body)).encode()))
        try:
            await send({"type": "http.response.start", "status": self.status, "headers": headers})
            if self.descriptor is None:
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            loop = asyncio.get_running_loop()
            if sendfile_enabled() and await self._zero_copy(scope, send, loop):
                return
            position, remaining = self.first, self.size  # the next piece is read while this one is sent
            upcoming = loop.run_in_executor(self.app.executor, os.pread, self.descriptor, min(PIECE, remaining), position)
            try:
                while remaining > 0:
                    data = await upcoming
                    upcoming = None
                    if not data:
                        raise EOFError("the blob ended early")
                    position, remaining = position + len(data), remaining - len(data)
                    if remaining > 0:
                        upcoming = loop.run_in_executor(self.app.executor, os.pread, self.descriptor, min(PIECE, remaining), position)
                    await send({"type": "http.response.body", "body": data, "more_body": remaining > 0})
            finally:
                if upcoming is not None:  # the client went away: let the read finish before the descriptor closes
                    await asyncio.wait([upcoming])
        finally:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None

    async def _zero_copy(self, scope, send, loop) -> bool:
        """Send the body with `os.sendfile`; False (nothing sent yet) where the server does not allow it."""
        cycle = getattr(scope.get("cairn.send"), "__self__", None)
        if cycle is None or type(cycle).__module__ != "uvicorn.protocols.http.httptools_impl" \
                or not all(hasattr(cycle, name) for name in ("transport", "expected_content_length", "disconnected")):
            return False
        transport = cycle.transport
        socket = transport.get_extra_info("socket")
        if socket is None or getattr(cycle, "chunked_encoding", False) or cycle.expected_content_length != self.size:
            return False
        while transport.get_write_buffer_size():  # the headers must be on the wire before the kernel appends the body
            if transport.is_closing():
                return True
            await asyncio.sleep(0.0005)
        self.app.zero_copy_sends += 1
        try:
            sent = await loop.run_in_executor(self.app.executor, _send_blocking, socket.fileno(), self.descriptor, self.first, self.size)
        except (OSError, EOFError, TimeoutError):  # the client went away (or stalled): drop the connection
            cycle.disconnected = True
            transport.close()
            return True
        cycle.expected_content_length -= sent
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        return True
