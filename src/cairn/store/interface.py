"""The repo interface: what a project's repo answers and accepts, whether it is a project of a folder on this machine
(`LocalProject`, project.py) or a project on a remote reached over HTTP (`Remote`, client/remote.py). Both implement
it, so the sync engine (client/sync.py) moves versions from one to the other through it alone, and tests can stand in
fakes. Neither names a project in its calls: each is bound to one, as a repo holds one.

Versions are named by number (`int`, the source of truth's name) or id (64 hex digits, the identity everywhere).
References are parsed by store/refs.py alone. Manifests are dicts: version, id, label, status (running | completed |
failed | deleted), created, completed, files (entries {path, sha256, size} or {path, link}), metadata, inputs (the ids
of the versions it consumed), source, digest. A short id is not stored: `short_ids` computes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .local import FileRead
from .refs import Ref


@runtime_checkable
class Repository(Protocol):
    @property
    def id(self) -> str:
        """The repo's id (`repo.json`; on a remote, the server's repo), never changed."""

    # ------------------------------------------------------------------ blobs

    def missing(self, entries: list[dict]) -> list[str]:
        """The sha256s of the entries whose contents the repo does not have."""

    def put_blob(self, sha: str, stream, size: int) -> bool:
        """Store `size` bytes from `stream` as blob `sha` (refused if they do not hash to it); False if it was there."""

    def blob_file(self, name: str, version: int | str, entry: dict, root) -> Path:
        """A local file with the contents of a version's entry (for a remote: downloaded into the repo `root`);
        `version` is its number or id."""

    def read_file(self, ref: Ref | str, range_header: str | None = None) -> FileRead:
        """The file a reference with a path names (`name@v<n>//path`, `<id>//path`, ...), or a byte range of it."""

    # ------------------------------------------------------------------ the project

    def record(self) -> dict:
        """The project's record: {name, title, description, created}."""

    def create(self, title: str | None = None, description: str = "") -> dict:
        """Make the project if it is new."""

    # ------------------------------------------------------------------ reading

    def summaries(self) -> list[dict]:
        """The artifacts at a glance: name, latest, versions, aliases, updated, metadata (of latest)."""

    def summary(self, name: str) -> dict: ...

    def artifacts(self) -> list[str]: ...

    def versions(self, name: str) -> list[dict]:
        """Every manifest of an artifact, oldest first, whatever its status."""

    def get_version(self, name: str, version: int | str) -> dict:
        """One manifest by number or id, whatever its status (NotFound if there is none)."""

    def aliases(self, name: str) -> dict[str, int]: ...

    def resolve(self, ref: Ref | str) -> tuple[str, dict]:
        """(name, manifest) of the completed version a reference names (a bare name: `latest`)."""

    def find(self, ref: Ref | str) -> tuple[str, dict]:
        """(name, manifest) of the version a reference names: any status for an id or `name@v<n>`."""

    def ids(self, prefix: str, name: str | None = None) -> list[dict]:
        """The versions whose id starts with `prefix`: [{name, version, id, short_id, status}] (refs.pick)."""

    def short_ids(self, ids) -> dict[str, str]:
        """{id: short id}: the shortest prefix of at least 7 digits that no other id of the project starts with."""

    def lineage(self, ref: Ref | str, marks: bool = False) -> dict[str, list[str]]:
        """{id, inputs, consumers} as ids (with `marks`: `deleted` ids and `nodes` {id: {name, version, status, kind,
        short_id}})."""

    # ------------------------------------------------------------------ writing

    def start_version(
        self,
        name: str,
        label: str | None = None,
        metadata: dict | None = None,
        source: dict | None = None,
        id: str | None = None,
    ) -> int:
        """Start a running version (idempotent by id); returns its number."""

    def add_files(self, name: str, version: int | str, entries: list[dict]) -> None: ...

    def set_metadata(self, name: str, version: int | str, metadata: dict) -> None: ...

    def complete_version(self, name: str, version: int | str, inputs=None, digest: str | None = None) -> None: ...

    def fail_version(self, name: str, version: int | str) -> None: ...

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
        """Start, fill, and complete a version in one step (idempotent by id; no new version when nothing changed
        since the latest); returns its number."""

    def set_alias(self, name: str, alias: str, version: int) -> None: ...

    def delete_version(
        self, name: str, version: int | str, *, reason: str | None = None, force: bool = False
    ) -> dict: ...

    def delete_artifact(self, name: str, *, reason: str | None = None, force: bool = False) -> dict: ...

    def retention(self) -> list[dict]: ...

    def set_retention(self, rules: list[dict]) -> list[dict]: ...

    def gc(self, dry_run: bool = True, grace=None) -> dict:
        """Retention rules and the blob sweep of the whole repo folder (on a remote: the server's, owner token only)."""


METHODS = [name for name in Repository.__dict__ if not name.startswith("_") and callable(getattr(Repository, name))] + [
    "id"
]
