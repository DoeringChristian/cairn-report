"""Artifact references: `name[@v<n>|@<uid>|@<short id>|@alias|@label][//path/in/artifact]`, or a bare `<short id>`.

    descent@latest//figures/trajectory.png      datasets/crops@v3      models/denoiser@best      3f9a2c1
    datasets/crops@0192f3a4-5b6c-7d8e-9f01-23456789abcd      datasets/crops@3f9a2c1

A reference names a version of the project it is used in: there is no project in it (a repo holds one project;
a server serves each of its projects as if it were its own repo). Names are slash paths, so the file path inside
the artifact follows a `//`. After `@`, `v<n>` is an exact version number and a UUIDv7 (`uids.UID`, lowercase with
dashes) an exact version uid; both are reserved, so no alias or label can take either form. Anything else is an
alias (movable, per artifact), a label (chosen at publish), or a short id (hex digits, 4 or more: a prefix of the
SHA-256 of the version's uid, see `short_id`). Hex digits are also a valid alias, label, or artifact name, so
resolving matches names, numbers, aliases, and labels first and short ids last (`pick`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .errors import Ambiguous, InvalidRef, NotFound
from .uids import UID

PROJECT = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
SEGMENT = re.compile(r"[a-z0-9][a-z0-9_.-]*")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")  # aliases and labels
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)*")  # labels may also be words joined by `/`
VERSION = re.compile(r"v(0|[1-9][0-9]*)")
HEX = re.compile(r"[0-9a-f]{4,64}")  # what may be a short id
MAX_NAME = 200
SHORT = 7  # hex digits of a short id, unless more are needed to tell it apart


def valid_project(project: str) -> str:
    """A project name on a server (`<server>/<project>`): a lowercase slug."""
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise InvalidRef(f"invalid project name {project!r}: use lowercase letters, digits, '_', '.', '-'")
    return project


def valid_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > MAX_NAME:
        raise InvalidRef(f"invalid artifact name {name!r}")
    for segment in name.split("/"):
        if not SEGMENT.fullmatch(segment) or ".." in segment:
            raise InvalidRef(f"invalid artifact name {name!r}: segment {segment!r} must match [a-z0-9][a-z0-9_.-]* without '..'")
    return name


def valid_word(word: str, what: str) -> str:
    """An alias or label: never `v<n>`, which always means a version."""
    if not isinstance(word, str) or not WORD.fullmatch(word) or len(word) > 128:
        raise InvalidRef(f"invalid {what} {word!r}: use [A-Za-z0-9][A-Za-z0-9_.-]*")
    if VERSION.fullmatch(word) or re.fullmatch(r"v[0-9]+", word):
        raise InvalidRef(f"invalid {what} {word!r}: v<number> is reserved for versions")
    if UID.fullmatch(word.lower()):
        raise InvalidRef(f"invalid {what} {word!r}: a UUIDv7 is reserved for version uids")
    return word


def valid_label(label: str) -> str:
    """A label: a word, or words joined by single slashes (a multirun trial's `<sweep-time>/<n>`)."""
    if not isinstance(label, str) or not LABEL.fullmatch(label) or len(label) > 128:
        raise InvalidRef(f"invalid label {label!r}: use [A-Za-z0-9][A-Za-z0-9_.-]* (words may be joined by '/')")
    for word in label.split("/"):
        valid_word(word, "label")
    return label


@dataclass(frozen=True)
class Ref:
    name: str
    selector: str | None = None
    path: str | None = None

    @property
    def version(self) -> int | None:
        """The exact version number if the selector is `v<n>`."""
        return int(self.selector[1:]) if self.selector and VERSION.fullmatch(self.selector) else None

    @property
    def uid(self) -> str | None:
        """The exact version uid if the selector is one."""
        return self.selector if self.selector and UID.fullmatch(self.selector) else None

    @property
    def exact(self) -> bool:
        """Whether the selector names one version for good (`v<n>` or a uid), not an alias, label, or short id."""
        return self.version is not None or self.uid is not None

    @property
    def id(self) -> str | None:
        """The short id (or uid) this reference may stand for when no name, alias, or label matches: a selector of hex
        digits, or, without a selector, a bare name of hex digits or a uid (a version of any artifact)."""
        if self.selector is not None:
            return self.selector if HEX.fullmatch(self.selector) else None
        return self.name if HEX.fullmatch(self.name) or UID.fullmatch(self.name) else None

    @property
    def bare(self) -> bool:
        """No selector and a name that may be an id: if no artifact has that name, it names a version of any artifact."""
        return self.selector is None and self.id is not None

    def __str__(self) -> str:
        return self.name + (f"@{self.selector}" if self.selector else "") + (f"//{self.path}" if self.path else "")


def parse(text: str) -> Ref:
    if not isinstance(text, str) or not text.strip() or text != text.strip():
        raise InvalidRef(f"invalid reference {text!r}")
    head, separator, path = text.partition("//")
    if separator:
        parts = path.split("/")
        if not path or any(part in ("", ".", "..") or any(ord(char) < 32 for char in part) for part in parts) or "\\" in path:
            raise InvalidRef(f"invalid reference {text!r}: bad path after '//'")
    if ":" in head:
        raise InvalidRef(f"invalid reference {text!r}: a reference names an artifact of this project (name@version), "
                         "without a project")
    name, at, selector = head.partition("@")
    valid_name(name)
    if at:
        if VERSION.fullmatch(selector) is None and UID.fullmatch(selector) is None:
            if re.fullmatch(r"v[0-9]+", selector):
                raise InvalidRef(f"invalid reference {text!r}: version numbers have no leading zeros")
            valid_label(selector) if "/" in selector else valid_word(selector, "alias or label")
    return Ref(name, selector if at else None, path if separator else None)


def exact(name: str, version: int) -> str:
    """The reference manifests store for a consumed version: `name@v<n>`."""
    return str(Ref(name, f"v{version}"))


# ------------------------------------------------------------------ short ids


def id_hash(uid: str) -> str:
    """The SHA-256 (hex) of a version uid: short ids are its prefixes."""
    return hashlib.sha256(uid.encode()).hexdigest()


def default_short_id(uid: str) -> str:
    """A version's short id unless more digits were needed to tell it from another one: SHORT hex digits."""
    return id_hash(uid)[:SHORT]


def short_id(uid: str, taken) -> str:
    """The short id of a new version: the first SHORT hex digits of `id_hash(uid)`, more while `taken(prefix)` says
    another version's hash starts with them."""
    full = id_hash(uid)
    for length in range(SHORT, len(full)):
        if not taken(full[:length]):
            return full[:length]
    return full


def short_of(manifest: dict) -> str:
    """The short id of a version manifest (its own, else the default for its uid)."""
    return manifest.get("short_id") or default_short_id(manifest["uid"])


def candidate(name: str, manifest: dict) -> dict:
    """A version as an id lookup lists it: {name, version, uid, short_id, status}."""
    return {"name": name, "version": manifest.get("version"), "uid": manifest["uid"], "short_id": short_of(manifest),
            "status": manifest.get("status")}


def describe(item: dict) -> str:
    """`name@v<n> (3f9a2c1)`, or `name@<uid> (3f9a2c1)` while the version has no number."""
    selector = f"v{item['version']}" if item.get("version") is not None else item["uid"]
    return f"{item['name']}@{selector} ({item['short_id']})"


def matches(text: str, uid: str) -> bool:
    """Whether a short id (or a uid) may name the version with this uid."""
    return uid == text if UID.fullmatch(text) else id_hash(uid).startswith(text)


def pick(text: str, candidates: list[dict], where: str = "") -> dict:
    """The one version a short id (or a uid) names among `candidates` (versions whose uid it may name, `matches`): a
    version whose short id is exactly `text` wins (short ids are fixed when a version is made, so an older one keeps
    its digits when a newer one needed more), else the only one. NotFound without one, Ambiguous with several."""
    found = [item for item in candidates if matches(text, item["uid"])]
    found = [item for item in found if item["short_id"] == text] or found
    unique = list({item["uid"]: item for item in found}.values())
    if not unique:
        raise NotFound(f"no version with the id {text}{where}")
    if len(unique) > 1:
        listed = ", ".join(describe(item) for item in sorted(unique, key=lambda item: (item["name"], item["version"] or 0)))
        raise Ambiguous(f"the id {text} names {len(unique)} versions{where}: {listed}; give more digits", unique)
    return unique[0]
