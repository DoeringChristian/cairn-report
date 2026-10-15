"""The store server: a LocalStore root shared over HTTP, behind tokens. Only an artifact store: a JSON API and
file downloads, no pages (`cairn-report ui` explores a store in a browser, locally or against this server).

    cairn-report store serve <root> [--host 127.0.0.1] [--port 8800] [--max-blob-gb 50] [--no-auth]

A new root gets a fresh owner token on first launch; every launch prints `Store: <url>` and
`Token: <owner token>` to the terminal (never into a URL). Scoped tokens (one project, read or
write) are made with `cairn-report store token create` (see tokens.py). Bind localhost, and put an
HTTPS reverse proxy in front of anything else: the server speaks plain HTTP.

Artifact names are slash paths (`datasets/crops`), so every route says where the name ends:

    API (JSON, `Authorization: Bearer <token>`; a name is ONE percent-encoded path segment, `datasets%2Fcrops`)
      GET  /api/v2/projects                                      [{..., artifacts, updated}]   POST {name, title?, description?}
      GET  /api/v2/projects/<p>                                  PATCH {title?, description?}
      GET  /api/v2/projects/<p>/artifacts                        [{name, latest, versions, aliases, updated, metadata}]
      GET  /api/v2/projects/<p>/artifacts/<name>                 that summary plus `latest_version` (its manifest, or null)
      GET  /api/v2/projects/<p>/artifacts/<name>/versions        every version, full manifests
      GET  /api/v2/projects/<p>/artifacts/<name>/versions/<n>    one manifest        .../versions/<n>/files  {files: [entries]}
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

References in the API are `project:name[@selector]` (the project is required: the server has no default).

Tokens are bearer tokens only: there are no cookies and no pages. Everything stored is append-only and the
server never executes pushed code. Files are served with `Content-Security-Policy: sandbox`.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import __version__
from . import refs
from .blobs import SHA256, clean_entries
from .local import BadRange, LocalStore, NotFound, StoreError
from .refs import Ref
from .tokens import OWNER, Principal, Tokens, init_root, read_token

MAX_JSON = 64 * 1024 * 1024
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
PARENT_ENV = "CAIRN_UI_PARENT"  # set for a UI the store server spawned: the pid it belongs to


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class StoreHandler(BaseHTTPRequestHandler):
    store: LocalStore
    owner_token: str = ""
    tokens: Tokens
    no_auth: bool = False  # --no-auth: every request acts as the owner (local testing only)
    max_blob: int = 0
    server_version = f"cairn-store/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------- auth

    def _principal_for(self, token: str) -> Principal | None:
        if hmac.compare_digest(token.encode(), self.owner_token.encode()):
            return OWNER
        return self.tokens.find(token)

    def _bearer(self) -> Principal | None:
        if self.no_auth:
            return OWNER
        header = self.headers.get("Authorization", "")
        return self._principal_for(header[7:].strip()) if header.startswith("Bearer ") else None

    @staticmethod
    def _allow(principal: Principal, project: str, write: bool = False) -> None:
        if not principal.can(project, write):
            raise HttpError(403, f"this token cannot {'write' if write else 'read'} project {project!r}")

    # --------------------------------------------------------------- dispatch

    def _run(self, action) -> None:
        try:
            action()
        except HttpError as error:
            self._json(error.status, {"error": error.message})
        except BadRange as error:
            self._send(416, b"", headers={"Content-Range": f"bytes */{error.total}"})
        except NotFound as error:
            self._json(404, {"error": str(error)})
        except StoreError as error:
            self._json(409 if "can no longer change" in str(error) else 400, {"error": str(error)})
        except ValueError as error:  # invalid names, entries, JSON
            self._json(400, {"error": str(error)})

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
        segments = self._segments(path)
        if segments[1:2] not in (["api"], ["files"]):
            raise HttpError(404, "this server has no pages: its API lives under /api/v2/ and its files under /files/")
        principal = self._bearer()
        if principal is None:
            raise HttpError(401, "missing or invalid token")
        if segments[1] == "files":
            return self._files(principal, path, query)
        if segments[1:3] != ["api", "v2"]:
            raise HttpError(404, "the API lives under /api/v2/")
        return self._api_get(principal, segments[3:], query)

    # -------------------------------------------------------------------- API

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
                return self._json(200, [item for item in store.overviews() if principal.can(item["name"])])
            case ["projects", project]:
                self._allow(principal, project)
                return self._json(200, store.get_project(project))
            case ["projects", project, "artifacts"]:
                self._allow(principal, project)
                store.get_project(project)
                return self._json(200, store.summaries(project))
            case ["projects", project, "artifacts", name]:
                self._allow(principal, project)
                if not store.versions(project, name):
                    raise NotFound(f"no artifact {project}:{name}")
                summary = store.summary(project, name)
                latest = summary["latest"]
                return self._json(200, {**summary, "latest_version": store.get_version(project, name, latest) if latest else None})
            case ["projects", project, "artifacts", name, "versions"]:
                self._allow(principal, project)
                return self._json(200, store.versions(project, name))
            case ["projects", project, "artifacts", name, "versions", number] if number.isdigit():
                self._allow(principal, project)
                return self._json(200, store.get_version(project, name, int(number)))
            case ["projects", project, "artifacts", name, "versions", number, "files"] if number.isdigit():
                self._allow(principal, project)
                return self._json(200, {"files": store.get_version(project, name, int(number))["files"]})
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


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8800, max_blob: int = 50 * 1024**3,
                no_auth: bool = False) -> ThreadingHTTPServer:
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
        "no_auth": no_auth,
        "max_blob": max_blob,
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def start_ui(host: str, port: int, ui_port: int, owner_token: str, no_auth: bool):
    """`cairn-report ui` as a child process beside this store (so page work never loads the store server).

    The child reaches the store at `host:port`; the owner token goes through its environment only (never its
    command line). Returns (process, the line it printed with its address), or (None, None) if it did not start.
    """
    where = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    url = f"http://[{where}]:{port}" if ":" in where else f"http://{where}:{port}"
    command = [sys.executable, "-m", "cairn_report", "ui", "--store", url, "--host", host, "--port", str(ui_port), "--allow-http"]
    if no_auth:
        command.append("--no-auth")
    if host in LOCAL_HOSTS:
        command.append("--insecure-cookies")  # plain HTTP on this machine
    env = dict(os.environ)
    env.pop("CAIRN_STORE_TOKEN", None)
    if not no_auth:
        env["CAIRN_STORE_TOKEN"] = owner_token
    env[PARENT_ENV] = str(os.getpid())  # the UI exits when this process is gone, even after a SIGKILL
    child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline().rstrip()
    if not line:
        child.wait()
        return None, None
    return child, line


def serve_store(root: Path, host: str = "127.0.0.1", port: int = 8800, max_blob: int = 50 * 1024**3,
                no_auth: bool = False, ui_port: int | None = None) -> None:
    """Serve a store; a new root (no token yet) is initialized first. Prints the owner token, which clients
    and `cairn-report ui` log in with, to the terminal only: never into a URL. With `ui_port` (0: any free
    port), `cairn-report ui` runs as a child process beside the store and stops with it."""
    created = None if (root / "token").exists() else init_root(root)
    server = make_server(root, host, port, max_blob=max_blob, no_auth=no_auth)
    if host not in LOCAL_HOSTS:
        print("warning: the store serves plain HTTP; expose it only through an HTTPS reverse proxy or a tunnel", flush=True)
    if created:
        print(f"Created store {root.resolve()} with a new owner token ({created}, owner-only)", flush=True)
    actual = server.server_address[1]
    print(f"Store: http://{host}:{actual}/ (data in {root.resolve()})", flush=True)
    child = None
    if ui_port is not None:
        child, line = start_ui(host, actual, ui_port, server.RequestHandlerClass.owner_token, no_auth)
        if child is None:
            print("warning: the UI did not start (see above); the store keeps running", file=sys.stderr, flush=True)
        else:
            print(line, flush=True)
            threading.Thread(target=forward, args=(child,), daemon=True).start()
    if no_auth:
        print("Authentication is off (--no-auth): every request acts as the owner; for local testing only", flush=True)
    else:
        print(f"Token: {server.RequestHandlerClass.owner_token}", flush=True)
        print("  clients read it from CAIRN_STORE_TOKEN or a token_file, and `cairn-report ui` logs in with it;"
              " `cairn-report store token create` makes project-scoped tokens", flush=True)
    previous = None
    if threading.current_thread() is threading.main_thread():  # SIGTERM stops the UI child too
        previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if child is not None:
            stop(child)


def forward(child) -> None:
    """Pass the UI's later output on, and say so if it dies while the store keeps running."""
    for line in child.stdout:
        print(line.rstrip(), flush=True)
    code = child.wait()
    if code not in (0, -signal.SIGTERM, -signal.SIGINT):
        print(f"warning: the UI stopped (exit code {code}); the store keeps running", file=sys.stderr, flush=True)


def stop(child) -> None:
    child.terminate()
    try:
        child.wait(5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
