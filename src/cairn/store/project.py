"""One project of a repo folder, as if it were its own repo: `LocalProject(repo, project)`.

A server's repo (a LocalRepo) holds many projects; the server serves each of them through a LocalProject. A
project's own repo (`.cairn/`) holds one, the project OWN (local.py), which its compositions (client/repo.py) read
and write through a LocalProject too. Every reading and writing method of the repo interface (interface.py) is the
LocalRepo method of the same name with the project filled in; what belongs to the whole folder (blobs, the meta
table, heartbeats, gc, verify) is the folder's.
"""

from __future__ import annotations

from pathlib import Path

from .local import OWN, LocalRepo

_PROJECT = (
    "summaries",
    "summaries_stamped",
    "summary",
    "artifacts",
    "versions",
    "get_version",
    "aliases",
    "alias_log",
    "version_numbers",
    "last_number",
    "resolve",
    "find",
    "ids",
    "lineage",
    "start_version",
    "add_files",
    "set_metadata",
    "complete_version",
    "fail_version",
    "publish",
    "set_alias",
    "delete_version",
    "delete_artifact",
    "retention",
    "set_retention",
    "read_file",
    "open_file",
    "put_copy",
    "mirror_aliases",
    "local_versions",
    "copies",
)
_FOLDER = (
    "missing",
    "missing_hashes",
    "put_blob",
    "blob_writer",
    "put_file",
    "blob_path",
    "has_blob",
    "meta",
    "set_meta",
    "beat",
    "reap",
    "forget_versions",
    "gc",
    "verify",
    "export_metadata",
)


class LocalProject:
    """The project `project` of the repo folder `repo` (OWN: the folder's own, unnamed project)."""

    def __init__(self, repo: LocalRepo, project: str = OWN):
        self.repo, self.project = repo, project

    def __repr__(self) -> str:
        return f"<cairn.LocalProject {self.repo.root}{f' {self.project}' if self.project else ''}>"

    @property
    def root(self) -> Path:
        return self.repo.root

    @property
    def uid(self) -> str:
        """The repo folder's uid (`repo.json`)."""
        return self.repo.uid

    @property
    def reaped(self) -> list:
        return self.repo.reaped

    @property
    def where(self) -> str:
        return self.repo.where

    def record(self) -> dict:
        """The project's record: {name, title, description, created}."""
        return self.repo.get_project(self.project)

    def create(self, title: str | None = None, description: str = "") -> dict:
        """Make the project if it is new (its record either way)."""
        return self.repo.create_project(self.project, title, description)

    def update(self, title: str | None = None, description: str | None = None) -> dict:
        return self.repo.update_project(self.project, title, description)

    def blob_file(self, name: str, version, entry: dict, root=None) -> Path:
        """A local file with the contents of a version's entry (here: the blob itself)."""
        return self.repo.blob_path(entry["sha256"])


def _bound(name: str):
    def method(self, *args, **kwargs):
        return getattr(self.repo, name)(self.project, *args, **kwargs)

    method.__name__ = method.__qualname__ = name
    method.__doc__ = f"`LocalRepo.{name}` of this project."
    return method


def _folder(name: str):
    def method(self, *args, **kwargs):
        return getattr(self.repo, name)(*args, **kwargs)

    method.__name__ = method.__qualname__ = name
    method.__doc__ = f"`LocalRepo.{name}`: the repo folder's (all its projects)."
    return method


for _name in _PROJECT:
    setattr(LocalProject, _name, _bound(_name))
for _name in _FOLDER:
    setattr(LocalProject, _name, _folder(_name))
del _name
