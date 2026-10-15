"""The store server: a LocalStore root shared over HTTP, behind tokens. Only an artifact store: a JSON API and
file downloads, no pages (`cairn ui` explores a store in a browser, locally or against this server).

    cairn server <root> [--host 127.0.0.1] [--port 8800] [--max-blob-gb 50] [--no-auth] [--gc-interval 24h]

A new root gets a fresh owner token on first launch; every launch prints `Store: <url>` and
`Token: <owner token>` to the terminal (never into a URL). Scoped tokens (one project, read or
write) are made with `cairn token create` (see tokens.py). Bind localhost, and put an
HTTPS reverse proxy in front of anything else: the server speaks plain HTTP.

Artifact names are slash paths (`datasets/crops`), so every route says where the name ends:

    API (JSON, `Authorization: Bearer <token>`; a name is ONE percent-encoded path segment, `datasets%2Fcrops`)
      GET  /api/v2/store                                         {uid, version}: the store's uid and the server's version
      GET  /api/v2/projects                                      [{..., artifacts, updated}]   POST {name, title?, description?}
      GET  /api/v2/projects/<p>                                  PATCH {title?, description?}
      GET  /api/v2/projects/<p>/artifacts                        [{name, latest, versions, aliases, updated, metadata}]
      GET  /api/v2/projects/<p>/artifacts/<name>                 that summary plus `latest_version` (its manifest, or null)
      GET  /api/v2/projects/<p>/artifacts/<name>/versions        every version, full manifests
      GET  /api/v2/projects/<p>/artifacts/<name>/versions/<n>    one manifest (<n>: a number or a uid)   .../files  {files: [entries]}
      GET  /api/v2/projects/<p>/artifacts/<name>/aliases         {alias: version}
      POST /api/v2/projects/<p>/artifacts/<name>/versions        {uid?, label?, metadata?, source?} -> {version, uid} (running)
      POST .../versions/<n>/files {entries}   (409 + `missing` when blobs are absent)   .../complete {inputs?, digest?}   .../fail
      POST .../versions/<n>/metadata {metadata}                  replace the metadata of a running version
      POST /api/v2/projects/<p>/artifacts/<name>/publish         {uid?, entries, label?, metadata?, inputs?, aliases?, source?, digest?}
                                                                 -> {version, uid, created}  (created=false: nothing changed,
                                                                 or this uid was published already)
      POST /api/v2/projects/<p>/artifacts/<name>/aliases         {alias, version}
      GET  /api/v2/resolve?ref=<ref>                             {project, name, version: manifest}
      GET  /api/v2/lineage?ref=<ref>                             {ref, inputs, consumers, deleted, nodes: {ref: {status, kind}}}
      POST /api/v2/blobs/missing {sha256: [...]}                 PUT /api/v2/blobs/<sha256> (streamed, hash-verified)
    Deleting (a write token of the project; `force=1` and gc need the owner token; 409 + `protected` when refused)
      DELETE /api/v2/projects/<p>/artifacts/<name>/versions/<n>  [?force=1&reason=...] -> {deleted: [refs], skipped: []}
      DELETE /api/v2/projects/<p>/artifacts/<name>               every version of it, under the same rules
      DELETE /api/v2/projects/<p>                                only when no version is left (or force)
      GET/PUT /api/v2/projects/<p>/retention                     {rules: [{prefix, keep_last, keep_days, keep_aliased}]}
      POST /api/v2/gc {dry_run (default true), grace (seconds or "24h")}   retention rules, then the blob sweep
    Files (the name ends at `@`; the path inside the artifact follows `//`)
      GET  /files/<p>/<name>[@v<n>|@uid|@alias|@label]//<path>   ETag, Range, immutable for `@v<n>` or `@uid` of a
                                                                 completed version, `?download=1` for an attachment

References in the API are `project:name[@selector]` (the project is required: the server has no default).
A client may supply a version's `uid` (UUIDv7) when it creates it: sending the same version again is
answered with the version it made (a client's local replica can retry safely), and a known uid with different content
is refused with 409.

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

from .._version import __version__
from ..store import BadRange, Conflict, Deleted, LocalStore, MissingBlobs, NotFound, Protected, Ref, StoreError, refs
from ..store.gc import GRACE, describe_bytes, parse_duration
from ..store.blobs import SHA256, clean_entries
from ..store.db import ServerLock
from ..store.digest import PREFIX as DIGEST_PREFIX
from ..store.fs import network_filesystem
from ..store.tokens import OWNER, Principal, Tokens, init_root, read_token
from ..store.uids import UID, valid_uid
from .auth import Auth, HttpError, allow
from .files import serve_file
from .spawn import LOCAL_HOSTS, forward, start_ui, stop

try:  # the [server] extra
    import uvicorn
    from starlette.requests import ClientDisconnect, Request
    from starlette.responses import Response
except ImportError as error:  # pragma: no cover - checked by make_server
    MISSING: ImportError | None = error
else:
    MISSING = None

MAX_JSON = 64 * 1024 * 1024
UPLOAD_PIECE = 4 << 20  # uploads are hashed and written in pieces this large, in worker threads
WORKERS = int(os.environ.get("CAIRN_SERVER_THREADS", "32"))  # worker threads for store work and file I/O
DIGEST = re.compile(re.escape(DIGEST_PREFIX) + r"[0-9a-f]{64}")
METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE")


def _require_extra() -> None:
    if MISSING is not None:
        raise SystemExit(f"`cairn server` needs the server extra (Starlette and uvicorn): pip install \"cairn[server]\" ({MISSING})")


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

    def __init__(self, store: LocalStore, auth: Auth, max_blob: int, workers: int = WORKERS):
        self.store, self.auth, self.max_blob = store, auth, max_blob
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
                query = scope.get("query_string") or b""
                target = path.decode("latin-1") + ("?" + query.decode("latin-1") if query else "")
                self.log(f'"{scope["method"]} {target} HTTP/{scope.get("http_version", "1.1")}" {status[0] or "-"} -',
                         scope.get("client"))

    async def handle(self, request: Request):
        raw = (request.scope.get("raw_path") or request.scope["path"].encode()).decode("latin-1")
        method = request.method
        try:
            if method == "GET" and raw.startswith("/files/"):
                principal = self.auth.require(request.headers.get("authorization"))
                query = parse_qs(request.scope.get("query_string", b"").decode("latin-1"))
                return await serve_file(self, request, principal, raw, query)
            if method == "PUT" and raw.startswith("/api/v2/blobs/"):
                return self._response(await self._put(request, raw))
            if method not in METHODS:
                return self._response(_error(501, f"unsupported method {method}"))
            body, principal = b"", None
            if method != "GET":
                principal = self.auth.require(request.headers.get("authorization"))
                body = await self._body(request, MAX_JSON)
            loop = asyncio.get_running_loop()
            answer = await loop.run_in_executor(self.executor, self._api, method, raw, request.scope.get("query_string", b""),
                                                request.headers.get("authorization"), request.headers.get("if-none-match"), body,
                                                principal)
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

    def _api(self, method: str, raw: str, query_string: bytes, authorization, if_none_match, body: bytes,
             principal: Principal | None) -> Answer:
        try:
            if method == "GET":
                answer = self._get(raw, parse_qs(query_string.decode("latin-1")), authorization)
                if answer.status == 200 and answer.headers.get("Content-Type") == "application/json":
                    return self._revalidated(answer, if_none_match)
                return answer
            segments = _segments(raw)
            if segments[1:3] != ["api", "v2"]:
                raise HttpError(404, "the API lives under /api/v2/")
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            if method == "POST":
                return self._post(principal, segments[3:], payload)
            if method == "DELETE":
                return self._delete(principal, segments[3:], parse_qs(query_string.decode("latin-1")), payload)
            if method == "PUT":
                return self._put_json(principal, segments[3:], payload)
            return self._patch(principal, segments, payload)
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

    def _ref(self, query: dict, principal: Principal) -> Ref:
        ref = refs.parse(query.get("ref", [""])[0])
        if ref.project is None:
            raise ValueError("the reference needs a project: project:name[@selector]")
        allow(principal, ref.project)
        return ref

    def _listing(self, project: str) -> Answer:
        """The artifact listing, serialized once per change of the project (LocalStore.summaries_stamped)."""
        stamp, summaries = self.store.summaries_stamped(project)
        cached = self._listings.get(project)
        if cached is None or cached[0] != stamp:
            body = json.dumps(summaries).encode()
            cached = (stamp, body, f'"{hashlib.blake2b(body, digest_size=16).hexdigest()}"')
            self._listings[project] = cached
        return Answer(200, cached[1], {"Content-Type": "application/json", "Cache-Control": "private, no-cache", "ETag": cached[2]})

    def _manifest(self, version: dict, value) -> Answer:
        """A version's manifest (or part of it): a final version never changes, so it may be cached for good."""
        final = version["status"] in ("completed", "failed")
        return _json(200, value, "private, max-age=31536000, immutable" if final else "no-store")

    def _get(self, raw: str, query: dict, authorization) -> Answer:
        segments = _segments(raw)
        if segments[1:2] != ["api"]:
            raise HttpError(404, "this server has no pages: its API lives under /api/v2/ and its files under /files/")
        principal = self.auth.require(authorization)
        if segments[1:3] != ["api", "v2"]:
            raise HttpError(404, "the API lives under /api/v2/")
        store = self.store
        match segments[3:]:
            case ["store"]:
                return _json(200, {"uid": store.uid, "version": __version__})
            case ["projects"]:
                return _json(200, [item for item in store.overviews() if principal.can(item["name"])])
            case ["projects", project]:
                allow(principal, project)
                return _json(200, store.get_project(project))
            case ["projects", project, "artifacts"]:
                allow(principal, project)
                store.get_project(project)
                return self._listing(project)
            case ["projects", project, "retention"]:
                allow(principal, project)
                return _json(200, {"project": project, "rules": store.retention(project)})
            case ["projects", project, "artifacts", name]:
                allow(principal, project)
                summary = store.summary(project, name)
                if not summary["versions"]:
                    raise NotFound(f"no artifact {project}:{name}")
                latest = summary["latest"]
                return _json(200, {**summary, "latest_version": store.get_version(project, name, latest) if latest else None})
            case ["projects", project, "artifacts", name, "versions"]:
                allow(principal, project)
                return _json(200, store.versions(project, name))
            case ["projects", project, "artifacts", name, "versions", number] if number.isdigit() or UID.fullmatch(number):
                allow(principal, project)
                version = store.get_version(project, name, int(number) if number.isdigit() else number)
                return self._manifest(version, version)
            case ["projects", project, "artifacts", name, "versions", number, "files"] if number.isdigit() or UID.fullmatch(number):
                allow(principal, project)
                version = store.get_version(project, name, int(number) if number.isdigit() else number)
                return self._manifest(version, {"files": version["files"]})
            case ["projects", project, "artifacts", name, "aliases"]:
                allow(principal, project)
                return _json(200, store.aliases(project, name))
            case ["resolve"]:
                ref = self._ref(query, principal)
                project, name, version = store.resolve(ref)
                return _json(200, {"project": project, "name": name, "version": version})
            case ["lineage"]:
                ref = self._ref(query, principal)
                if ref.exact:  # any status: a deleted version keeps its edges, a running run already has consumers
                    project, name, version = ref.project, ref.name, store.get_version(ref.project, ref.name, ref.uid or ref.version)
                else:
                    project, name, version = store.resolve(ref)
                found = store.lineage(Ref(project, name, f"v{version['version']}"), marks=True)
                nodes = found.pop("nodes", {})
                visible = {key: [item for item in items if principal.can(refs.parse(item).project)] for key, items in found.items()}
                nodes = {item: mark for item, mark in nodes.items() if principal.can(refs.parse(item).project)}
                return _json(200, {"ref": refs.exact(project, name, version["version"]), **visible, "nodes": nodes})
        raise HttpError(404, "no such API route")

    def _post(self, principal: Principal, route: list[str], payload: dict) -> Answer:
        store = self.store
        match route:
            case ["blobs", "missing"]:
                if not principal.write:
                    raise HttpError(403, "this token cannot write")
                hashes = payload.get("sha256", [])
                if not isinstance(hashes, list) or len(hashes) > 200_000 or not all(isinstance(h, str) and SHA256.fullmatch(h) for h in hashes):
                    raise ValueError("sha256 must be a list of hex digests")
                absent = set(store.missing_hashes(hashes))
                return _json(200, {"missing": [h for h in hashes if h in absent]})
            case ["gc"]:
                if principal != OWNER:
                    raise HttpError(403, "only the owner token may collect garbage")
                dry_run = payload.get("dry_run", True)
                if not isinstance(dry_run, bool):
                    raise ValueError("dry_run must be true or false")
                grace = parse_duration(payload["grace"]) if payload.get("grace") is not None else GRACE
                return _json(200, self.gc(dry_run, grace, by=principal.name))
            case ["projects"]:
                name = payload.get("name")
                exists = isinstance(name, str) and any(item["name"] == name for item in store.projects())
                if principal.project is not None and not (exists and principal.project == name):
                    raise HttpError(403, "this token cannot create projects")
                record = store.create_project(name, payload.get("title"), payload.get("description") or "")
                return _json(200 if exists else 201, record)
            case ["projects", project, "artifacts", name, "versions"]:
                allow(principal, project, write=True)
                source = payload.get("source")
                if source is not None and not isinstance(source, dict):
                    raise ValueError("source must be an object")
                label = payload.get("label")
                if label is not None and not isinstance(label, str):
                    raise ValueError("label must be text")
                number = store.start_version(project, name, label, payload.get("metadata"), source, uid=self._uid(payload))
                return _json(201, {"version": number, "uid": store.get_version(project, name, number)["uid"]})
            case ["projects", project, "artifacts", name, "versions", number, action] if number.isdigit():
                allow(principal, project, write=True)
                number = int(number)
                if action == "files":
                    entries = clean_entries(payload.get("entries"))
                    missing = store.missing(entries)
                    if missing:
                        return _json(409, {"error": "upload these blobs first", "missing": missing})
                    store.add_files(project, name, number, entries)
                    return _json(200, {"files": len(entries)})
                if action == "metadata":
                    store.set_metadata(project, name, number, payload.get("metadata"))
                    return _json(200, {"version": number})
                if action == "complete":
                    store.complete_version(project, name, number, self._inputs(principal, project, payload.get("inputs")),
                                           digest=self._digest(payload))
                    return _json(200, {"version": number})
                if action == "fail":
                    store.fail_version(project, name, number)
                    return _json(200, {"version": number})
            case ["projects", project, "artifacts", name, "publish"]:
                allow(principal, project, write=True)
                entries = clean_entries(payload.get("entries"))
                missing = store.missing(entries)
                if missing:
                    return _json(409, {"error": "upload these blobs first", "missing": missing})
                aliases = payload.get("aliases") or []
                if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
                    raise ValueError("aliases must be a list of names")
                if payload.get("source") is not None and not isinstance(payload["source"], dict):
                    raise ValueError("source must be an object")
                before = store.last_number(project, name)
                number = store.publish(project, name, entries, label=payload.get("label"), metadata=payload.get("metadata"),
                                       inputs=self._inputs(principal, project, payload.get("inputs")), aliases=aliases,
                                       source=payload.get("source"), uid=self._uid(payload), digest=self._digest(payload))
                created = number > before  # new versions are numbered after every existing one
                return _json(201 if created else 200, {"version": number, "uid": store.get_version(project, name, number)["uid"],
                                                       "created": created})
            case ["projects", project, "artifacts", name, "aliases"]:
                allow(principal, project, write=True)
                alias, number = payload.get("alias"), payload.get("version")
                if not isinstance(alias, str) or isinstance(number, bool) or not isinstance(number, int):
                    raise ValueError("an alias is {alias: text, version: number}")
                store.set_alias(project, name, alias, number)
                return _json(200, {"alias": alias, "version": number})
        raise HttpError(404, "no such API route")

    def _patch(self, principal: Principal, segments: list[str], payload: dict) -> Answer:
        if segments[1:4] == ["api", "v2", "projects"] and len(segments) == 5:
            project = segments[4]
            allow(principal, project, write=True)
            for key in ("title", "description"):
                if payload.get(key) is not None and not isinstance(payload[key], str):
                    raise ValueError(f"{key} must be text")
            return _json(200, self.store.update_project(project, payload.get("title"), payload.get("description")))
        raise HttpError(404, "no such API route")

    def _delete(self, principal: Principal, route: list[str], query: dict, payload: dict) -> Answer:
        """Deleting versions, artifacts, and projects: a write token of the project; forcing needs the owner token."""
        force = query.get("force", [""])[0].lower() in ("1", "true", "yes") or payload.get("force") is True
        reason = query.get("reason", [None])[0] or payload.get("reason")
        store = self.store
        match route:
            case ["projects", project, "artifacts", name, "versions", number] if number.isdigit() or UID.fullmatch(number):
                allow(principal, project, write=True)
                _owner_for_force(principal, force)
                return _json(200, store.delete_version(project, name, int(number) if number.isdigit() else number, reason=reason,
                                                       force=force, by=principal.name))
            case ["projects", project, "artifacts", name]:
                allow(principal, project, write=True)
                _owner_for_force(principal, force)
                return _json(200, store.delete_artifact(project, name, reason=reason, force=force, by=principal.name))
            case ["projects", project]:
                allow(principal, project, write=True)
                _owner_for_force(principal, force)
                return _json(200, store.delete_project(project, reason=reason, force=force, by=principal.name))
        raise HttpError(404, "no such API route")

    def _put_json(self, principal: Principal, route: list[str], payload: dict) -> Answer:
        match route:
            case ["projects", project, "retention"]:
                allow(principal, project, write=True)
                return _json(200, {"project": project, "rules": self.store.set_retention(project, payload.get("rules"))})
        raise HttpError(404, "no such API route")

    def gc(self, dry_run: bool = True, grace=GRACE, by: str = "owner") -> dict:
        """Retention rules and the blob sweep (LocalStore.gc), here: this server is the root's single writer."""
        return self.store.gc(dry_run=dry_run, grace=grace, by=by)

    @staticmethod
    def _digest(payload: dict) -> str | None:
        """The manifest digest a client supplies (digest.py); the store checks it against the files (409 if it differs)."""
        digest = payload.get("digest")
        if digest is not None and not (isinstance(digest, str) and DIGEST.fullmatch(digest)):
            raise ValueError("digest must look like sha256:<64 hex digits>")
        return digest

    @staticmethod
    def _uid(payload: dict) -> str | None:
        return None if payload.get("uid") is None else valid_uid(payload["uid"])

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
            allow(principal, ref.project)
            qualified.append(str(ref))
        return qualified

    # ------------------------------------------------------------------ uploads (event loop + worker threads)

    async def _put(self, request: Request, raw: str) -> Answer:
        """`PUT /api/v2/blobs/<sha256>`: the body streams into a BlobWriter in large pieces, written and hashed in a
        worker thread while the next piece arrives; fsynced, verified, and renamed into place at the end."""
        try:
            match = _segments(raw)
            principal = self.auth.require(request.headers.get("authorization"))
            if match[1:4] != ["api", "v2", "blobs"] or len(match) != 5 or not SHA256.fullmatch(match[4]):
                raise HttpError(404, "no such API route")
            if not principal.write:
                raise HttpError(403, "this token cannot write")
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
                    pending = asyncio.gather(loop.run_in_executor(self.executor, writer.hash_part, piece),
                                             loop.run_in_executor(self.executor, writer.write_part, piece),
                                             return_exceptions=True)  # both finish before anything else touches the writer
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


def _owner_for_force(principal: Principal, force: bool) -> None:
    if force and principal != OWNER:
        raise HttpError(403, "only the owner token may force a deletion")


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
            say(f"gc: deleted {len(found['deleted'])} versions ({len(found['skipped'])} protected, kept), removed "
                f"{found['blobs']} blobs ({describe_bytes(found['bytes'])}) and {found['temporary']} stale uploads")

    threading.Thread(target=loop, name="cairn-gc", daemon=True).start()
    return stop


def default_log(line: str, client) -> None:
    """One line per request on stderr, without the token."""
    address = client[0] if client else "-"
    sys.stderr.write(f"[{time.strftime('%d/%b/%Y %H:%M:%S')}] {address} {line}\n")


class StoreServer:
    """A bound store server, not yet serving: `serve_forever()` runs uvicorn on its socket until `shutdown()`
    (from another thread) or a signal. `server_address` is known as soon as it is made (port 0: any free port).
    `log` is called with one line per request (None: no log); `app` is the ASGI app."""

    def __init__(self, app: StoreApp, host: str, port: int, owner_token: str):
        self.app, self.owner_token, self.store = app, owner_token, app.store
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.socket = socket.socket(family, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.bind((host, port))
            self.socket.listen(2048)
        except OSError:
            self.socket.close()
            raise
        self.server_address = self.socket.getsockname()[:2]
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
        config = uvicorn.Config(app, loop=loop, http=protocol, lifespan="off", log_config=None, log_level="warning",
                                access_log=False, proxy_headers=False, server_header=False,
                                headers=[("Server", f"cairn-store/{__version__}")], timeout_keep_alive=30,
                                timeout_graceful_shutdown=5)
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


def _network_root(root: Path, allowed: bool) -> bool:
    """True if `root` lies on a network filesystem and that is allowed; SystemExit if it is not allowed."""
    fstype = network_filesystem(root)
    if fstype and not allowed:
        raise SystemExit(f"{root} is on a network filesystem ({fstype}): appends, locks, and file timestamps there are not "
                         "safe between hosts, so a store root must live on a local disk of the machine running "
                         "`cairn server`. If this server is the only process that ever writes the root, pass "
                         "--allow-network-fs.")
    return bool(fstype)


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8800, max_blob: int = 50 * 1024**3,
                no_auth: bool = False, allow_network_fs: bool = False) -> StoreServer:
    """A bound store server (not yet serving) for a root that has its owner token. With no_auth every
    request acts as the owner, so it is refused on any address but this machine's loopback. A root on a
    network filesystem is refused unless `allow_network_fs` (this server is then its only writer; see store/fs.py)."""
    _require_extra()
    if no_auth and host not in LOCAL_HOSTS:
        raise SystemExit("--no-auth serves every project to anyone who can connect: only with a loopback --host")
    root = root.resolve()
    if not (root / "token").is_file():
        raise SystemExit(f"{root} is not a store root; `cairn server {root}` creates it")
    if _network_root(root, allow_network_fs):
        print(f"warning: {root} is on a network filesystem ({network_filesystem(root)}); this server must be the only "
              "process writing it", file=sys.stderr, flush=True)
    lock = ServerLock(root)
    lock.acquire()  # one server per root, and other processes leave its metadata alone (store/db.py)
    try:
        store = LocalStore(root, network_warning=False, served=True)
        app = StoreApp(store, Auth(read_token(root / "token"), Tokens(root), no_auth), max_blob)
        server = StoreServer(app, host, port, app.auth.owner_token)
    except BaseException:
        lock.release()
        raise
    server.lock = lock
    return server


SERVED = "server.json"


def local_address(host: str) -> str:
    """The address a client on this machine reaches a server bound to `host` at."""
    where = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    return f"[{where}]" if ":" in where else where


def announce(root: Path, url: str, ui: str | None) -> None:
    """`<root>/server.json`: where this server listens, so a project whose local store it serves sends its writes
    here (client/open.py) instead of being refused. Valid only while the server holds `server.lock`."""
    path = root / SERVED
    temporary = path.with_name(f".{SERVED}.{os.getpid()}")
    temporary.write_text(json.dumps({"pid": os.getpid(), "url": url, "ui": ui}) + "\n")
    os.replace(temporary, path)


def withdraw(root: Path) -> None:
    try:
        if json.loads((root / SERVED).read_text()).get("pid") == os.getpid():
            (root / SERVED).unlink()
    except (OSError, ValueError, AttributeError):
        pass


def serve_store(root: Path, host: str = "127.0.0.1", port: int = 8800, max_blob: int = 50 * 1024**3,
                no_auth: bool = False, ui_port: int | None = None, allow_network_fs: bool = False, gc_interval: float | None = None,
                gc_grace: float = GRACE) -> None:
    """Serve a store; a new root (no token yet) is initialized first. Prints the owner token, which clients
    and `cairn ui` log in with, to the terminal only: never into a URL. With `ui_port` (0: any free
    port), `cairn ui` runs as a child process beside the store and stops with it. With `gc_interval` (seconds),
    garbage collection (retention rules, then blobs older than `gc_grace`) runs that often."""
    _require_extra()
    _network_root(root, allow_network_fs)  # refused before anything is created
    created = None if (root / "token").exists() else init_root(root)
    server = make_server(root, host, port, max_blob=max_blob, no_auth=no_auth, allow_network_fs=allow_network_fs)
    if host not in LOCAL_HOSTS:
        print("warning: the store serves plain HTTP; expose it only through an HTTPS reverse proxy or a tunnel", flush=True)
    if created:
        print(f"Created store {root.resolve()} with a new owner token ({created}, owner-only)", flush=True)
    actual = server.server_address[1]
    print(f"Store: http://{host}:{actual}/ (data in {root.resolve()})", flush=True)
    announce(root, f"http://{local_address(host)}:{actual}", None)  # before any client can see the login link
    child, ui_url = None, None
    if ui_port is not None:
        child, line = start_ui(host, actual, ui_port, server.owner_token, no_auth)
        found = re.search(r"https?://\S+", line or "")
        ui_url = found.group(0) if found else None
        if child is None:
            print("warning: the UI did not start (see above); the store keeps running", file=sys.stderr, flush=True)
        else:
            print(line, flush=True)
            threading.Thread(target=forward, args=(child,), daemon=True).start()
    if no_auth:
        print("Authentication is off (--no-auth): every request acts as the owner; for local testing only", flush=True)
    else:
        print(f"Token: {server.owner_token}", flush=True)
        print("  clients read it from CAIRN_STORE_TOKEN or a token_file, and `cairn ui` logs in with it;"
              " `cairn token create` makes project-scoped tokens", flush=True)
    if gc_interval:
        gc_schedule(server.app, gc_interval, gc_grace)
        print(f"Garbage collection every {gc_interval / 3600:g} h (grace {gc_grace / 3600:g} h)", flush=True)
    if ui_url:
        announce(root, f"http://{local_address(host)}:{actual}", ui_url)
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
        withdraw(root)
        if child is not None:
            stop(child)
