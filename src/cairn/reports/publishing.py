"""`cairn publish reports/<slug>`: build a report and store it as the artifact `reports/<slug>`."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from ..client.http import HashCache, entries_for, scan
from ..client.open import open_store, versions_of
from ..client.publishing import describe, publish
from ..config import Config
from ..store import StoreError, refs


def is_report_folder(config: Config, folder: Path) -> bool:
    """`reports/<slug>` with a report.md: a folder that publishes as a report."""
    folder = folder.resolve()
    return (folder / "report.md").is_file() and folder.parent == config.reports_dir.resolve()


def publish_report(config: Config, folder: Path) -> None:
    """Build a report and store it as the next version of `reports/<slug>`.

    The version holds `source/` (the report folder), `site/<slug>/` (its built page) and `site/_static/`
    (the shared page files it needs), so it can be viewed on its own. Its inputs are the exact versions
    the report read. A report whose source, inputs, and metadata match the latest version creates none
    (the built pages carry build times, so the source is what is compared).
    """
    from . import build as builder

    folder = folder.resolve()
    slug = folder.name
    build = next(item for item in builder.build_all(config) if item.key == slug)  # all: links are checked against the other pages
    data = json.loads((build.generated / "report-data.json").read_text())
    metadata = {field: data[field] for field in ("title", "status", "summary")}
    inputs = list(dict.fromkeys(item["resolved"] for item in build.inputs.values()))
    name = f"reports/{slug}"
    store = open_store(config)
    with tempfile.TemporaryDirectory(prefix="cairn-") as temporary:
        stage = Path(temporary) / slug
        shutil.copytree(folder, stage / "source", symlinks=True)
        shutil.copytree(build.generated, stage / "site" / slug, symlinks=True)
        static = stage / "site" / builder.STATIC
        static.mkdir()
        for hashed in builder.STATIC_NAMES.values():
            for file in (hashed, f"{hashed}.gz"):
                shutil.copy2(builder.OUTPUT_ROOT / builder.STATIC / file, static / file)
        before = max((v["version"] for v in versions_of(store, name)), default=0) if _exists(store, name) else 0
        if before and _same_source(store, name, stage / "source", config, inputs, metadata):
            print(f"{name}@v{before} unchanged", flush=True)
            return
        number = publish(name, stage, metadata=metadata, inputs=inputs, label=time.strftime("%Y-%m-%d_%H-%M-%S"),
                                   store=store, config=config)
    if number is None:
        return print(f"queued {name} in the outbox (the store does not answer); `cairn status` lists it, `cairn sync` sends it", flush=True)
    state = store.resolve(refs.exact(store.project, name, number))[2]
    print(f"published {name}@v{number} ({describe(state)}; inputs: {', '.join(inputs) or 'none'})", flush=True)


def _exists(store, name: str) -> bool:
    try:
        return bool(versions_of(store, name))
    except StoreError:
        return False


def _same_source(store, name: str, source: Path, config: Config, inputs: list[str], metadata: dict) -> bool:
    """Whether the latest completed version has this source folder, these inputs, and this metadata."""
    try:
        _, _, latest = store.resolve(name)
    except StoreError:
        return False
    files = scan(source, "", config, exclude=config.store.get("exclude", []))
    entries, _ = entries_for(files, HashCache(config.project))

    def key(entry: dict, prefix: str = ""):
        return (prefix + entry["path"], entry.get("sha256"), entry.get("link"))

    old = sorted(key(entry) for entry in latest["files"] if entry["path"].startswith("source/"))
    return (old == sorted(key(entry, "source/") for entry in entries) and sorted(latest["inputs"]) == sorted(inputs)
            and latest["metadata"] == metadata)
