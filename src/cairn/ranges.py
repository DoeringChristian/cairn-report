"""HTTP byte ranges (RFC 9110), so browsers can seek in videos served by `serve` and the store server."""

from __future__ import annotations

import re

RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def byte_range(header: str | None, size: int) -> tuple[int, int] | None | bool:
    """The (first, last) byte a single-range request asks for, None to send the whole file, or
    False when the range cannot be satisfied (answer 416). Multiple ranges are answered whole."""
    match = RANGE.fullmatch((header or "").strip())
    if not match or match.group(1) == match.group(2) == "":
        return None
    first, last = match.groups()
    if first == "":  # the last N bytes
        count = int(last)
        if count == 0:
            return False
        return max(size - count, 0), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        return False
    return start, end
