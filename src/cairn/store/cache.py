"""Local copies of artifact versions: `<cache_root>/<project>/<name>/v<n>/`, read-only and shared by all runs."""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

from . import refs
from .errors import NotFound
from .inputs import record_input

MARKER = ".cairn-complete"


def default_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cairn" / "artifacts"


def materialize(store, ref, cache_root: Path | str) -> Path:
    """Write a version's files into the cache (once) and return its folder, or the file `ref` names after `//`.

    Files are hard-linked from the store's blobs (a remote store keeps them in a blob cache under `cache_root`) when both are on one filesystem, else copied, and made
    read-only (a hard link shares its blob, so the blob is protected too). The folder is built aside and
    renamed into place, and holds a marker once complete, so an interrupted pull is simply redone.
    """
    ref = refs.parse(ref) if isinstance(ref, str) else ref
    project, name, version = store.resolve(ref)
    folder = Path(cache_root) / project / name / f"v{version['version']}"
    if not (folder / MARKER).is_file():
        build = folder.parent / f".tmp-{secrets.token_hex(6)}"
        try:
            for entry in version["files"]:
                target = build / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if "link" in entry:
                    os.symlink(entry["link"], target)
                    continue
                source = store.blob_file(project, name, version["version"], entry, cache_root)
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copyfile(source, target)
                os.chmod(target, 0o444)
            build.mkdir(parents=True, exist_ok=True)
            (build / MARKER).write_text("")
            if folder.exists():  # an interrupted earlier attempt
                shutil.rmtree(folder, ignore_errors=True)
            try:
                os.rename(build, folder)
            except OSError:
                if not (folder / MARKER).is_file():  # lost a race to another process only if it finished
                    raise
        finally:
            shutil.rmtree(build, ignore_errors=True)
    if ref.path:
        target = folder / ref.path
        if not os.path.lexists(target):
            raise NotFound(f"{project}:{name}@v{version['version']} has no {ref.path}")
        return target
    return folder


def pull(store, ref: str, record: bool = True) -> Path:
    """A read-only local copy of an artifact version (or of the file after `//`) from an open store.

    `record=False` leaves the inputs file alone: the report builder records a report's inputs in the
    report's own manifest instead.
    """
    parsed = refs.parse(ref)
    project, name, version = store.resolve(parsed)
    path = materialize(store, parsed, default_root())
    if record:
        record_input(refs.exact(project, name, version["version"]))
    return path
