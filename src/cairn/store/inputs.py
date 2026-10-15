"""Recording what a job reads: `pull` appends each exact version to `$CAIRN_INPUTS`, else to
`.cairn-inputs.jsonl` in the working directory, and `publish` carries that file along as the version's
`inputs`. That is how lineage is recorded without any framework hook."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..formats import read_jsonl
from .blobs import now

INPUTS_FILE = ".cairn-inputs.jsonl"
INPUTS_ENV = "CAIRN_INPUTS"


def record_input(exact: str) -> None:
    """Append one exact reference to the inputs file the environment names (else the working directory's)."""
    target = Path(os.environ.get(INPUTS_ENV) or INPUTS_FILE)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ref": exact, "time": now()}) + "\n")


def read_inputs(folder: Path) -> list[str]:
    """The distinct references recorded by every `.cairn-inputs.jsonl` inside a folder (the folder's own first,
    then those of its subfolders in name order, such as one per multirun trial), in order."""
    found: list[str] = []
    for directory, folders, files in os.walk(folder):
        folders.sort()
        if INPUTS_FILE in files:
            for row in read_jsonl(Path(directory) / INPUTS_FILE):
                if row.get("ref") and row["ref"] not in found:
                    found.append(row["ref"])
    return found


def drop_inputs(files: dict) -> None:
    """Remove the inputs records (at any depth) from a scanned file map: they become inputs, never files."""
    for path in [path for path in files if path.rsplit("/", 1)[-1] == INPUTS_FILE]:
        del files[path]


def run_status(folder: Path) -> str | None:
    """The `status` in the folder's `run.json`, or None when there is no readable one."""
    try:
        status = json.loads((Path(folder) / "run.json").read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError, AttributeError):
        return None
    return status if isinstance(status, str) else None
