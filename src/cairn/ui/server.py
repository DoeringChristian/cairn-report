"""The UI: a browser view of a repo, as a process of its own.

    cairn ui [--remote URL | --repo FOLDER] [--host 127.0.0.1] [--port 8801] [--no-auth]

It reaches the data only through the repo interface (the project's repo, which reads through its remote when it
has one and shows the versions still waiting for it as pending; a repo folder; or a remote), so it runs next to the
repo or on a laptop against a remote. It shows projects (backends.py): the project's repo is one project, named after
the project folder or the remote's project (`mode: local` in /ui-api/me), a server or a server's repo folder has
many (`mode: server`). Inside a project, references have no project. Every reference the UI reads, from its URLs and
from the app's API calls, is parsed by store/refs.py and nothing else (the app's own `web/src/refs.ts` mirrors it to
tell a name from an id); the JSON names versions by id, with `nodes` saying each one's name, number, and short id. It
serves the React app (web/, built into static/) and a small JSON API for it:

    /, /login, /p/<p>/                           the app (index.html; the app routes on the client)
    /p/<p>/a/<name>[@v<n>|@<alias or label>]     an artifact page at a version (the app)
    /p/<p>/v/<id>                                a version by its id, or a unique prefix of it (the app)
    /assets/<file>                               the app's hashed scripts and styles
    /ui-api/...                                  the app's JSON API (see `_api`), session-scoped
    /p/<p>/r/<slug>[@v<n>]/...                   a report version's built site (latest without @), its `_static/`
                                                 files and a generated `site.json` at /p/<p>/r/
    /files/<p>/<reference>//<path>               the repo's files, with Range (so videos seek)
    /files/<p>/<reference>.zip                   a whole version as a zip, streamed (`<name>-v<n>/...` inside)

Reports are a UI concept: an artifact `reports/<slug>` whose files have `site/` and `source/`.

Login: `cairn ui` prints a one-time link (a random code that works once and does not expire; every launch prints a
new one); or the user enters the token on /login (the app posts it to /ui-api/login). The UI checks it (a remote: an
authenticated API call; a repo on this machine: its token file), keeps it in the process, and gives the browser only
the session id, an HMAC of the token with a fixed label (sessions.py): an HttpOnly, SameSite=Strict cookie with a
Max-Age of ten years, Secure when a reverse proxy says X-Forwarded-Proto: https. So a session survives restarts of the
UI and ends exactly when the token changes; /logout clears the cookie. Every /ui-api/ call also needs the header
`X-Cairn: 1`, which a cross-site form or image cannot send (on top of SameSite=Strict). `--no-auth` (loopback only)
skips the login and uses the client's token (`CAIRN_TOKEN`, `<repo>/token`, `~/.config/cairn/token`) for a remote,
or direct access to a repo on this machine.
"""

from __future__ import annotations

import errno
import hmac
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
from ..term import cause, field, request_line, warn
from .._version import __version__
from ..store import (
    Ambiguous,
    BadRange,
    Deleted,
    FileRead,
    Forbidden,
    InvalidRef,
    NotFound,
    Ref,
    StoreError,
    Unauthorized,
    refs,
)
from ..store.blobs import resolve_entry, valid_path
from ..store.outbox import exact_ref
from .backends import LocalBackend, RemoteBackend
from .sessions import COOKIE, COOKIE_SECONDS, LOGIN_DELAY, Session, session_id

STATIC = Path(__file__).parent / "static"  # the built app (web/ -> npm run build)
MAX_LINEAGE_DEPTH = 3
MAX_LINEAGE_NODES = 200
MAX_LOGIN_BODY = 4096
RUN_TAIL = 64 * 1024  # bytes of logs/stdout.txt and stderr.txt the run panel shows
RUN_ROWS_BYTES = 8 * 1024 * 1024  # the most of a metrics file the run panel reads (its end)
RUN_POINTS = 2000  # rows per chart, at most (evenly thinned)
STORED = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
    ".mkv",
    ".zip",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".zst",
    ".7z",
    ".npz",
    ".pt",
    ".pth",
    ".safetensors",
    ".h5",
    ".parquet",
    ".jxl",
    ".heic",
    ".mp3",
    ".ogg",
}


class NoSession(Unauthorized):
    """A request without a valid session (as opposed to a token the remote refused)."""


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


def message_page(title: str, text: str) -> bytes:
    """A plain page for errors outside the app (the app shows its own for API errors)."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(title)}</title></head><body style="font:16px/1.5 system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem">'
        f'<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p><p><a href="/">Back to the repo</a></p></body></html>'
    ).encode()


def app_route(segments: list[str]) -> bool:
    """Whether a path (split on `/`) is a client-side route of the app."""
    if segments in (["", ""], ["", "login"], ["", "palette"]):
        return True
    return (
        len(segments) >= 4
        and segments[1] == "p"
        and (
            segments[3] == ""
            and len(segments) == 4
            or segments[3] == "a"
            and len(segments) > 4
            or segments[3] == "v"
            and len(segments) == 5
        )
    )


def file_ref(name: str, version: dict, path: str | None = None) -> Ref:
    """The exact reference of a version (`name@v<n>`, or its id while it has no number), at `path` inside it."""
    if version.get("version") is not None:
        return Ref(name, f"v{version['version']}", path)
    return Ref.of(version["id"], path)


def summarize_version(version: dict, short: str) -> dict:
    """A version without its file list: the table of versions needs only counts (a tombstone keeps those it had)."""
    files = version.get("files", [])
    version = {**version, "short_id": short}
    slim = {key: value for key, value in version.items() if key not in ("files", "inputs")}
    counts = {"file_count": len(files), "size": sum(entry.get("size", 0) for entry in files)}
    if version.get("status") == "deleted":
        counts = {"file_count": version.get("file_count", 0), "size": version.get("size", 0)}
    return {**slim, "input_count": len(version.get("inputs", [])), **counts}


class UIHandler(BaseHTTPRequestHandler):
    backend: LocalBackend | RemoteBackend
    state: dict  # "token": the token the UI acts with ("" before the first login), "session": its Session
    login_codes: set
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
            token, session = self.state.get("token"), self.state.get("session")
        if not value or not token or not hmac.compare_digest(value.encode(), session_id(token).encode()):
            return None
        if session is None:  # the first request after a restart: the token is known, its store is opened now
            session = self.backend.login(token)
            if session is None:
                return None
            with self.lock:
                self.state["session"] = session
        return session

    def _forget(self) -> None:
        """The remote refused the token: no session works until the next login."""
        with self.lock:
            self.state.update(token="", session=None)

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
        except Ambiguous as error:  # an id prefix of several versions: the app lists them
            if self._is_api():
                return self._json(409, {"error": str(error), "candidates": error.candidates})
            self._error(409, str(error))
        except NotFound as error:
            self._error(404, str(error))
        except Forbidden as error:
            self._error(403, str(error))
        except Unauthorized as error:
            if self.no_auth:
                return self._error(
                    401, "the remote rejected the token from CAIRN_TOKEN, <repo>/token, or ~/.config/cairn/token"
                )
            if not isinstance(error, NoSession):  # the remote refused the token itself
                self._forget()
            if self._is_api() or urlparse(self.path).path.startswith("/files/"):
                return self._send(
                    401,
                    json.dumps({"error": "log in first"}).encode(),
                    "application/json",
                    {"Set-Cookie": self._cookie_header("", 0), "Cache-Control": "no-store"},
                )
            self._send(
                303,
                b"",
                headers={"Location": "/login?next=" + quote(self.path), "Set-Cookie": self._cookie_header("", 0)},
            )
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
            return self._asset(path[len("/assets/") :])
        if path == "/login":
            if self.no_auth:
                raise HttpError(404, "no such page")
            if "code" in query:  # the one-time link `cairn ui` prints and opens: a login without typing the token
                with self.lock:
                    known = query["code"][0] in self.login_codes
                    self.login_codes.discard(query["code"][0])
                token = self.state.get("login", "") if known else ""
                session = self.backend.login(token) if token else None
                if session is not None:
                    return self._send(
                        303,
                        b"",
                        headers={
                            "Location": "/",
                            "Set-Cookie": self._start(token, session),
                            "Cache-Control": "no-store",
                        },
                    )
                return self._send(303, b"", headers={"Location": "/login"})  # used already: the token form
            if self._session() is not None:
                return self._send(303, b"", headers={"Location": safe_target(query.get("next", ["/"])[0])})
            return self._app()
        if path == "/logout":
            return self._send(
                303,
                b"",
                headers={"Location": "/" if self.no_auth else "/login", "Set-Cookie": self._cookie_header("", 0)},
            )
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
            refs.valid_project(segments[2])
            return self._report(session, session.store.get(segments[2]), segments[2], segments[4:], url)
        raise HttpError(404, "no such page")

    # ------------------------------------------------------------------ the app

    def _app(self) -> None:
        index = STATIC / "index.html"
        if not index.is_file():
            return self._send(
                503,
                message_page(
                    "The UI is not built",
                    "Run `npm ci && npm run build` in web/ (a released package ships the built app).",
                ),
                "text/html; charset=utf-8",
            )
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
            raise NoSession()
        store = session.store
        match [unquote(part) for part in url.path.split("/")[2:]]:
            case ["me"]:
                return self._json(
                    200, {"auth": not self.no_auth, "mode": store.mode, "home": getattr(store, "name", None)}
                )
            case ["projects"]:
                found = list(store.overviews())
                return self._json(200, sorted(found, key=lambda item: item["updated"], reverse=True))
            case ["projects", project]:
                repo = store.get(project)
                return self._json(200, {"project": store.record(project), "artifacts": repo.summaries()})
            case ["projects", project, "artifacts", *rest] if rest:
                return self._json(200, self._artifact_api(store.get(project), project, refs.valid_name("/".join(rest))))
            case ["projects", project, "version"]:  # ?ref=: any of the four forms (refs.parse)
                ref = refs.parse(query.get("ref", [""])[0])
                return self._json(200, self._version_api(store.get(project), project, ref))
            case ["projects", project, "run"]:
                ref = refs.parse(query.get("ref", [""])[0])
                return self._json(200, self._run_api(store.get(project), project, ref))
            case ["projects", project, "lineage"]:
                ref = refs.parse(query.get("ref", [""])[0])
                depth = int(query.get("depth", ["1"])[0])
                if not 1 <= depth <= MAX_LINEAGE_DEPTH:
                    raise ValueError(f"depth must be between 1 and {MAX_LINEAGE_DEPTH}")
                return self._json(200, self._lineage_api(store.get(project), project, ref, depth))
        raise HttpError(404, "no such API route")

    @staticmethod
    def _shorts(repo, ids) -> dict[str, str]:
        """{id: short id} of versions of the project (the first 7 digits for any the repo cannot say)."""
        ids = [id for id in dict.fromkeys(ids) if id]
        try:
            found = repo.short_ids(ids) if ids else {}
        except (StoreError, ValueError):
            found = {}
        return {id: found.get(id) or id[: refs.SHORT] for id in ids}

    @classmethod
    def _artifact_api(cls, repo, project: str, name: str) -> dict:
        versions = repo.versions(name)
        if not versions:
            raise NotFound(f"no artifact {name} in the project {project}")
        shorts = cls._shorts(repo, [version["id"] for version in versions])
        return {
            "project": project,
            "name": name,
            "summary": repo.summary(name),
            "versions": [summarize_version(version, shorts[version["id"]]) for version in versions],
            "aliases": repo.aliases(name),
        }

    def _version_api(self, repo, project: str, ref: Ref) -> dict:
        """One version (any of the four reference forms, refs.parse): its manifest (files, metadata, inputs as ids), its
        consumers (ids), the ids among them that were deleted, and `nodes` {id: {name, version, short_id, status,
        kind}} for each, so the app shows them without parsing anything."""
        name, version = self._find(repo, ref)
        consumers, deleted, nodes = [], [], {}
        if version["status"] in ("completed", "deleted", "running", "failed") and not version.get("pending"):
            found = repo.lineage(Ref.of(version["id"]), marks=True)  # a deleted version keeps its edges
            consumers, deleted, nodes = found["consumers"], found.get("deleted", []), dict(found.get("nodes", {}))
        shorts = self._shorts(repo, [version["id"], *(version.get("inputs") or []), *consumers])
        for id in shorts:
            nodes.setdefault(id, {"name": None, "version": None, "status": None})
            nodes[id]["short_id"] = shorts[id]
        nodes[version["id"]].update(name=name, version=version.get("version"), status=version["status"])
        return {
            "project": project,
            "name": name,
            "ref": exact_ref(name, version),
            "version": {**version, "short_id": shorts[version["id"]]},
            "consumers": consumers,
            "deleted": deleted,
            "nodes": nodes,
        }

    def _run_api(self, store, project: str, ref: Ref) -> dict:
        """What the app's Run panel shows of a run version: its metrics.jsonl and system/metrics.jsonl rows (numbers
        only, thinned to RUN_POINTS), the tails of logs/stdout.txt, stderr.txt, and error.txt, system/info.json, and
        the config. A running version answers with what it has synced so far."""
        name, version = self._find(store, ref)
        if version["status"] == "deleted":
            raise Deleted(f"{exact_ref(name, version)} was deleted")
        files = {entry["path"]: entry for entry in version.get("files", [])}

        def read(path: str, limit: int) -> tuple[bytes, bool]:
            entry = files.get(path)
            if entry is None:
                return b"", False
            size = entry.get("size") or 0
            wanted = f"bytes=-{limit}" if size > limit else None
            try:
                found = store.read_file(file_ref(name, version, path), wanted)
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
                if (
                    isinstance(row.get("name"), str)
                    and "value" in row
                    and set(row) <= {"name", "value", "step", "time", "origin", "nonfinite"}
                ):
                    row = {
                        row["name"]: row["value"],
                        "step": row.get("step"),
                        "time": row.get("time"),
                    }  # an earlier release's line
                keep = {
                    key: value
                    for key, value in row.items()
                    if key == "time" or (isinstance(value, (int, float)) and not isinstance(value, bool))
                }
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
        return {
            "project": project,
            "name": name,
            "version": version["version"],
            "id": version["id"],
            "short_id": self._shorts(store, [version["id"]])[version["id"]],
            "status": version["status"],
            "metrics": rows("metrics.jsonl"),
            "system": rows("system/metrics.jsonl"),
            "stdout": text("logs/stdout.txt"),
            "stderr": text("logs/stderr.txt"),
            "error": text("logs/error.txt"),
            "info": info,
            "config": metadata.get("config"),
            "sweep": metadata.get("sweep"),
        }

    def _lineage_api(self, store, project: str, ref: Ref, depth: int) -> dict:
        """Inputs to the left and consumers to the right of a version, `depth` levels each way, from one-level lineage
        calls; every version is named by its id.

        `nodes` maps each id to its `inputs` and `consumers` (ids) when they were looked up (null: not looked up, so the
        app may offer to expand it), its `name`, `version` (number, or null while pending), and `short_id`. Ancestors
        are expanded through their inputs only, descendants through their consumers only. Lineage never leaves the
        project. A deleted version keeps its edges; its node says `deleted: true` (the key is there only then)."""
        name, manifest = self._find(store, ref)
        root = manifest["id"]
        first = store.lineage(Ref.of(root), marks=True)
        nodes: dict[str, dict] = {root: {"inputs": None, "consumers": None}}
        gone = set(first.get("deleted", []))
        marks: dict[str, dict] = dict(first.get("nodes", {}))
        truncated = False
        marks.setdefault(root, {"name": name, "version": manifest.get("version"), "status": manifest["status"]})

        def is_run(item: str) -> bool:
            return marks.get(item, {}).get("kind") == "run"

        for side in ("inputs", "consumers"):
            queue = [(root, 0)]  # a run in between counts no level: the data on its other side comes along
            while queue:
                current, level = queue.pop(0)
                if level >= depth or nodes[current][side] is not None:
                    continue
                if current == root:
                    found = list(first[side])
                else:
                    try:
                        answer = store.lineage(Ref.of(current), marks=True)
                        gone.update(answer.get("deleted", []))
                        marks.update(answer.get("nodes", {}))
                        found = list(answer[side])
                    except (NotFound, StoreError, Forbidden, ValueError):
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
        shorts = self._shorts(store, nodes)
        for item, node in nodes.items():
            mark = marks.get(item, {})
            node.update(name=mark.get("name"), version=mark.get("version"), short_id=shorts.get(item))
            if item in gone:
                node["deleted"] = True
            if mark.get("kind"):
                node["kind"] = mark["kind"]
            if mark.get("status") not in (None, "completed", "deleted"):
                node["status"] = mark["status"]
        return {"ref": root, "depth": depth, "nodes": nodes, "truncated": truncated}

    # ------------------------------------------------------------------ files

    def _files(self, session: Session, path: str, query: dict) -> None:
        rest = unquote(path)[len("/files/") :]
        project, _, remainder = rest.partition("/")
        head, separator, inner = remainder.partition("//")
        if not separator and head.endswith(".zip"):
            return self._zip(session, project, head[: -len(".zip")])
        if not separator:
            raise HttpError(404, "file URLs look like /files/<project>/<reference>//<path>")
        ref = refs.parse(f"{head}//{inner}")
        read = session.store.get(project).read_file(ref, self.headers.get("Range"))
        # a pushed HTML file must not act as the UI's origin (its session)
        extra = {"Content-Security-Policy": "sandbox"}
        if query.get("download"):
            extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(ref.path.rsplit('/', 1)[-1])}"
        self._stream(read, entry_type(ref.path), read.immutable, extra)

    def _zip(self, session: Session, project: str, head: str) -> None:
        """`/files/<p>/<reference>.zip`: the version's files as one zip, streamed while it is written (chunked; ZIP64,
        data descriptors, no temporary file): `<name>-v<n>/<path>` inside, a stored symlink as the file it names;
        already compressed types are stored, the rest deflated."""
        ref = refs.parse(head)
        if ref.path:
            raise HttpError(404, "zip URLs look like /files/<project>/<reference>.zip")
        store = session.store.get(project)
        name, version = self._find(store, ref)
        if version["status"] == "deleted":
            raise Deleted(f"{exact_ref(name, version)} was deleted: its files are gone")
        number = f"v{version['version']}" if version.get("version") is not None else version["id"][: refs.SHORT]
        top = f"{name.replace('/', '-')}-{number}"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{top}.zip"')
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header(
            "Cache-Control", "private, max-age=31536000, immutable" if version["status"] == "completed" else "no-cache"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        sink = _ChunkedSink(self.wfile)
        try:
            self._zip_into(sink, store, name, version, top, version.get("files", []))
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as error:  # noqa: BLE001  (the headers are sent: end the connection, the zip stays incomplete)
            self.close_connection = True
            sys.stderr.write(f"zip of {project}:{exact_ref(name, version)} broke off: {cause(error)}\n")
            return
        sink.finish()

    @staticmethod
    def _zip_into(sink, store, name: str, version: dict, top: str, files: list[dict]) -> None:
        stamp = time.localtime()[:6]
        with zipfile.ZipFile(sink, "w", allowZip64=True) as archive:
            for entry in sorted(files, key=lambda item: item["path"]):
                try:
                    read = store.read_file(file_ref(name, version, entry["path"]))
                except (NotFound, StoreError):
                    continue  # a link to a file the version does not have
                info = zipfile.ZipInfo(f"{top}/{entry['path']}", date_time=max(stamp, (1980, 1, 1, 0, 0, 0)))
                info.compress_type = (
                    zipfile.ZIP_STORED if Path(entry["path"]).suffix.lower() in STORED else zipfile.ZIP_DEFLATED
                )
                info.external_attr = 0o644 << 16
                try:
                    with archive.open(info, "w", force_zip64=True) as target:
                        for chunk in read.chunks:
                            view = memoryview(chunk)
                            for start in range(
                                0, len(view), 1 << 20
                            ):  # small pieces: the compressor's output stays small
                                target.write(view[start : start + (1 << 20)])
                finally:
                    read.close()

    def _stream(
        self, read: FileRead, kind: str, immutable: bool, extra: dict | None = None, encoding: str | None = None
    ) -> None:
        etag = f'"{read.sha256}"'
        headers = {
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
            "Vary": "Accept-Encoding",
            "Cache-Control": "private, max-age=31536000, immutable" if immutable else "no-cache",
            **(extra or {}),
        }
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
        """(name, manifest) of the version a reference names: any status for an id or `name@v<n>`, a completed one for
        an alias, a label, or a bare name. The repo's own `find` does it all; nothing is tried in turn here."""
        return store.find(ref)

    def _report(self, session: Session, store, project: str, rest: list[str], url) -> None:
        first = rest[0] if rest else ""
        if first == "":
            return self._send(303, b"", headers={"Location": f"/p/{quote(project)}/"})
        if first == "site.json" and len(rest) == 1:
            return self._json(200, self._site(session, project))
        if first == "_static" and len(rest) == 2:
            return self._static(store, project, rest[1])
        ref = refs.parse(f"reports/{first}")  # reports/<slug>[@v<n>|@<alias or label>]
        if ref.path or ref.name.count("/") != 1:
            raise HttpError(404, "report URLs look like /p/<project>/r/<slug>[@v<n>]/")
        name, slug = ref.name, ref.name.split("/", 1)[1]
        if len(rest) == 1:  # relative links in the page need the trailing slash
            return self._send(301, b"", headers={"Location": url.path + "/" + (f"?{url.query}" if url.query else "")})
        name, version = self._find(store, ref)
        inside = "/".join(rest[1:])
        file = f"site/{slug}/{inside}index.html" if inside.endswith("/") or not inside else f"site/{slug}/{inside}"
        if not valid_path(file):
            raise HttpError(404, "no such file")
        if resolve_entry(version["files"], file) is None:
            raise HttpError(404, f"the report has no {file}")
        immutable = re.search(r"(^|&)v=[0-9a-f]+", url.query) is not None
        if resolve_entry(version["files"], file + ".gz") and "gzip" in self.headers.get("Accept-Encoding", ""):
            read = store.read_file(file_ref(name, version, file + ".gz"))
            return self._stream(read, entry_type(file), immutable, encoding="gzip")
        self._stream(
            store.read_file(file_ref(name, version, file), self.headers.get("Range")), entry_type(file), immutable
        )

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
                        found = (
                            name,
                            version,
                            resolve_entry(version["files"], f"site/_static/{file}.gz") is not None,
                        )
                        break
                if found:
                    break
            if found is None:
                raise HttpError(404, "no such file")
            self.static_cache[key] = (
                found  # where it lives, not its bytes: every read still goes to the store with the session's token
            )
        name, version, compressed = self.static_cache[key]
        if compressed and "gzip" in self.headers.get("Accept-Encoding", ""):
            return self._stream(
                store.read_file(file_ref(name, version, f"site/_static/{file}.gz")),
                entry_type(file),
                True,
                encoding="gzip",
            )
        self._stream(store.read_file(file_ref(name, version, f"site/_static/{file}")), entry_type(file), True)

    @staticmethod
    def _site(session: Session, project: str) -> dict:
        """What a report page's navigation reads (site.json): the project's reports, latest versions, and `groups`,
        the sections of the app's own rail (Projects, then this project's Reports, Artifacts, Runs), as the app lists them.
        """
        projects_, base = session.store, f"/p/{quote(project)}/"
        store = projects_.get(project)
        reports, artifacts, runs = [], [], []
        for summary in sorted(store.summaries(), key=lambda item: item["updated"], reverse=True):
            name = summary["name"]
            if is_report(name):
                if summary["latest"]:
                    slug, meta = name.split("/", 1)[1], summary["metadata"]
                    reports.append(
                        {
                            "key": slug,
                            "title": meta.get("title") or slug,
                            "subtitle": "",
                            "status": meta.get("status", ""),
                            "summary": meta.get("summary", ""),
                            "href": f"{slug}/",
                            "updated": summary["updated"],
                        }
                    )
            elif name.startswith("runs/"):
                last = f"@v{summary['latest']}" if summary.get("latest") else ""
                runs.append({"title": name[len("runs/") :], "href": f"{base}a/{quote(name)}{last}", "plain": True})
            else:
                artifacts.append({"title": name, "href": f"{base}a/{quote(name)}", "plain": True})
        reports.sort(key=lambda item: (item["title"].lower(), item["key"]))
        projects = [
            {
                "title": item["title"] or item["name"],
                "href": f"/p/{quote(item['name'])}/",
                "plain": True,
                "current": item["name"] == project,
            }
            for item in sorted(projects_.overviews(), key=lambda item: item["updated"], reverse=True)
        ]
        groups = [
            {"label": "Projects", "items": projects},
            {
                "label": "Reports",
                "items": [
                    {"key": r["key"], "title": r["title"], "href": f"{base}r/{quote(r['key'])}/"} for r in reports
                ],
            },
            {"label": "Artifacts", "items": artifacts},
            {"label": "Runs", "items": runs},
        ]
        return {
            "title": projects_.record(project)["title"],
            "brand": "Cairn",
            "mark": "C",
            "reports": reports,
            "groups": [g for g in groups if g["items"]],
        }

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
        self._send(
            200,
            b'{"ok":true}',
            "application/json",
            {"Set-Cookie": self._start(given, session), "Cache-Control": "no-store"},
        )

    def _start(self, token: str, session: Session) -> str:
        """Act with `token` from now on; returns the Set-Cookie header that hands its session id to the browser."""
        with self.lock:
            self.state.update(token=token, session=session)
        return self._cookie_header(session_id(token), COOKIE_SECONDS)

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
        sys.stderr.write(f"{time.strftime('%H:%M:%S')}  {self.address_string()}  {format % args}\n")

    def log_request(self, code="-", size="-"):  # the request log's line, the same as the API's
        status = getattr(code, "value", code)
        sys.stderr.write(
            request_line(self.address_string(), self.command or "-", (self.path or "").split("?")[0], status) + "\n"
        )


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


def make_ui_server(
    backend: LocalBackend | RemoteBackend,
    host: str = "127.0.0.1",
    port: int = 8801,
    no_auth: bool = False,
    login_token: str = "",
) -> ThreadingHTTPServer:
    """A bound UI server (not yet serving). `login_token`: the token the login link logs in with, and whose session id
    a browser's cookie from an earlier launch carries. With no_auth there is no login and every request uses the
    backend's own access, so it is refused on any address but this machine's loopback."""
    if no_auth and host not in LOCAL_HOSTS:
        raise SystemExit("cairn: --no-auth needs a loopback --host (it shows the repo to anyone)")
    handler = type(
        "Handler",
        (UIHandler,),
        {
            "backend": backend,
            "state": {"token": login_token, "login": login_token, "session": None},
            "login_codes": set(),
            "lock": threading.Lock(),
            "no_auth": no_auth,
            "static_cache": {},
            "fixed": backend.anonymous() if no_auth else None,
        },
    )
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


def login_link(server: ThreadingHTTPServer, base: str) -> str:
    """A one-time login link: a random code (never the token) that logs in once with the server's login token."""
    code = secrets.token_urlsafe(24)
    handler = server.RequestHandlerClass
    with handler.lock:
        handler.login_codes.add(code)
    return f"{base}login?code={code}"


PLAIN_HTTP_WARNING = "plain HTTP: use a VPN (Tailscale), an SSH tunnel, or an HTTPS proxy"


def serve_ui(
    backend: LocalBackend | RemoteBackend,
    host: str = "127.0.0.1",
    port: int = 8801,
    no_auth: bool = False,
    login_token: str = "",
    open_browser: bool = False,
) -> None:
    """Serve the UI. With `login_token`, print a one-time login link for it (and open it with `open_browser`).
    Beside `cairn server` (CAIRN_UI_PARENT set) it prints only its lines of the server's startup block."""
    try:
        server = make_ui_server(backend, host, port, no_auth=no_auth, login_token=login_token)
    except OSError as error:
        if not port or error.errno != errno.EADDRINUSE:
            raise
        server = make_ui_server(backend, host, 0, no_auth=no_auth, login_token=login_token)
        print(f"port {port} in use; using {server.server_address[1]}", flush=True)
    base = f"http://{url_host(host)}:{server.server_address[1]}/"  # this machine's name for 0.0.0.0
    beside = bool(os.environ.get("CAIRN_UI_PARENT"))  # the server prints the rest of the block (and the warnings)
    if beside:
        print(field("  UI", base), flush=True)
    else:
        print(field("Cairn UI", base), flush=True)
        print(field("  Repo", backend.describe), flush=True)
        if no_auth:
            print(field("  Auth", "off (--no-auth): everyone who can connect sees the repo"), flush=True)
    opening = base
    if login_token and not no_auth:
        opening = login_link(server, base)
        print(field("  Log in", f"{opening}   (works once)"), flush=True)
    if host not in LOCAL_HOSTS and not beside:
        warn(PLAIN_HTTP_WARNING)
    if not beside:
        print("Serving; Ctrl-C stops it.", flush=True)
    if open_browser:
        import webbrowser

        threading.Timer(0.3, webbrowser.open, (opening,)).start()
    follow_parent()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
