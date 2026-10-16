"""Version and repo identity: an id is 64 lowercase hex digits, 256 random bits from the operating system's generator,
made where a version (or a repo) is created and never changed, as a commit's hash is in git.

    3f9a2c1e0b...   (64 digits)        a short id is a prefix of it (refs.short_id)

An id carries nothing: no time, no order. Listings, `@latest`, and the order the outbox is sent in come from version
numbers and `created` (store/local.py, client/sync.py), never from the digits.

Tests choose ids with `chosen(...)`: inside the block the next `new_id()` calls return them, in order.
"""

from __future__ import annotations

import re
import secrets
import threading
from contextlib import contextmanager

ID = re.compile(r"[0-9a-f]{64}")

_lock = threading.Lock()
_queue: list[str] = []  # ids `new_id` hands out before random ones (`chosen`, a test hook)


def new_id() -> str:
    """A fresh id: 64 lowercase hex digits (`secrets.token_hex(32)`)."""
    with _lock:
        if _queue:
            return _queue.pop(0)
    return secrets.token_hex(32)


def is_id(value) -> bool:
    """Whether `value` is a whole id (64 lowercase hex digits)."""
    return isinstance(value, str) and ID.fullmatch(value) is not None


def valid_id(value) -> str:
    """`value` if it is a whole id, else ValueError."""
    if not is_id(value):
        raise ValueError(f"invalid id {value!r}: expected 64 lowercase hex digits")
    return value


@contextmanager
def chosen(*ids: str):
    """A test hook: inside the block, `new_id()` returns these ids first (each checked), then random ones."""
    for value in ids:
        valid_id(value)
    with _lock:
        _queue[:0] = list(ids)
    try:
        yield
    finally:
        with _lock:
            for value in ids:
                if value in _queue:
                    _queue.remove(value)
