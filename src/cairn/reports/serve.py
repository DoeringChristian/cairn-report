"""Serve the built reports as a static site, with HTTP caching.

    /                  the root report (<build dir>/index.html)
    /<path>/           a child report (<build dir>/<path>/index.html)
    /_static/...       shared page files, named by content hash

Hashed (`_static/`) and versioned (`?v=`) files are cached for good, everything
else is revalidated with an ETag; gzip copies are sent when the browser accepts
them.
"""

from __future__ import annotations

import errno
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import build as builder
from ..config import Config, load_config
from ..ranges import byte_range

OUTPUT_ROOT = Path()  # set by serve()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        root = OUTPUT_ROOT.resolve()
        candidate = (root / path.lstrip("/")).resolve()
        if candidate != root and root not in candidate.parents:
            return self.send_error(404)
        if candidate.is_dir():
            if not path.endswith("/"):
                return self._send(301, b"", None, {"Location": path + "/"})
            candidate = candidate / "index.html"
        if not candidate.is_file():
            return self.send_error(404)
        self._file(candidate)

    def _file(self, path: Path) -> None:
        """A built file with HTTP caching: hashed or versioned URLs never change, everything else revalidates."""
        query = urlparse(self.path).query
        immutable = "/_static/" in self.path or re.search(r"(^|&)v=[0-9a-f]+", query) is not None
        info = path.stat()
        etag = f'"{info.st_mtime_ns:x}-{info.st_size:x}"'
        headers = {
            "ETag": etag,
            "Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache",
            "Vary": "Accept-Encoding",
        }
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", None, headers)
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        compressed = path.with_name(path.name + ".gz")
        if "gzip" in self.headers.get("Accept-Encoding", "") and compressed.is_file():
            headers["Content-Encoding"] = "gzip"
            return self._send(200, compressed.read_bytes(), kind, headers)
        headers["Accept-Ranges"] = "bytes"  # videos seek with range requests
        wanted = byte_range(self.headers.get("Range"), info.st_size)
        if wanted is False:
            return self._send(416, b"", None, headers | {"Content-Range": f"bytes */{info.st_size}"})
        if wanted is None:
            return self._send(200, path.read_bytes(), kind, headers)
        first, last = wanted
        with path.open("rb") as handle:
            handle.seek(first)
            body = handle.read(last - first + 1)
        self._send(206, body, kind, headers | {"Content-Range": f"bytes {first}-{last}/{info.st_size}"})

    def _send(self, status: int, body: bytes, kind: str | None, headers: dict) -> None:
        self.send_response(status)
        if kind:
            self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)


def serve(config: Config | None = None, host: str = "127.0.0.1", port: int = 8000,
          strict_port: bool = False, build: bool = True) -> None:
    """Build (unless build=False) and serve; move to the next free port unless strict_port."""
    global OUTPUT_ROOT
    config = config or load_config()
    OUTPUT_ROOT = config.output_dir
    try:
        _serve(config, host, port, strict_port, build)
    except KeyboardInterrupt:
        pass


def _serve(config: Config, host: str, port: int, strict_port: bool, build: bool) -> None:
    if build:
        builder.build_all(config)
    server, chosen = None, port
    for chosen in [port] if strict_port else range(port, port + 50):
        try:
            server = ThreadingHTTPServer((host, chosen), Handler)  # bind before announcing the URL
            break
        except OSError as error:
            if strict_port or error.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise SystemExit(f"cairn: cannot serve on {host}:{chosen}: {error.strerror}")
    if server is None:
        raise SystemExit(f"cairn: no free port in {port}-{port + 49} on {host}; choose one with --port")
    if chosen != port:
        print(f"port {port} in use; using {chosen}", flush=True)
    print(f"reports  http://{host}:{chosen}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
