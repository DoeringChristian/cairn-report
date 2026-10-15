"""RemoteStore: the LocalStore interface over HTTP, against `cairn-report store serve`.

It offers what `cairn_report.store` and `Publication` use: projects, blob upload, versions, aliases,
`resolve`, `lineage`, and `blob_file` for the pull cache. HTTP, the token, and the HTTPS rule are
`push.Client`'s. Artifact names travel percent-encoded in API paths (`datasets%2Fcrops`).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from urllib.parse import quote

from ..push import BATCH, Client, ClientError, upload_missing
from . import refs
from .local import NotFound, StoreError
from .refs import Ref


def q(text: str) -> str:
    return quote(text, safe="")


class RemoteStore:
    def __init__(self, client: Client, project: str | None = None):
        """`project` is the default for references without one."""
        self.client, self.project = client, project
        self.url = client.url

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

    def upload(self, sources: dict[str, tuple[Path, int]], cache) -> None:
        """Upload, in parallel, the blobs of `sources` ({sha: (file, size)}) that the server lacks."""
        try:
            failed, _ = upload_missing(self.client, sources, cache)
        except ClientError as error:
            raise _translate(error) from error
        if failed:
            raise StoreError(f"{len(failed)} files were not uploaded; publish again")

    def put_blob(self, sha: str, stream, size: int) -> bool:
        try:
            status, _ = self.client.request("PUT", f"/api/v2/blobs/{sha}", body=stream,
                                         headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
                                         expect=(200, 201))
        except ClientError as error:
            raise _translate(error) from error
        return status == 201

    def blob_file(self, project: str, name: str, version: int, entry: dict, cache_root) -> Path:
        """The entry's contents in the blob cache under `cache_root`, downloaded (and verified) once."""
        sha = entry["sha256"]
        target = Path(cache_root) / ".blobs" / sha[:2] / sha[2:]
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{sha[2:]}.{secrets.token_hex(6)}.tmp")
        try:
            where = f"/files/{q(project)}/{quote(name)}@v{version}//{quote(entry['path'])}"
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
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    # --------------------------------------------------------------- projects

    def create_project(self, name: str, title: str | None = None, description: str = "") -> dict:
        return self._call("POST", "/api/v2/projects", {"name": name, "title": title, "description": description}, expect=(200, 201))

    def get_project(self, name: str) -> dict:
        return self._call("GET", f"/api/v2/projects/{q(name)}")

    def update_project(self, name: str, title: str | None = None, description: str | None = None) -> dict:
        return self._call("PATCH", f"/api/v2/projects/{q(name)}", {"title": title, "description": description})

    def projects(self) -> list[dict]:
        return self._call("GET", "/api/v2/projects")

    # --------------------------------------------------------------- artifacts

    def _artifact(self, project: str, name: str) -> str:
        return f"/api/v2/projects/{q(project)}/artifacts/{q(name)}"

    def summaries(self, project: str) -> list[dict]:
        return self._call("GET", f"/api/v2/projects/{q(project)}/artifacts")

    def artifacts(self, project: str) -> list[str]:
        return [item["name"] for item in self.summaries(project)]

    def versions(self, project: str, name: str) -> list[dict]:
        return self._call("GET", f"{self._artifact(project, name)}/versions")

    def get_version(self, project: str, name: str, version: int) -> dict:
        return self._call("GET", f"{self._artifact(project, name)}/versions/{version}")

    def aliases(self, project: str, name: str) -> dict[str, int]:
        return self._call("GET", f"{self._artifact(project, name)}/aliases")

    def start_version(self, project: str, name: str, label: str | None = None, metadata: dict | None = None,
                      source: dict | None = None) -> int:
        return self._call("POST", f"{self._artifact(project, name)}/versions",
                          {"label": label, "metadata": metadata, "source": source}, expect=(201,))["version"]

    def add_files(self, project: str, name: str, version: int, entries: list[dict]) -> None:
        for start in range(0, len(entries), BATCH):
            self._call("POST", f"{self._artifact(project, name)}/versions/{version}/files", {"entries": entries[start:start + BATCH]})

    def complete_version(self, project: str, name: str, version: int, inputs=None) -> None:
        self._call("POST", f"{self._artifact(project, name)}/versions/{version}/complete", {"inputs": [str(item) for item in inputs or []]})

    def fail_version(self, project: str, name: str, version: int) -> None:
        self._call("POST", f"{self._artifact(project, name)}/versions/{version}/fail", {})

    def publish(self, project: str, name: str, entries: list[dict], label: str | None = None, metadata: dict | None = None,
                inputs=None, aliases=(), source: dict | None = None) -> int:
        answer = self._call("POST", f"{self._artifact(project, name)}/publish", {
            "entries": entries, "label": label, "metadata": metadata, "inputs": [str(item) for item in inputs or []],
            "aliases": list(aliases), "source": source}, expect=(200, 201))
        return answer["version"]

    def set_alias(self, project: str, name: str, alias: str, version: int) -> None:
        self._call("POST", f"{self._artifact(project, name)}/aliases", {"alias": alias, "version": version})

    # ------------------------------------------------------------- resolving

    def _qualified(self, ref: Ref | str) -> Ref:
        ref = refs.parse(ref) if isinstance(ref, str) else ref
        return Ref(self._project(ref.project, f"reference {ref}"), ref.name, ref.selector, ref.path)

    def resolve(self, ref: Ref | str) -> tuple[str, str, dict]:
        answer = self._call("GET", f"/api/v2/resolve?ref={q(str(self._qualified(ref)))}")
        return answer["project"], answer["name"], answer["version"]

    def lineage(self, ref: Ref | str) -> dict[str, list[str]]:
        answer = self._call("GET", f"/api/v2/lineage?ref={q(str(self._qualified(ref)))}")
        return {"inputs": answer["inputs"], "consumers": answer["consumers"]}


def _translate(error: ClientError) -> StoreError:
    """404 is a NotFound; every other server answer is a StoreError with the server's message."""
    text = str(error)
    return NotFound(text) if error.status == 404 else StoreError(text)
