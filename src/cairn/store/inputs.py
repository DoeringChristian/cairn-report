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
    """The distinct references a folder's `.cairn-inputs.jsonl` recorded, in order."""
    found: list[str] = []
    for row in read_jsonl(folder / INPUTS_FILE):
        if row.get("ref") and row["ref"] not in found:
            found.append(row["ref"])
    return found
