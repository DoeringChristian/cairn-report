"""Remote: the LocalRepo interface over HTTP, against a `cairn server` (a repo's remote).

It offers what the repo (client/repo.py), sync (client/sync.py), and the UI use: projects, blob upload, versions,
aliases, `resolve`, `lineage`, and `blob_file`, which downloads a blob into a repo. HTTP, the token, and the HTTPS
rule are `http.Client`'s. Artifact names travel percent-encoded in API paths (`datasets%2Fcrops`).

A remote that does not answer raises `Unreachable` (a StoreError), which the repo catches: versions wait in the
outbox, and reads fall back to the repo. `uid` is the remote's repo uid (`GET /api/v2/repo`).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import quote

from ..store import refs
from ..store.blobs import blob_path
from ..store.folders import place, scratch
from ..store.errors import BadRange, Conflict, Deleted, Forbidden, MissingBlobs, NotFound, Protected, StoreError, Unauthorized, Unreachable
from ..store.local import FileRead
from ..store.refs import Ref
from ..store.uids import UID
from .http import BATCH, Client, ClientError, upload_missing


def q(text: str) -> str:
    return quote(text, safe="")


class Remote:
    def __init__(self, client: Client, project: str | None = None):
        """`project` is the default for references without one."""
        self.client, self.project = client, project
        self.url = client.url
        self._uid: str | None = None

    @property
    def uid(self) -> str:
        """The remote's repo uid (asked once; Unreachable while it does not answer)."""
        if self._uid is None:
            self._uid = self._call("GET", "/api/v2/repo")["uid"]
        return self._uid

    def _call(self, method: str, path: str, value=None, expect=(200,)):
        try:
            return self.client.json(method, path, value, expect)[1]
        except ClientError as error:
            raise _translate(error) from error

    def _project(self, project: str | None, what: str) -> str:
        project = project or self.project
        if project is None:
            raise StoreError(f"{what} has no project and the store has no default")
        return project

    # ------------------------------------------------------------------ blobs

    def missing(self, entries: list[dict]) -> list[str]:
        try:
            return sorted(self.client.missing(sorted({entry["sha256"] for entry in entries if "sha256" in entry})))
        except ClientError as error:
            raise _translate(error) from error

    def upload(self, sources: dict[str, tuple[Path, int]], cache, quiet: bool = False) -> None:
        """Upload, in parallel, the blobs of `sources` ({sha: (file, size)}) that the server lacks."""
        errors: list = []
        try:
            failed, _ = upload_missing(self.client, sources, cache, errors=errors, quiet=quiet)
        except ClientError as error:
            raise _translate(error) from error
        if failed:
            if any(isinstance(error, ClientError) and error.unreachable for error in errors):
                raise Unreachable(f"{len(failed)} files were not uploaded: {errors[0]}")
            raise StoreError(f"{len(failed)} files were not uploaded; publish again")

    def put_blob(self, sha: str, stream, size: int) -> bool:
        try:
            status, _ = self.client.request("PUT", f"/api/v2/blobs/{sha}", body=stream,
                                         headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
                                         expect=(200, 201))
        except ClientError as error:
            raise _translate(error) from error
        return status == 201

    def blob_file(self, project: str, name: str, version: int | str, entry: dict, root) -> Path:
        """The entry's contents in the blobs of the repo `root`, downloaded (and verified) once; `version` is the
        number (or uid) of a version that has the file.

        The download goes to a scratch name and is renamed into place, so concurrent jobs sharing the repo
        (on NFS too) never see a partial blob; when two download the same blob, the second rename is skipped."""
        sha = entry["sha256"]
        target = blob_path(root, sha)
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = scratch(target)
        try:
            selector = f"v{version}" if isinstance(version, int) else version
            where = f"/files/{q(project)}/{quote(name)}@{selector}//{quote(entry['path'])}"
            try:
                self.client.request("GET", where, stream_to=temporary)
            except ClientError as error:
                raise _translate(error) from error
            digest = hashlib.sha256()
            with temporary.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
            if digest.hexdigest() != sha:
                raise StoreError(f"{where}: downloaded content does not match its SHA-256")
            place(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def read_file(self, project: str, name: str, selector: str | None, path: str, range_header: str | None = None) -> FileRead:
        """`LocalRepo.read_file` through the server's `/files` route, which gets the Range header as is."""
        where = f"/files/{q(project)}/{quote(name)}@{selector or 'latest'}//{quote(path)}"
        try:
            status, headers, chunks = self.client.stream(where, {"Range": range_header} if range_header else None)
        except ClientError as error:
            if error.status == 416:
                total = re.fullmatch(r"bytes \*/(\d+)", error.headers.get("content-range", ""))
                raise BadRange(int(total.group(1)) if total else 0) from error
            raise _translate(error) from error
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("content-range", ""))
        if match:
            first, last, total = map(int, match.groups())
        else:
            total = int(headers.get("content-length", 0))
            first, last = 0, total - 1
        return FileRead(total, first, last, headers.get("etag", "").strip('"'), "immutable" in headers.get("cache-control", ""), chunks)

    # --------------------------------------------------------------- projects

    def create_project(self, name: str, title: str | None = None, description: str = "") -> dict:
        return self._call("POST", "/api/v2/projects", {"name": name, "title": title, "description": description}, expect=(200, 201))

    def get_project(self, name: str) -> dict:
        return self._call("GET", f"/api/v2/projects/{q(name)}")

    def update_project(self, name: str, title: str | None = None, description: str | None = None) -> dict:
        return self._call("PATCH", f"/api/v2/projects/{q(name)}", {"title": title, "description": description})

    def projects(self) -> list[dict]:
        return self._call("GET", "/api/v2/projects")

    overviews = projects  # the server's project list already carries the artifact count and last update

    # --------------------------------------------------------------- artifacts

    def _artifact(self, project: str, name: str) -> str:
        return f"/api/v2/projects/{q(project)}/artifacts/{q(name)}"

    def summaries(self, project: str) -> list[dict]:
        return self._call("GET", f"/api/v2/projects/{q(project)}/artifacts")

    def summary(self, project: str, name: str) -> dict:
        return {key: value for key, value in self._call("GET", self._artifact(project, name)).items() if key != "latest_version"}

    def artifacts(self, project: str) -> list[str]:
        return [item["name"] for item in self.summaries(project)]

    def versions(self, project: str, name: str) -> list[dict]:
        return self._call("GET", f"{self._artifact(project, name)}/versions")

    def get_version(self, project: str, name: str, version: int | str) -> dict:
        """One manifest by number or uid, whatever its status."""
        if isinstance(version, str) and not UID.fullmatch(version):
            raise ValueError(f"invalid version {version!r}")
        return self._call("GET", f"{self._artifact(project, name)}/versions/{version}")

    def aliases(self, project: str, name: str) -> dict[str, int]:
        return self._call("GET", f"{self._artifact(project, name)}/aliases")

    def start_version(self, project: str, name: str, label: str | None = None, metadata: dict | None = None,
                      source: dict | None = None, uid: str | None = None) -> int:
        return self._call("POST", f"{self._artifact(project, name)}/versions",
                          {"label": label, "metadata": metadata, "source": source, "uid": uid}, expect=(201,))["version"]

    def add_files(self, project: str, name: str, version: int, entries: list[dict]) -> None:
        for start in range(0, len(entries), BATCH):
            self._call("POST", f"{self._artifact(project, name)}/versions/{version}/files", {"entries": entries[start:start + BATCH]})

    def keep_alive(self, project: str, name: str, version: int) -> None:
        """Tell the remote a running version is still being synced (an empty file list), so it does not time it out."""
        self._call("POST", f"{self._artifact(project, name)}/versions/{version}/files", {"entries": []})

    def set_metadata(self, project: str, name: str, version: int, metadata: dict) -> None:
        """Replace the metadata of a running version."""
        self._call("POST", f"{self._artifact(project, name)}/versions/{version}/metadata", {"metadata": metadata})

    def complete_version(self, project: str, name: str, version: int, inputs=None, digest: str | None = None) -> None:
        """Complete the version; a `digest` (store.digest) of its files makes the server check them (409 on a mismatch)."""
        body = {"inputs": [str(item) for item in inputs or []]}
        if digest is not None:
            body["digest"] = digest
        self._call("POST", f"{self._artifact(project, name)}/versions/{version}/complete", body)

    def fail_version(self, project: str, name: str, version: int) -> None:
        self._call("POST", f"{self._artifact(project, name)}/versions/{version}/fail", {})

    def publish(self, project: str, name: str, entries: list[dict], label: str | None = None, metadata: dict | None = None,
                inputs=None, aliases=(), source: dict | None = None, uid: str | None = None,
                digest: str | None = None) -> int:
        body = {"entries": entries, "label": label, "metadata": metadata, "inputs": [str(item) for item in inputs or []],
                "aliases": list(aliases), "source": source, "uid": uid}
        if digest is not None:
            body["digest"] = digest
        answer = self._call("POST", f"{self._artifact(project, name)}/publish", body, expect=(200, 201))
        return answer["version"]

    def set_alias(self, project: str, name: str, alias: str, version: int) -> None:
        self._call("POST", f"{self._artifact(project, name)}/aliases", {"alias": alias, "version": version})

    # ------------------------------------------------------- deleting, retention, gc

    @staticmethod
    def _flags(force: bool, reason: str | None) -> str:
        flags = (["force=1"] if force else []) + ([f"reason={q(reason)}"] if reason else [])
        return "?" + "&".join(flags) if flags else ""

    def delete_version(self, project: str, name: str, version: int | str, *, reason: str | None = None, force: bool = False) -> dict:
        """`LocalRepo.delete_version` on the server (a write token; `force` needs the owner token)."""
        if isinstance(version, str) and refs.VERSION.fullmatch(version):
            version = int(version[1:])
        if isinstance(version, str) and not UID.fullmatch(version):
            raise ValueError(f"invalid version {version!r}")
        return self._call("DELETE", f"{self._artifact(project, name)}/versions/{version}{self._flags(force, reason)}")

    def delete_artifact(self, project: str, name: str, *, reason: str | None = None, force: bool = False) -> dict:
        return self._call("DELETE", f"{self._artifact(project, name)}{self._flags(force, reason)}")

    def delete_project(self, project: str, *, reason: str | None = None, force: bool = False) -> dict:
        return self._call("DELETE", f"/api/v2/projects/{q(project)}{self._flags(force, reason)}")

    def retention(self, project: str) -> list[dict]:
        return self._call("GET", f"/api/v2/projects/{q(project)}/retention")["rules"]

    def set_retention(self, project: str, rules: list[dict]) -> list[dict]:
        return self._call("PUT", f"/api/v2/projects/{q(project)}/retention", {"rules": rules})["rules"]

    def gc(self, dry_run: bool = True, grace: float | str | None = None) -> dict:
        """Garbage collection on the server (the owner token only)."""
        return self._call("POST", "/api/v2/gc", {"dry_run": dry_run, "grace": grace})

    # ------------------------------------------------------------- resolving

    def _qualified(self, ref: Ref | str) -> Ref:
        ref = refs.parse(ref) if isinstance(ref, str) else ref
        return Ref(self._project(ref.project, f"reference {ref}"), ref.name, ref.selector, ref.path)

    def resolve(self, ref: Ref | str) -> tuple[str, str, dict]:
        answer = self._call("GET", f"/api/v2/resolve?ref={q(str(self._qualified(ref)))}")
        return answer["project"], answer["name"], answer["version"]

    def lineage(self, ref: Ref | str, marks: bool = False) -> dict[str, list[str]]:
        answer = self._call("GET", f"/api/v2/lineage?ref={q(str(self._qualified(ref)))}")
        found = {"inputs": answer["inputs"], "consumers": answer["consumers"]}
        if marks:
            found["deleted"] = answer.get("deleted", [])
            found["nodes"] = answer.get("nodes", {})
        return found


def _translate(error: ClientError) -> StoreError:
    """404 is a NotFound, 410 a Deleted; every other server answer is a StoreError with the server's message."""
    text = str(error)
    if error.unreachable or (error.status or 0) >= 500:  # a remote that errors is a remote that does not answer now
        return Unreachable(text)
    if error.status == 404:
        return NotFound(text)
    if error.status == 410:
        return Deleted(text)
    answer = error.answer() if error.status == 409 else {}
    if isinstance(answer.get("missing"), list):
        return MissingBlobs(answer["missing"], text)
    if isinstance(answer.get("protected"), list):
        return Protected(text, answer["protected"])
    return {401: Unauthorized, 403: Forbidden, 409: Conflict}.get(error.status, StoreError)(text)
