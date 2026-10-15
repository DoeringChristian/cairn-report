"""Migrate an old hub root (`cairn-report` before 0.13) into a store; the old root is only read.

Old layout: `<root>/blobs/<aa>/<rest>`, `projects/<p>/report.jsonl` (one line per report version, paths
`source/...` and `site/...`) and `runs.jsonl` (append-only file states `<run>/<timestamp>/<file>`).

- every old project becomes a project (title = its name);
- every `<run>/<timestamp>` becomes a version of the artifact `<run>`, labelled by the timestamp, in
  timestamp order; `completed` when its `run.json` says so, else `failed`;
- the latest old report version is split into one artifact `reports/<slug>` per report folder.

Everything migrated carries `migrated_from` in its metadata, which is what makes a second run a no-op.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import yaml

from ..formats import read_jsonl
from . import refs
from .blobs import clean_entries
from .local import LocalStore

FRONT = re.compile(r"---\n(.*?)\n---", re.S)


class _Project:
    def __init__(self, name: str):
        self.name = name
        self.runs_migrated = self.runs_existing = 0
        self.reports_migrated = self.reports_existing = 0
        self.skipped: list[str] = []
        self.warnings: list[str] = []

    def lines(self) -> list[str]:
        out = [f"project {self.name}: {self.runs_migrated} runs migrated ({self.runs_existing} already there), "
               f"{self.reports_migrated} reports migrated ({self.reports_existing} already there)"]
        out += [f"  skipped: {text}" for text in self.skipped]
        out += [f"  warning: {text}" for text in self.warnings]
        return out


def _copy_blobs(old: Path, store: LocalStore, entries: list[dict]) -> list[str]:
    """Hard-link (else copy) the entries' blobs into the store; the missing ones are returned."""
    missing = []
    for entry in entries:
        sha = entry.get("sha256")
        if sha is None or store.has_blob(sha):
            continue
        source = old / "blobs" / sha[:2] / sha[2:]
        if not source.is_file():
            missing.append(sha)
            continue
        target = store.blob_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
    return missing


def _status(old: Path, entries: list[dict]) -> str:
    for entry in entries:
        if entry["path"] == "run.json" and "sha256" in entry:
            try:
                data = json.loads((old / "blobs" / entry["sha256"][:2] / entry["sha256"][2:]).read_text())
            except (OSError, ValueError):
                return "failed"
            return "completed" if isinstance(data, dict) and data.get("status") == "completed" else "failed"
    return "failed"


def _runs(old: Path, store: LocalStore, project: str, report: _Project) -> None:
    latest: dict[str, dict] = {}
    for row in read_jsonl(old / "projects" / project / "runs.jsonl"):
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            latest[row["path"]] = row
    groups: dict[tuple[str, str], list[dict]] = {}
    for path, row in latest.items():
        parts = path.split("/")
        if len(parts) < 3:  # `<run>/latest` links and stray files
            continue
        entry = {key: row[key] for key in ("sha256", "size", "mtime", "link") if key in row}
        groups.setdefault((parts[0], parts[1]), []).append({"path": "/".join(parts[2:]), **entry})
    done: dict[str, set] = {}
    for (name, stamp) in sorted(groups):
        origin = f"{project}/{name}/{stamp}"
        try:
            refs.valid_name(name)
            refs.valid_word(stamp, "label")
            entries = clean_entries(groups[name, stamp])
        except ValueError as error:
            report.skipped.append(f"run {origin}: {error}")
            continue
        if name not in done:
            done[name] = {v["metadata"].get("migrated_from") for v in store.versions(project, name)}
        if origin in done[name]:
            report.runs_existing += 1
            continue
        missing = _copy_blobs(old, store, entries)
        if missing:
            report.skipped.append(f"run {origin}: {len(missing)} blobs missing in the old root")
            continue
        number = store.start_version(project, name, label=stamp, metadata={"migrated_from": origin})
        store.add_files(project, name, number, entries)
        if _status(old, groups[name, stamp]) == "completed":
            store.complete_version(project, name, number)
        else:
            store.fail_version(project, name, number)
        report.runs_migrated += 1


def _title(old: Path, entry: dict, fallback: str) -> str:
    try:
        text = (old / "blobs" / entry["sha256"][:2] / entry["sha256"][2:]).read_text()
        match = FRONT.match(text)
        data = yaml.safe_load(match.group(1)) if match else None
        if isinstance(data, dict) and data.get("title"):
            return str(data["title"])
    except (OSError, ValueError, yaml.YAMLError):
        pass
    return fallback


def _reports(old: Path, store: LocalStore, project: str, report: _Project) -> None:
    versions = [row for row in read_jsonl(old / "projects" / project / "report.jsonl") if isinstance(row, dict) and "entries" in row]
    if not versions:
        return
    newest = max(versions, key=lambda row: row.get("version", 0))
    number = newest.get("version", len(versions))
    if len(versions) > 1:
        report.skipped.append(f"{len(versions) - 1} older report versions (only the latest, v{number}, is migrated)")
    entries = [entry for entry in newest["entries"] if isinstance(entry, dict) and isinstance(entry.get("path"), str)]
    folders = sorted(entry["path"][len("source/"):-len("report.md")].rstrip("/") for entry in entries
                     if entry["path"] == "source/report.md" or (entry["path"].startswith("source/") and entry["path"].endswith("/report.md")))
    taken: dict[str, str] = {}
    for folder in folders:
        slug = "overview" if not folder else folder.replace("/", "-")
        if slug in taken:
            report.skipped.append(f"report {folder or '.'}: slug {slug!r} is already taken by {taken[slug] or '.'}")
            continue
        taken[slug] = folder
        children = [other for other in folders if other != folder and (not folder or other.startswith(folder + "/"))]
        chosen = _report_files(entries, folder, slug, children)
        key = f"source/{folder + '/' if folder else ''}report.md"
        source_entry = next((e for e in chosen if e["path"] == "source/report.md" and "sha256" in e), None)
        if source_entry is None:
            report.skipped.append(f"report {key}: no report.md content")
            continue
        if not any(e["path"].startswith(f"site/{slug}/") for e in chosen):
            report.warnings.append(f"report {slug}: no built page under site/ in the old version (the report page will be empty)")
        origin = f"{project}@{number} {key}"
        name = f"reports/{slug}"
        try:
            refs.valid_name(name)
            clean = clean_entries(chosen)
        except ValueError as error:
            report.skipped.append(f"report {key}: {error}")
            continue
        if any(v["metadata"].get("migrated_from") == origin for v in store.versions(project, name)):
            report.reports_existing += 1
            continue
        missing = _copy_blobs(old, store, clean)
        if missing:
            report.skipped.append(f"report {key}: {len(missing)} blobs missing in the old root")
            continue
        metadata = {"title": _title(old, source_entry, folder.rsplit("/", 1)[-1] or "overview"), "migrated_from": origin}
        store.publish(project, name, clean, label=f"migrated-v{number}", metadata=metadata)
        report.reports_migrated += 1


def _report_files(entries: list[dict], folder: str, slug: str, children: list[str]) -> list[dict]:
    """The entries of one report folder, renamed into the layout of a published report."""
    prefix = folder + "/" if folder else ""
    chosen = []
    for entry in entries:
        path, rest = entry["path"], None
        body = {key: value for key, value in entry.items() if key != "path"}
        if path.startswith("source/" + prefix):
            rest = path[len("source/" + prefix):]
            if rest == "report.md" or rest.startswith("assets/"):
                chosen.append({"path": "source/" + rest, **body})
        elif path.startswith("site/_static/"):
            chosen.append({"path": path, **body})
        elif path.startswith("site/" + prefix):
            rest = path[len("site/" + prefix):]
            if not any(rest.startswith(child[len(prefix):] + "/") for child in children):
                chosen.append({"path": f"site/{slug}/{rest}", **body})
    return chosen


def migrate(old_root: Path | str, new_root: Path | str) -> list[str]:
    """Migrate; returns the summary lines. `new_root` may be an existing store."""
    old, new = Path(old_root).resolve(), Path(new_root).resolve()
    if not (old / "projects").is_dir() or not (old / "blobs").is_dir():
        raise SystemExit(f"{old} is not a hub root (no blobs/ and projects/)")
    if old == new:
        raise SystemExit("the old and new roots must differ")
    store = LocalStore(new)
    lines: list[str] = []
    for folder in sorted((old / "projects").iterdir()):
        if not folder.is_dir():
            continue
        report = _Project(folder.name)
        try:
            refs.valid_project(folder.name)
        except ValueError as error:
            report.skipped.append(f"project: {error}")
            lines += report.lines()
            continue
        store.create_project(folder.name, title=folder.name)
        _runs(old, store, folder.name, report)
        _reports(old, store, folder.name, report)
        lines += report.lines()
    lines.append("Reports that read runs with `run:` in the old sources must be rewritten by hand to declare `inputs:` "
                 "(migrated reports are stored as they were, not rebuilt).")
    return lines
