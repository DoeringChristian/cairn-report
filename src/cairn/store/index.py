"""The in-memory index of a LocalStore: per project, its artifacts' versions, aliases, and summaries, and who
consumed which version. The append-only files stay the only source of truth; the index is a cache of
them that is read once and then kept current by reading only what was appended since.

Consistency model (one host, a local filesystem; see fs.py for network filesystems):

- Every file is append-only, so a file that grew is read from where the index stopped, up to its last
  complete line (a line still being written is read next time). A file that shrank or was replaced
  (another inode) is read again from the start.
- An artifact is checked before every single-artifact read and write: one `stat` of its `versions.jsonl`
  and one of its `aliases.jsonl`. Reads and writes of one artifact therefore see everything any process
  on this host wrote before (read-your-writes across processes).
- A listing of a project (its artifacts, the project overview) does not stat every artifact. Every write
  also appends the artifact's name to the project's `changes.jsonl`, a hint without fsync, and a listing
  reads only the new lines of it and refreshes the artifacts they name. A writer that dies between its
  write and the hint leaves the listing stale for that artifact until the next sweep: every
  `SWEEP_SECONDS` a listing stats every artifact, and every `SCAN_SECONDS` it also looks for artifact
  folders it does not know.
- Lineage reads the new lines of the project's `consumers.jsonl` (one `stat`) on every request.
- A process keeps its own index; nothing is shared between processes but the files.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

SWEEP_SECONDS = 5.0
SCAN_SECONDS = 60.0


class Tail:
    """The complete lines of an append-only JSON Lines file, read incrementally."""

    __slots__ = ("path", "offset", "inode", "exists")

    def __init__(self, path: Path):
        self.path, self.offset, self.inode, self.exists = os.fspath(path), 0, None, False

    def skip(self) -> None:
        """Start at the current end of the file (a line being written there is skipped as torn)."""
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return
        self.offset, self.inode, self.exists = info.st_size, info.st_ino, True

    def poll(self) -> tuple[bool, list[dict]]:
        """(reset, rows): rows appended since the last poll; reset says the file was replaced, shrank, or
        vanished, and the rows are then the whole file again (the caller drops what it knew)."""
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            reset = self.exists or self.offset > 0
            self.offset, self.inode, self.exists = 0, None, False
            return reset, []
        reset = False
        if (self.inode is not None and info.st_ino != self.inode) or info.st_size < self.offset:
            self.offset, reset = 0, True
        self.inode, self.exists = info.st_ino, True
        if info.st_size == self.offset:
            return reset, []
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = handle.read(info.st_size - self.offset)
        end = data.rfind(b"\n")
        if end < 0:
            return reset, []
        self.offset += end + 1
        rows = []
        for line in data[:end].split(b"\n"):
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:  # a torn line (its writer died); the next append starts a new line
                continue
            if isinstance(row, dict):
                rows.append(row)
        return reset, rows


class ArtifactIndex:
    """One artifact: the latest state per version number, uids, aliases, and its summary.

    `write_lock` serializes this process's writers to the artifact (held while it checks and appends);
    `lock` guards the index itself and is only ever held briefly, never while taking another lock."""

    def __init__(self, project: str, name: str, folder: Path):
        self.project, self.name, self.folder = project, name, folder
        self.versions_tail = Tail(folder / "versions.jsonl")
        self.aliases_tail = Tail(folder / "aliases.jsonl")
        self.states: dict[int, dict] = {}
        self.by_uid: dict[str, int] = {}
        self.aliases: dict[str, int] = {}
        self.summary: dict | None = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.numbers_ready = False  # `.numbers/` exists (local.py claims version numbers there)

    @property
    def exists(self) -> bool:
        return self.versions_tail.exists

    def refresh(self, upgrade) -> bool:
        """Read what was appended since last time; True if anything changed. `upgrade(artifact, states, numbers)`
        gets the versions whose stored lines lack a uid (stores written before uids) and returns {number: uid}."""
        with self.lock:
            reset, rows = self.versions_tail.poll()
            changed = reset or bool(rows)
            if reset:
                self.states, self.by_uid = {}, {}
            missing_uid = []
            for row in rows:
                number = row.get("version")
                if not isinstance(number, int):
                    continue
                self.states[number] = row
                if row.get("uid"):
                    self.by_uid[row["uid"]] = number
                else:
                    missing_uid.append(number)
            if missing_uid:
                for number, uid in upgrade(self, self.states, sorted(set(missing_uid))).items():
                    self.by_uid[uid] = number
            reset, rows = self.aliases_tail.poll()
            if reset:
                self.aliases = {}
            for row in rows:
                if isinstance(row.get("alias"), str):
                    self.aliases[row["alias"]] = row.get("version")
            changed = changed or reset or bool(rows)
            if changed:
                self.summary = None
            return changed

    def snapshot(self) -> tuple[dict[int, dict], dict[str, int]]:
        with self.lock:
            return dict(self.states), dict(self.aliases)

    def get_summary(self) -> dict:
        with self.lock:
            if self.summary is None:
                stamps = [state.get("completed") or state.get("created") or "" for state in self.states.values()]
                head = self.states.get(self.aliases.get("latest"))
                self.summary = {"name": self.name, "latest": self.aliases.get("latest"), "versions": len(self.states),
                                "aliases": dict(self.aliases), "updated": max(stamps, default=""),
                                "metadata": head["metadata"] if head else {}}
            return self.summary


class ProjectIndex:
    """A project's artifacts (each an ArtifactIndex), the change hints, and the consumers of its versions."""

    def __init__(self, folder: Path):
        self.folder, self.project = folder, folder.name
        self.base = folder / "artifacts"
        self.changes = Tail(folder / "changes.jsonl")
        self.consumers_tail = Tail(folder / "consumers.jsonl")
        self.consumers: dict[str, list[str]] = {}
        self.artifacts: dict[str, ArtifactIndex] = {}
        self.lock = threading.Lock()
        self.fresh_lock = threading.Lock()  # one listing refresh at a time; taken before any artifact's lock
        self.loaded = False
        self.swept = self.scanned = 0.0
        self.generation = 0  # bumped whenever a listing would change
        self.listing: tuple[int, list[dict]] | None = None

    def artifact(self, name: str, upgrade, create: bool = True) -> ArtifactIndex:
        """The artifact's index, brought up to date. An artifact without versions is only kept with `create`
        (a writer), so looking up names that do not exist leaves nothing behind."""
        with self.lock:
            found = self.artifacts.get(name)
        if found is None:
            found = ArtifactIndex(self.project, name, self.base / name)
            found.refresh(upgrade)
            if not (found.exists or create):
                return found
            with self.lock:
                found = self.artifacts.setdefault(name, found)
                self.generation += 1
        if found.refresh(upgrade):
            self.bump()
        return found

    def _scan(self) -> None:
        """Find every artifact folder (a folder with a versions.jsonl)."""
        names = []
        if self.base.is_dir():
            for directory, folders, files in os.walk(self.base):
                folders[:] = [folder for folder in folders if not folder.startswith(".")]
                if "versions.jsonl" in files:
                    names.append(Path(directory).relative_to(self.base).as_posix())
        with self.lock:
            for name in names:
                if name not in self.artifacts:
                    self.artifacts[name] = ArtifactIndex(self.project, name, self.base / name)
                    self.generation += 1
        self.scanned = time.monotonic()

    def fresh(self, upgrade) -> list[tuple[str, ArtifactIndex]]:
        """The project's artifacts, sorted by name, with the index brought up to date (see the module doc)."""
        with self.fresh_lock:
            return self._fresh(upgrade)

    def _fresh(self, upgrade) -> list[tuple[str, ArtifactIndex]]:
        now = time.monotonic()
        if not self.loaded:
            self.changes.skip()  # the hints so far are not needed: the scan below reads everything
            self._scan()
            self.loaded, self.swept = True, now
            self._refresh_all(upgrade)
            return self.items()
        reset, rows = self.changes.poll()
        names = {row.get("artifact") for row in rows if isinstance(row.get("artifact"), str)}
        if reset or now - self.scanned > SCAN_SECONDS:
            self._scan()
        if reset or now - self.swept > SWEEP_SECONDS:
            self.swept = now
            self._refresh_all(upgrade)
        for name in names:
            self.artifact(name, upgrade, create=False)
        return self.items()

    def _refresh_all(self, upgrade) -> None:
        with self.lock:
            known = list(self.artifacts.values())
        for artifact in known:
            if artifact.refresh(upgrade):
                self.bump()

    def items(self) -> list[tuple[str, ArtifactIndex]]:
        with self.lock:
            found = [(name, artifact) for name, artifact in self.artifacts.items() if artifact.exists]
        return sorted(found, key=lambda item: item[0])

    def bump(self) -> None:
        with self.lock:
            self.generation += 1

    def lineage_consumers(self, exact: str) -> list[str]:
        with self.lock:
            reset, rows = self.consumers_tail.poll()
            if reset:
                self.consumers = {}
            for row in rows:
                consumer, source = row.get("consumer"), row.get("input")
                if isinstance(consumer, str) and isinstance(source, str):
                    known = self.consumers.setdefault(source, [])
                    if consumer not in known:
                        known.append(consumer)
            return list(self.consumers.get(exact, []))
