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

from . import refs
from .blobs import now
from .errors import InvalidRef
from .ids import is_id

INPUTS_FILE = ".cairn-inputs.jsonl"
INPUTS_ENV = "CAIRN_INPUTS"

_SINKS: list[Path] = []  # further inputs files every pull of this process is recorded in


def _append(target: Path, id: str, shown: str | None) -> None:
    row = {"id": id, **({"ref": shown} if shown else {}), "time": now()}
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def record_input(id: str, shown: str | None = None) -> None:
    """Append one version (its id; `shown`, how it was named then, e.g. `data@v3`, only for a reader of the file) to the
    inputs file the environment names (else the working directory's), and to every sink (each file once)."""
    target = Path(os.environ.get(INPUTS_ENV) or INPUTS_FILE)
    done = {os.path.abspath(target)}
    _append(target, id, shown)
    for sink in list(_SINKS):
        if os.path.abspath(sink) not in done:
            done.add(os.path.abspath(sink))
            _append(sink, id, shown)


def _reference(row, where: str) -> str:
    """What a recorded row names: `{"id": <64 hex digits>}` (what Cairn writes); a row of Cairn 0.14 (`uid`) names the
    id `cairn upgrade` gave that version; a row written by hand may name a reference instead (`{"ref": "data@v3"}`).
    Anything else is an InvalidRef naming the file and line (`where`): a row is never dropped silently."""
    if not isinstance(row, dict):
        raise InvalidRef(f'{where}: a row is a JSON object such as {{"id": "<64 hex digits>"}}, not {row!r}')
    if "id" in row:
        if not is_id(row["id"]):
            raise InvalidRef(f"{where}: {row['id']!r} is not a whole id (64 lowercase hex digits)")
        return row["id"]
    if "uid" in row:
        from .upgrade import is_uid, legacy_id

        if not is_uid(row["uid"]):
            raise InvalidRef(f"{where}: {row['uid']!r} is not a Cairn 0.14 uid")
        return legacy_id(row["uid"])
    if "ref" in row:
        if not isinstance(row["ref"], str):
            raise InvalidRef(f"{where}: the reference {row['ref']!r} is not text")
        try:
            refs.parse(row["ref"])
        except InvalidRef as error:
            raise InvalidRef(f"{where}: {error}") from None
        return row["ref"]
    raise InvalidRef(f'{where}: the row names no version (an "id", or a "ref")')


def _rows(path: Path) -> list[str]:
    """The references of one inputs file, in order, each row checked (`_reference`); blank lines are skipped."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as error:
        raise InvalidRef(f"{path}: cannot read the inputs file ({error})") from None
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as error:
            raise InvalidRef(f"{path}:{number}: not a JSON row ({error.msg})") from None
        found.append(_reference(row, f"{path}:{number}"))
    return found


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
    for ref in _rows(path):
        if ref not in found:
            found.append(ref)
    return found


def read_inputs(folder: Path) -> list[str]:
    """The distinct references recorded by every `.cairn-inputs.jsonl` inside a folder (the folder's own first,
    then those of its subfolders in name order, such as one per multirun trial), in order."""
    found: list[str] = []
    for directory, folders, files in os.walk(folder):
        folders.sort()
        if INPUTS_FILE in files:
            for ref in _rows(Path(directory) / INPUTS_FILE):
                if ref not in found:
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
