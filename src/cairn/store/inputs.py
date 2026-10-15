"""Recording what a job reads: `pull` appends each exact version to `$CAIRN_INPUTS`, else to
`.cairn-inputs.jsonl` in the working directory, and `publish` carries that file along as the version's
`inputs`. That is how lineage is recorded without any framework hook.

Inside one process, open drafts (cairn/client/drafts.py) add their own inputs files as sinks: a pull is recorded
in every open draft, not only the innermost one `$CAIRN_INPUTS` names. A pull made while no draft is open is
also kept as a loose input, which every draft opened before the next one closes carries along (so
`x = cairn.pull(...)` right before `with cairn.draft(...)` is an input of that draft)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..formats import read_jsonl
from .blobs import now

INPUTS_FILE = ".cairn-inputs.jsonl"
INPUTS_ENV = "CAIRN_INPUTS"

_SINKS: list[Path] = []  # the inputs files of the drafts open in this process
_LOOSE: list[str] = []  # pulls made while no draft was open, carried into the drafts opened next


def _append(target: Path, exact: str) -> None:
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ref": exact, "time": now()}) + "\n")


def record_input(exact: str) -> None:
    """Append one exact reference to the inputs file the environment names (else the working directory's), and to
    every open draft's (each file once); with no draft open, also remember it as a loose input."""
    target = Path(os.environ.get(INPUTS_ENV) or INPUTS_FILE)
    done = {os.path.abspath(target)}
    _append(target, exact)
    for sink in list(_SINKS):
        if os.path.abspath(sink) not in done:
            done.add(os.path.abspath(sink))
            _append(sink, exact)
    if not _SINKS and exact not in _LOOSE:
        _LOOSE.append(exact)


def open_sink(path: Path) -> None:
    """A draft opened: record pulls in `path` from now on, starting with the loose ones made before."""
    for exact in _LOOSE:
        _append(path, exact)
    _SINKS.append(Path(path))


def close_sink(path: Path) -> None:
    """A draft closed: stop recording into `path`; the loose inputs have been handed out."""
    for index, sink in enumerate(_SINKS):
        if sink == Path(path):
            del _SINKS[index]
            break
    _LOOSE.clear()


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
