"""Readers for the run logger's files (JSON Lines): pure, and importing nothing from cairn, so the logger that
writes them and the report builder and store that read them share one definition."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

METRICS = "metrics.jsonl"  # one event per logged scalar
ARTIFACTS = "artifacts.jsonl"  # one event per logged file


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


def fold_scalars(events: Iterable[dict]) -> dict[str, Any]:
    """Latest value per scalar name from metrics.jsonl events (None for non-finite values)."""
    return {event["name"]: event["value"] for event in events}


def fold_artifacts(entries: Iterable[dict]) -> dict[str, dict]:
    """Latest index entry per logged file, keyed by its path in the run."""
    return {entry["path"]: entry for entry in entries}


def read_scalars(run_dir: Path | str) -> dict[str, Any]:
    """Latest logged value of every scalar name (None for non-finite values)."""
    return fold_scalars(read_jsonl(Path(run_dir) / METRICS))


def read_artifacts(run_dir: Path | str) -> dict[str, dict]:
    """Latest index entry for every logged file, keyed by its path in the run."""
    return fold_artifacts(read_jsonl(Path(run_dir) / ARTIFACTS))
