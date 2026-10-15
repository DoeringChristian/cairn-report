"""Artifact references: `[project:]name[@version|@alias|@label][//path/in/artifact]`.

    descent@latest//figures/trajectory.png      datasets/crops@v3      other-project:models/denoiser@best

Names are slash paths, so the file path inside the artifact follows a `//`. After `@`, `v<n>` is an
exact version; anything else is an alias (movable, per artifact) or a label (chosen at publish).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PROJECT = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
SEGMENT = re.compile(r"[a-z0-9][a-z0-9_.-]*")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")  # aliases and labels
VERSION = re.compile(r"v(0|[1-9][0-9]*)")
MAX_NAME = 200


def valid_project(project: str) -> str:
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise ValueError(f"invalid project name {project!r}: use lowercase letters, digits, '_', '.', '-'")
    return project


def valid_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > MAX_NAME:
        raise ValueError(f"invalid artifact name {name!r}")
    for segment in name.split("/"):
        if not SEGMENT.fullmatch(segment) or ".." in segment:
            raise ValueError(f"invalid artifact name {name!r}: segment {segment!r} must match [a-z0-9][a-z0-9_.-]* without '..'")
    return name


def valid_word(word: str, what: str) -> str:
    """An alias or label: never `v<n>`, which always means a version."""
    if not isinstance(word, str) or not WORD.fullmatch(word) or len(word) > 128:
        raise ValueError(f"invalid {what} {word!r}: use [A-Za-z0-9][A-Za-z0-9_.-]*")
    if VERSION.fullmatch(word) or re.fullmatch(r"v[0-9]+", word):
        raise ValueError(f"invalid {what} {word!r}: v<number> is reserved for versions")
    return word


@dataclass(frozen=True)
class Ref:
    project: str | None
    name: str
    selector: str | None = None
    path: str | None = None

    @property
    def version(self) -> int | None:
        """The exact version number if the selector is `v<n>`."""
        return int(self.selector[1:]) if self.selector and VERSION.fullmatch(self.selector) else None

    def __str__(self) -> str:
        return (f"{self.project}:" if self.project else "") + self.name + (f"@{self.selector}" if self.selector else "") \
            + (f"//{self.path}" if self.path else "")


def parse(text: str) -> Ref:
    if not isinstance(text, str) or not text.strip() or text != text.strip():
        raise ValueError(f"invalid reference {text!r}")
    head, separator, path = text.partition("//")
    if separator:
        parts = path.split("/")
        if not path or any(part in ("", ".", "..") or any(ord(char) < 32 for char in part) for part in parts) or "\\" in path:
            raise ValueError(f"invalid reference {text!r}: bad path after '//'")
    project = None
    if ":" in head:
        project, _, head = head.partition(":")
        valid_project(project)
    name, at, selector = head.partition("@")
    valid_name(name)
    if at:
        if VERSION.fullmatch(selector) is None:
            if re.fullmatch(r"v[0-9]+", selector):
                raise ValueError(f"invalid reference {text!r}: version numbers have no leading zeros")
            valid_word(selector, "alias or label")
    return Ref(project, name, selector if at else None, path if separator else None)


def exact(project: str, name: str, version: int) -> str:
    """The reference manifests store for a consumed version: `project:name@v<n>`."""
    return str(Ref(project, name, f"v{version}"))
