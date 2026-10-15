"""The store server: a LocalStore root shared over HTTP, behind tokens.

    cairn-report store serve <root> [--host 127.0.0.1] [--port 8800]

A new root gets a fresh owner token on first launch; every launch prints `Store: <url>` and
`Token: <owner token>` to the terminal (never into a URL). Scoped tokens (one project, read or
write) are made with `cairn-report store token create` (see tokens.py). Bind localhost, and put an
HTTPS reverse proxy in front of anything else: the server speaks plain HTTP.

Artifact names are slash paths (`datasets/crops`), so every route says where the name ends:

    API (JSON, `Authorization: Bearer <token>`; a name is ONE percent-encoded path segment, `datasets%2Fcrops`)
      GET  /api/v2/projects                                      POST {name, title?, description?}
      GET  /api/v2/projects/<p>                                  PATCH {title?, description?}
      GET  /api/v2/projects/<p>/artifacts                        [{name, latest, versions, aliases, updated}]
      GET  /api/v2/projects/<p>/artifacts/<name>/versions        every version, full manifests
      GET  /api/v2/projects/<p>/artifacts/<name>/versions/<n>    one manifest
      GET  /api/v2/projects/<p>/artifacts/<name>/aliases         {alias: version}
      POST /api/v2/projects/<p>/artifacts/<name>/versions        {label?, metadata?, source?} -> {version} (running)
      POST .../versions/<n>/files {entries}   (409 + `missing` when blobs are absent)   .../complete {inputs?}   .../fail
      POST /api/v2/projects/<p>/artifacts/<name>/publish         {entries, label?, metadata?, inputs?, aliases?, source?}
                                                                 -> {version, created}  (created=false: nothing changed)
      POST /api/v2/projects/<p>/artifacts/<name>/aliases         {alias, version}
      GET  /api/v2/resolve?ref=<ref>                             {project, name, version: manifest}
      GET  /api/v2/lineage?ref=<ref>                             {ref, inputs, consumers}
      POST /api/v2/blobs/missing {sha256: [...]}                 PUT /api/v2/blobs/<sha256> (streamed, hash-verified)
    Files (the name ends at `@`; the path inside the artifact follows `//`)
      GET  /files/<p>/<name>[@v<n>|@alias|@label]//<path>        ETag, Range, immutable for `@v<n>` of a completed
                                                                 version, `?download=1` for an attachment
    Pages (browser; the cookie from /login, any valid token)
      /                                      projects
      /p/<p>/                                 the project: reports, then other artifacts
      /p/<p>/a/<name>[@v<n>|@alias]           an artifact: versions, files, metadata, inputs, consumers
      /p/<p>/r/<slug>[@v<n>]/...              a report version's built site (latest without @), its `_static/`
                                              files and a generated `site.json` at /p/<p>/r/

References in the API are `project:name[@selector]` (the project is required: the server has no default).

Cookies are accepted for pages and files only, never for the API. Everything stored is append-only
and the server never executes pushed code. Pushed HTML is served as is: write tokens belong to
people whose reports you trust.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import re
import sys
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import __version__
from ..ranges import byte_range
from . import pages, refs
from .blobs import CHUNK, SHA256, clean_entries, resolve_entry, valid_path
from .local import LocalStore, NotFound, StoreError
from .refs import Ref
from .tokens import OWNER, Principal, Tokens, init_root, read_token

COOKIE = "cairn_store"
SESSION_LABEL = b"cairn-store-session-v1"
MAX_JSON = 64 * 1024 * 1024
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def safe_target(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


class StoreHandler(BaseHTTPRequestHandler):
    store: LocalStore
    owner_token: str = ""
    tokens: Tokens
    secure_cookie: bool = True
    no_auth: bool = False  # --no-auth: every request acts as the owner (local testing only)
    max_blob: int = 0
    static_cache: dict
    server_version = f"cairn-store/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------- auth

    def _principal_for(self, token: str) -> Principal | None:
        if hmac.compare_digest(token.encode(), self.owner_token.encode()):
            return OWNER
        return self.tokens.find(token)

    def _mac(self, identifier: str) -> str:
        return hmac.new(self.owner_token.encode(), SESSION_LABEL + b":" + identifier.encode(), hashlib.sha256).hexdigest()

    def _bearer(self) -> Principal | None:
        if self.no_auth:
            return OWNER
        header = self.headers.get("Authorization", "")
        return self._principal_for(header[7:].strip()) if header.startswith("Bearer ") else None

    def _cookie(self) -> Principal | None:
        """The session cookie is `<token id>.<HMAC of the id>`: the token itself is never in it, and a revoked token stops working."""
        if self.no_auth:
            return OWNER
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        if not morsel:
            return None
        identifier, _, mac = morsel.value.partition(".")
        if not hmac.compare_digest(mac.encode(), self._mac(identifier).encode()):
            return None
        return OWNER if identifier == OWNER.id else self.tokens.by_id(identifier)

    @staticmethod
    def _allow(principal: Principal, project: str, write: bool = False) -> None:
        if not principal.can(project, write):
            raise HttpError(403, f"this token cannot {'write' if write else 'read'} project {project!r}")

    def _cookie_header(self, value: str, max_age: int) -> str:
        secure = "; Secure" if self.secure_cookie else ""
        return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}"

    # --------------------------------------------------------------- dispatch

    def _run(self, action) -> None:
        try:
            action()
        except HttpError as error:
            self._fail(error.status, error.message)
        except NotFound as error:
            self._fail(404, str(error))
        except StoreError as error:
            self._fail(409 if "can no longer change" in str(error) else 400, str(error))
        except ValueError as error:  # invalid names, entries, JSON
            self._fail(400, str(error))

    def _fail(self, status: int, message: str) -> None:
        if urlparse(self.path).path.startswith("/api/"):
            return self._json(status, {"error": message})
        self._send(status, pages.message_page(f"Error {status}", message), "text/html; charset=utf-8")

    def do_GET(self):
        self._run(self._get)

    def do_POST(self):
        self._run(self._post)

    def do_PATCH(self):
        self._run(self._patch)

    def do_PUT(self):
        self._run(self._put)

    @staticmethod
    def _segments(path: str) -> list[str]:
        return [unquote(part) for part in path.split("/")]

    def _get(self) -> None:
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/login":
            return self._send(200, pages.login_page(safe_target(query.get("next", ["/"])[0])), "text/html; charset=utf-8")
        if path == "/logout":
            return self._send(303, b"", headers={"Location": "/login", "Set-Cookie": self._cookie_header("", 0)})
        api = path.startswith("/api/")
        principal = self._bearer() if api else (self._bearer() or self._cookie())
        if principal is None:
            if api:
                raise HttpError(401, "missing or invalid token")
            return self._send(303, b"", headers={"Location": "/login?next=" + quote(self.path)})
        segments = self._segments(path)
        if api:
            if segments[1:3] != ["api", "v2"]:
                raise HttpError(404, "the API lives under /api/v2/")
            return self._api_get(principal, segments[3:], query)
        if path == "/":
            return self._send(200, pages.index_page(self.store, principal), "text/html; charset=utf-8")
        if segments[1] == "files":
            return self._files(principal, path, query)
        if segments[1] == "p" and len(segments) >= 3:
            return self._pages(principal, segments, url)
        raise HttpError(404, "no such page")

    # -------------------------------------------------------------------- API

    def _version(self, project: str, name: str, selector: str | None) -> dict:
        """A version manifest: `v<n>` exactly (whatever its status), else the completed version an alias or label names."""
        if selector and refs.VERSION.fullmatch(selector):
            return self.store.get_version(project, name, int(selector[1:]))
        return self.store.resolve(Ref(project, name, selector))[2]

    def _ref(self, query: dict, principal: Principal) -> Ref:
        ref = refs.parse(query.get("ref", [""])[0])
        if ref.project is None:
            raise ValueError("the reference needs a project: project:name[@selector]")
        self._allow(principal, ref.project)
        return ref

    def _api_get(self, principal: Principal, route: list[str], query: dict) -> None:
        store = self.store
        match route:
            case ["projects"]:
                return self._json(200, [pages.overview(store, item) for item in store.projects() if principal.can(item["name"])])
            case ["projects", project]:
                self._allow(principal, project)
                return self._json(200, store.get_project(project))
            case ["projects", project, "artifacts"]:
                self._allow(principal, project)
                store.get_project(project)
                return self._json(200, [store.summary(project, name) for name in store.artifacts(project)])
            case ["projects", project, "artifacts", name, "versions"]:
                self._allow(principal, project)
                return self._json(200, store.versions(project, name))
            case ["projects", project, "artifacts", name, "versions", number] if number.isdigit():
                self._allow(principal, project)
                return self._json(200, store.get_version(project, name, int(number)))
            case ["projects", project, "artifacts", name, "aliases"]:
                self._allow(principal, project)
                return self._json(200, store.aliases(project, name))
            case ["resolve"]:
                ref = self._ref(query, principal)
                project, name, version = store.resolve(ref)
                return self._json(200, {"project": project, "name": name, "version": version})
            case ["lineage"]:
                ref = self._ref(query, principal)
                project, name, version = store.resolve(ref)
                found = store.lineage(Ref(project, name, f"v{version['version']}"))
                visible = {key: [item for item in items if principal.can(refs.parse(item).project)] for key, items in found.items()}
                return self._json(200, {"ref": refs.exact(project, name, version["version"]), **visible})
        raise HttpError(404, "no such API route")

    def _post(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            return self._login()
        principal = self._bearer()
        if principal is None:
            raise HttpError(401, "missing or invalid token")
        segments = self._segments(path)
        if segments[1:3] != ["api", "v2"]:
            raise HttpError(404, "the API lives under /api/v2/")
        route, payload = segments[3:], self._payload(MAX_JSON)
        store = self.store
        match route:
            case ["blobs", "missing"]:
                if not principal.write:
                    raise HttpError(403, "this token cannot write")
                hashes = payload.get("sha256", [])
                if not isinstance(hashes, list) or len(hashes) > 200_000 or not all(isinstance(h, str) and SHA256.fullmatch(h) for h in hashes):
                    raise ValueError("sha256 must be a list of hex digests")
                return self._json(200, {"missing": [h for h in hashes if not store.has_blob(h)]})
            case ["projects"]:
                name = payload.get("name")
                exists = isinstance(name, str) and any(item["name"] == name for item in store.projects())
                if principal.project is not None and not (exists and principal.project == name):
                    raise HttpError(403, "this token cannot create projects")
                record = store.create_project(name, payload.get("title"), payload.get("description") or "")
                return self._json(200 if exists else 201, record)
            case ["projects", project, "artifacts", name, "versions"]:
                self._allow(principal, project, write=True)
                source = payload.get("source")
                if source is not None and not isinstance(source, dict):
                    raise ValueError("source must be an object")
                label = payload.get("label")
                if label is not None and not isinstance(label, str):
                    raise ValueError("label must be text")
                number = store.start_version(project, name, label, payload.get("metadata"), source)
                return self._json(201, {"version": number})
            case ["projects", project, "artifacts", name, "versions", number, action] if number.isdigit():
                self._allow(principal, project, write=True)
                number = int(number)
                if action == "files":
                    entries = clean_entries(payload.get("entries"))
                    missing = store.missing(entries)
                    if missing:
                        return self._json(409, {"error": "upload these blobs first", "missing": missing})
                    store.add_files(project, name, number, entries)
                    return self._json(200, {"files": len(entries)})
                if action == "complete":
                    store.complete_version(project, name, number, self._inputs(principal, project, payload.get("inputs")))
                    return self._json(200, {"version": number})
                if action == "fail":
                    store.fail_version(project, name, number)
                    return self._json(200, {"version": number})
            case ["projects", project, "artifacts", name, "publish"]:
                self._allow(principal, project, write=True)
                entries = clean_entries(payload.get("entries"))
                missing = store.missing(entries)
                if missing:
                    return self._json(409, {"error": "upload these blobs first", "missing": missing})
                aliases = payload.get("aliases") or []
                if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
                    raise ValueError("aliases must be a list of names")
                if payload.get("source") is not None and not isinstance(payload["source"], dict):
                    raise ValueError("source must be an object")
                before = {version["version"] for version in store.versions(project, name)}
                number = store.publish(project, name, entries, label=payload.get("label"), metadata=payload.get("metadata"),
                                       inputs=self._inputs(principal, project, payload.get("inputs")), aliases=aliases,
                                       source=payload.get("source"))
                created = number not in before
                return self._json(201 if created else 200, {"version": number, "created": created})
            case ["projects", project, "artifacts", name, "aliases"]:
                self._allow(principal, project, write=True)
                alias, number = payload.get("alias"), payload.get("version")
                if not isinstance(alias, str) or isinstance(number, bool) or not isinstance(number, int):
                    raise ValueError("an alias is {alias: text, version: number}")
                store.set_alias(project, name, alias, number)
                return self._json(200, {"alias": alias, "version": number})
        raise HttpError(404, "no such API route")

    def _patch(self) -> None:
        principal = self._bearer()
        if principal is None:
            raise HttpError(401, "missing or invalid token")
        segments = self._segments(urlparse(self.path).path)
        payload = self._payload(MAX_JSON)
        if segments[1:4] == ["api", "v2", "projects"] and len(segments) == 5:
            project = segments[4]
            self._allow(principal, project, write=True)
            for key in ("title", "description"):
                if payload.get(key) is not None and not isinstance(payload[key], str):
                    raise ValueError(f"{key} must be text")
            return self._json(200, self.store.update_project(project, payload.get("title"), payload.get("description")))
        raise HttpError(404, "no such API route")

    def _inputs(self, principal: Principal, project: str, items) -> list[str]:
        """Input references with the version's project filled in; a scoped token may only name its own project."""
        if items is None:
            return []
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValueError("inputs must be a list of references")
        qualified = []
        for item in items:
            ref = refs.parse(item)
            ref = Ref(ref.project or project, ref.name, ref.selector, ref.path)
            self._allow(principal, ref.project)
            qualified.append(str(ref))
        return qualified

    def _put(self) -> None:
        match = self._segments(urlparse(self.path).path)
        principal = self._bearer()
        if principal is None:
            raise HttpError(401, "missing or invalid token")
        if match[1:4] != ["api", "v2", "blobs"] or len(match) != 5 or not SHA256.fullmatch(match[4]):
            raise HttpError(404, "no such API route")
        if not principal.write:
            raise HttpError(403, "this token cannot write")
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise HttpError(411, "Content-Length required")
        if size < 0 or size > self.max_blob:
            raise HttpError(413, f"blob larger than {self.max_blob} bytes")
        created = self.store.put_blob(match[4], self.rfile, size)
        self._json(201 if created else 200, {"sha256": match[4], "created": created})

    # ------------------------------------------------------------------ files

    def _files(self, principal: Principal, path: str, query: dict) -> None:
        rest = unquote(path)[len("/files/"):]
        project, _, remainder = rest.partition("/")
        head, separator, inner = remainder.partition("//")
        if not separator:
            raise HttpError(404, "file URLs look like /files/<project>/<name>@v<n>//<path>")
        ref = refs.parse(f"{project}:{head}//{inner}")
        self._allow(principal, project)
        version = self._version(project, ref.name, ref.selector)
        entry = resolve_entry(version["files"], ref.path)
        if entry is None:
            raise HttpError(404, f"{ref.name} has no file {ref.path}")
        immutable = ref.version is not None and version["status"] == "completed"
        self._blob(entry, ref.path, download=bool(query.get("download")), immutable=immutable, sandbox=True)

    # ------------------------------------------------------------------ pages

    def _pages(self, principal: Principal, segments: list[str], url) -> None:
        project = segments[2]
        self._allow(principal, project)
        self.store.get_project(project)
        if len(segments) == 3:
            return self._send(301, b"", headers={"Location": url.path + "/"})
        kind = segments[3]
        if kind == "" and len(segments) == 4:
            return self._send(200, pages.project_page(self.store, project), "text/html; charset=utf-8")
        if kind == "a":
            name, at, selector = "/".join(segments[4:]).rstrip("/").partition("@")
            if not name:
                raise HttpError(404, "no such page")
            chosen = self._version(project, name, selector) if at else None
            return self._send(200, pages.artifact_page(self.store, principal, project, name, chosen), "text/html; charset=utf-8")
        if kind == "r":
            return self._report(project, segments[4:], url)
        raise HttpError(404, "no such page")

    def _report(self, project: str, rest: list[str], url) -> None:
        first = rest[0] if rest else ""
        if first == "":
            return self._send(303, b"", headers={"Location": pages.project_href(project)})
        if first == "site.json" and len(rest) == 1:
            return self._json(200, self._site(project))
        if first == "_static" and len(rest) == 2:
            return self._static(project, rest[1])
        slug, at, selector = first.partition("@")
        name = f"reports/{slug}"
        refs.valid_name(name)
        if len(rest) == 1:  # relative links in the page need the trailing slash
            return self._send(301, b"", headers={"Location": url.path + "/" + (f"?{url.query}" if url.query else "")})
        version = self._version(project, name, selector if at else None)
        inside = "/".join(rest[1:])
        file = f"site/{slug}/{inside}index.html" if inside.endswith("/") or not inside else f"site/{slug}/{inside}"
        if not valid_path(file):
            raise HttpError(404, "no such file")
        entry = resolve_entry(version["files"], file)
        if entry is None:
            raise HttpError(404, f"the report has no {file}")
        immutable = re.search(r"(^|&)v=[0-9a-f]+", url.query) is not None
        compressed = resolve_entry(version["files"], file + ".gz")
        if compressed and "gzip" in self.headers.get("Accept-Encoding", ""):
            return self._blob(compressed, file, immutable=immutable, encoding="gzip", kind=entry_type(file))
        self._blob(entry, file, immutable=immutable)

    def _static(self, project: str, file: str) -> None:
        """A shared page file (`site/_static/<name>`, named by content hash): the newest report version that has it."""
        if not valid_path(file) or "/" in file:
            raise HttpError(404, "no such file")
        key = (project, file)
        if key not in self.static_cache:
            found = None
            for name in self.store.artifacts(project):
                for version in reversed(self.store.versions(project, name)) if pages.is_report(name) else ():
                    entry = resolve_entry(version["files"], f"site/_static/{file}") if version["status"] == "completed" else None
                    if entry:
                        found = (entry, resolve_entry(version["files"], f"site/_static/{file}.gz"))
                        break
                if found:
                    break
            if found is None:
                raise HttpError(404, "no such file")
            self.static_cache[key] = found
        entry, compressed = self.static_cache[key]
        if compressed and "gzip" in self.headers.get("Accept-Encoding", ""):
            return self._blob(compressed, file, immutable=True, encoding="gzip", kind=entry_type(file))
        self._blob(entry, file, immutable=True)

    def _site(self, project: str) -> dict:
        """What a report page's navigation reads (site.json): the project's reports, latest versions."""
        reports = []
        for name in self.store.artifacts(project):
            head = pages.latest_version(self.store, project, name) if pages.is_report(name) else None
            if head:
                slug, meta = name.split("/", 1)[1], head["metadata"]
                reports.append({"key": slug, "title": meta.get("title") or slug, "subtitle": "", "status": meta.get("status", ""),
                                "summary": meta.get("summary", ""), "href": f"{slug}/", "updated": head.get("completed", "")})
        reports.sort(key=lambda item: (item["title"].lower(), item["key"]))
        return {"title": self.store.get_project(project)["title"], "reports": reports, "copy_preamble": ""}

    # ------------------------------------------------------------------ login

    def _login(self) -> None:
        form = parse_qs(self._body(4096).decode())
        given = form.get("token", [""])[0].strip()
        target = safe_target(form.get("next", ["/"])[0])
        principal = self._principal_for(given) if given else None
        if principal is None:
            time.sleep(1)  # slows down guessing
            return self._send(401, pages.login_page(target, failed=True), "text/html; charset=utf-8")
        value = f"{principal.id}.{self._mac(principal.id)}"
        self._send(303, b"", headers={"Location": target, "Set-Cookie": self._cookie_header(value, 30 * 86400)})

    # ------------------------------------------------------------------- I/O

    def _body(self, limit: int, allow_empty: bool = False) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        if size > limit or size < 0 or (size == 0 and not allow_empty):
            raise ValueError("invalid payload size")
        return self.rfile.read(size) if size else b""

    def _payload(self, limit: int) -> dict:
        value = json.loads(self._body(limit, allow_empty=True) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON object")
        return value

    def _blob(self, entry: dict, path: str, download: bool = False, immutable: bool = False, encoding: str | None = None,
              sandbox: bool = False, kind: str | None = None) -> None:
        etag = f'"{entry["sha256"]}"'
        headers = {"ETag": etag, "X-Content-Type-Options": "nosniff", "Vary": "Accept-Encoding",
                   "Cache-Control": "private, max-age=31536000, immutable" if immutable else "no-cache"}
        if sandbox:  # a pushed HTML file must not act as the store's origin (its cookie)
            headers["Content-Security-Policy"] = "sandbox"
        if encoding:
            headers["Content-Encoding"] = encoding
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", headers=headers)
        blob = self.store.blob_path(entry["sha256"])
        kind = kind or entry_type(path)
        if download:
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.rsplit('/', 1)[-1])}"
        size = blob.stat().st_size
        wanted = None if encoding else byte_range(self.headers.get("Range"), size)  # videos seek with ranges
        if not encoding:
            headers["Accept-Ranges"] = "bytes"
        if wanted is False:
            return self._send(416, b"", headers=headers | {"Content-Range": f"bytes */{size}"})
        first, last = wanted or (0, size - 1)
        if wanted:
            headers["Content-Range"] = f"bytes {first}-{last}/{size}"
        self.send_response(206 if wanted else 200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(last - first + 1))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        with blob.open("rb") as handle:
            handle.seek(first)
            remaining = last - first + 1
            while remaining > 0:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send(self, status: int, body: bytes, kind: str | None = None, headers: dict | None = None) -> None:
        if status >= 400:
            self.close_connection = True  # an unread request body must not be parsed as the next request
        self.send_response(status)
        if kind:
            self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value).encode(), "application/json", {"Cache-Control": "no-store"})

    def log_message(self, format, *args):  # one line per request, without the token
        sys.stderr.write(f"{self.log_date_time_string()} {self.address_string()} {format % args}\n")


def entry_type(path: str) -> str:
    return mimetypes.guess_type(path.rsplit("/", 1)[-1])[0] or "application/octet-stream"


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8800, max_blob: int = 50 * 1024**3,
                secure_cookie: bool = True, no_auth: bool = False) -> ThreadingHTTPServer:
    """A bound store server (not yet serving) for a root that has its owner token. With no_auth every
    request acts as the owner, so it is refused on any address but this machine's loopback."""
    if no_auth and host not in LOCAL_HOSTS:
        raise SystemExit("--no-auth serves every project to anyone who can connect: only with a loopback --host")
    root = root.resolve()
    if not (root / "token").is_file():
        raise SystemExit(f"{root} is not a store root; `cairn-report store serve {root}` creates it")
    handler = type("Handler", (StoreHandler,), {
        "store": LocalStore(root),
        "owner_token": read_token(root / "token"),
        "tokens": Tokens(root),
        "secure_cookie": secure_cookie,
        "no_auth": no_auth,
        "max_blob": max_blob,
        "static_cache": {},
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve_store(root: Path, host: str = "127.0.0.1", port: int = 8800, max_blob: int = 50 * 1024**3,
                secure_cookie: bool = True, no_auth: bool = False) -> None:
    """Serve a store; a new root (no token yet) is initialized first. Prints the owner token, which is the
    login for browsers and clients, to the terminal only: never into a URL."""
    created = None if (root / "token").exists() else init_root(root)
    server = make_server(root, host, port, max_blob=max_blob, secure_cookie=secure_cookie, no_auth=no_auth)
    if host not in LOCAL_HOSTS:
        print("warning: the store serves plain HTTP; expose it only through an HTTPS reverse proxy or a tunnel", flush=True)
    if created:
        print(f"Created store {root.resolve()} with a new owner token ({created}, owner-only)", flush=True)
    print(f"Store: http://{host}:{server.server_address[1]}/ (data in {root.resolve()})", flush=True)
    if no_auth:
        print("Authentication is off (--no-auth): every request acts as the owner; for local testing only", flush=True)
        server.serve_forever()
        return
    print(f"Token: {server.RequestHandlerClass.owner_token}", flush=True)
    print("  log in with it in the browser; clients read it from CAIRN_STORE_TOKEN or a token_file;"
          " `cairn-report store token create` makes project-scoped tokens", flush=True)
    server.serve_forever()
