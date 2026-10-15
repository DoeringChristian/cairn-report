"""Version and store identity: UUIDv7 (RFC 9562), time-ordered, made where a version is created.

    0192f3a4-5b6c-7d8e-9f01-23456789abcd       48-bit Unix milliseconds, version 7, 74 random bits

A uid is written in the canonical lowercase form with dashes, which no alias or label may take
(`refs.valid_word` refuses it), so `name@<uid>` is never ambiguous. Sorting uids as text sorts them
by creation time; within one millisecond a process counts up, so its own uids stay in order.
"""

from __future__ import annotations

import os
import re
import threading
import time

UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

_lock = threading.Lock()
_last = [0, 0]  # the last millisecond and its 12-bit counter (rand_a)


def _format(milliseconds: int, rand_a: int, rand_b: int) -> str:
    value = (milliseconds & (2**48 - 1)) << 80 | 0x7 << 76 | (rand_a & 0xFFF) << 64 | 0b10 << 62 | (rand_b & (2**62 - 1))
    text = f"{value:032x}"
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def new_uid() -> str:
    """A fresh UUIDv7; uids made by one process are strictly increasing."""
    with _lock:
        milliseconds = time.time_ns() // 1_000_000
        if milliseconds <= _last[0]:
            milliseconds, counter = _last[0], _last[1] + 1
            if counter > 0xFFF:  # 4096 uids in one millisecond: borrow the next one
                milliseconds, counter = milliseconds + 1, int.from_bytes(os.urandom(2), "big") & 0x7FF
        else:
            counter = int.from_bytes(os.urandom(2), "big") & 0x7FF  # leave room to count up
        _last[:] = [milliseconds, counter]
    return _format(milliseconds, counter, int.from_bytes(os.urandom(8), "big"))


def valid_uid(uid) -> str:
    if not isinstance(uid, str) or not UID.fullmatch(uid):
        raise ValueError(f"invalid uid {uid!r}: expected a lowercase UUIDv7")
    return uid


def uid_time(uid: str) -> float:
    """The creation time a uid carries, in Unix seconds."""
    return int(uid.replace("-", "")[:12], 16) / 1000
