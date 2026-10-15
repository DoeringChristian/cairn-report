"""The repo interface: what a repo answers and accepts, whether it is a folder on this machine (`LocalRepo`,
local.py) or a remote reached over HTTP (`Remote`, client/remote.py). Both implement it, so the sync engine
(client/sync.py) moves versions from one to the other through it alone, and tests can stand in fakes.

Versions are named by number (`int`, the source of truth's name) or uid (a UUIDv7 text, the identity everywhere).
Manifests are dicts: version, uid, label, status (running | completed | failed | deleted), created, completed, files
(entries {path, sha256, size} or {path, link}), metadata, inputs (exact references), source, digest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .local import FileRead
from .refs import Ref


@runtime_checkable
class Repository(Protocol):
    project: str | None  # the default project of references without one

    @property
    def uid(self) -> str:
        """The repo's uid (`repo.json`), never changed."""

    # ------------------------------------------------------------------ blobs

    def missing(self, entries: list[dict]) -> list[str]:
        """The sha256s of the entries whose contents the repo does not have."""

    def put_blob(self, sha: str, stream, size: int) -> bool:
        """Store `size` bytes from `stream` as blob `sha` (refused if they do not hash to it); False if it was there."""

    def blob_file(self, project: str, name: str, version: int | str, entry: dict, root) -> Path:
        """A local file with the contents of a version's entry (for a remote: downloaded into the repo `root`)."""

    def read_file(self, project: str, name: str, selector: str | None, path: str, range_header: str | None = None) -> FileRead:
        """A file of a version, or a byte range of it (`selector`: `v<n>`, a uid, an alias, or a label)."""

    # ------------------------------------------------------------------ projects

    def create_project(self, name: str, title: str | None = None, description: str = "") -> dict: ...

    def get_project(self, name: str) -> dict: ...

    def update_project(self, name: str, title: str | None = None, description: str | None = None) -> dict: ...

    def projects(self) -> list[dict]: ...

    def overviews(self) -> list[dict]:
        """Every project record with its artifact count and last update."""

    # ------------------------------------------------------------------ reading

    def summaries(self, project: str) -> list[dict]:
        """The project's artifacts at a glance: name, latest, versions, aliases, updated, metadata (of latest)."""

    def summary(self, project: str, name: str) -> dict: ...

    def artifacts(self, project: str) -> list[str]: ...

    def versions(self, project: str, name: str) -> list[dict]:
        """Every manifest of an artifact, oldest first, whatever its status."""

    def get_version(self, project: str, name: str, version: int | str) -> dict:
        """One manifest by number or uid, whatever its status (NotFound if there is none)."""

    def aliases(self, project: str, name: str) -> dict[str, int]: ...

    def resolve(self, ref: Ref | str) -> tuple[str, str, dict]:
        """(project, name, manifest) of the completed version a reference names (no selector: `latest`)."""

    def lineage(self, ref: Ref | str, marks: bool = False) -> dict[str, list[str]]:
        """{inputs, consumers} as exact references (with `marks`: `deleted` and `nodes` {ref: {status, kind}})."""

    # ------------------------------------------------------------------ writing

    def start_version(self, project: str, name: str, label: str | None = None, metadata: dict | None = None,
                      source: dict | None = None, uid: str | None = None) -> int:
        """Start a running version (idempotent by uid); returns its number."""

    def add_files(self, project: str, name: str, version: int, entries: list[dict]) -> None: ...

    def set_metadata(self, project: str, name: str, version: int, metadata: dict) -> None: ...

    def complete_version(self, project: str, name: str, version: int, inputs=None, digest: str | None = None) -> None: ...

    def fail_version(self, project: str, name: str, version: int) -> None: ...

    def publish(self, project: str, name: str, entries: list[dict], label: str | None = None, metadata: dict | None = None,
                inputs=None, aliases=(), source: dict | None = None, uid: str | None = None, digest: str | None = None) -> int:
        """Start, fill, and complete a version in one step (idempotent by uid; no new version when nothing changed
        since the latest); returns its number."""

    def set_alias(self, project: str, name: str, alias: str, version: int) -> None: ...

    def delete_version(self, project: str, name: str, version: int | str, *, reason: str | None = None, force: bool = False) -> dict: ...

    def delete_artifact(self, project: str, name: str, *, reason: str | None = None, force: bool = False) -> dict: ...

    def delete_project(self, project: str, *, reason: str | None = None, force: bool = False) -> dict: ...

    def retention(self, project: str) -> list[dict]: ...

    def set_retention(self, project: str, rules: list[dict]) -> list[dict]: ...

    def gc(self, dry_run: bool = True, grace=None) -> dict: ...


METHODS = [name for name in Repository.__dict__ if not name.startswith("_") and callable(getattr(Repository, name))] + ["uid"]
