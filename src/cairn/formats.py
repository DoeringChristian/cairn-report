"""Readers for the run logger's files (JSON Lines): pure, and importing nothing from cairn, so the logger that
writes them and the report builder and store that read them share one definition.

`metrics.jsonl` holds one entry per `add({...})`: the dict as given (any keys; `step` is just a key), plus
`time`, `origin`, and, when a value was not finite, `nonfinite` ({key: "nan"}; the value itself is null).
Lines written by cairn-report 0.11 hold one scalar each, `{name, value, step, time, origin[, nonfinite]}`; the readers
take both."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

METRICS = "metrics.jsonl"  # one entry per add() (cairn-report 0.11 wrote one event per logged scalar)
ARTIFACTS = "artifacts.jsonl"  # one event per logged file
RESERVED = ("time", "origin", "nonfinite")  # added by the logger, never a logged value
OLD_KEYS = {"name", "value", "step", "time", "origin", "nonfinite"}


def read_jsonl(path: Path | str) -> list[dict]:
    """All complete records of a JSON Lines file; a torn final line is skipped."""
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def is_old_event(event: dict) -> bool:
    """A line of cairn-report 0.11: one scalar as {name, value, step, ...} and nothing else. (An entry logged
    with exactly the keys `name` and `value`, and nothing but `step` besides, reads the same way.)"""
    return isinstance(event.get("name"), str) and "value" in event and set(event) <= OLD_KEYS


def entry_values(event: dict) -> dict[str, Any]:
    """The logged values of one metrics.jsonl line, as {key: value} (an old line gives {name: value, step: n})."""
    if is_old_event(event):
        found = {event["name"]: event["value"]}
        if event.get("step") is not None:
            found["step"] = event["step"]
        return found
    return {key: value for key, value in event.items() if key not in RESERVED}


def fold_scalars(events: Iterable[dict]) -> dict[str, Any]:
    """Latest value per key over metrics.jsonl entries (None for non-finite values). `step` is not a value."""
    latest: dict[str, Any] = {}
    for event in events:
        for key, value in entry_values(event).items():
            if key != "step":
                latest[key] = value
    return latest


def fold_series(events: Iterable[dict], key: str) -> list[dict]:
    """Every logged value of one key, in order, as {"value", "step", "time"} (step None when the entry had none)."""
    found = []
    for event in events:
        values = entry_values(event)
        if key in values:
            found.append({"value": values[key], "step": values.get("step"), "time": event.get("time")})
    return found


def fold_artifacts(entries: Iterable[dict]) -> dict[str, dict]:
    """Latest index entry per logged file, keyed by its path in the run."""
    return {entry["path"]: entry for entry in entries}


def read_scalars(run_dir: Path | str) -> dict[str, Any]:
    """Latest logged value of every key (None for non-finite values)."""
    return fold_scalars(read_jsonl(Path(run_dir) / METRICS))


def read_series(run_dir: Path | str, key: str) -> list[dict]:
    """Every logged value of `key`, as {"value", "step", "time"}, oldest first."""
    return fold_series(read_jsonl(Path(run_dir) / METRICS), key)


def read_artifacts(run_dir: Path | str) -> dict[str, dict]:
    """Latest index entry for every logged file, keyed by its path in the run."""
    return fold_artifacts(read_jsonl(Path(run_dir) / ARTIFACTS))
