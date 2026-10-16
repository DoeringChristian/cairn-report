"""Remote: one project on a `cairn server`, `<server>/<project>`, as the repo interface over HTTP (a repo's remote);
Server: the server itself, with its projects (what `cairn ui --remote <server>` lists).

A Remote offers what the repo (client/repo.py), sync (client/sync.py), and the UI use: the project's record, blob
upload, versions, aliases, `resolve`, `ids`, `lineage`, and `blob_file`, which downloads a blob into a repo; it never
reaches another project. HTTP, the token, and the HTTPS rule are `http.Client`'s. Artifact names travel
percent-encoded in API paths (`datasets%2Fcrops`).

A remote that does not answer raises `Unreachable` (a StoreError), which the repo catches: versions wait in the
outbox, and reads fall back to the repo. `id` is the server's repo id, from the handshake (`GET /api/v3/repo`, which
also says the server's API version: a server of another Cairn release is refused with Incompatible, a 0.14 one found
by its `/api/v2/repo`). Errors say on which remote they happened, and a refused token says where it came from (never
the token). References travel as text and are parsed by the server (store/refs.py); versions in API paths are made by
code (`refs.version_segment`: `v<n>` or an id).
"""

from __future__ import annotations

import hashlib
import re
import sys
import threading
from pathlib import Path
from urllib.parse import quote

from .._version import API, API_PREFIX, __version__
from ..store import refs
from ..store.blobs import blob_path
from ..store.folders import place, scratch
from ..store.errors import (
    Ambiguous,
    BadRange,
    Conflict,
    Deleted,
    Forbidden,
    Incompatible,
    MissingBlobs,
    NotFound,
    Protected,
    StoreError,
    Unauthorized,
    Unreachable,
)
from ..store.ids import valid_id
from ..store.local import FileRead
from ..store.refs import Ref
from .http import BATCH, Client, ClientError, upload_missing

ANNOUNCE = 3.0  # seconds after which an upload in progress is announced (once)


def q(text: str) -> str:
    return quote(text, safe="")


def handshake(client: Client, url: str) -> str:
    """The server's repo id, after making sure it speaks this client's API version (`GET /api/v3/repo`). A server of
    another release is refused (Incompatible): a 0.14 server answers only `/api/v2/repo`."""
    try:
        answer = client.json("GET", f"{API_PREFIX}/repo")[1]
    except ClientError as error:
        if error.status in (400, 404, 410):
            try:
                old = client.json("GET", "/api/v2/repo")[1]
            except ClientError:
                old = None
            if isinstance(old, dict) and old.get("uid"):
                raise Incompatible(
                    f"the remote {url} runs Cairn {old.get('version') or '0.14'} (API v2), and this is Cairn "
                    f"{__version__} (API v{API}): a server and its clients upgrade together (on the server: install "
                    "Cairn 0.15, stop `cairn server`, run `cairn upgrade <its repo>`, start it again)"
                ) from None
        raise _translate(error, url, client) from error
    if not isinstance(answer, dict) or answer.get("api") != API:
        theirs = answer.get("api") if isinstance(answer, dict) else None
        raise Incompatible(
            f"the remote {url} runs Cairn {answer.get('cairn') if isinstance(answer, dict) else '?'} (API v{theirs}), "
            f"and this is Cairn {__version__} (API v{API}): install the same release on both"
        )
    try:
        return valid_id(answer.get("id"))
    except ValueError:
        raise Incompatible(f"the remote {url} answered its handshake without an id: {answer!r}") from None


class Server:
    """A `cairn server` (`url`, with its path prefix if it has one): its id, its projects, and each project as a Remote."""

    def __init__(self, client: Client):
        self.client, self.url = client, client.url
        self._id: str | None = None

    def __repr__(self) -> str:
        return f"<cairn.Server {self.url}>"

    def _call(self, method: str, path: str, value=None, expect=(200,)):
        try:
            return self.client.json(method, path, value, expect)[1]
        except ClientError as error:
            raise _translate(error, self.url, self.client) from error

    @property
    def id(self) -> str:
        """The server's repo id (the handshake, once; Unreachable while it does not answer)."""
        if self._id is None:
            self._id = handshake(self.client, self.url)
        return self._id

    def projects(self) -> list[dict]:
        """The projects the token may read, with their artifact count and last update."""
        self.id  # noqa: B018  (the handshake first)
        return self._call("GET", f"{API_PREFIX}/projects")

    overviews = projects

    def project(self, name: str) -> "Remote":
        remote = Remote(self.client, name)
        remote._id = self._id
        return remote

    def gc(self, dry_run: bool = True, grace: float | str | None = None) -> dict:
        """Garbage collection on the server (the owner token only)."""
        return self._call("POST", f"{API_PREFIX}/gc", {"dry_run": dry_run, "grace": grace})


class Remote:
    """The project `project` of the server the client talks to: `url` is `<server>/<project>`."""

    def __init__(self, client: Client, project: str):
        self.client, self.project = client, refs.valid_project(project)
        self.url = f"{client.url}/{project}"
        self._id: str | None = None

    def __repr__(self) -> str:
        return f"<cairn.Remote {self.url}>"

    @property
    def id(self) -> str:
        """The server's repo id (the handshake, once; Unreachable while it does not answer)."""
        if self._id is None:
            self._id = handshake(self.client, self.url)
        return self._id

    def _call(self, method: str, path: str, value=None, expect=(200,)):
        try:
            return self.client.json(method, path, value, expect)[1]
        except ClientError as error:
            raise _translate(error, self.url, self.client) from error

    @property
    def _base(self) -> str:
        return f"{API_PREFIX}/projects/{q(self.project)}"

    # ------------------------------------------------------------------ blobs (the server's, shared by its projects)

    def missing(self, entries: list[dict]) -> list[str]:
        try:
            return sorted(self.client.missing(sorted({entry["sha256"] for entry in entries if "sha256" in entry})))
        except ClientError as error:
            raise _translate(error, self.url, self.client) from error

    def upload(self, sources: dict[str, tuple[Path, int]], cache, quiet: bool = False, what: str | None = None) -> None:
        """Upload, in parallel, the blobs of `sources` ({sha: (file, size)}) that the server lacks. With `what` (a
        version's name), an upload still going after ANNOUNCE seconds says so once on stderr."""
        errors: list = []
        size = sum(item[1] for item in sources.values())
        timer = (
            threading.Timer(
                ANNOUNCE,
                lambda: print(
                    f"cairn: sending {size / 1e6:.0f} MB of {what} to {self.url}", file=sys.stderr, flush=True
                ),
            )
            if what
            else None
        )
        if timer is not None:
            timer.daemon = True
            timer.start()
        try:
            failed, _ = upload_missing(self.client, sources, cache, errors=errors, quiet=quiet)
        except ClientError as error:
            raise _translate(error, self.url, self.client) from error
        finally:
            if timer is not None:
                timer.cancel()
        if failed:
            if any(
                isinstance(error, ClientError) and (error.unreachable or (error.status or 0) >= 500) for error in errors
            ):
                # a remote that errors (5xx: a full disk, a crash) is a remote that does not answer now (as _translate)
                raise Unreachable(f"{len(failed)} files were not uploaded to {self.url}: {errors[0]}")
            raise StoreError(f"{len(failed)} files were not uploaded to {self.url}; publish again")

    def put_blob(self, sha: str, stream, size: int) -> bool:
        try:
            status, _ = self.client.request(
                "PUT",
                f"{API_PREFIX}/blobs/{sha}",
                body=stream,
                headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
                expect=(200, 201),
            )
        except ClientError as error:
            raise _translate(error, self.url, self.client) from error
        return status == 201

    def _file_path(self, ref: Ref) -> str:
        """`/files/<project>/<reference>//<path>`: the reference as text (the server parses it), quoted."""
        head = ref.id if ref.id is not None else quote(str(Ref(ref.name, ref.selector)))
        return f"/files/{q(self.project)}/{head}//{quote(ref.path)}"

    def blob_file(self, name: str, version: int | str, entry: dict, root) -> Path:
        """The entry's contents in the blobs of the repo `root`, downloaded (and verified) once; `version` is the
        number (or id) of a version that has the file.

        The download goes to a scratch name and is renamed into place, so concurrent jobs sharing the repo
        (on NFS too) never see a partial blob; when two download the same blob, the second rename is skipped."""
        sha = entry["sha256"]
        target = blob_path(root, sha)
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = scratch(target)
        try:
            ref = (
                Ref(name, f"v{version}", entry["path"]) if isinstance(version, int) else Ref.of(version, entry["path"])
            )
            where = self._file_path(ref)
            try:
                self.client.request("GET", where, stream_to=temporary)
            except ClientError as error:
                raise _translate(error, self.url, self.client) from error
            digest = hashlib.sha256()
            with temporary.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
            if digest.hexdigest() != sha:
                raise StoreError(f"{self.url}{where}: downloaded content does not match its SHA-256")
            place(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def read_file(self, ref: Ref | str, range_header: str | None = None) -> FileRead:
        """`LocalRepo.read_file` through the server's `/files` route, which gets the Range header as is."""
        ref = refs.parse(ref)
        if not ref.path:
            raise refs.InvalidRef(f"{ref} names a version, not a file in it (add //<path>)")
        where = self._file_path(ref)
        try:
            status, headers, chunks = self.client.stream(where, {"Range": range_header} if range_header else None)
        except ClientError as error:
            if error.status == 416:
                total = re.fullmatch(r"bytes \*/(\d+)", error.headers.get("content-range", ""))
                raise BadRange(int(total.group(1)) if total else 0) from error
            raise _translate(error, self.url, self.client) from error
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("content-range", ""))
        if match:
            first, last, total = map(int, match.groups())
        else:
            total = int(headers.get("content-length", 0))
            first, last = 0, total - 1
        return FileRead(
            total,
            first,
            last,
            headers.get("etag", "").strip('"'),
            "immutable" in headers.get("cache-control", ""),
            chunks,
        )

    # --------------------------------------------------------------- the project

    def record(self) -> dict:
        return self._call("GET", self._base)

    def create(self, title: str | None = None, description: str = "") -> dict:
        """Make the project on the server if it is new."""
        return self._call(
            "POST",
            f"{API_PREFIX}/projects",
            {"name": self.project, "title": title, "description": description},
            expect=(200, 201),
        )

    def update(self, title: str | None = None, description: str | None = None) -> dict:
        return self._call("PATCH", self._base, {"title": title, "description": description})

    # --------------------------------------------------------------- artifacts

    def _artifact(self, name: str) -> str:
        return f"{self._base}/artifacts/{q(name)}"

    def summaries(self) -> list[dict]:
        return self._call("GET", f"{self._base}/artifacts")

    def summary(self, name: str) -> dict:
        return {key: value for key, value in self._call("GET", self._artifact(name)).items() if key != "latest_version"}

    def artifacts(self) -> list[str]:
        return [item["name"] for item in self.summaries()]

    def versions(self, name: str) -> list[dict]:
        return self._call("GET", f"{self._artifact(name)}/versions")

    def _version(self, name: str, version: int | str) -> str:
        return f"{self._artifact(name)}/versions/{refs.version_segment(version)}"

    def get_version(self, name: str, version: int | str) -> dict:
        """One manifest by number or id, whatever its status."""
        return self._call("GET", self._version(name, version))

    def aliases(self, name: str) -> dict[str, int]:
        return self._call("GET", f"{self._artifact(name)}/aliases")

    def start_version(
        self,
        name: str,
        label: str | None = None,
        metadata: dict | None = None,
        source: dict | None = None,
        id: str | None = None,
    ) -> int:
        return self._call(
            "POST",
            f"{self._artifact(name)}/versions",
            {"label": label, "metadata": metadata, "source": source, "id": id},
            expect=(201,),
        )["version"]

    def add_files(self, name: str, version: int | str, entries: list[dict]) -> None:
        for start in range(0, len(entries), BATCH):
            self._call("POST", f"{self._version(name, version)}/files", {"entries": entries[start : start + BATCH]})

    def keep_alive(self, name: str, version: int | str) -> None:
        """Tell the remote a running version is still being synced (an empty file list), so it does not time it out."""
        self._call("POST", f"{self._version(name, version)}/files", {"entries": []})

    def set_metadata(self, name: str, version: int | str, metadata: dict) -> None:
        """Replace the metadata of a running version."""
        self._call("POST", f"{self._version(name, version)}/metadata", {"metadata": metadata})

    def complete_version(self, name: str, version: int | str, inputs=None, digest: str | None = None) -> None:
        """Complete the version; a `digest` (store.digest) of its files makes the server check them (409 on a mismatch)."""
        body = {"inputs": [str(item) for item in inputs or []]}
        if digest is not None:
            body["digest"] = digest
        self._call("POST", f"{self._version(name, version)}/complete", body)

    def fail_version(self, name: str, version: int | str) -> None:
        self._call("POST", f"{self._version(name, version)}/fail", {})

    def publish(
        self,
        name: str,
        entries: list[dict],
        label: str | None = None,
        metadata: dict | None = None,
        inputs=None,
        aliases=(),
        source: dict | None = None,
        id: str | None = None,
        digest: str | None = None,
    ) -> int:
        body = {
            "entries": entries,
            "label": label,
            "metadata": metadata,
            "inputs": [str(item) for item in inputs or []],
            "aliases": list(aliases),
            "source": source,
            "id": id,
        }
        if digest is not None:
            body["digest"] = digest
        answer = self._call("POST", f"{self._artifact(name)}/publish", body, expect=(200, 201))
        return answer["version"]

    def set_alias(self, name: str, alias: str, version: int) -> None:
        self._call("POST", f"{self._artifact(name)}/aliases", {"alias": alias, "version": version})

    # ------------------------------------------------------- deleting, retention, gc

    @staticmethod
    def _flags(force: bool, reason: str | None) -> str:
        flags = (["force=1"] if force else []) + ([f"reason={q(reason)}"] if reason else [])
        return "?" + "&".join(flags) if flags else ""

    def delete_version(self, name: str, version: int | str, *, reason: str | None = None, force: bool = False) -> dict:
        """`LocalRepo.delete_version` on the server (a write token; `force` needs the owner token); `version` is a
        number or an id."""
        return self._call("DELETE", f"{self._version(name, version)}{self._flags(force, reason)}")

    def delete_artifact(self, name: str, *, reason: str | None = None, force: bool = False) -> dict:
        return self._call("DELETE", f"{self._artifact(name)}{self._flags(force, reason)}")

    def retention(self) -> list[dict]:
        return self._call("GET", f"{self._base}/retention")["rules"]

    def set_retention(self, rules: list[dict]) -> list[dict]:
        return self._call("PUT", f"{self._base}/retention", {"rules": rules})["rules"]

    def gc(self, dry_run: bool = True, grace: float | str | None = None) -> dict:
        """Garbage collection on the whole server (the owner token only)."""
        return self._call("POST", f"{API_PREFIX}/gc", {"dry_run": dry_run, "grace": grace})

    # ------------------------------------------------------------- resolving

    def resolve(self, ref: Ref | str) -> tuple[str, dict]:
        """(name, manifest) of the completed version a reference names (the server parses it)."""
        answer = self._call("GET", f"{self._base}/resolve?ref={q(str(refs.parse(ref)))}")
        return answer["name"], answer["version"]

    def find(self, ref: Ref | str) -> tuple[str, dict]:
        """(name, manifest) of the version a reference names, any status for an id or `name@v<n>`."""
        answer = self._call("GET", f"{self._base}/find?ref={q(str(refs.parse(ref)))}")
        return answer["name"], answer["version"]

    def ids(self, prefix: str, name: str | None = None) -> list[dict]:
        """The versions of the project whose id starts with `prefix` (refs.pick chooses among them)."""
        return self._call("GET", f"{self._base}/ids?prefix={q(prefix)}" + (f"&name={q(name)}" if name else ""))[
            "versions"
        ]

    def short_ids(self, ids) -> dict[str, str]:
        """{id: short id} in the project, as the server computes them (an id it does not have: the prefix none of its
        versions shares)."""
        ids = list(dict.fromkeys(ids))
        found: dict[str, str] = {}
        for start in range(0, len(ids), 1000):
            found.update(self._call("POST", f"{self._base}/short-ids", {"ids": ids[start : start + 1000]})["ids"])
        return found

    def lineage(self, ref: Ref | str, marks: bool = False) -> dict[str, list[str]]:
        answer = self._call("GET", f"{self._base}/lineage?ref={q(str(refs.parse(ref)))}")
        found = {"id": answer["id"], "inputs": answer["inputs"], "consumers": answer["consumers"]}
        if marks:
            found["deleted"] = answer.get("deleted", [])
            found["nodes"] = answer.get("nodes", {})
        return found


def _translate(error: ClientError, url: str, client: Client) -> StoreError:
    """404 is a NotFound, 410 a Deleted, 401 and 403 say where the token came from; every other server answer is a
    StoreError with the server's message. What was looked for names the remote (`url`) it was looked for on."""
    text = str(error)
    if error.unreachable or (error.status or 0) >= 500:  # a remote that errors is a remote that does not answer now
        return Unreachable(text)
    if error.status == 401:
        return Unauthorized(client.refused(url))
    if error.status == 403:
        return Forbidden(
            f"{text} ({client.refused(url).replace(' was refused by ', ' on ')})" if client.token else text
        )
    if error.status in (404, 410):
        located = f"{text} on the remote {url}" if " on the remote " not in text else text
        if error.status == 410:
            return Deleted(located)
        answer = error.answer()
        if isinstance(answer.get("candidates"), list):
            return Ambiguous(located, answer["candidates"])
        return NotFound(located)
    answer = error.answer() if error.status == 409 else {}
    if isinstance(answer.get("missing"), list):
        return MissingBlobs(answer["missing"], text)
    if isinstance(answer.get("protected"), list):
        return Protected(text, answer["protected"])
    if isinstance(answer.get("candidates"), list):
        return Ambiguous(f"{text} on the remote {url}", answer["candidates"])
    return {409: Conflict}.get(error.status, StoreError)(text)
