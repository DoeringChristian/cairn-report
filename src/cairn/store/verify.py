"""Integrity checks: re-hash what a store, the pull cache, or the pending versions hold and say what is wrong.

    Report                              the problems found (kind, path, message) and how many things were checked
    sha256_file(path)                   (hex digest, size) of a file
    check_entries(folder, entries, ...) a materialized `files/` folder against the manifest entries it was made from
    verify_cache(root, repair)          a cache or data root: every version folder, and every downloaded/staged blob
    verify_store(store)                 a LocalStore: every blob matches its name, every version's blobs exist and
                                        its manifest digest matches its files

Nothing here changes anything except `repair=True` on a cache root, which removes what is broken (a version
folder, a blob) so the next pull downloads it again. Never repair a data root's pending versions: they are the
only copy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .blobs import SHA256
from .cache import FILES, read_record, remove
from .digest import manifest_digest, sha256_file


@dataclass
class Problem:
    kind: str       # blob | file | link | digest | version
    path: str       # the file or folder concerned (or `project:name@vN` for a store's version)
    message: str
    repaired: bool = False

    def __str__(self) -> str:
        return f"{self.kind}: {self.path}: {self.message}" + (" (removed)" if self.repaired else "")


@dataclass
class Report:
    """What a verification found. `ok` is True when nothing is wrong any more (a problem that `repair` removed
    does not count); `checked` counts what was looked at."""
    problems: list[Problem] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=lambda: {"versions": 0, "files": 0, "blobs": 0})

    @property
    def ok(self) -> bool:
        return all(problem.repaired for problem in self.problems)

    def add(self, kind: str, path, message: str, repaired: bool = False) -> Problem:
        problem = Problem(kind, str(path), message, repaired)
        self.problems.append(problem)
        return problem

    def count(self, what: str, number: int = 1) -> None:
        self.checked[what] = self.checked.get(what, 0) + number

    def merge(self, other: "Report") -> "Report":
        self.problems += other.problems
        for key, value in other.checked.items():
            self.count(key, value)
        return self

    def lines(self) -> list[str]:
        return [str(problem) for problem in self.problems]

    def summary(self) -> str:
        counted = ", ".join(f"{value} {key}" for key, value in self.checked.items())
        fixed = sum(problem.repaired for problem in self.problems)
        found = f"{len(self.problems)} problems" + (f", {fixed} repaired" if fixed else "") if self.problems else "no problems"
        return f"checked {counted}: {found}"


def check_entries(folder: Path, entries: list[dict], report: Report, digest: str | None = None, label: str = "") -> bool:
    """Compare a version's `files/` folder with the entries it was made from (contents, sizes, symlinks, extra
    files) and its manifest digest; True if all is well. Problems are added to `report`."""
    before = len(report.problems)
    wanted = set()
    for entry in entries:
        path = folder / entry["path"]
        wanted.add(entry["path"])
        report.count("files")
        if "link" in entry:
            try:
                target = os.readlink(path)
            except OSError:
                report.add("link", path, "the symlink is missing or is not a symlink")
                continue
            if target != entry["link"]:
                report.add("link", path, f"points to {target!r}, the manifest says {entry['link']!r}")
            continue
        try:
            sha, size = sha256_file(path)
        except OSError as error:
            report.add("file", path, f"cannot be read ({error.strerror or error})")
            continue
        if sha != entry["sha256"] or size != entry["size"]:
            report.add("file", path, f"its contents hash to {sha[:16]}..., the manifest says {entry['sha256'][:16]}..." if sha != entry["sha256"]
                       else f"is {size} bytes, the manifest says {entry['size']}")
    for root, folders, names in os.walk(folder):
        for name in [*folders, *names]:
            relative = (Path(root) / name).relative_to(folder).as_posix()
            if relative not in wanted and not any(item.startswith(relative + "/") for item in wanted):
                report.add("file", Path(root) / name, "is not in the manifest")
    if digest is not None and manifest_digest(entries) != digest:
        report.add("digest", label or folder, f"the manifest's files hash to {manifest_digest(entries)}, not the recorded {digest}")
    return len(report.problems) == before


def _blob_files(blobs: Path):
    """(path, expected sha256 or None) of every file in a blob folder `blobs/<aa>/<name>`; the name is the rest of
    the hash in a store and the whole hash in a cache."""
    try:
        prefixes = sorted(os.scandir(blobs), key=lambda item: item.name)
    except FileNotFoundError:
        return
    for prefix in prefixes:
        if not prefix.is_dir() or len(prefix.name) != 2:
            continue
        for item in sorted(os.scandir(prefix.path), key=lambda item: item.name):
            if ".tmp-" in item.name or not item.is_file(follow_symlinks=False):
                continue
            name = item.name if len(item.name) == 64 else prefix.name + item.name
            yield Path(item.path), name if SHA256.fullmatch(name) else None


def check_blobs(blobs: Path, report: Report, repair: bool = False, keep=()) -> None:
    """Every blob's contents must hash to its name. With `repair`, a broken blob is removed, unless its hash is in
    `keep` (contents a pending version needs: the only copy is never deleted)."""
    for path, sha in _blob_files(blobs):
        report.count("blobs")
        if sha is None:
            report.add("blob", path, "its name is not a SHA-256")
            continue
        try:
            actual, _ = sha256_file(path)
        except OSError as error:
            report.add("blob", path, f"cannot be read ({error.strerror or error})")
            continue
        if actual != sha:
            problem = report.add("blob", path, f"its contents hash to {actual[:16]}..., not {sha[:16]}...")
            if repair and sha not in keep:
                try:
                    path.unlink()
                    problem.repaired = True
                except OSError:
                    pass


def verify_cache(root: Path | str, repair: bool = False, synced: bool | None = None, keep=()) -> Report:
    """A replica root (the pull cache, or the data root with the pending versions): each version folder's files
    against its manifest (and digest), then each blob against its name.

    `synced`: None checks every record; True only synced ones (the cache), False only the others (pending).
    With `repair` (the cache only), a broken version folder and a broken blob are removed, so the next pull
    downloads them again; blobs whose hash is in `keep` are never removed."""
    root, report = Path(root), Report()
    base = root / "versions"
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        record = None if ".tmp-" in folder.name else read_record(folder)
        if record is None:
            continue
        is_synced = record.get("sync") == "synced"
        if synced is not None and is_synced != synced:
            continue
        manifest = record.get("manifest") or {} if is_synced else record
        entries, digest = manifest.get("files") or [], manifest.get("digest")
        if not is_synced and record.get("status") != "completed":
            continue  # a running or failed version has no digest; its files are not final
        report.count("versions")
        label = f"{record.get('project')}:{record.get('name')}@{record['uid']}"
        files = folder / FILES
        if not files.is_dir():
            if is_synced:
                problem = report.add("version", folder, f"{label} has no files folder")
                if repair:
                    remove(folder)
                    problem.repaired = True
            continue  # a pending version's files folder is made on first use
        first = len(report.problems)
        if not check_entries(files, entries, report, digest, label) and repair:
            for problem in report.problems[first:]:
                problem.repaired = True
            remove(folder)
    check_blobs(root / "blobs", report, repair=repair, keep=keep)
    return report


def verify_store(store) -> Report:
    """A LocalStore: every blob matches its name; every version's blobs exist (with the recorded size) and a
    completed version's manifest digest matches its files."""
    report = Report()
    check_blobs(store.root / "blobs", report)
    for project in store.projects():
        for name in store.artifacts(project["name"]):
            for version in store.versions(project["name"], name):
                label = f"{project['name']}:{name}@v{version['version']}"
                report.count("versions")
                for entry in version["files"]:
                    if "sha256" not in entry:
                        continue
                    report.count("files")
                    try:
                        size = os.stat(store.blob_path(entry["sha256"])).st_size
                    except FileNotFoundError:
                        report.add("blob", label, f"{entry['path']}: the blob {entry['sha256'][:16]}... is missing")
                        continue
                    if size != entry["size"]:
                        report.add("blob", label, f"{entry['path']}: the blob is {size} bytes, the manifest says {entry['size']}")
                if version["status"] != "completed":
                    continue
                if not version.get("digest"):
                    report.add("digest", label, "the version has no manifest digest")
                elif manifest_digest(version["files"]) != version["digest"]:
                    report.add("digest", label, f"its files hash to {manifest_digest(version['files'])}, not the recorded {version['digest']}")
    return report
