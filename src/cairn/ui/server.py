"""The UI: a browser view of a repo, as a process of its own.

    cairn ui [--remote URL | --repo FOLDER] [--host 127.0.0.1] [--port 8801] [--no-auth] [--allow-http]

It reaches the data only through the repo interface (the project's repo, which reads through its remote when it
has one and shows the versions still waiting for it as pending; a repo folder; or a remote), so it runs next to the
repo or on a laptop against a remote. It shows projects (backends.py): the project's repo is one project, named after
the project folder or the remote's project (`mode: local` in /ui-api/me), a server or a server's repo folder has
many (`mode: server`). Inside a project, references have no project; the JSON API names them `project:name@sel`
(inputs, consumers, lineage), as the app reads them, and every version carries its `short_id`. It serves the React
app (web/, built into static/) and a small JSON API for it:

    /, /login, /p/<p>/, /p/<p>/a/<name>[@sel]   the app (index.html; the app routes on the client)
    /assets/<file>                               the app's hashed scripts and styles
    /ui-api/...                                  the app's JSON API (see `_api`), session-scoped
    /p/<p>/r/<slug>[@v<n>]/...                   a report version's built site (latest without @), its `_static/`
                                                 files and a generated `site.json` at /p/<p>/r/
    /files/<p>/<name>@sel//<path>                the repo's files, with Range (so videos seek)
    /files/<p>/<name>@sel.zip                    a whole version as a zip, streamed (`<name>-v<n>/...` inside)

A selector is `v<n>`, a uid (a pending version has no number yet), an alias, or a label. Reports are a UI concept:
an artifact `reports/<slug>` whose files have `site/` and `source/`.

Login: `cairn ui` prints a one-time link (a random code, valid once for 10 minutes); or the user enters a token on
/login (the app posts it to /ui-api/login). The UI checks it (a remote: an authenticated API call; a repo on this
machine: its owner token file and tokens.jsonl), keeps it server-side in an in-memory session table, and gives the
browser only a random session id (HttpOnly, SameSite=Strict cookie, Secure when a reverse proxy says X-Forwarded-Proto: https). Every
request of a session uses its token, so a scoped token sees only its project. Every /ui-api/ call also needs the
header `X-Cairn: 1`, which a cross-site form or image cannot send (on top of SameSite=Strict). `--no-auth`
(loopback only) skips the login and uses `CAIRN_TOKEN` / `[repo] token_file` for a remote, or the owner's full
access to a repo on this machine.
"""

from __future__ import annotations

import errno
import html
import io
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from ..hosts import LOCAL_HOSTS, url_host
from .._version import __version__
from ..store import BadRange, Deleted, FileRead, Forbidden, NotFound, Ref, StoreError, Unauthorized, refs
from ..store.blobs import resolve_entry, valid_path
from ..store.outbox import exact_ref
from ..store.tokens import Principal
from ..store.uids import UID
from .backends import LocalBackend, RemoteBackend
from .sessions import COOKIE, LOGIN_DELAY, MAX_SESSIONS, SESSION_SECONDS, Session

STATIC = Path(__file__).parent / "static"  # the built app (web/ -> npm run build)
MAX_LINEAGE_DEPTH = 3
MAX_LINEAGE_NODES = 200
MAX_LOGIN_BODY = 4096
RUN_TAIL = 64 * 1024  # bytes of logs/stdout.txt and stderr.txt the run panel shows
RUN_ROWS_BYTES = 8 * 1024 * 1024  # the most of a metrics file the run panel reads (its end)
RUN_POINTS = 2000  # rows per chart, at most (evenly thinned)
STORED = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".mp4", ".webm", ".mov", ".m4v", ".mkv", ".zip", ".gz", ".tgz",
          ".bz2", ".xz", ".zst", ".7z", ".npz", ".pt", ".pth", ".safetensors", ".h5", ".parquet", ".jxl", ".heic", ".mp3", ".ogg"}


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
        f'<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p><p><a href="/">Back to the repo</a></p></body></html>'
    ).encode()


def app_route(segments: list[str]) -> bool:
    """Whether a path (split on `/`) is a client-side route of the app."""
    if segments in (["", ""], ["", "login"], ["", "palette"]):
        return True
    return len(segments) >= 4 and segments[1] == "p" and (segments[3] == "" and len(segments) == 4 or segments[3] == "a" and len(segments) > 4)


def selector_of(version: dict) -> str:
    """`v<n>`, or the uid of a pending version (not numbered yet)."""
    return f"v{version['version']}" if version.get("version") is not None else version["uid"]


def qualified(project: str, ref: str) -> str:
    """A reference inside a project as the app reads it: `project:name@sel`."""
    return f"{project}:{ref}"


def split_ref(text: str) -> tuple[str, refs.Ref]:
    """(project, reference) of the app's `project:name@sel`."""
    project, colon, rest = text.partition(":")
    if not colon or not project:
        raise ValueError("the reference needs a project: project:name[@selector]")
    return refs.valid_project(project) if project else project, refs.parse(rest)


def with_short_id(version: dict) -> dict:
    """A manifest with its `short_id` (every version has one; a pending one its default)."""
    return {**version, "short_id": refs.short_of(version)} if version.get("uid") else version


def shown(project: str, version: dict) -> dict:
    """A manifest as the app shows it: its short id, its inputs as `project:name@sel`."""
    version = with_short_id(version)
    if "inputs" in version:
        version = {**version, "inputs": [qualified(project, item) for item in version.get("inputs") or []]}
    return version


def summarize_version(version: dict) -> dict:
    """A version without its file list: the table of versions needs only counts (a tombstone keeps those it had)."""
    files = version.get("files", [])
    version = with_short_id(version)
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
    static_cache: dict
    fixed: Session | None = None  # --no-auth: the one session every request uses
    server_version = f"cairn-ui/{__version__}"
    protocol_version = "HTTP/1.1"

    # --------------------------------------------------------------- sessions

    def _cookie_header(self, value: str, max_age: int) -> str:
        # Secure exactly when the browser uses HTTPS, which the reverse proxy in front says in X-Forwarded-Proto:
        # over plain HTTP (this machine, an SSH tunnel, a VPN such as Tailscale) a browser drops a Secure cookie,
        # and every login would end on the login page again.
        https = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        secure = "; Secure" if https else ""
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
                return self._error(401, "the remote rejected the token from CAIRN_TOKEN or [repo] token_file")
            self._drop()
            if self._is_api() or urlparse(self.path).path.startswith("/files/"):
                return self._send(401, json.dumps({"error": "log in first"}).encode(), "application/json",
                                  {"Set-Cookie": self._cookie_header("", 0), "Cache-Control": "no-store"})
            self._send(303, b"", headers={"Location": "/login?next=" + quote(self.path), "Set-Cookie": self._cookie_header("", 0)})
        except (StoreError, ValueError) as error:
            self._error(400, str(error))
        except OSError as error:
            self._error(502, f"cannot reach the remote: {error}")

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
            if "code" in query:  # the one-time link `cairn ui` prints and opens: a login without typing the token
                with self.lock:
                    entry = self.login_codes.pop(query["code"][0], None)
                session = self.backend.login(entry[1]) if entry and entry[0] > time.time() else None
                if session is not None:
                    return self._send(303, b"", headers={"Location": "/", "Set-Cookie": self._start(session), "Cache-Control": "no-store"})
                return self._send(303, b"", headers={"Location": "/login"})  # used or expired: the token form
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
            return self._report(session, session.store.get(segments[2]), segments[2], segments[4:], url)
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
        """The app's data, as the session's repo handle sees it (never the repo's folders). Needs the session and `X-Cairn: 1`."""
        if self.headers.get("X-Cairn") != "1":
            raise HttpError(403, "missing X-Cairn header")
        session = self._session()
        if session is None:
            raise Unauthorized()
        store, principal = session.store, session.principal
        match [unquote(part) for part in url.path.split("/")[2:]]:
            case ["me"]:
                scope = "project" if principal and principal.project else "owner" if principal else "remote"
                return self._json(200, {"scope": scope, "project": principal.project if principal else None, "auth": not self.no_auth,
                                        "mode": store.mode, "home": getattr(store, "name", None)})
            case ["projects"]:
                found = [item for item in store.overviews() if can(principal, item["name"])]
                return self._json(200, sorted(found, key=lambda item: item["updated"], reverse=True))
            case ["projects", project]:
                self._allow(session, project)
                repo = store.get(project)
                return self._json(200, {"project": store.record(project), "artifacts": repo.summaries()})
            case ["projects", project, "artifacts", *rest] if rest:
                self._allow(session, project)
                repo = store.get(project)
                if len(rest) >= 4 and rest[-3] == "versions" and rest[-1] == "run":
                    return self._json(200, self._run_api(repo, project, "/".join(rest[:-3]), rest[-2]))
                if len(rest) >= 3 and rest[-2] == "versions":
                    return self._json(200, self._version_api(repo, project, "/".join(rest[:-2]), rest[-1]))
                return self._json(200, self._artifact_api(repo, project, "/".join(rest)))
            case ["lineage"]:
                project, ref = split_ref(query.get("ref", [""])[0])
                self._allow(session, project)
                depth = int(query.get("depth", ["1"])[0])
                if not 1 <= depth <= MAX_LINEAGE_DEPTH:
                    raise ValueError(f"depth must be between 1 and {MAX_LINEAGE_DEPTH}")
                return self._json(200, self._lineage_api(store.get(project), project, ref, depth))
        raise HttpError(404, "no such API route")

    @staticmethod
    def _artifact_api(repo, project: str, name: str) -> dict:
        versions = repo.versions(name)
        if not versions:
            raise NotFound(f"no artifact {name} in the project {project}")
        return {"project": project, "name": name, "summary": repo.summary(name),
                "versions": [summarize_version(version) for version in versions], "aliases": repo.aliases(name)}

    def _version_api(self, repo, project: str, name: str, selector: str) -> dict:
        """One manifest (files, metadata, inputs) with its consumers; `selector` is a number, `v<n>`, a uid, a short id,
        an alias or a label."""
        chosen = f"v{selector}" if selector.isdigit() else selector
        version = self._version(repo, name, chosen)
        consumers, deleted, nodes = [], [], {}
        if version["status"] in ("completed", "deleted", "running", "failed"):  # a deleted version keeps its edges, a running run has some
            found = repo.lineage(exact_ref(name, version), marks=True)
            consumers, deleted, nodes = found["consumers"], found.get("deleted", []), found.get("nodes", {})
        return {"project": project, "name": name, "version": shown(project, version),
                "consumers": [qualified(project, item) for item in consumers], "deleted": [qualified(project, item) for item in deleted],
                "nodes": {qualified(project, item): mark for item, mark in nodes.items()}}

    def _run_api(self, store, project: str, name: str, selector: str) -> dict:
        """What the app's Run panel shows of a run version: its metrics.jsonl and system/metrics.jsonl rows (numbers
        only, thinned to RUN_POINTS), the tails of logs/stdout.txt, stderr.txt, and error.txt, system/info.json, and
        the config. A running version answers with what it has synced so far."""
        chosen = f"v{selector}" if selector.isdigit() else selector
        version = self._version(store, name, chosen)
        if version["status"] == "deleted":
            raise Deleted(f"{exact_ref(name, version)} was deleted")
        files = {entry["path"]: entry for entry in version.get("files", [])}
        exact = selector_of(version)

        def read(path: str, limit: int) -> tuple[bytes, bool]:
            entry = files.get(path)
            if entry is None:
                return b"", False
            size = entry.get("size") or 0
            wanted = f"bytes=-{limit}" if size > limit else None
            try:
                found = store.read_file(name, exact, path, wanted)
            except (NotFound, StoreError, BadRange):
                return b"", False
            try:
                return b"".join(found.chunks), size > limit
            finally:
                found.close()

        def text(path: str) -> dict | None:
            data, cut = read(path, RUN_TAIL)
            if path not in files:
                return None
            return {"text": data.decode("utf-8", "replace"), "truncated": cut, "size": files[path].get("size", 0)}

        def rows(path: str) -> dict:
            data, cut = read(path, RUN_ROWS_BYTES)
            lines = data.decode("utf-8", "replace").splitlines()
            if cut and lines:
                lines = lines[1:]  # the first line of a tail is cut
            found = []
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if isinstance(row.get("name"), str) and "value" in row and set(row) <= {"name", "value", "step", "time", "origin", "nonfinite"}:
                    row = {row["name"]: row["value"], "step": row.get("step"), "time": row.get("time")}  # an earlier release's line
                keep = {key: value for key, value in row.items() if key == "time" or (isinstance(value, (int, float)) and not isinstance(value, bool))}
                if len(keep) > 1 or (keep and "time" not in keep):
                    found.append(keep)
            total = len(found)
            if total > RUN_POINTS:
                stride = total / RUN_POINTS
                found = [found[int(index * stride)] for index in range(RUN_POINTS - 1)] + [found[-1]]
            return {"rows": found, "total": total, "truncated": cut}

        info = None
        raw, _ = read("system/info.json", 1 << 20)
        if raw:
            try:
                info = json.loads(raw)
            except ValueError:
                info = None
        metadata = version.get("metadata") or {}
        return {"project": project, "name": name, "version": version["version"], "short_id": refs.short_of(version),
                "status": version["status"],
                "metrics": rows("metrics.jsonl"), "system": rows("system/metrics.jsonl"), "stdout": text("logs/stdout.txt"),
                "stderr": text("logs/stderr.txt"), "error": text("logs/error.txt"), "info": info,
                "config": metadata.get("config"), "sweep": metadata.get("sweep")}

    def _lineage_api(self, store, project: str, ref: Ref, depth: int) -> dict:
        """Inputs to the left and consumers to the right of a version, `depth` levels each way, from one-level lineage calls.

        `nodes` maps each reference to its `inputs` and `consumers` when they were looked up (null: not looked
        up, so the app may offer to expand it). Ancestors are expanded through their inputs only, descendants
        through their consumers only. Lineage never leaves the project. A deleted version keeps its edges; its node says
        `deleted: true` (the key is there only then). References are `project:name@sel`, as the app reads them; every
        node has the version's `short_id` once it was looked up."""
        name, manifest = self._find(store, ref)
        root = exact_ref(name, manifest)
        first = store.lineage(root, marks=True)
        nodes: dict[str, dict] = {root: {"inputs": None, "consumers": None}}
        gone = set(first.get("deleted", []))
        marks: dict[str, dict] = dict(first.get("nodes", {}))
        truncated = False

        marks.setdefault(root, {"status": manifest["status"], "short_id": refs.short_of(manifest)})

        def visible(items: list[str]) -> list[str]:
            return list(items)

        def is_run(item: str) -> bool:
            return marks.get(item, {}).get("kind") == "run"

        for side in ("inputs", "consumers"):
            queue = [(root, 0)]  # a run in between counts no level: the data on its other side comes along
            while queue:
                current, level = queue.pop(0)
                if level >= depth or nodes[current][side] is not None:
                    continue
                if current == root:
                    found = visible(first[side])
                else:
                    try:
                        answer = store.lineage(current, marks=True)
                        gone.update(answer.get("deleted", []))
                        marks.update(answer.get("nodes", {}))
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
                        queue.append((item, level + (0 if is_run(item) else 1)))
                    kept.append(item)
                nodes[current][side] = kept
        for item in gone & nodes.keys():
            nodes[item]["deleted"] = True
        for item, node in nodes.items():
            mark = marks.get(item, {})
            if mark.get("kind"):
                node["kind"] = mark["kind"]
            if mark.get("status") not in (None, "completed", "deleted"):
                node["status"] = mark["status"]
            if mark.get("short_id"):
                node["short_id"] = mark["short_id"]

        def outward(node: dict) -> dict:
            return {key: [qualified(project, item) for item in value] if key in ("inputs", "consumers") and value is not None else value
                    for key, value in node.items()}

        return {"ref": qualified(project, root), "depth": depth, "nodes": {qualified(project, item): outward(node) for item, node in nodes.items()},
                "truncated": truncated}

    # ------------------------------------------------------------------ files

    def _files(self, session: Session, path: str, query: dict) -> None:
        rest = unquote(path)[len("/files/"):]
        project, _, remainder = rest.partition("/")
        head, separator, inner = remainder.partition("//")
        if not separator and head.endswith(".zip") and "@" in head:
            return self._zip(session, project, head[: -len(".zip")])
        if not separator:
            raise HttpError(404, "file URLs look like /files/<project>/<name>@v<n>//<path>")
        ref = refs.parse(f"{head}//{inner}")
        self._allow(session, project)
        read = session.store.get(project).read_file(ref.name, ref.selector, ref.path, self.headers.get("Range"))
        # a pushed HTML file must not act as the UI's origin (its session)
        extra = {"Content-Security-Policy": "sandbox"}
        if query.get("download"):
            extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(ref.path.rsplit('/', 1)[-1])}"
        self._stream(read, entry_type(ref.path), read.immutable, extra)

    def _zip(self, session: Session, project: str, head: str) -> None:
        """`/files/<p>/<name>@<sel>.zip`: the version's files as one zip, streamed while it is written (chunked; ZIP64,
        data descriptors, no temporary file): `<name>-v<n>/<path>` inside, a stored symlink as the file it names;
        already compressed types are stored, the rest deflated."""
        ref = refs.parse(head)
        if ref.path or not ref.selector:
            raise HttpError(404, "zip URLs look like /files/<project>/<name>@v<n>.zip")
        self._allow(session, project)
        store = session.store.get(project)
        version = self._version(store, ref.name, ref.selector)
        if version["status"] == "deleted":
            raise Deleted(f"{exact_ref(ref.name, version)} was deleted: its files are gone")
        exact = selector_of(version)
        top = f"{ref.name.replace('/', '-')}-{exact}"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{top}.zip"')
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "private, max-age=31536000, immutable" if version["status"] == "completed" else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        sink = _ChunkedSink(self.wfile)
        try:
            self._zip_into(sink, store, ref.name, exact, top, version.get("files", []))
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as error:  # noqa: BLE001  (the headers are sent: end the connection, the zip stays incomplete)
            self.close_connection = True
            sys.stderr.write(f"zip of {project}:{ref.name}@{exact} broke off: {error}\n")
            return
        sink.finish()

    @staticmethod
    def _zip_into(sink, store, name: str, exact: str, top: str, files: list[dict]) -> None:
        stamp = time.localtime()[:6]
        with zipfile.ZipFile(sink, "w", allowZip64=True) as archive:
            for entry in sorted(files, key=lambda item: item["path"]):
                try:
                    read = store.read_file(name, exact, entry["path"])
                except (NotFound, StoreError):
                    continue  # a link to a file the version does not have
                info = zipfile.ZipInfo(f"{top}/{entry['path']}", date_time=max(stamp, (1980, 1, 1, 0, 0, 0)))
                info.compress_type = zipfile.ZIP_STORED if Path(entry["path"]).suffix.lower() in STORED else zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                try:
                    with archive.open(info, "w", force_zip64=True) as target:
                        for chunk in read.chunks:
                            view = memoryview(chunk)
                            for start in range(0, len(view), 1 << 20):  # small pieces: the compressor's output stays small
                                target.write(view[start:start + (1 << 20)])
                finally:
                    read.close()

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
        try:
            for chunk in read.chunks:
                self.wfile.write(chunk)
        finally:
            read.close()  # also when the client went away halfway

    # ------------------------------------------------------------------ pages

    @staticmethod
    def _find(store, ref: Ref) -> tuple[str, dict]:
        """(name, manifest) of the version a reference names (any status for `v<n>`, a uid, or a short id)."""
        if hasattr(store, "find"):
            return store.find(ref)
        if ref.exact:
            return ref.name, store.get_version(ref.name, ref.version if ref.version is not None else ref.uid)
        try:
            return store.resolve(ref)
        except NotFound:
            if ref.id is None:
                raise
            found = refs.pick(ref.id, store.ids(ref.id, None if ref.bare else ref.name))
            return found["name"], store.get_version(found["name"], found["uid"])

    @classmethod
    def _version(cls, store, name: str, selector: str | None) -> dict:
        """A version manifest: `v<n>`, a uid, or a short id (whatever its status), else the completed version an alias or
        label names."""
        return cls._find(store, Ref(name, selector))[1]

    def _report(self, session: Session, store, project: str, rest: list[str], url) -> None:
        first = rest[0] if rest else ""
        if first == "":
            return self._send(303, b"", headers={"Location": f"/p/{quote(project)}/"})
        if first == "site.json" and len(rest) == 1:
            return self._json(200, self._site(session, project))
        if first == "_static" and len(rest) == 2:
            return self._static(store, project, rest[1])
        slug, at, selector = first.partition("@")
        name = f"reports/{slug}"
        refs.valid_name(name)
        if len(rest) == 1:  # relative links in the page need the trailing slash
            return self._send(301, b"", headers={"Location": url.path + "/" + (f"?{url.query}" if url.query else "")})
        version = self._version(store, name, selector if at else None)
        inside = "/".join(rest[1:])
        file = f"site/{slug}/{inside}index.html" if inside.endswith("/") or not inside else f"site/{slug}/{inside}"
        if not valid_path(file):
            raise HttpError(404, "no such file")
        if resolve_entry(version["files"], file) is None:
            raise HttpError(404, f"the report has no {file}")
        immutable = re.search(r"(^|&)v=[0-9a-f]+", url.query) is not None
        exact = selector_of(version)
        if resolve_entry(version["files"], file + ".gz") and "gzip" in self.headers.get("Accept-Encoding", ""):
            read = store.read_file(name, exact, file + ".gz")
            return self._stream(read, entry_type(file), immutable, encoding="gzip")
        self._stream(store.read_file(name, exact, file, self.headers.get("Range")), entry_type(file), immutable)

    def _static(self, store, project: str, file: str) -> None:
        """A shared page file (`site/_static/<name>`, named by content hash): the newest report version that has it."""
        if not valid_path(file) or "/" in file:
            raise HttpError(404, "no such file")
        key = (project, file)
        if key not in self.static_cache:
            found = None
            for summary in store.summaries():
                name = summary["name"]
                for version in reversed(store.versions(name)) if is_report(name) else ():
                    if version["status"] == "completed" and resolve_entry(version["files"], f"site/_static/{file}"):
                        found = (name, selector_of(version), resolve_entry(version["files"], f"site/_static/{file}.gz") is not None)
                        break
                if found:
                    break
            if found is None:
                raise HttpError(404, "no such file")
            self.static_cache[key] = found  # where it lives, not its bytes: every read still goes to the store with the session's token
        name, number, compressed = self.static_cache[key]
        if compressed and "gzip" in self.headers.get("Accept-Encoding", ""):
            return self._stream(store.read_file(name, number, f"site/_static/{file}.gz"), entry_type(file), True, encoding="gzip")
        self._stream(store.read_file(name, number, f"site/_static/{file}"), entry_type(file), True)

    @staticmethod
    def _site(session: Session, project: str) -> dict:
        """What a report page's navigation reads (site.json): the project's reports, latest versions, and `groups`,
        the sections of the app's own rail (Projects, then this project's Reports, Artifacts, Runs), as the app lists them."""
        projects_, base = session.store, f"/p/{quote(project)}/"
        store = projects_.get(project)
        reports, artifacts, runs = [], [], []
        for summary in sorted(store.summaries(), key=lambda item: item["updated"], reverse=True):
            name = summary["name"]
            if is_report(name):
                if summary["latest"]:
                    slug, meta = name.split("/", 1)[1], summary["metadata"]
                    reports.append({"key": slug, "title": meta.get("title") or slug, "subtitle": "", "status": meta.get("status", ""),
                                    "summary": meta.get("summary", ""), "href": f"{slug}/", "updated": summary["updated"]})
            elif name.startswith("runs/"):
                last = f"@v{summary['versions']}" if summary["versions"] else ""
                runs.append({"title": name[len("runs/"):], "href": f"{base}a/{quote(name)}{last}", "plain": True})
            else:
                artifacts.append({"title": name, "href": f"{base}a/{quote(name)}", "plain": True})
        reports.sort(key=lambda item: (item["title"].lower(), item["key"]))
        projects = [{"title": item["title"] or item["name"], "href": f"/p/{quote(item['name'])}/", "plain": True, "current": item["name"] == project}
                    for item in sorted(projects_.overviews(), key=lambda item: item["updated"], reverse=True) if can(session.principal, item["name"])]
        groups = [{"label": "Projects", "items": projects},
                  {"label": "Reports", "items": [{"key": r["key"], "title": r["title"], "href": f"{base}r/{quote(r['key'])}/"} for r in reports]},
                  {"label": "Artifacts", "items": artifacts}, {"label": "Runs", "items": runs}]
        return {"title": projects_.record(project)["title"], "brand": "Cairn", "mark": "C", "reports": reports,
                "groups": [g for g in groups if g["items"]]}

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
        self._send(200, b'{"ok":true}', "application/json", {"Set-Cookie": self._start(session), "Cache-Control": "no-store"})

    def _start(self, session) -> str:
        """Keep a new session; returns the Set-Cookie header that hands its random id to the browser."""
        session.expires = time.time() + SESSION_SECONDS
        identifier = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            for key in [key for key, other in self.sessions.items() if other.expires < now]:
                del self.sessions[key]
            if len(self.sessions) >= MAX_SESSIONS:
                del self.sessions[min(self.sessions, key=lambda key: self.sessions[key].expires)]
            self.sessions[identifier] = session
        return self._cookie_header(identifier, SESSION_SECONDS)

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

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):  # the browser went away mid-response: nothing to do
            pass

    def log_message(self, format, *args):  # one line per request, without the session
        sys.stderr.write(f"{self.log_date_time_string()} {self.address_string()} {format % args}\n")


class _ChunkedSink(io.RawIOBase):
    """A write-only stream that sends what is written as HTTP/1.1 chunks of about 256 KB (zipfile writes to it)."""

    SIZE = 256 * 1024

    def __init__(self, wfile):
        self.wfile, self.buffer, self.position = wfile, bytearray(), 0

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        size = len(data)
        self.position += size
        if size >= self.SIZE:  # a large piece goes out as its own chunk, uncopied
            self._send()
            self.wfile.write(b"%X\r\n" % size)
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            return size
        self.buffer += data
        if len(self.buffer) >= self.SIZE:
            self._send()
        return size

    def tell(self) -> int:
        return self.position

    def _send(self) -> None:
        if self.buffer:
            self.wfile.write(b"%X\r\n" % len(self.buffer))
            self.wfile.write(self.buffer)
            self.wfile.write(b"\r\n")
            self.buffer = bytearray()

    def flush(self) -> None:
        self._send()

    def finish(self) -> None:
        self._send()
        self.wfile.write(b"0\r\n\r\n")


def make_ui_server(backend: LocalBackend | RemoteBackend, host: str = "127.0.0.1", port: int = 8801,
                   no_auth: bool = False) -> ThreadingHTTPServer:
    """A bound UI server (not yet serving). With no_auth there is no login and every request uses the
    backend's own access, so it is refused on any address but this machine's loopback."""
    if no_auth and host not in LOCAL_HOSTS:
        raise SystemExit("--no-auth shows the repo to anyone who can connect: only with a loopback --host")
    handler = type("Handler", (UIHandler,), {
        "backend": backend, "sessions": {}, "login_codes": {}, "lock": threading.Lock(), "no_auth": no_auth,
        "static_cache": {}, "fixed": backend.anonymous() if no_auth else None,
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


LOGIN_LINK_SECONDS = 600


def login_link(server: ThreadingHTTPServer, base: str, token: str) -> str:
    """A one-time login link: a random code (never the token) that logs in once, within LOGIN_LINK_SECONDS."""
    code = secrets.token_urlsafe(24)
    handler = server.RequestHandlerClass
    with handler.lock:
        handler.login_codes[code] = (time.time() + LOGIN_LINK_SECONDS, token)
    return f"{base}login?code={code}"


PLAIN_HTTP_WARNING = ("warning: plain HTTP on a network address: use it over a VPN (Tailscale, WireGuard), an SSH tunnel, "
                      "or an HTTPS reverse proxy")

def serve_ui(backend: LocalBackend | RemoteBackend, host: str = "127.0.0.1", port: int = 8801, no_auth: bool = False,
             login_token: str = "", open_browser: bool = False) -> None:
    """Serve the UI. With `login_token`, print a one-time login link for it (and open it with `open_browser`)."""
    try:
        server = make_ui_server(backend, host, port, no_auth=no_auth)
    except OSError as error:
        if not port or error.errno != errno.EADDRINUSE:
            raise
        print(f"port {port} is in use; taking a free one", flush=True)
        server = make_ui_server(backend, host, 0, no_auth=no_auth)
    base = f"http://{url_host(host)}:{server.server_address[1]}/"  # this machine's name for 0.0.0.0
    print(f"UI: {base} (repo: {backend.describe})", flush=True)
    if host not in LOCAL_HOSTS and not os.environ.get("CAIRN_UI_PARENT"):  # beside `cairn server`, it warns for both
        print(PLAIN_HTTP_WARNING, flush=True)
    if no_auth:
        print("Authentication is off (--no-auth): everyone who can connect sees the repo; for local use only", flush=True)
    opening = base
    if login_token and not no_auth:
        opening = login_link(server, base, login_token)
        print(f"Log in: {opening}  (one-time link, valid {LOGIN_LINK_SECONDS // 60} minutes)", flush=True)
    if open_browser:
        import webbrowser

        threading.Timer(0.3, webbrowser.open, (opening,)).start()
    follow_parent()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
