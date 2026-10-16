"""`cairn publish reports/<slug>`: build a report and store it as the artifact `reports/<slug>`."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from ..client.http import entries_for, scan
from ..client.open import open_repo
from ..store.hashes import HashCache
from ..client.publishing import publish_version, published
from ..config import Config
from ..store import StoreError
from ..term import plural


def is_report_folder(config: Config, folder: Path) -> bool:
    """`reports/<slug>` with a report.md: a folder that publishes as a report."""
    folder = folder.resolve()
    return (folder / "report.md").is_file() and folder.parent == config.reports_dir.resolve()


def publish_report(config: Config, folder: Path, *, quiet: bool = False) -> int | None:
    """Build a report and store it as the next version of `reports/<slug>`.

    The version holds `source/` (the report folder), `site/<slug>/` (its built page) and `site/_static/`
    (the shared page files it needs), so it can be viewed on its own. Its inputs are the exact versions
    the report read. A report whose source, inputs, and metadata match the latest version creates none
    (the built pages carry build times, so the source is what is compared). Returns the version number
    (the existing one when unchanged), or None while it is pending (the remote did not answer).
    """
    say = (lambda *_args, **_kwargs: None) if quiet else print
    from . import build as builder

    folder = folder.resolve()
    slug = folder.name
    build = next(item for item in builder.build_all(config) if item.key == slug)  # all: links are checked against the other pages
    data = json.loads((build.generated / "report-data.json").read_text())
    metadata = {field: data[field] for field in ("title", "status", "summary")}
    inputs = list(dict.fromkeys(item["resolved"] for item in build.inputs.values()))
    name = f"reports/{slug}"
    store = open_repo(config)
    store.quiet_pending = True  # the line it prints says why a version is pending
    with tempfile.TemporaryDirectory(prefix="cairn-") as temporary:
        stage = Path(temporary) / slug
        shutil.copytree(folder, stage / "source", symlinks=True)
        shutil.copytree(build.generated, stage / "site" / slug, symlinks=True)
        static = stage / "site" / builder.STATIC
        static.mkdir()
        for hashed in builder.STATIC_NAMES.values():
            for file in (hashed, f"{hashed}.gz"):
                shutil.copy2(builder.OUTPUT_ROOT / builder.STATIC / file, static / file)
        before = max((v["version"] or 0 for v in store.versions(name)), default=0) if _exists(store, name) else 0
        latest = _same_source(store, name, stage / "source", config, inputs, metadata) if before else None
        if latest is not None:
            say(published(name, latest, before), flush=True)
            return latest["version"]
        version = publish_version(store, name, stage, metadata=metadata, inputs=inputs, label=time.strftime("%Y-%m-%d_%H-%M-%S"),
                                  config=config)
    line = published(name, version.manifest, before, getattr(store, "pending_reasons", {}).get(version.manifest.get("uid")))
    new = version.number is not None and version.number > before
    say(line[:-1] + f"; {plural(len(inputs), 'input')})" if new and line.endswith(")") else line, flush=True)
    return version.number


def _exists(store, name: str) -> bool:
    try:
        return bool(store.versions(name))
    except StoreError:
        return False


def _same_source(store, name: str, source: Path, config: Config, inputs: list[str], metadata: dict) -> dict | None:
    """The latest completed version when it has this source folder, these inputs, and this metadata, else None."""
    try:
        _, latest = store.resolve(name)
    except StoreError:
        return None
    files = scan(source, "", config.project, config.repo.get("exclude", []))
    entries, _ = entries_for(files, HashCache.of(store.root, config.project))

    def key(entry: dict, prefix: str = ""):
        return (prefix + entry["path"], entry.get("sha256"), entry.get("link"))

    old = sorted(key(entry) for entry in latest["files"] if entry["path"].startswith("source/"))
    same = (old == sorted(key(entry, "source/") for entry in entries) and sorted(latest["inputs"]) == sorted(inputs)
            and latest["metadata"] == metadata)
    return latest if same else None
