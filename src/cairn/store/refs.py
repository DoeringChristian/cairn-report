"""References: the one parser of every reference Cairn reads, wherever it comes from (the CLI, the Python API, report
`inputs:`, `.cairn-inputs.jsonl`, the server's API, the UI's URLs). Nothing rewrites a reference before `parse`, and
no other module takes one apart.

A reference has exactly one of four forms, told apart by its shape alone, before anything is looked up:

    <id>              3f9a2c1                one version: 4 to 64 hex digits, a prefix of its id (ids.py)
    <name>            descent                the artifact's newest completed version (`@latest`)
    <name>@v<n>       descent@v3             version n of the artifact
    <name>@<word>     models/denoiser@best   the version an alias (`best`, `latest`) or a label names

and a file inside the version may follow after `//` (`descent@v3//figures/a.png`, `3f9a2c1//figures/a.png`).

The forms cannot overlap: an id has no `@`; `v<n>` is never a word, nor anything else of a version's shape (MALFORMED); a 0.14 uid is neither a name nor a word; an artifact name, an alias, and a label are never 4
or more hex digits alone (`cafe`, `1234`, `DEADBEEF`: refused when one is created, matched case-insensitively), so a
bare word is an id or a name by its shape, and a word after `@` is a number or a word. A reference names a version of
the project it is used in: there is no project in it.

A short id is the shortest prefix of at least SHORT digits that no other id of the project starts with (`short_id`);
any prefix of 4 or more digits names the version while it is unique, and one that matches several versions is
Ambiguous, listing them (`pick`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .errors import Ambiguous, InvalidRef, NotFound
from .ids import ID

PROJECT = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
SEGMENT = re.compile(r"[a-z0-9][a-z0-9_.-]*")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")  # aliases and labels
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)*")  # words, maybe joined by `/`
VERSION = re.compile(r"v(0|[1-9][0-9]*)")
MALFORMED = re.compile(
    r"[vV][-+]?[0-9].*", re.DOTALL
)  # looks like a version: never a word (`v<n>` aside, which is one)
HEX = re.compile(r"[0-9a-fA-F]{4,}")  # never a name, an alias, or a label alone
UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)  # Cairn 0.14's
BLANK = " \t\n\r\x0b\x0c"  # a reference never starts or ends with one
MAX_NAME = 200
MIN_PREFIX = 4
SHORT = 7  # digits of a short id, unless more are needed to tell it apart


def _old_uid(text: str) -> str:
    """What a pasted Cairn 0.14 uid gets: the id `cairn upgrade` gave that version (the SHA-256 of the uid)."""
    id = hashlib.sha256(text.lower().encode()).hexdigest()
    return (
        f"{text} is a Cairn 0.14 uid: since 0.15 a version is named by its id; after `cairn upgrade` this one's id is "
        f"{id} (its short id {id[:SHORT]})"
    )


def valid_project(project: str) -> str:
    """A project name on a server (`<server>/<project>`): a lowercase slug."""
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise InvalidRef(f"invalid project name {project!r}: use lowercase letters, digits, '_', '.', '-'")
    return project


def hex_alone(text) -> bool:
    """Whether `text` is 4 or more hex digits and nothing else (case-insensitive): the shape of an id."""
    return isinstance(text, str) and HEX.fullmatch(text) is not None


def valid_name(name: str) -> str:
    """An artifact name: slash-separated lowercase segments, never 4 or more hex digits alone (that is an id)."""
    if not isinstance(name, str) or not name or len(name) > MAX_NAME:
        raise InvalidRef(f"invalid artifact name {name!r}")
    if hex_alone(name):
        raise InvalidRef(
            f"invalid artifact name {name!r}: 4 or more hex digits alone are an id, never a name (add a letter "
            f"outside a-f, a '-', or a folder, e.g. 'data-{name}')"
        )
    if UID.fullmatch(name):
        raise InvalidRef(_old_uid(name))
    for segment in name.split("/"):
        if not SEGMENT.fullmatch(segment) or ".." in segment:
            raise InvalidRef(
                f"invalid artifact name {name!r}: segment {segment!r} must match [a-z0-9][a-z0-9_.-]* without '..'"
            )
    return name


def valid_word(word: str, what: str) -> str:
    """An alias or a label word: never `v<n>` (a version) and never 4 or more hex digits alone (an id)."""
    if not isinstance(word, str) or not WORD.fullmatch(word) or len(word) > 128:
        raise InvalidRef(f"invalid {what} {word!r}: use [A-Za-z0-9][A-Za-z0-9_.-]*")
    if MALFORMED.fullmatch(word):
        raise InvalidRef(f"invalid {what} {word!r}: v<number> is reserved for versions")
    if UID.fullmatch(word):
        raise InvalidRef(_old_uid(word))
    if hex_alone(word):
        raise InvalidRef(f"invalid {what} {word!r}: 4 or more hex digits alone are an id, never an alias or a label")
    return word


def valid_label(label: str) -> str:
    """A label: a word, or words joined by single slashes (a multirun trial's `<sweep-time>/<n>`)."""
    if not isinstance(label, str) or not LABEL.fullmatch(label) or len(label) > 128:
        raise InvalidRef(f"invalid label {label!r}: use [A-Za-z0-9][A-Za-z0-9_.-]* (words may be joined by '/')")
    if "/" not in label:
        return valid_word(label, "label")
    for word in label.split("/"):
        if MALFORMED.fullmatch(word):
            raise InvalidRef(f"invalid label {label!r}: v<number> is reserved for versions")
    return label


def new_label(label: str) -> str:
    """A label a new version may get: a valid label, and not `latest` (the alias of every artifact)."""
    valid_label(label)
    if label == "latest":
        raise InvalidRef("invalid label 'latest': it is the alias of every artifact's newest version")
    return label


def new_alias(alias: str) -> str:
    """An alias that may be set: a valid word, and not `latest` (it moves by itself)."""
    valid_word(alias, "alias")
    if alias == "latest":
        raise InvalidRef("'latest' moves automatically when a version completes")
    return alias


def default_label(text: str | None) -> str | None:
    """`text` as a label when a new version may have it (a folder's name), else None."""
    try:
        return new_label(text) if text else None
    except InvalidRef:
        return None


@dataclass(frozen=True)
class Ref:
    """A parsed reference: the id form (`id`, a prefix of 4 to 64 hex digits, and no name), or a name with an optional
    selector (`v<n>`, or an alias or label; None is `latest`); either with an optional path inside the version."""

    name: str | None = None
    selector: str | None = None
    path: str | None = None
    id: str | None = None

    @classmethod
    def of(cls, id: str, path: str | None = None) -> "Ref":
        """The reference of a version by its id (whole, or a prefix)."""
        return cls(None, None, path, id)

    @property
    def version(self) -> int | None:
        """The number of `name@v<n>`."""
        return int(self.selector[1:]) if self.selector and VERSION.fullmatch(self.selector) else None

    @property
    def word(self) -> str | None:
        """The alias or label of `name@<word>`; `latest` for a bare name; None for `v<n>` and the id form."""
        if self.id is not None or self.version is not None:
            return None
        return self.selector or "latest"

    @property
    def whole(self) -> bool:
        """Whether this is the id form with a whole id (64 digits)."""
        return self.id is not None and ID.fullmatch(self.id) is not None

    @property
    def exact(self) -> bool:
        """Whether it names one version for good: `name@v<n>`, or a whole id (not an alias, a label, or a prefix)."""
        return self.version is not None or self.whole

    def at(self, path: str | None) -> "Ref":
        """The same version, another path inside it."""
        return Ref(self.name, self.selector, path, self.id)

    def __str__(self) -> str:
        head = self.id if self.id is not None else self.name + (f"@{self.selector}" if self.selector else "")
        return head + (f"//{self.path}" if self.path else "")


def parse(text) -> Ref:
    """The reference `text` (see the module doc); InvalidRef when it has none of the four forms. A Ref is returned
    as it is."""
    if isinstance(text, Ref):
        return text
    if not isinstance(text, str) or not text or text[0] in BLANK or text[-1] in BLANK:
        raise InvalidRef(f"invalid reference {text!r}")
    head, separator, path = text.partition("//")
    if separator:
        parts = path.split("/")
        if (
            not path
            or any(part in ("", ".", "..") or any(ord(char) < 32 for char in part) for part in parts)
            or "\\" in path
        ):
            raise InvalidRef(f"invalid reference {text!r}: bad path after '//'")
    if ":" in head:
        raise InvalidRef(
            f"invalid reference {text!r}: a reference names a version of this project (an id, a name, name@v<n>, or "
            "name@<alias or label>), without a project"
        )
    path = path if separator else None
    name, at, selector = head.partition("@")
    if not at:
        if hex_alone(head):
            if len(head) > 64:
                raise InvalidRef(f"invalid reference {text!r}: an id has at most 64 hex digits")
            return Ref(None, None, path, head.lower())
        valid_name(head)
        return Ref(head, None, path)
    if hex_alone(name):
        raise InvalidRef(
            f"invalid reference {text!r}: {name} is an id (4 or more hex digits), which names one version alone, "
            "without '@'"
        )
    valid_name(name)
    if not selector:
        raise InvalidRef(f"invalid reference {text!r}: nothing after '@' (v<n>, an alias, or a label)")
    if VERSION.fullmatch(selector):
        return Ref(name, selector, path)
    if MALFORMED.fullmatch(selector):
        raise InvalidRef(f"invalid reference {text!r}: a version is v<n>, n without leading zeros (v1, v12)")
    if UID.fullmatch(selector):
        raise InvalidRef(_old_uid(selector))
    if hex_alone(selector):
        raise InvalidRef(
            f"invalid reference {text!r}: {selector} is an id, which names a version alone: write "
            f"{selector.lower()}{f'//{path}' if path else ''} (aliases and labels are never hex digits)"
        )
    valid_label(selector)
    return Ref(name, selector, path)


def version_segment(version: int | str) -> str:
    """How an API path names a version made by code: `v<n>` for a number, the whole id for an id."""
    if isinstance(version, bool) or not (isinstance(version, int) or ID.fullmatch(str(version))):
        raise InvalidRef(f"a version is a number or an id (64 hex digits), not {version!r}")
    return f"v{version}" if isinstance(version, int) else version


def version_key(segment: str) -> int | str:
    """The version an API path segment names (`version_segment`): `v<n>` is the number n, 64 hex digits an id."""
    if isinstance(segment, str) and VERSION.fullmatch(segment):
        return int(segment[1:])
    if isinstance(segment, str) and ID.fullmatch(segment):
        return segment
    raise InvalidRef(f"invalid version {segment!r} in the path: v<n>, or an id (64 hex digits)")


def of_artifact(name: str, selector: int | str) -> Ref:
    """The reference to a version of the artifact `name` by a selector (`Artifact.version`): a number, `v<n>`, an alias
    or a label, or an id (4 or more hex digits alone, which names a version by itself)."""
    if isinstance(selector, bool) or not isinstance(selector, (int, str)):
        raise TypeError("a version is a number, 'v<n>', an alias, a label, or an id")
    if isinstance(selector, int):
        return parse(f"{valid_name(name)}@v{selector}")
    if hex_alone(selector):
        return parse(selector)
    return parse(f"{name}@{selector}")


def exact(name: str, version: int) -> str:
    """`name@v<n>`: a numbered version, as messages and listings show it."""
    return f"{name}@v{version}"


# ------------------------------------------------------------------ short ids


def common(a: str, b: str) -> int:
    """How many leading digits two ids share."""
    count = 0
    for x, y in zip(a, b):
        if x != y:
            break
        count += 1
    return count


def short_id(id: str, others) -> str:
    """The short id of `id` among `others` (ids of the same project; `id` itself is skipped): the shortest prefix of at
    least SHORT digits that none of them starts with. Passing the nearest ids in sorted order is enough."""
    length = SHORT
    for other in others:
        if other and other != id:
            length = max(length, common(id, other) + 1)
    return id[: min(length, len(id))]


def shown(name: str, manifest: dict, short: str | None = None) -> str:
    """How a message names a version: `name@v<n>`, or `name (3f9a2c1)` while the source of truth has not numbered it."""
    if manifest.get("version") is not None:
        return exact(name, manifest["version"])
    return f"{name} ({short or manifest['id'][:SHORT]})"


def candidate(name: str, manifest: dict, short: str) -> dict:
    """A version as an id lookup lists it: {name, version, id, short_id, status}."""
    return {
        "name": name,
        "version": manifest.get("version"),
        "id": manifest["id"],
        "short_id": short,
        "status": manifest.get("status"),
    }


def describe(item: dict) -> str:
    """`name@v<n> (3f9a2c1)`, or `name (3f9a2c1, pending)` while the version has no number."""
    if item.get("version") is not None:
        return f"{item['name']}@v{item['version']} ({item['short_id']})"
    return f"{item['name']} ({item['short_id']}, pending)"


def pick(prefix: str, candidates: list[dict], where: str = "") -> dict:
    """The one version whose id starts with `prefix` among `candidates` ({name, version, id, short_id, status}):
    NotFound without one, Ambiguous (listing each as `name@v<n> (short id)`) with several."""
    unique = list({item["id"]: item for item in candidates if item["id"].startswith(prefix)}.values())
    if not unique:
        raise NotFound(f"no version with the id {prefix}{where}")
    if len(unique) > 1:
        listed = ", ".join(
            describe(item)
            for item in sorted(unique, key=lambda item: (item["name"], item["version"] is None, item["version"] or 0))
        )
        raise Ambiguous(f"the id {prefix} names {len(unique)} versions{where}: {listed}; give more digits", unique)
    return unique[0]
