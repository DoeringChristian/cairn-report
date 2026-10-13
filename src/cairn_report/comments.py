"""Comment logs: append-only JSON Lines, folded to the latest record per comment id.

Shared by the project server and the hub, so both accept the same requests and
write the same records.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .logger import read_jsonl

CLOSED = ("resolve", "delete")
MAX_PAYLOAD = 16_384


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fold(records: Iterable[dict]) -> list[dict]:
    """The latest record per comment id, in first-seen order."""
    latest, order = {}, []
    for row in records:
        if not isinstance(row, dict) or "id" not in row:
            continue
        if row["id"] not in latest:
            order.append(row["id"])
        latest[row["id"]] = row
    return [latest[key] for key in order]


def canonical(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def merge(*logs: Iterable[dict]) -> list[dict]:
    """Several logs of the same report as one: exact duplicates dropped, ordered by timestamp."""
    seen, rows = set(), []
    for log in logs:
        for row in log:
            key = canonical(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("timestamp", "")))  # stable for equal times


def append(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def read_log(path: Path) -> list[dict]:
    return read_jsonl(path)


def apply(records: list[dict], action: str | None, payload: dict) -> tuple[list[dict], int, dict]:
    """Validate one comment request against the current log.

    Returns the rows to append, the HTTP status, and the response body. Raises
    ValueError for an invalid request.
    """
    current = fold(records)
    if action == "clear":
        rows = [
            {"id": row["id"], "timestamp": now(), "event": "resolve", "resolution": "Cleared from the report"}
            for row in current
            if row.get("event") not in CLOSED
        ]
        return rows, 200, {"cleared": len(rows)}
    if action == "delete":
        key = str(payload.get("id", "")).strip()
        if not any(row["id"] == key and row.get("event") not in CLOSED for row in current):
            raise ValueError("unknown comment ID")
        return [{"id": key, "timestamp": now(), "event": "delete"}], 200, {"deleted": key}
    text = str(payload.get("text", "")).strip()
    target = str(payload.get("target", "")).strip()
    if not text or len(text) > 4_000 or not target or len(target) > 256:
        raise ValueError("invalid comment")
    key = str(payload.get("id", "")).strip() or str(uuid.uuid4())
    if len(key) > 128:
        raise ValueError("invalid comment ID")
    row = {
        "id": key,
        "timestamp": now(),
        "event": "upsert",
        "target": target,
        "line": payload.get("line"),
        "quote": str(payload.get("quote", ""))[:1_000],
        "text": text,
    }
    return [row], 201, row
