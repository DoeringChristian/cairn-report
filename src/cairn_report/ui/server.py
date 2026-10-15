"""The store UI: a browser view of an artifact store, as a process of its own.

    cairn-report ui [--store URL|PATH] [--host 127.0.0.1] [--port 8801] [--no-auth] [--allow-http] [--insecure-cookies]

It reaches the store only through the store interface (`RemoteStore` for a store server, `LocalStore` for
a folder), so it runs next to the store or on a laptop against a remote one. Pages (see pages.py):

    /                                      projects
    /p/<p>/                                the project: reports, then other artifacts
    /p/<p>/a/<name>[@v<n>|@alias]          an artifact: versions, files, metadata, inputs, consumers
    /p/<p>/r/<slug>[@v<n>]/...             a report version's built site (latest without @), its `_static/`
                                           files and a generated `site.json` at /p/<p>/r/
    /files/<p>/<name>@sel//<path>          the store's files, with Range (so videos seek)

Reports are a UI concept: an artifact `reports/<slug>` whose files have `site/` and `source/`.

Login: the user enters a store token on /login. The UI checks it against the store (a remote store: an
authenticated API call; a folder: the folder's owner token file and tokens.jsonl), keeps it server-side in
an in-memory session table, and gives the browser only a random session id (HttpOnly, SameSite=Strict
cookie, Secure unless --insecure-cookies). Every request of a session uses its token, so a scoped token sees
only its project. `--no-auth` (loopback only) skips the login and uses `CAIRN_STORE_TOKEN` / `[store]
token_file` for a remote store, or the owner's full access to a folder.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import __version__
from ..push import Client
from ..store import BadRange, LocalStore, NotFound, RemoteStore, StoreError, refs
from ..store.blobs import resolve_entry, valid_path
from ..store.local import FileRead, Forbidden, Unauthorized
from ..store.refs import Ref
from ..store.tokens import OWNER, Principal, Tokens, read_token
from . import pages

COOKIE = "cairn_ui"
SESSION_SECONDS = 30 * 86400
MAX_SESSIONS = 1000
LOGIN_DELAY = 1.0  # seconds a wrong token waits, which slows down guessing
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Session:
    """What a browser session acts as: a store handle (carrying the user's token) and, for a folder, its principal."""
    store: LocalStore | RemoteStore
    principal: Principal | None  # None: no restriction here (a remote store enforces its own)
    expires: float = 0.0
    cache: dict = field(default_factory=dict)


class LocalBackend:
    """A store folder. Login tokens are the folder's owner token and its scoped tokens."""

    def __init__(self, root: Path, no_auth: bool):
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"no store folder {root}")
        self.root, self.describe = root, str(root)
        self.store = LocalStore(root)
        if not no_auth:
            if not (root / "token").is_file():
                raise SystemExit(f"{root} has no owner token (not a store root?); use --no-auth to browse it without a login")
            self.owner, self.tokens = read_token(root / "token"), Tokens(root)

    def anonymous(self) -> Session:
        return Session(self.store, None)

    def login(self, token: str) -> Session | None:
        if hmac.compare_digest(token.encode(), self.owner.encode()):
            return Session(self.store, OWNER)
        principal = self.tokens.find(token)
        return Session(self.store, principal) if principal else None

    def valid(self, session: Session) -> bool:
        return session.principal is OWNER or self.tokens.by_id(session.principal.id) is not None


class RemoteBackend:
    """A store server. Login tokens are checked by the server itself."""

    def __init__(self, url: str, allow_http: bool, token: str = ""):
        self.client = Client(url, token, allow_http=allow_http)  # refuses plain HTTP to a non-local host
        self.url, self.describe, self.allow_http, self.token = self.client.url, self.client.url, allow_http, token

    def _store(self, token: str) -> RemoteStore:
        return RemoteStore(Client(self.url, token, allow_http=self.allow_http))

    def anonymous(self) -> Session:
        return Session(self._store(self.token), None)

    def login(self, token: str) -> Session | None:
        store = self._store(token)
        try:
            store.projects()
        except Unauthorized:
            return None
        return Session(store, None)

    def valid(self, session: Session) -> bool:
        return True  # a revoked token fails on its next request (401)


def safe_target(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


def entry_type(path: str) -> str:
    return mimetypes.guess_type(path.rsplit("/", 1)[-1])[0] or "application/octet-stream"


class UIHandler(BaseHTTPRequestHandler):
    backend: LocalBackend | RemoteBackend
    sessions: dict
    lock: threading.Lock
    no_auth: bool = False
    secure_cookie: bool = True
    static_cache: dict
    fixed: Session | None = None  # --no-auth: the one session every request uses
    server_version = f"cairn-ui/{__version__}"
    protocol_version = "HTTP/1.1"

    # --------------------------------------------------------------- sessions

    def _cookie_header(self, value: str, max_age: int) -> str:
        secure = "; Secure" if self.secure_cookie else ""
        return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}"

    def _cookie_value(self) -> str | None:
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        return morsel.value if morsel else None

    def _session(self) -> Session | None:
        if self.no_auth:
            return self.fixed
        value = self._cookie_value()
        with self.lock:
            session = self.sessions.get(value) if value else None
            if session and session.expires < time.time():
                self.sessions.pop(value, None)
                session = None
        if session and not self.backend.valid(session):
            self._drop()
            return None
        return session

    def _drop(self) -> None:
        with self.lock:
            self.sessions.pop(self._cookie_value(), None)

    # --------------------------------------------------------------- dispatch

    def _run(self, action) -> None:
        try:
            action()
        except HttpError as error:
            self._error(error.status, error.message)
        except BadRange as error:
            self._send(416, b"", headers={"Content-Range": f"bytes */{error.total}"})
        except NotFound as error:
            self._error(404, str(error))
        except Forbidden as error:
            self._error(403, str(error))
        except Unauthorized:
            if self.no_auth:
                return self._error(401, "the store rejected the token from CAIRN_STORE_TOKEN or [store] token_file")
            self._drop()
            self._send(303, b"", headers={"Location": "/login?next=" + quote(self.path), "Set-Cookie": self._cookie_header("", 0)})
        except (StoreError, ValueError) as error:
            self._error(400, str(error))
        except OSError as error:
            self._error(502, f"cannot reach the store: {error}")

    def _error(self, status: int, message: str) -> None:
        self._send(status, pages.message_page(f"Error {status}", message), "text/html; charset=utf-8")

    def do_GET(self):
        self._run(self._get)

    def do_POST(self):
        self._run(self._post)

    @staticmethod
    def _segments(path: str) -> list[str]:
        return [unquote(part) for part in path.split("/")]

    def _get(self) -> None:
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/login" and not self.no_auth:
            return self._send(200, pages.login_page(safe_target(query.get("next", ["/"])[0])), "text/html; charset=utf-8")
        if path == "/logout":
            self._drop()
            return self._send(303, b"", headers={"Location": "/" if self.no_auth else "/login", "Set-Cookie": self._cookie_header("", 0)})
        session = self._session()
        if session is None:
            if path.startswith("/files/"):
                raise HttpError(401, "log in first")
            return self._send(303, b"", headers={"Location": "/login?next=" + quote(self.path)})
        segments = self._segments(path)
        if path == "/":
            return self._send(200, pages.index_page(session.store, session.principal, logout=not self.no_auth), "text/html; charset=utf-8")
        if segments[1] == "files":
            return self._files(session, path, query)
        if segments[1] == "p" and len(segments) >= 3:
            return self._pages(session, segments, url)
        raise HttpError(404, "no such page")

    @staticmethod
    def _allow(session: Session, project: str) -> None:
        if not pages.can(session.principal, project):
            raise Forbidden(f"this token cannot read project {project!r}")

    # ------------------------------------------------------------------ files

    def _files(self, session: Session, path: str, query: dict) -> None:
        rest = unquote(path)[len("/files/"):]
        project, _, remainder = rest.partition("/")
        head, separator, inner = remainder.partition("//")
        if not separator:
            raise HttpError(404, "file URLs look like /files/<project>/<name>@v<n>//<path>")
        ref = refs.parse(f"{project}:{head}//{inner}")
        self._allow(session, project)
        read = session.store.read_file(project, ref.name, ref.selector, ref.path, self.headers.get("Range"))
        # a pushed HTML file must not act as the UI's origin (its session)
        extra = {"Content-Security-Policy": "sandbox"}
        if query.get("download"):
            extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(ref.path.rsplit('/', 1)[-1])}"
        self._stream(read, entry_type(ref.path), read.immutable, extra)

    def _stream(self, read: FileRead, kind: str, immutable: bool, extra: dict | None = None, encoding: str | None = None) -> None:
        etag = f'"{read.sha256}"'
        headers = {"ETag": etag, "X-Content-Type-Options": "nosniff", "Vary": "Accept-Encoding",
                   "Cache-Control": "private, max-age=31536000, immutable" if immutable else "no-cache", **(extra or {})}
        if encoding:
            headers["Content-Encoding"] = encoding
        else:
            headers["Accept-Ranges"] = "bytes"
        if self.headers.get("If-None-Match") == etag:
            read.close()
            return self._send(304, b"", headers=headers)
        if read.partial:
            headers["Content-Range"] = f"bytes {read.first}-{read.last}/{read.total}"
        self.send_response(206 if read.partial else 200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(read.size))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        for chunk in read.chunks:
            self.wfile.write(chunk)

    # ------------------------------------------------------------------ pages

    @staticmethod
    def _version(store, project: str, name: str, selector: str | None) -> dict:
        """A version manifest: `v<n>` exactly (whatever its status), else the completed version an alias or label names."""
        if selector and refs.VERSION.fullmatch(selector):
            return store.get_version(project, name, int(selector[1:]))
        return store.resolve(Ref(project, name, selector))[2]

    def _pages(self, session: Session, segments: list[str], url) -> None:
        project, store = segments[2], session.store
        self._allow(session, project)
        refs.valid_project(project)
        if len(segments) == 3:
            store.get_project(project)
            return self._send(301, b"", headers={"Location": url.path + "/"})
        kind = segments[3]
        if kind == "" and len(segments) == 4:
            return self._send(200, pages.project_page(store, project), "text/html; charset=utf-8")
        if kind == "a":
            name, at, selector = "/".join(segments[4:]).rstrip("/").partition("@")
            if not name:
                raise HttpError(404, "no such page")
            chosen = self._version(store, project, name, selector) if at else None
            return self._send(200, pages.artifact_page(store, session.principal, project, name, chosen), "text/html; charset=utf-8")
        if kind == "r":
            return self._report(session, project, segments[4:], url)
        raise HttpError(404, "no such page")

    def _report(self, session: Session, project: str, rest: list[str], url) -> None:
        store = session.store
        first = rest[0] if rest else ""
        if first == "":
            return self._send(303, b"", headers={"Location": pages.project_href(project)})
        if first == "site.json" and len(rest) == 1:
            return self._json(200, self._site(store, project))
        if first == "_static" and len(rest) == 2:
            return self._static(store, project, rest[1])
        slug, at, selector = first.partition("@")
        name = f"reports/{slug}"
        refs.valid_name(name)
        if len(rest) == 1:  # relative links in the page need the trailing slash
            return self._send(301, b"", headers={"Location": url.path + "/" + (f"?{url.query}" if url.query else "")})
        version = self._version(store, project, name, selector if at else None)
        inside = "/".join(rest[1:])
        file = f"site/{slug}/{inside}index.html" if inside.endswith("/") or not inside else f"site/{slug}/{inside}"
        if not valid_path(file):
            raise HttpError(404, "no such file")
        if resolve_entry(version["files"], file) is None:
            raise HttpError(404, f"the report has no {file}")
        immutable = re.search(r"(^|&)v=[0-9a-f]+", url.query) is not None
        exact = f"v{version['version']}"
        if resolve_entry(version["files"], file + ".gz") and "gzip" in self.headers.get("Accept-Encoding", ""):
            read = store.read_file(project, name, exact, file + ".gz")
            return self._stream(read, entry_type(file), immutable, encoding="gzip")
        self._stream(store.read_file(project, name, exact, file, self.headers.get("Range")), entry_type(file), immutable)

    def _static(self, store, project: str, file: str) -> None:
        """A shared page file (`site/_static/<name>`, named by content hash): the newest report version that has it."""
        if not valid_path(file) or "/" in file:
            raise HttpError(404, "no such file")
        key = (project, file)
        if key not in self.static_cache:
            found = None
            for summary in store.summaries(project):
                name = summary["name"]
                for version in reversed(store.versions(project, name)) if pages.is_report(name) else ():
                    if version["status"] == "completed" and resolve_entry(version["files"], f"site/_static/{file}"):
                        found = (name, version["version"], resolve_entry(version["files"], f"site/_static/{file}.gz") is not None)
                        break
                if found:
                    break
            if found is None:
                raise HttpError(404, "no such file")
            self.static_cache[key] = found  # where it lives, not its bytes: every read still goes to the store with the session's token
        name, number, compressed = self.static_cache[key]
        if compressed and "gzip" in self.headers.get("Accept-Encoding", ""):
            return self._stream(store.read_file(project, name, f"v{number}", f"site/_static/{file}.gz"), entry_type(file), True, encoding="gzip")
        self._stream(store.read_file(project, name, f"v{number}", f"site/_static/{file}"), entry_type(file), True)

    @staticmethod
    def _site(store, project: str) -> dict:
        """What a report page's navigation reads (site.json): the project's reports, latest versions."""
        reports = []
        for summary in store.summaries(project):
            name = summary["name"]
            if pages.is_report(name) and summary["latest"]:
                slug, meta = name.split("/", 1)[1], summary["metadata"]
                reports.append({"key": slug, "title": meta.get("title") or slug, "subtitle": "", "status": meta.get("status", ""),
                                "summary": meta.get("summary", ""), "href": f"{slug}/", "updated": summary["updated"]})
        reports.sort(key=lambda item: (item["title"].lower(), item["key"]))
        return {"title": store.get_project(project)["title"], "reports": reports, "copy_preamble": ""}

    # ------------------------------------------------------------------ login

    def _post(self) -> None:
        if urlparse(self.path).path != "/login" or self.no_auth:
            raise HttpError(404, "no such page")
        form = parse_qs(self._body(4096).decode())
        given = form.get("token", [""])[0].strip()
        target = safe_target(form.get("next", ["/"])[0])
        session = self.backend.login(given) if given else None
        if session is None:
            time.sleep(LOGIN_DELAY)
            return self._send(401, pages.login_page(target, failed=True), "text/html; charset=utf-8")
        session.expires = time.time() + SESSION_SECONDS
        identifier = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            for key in [key for key, other in self.sessions.items() if other.expires < now]:
                del self.sessions[key]
            if len(self.sessions) >= MAX_SESSIONS:
                del self.sessions[min(self.sessions, key=lambda key: self.sessions[key].expires)]
            self.sessions[identifier] = session
        self._send(303, b"", headers={"Location": target, "Set-Cookie": self._cookie_header(identifier, SESSION_SECONDS)})

    # ------------------------------------------------------------------- I/O

    def _body(self, limit: int) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        if size > limit or size < 0:
            raise ValueError("invalid payload size")
        return self.rfile.read(size) if size else b""

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

    def log_message(self, format, *args):  # one line per request, without the session
        sys.stderr.write(f"{self.log_date_time_string()} {self.address_string()} {format % args}\n")


def make_ui_server(backend: LocalBackend | RemoteBackend, host: str = "127.0.0.1", port: int = 8801,
                   no_auth: bool = False, secure_cookie: bool = True) -> ThreadingHTTPServer:
    """A bound UI server (not yet serving). With no_auth there is no login and every request uses the
    backend's own access, so it is refused on any address but this machine's loopback."""
    if no_auth and host not in LOCAL_HOSTS:
        raise SystemExit("--no-auth shows the store to anyone who can connect: only with a loopback --host")
    handler = type("Handler", (UIHandler,), {
        "backend": backend, "sessions": {}, "lock": threading.Lock(), "no_auth": no_auth,
        "secure_cookie": secure_cookie, "static_cache": {}, "fixed": backend.anonymous() if no_auth else None,
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve_ui(backend: LocalBackend | RemoteBackend, host: str = "127.0.0.1", port: int = 8801, no_auth: bool = False,
             secure_cookie: bool = True) -> None:
    server = make_ui_server(backend, host, port, no_auth=no_auth, secure_cookie=secure_cookie)
    shown = host if ":" not in host else f"[{host}]"
    print(f"UI: http://{shown}:{server.server_address[1]}/ (store: {backend.describe})", flush=True)
    if host not in LOCAL_HOSTS:
        print("warning: the UI speaks plain HTTP; expose it only through an HTTPS reverse proxy or a tunnel", flush=True)
    if no_auth:
        print("Authentication is off (--no-auth): everyone who can connect sees the store; for local use only", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
