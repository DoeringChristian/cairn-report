"""`GET /files/<project>/<name>[@selector]//<path>`: a stored file with ETag, Range, and a sandbox policy."""

from __future__ import annotations

import mimetypes
from urllib.parse import quote, unquote

from ..store import refs
from .auth import HttpError
from ..store.tokens import Principal


class FilesMixin:
    """The file route of the handler (needs `store`, `headers`, `_allow`, `_send`, and the socket writers)."""

    def _files(self, principal: Principal, path: str, query: dict) -> None:
        rest = unquote(path)[len("/files/"):]
        project, _, remainder = rest.partition("/")
        head, separator, inner = remainder.partition("//")
        if not separator:
            raise HttpError(404, "file URLs look like /files/<project>/<name>@v<n>//<path>")
        ref = refs.parse(f"{project}:{head}//{inner}")
        self._allow(principal, project)
        read = self.store.read_file(project, ref.name, ref.selector, ref.path, self.headers.get("Range"))
        etag = f'"{read.sha256}"'
        headers = {"ETag": etag, "X-Content-Type-Options": "nosniff", "Accept-Ranges": "bytes",
                   "Cache-Control": "private, max-age=31536000, immutable" if read.immutable else "no-cache",
                   "Content-Security-Policy": "sandbox"}  # a pushed HTML file must not act as the store's origin
        if self.headers.get("If-None-Match") == etag:
            read.close()
            return self._send(304, b"", headers=headers)
        if query.get("download"):
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(ref.path.rsplit('/', 1)[-1])}"
        if read.partial:
            headers["Content-Range"] = f"bytes {read.first}-{read.last}/{read.total}"
        self.send_response(206 if read.partial else 200)
        self.send_header("Content-Type", mimetypes.guess_type(ref.path.rsplit("/", 1)[-1])[0] or "application/octet-stream")
        self.send_header("Content-Length", str(read.size))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        for chunk in read.chunks:
            self.wfile.write(chunk)
