"""`cairn prune`: trim the cache root by last access, never touching what only this machine has.

Synced versions (`<cache>/versions/<uid>/`) go, least recently used first, when their `accessed` time
(written into the record by pulls, not the filesystem's atime) is older than `older_than`, and then until
the cache is under `max_size`. Blobs no remaining cached version references go too, except those a
state record (pending, running, rejected) needs, and except blobs written in the last hour (a pull may
be about to link them). Pending and running versions live in the state root and are never pruned; when
both roots are the same folder, their records are skipped as well. Leftover scratch names
(`.tmp-<host>-<pid>-...`) older than a day are removed.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from ..store.cache import cache_root, remove
from . import replica

RECENT = 3600.0  # seconds: unreferenced blobs younger than this stay (a pull may be linking them)
LEFTOVER = 86400.0  # seconds: scratch folders and files older than this are leftovers of a crash

UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
SPANS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_size(text: str) -> int:
    """`20G`, `500M`, `1.5T`, or bytes."""
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([kmgtb]?)i?b?\s*", str(text).lower())
    if not match:
        raise ValueError(f"invalid size {text!r}: use a number with K, M, G, or T, e.g. 20G")
    return int(float(match.group(1)) * UNITS[match.group(2)])


def parse_age(text: str) -> float:
    """`30d`, `12h`, `90m`, `2w`, or seconds."""
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([smhdw]?)\s*", str(text).lower())
    if not match:
        raise ValueError(f"invalid age {text!r}: use a number with s, m, h, d, or w, e.g. 30d")
    return float(match.group(1)) * SPANS[match.group(2) or "s"]


def _files(folder: Path):
    for directory, _, names in os.walk(folder):
        for name in names:
            path = Path(directory) / name
            try:
                yield path, path.lstat()
            except OSError:
                continue


def prune(older_than: float | None = None, max_size: int | None = None, dry_run: bool = False) -> dict:
    """Remove synced versions and unreferenced blobs from the cache root (see the module doc).

    Returns {"versions": [uids removed], "blobs": count, "freed": bytes, "size": bytes left}."""
    root = cache_root()
    keep = replica.needed()  # contents a state record needs
    versions, inodes = [], {}  # (accessed, uid, folder, shas); inode -> [size, paths left]
    for record in replica.synced_records():
        folder = root / "versions" / record["uid"]
        shas = {item["sha256"] for item in (record.get("manifest") or {}).get("files", []) if "sha256" in item}
        owned = []
        for path, info in _files(folder):
            key = (info.st_dev, info.st_ino)
            inodes.setdefault(key, [info.st_size, 0])[1] += 1
            owned.append(key)
        accessed = record.get("accessed_at") or replica.epoch(record.get("accessed"))
        versions.append((accessed, record["uid"], folder, shas, owned))
    blobs = {}
    for path, info in _files(root / "blobs"):
        if ".tmp-" in path.name:
            continue
        key = (info.st_dev, info.st_ino)
        inodes.setdefault(key, [info.st_size, 0])[1] += 1
        blobs[path.name] = (path, info, key)
    size = sum(value[0] for value in inodes.values())
    removed, freed, count = [], 0, 0

    def release(keys) -> int:
        gained = 0
        for key in keys:
            entry = inodes[key]
            entry[1] -= 1
            if entry[1] == 0:
                gained += entry[0]
        return gained

    versions.sort()
    now = time.time()
    left = []
    for accessed, uid, folder, shas, owned in versions:
        old = older_than is not None and now - accessed > older_than
        big = max_size is not None and size > max_size
        if old or big:
            if not dry_run:
                remove(folder)
            gained = release(owned)
            size, freed = size - gained, freed + gained
            removed.append(uid)
        else:
            left.append(shas)
    referenced = set().union(*left) if left else set()
    for sha, (path, info, key) in sorted(blobs.items(), key=lambda item: item[1][1].st_mtime):
        if sha in referenced or sha in keep or now - info.st_mtime < RECENT:
            continue
        if not dry_run:
            path.unlink(missing_ok=True)
        gained = release([key])
        size, freed, count = size - gained, freed + gained, count + 1
    if not dry_run:
        for base in (root / "versions", root / "blobs"):
            for path in base.rglob("*.tmp-*") if base.is_dir() else []:
                try:
                    if now - path.lstat().st_mtime > LEFTOVER:
                        remove(path) if path.is_dir() and not path.is_symlink() else path.unlink(missing_ok=True)
                except OSError:
                    pass
    return {"versions": removed, "blobs": count, "freed": freed, "size": size}


def describe_size(size: int) -> str:
    for unit, factor in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}"
    return f"{size} B"

