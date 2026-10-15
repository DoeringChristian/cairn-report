"""The store UI: a browser view of an artifact store, as a process of its own.

    cairn ui [--store URL|PATH] [--host 127.0.0.1] [--port 8801] [--no-auth] [--allow-http] [--insecure-cookies]

It reaches the store only through the store interface (`RemoteStore` for a store server, `LocalStore` for
a folder), so it runs next to the store or on a laptop against a remote one. It serves the React app
(web/, built into static/) and a small JSON API for it:

    /, /login, /p/<p>/, /p/<p>/a/<name>[@sel]   the app (index.html; the app routes on the client)
    /assets/<file>                               the app's hashed scripts and styles
    /ui-api/...                                  the app's JSON API (see `_api`), session-scoped
    /p/<p>/r/<slug>[@v<n>]/...                   a report version's built site (latest without @), its `_static/`
                                                 files and a generated `site.json` at /p/<p>/r/
    /files/<p>/<name>@sel//<path>                the store's files, with Range (so videos seek)

Reports are a UI concept: an artifact `reports/<slug>` whose files have `site/` and `source/`.

Login: the user enters a store token on /login (the app posts it to /ui-api/login). The UI checks it against
the store (a remote store: an authenticated API call; a folder: the folder's owner token file and
tokens.jsonl), keeps it server-side in an in-memory session table, and gives the browser only a random
session id (HttpOnly, SameSite=Strict cookie, Secure unless --insecure-cookies). Every request of a session
uses its token, so a scoped token sees only its project. Every /ui-api/ call also needs the header
`X-Cairn: 1`, which a cross-site form or image cannot send (on top of SameSite=Strict). `--no-auth`
(loopback only) skips the login and uses `CAIRN_STORE_TOKEN` / `[store] token_file` for a remote store, or
the owner's full access to a folder.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .._version import __version__
from ..store import BadRange, Deleted, FileRead, Forbidden, NotFound, Ref, StoreError, Unauthorized, refs
from ..store.blobs import resolve_entry, valid_path
from ..store.tokens import Principal
from .backends import LocalBackend, RemoteBackend
from .sessions import COOKIE, LOGIN_DELAY, MAX_SESSIONS, SESSION_SECONDS, Session

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
STATIC = Path(__file__).parent / "static"  # the built app (web/ -> npm run build)
MAX_LINEAGE_DEPTH = 3
MAX_LINEAGE_NODES = 200
MAX_LOGIN_BODY = 4096


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def safe_target(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


def entry_type(path: str) -> str:
    return mimetypes.guess_type(path.rsplit("/", 1)[-1])[0] or "application/octet-stream"


def is_report(name: str) -> bool:
    return name.startswith("reports/") and name.count("/") == 1


def can(principal: Principal | None, project: str | None) -> bool:
    """Whether the viewer may see a project; no principal means the store itself decides (a remote store, or --no-auth)."""
    return principal is None or principal.can(project)


def message_page(title: str, text: str) -> bytes:
    """A plain page for errors outside the app (the app shows its own for API errors)."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title></head><body style=\"font:16px/1.5 system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem\">"
        f'<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p><p><a href="/">Back to the store</a></p></body></html>'
    ).encode()


def app_route(segments: list[str]) -> bool:
    """Whether a path (split on `/`) is a client-side route of the app."""
    if segments == ["", ""] or segments == ["", "login"]:
        return True
    return len(segments) >= 4 and segments[1] == "p" and (segments[3] == "" and len(segments) == 4 or segments[3] == "a" and len(segments) > 4)


def summarize_version(version: dict) -> dict:
    """A version without its file list: the table of versions needs only counts (a tombstone keeps those it had)."""
    files = version.get("files", [])
    slim = {key: value for key, value in version.items() if key not in ("files", "inputs")}
    counts = {"file_count": len(files), "size": sum(entry.get("size", 0) for entry in files)}
    if version.get("status") == "deleted":
        counts = {"file_count": version.get("file_count", 0), "size": version.get("size", 0)}
    return {**slim, "input_count": len(version.get("inputs", [])), **counts}


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
        except Deleted as error:
            self._error(410, str(error))
        except NotFound as error:
            self._error(404, str(error))
        except Forbidden as error:
            self._error(403, str(error))
        except Unauthorized:
            if self.no_auth:
                return self._error(401, "the store rejected the token from CAIRN_STORE_TOKEN or [store] token_file")
            self._drop()
            if self._is_api() or urlparse(self.path).path.startswith("/files/"):
                return self._send(401, json.dumps({"error": "log in first"}).encode(), "application/json",
                                  {"Set-Cookie": self._cookie_header("", 0), "Cache-Control": "no-store"})
            self._send(303, b"", headers={"Location": "/login?next=" + quote(self.path), "Set-Cookie": self._cookie_header("", 0)})
        except (StoreError, ValueError) as error:
            self._error(400, str(error))
        except OSError as error:
            self._error(502, f"cannot reach the store: {error}")

    def _is_api(self) -> bool:
        return urlparse(self.path).path.startswith("/ui-api/")

    def _error(self, status: int, message: str) -> None:
        if self._is_api():
            return self._json(status, {"error": message})
        self._send(status, message_page(f"Error {status}", message), "text/html; charset=utf-8")

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
        segments = self._segments(path)
        if path.startswith("/assets/"):
            return self._asset(path[len("/assets/"):])
        if path == "/login":
            if self.no_auth:
                raise HttpError(404, "no such page")
            if self._session() is not None:
                return self._send(303, b"", headers={"Location": safe_target(query.get("next", ["/"])[0])})
            return self._app()
        if path == "/logout":
            self._drop()
            return self._send(303, b"", headers={"Location": "/" if self.no_auth else "/login", "Set-Cookie": self._cookie_header("", 0)})
        if self._is_api():
            return self._api(url, query)
        session = self._session()
        if session is None:
            if path.startswith("/files/"):
                raise HttpError(401, "log in first")
            return self._send(303, b"", headers={"Location": "/login?next=" + quote(self.path)})
        if path != "/" and segments[1] == "p" and len(segments) == 3 and segments[2]:
            return self._send(301, b"", headers={"Location": url.path + "/"})  # /p/<p> -> /p/<p>/
        if app_route(segments):
            return self._app()
        if segments[1] == "files":
            return self._files(session, path, query)
        if segments[1] == "p" and len(segments) >= 5 and segments[3] == "r":
            self._allow(session, segments[2])
            refs.valid_project(segments[2])
            return self._report(session, segments[2], segments[4:], url)
        raise HttpError(404, "no such page")

    @staticmethod
    def _allow(session: Session, project: str) -> None:
        if not can(session.principal, project):
            raise Forbidden(f"this token cannot read project {project!r}")

    # ------------------------------------------------------------------ the app

    def _app(self) -> None:
        index = STATIC / "index.html"
        if not index.is_file():
            return self._send(503, message_page("The UI is not built", "Run `npm ci && npm run build` in web/ (a released package ships the built app)."),
                              "text/html; charset=utf-8")
        self._send(200, index.read_bytes(), "text/html; charset=utf-8", {"Cache-Control": "no-cache"})

    def _asset(self, name: str) -> None:
        file = (STATIC / "assets" / name).resolve()
        if "/" in name or not file.is_file() or file.parent != (STATIC / "assets").resolve():
            raise HttpError(404, "no such file")
        self._send(200, file.read_bytes(), entry_type(name), {"Cache-Control": "public, max-age=31536000, immutable"})

    # ------------------------------------------------------------------ JSON API

    def _api(self, url, query: dict) -> None:
        """The app's data, as the session's store handle sees it (never the store's folders). Needs the session and `X-Cairn: 1`."""
        if self.headers.get("X-Cairn") != "1":
            raise HttpError(403, "missing X-Cairn header")
        session = self._session()
        if session is None:
            raise Unauthorized()
        store, principal = session.store, session.principal
        match [unquote(part) for part in url.path.split("/")[2:]]:
            case ["me"]:
                scope = "project" if principal and principal.project else "owner" if principal else "store"
                return self._json(200, {"scope": scope, "project": principal.project if principal else None, "auth": not self.no_auth})
            case ["projects"]:
                found = [item for item in store.overviews() if can(principal, item["name"])]
                return self._json(200, sorted(found, key=lambda item: item["updated"], reverse=True))
            case ["projects", project]:
                self._allow(session, project)
                record = store.get_project(project)
                return self._json(200, {"project": record, "artifacts": store.summaries(project)})
            case ["projects", project, "artifacts", *rest] if rest:
                self._allow(session, project)
                if len(rest) >= 3 and rest[-2] == "versions":
                    return self._json(200, self._version_api(store, project, "/".join(rest[:-2]), rest[-1]))
                return self._json(200, self._artifact_api(store, project, "/".join(rest)))
            case ["lineage"]:
                ref = refs.parse(query.get("ref", [""])[0])
                if not ref.project:
                    raise ValueError("the reference needs a project: project:name[@selector]")
                self._allow(session, ref.project)
                depth = int(query.get("depth", ["1"])[0])
                if not 1 <= depth <= MAX_LINEAGE_DEPTH:
                    raise ValueError(f"depth must be between 1 and {MAX_LINEAGE_DEPTH}")
                return self._json(200, self._lineage_api(session, ref, depth))
        raise HttpError(404, "no such API route")

    @staticmethod
    def _artifact_api(store, project: str, name: str) -> dict:
        versions = store.versions(project, name)
        if not versions:
            raise NotFound(f"no artifact {project}:{name}")
        return {"project": project, "name": name, "summary": store.summary(project, name),
                "versions": [summarize_version(version) for version in versions], "aliases": store.aliases(project, name)}

    def _version_api(self, store, project: str, name: str, selector: str) -> dict:
        """One manifest (files, metadata, inputs) with its consumers; `selector` is a number, `v<n>`, an alias or a label."""
        chosen = f"v{selector}" if selector.isdigit() else selector
        version = self._version(store, project, name, chosen)
        consumers, deleted = [], []
        if version["status"] in ("completed", "deleted"):  # a deleted version keeps its edges
            found = store.lineage(refs.exact(project, name, version["version"]), marks=True)
            consumers, deleted = found["consumers"], found.get("deleted", [])
        return {"project": project, "name": name, "version": version, "consumers": consumers, "deleted": deleted}

    def _lineage_api(self, session: Session, ref: Ref, depth: int) -> dict:
        """Inputs to the left and consumers to the right of a version, `depth` levels each way, from one-level lineage calls.

        `nodes` maps each reference to its `inputs` and `consumers` when they were looked up (null: not looked
        up, so the app may offer to expand it). Ancestors are expanded through their inputs only, descendants
        through their consumers only. References in projects the session may not read are left out. A deleted
        version keeps its edges; its node says `deleted: true` (the key is there only then)."""
        store, principal = session.store, session.principal
        first = store.lineage(ref, marks=True)
        try:
            number = store.resolve(ref)[2]["version"]
        except Deleted:
            if not ref.exact:
                raise
            number = ref.version or store.get_version(ref.project, ref.name, ref.uid)["version"]
        root = refs.exact(ref.project, ref.name, number)
        nodes: dict[str, dict] = {root: {"inputs": None, "consumers": None}}
        gone = set(first.get("deleted", []))
        truncated = False

        def visible(items: list[str]) -> list[str]:
            return [item for item in items if can(principal, refs.parse(item).project)]

        for side, other in (("inputs", "consumers"), ("consumers", "inputs")):
            frontier = [root]
            for level in range(depth):
                following: list[str] = []
                for current in frontier:
                    if level == 0 and current == root:
                        found = visible(first[side])
                    else:
                        try:
                            answer = store.lineage(current, marks=True)
                            gone.update(answer.get("deleted", []))
                            found = visible(answer[side])
                        except (NotFound, StoreError, Forbidden):
                            found = []
                    kept = []
                    for item in found:
                        if item not in nodes:
                            if len(nodes) >= MAX_LINEAGE_NODES:
                                truncated = True
                                continue
                            nodes[item] = {"inputs": None, "consumers": None}
                            following.append(item)
                        kept.append(item)
                    nodes[current][side] = kept
                frontier = following
        for item in gone & nodes.keys():
            nodes[item]["deleted"] = True
        return {"ref": root, "depth": depth, "nodes": nodes, "truncated": truncated}

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

    def _report(self, session: Session, project: str, rest: list[str], url) -> None:
        store = session.store
        first = rest[0] if rest else ""
        if first == "":
            return self._send(303, b"", headers={"Location": f"/p/{quote(project)}/"})
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
                for version in reversed(store.versions(project, name)) if is_report(name) else ():
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
            if is_report(name) and summary["latest"]:
                slug, meta = name.split("/", 1)[1], summary["metadata"]
                reports.append({"key": slug, "title": meta.get("title") or slug, "subtitle": "", "status": meta.get("status", ""),
                                "summary": meta.get("summary", ""), "href": f"{slug}/", "updated": summary["updated"]})
        reports.sort(key=lambda item: (item["title"].lower(), item["key"]))
        return {"title": store.get_project(project)["title"], "reports": reports, "copy_preamble": ""}

    # ------------------------------------------------------------------ login

    def _post(self) -> None:
        if urlparse(self.path).path != "/ui-api/login" or self.no_auth:
            raise HttpError(404, "no such page")
        if self.headers.get("X-Cairn") != "1":
            raise HttpError(403, "missing X-Cairn header")
        try:
            given = str(json.loads(self._body(MAX_LOGIN_BODY) or b"{}").get("token", "")).strip()
        except (ValueError, AttributeError):
            raise ValueError("expected a JSON object with a token")
        session = self.backend.login(given) if given else None
        if session is None:
            time.sleep(LOGIN_DELAY)
            return self._json(401, {"error": "That token is not valid."})
        session.expires = time.time() + SESSION_SECONDS
        identifier = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            for key in [key for key, other in self.sessions.items() if other.expires < now]:
                del self.sessions[key]
            if len(self.sessions) >= MAX_SESSIONS:
                del self.sessions[min(self.sessions, key=lambda key: self.sessions[key].expires)]
            self.sessions[identifier] = session
        self._send(200, b'{"ok":true}', "application/json", {"Set-Cookie": self._cookie_header(identifier, SESSION_SECONDS), "Cache-Control": "no-store"})

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


def follow_parent() -> None:
    """A UI spawned by `cairn server` exits as soon as that server is gone (stopped, crashed, or
    killed with SIGKILL, which no handler in the server can catch)."""
    parent = os.environ.get("CAIRN_UI_PARENT")
    if not parent or not parent.isdigit():
        return

    def watch() -> None:
        while os.getppid() == int(parent):
            time.sleep(1)
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def serve_ui(backend: LocalBackend | RemoteBackend, host: str = "127.0.0.1", port: int = 8801, no_auth: bool = False,
             secure_cookie: bool = True) -> None:
    server = make_ui_server(backend, host, port, no_auth=no_auth, secure_cookie=secure_cookie)
    shown = host if ":" not in host else f"[{host}]"
    print(f"UI: http://{shown}:{server.server_address[1]}/ (store: {backend.describe})", flush=True)
    if host not in LOCAL_HOSTS:
        print("warning: the UI speaks plain HTTP; expose it only through an HTTPS reverse proxy or a tunnel", flush=True)
    if no_auth:
        print("Authentication is off (--no-auth): everyone who can connect sees the store; for local use only", flush=True)
    follow_parent()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
