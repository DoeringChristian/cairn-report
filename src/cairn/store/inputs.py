"""Recording what a job reads: `pull` appends each exact version to `$CAIRN_INPUTS`, else to
`.cairn-inputs.jsonl` in the working directory, and `publish` carries that file along as the version's
`inputs`. That is how lineage is recorded without any framework hook.

A Cairn run (cairn/client/runs.py) points `$CAIRN_INPUTS` at its own folder's inputs file, so what the process
and its plain subprocesses pull are the run's inputs. When the process inherited `$CAIRN_INPUTS` from a parent
that is not a Cairn run (`cairn publish --watch -- command`), the run keeps that file as an extra sink: every pull
is recorded there too."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..formats import read_jsonl
from . import refs
from .blobs import now
from .uids import UID

INPUTS_FILE = ".cairn-inputs.jsonl"
INPUTS_ENV = "CAIRN_INPUTS"

_SINKS: list[Path] = []  # further inputs files every pull of this process is recorded in


def _append(target: Path, exact: str, uid: str | None) -> None:
    row = {"ref": exact, **({"uid": uid} if uid else {}), "time": now()}
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def record_input(exact: str, uid: str | None = None) -> None:
    """Append one exact reference (and the version's uid) to the inputs file the environment names (else the working
    directory's), and to every sink (each file once)."""
    target = Path(os.environ.get(INPUTS_ENV) or INPUTS_FILE)
    done = {os.path.abspath(target)}
    _append(target, exact, uid)
    for sink in list(_SINKS):
        if os.path.abspath(sink) not in done:
            done.add(os.path.abspath(sink))
            _append(sink, exact, uid)


def _reference(row: dict) -> str | None:
    """What a recorded row names: the version by its uid when the row has one. A number this repo gave its own version
    changes when a remote added later numbers it (client/sync.py adopts it); its uid never does."""
    ref, uid = row.get("ref"), row.get("uid")
    if not ref or not isinstance(uid, str) or not UID.fullmatch(uid):
        return ref or None
    try:
        parsed = refs.parse(ref)
    except ValueError:
        return ref
    return str(refs.Ref(parsed.name, uid, parsed.path))


def open_sink(path: Path) -> None:
    """Record every pull in `path` too, from now on."""
    _SINKS.append(Path(path))


def close_sink(path: Path) -> None:
    """Stop recording into `path`."""
    for index, sink in enumerate(_SINKS):
        if sink == Path(path):
            del _SINKS[index]
            break


def recorded(path: Path) -> list[str]:
    """The distinct references one inputs file holds, in order."""
    found: list[str] = []
    for row in read_jsonl(path):
        ref = _reference(row)
        if ref and ref not in found:
            found.append(ref)
    return found


def read_inputs(folder: Path) -> list[str]:
    """The distinct references recorded by every `.cairn-inputs.jsonl` inside a folder (the folder's own first,
    then those of its subfolders in name order, such as one per multirun trial), in order."""
    found: list[str] = []
    for directory, folders, files in os.walk(folder):
        folders.sort()
        if INPUTS_FILE in files:
            for row in read_jsonl(Path(directory) / INPUTS_FILE):
                ref = _reference(row)
                if ref and ref not in found:
                    found.append(ref)
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
