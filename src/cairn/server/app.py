"""`cairn server`: a repo (a LocalRepo folder) shared over HTTP, behind one token: the remote of other machines' repos.
Only a JSON API and file downloads, no pages (`cairn ui` explores a repo in a browser, locally or against this server).

A server hosts many projects in its one repo folder: each is served at `<server>/<project>` as if it were its own
repo (a LocalProject, store/project.py), and they share the folder's one database and one content-addressed
`blobs/`, so a file several projects publish is stored once. Nothing reaches across projects: references and inputs
name artifacts of the project they are sent to. Serving a project's own repo (`cairn server` in a project), its one
project is served under the project folder's name (`own`).

    cairn server [repo] [--host 127.0.0.1] [--port 8800] [--max-blob-gb 50] [--no-auth] [--gc-interval 24h]

Processes on the server's machine keep opening the repo directly, whether the server runs or not; the server is one
more such process. A new repo gets a fresh owner token on first launch; every launch prints `Repo: <url>` and
`Token: <owner token>` and the token file's path to the terminal (never into a URL). That one token is the only
credential: it reaches every project and creates one on its first sync. The server speaks plain HTTP: bind localhost,
or use it over a VPN (Tailscale) or an SSH tunnel, or put an HTTPS reverse proxy in front.

Artifact names are slash paths (`datasets/crops`), so every route says where the name ends. A version in a path is
`v<n>` or a whole id (`<sel>`, made by code: refs.version_segment); a reference in a query or a /files path is any of
the four forms, parsed by store/refs.py alone.

    API (JSON, `Authorization: Bearer <token>`; a name is ONE percent-encoded path segment, `datasets%2Fcrops`)
      GET  /api/v3/repo                                          {id, cairn, api}: the handshake (the repo's id, the
                                                                 server's release, the API version: 3)
      GET  /api/v3/projects                                      [{..., artifacts, updated}]   POST {name, title?, description?}
      GET  /api/v3/projects/<p>                                  PATCH {title?, description?}
      GET  /api/v3/projects/<p>/artifacts                        [{name, latest, versions, aliases, updated, metadata}]
      GET  /api/v3/projects/<p>/artifacts/<name>                 that summary plus `latest_version` (its manifest, or null)
      GET  /api/v3/projects/<p>/artifacts/<name>/versions        every version, full manifests
      GET  /api/v3/projects/<p>/artifacts/<name>/versions/<sel>  one manifest   .../files  {files: [entries]}
      GET  /api/v3/projects/<p>/artifacts/<name>/aliases         {alias: version}
      POST /api/v3/projects/<p>/artifacts/<name>/versions        {id?, label?, metadata?, source?} -> {version, id} (running)
      POST .../versions/<sel>/files {entries}   (409 + `missing` when blobs are absent)   .../complete {inputs?, digest?}   .../fail
      POST .../versions/<sel>/metadata {metadata}                replace the metadata of a running version
      POST /api/v3/projects/<p>/artifacts/<name>/publish         {id?, entries, label?, metadata?, inputs?, aliases?, source?, digest?}
                                                                 -> {version, id, created}  (created=false: nothing changed,
                                                                 or this id was published already)
      POST /api/v3/projects/<p>/artifacts/<name>/aliases         {alias, version}
      GET  /api/v3/projects/<p>/resolve?ref=<ref>                {name, version: manifest}: the completed version it names
      GET  /api/v3/projects/<p>/find?ref=<ref>                   {name, version: manifest}: any status for an id or name@v<n>
                                                                 (409 + `candidates`: an ambiguous id prefix, either route)
      GET  /api/v3/projects/<p>/ids?prefix=<hex>[&name=<name>]   {versions: [{name, version, id, short_id, status}]}
      POST /api/v3/projects/<p>/short-ids {ids: [...]}           {ids: {id: short id}}
      GET  /api/v3/projects/<p>/lineage?ref=<ref>                {id, inputs, consumers, deleted, nodes: {id: {name, version,
                                                                 status, kind, short_id}}}  (inputs, consumers: ids)
      POST /api/v3/blobs/missing {sha256: [...]}                 PUT /api/v3/blobs/<sha256> (streamed, hash-verified)
    Deleting (`force=1` deletes protected versions too; 409 + `protected` when refused)
      DELETE /api/v3/projects/<p>/artifacts/<name>/versions/<sel>  [?force=1&reason=...] -> {deleted: [refs], ids, skipped}
      DELETE /api/v3/projects/<p>/artifacts/<name>               every version of it, under the same rules
      DELETE /api/v3/projects/<p>                                only when no version is left (or force)
      GET/PUT /api/v3/projects/<p>/retention                     {rules: [{prefix, keep_last, keep_days, keep_aliased}]}
      POST /api/v3/gc {dry_run (default true), grace (seconds or "24h")}   retention rules, then the blob sweep
    Files (the reference ends at `//`; the path inside the version follows)
      GET  /files/<p>/<reference>//<path>                        ETag, Range, immutable for `name@v<n>` or a whole id of a
                                                                 completed version, `?download=1` for an attachment
    Anything under /api/v2/ (Cairn 0.14's API) is refused with 400 and a message: a server and its clients upgrade
    together.

A client may supply a version's `id` when it creates it: sending the same version again is answered with the version
it made (a repo sending its outbox can retry safely), and a known id with different content is refused with 409.

Tokens are bearer tokens only: there are no cookies and no pages. A version never changes once final, except to
be deleted (a tombstone: 410 Gone for its files and references); the server never executes pushed code. Files are served with `Content-Security-Policy: sandbox`.

The server is Starlette on uvicorn (uvloop and httptools: the `[server]` extra). API work runs in worker
threads, so the event loop never waits on the disk; uploads stream into the blob store in 4 MB pieces
(hashed and written in a worker thread while the next piece arrives); downloads use `sendfile` (files.py).
Completed versions' files and manifests are sent with `immutable` caching, and JSON answers carry a content
ETag (`If-None-Match` gets 304).
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, unquote

from .._version import API, API_PREFIX, __version__
from ..store import (
    OWN,
    Ambiguous,
    BadRange,
    Conflict,
    Deleted,
    LocalProject,
    LocalRepo,
    MissingBlobs,
    NotFound,
    Protected,
    StoreError,
    refs,
)
from ..store.gc import GRACE, describe_bytes, parse_duration
from ..store.blobs import SHA256, clean_entries
from ..store.db import ServerLock
from ..store.digest import PREFIX as DIGEST_PREFIX
from ..store.tokens import init_root, read_token
from ..store.ids import valid_id
from .auth import Auth, HttpError
from .files import serve_file
from ..hosts import LOCAL_HOSTS, url_host
from .spawn import forward, start_ui, stop
from ..term import field, short_path, warn

try:  # the [server] extra
    import uvicorn
    from starlette.requests import ClientDisconnect, Request
    from starlette.responses import Response
except ImportError as error:  # pragma: no cover - checked by make_server
    MISSING: ImportError | None = error
else:
    MISSING = None

MAX_JSON = 64 * 1024 * 1024
OLD_CLIENT = (
    f"this server runs Cairn {__version__} (API v{API}); this client is older (Cairn 0.14, API v2): a server and its "
    "clients upgrade together: install Cairn 0.15 here too, then run `cairn upgrade` in each project"
)
UPLOAD_PIECE = 4 << 20  # uploads are hashed and written in pieces this large, in worker threads
WORKERS = int(os.environ.get("CAIRN_SERVER_THREADS", "32"))  # worker threads for store work and file I/O
DIGEST = re.compile(re.escape(DIGEST_PREFIX) + r"[0-9a-f]{64}")
METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE")


def _require_extra() -> None:
    if MISSING is not None:
        raise SystemExit(f'cairn: `cairn server` needs the server extra: pip install "cairn[server]" ({MISSING})')


class Answer:
    """A response computed in a worker thread: status, body bytes, and headers (made a Starlette Response on the loop)."""

    __slots__ = ("status", "body", "headers")

    def __init__(self, status: int, body: bytes, headers: dict):
        self.status, self.body, self.headers = status, body, headers


def _json(status: int, value, cache: str = "no-store") -> Answer:
    return Answer(status, json.dumps(value).encode(), {"Content-Type": "application/json", "Cache-Control": cache})


def _error(status: int, message: str, headers: dict | None = None) -> Answer:
    answer = _json(status, {"error": message})
    answer.headers.update(headers or {})
    return answer


def _translate(error: Exception) -> Answer:
    """The status an exception from a route stands for (the same mapping the API always had)."""
    if isinstance(error, HttpError):
        return _error(error.status, error.message)
    if isinstance(error, BadRange):
        return Answer(416, b"", {"Content-Range": f"bytes */{error.total}"})
    if isinstance(error, Deleted):
        return _error(410, str(error))
    if isinstance(error, MissingBlobs):
        return _json(409, {"error": f"upload these blobs first ({error})", "missing": error.missing})
    if isinstance(error, Protected):
        return _json(409, {"error": str(error), "protected": error.protected})
    if isinstance(error, Ambiguous):
        return _json(409, {"error": str(error), "candidates": error.candidates})
    if isinstance(error, NotFound):
        return _error(404, str(error))
    if isinstance(error, Conflict):
        return _error(409, str(error))
    if isinstance(error, StoreError):
        return _error(409 if "can no longer change" in str(error) else 400, str(error))
    if isinstance(error, ValueError):  # invalid names, entries, JSON
        return _error(400, str(error))
    raise error


def _raise_first(outcomes) -> None:
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


def _segments(path: str) -> list[str]:
    return [unquote(part) for part in path.split("/")]


class StoreApp:
    """The ASGI app: Starlette's requests and responses, dispatched here on the raw path (names travel
    percent-encoded as one segment, `datasets%2Fcrops`, which a router matching decoded paths would split; and
    one dispatch is cheaper than a middleware stack). API work runs in worker threads (the store blocks on
    files); uploads and downloads stream on the event loop with their disk work in worker threads."""

    def __init__(self, store: LocalRepo, auth: Auth, max_blob: int, workers: int = WORKERS, own: str | None = None):
        """`own`: the name the repo folder's own project (store/local.py OWN) is served under (None: it is not)."""
        self.store, self.auth, self.max_blob, self.own = store, auth, max_blob, own
        self.executor = ThreadPoolExecutor(workers, thread_name_prefix="cairn-store")
        self.log = default_log
        self.zero_copy_sends = 0
        self._listings: dict[str, tuple] = {}

    # ------------------------------------------------------------------ ASGI

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        scope["cairn.send"] = send  # the server's own `send` (files.py reaches its transport for sendfile)
        status = [0]

        async def logged(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            try:
                response = await self.handle(Request(scope, receive))
            except Exception:  # a bug: answer 500 and keep serving
                traceback.print_exc()
                response = self._response(_error(500, "internal error"))
            await response(scope, receive, logged)
        finally:
            if self.log is not None:
                path = scope.get("raw_path") or scope["path"].encode()
                target = path.decode("latin-1")  # no query string: a login code or a filter stays out of the log
                self.log(f'{scope["method"]} {target}  {status[0] or "-"}', scope.get("client"))

    async def handle(self, request: Request):
        raw = (request.scope.get("raw_path") or request.scope["path"].encode()).decode("latin-1")
        method = request.method
        try:
            if method == "GET" and raw.startswith("/files/"):
                self.auth.require(request.headers.get("authorization"))
                query = parse_qs(request.scope.get("query_string", b"").decode("latin-1"))
                return await serve_file(self, request, raw, query)
            if raw.startswith("/api/v2/") or raw == "/api/v2":
                return self._response(_error(400, OLD_CLIENT))
            if method == "PUT" and raw.startswith(f"{API_PREFIX}/blobs/"):
                return self._response(await self._put(request, raw))
            if method not in METHODS:
                return self._response(_error(501, f"unsupported method {method}"))
            body = b""
            if method != "GET":
                self.auth.require(request.headers.get("authorization"))
                body = await self._body(request, MAX_JSON)
            loop = asyncio.get_running_loop()
            answer = await loop.run_in_executor(
                self.executor,
                self._api,
                method,
                raw,
                request.scope.get("query_string", b""),
                request.headers.get("authorization"),
                request.headers.get("if-none-match"),
                body,
            )
        except ClientDisconnect:
            return self._response(_error(400, "the client went away"))
        except Exception as error:  # noqa: BLE001 - mapped to a status, or raised again
            answer = _translate(error)
        return self._response(answer)

    @staticmethod
    def _response(answer: Answer) -> "Response":
        headers = {"X-Content-Type-Options": "nosniff", **answer.headers}
        if answer.status >= 400:
            headers["Connection"] = "close"  # an unread request body must not be parsed as the next request
        return Response(answer.body, answer.status, headers=headers)

    async def _body(self, request: Request, limit: int) -> bytes:
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        if size > limit or size < 0:
            raise ValueError("invalid payload size")
        return await request.body() if size else b""

    # ------------------------------------------------------------------ API (worker threads)

    def _api(self, method: str, raw: str, query_string: bytes, authorization, if_none_match, body: bytes) -> Answer:
        try:
            if method == "GET":
                answer = self._get(raw, parse_qs(query_string.decode("latin-1")), authorization)
                if answer.status == 200 and answer.headers.get("Content-Type") == "application/json":
                    return self._revalidated(answer, if_none_match)
                return answer
            segments = _segments(raw)
            if segments[1:3] != ["api", f"v{API}"]:
                raise HttpError(404, f"the API lives under {API_PREFIX}/")
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            if method == "POST":
                return self._post(segments[3:], payload)
            if method == "DELETE":
                return self._delete(segments[3:], parse_qs(query_string.decode("latin-1")), payload)
            if method == "PUT":
                return self._put_json(segments[3:], payload)
            return self._patch(segments, payload)
        except Exception as error:  # noqa: BLE001
            return _translate(error)

    @staticmethod
    def _revalidated(answer: Answer, if_none_match: str | None) -> Answer:
        """A content ETag on a JSON answer, and 304 when the client already has it."""
        etag = answer.headers.get("ETag") or f'"{hashlib.blake2b(answer.body, digest_size=16).hexdigest()}"'
        answer.headers["ETag"] = etag
        if answer.headers.get("Cache-Control") == "no-store":
            answer.headers["Cache-Control"] = "private, no-cache"
        if if_none_match and etag in [tag.strip() for tag in if_none_match.split(",")]:
            return Answer(304, b"", {key: value for key, value in answer.headers.items() if key != "Content-Type"})
        return answer

    def key(self, project: str) -> str:
        """The core's name of a served project (OWN for the repo's own one)."""
        if self.own is not None and project == self.own:
            return OWN
        return refs.valid_project(project)

    def view(self, project: str) -> LocalProject:
        """The served project `project` as its own repo."""
        return LocalProject(self.store, self.key(project))

    def _public(self, record: dict) -> dict | None:
        """A project record as clients see it: the own project under its served name; None for one not served."""
        if record["name"] != OWN:
            return record
        if self.own is None:
            return None
        return {**record, "name": self.own, "title": record.get("title") or self.own}

    def _listing(self, project: str) -> Answer:
        """The artifact listing, serialized once per change of the project (LocalRepo.summaries_stamped)."""
        stamp, summaries = self.view(project).summaries_stamped()
        cached = self._listings.get(project)
        if cached is None or cached[0] != stamp:
            body = json.dumps(summaries).encode()
            cached = (stamp, body, f'"{hashlib.blake2b(body, digest_size=16).hexdigest()}"')
            self._listings[project] = cached
        return Answer(
            200,
            cached[1],
            {"Content-Type": "application/json", "Cache-Control": "private, no-cache", "ETag": cached[2]},
        )

    def _manifest(self, version: dict, value) -> Answer:
        """A version's manifest (or part of it): a final version never changes, so it may be cached for good."""
        final = version["status"] in ("completed", "failed")
        return _json(200, value, "private, max-age=31536000, immutable" if final else "no-store")

    def _get(self, raw: str, query: dict, authorization) -> Answer:
        segments = _segments(raw)
        if segments[1:2] != ["api"]:
            raise HttpError(
                404, f"this server has no pages: its API lives under {API_PREFIX}/ and its files under /files/"
            )
        self.auth.require(authorization)
        if segments[1:3] != ["api", f"v{API}"]:
            raise HttpError(404, f"the API lives under {API_PREFIX}/")
        store = self.store
        match segments[3:]:
            case ["repo"]:
                return _json(200, {"id": store.id, "cairn": __version__, "api": API})
            case ["projects"]:
                listed = [self._public(item) for item in store.overviews()]
                return _json(200, [item for item in listed if item is not None])
        if segments[3:4] != ["projects"] or len(segments) < 5:
            raise HttpError(404, "no such API route")
        project = segments[4]
        view = self.view(project)
        match segments[5:]:
            case []:
                return _json(200, self._public(view.record()))
            case ["artifacts"]:
                view.record()
                return self._listing(project)
            case ["retention"]:
                return _json(200, {"project": project, "rules": view.retention()})
            case ["artifacts", name]:
                summary = view.summary(name)
                if not summary["versions"]:
                    raise NotFound(f"no artifact {name}")
                latest = summary["latest"]
                return _json(200, {**summary, "latest_version": view.get_version(name, latest) if latest else None})
            case ["artifacts", name, "versions"]:
                return _json(200, view.versions(name))
            case ["artifacts", name, "versions", selector]:
                version = view.get_version(name, refs.version_key(selector))
                return self._manifest(version, version)
            case ["artifacts", name, "versions", selector, "files"]:
                version = view.get_version(name, refs.version_key(selector))
                return self._manifest(version, {"files": version["files"]})
            case ["artifacts", name, "aliases"]:
                return _json(200, view.aliases(name))
            case ["resolve"]:
                name, version = view.resolve(refs.parse(query.get("ref", [""])[0]))
                return _json(200, {"name": name, "version": version})
            case ["find"]:
                name, version = view.find(refs.parse(query.get("ref", [""])[0]))
                return _json(200, {"name": name, "version": version})
            case ["ids"]:
                name = query.get("name", [None])[0]
                return _json(
                    200, {"versions": view.ids(query.get("prefix", [""])[0], refs.valid_name(name) if name else None)}
                )
            case ["lineage"]:  # any status for an id or name@v<n>: a deleted version keeps its edges
                return _json(200, view.lineage(refs.parse(query.get("ref", [""])[0]), marks=True))
        raise HttpError(404, "no such API route")

    def _post(self, route: list[str], payload: dict) -> Answer:
        store = self.store
        match route:
            case ["blobs", "missing"]:
                hashes = payload.get("sha256", [])
                if (
                    not isinstance(hashes, list)
                    or len(hashes) > 200_000
                    or not all(isinstance(h, str) and SHA256.fullmatch(h) for h in hashes)
                ):
                    raise ValueError("sha256 must be a list of hex digests")
                absent = set(store.missing_hashes(hashes))
                return _json(200, {"missing": [h for h in hashes if h in absent]})
            case ["gc"]:
                dry_run = payload.get("dry_run", True)
                if not isinstance(dry_run, bool):
                    raise ValueError("dry_run must be true or false")
                grace = parse_duration(payload["grace"]) if payload.get("grace") is not None else GRACE
                return _json(200, self.gc(dry_run, grace, by="owner"))
            case ["projects", project, "short-ids"]:
                ids = payload.get("ids")
                if not isinstance(ids, list) or len(ids) > 10_000 or not all(isinstance(item, str) for item in ids):
                    raise ValueError("ids must be a list of ids")
                return _json(200, {"ids": self.view(project).short_ids([valid_id(item) for item in ids])})
            case ["projects"]:
                name = payload.get("name")
                if not isinstance(name, str):
                    raise ValueError("a project is {name, title?, description?}")
                key = self.key(name)
                exists = any(item["name"] == key for item in store.projects())
                record = store.create_project(key, payload.get("title"), payload.get("description") or "")
                return _json(200 if exists else 201, self._public(record))
        if route[:1] != ["projects"] or len(route) < 2:
            raise HttpError(404, "no such API route")
        view = self.view(route[1])
        match route:
            case ["projects", project, "artifacts", name, "versions"]:
                source = payload.get("source")
                if source is not None and not isinstance(source, dict):
                    raise ValueError("source must be an object")
                label = payload.get("label")
                if label is not None and not isinstance(label, str):
                    raise ValueError("label must be text")
                number = view.start_version(name, label, payload.get("metadata"), source, id=self._id(payload))
                id = view.get_version(name, number)["id"]
                store.beat(id, owner=False)  # a client's running version: alive while it syncs (see reap_schedule)
                return _json(201, {"version": number, "id": id})
            case ["projects", project, "artifacts", name, "versions", selector, action]:
                number = refs.version_key(selector)  # a number or an id
                if action == "files":
                    entries = clean_entries(payload.get("entries"), removals=True)
                    missing = store.missing(entries)
                    if missing:
                        return _json(409, {"error": "upload these blobs first", "missing": missing})
                    view.add_files(name, number, entries)  # no entries: a keep-alive of a running version
                    store.beat(view.get_version(name, number)["id"], owner=False)
                    return _json(200, {"files": len(entries)})
                if action == "metadata":
                    view.set_metadata(name, number, payload.get("metadata"))
                    version = view.get_version(name, number)
                    store.beat(version["id"], owner=False)
                    return _json(200, {"version": version["version"], "id": version["id"]})
                if action == "complete":
                    view.complete_version(
                        name, number, self._inputs(payload.get("inputs")), digest=self._digest(payload)
                    )
                    version = view.get_version(name, number)
                    return _json(200, {"version": version["version"], "id": version["id"]})
                if action == "fail":
                    view.fail_version(name, number)
                    version = view.get_version(name, number)
                    return _json(200, {"version": version["version"], "id": version["id"]})
            case ["projects", project, "artifacts", name, "publish"]:
                entries = clean_entries(payload.get("entries"))
                missing = store.missing(entries)
                if missing:
                    return _json(409, {"error": "upload these blobs first", "missing": missing})
                aliases = payload.get("aliases") or []
                if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
                    raise ValueError("aliases must be a list of names")
                if payload.get("source") is not None and not isinstance(payload["source"], dict):
                    raise ValueError("source must be an object")
                before = view.last_number(name)
                number = view.publish(
                    name,
                    entries,
                    label=payload.get("label"),
                    metadata=payload.get("metadata"),
                    inputs=self._inputs(payload.get("inputs")),
                    aliases=aliases,
                    source=payload.get("source"),
                    id=self._id(payload),
                    digest=self._digest(payload),
                )
                created = number > before  # new versions are numbered after every existing one
                return _json(
                    201 if created else 200,
                    {"version": number, "id": view.get_version(name, number)["id"], "created": created},
                )
            case ["projects", project, "artifacts", name, "aliases"]:
                alias, number = payload.get("alias"), payload.get("version")
                if not isinstance(alias, str) or isinstance(number, bool) or not isinstance(number, int):
                    raise ValueError("an alias is {alias: text, version: number}")
                view.set_alias(name, alias, number)
                return _json(200, {"alias": alias, "version": number})
        raise HttpError(404, "no such API route")

    def _patch(self, segments: list[str], payload: dict) -> Answer:
        if segments[1:4] == ["api", f"v{API}", "projects"] and len(segments) == 5:
            project = segments[4]
            for key in ("title", "description"):
                if payload.get(key) is not None and not isinstance(payload[key], str):
                    raise ValueError(f"{key} must be text")
            return _json(200, self._public(self.view(project).update(payload.get("title"), payload.get("description"))))
        raise HttpError(404, "no such API route")

    def _delete(self, route: list[str], query: dict, payload: dict) -> Answer:
        """Deleting versions, artifacts, and projects: a write token of the project; forcing needs the owner token."""
        force = query.get("force", [""])[0].lower() in ("1", "true", "yes") or payload.get("force") is True
        reason = query.get("reason", [None])[0] or payload.get("reason")
        if route[:1] != ["projects"] or len(route) < 2:
            raise HttpError(404, "no such API route")
        view = self.view(route[1])
        match route:
            case ["projects", project, "artifacts", name, "versions", selector]:
                return _json(
                    200,
                    view.delete_version(name, refs.version_key(selector), reason=reason, force=force, by="owner"),
                )
            case ["projects", project, "artifacts", name]:
                return _json(200, view.delete_artifact(name, reason=reason, force=force, by="owner"))
            case ["projects", project]:
                return _json(
                    200,
                    {
                        **self.store.delete_project(view.project, reason=reason, force=force, by="owner"),
                        "project": project,
                    },
                )
        raise HttpError(404, "no such API route")

    def _put_json(self, route: list[str], payload: dict) -> Answer:
        match route:
            case ["projects", project, "retention"]:
                return _json(200, {"project": project, "rules": self.view(project).set_retention(payload.get("rules"))})
        raise HttpError(404, "no such API route")

    def gc(self, dry_run: bool = True, grace=GRACE, by: str = "owner") -> dict:
        """Retention rules and the blob sweep (LocalRepo.gc), here: this server is the root's single writer."""
        return self.store.gc(dry_run=dry_run, grace=grace, by=by)

    @staticmethod
    def _digest(payload: dict) -> str | None:
        """The manifest digest a client supplies (digest.py); the store checks it against the files (409 if it differs)."""
        digest = payload.get("digest")
        if digest is not None and not (isinstance(digest, str) and DIGEST.fullmatch(digest)):
            raise ValueError("digest must look like sha256:<64 hex digits>")
        return digest

    @staticmethod
    def _id(payload: dict) -> str | None:
        return None if payload.get("id") is None else valid_id(payload["id"])

    @staticmethod
    def _inputs(items) -> list[str]:
        """Input references: versions of the project the version is sent to (a reference names no project)."""
        if items is None:
            return []
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValueError("inputs must be a list of references")
        return [str(refs.parse(item)) for item in items]

    # ------------------------------------------------------------------ uploads (event loop + worker threads)

    async def _put(self, request: Request, raw: str) -> Answer:
        """`PUT /api/v3/blobs/<sha256>`: the body streams into a BlobWriter in large pieces, written and hashed in a
        worker thread while the next piece arrives; fsynced, verified, and renamed into place at the end."""
        try:
            match = _segments(raw)
            self.auth.require(request.headers.get("authorization"))
            if match[1:4] != ["api", f"v{API}", "blobs"] or len(match) != 5 or not SHA256.fullmatch(match[4]):
                raise HttpError(404, "no such API route")
            try:
                size = int(request.headers.get("content-length", ""))
            except ValueError:
                raise HttpError(411, "Content-Length required")
            if size < 0 or size > self.max_blob:
                raise HttpError(413, f"blob larger than {self.max_blob} bytes")
            sha = match[4]
            loop = asyncio.get_running_loop()
            if size <= UPLOAD_PIECE:  # small: the whole body, then one trip to a worker thread
                body = await request.body()
                created = await loop.run_in_executor(self.executor, self.store.put_blob, sha, io.BytesIO(body), size)
                return _json(201 if created else 200, {"sha256": sha, "created": created})
            writer = await loop.run_in_executor(self.executor, self.store.blob_writer, sha, size)
        except ClientDisconnect:
            return _error(400, "upload ended before Content-Length bytes arrived")
        except Exception as error:  # noqa: BLE001
            return _translate(error)
        pending = None
        try:
            pieces, held = [], 0
            async for chunk in request.stream():
                if not chunk:
                    continue
                pieces.append(chunk)
                held += len(chunk)
                if held >= UPLOAD_PIECE:
                    if pending is not None:
                        _raise_first(await pending)
                    piece = b"".join(pieces)  # hashed and written in two threads at once, while the next piece arrives
                    pending = asyncio.gather(
                        loop.run_in_executor(self.executor, writer.hash_part, piece),
                        loop.run_in_executor(self.executor, writer.write_part, piece),
                        return_exceptions=True,
                    )  # both finish before anything else touches the writer
                    pieces, held = [], 0
            if pending is not None:
                outcome, pending = await pending, None
                _raise_first(outcome)
            if pieces:
                await loop.run_in_executor(self.executor, writer.write, b"".join(pieces))
            created = await loop.run_in_executor(self.executor, writer.finish)
        except ClientDisconnect:
            return _error(400, "upload ended before Content-Length bytes arrived")
        except Exception as error:  # noqa: BLE001
            return _translate(error)
        finally:
            if pending is not None:
                try:
                    await pending
                except Exception:  # noqa: BLE001 - the upload already failed
                    pass
            await loop.run_in_executor(self.executor, writer.abort)
        return _json(201 if created else 200, {"sha256": sha, "created": created})


def gc_schedule(app: StoreApp, interval: float, grace: float = GRACE, say=None) -> threading.Event:
    """Run `app.gc` (not a dry run) every `interval` seconds in a daemon thread; set the returned event to stop it."""
    stop = threading.Event()
    say = say or (lambda line: print(line, file=sys.stderr, flush=True))

    def loop():
        while not stop.wait(interval):
            try:
                found = app.gc(dry_run=False, grace=grace, by="gc schedule")
            except Exception as error:  # noqa: BLE001 - keep serving; try again next time
                say(f"gc: failed: {error}")
                continue
            say(
                f"gc: deleted {len(found['deleted'])} versions ({len(found['skipped'])} protected, kept), removed "
                f"{found['blobs']} blobs ({describe_bytes(found['bytes'])}) and {found['temporary']} stale uploads"
            )

    threading.Thread(target=loop, name="cairn-gc", daemon=True).start()
    return stop


REAP_EVERY = 60.0  # seconds between looks for running versions whose clients stopped syncing them
RUNNING_TIMEOUT = 600.0  # seconds without a sync before the server fails a client's running version


def reap_schedule(
    app: StoreApp, every: float = REAP_EVERY, timeout: float = RUNNING_TIMEOUT, say=None
) -> threading.Event:
    """Every `every` seconds, fail the running versions no client synced for `timeout` seconds (their job died where
    nothing could tell: a node that went away). Set the returned event to stop it."""
    stop = threading.Event()
    say = say or (lambda line: print(line, file=sys.stderr, flush=True))

    def loop():
        while not stop.wait(every):
            try:
                for ref, why in app.store.reap(timeout):
                    say(f"failed {ref}: {why}")
            except Exception as error:  # noqa: BLE001 - keep serving; try again next time
                say(f"reap: failed: {error}")

    threading.Thread(target=loop, name="cairn-reap", daemon=True).start()
    return stop


def default_log(line: str, client) -> None:
    """One line per request on stderr, without the token."""
    address = client[0] if client else "-"
    sys.stderr.write(f"{time.strftime('%H:%M:%S')}  {address}  {line}\n")


PORT_TRIES = 20  # a taken port moves the server to the next free one of these (the startup block shows it)


class StoreServer:
    """A bound server, not yet serving: `serve_forever()` runs uvicorn on its socket until `shutdown()`
    (from another thread) or a signal. `server_address` is known as soon as it is made (port 0: any free port).
    `log` is called with one line per request (None: no log); `app` is the ASGI app."""

    def __init__(self, app: StoreApp, host: str, port: int, owner_token: str):
        self.app, self.owner_token, self.store = app, owner_token, app.store
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.moved_from = None  # the port asked for, when it was taken and the next free one was used
        for candidate in [port] if port == 0 else range(port, min(port + PORT_TRIES, 65536)):
            self.socket = socket.socket(family, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.socket.bind((host, candidate))
                self.socket.listen(2048)
                break
            except OSError as error:
                self.socket.close()
                if error.errno != errno.EADDRINUSE:
                    raise
        else:
            raise SystemExit(
                f"cairn: ports {port}-{port + PORT_TRIES - 1} on {host} all in use; choose one with --port"
            )
        self.server_address = self.socket.getsockname()[:2]
        if port and self.server_address[1] != port:
            self.moved_from = port
        try:
            import uvloop  # noqa: F401

            loop = "uvloop"
        except ImportError:
            loop = "asyncio"
        try:
            import httptools  # noqa: F401

            protocol = "httptools"
        except ImportError:
            protocol = "h11"
        config = uvicorn.Config(
            app,
            loop=loop,
            http=protocol,
            lifespan="off",
            log_config=None,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
            server_header=False,
            headers=[("Server", f"cairn-store/{__version__}")],
            timeout_keep_alive=30,
            timeout_graceful_shutdown=5,
        )
        self.uvicorn = uvicorn.Server(config)
        self._done = threading.Event()
        self._started = False
        self.lock = None  # the root's ServerLock, released by server_close

    @property
    def log(self):
        return self.app.log

    @log.setter
    def log(self, value) -> None:
        """A callable taking (line[, client]) per request, or None."""
        if value is None or value is default_log:
            self.app.log = value
            return
        try:
            import inspect

            two = len(inspect.signature(value).parameters) >= 2
        except (TypeError, ValueError):
            two = False
        self.app.log = value if two else (lambda line, client: value(line))

    def serve_forever(self) -> None:
        self._started = True
        try:
            self.uvicorn.run(sockets=[self.socket])
        finally:
            self._done.set()

    def shutdown(self) -> None:
        self.uvicorn.should_exit = True
        if self._started:
            self._done.wait(15)

    def server_close(self) -> None:
        self.socket.close()
        self.app.executor.shutdown(wait=False, cancel_futures=True)
        if self.lock is not None:
            self.lock.release()


def make_server(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8800,
    max_blob: int = 50 * 1024**3,
    no_auth: bool = False,
    allow_network_fs: bool = False,
    own: str | None = None,
) -> StoreServer:
    """A bound server (not yet serving) for a repo that has its owner token. With no_auth every request acts as the
    owner, so it is refused on any address but this machine's loopback. A repo on a network filesystem is refused
    (by LocalRepo) unless `allow_network_fs` (this server is then its only writer; see store/fs.py). `own`: the name
    the repo's own project is served under (a project's repo; None for a server's repo, which has none)."""
    _require_extra()
    if no_auth and host not in LOCAL_HOSTS:
        raise SystemExit("cairn: --no-auth needs a loopback --host (it serves every project to anyone)")
    root = root.resolve()
    if not (root / "token").is_file():
        raise SystemExit(
            f"cairn: {short_path(root)} is not a repo with a token (`cairn server {short_path(root)}` creates it)"
        )
    lock = ServerLock(root)
    lock.acquire()  # one server per root, and other processes leave its metadata alone (store/db.py)
    try:
        try:
            store = LocalRepo(root, allow_network_fs=allow_network_fs, where="")
        except StoreError as error:
            raise SystemExit(f"cairn: {error}") from None
        app = StoreApp(
            store,
            Auth(read_token(root / "token"), no_auth),
            max_blob,
            own=refs.valid_project(own) if own is not None else None,
        )
        server = StoreServer(app, host, port, app.auth.owner_token)
    except BaseException:
        lock.release()
        raise
    server.lock = lock
    return server


READY = "Serving; Ctrl-C stops it."
PLAIN_HTTP = "plain HTTP: use a VPN (Tailscale), an SSH tunnel, or an HTTPS proxy"


def serve_store(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8800,
    max_blob: int = 50 * 1024**3,
    no_auth: bool = False,
    ui_port: int | None = None,
    allow_network_fs: bool = False,
    gc_interval: float | None = None,
    gc_grace: float = GRACE,
    own: str | None = None,
) -> None:
    """Serve a store; a new root (no token yet) is initialized first. Prints the owner token, which clients
    and `cairn ui` log in with, to the terminal only: never into a URL. With `ui_port` (0: any free
    port), `cairn ui` runs as a child process beside the store and stops with it. With `gc_interval` (seconds),
    garbage collection (retention rules, then blobs older than `gc_grace`) runs that often. `own`: the name the repo's own
    project is served under (`cairn server` in a project: `<server>/<project folder name>`)."""
    _require_extra()
    try:
        LocalRepo(root, allow_network_fs=allow_network_fs)  # refused on a network filesystem before anything is written
    except StoreError as error:
        raise SystemExit(f"cairn: {error}") from None
    created = None if (root / "token").exists() else init_root(root)
    server = make_server(
        root, host, port, max_blob=max_blob, no_auth=no_auth, allow_network_fs=allow_network_fs, own=own
    )
    actual = server.server_address[1]
    here = f"http://{url_host(host)}:{actual}/"  # this machine's name for 0.0.0.0, which nobody can connect to
    if server.moved_from:
        print(f"port {server.moved_from} in use; using {actual}", flush=True)
    child, lines = None, []
    if ui_port is not None:
        child, lines = start_ui(host, actual, ui_port, server.owner_token, no_auth)
        if child is None:
            warn("the UI did not start (see above); the server keeps running")
            lines = []
    notices = [line for line in lines if not line.startswith("  ")]  # the UI's own notices (a moved port) go first
    for line in notices:
        print(line, flush=True)
    token_file = short_path(root / "token")
    rows = [("API", here)]
    block_lines = [line for line in lines if line.startswith("  ")]  # its address and login link, aligned like these
    if no_auth:
        rows.append(("Auth", "off (--no-auth): every request acts as the owner"))
    else:
        rows.append(("Token", f"{server.owner_token}   ({token_file})"))
    print(
        field("Cairn server", short_path(root) + ("   (created, with a new owner token)" if created else "")),
        flush=True,
    )
    print(field("  API", here), flush=True)
    for line in block_lines:
        print(line, flush=True)
    for key, value in rows[1:]:
        print(field(f"  {key}", value), flush=True)
    if child is not None:
        threading.Thread(target=forward, args=(child,), daemon=True).start()
    project = own if own is not None else "<project>"
    print(f"Clients:  cairn remote set {here}{project}", flush=True)
    if not no_auth:
        print(f"          token: copy {token_file} to their .cairn/token, or set CAIRN_TOKEN", flush=True)
    if host not in LOCAL_HOSTS:
        warn(PLAIN_HTTP)
    reap_schedule(server.app)
    if gc_interval:
        gc_schedule(server.app, gc_interval, gc_grace)
        print(f"gc every {gc_interval / 3600:g} h (grace {gc_grace / 3600:g} h)", flush=True)
    print(READY, flush=True)  # always the startup block's last line
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
        server.server_close()
        if child is not None:
            stop(child)
