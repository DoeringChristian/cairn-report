"""Retention rules and garbage collection of a store root.

Deleting a version (LocalStore.delete_version) leaves a tombstone in the metadata: its number and uid are never
handed out again, its file list is dropped, and its blobs may become garbage. `LocalStore.gc` then

1. applies the retention rules (per project, or per artifact-name prefix; the longest matching prefix wins):

       {"prefix": "", "keep_last": 5, "keep_days": 30, "keep_aliased": true}

   A version is kept when it is among the `keep_last` newest versions of its artifact (running ones take no
   place), or younger than `keep_days` (either rule keeping it is enough), or the artifact's `latest`. With `keep_aliased` (the default)
   a version an alias other than `latest` points at is kept and takes no `keep_last` place; without it, it takes
   a place like any other. Rules never delete protected versions (an alias other than `latest`, an input of a
   version that is not deleted, in any project, or running): those are skipped and reported.

2. sweeps blobs (mark and sweep): a blob no version that is not deleted names, in any project, whose file is
   older than the grace period (default 24 h), is removed. Blobs are uploaded before the version that names them
   is committed, so the grace period is what keeps an upload in progress safe. The sweep marks outside any
   transaction, then removes inside one write transaction: there it marks again the projects written since
   (every write bumps a project's generation) and checks each file's age again, then unlinks it. A publish
   checks its blobs inside its own transaction whenever a sweep ran since it last looked (`sweeps` in the meta
   table), so it either sees the blob kept or fails with MissingBlobs, uploads it again, and retries.

Stale temporary uploads (`<root>/tmp/`, untouched for the grace period, and at least an hour, so a short grace
period never removes an upload while it is being written) are removed too.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime

from . import refs

GRACE = 24 * 3600.0
STALE_UPLOAD = 3600.0  # a temporary upload file untouched this long (or the grace period, if longer) is a dead upload
DAY = 86400.0
UNITS = {"s": 1, "m": 60, "h": 3600, "d": DAY, "w": 7 * DAY}
HEX = re.compile(r"[0-9a-f]+")


def parse_duration(text) -> float:
    """Seconds from a number (seconds) or text like `90s`, `15m`, `24h`, `7d`, `2w`."""
    if isinstance(text, bool):
        raise ValueError(f"invalid duration {text!r}")
    if isinstance(text, (int, float)):
        seconds = float(text)
    else:
        match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([smhdw]?)\s*", str(text))
        if match is None:
            raise ValueError(f"invalid duration {text!r}: use a number of seconds or e.g. 30m, 24h, 7d")
        seconds = float(match.group(1)) * UNITS[match.group(2) or "s"]
    if seconds < 0:
        raise ValueError(f"invalid duration {text!r}: negative")
    return seconds


def describe_bytes(size: int) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


# ------------------------------------------------------------------ retention rules


def clean_rule(rule) -> dict:
    """A validated rule: {prefix, keep_last, keep_days, keep_aliased}."""
    if not isinstance(rule, dict):
        raise ValueError("a retention rule is an object {prefix?, keep_last?, keep_days?, keep_aliased?}")
    unknown = set(rule) - {"prefix", "keep_last", "keep_days", "keep_aliased"}
    if unknown:
        raise ValueError(f"unknown retention settings {sorted(unknown)}")
    prefix = rule.get("prefix") or ""
    if not isinstance(prefix, str) or len(prefix) > refs.MAX_NAME or (prefix and not re.fullmatch(r"[a-z0-9][a-z0-9_./-]*", prefix)):
        raise ValueError(f"invalid artifact prefix {prefix!r}")
    keep_last, keep_days = rule.get("keep_last"), rule.get("keep_days")
    if keep_last is not None and (isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 1):
        raise ValueError("keep_last must be a whole number of at least 1")
    if keep_days is not None and (isinstance(keep_days, bool) or not isinstance(keep_days, (int, float)) or keep_days < 0):
        raise ValueError("keep_days must be a number of days, at least 0")
    if keep_last is None and keep_days is None:
        raise ValueError("a retention rule needs keep_last and/or keep_days")
    keep_aliased = rule.get("keep_aliased", True)
    if not isinstance(keep_aliased, bool):
        raise ValueError("keep_aliased must be true or false")
    return {"prefix": prefix, "keep_last": keep_last, "keep_days": keep_days, "keep_aliased": keep_aliased}


def clean_rules(rules) -> list[dict]:
    if not isinstance(rules, list) or len(rules) > 1000:
        raise ValueError("retention rules are a list")
    clean = [clean_rule(rule) for rule in rules]
    prefixes = [rule["prefix"] for rule in clean]
    if len(set(prefixes)) != len(prefixes):
        raise ValueError("one retention rule per prefix")
    return sorted(clean, key=lambda rule: rule["prefix"])


def rule_for(rules: list[dict], name: str) -> dict | None:
    """The rule for an artifact: the one with the longest prefix the name starts with."""
    matching = [rule for rule in rules if name.startswith(rule["prefix"])]
    return max(matching, key=lambda rule: len(rule["prefix"])) if matching else None


def _epoch(stamp: str | None) -> float | None:
    try:
        return datetime.fromisoformat(stamp).timestamp() if stamp else None
    except ValueError:
        return None


def expired(states: list[dict], aliases: dict[str, int], rule: dict, now: float) -> list[int]:
    """The version numbers a rule would delete, newest first, before the protection checks (which then skip running
    and aliased versions, and inputs of other versions). Running versions take no `keep_last` place; `latest` takes
    one and is always kept."""
    aliased = {number for alias, number in aliases.items() if alias != "latest"}
    latest = aliases.get("latest")
    rank, found = 0, []
    for state in sorted(states, key=lambda item: -item["version"]):
        number = state["version"]
        if state["status"] == "deleted" or (rule["keep_aliased"] and number in aliased):
            continue
        keep = number == latest
        if state["status"] != "running":
            keep = keep or (rule["keep_last"] is not None and rank < rule["keep_last"])
            rank += 1
        created = _epoch(state.get("created"))
        if rule["keep_days"] is not None and (created is None or now - created < rule["keep_days"] * DAY):
            keep = True
        if not keep:
            found.append(number)
    return found


# ------------------------------------------------------------------ blobs


def blob_files(blobs_folder: str):
    """(sha256, path) of every blob file under `<root>/blobs/`."""
    try:
        folders = os.scandir(blobs_folder)
    except FileNotFoundError:
        return
    with folders:
        for folder in folders:
            if len(folder.name) != 2 or not HEX.fullmatch(folder.name) or not folder.is_dir(follow_symlinks=False):
                continue
            with os.scandir(folder.path) as entries:
                for entry in entries:
                    sha = folder.name + entry.name
                    if len(sha) == 64 and HEX.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                        yield sha, entry.path


def candidates(blobs_folder: str, referenced: set[str], cutoff: float) -> list[tuple[str, str, int]]:
    """(sha256, path, size) of the blobs no version names whose file is older than `cutoff` (epoch seconds)."""
    found = []
    for sha, path in blob_files(blobs_folder):
        if sha in referenced:
            continue
        try:
            info = os.stat(path)
        except FileNotFoundError:
            continue
        if info.st_mtime < cutoff:
            found.append((sha, path, info.st_size))
    return found


def stale_temporaries(tmp_folder: str, cutoff: float) -> list[tuple[str, int]]:
    """(path, size) of files in `<root>/tmp/` untouched since `cutoff`: uploads that died."""
    found = []
    try:
        entries = os.scandir(tmp_folder)
    except FileNotFoundError:
        return found
    with entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if entry.is_file(follow_symlinks=False) and info.st_mtime < cutoff:
                found.append((entry.path, info.st_size))
    return found


def now() -> float:
    return time.time()
