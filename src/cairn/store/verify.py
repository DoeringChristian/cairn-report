"""Integrity checks: re-hash what a repo holds and say what is wrong.

    Report                              the problems found (kind, path, message) and how many things were checked
    sha256_file(path)                   (hex digest, size) of a file
    check_entries(folder, entries, ...) a read-only `files/` folder against the manifest entries it was made from
    check_blobs(blobs, report, ...)     every blob against its name
    verify_folders(root, repair)        every read-only folder `pull` made in a repo (`versions/<id>/`)
    verify_store(store)                 a LocalRepo's database: every version has an id, its blobs exist (with their
                                        size), its manifest digest matches its files, and its inputs are versions of its
                                        project

Nothing here changes anything except `repair=True`, which removes what can be made again: a broken read-only folder
(the next pull makes it again) and a broken blob whose hash is not in `keep` (the caller keeps everything that may be
the only copy: a repo's own versions, the outbox).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .blobs import SHA256
from .folders import FILES, read_record, remove
from .digest import manifest_digest, sha256_file


@dataclass
class Problem:
    kind: str  # blob | file | link | digest | version
    path: str  # the file or folder concerned (or `name@vN` for a repo's version)
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
        found = (
            f"{len(self.problems)} problems" + (f", {fixed} repaired" if fixed else "")
            if self.problems
            else "no problems"
        )
        return f"checked {counted}: {found}"


def check_entries(
    folder: Path, entries: list[dict], report: Report, digest: str | None = None, label: str = ""
) -> bool:
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
            report.add(
                "file",
                path,
                (
                    f"its contents hash to {sha[:16]}..., the manifest says {entry['sha256'][:16]}..."
                    if sha != entry["sha256"]
                    else f"is {size} bytes, the manifest says {entry['size']}"
                ),
            )
    for root, folders, names in os.walk(folder):
        for name in [*folders, *names]:
            relative = (Path(root) / name).relative_to(folder).as_posix()
            if relative not in wanted and not any(item.startswith(relative + "/") for item in wanted):
                report.add("file", Path(root) / name, "is not in the manifest")
    if digest is not None and manifest_digest(entries) != digest:
        report.add(
            "digest",
            label or folder,
            f"the manifest's files hash to {manifest_digest(entries)}, not the recorded {digest}",
        )
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


def verify_folders(root: Path | str, repair: bool = False) -> Report:
    """Every read-only folder `pull` made in the repo `root` (`versions/<id>/files/`) against the manifest it was made
    from (and its digest). With `repair`, a broken folder is removed: the next pull makes it again from the blobs."""
    root, report = Path(root), Report()
    base = root / "versions"
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        record = None if ".tmp-" in folder.name else read_record(folder)
        if record is None:
            continue
        manifest = record.get("manifest") or {}
        entries, digest = manifest.get("files") or [], manifest.get("digest")
        report.count("versions")
        label = f"{record.get('name')} ({record['id'][:7]})"
        if record["id"] != folder.name or manifest.get("id") not in (None, record["id"]):
            report.add("version", folder, f"{label}: the folder's record names another id")
        files = folder / FILES
        if not files.is_dir():
            problem = report.add("version", folder, f"{label} has no files folder")
            if repair:
                remove(folder)
                problem.repaired = True
            continue
        first = len(report.problems)
        if not check_entries(files, entries, report, digest, label) and repair:
            for problem in report.problems[first:]:
                problem.repaired = True
            remove(folder)
    return report


def verify_store(store) -> Report:
    """A LocalRepo (every project of it): every blob matches its name; every version has an id; its blobs exist (with
    the recorded size); a completed version's manifest digest matches its files; and the inputs of a version the repo
    made (not a copy of a remote's, whose inputs need not be copied) are ids of versions of its project."""
    from .ids import is_id

    report = Report()
    check_blobs(store.root / "blobs", report)
    for project in store.projects():
        listed = [
            (name, version)
            for name in store.artifacts(project["name"])
            for version in store.versions(project["name"], name)
        ]
        known = store.version_ids(project["name"])  # tombstones too: a deleted input keeps its edge
        bound = bool(store.meta("remote"))  # a remote's cache: an input may be a version it never copied
        for name, version in listed:
            label = f"{name}@v{version['version']}" + (f" (project {project['name']})" if project["name"] else "")
            report.count("versions")
            if not is_id(version.get("id")):
                report.add("version", label, f"its id {version.get('id')!r} is not 64 hex digits")
            for input_id in version.get("inputs") or []:
                if not is_id(input_id):
                    report.add("lineage", label, f"the input {input_id!r} is not an id")
                elif input_id not in known and not version.get("origin") and not bound:
                    report.add("lineage", label, f"the input {input_id[:12]}... is no version of the project")
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
                    report.add(
                        "blob",
                        label,
                        f"{entry['path']}: the blob is {size} bytes, the manifest says {entry['size']}",
                    )
            if version["status"] != "completed":
                continue
            if not version.get("digest"):
                report.add("digest", label, "the version has no manifest digest")
            elif manifest_digest(version["files"]) != version["digest"]:
                report.add(
                    "digest",
                    label,
                    f"its files hash to {manifest_digest(version['files'])}, not the recorded {version['digest']}",
                )
    return report


def verify_outbox(outbox, root: Path) -> Report:
    """Every outbox entry: a completed one's digest against its files, and every blob it needs is in the repo `root`."""
    from .blobs import blob_path

    report = Report()
    for record in outbox.records():
        label = f"{record['name']} ({record['id'][:7]})"
        report.count("versions")
        entries = record.get("files") or []
        if (
            record.get("status") == "completed"
            and record.get("digest")
            and manifest_digest(entries) != record["digest"]
        ):
            report.add(
                "digest",
                label,
                f"the entry's files hash to {manifest_digest(entries)}, not the recorded {record['digest']}",
            )
        for entry in entries:
            if "sha256" in entry and not blob_path(root, entry["sha256"]).is_file():
                report.add(
                    "blob",
                    label,
                    f"{entry['path']}: the blob {entry['sha256'][:16]}... is missing from the repo: this "
                    "pending version cannot be sent",
                )
    return report


def verify_copies(copies: list[tuple[str, dict]]) -> Report:
    """Copies of a remote's versions, (name, record): a completed one's digest against its file list (their blobs are
    fetched when needed)."""
    report = Report()
    for name, state in copies:
        report.count("versions")
        if (
            state["status"] == "completed"
            and state.get("digest")
            and manifest_digest(state["files"]) != state["digest"]
        ):
            report.add(
                "digest",
                f"{name}@v{state['version']}",
                f"its files hash to {manifest_digest(state['files'])}, not the recorded {state['digest']}",
            )
    return report


def verify_version(root: Path | None, label: str, version: dict, own: bool, repair: bool = False) -> Report:
    """One version: the digest of its manifest, its read-only folder in the repo `root`, the blobs the repo holds for
    it. `own`: the repo's own version (a missing blob is a problem, a broken one is never removed); else a copy of a
    remote's (`repair` removes a broken blob: the next pull downloads it again)."""
    from .blobs import blob_path
    from .folders import files_folder

    report = Report()
    report.count("versions")
    entries, digest = version["files"], version.get("digest")
    if digest is not None and manifest_digest(entries) != digest:
        report.add(
            "digest", label, f"the manifest's files hash to {manifest_digest(entries)}, not the recorded {digest}"
        )
    elif digest is None and version.get("status") == "completed":
        report.add("digest", label, "the version has no manifest digest")
    if root is None:
        return report
    folder = files_folder(root, version["id"])
    if folder.is_dir():
        check_entries(folder, entries, report, None, label)
    for entry in entries:
        if "sha256" not in entry:
            continue
        blob = blob_path(root, entry["sha256"])
        if not blob.is_file():
            if own:
                report.add(
                    "blob", label, f"{entry['path']}: the blob {entry['sha256'][:16]}... is missing from the repo"
                )
            continue
        report.count("blobs")
        sha, _ = sha256_file(blob)
        if sha != entry["sha256"]:
            problem = report.add("blob", blob, f"its contents hash to {sha[:16]}..., not {entry['sha256'][:16]}...")
            if repair and not own:
                blob.unlink(missing_ok=True)
                problem.repaired = True
    return report
