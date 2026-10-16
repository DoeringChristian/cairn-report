"""How Cairn prints (a leaf: it imports nothing from cairn).

One line per event, the verb first (`published datasets/x@v1 (48d5843, 1 file, 12 KB)`); a hint, when the user must act,
on a second line indented by two spaces; errors as `cairn: <what went wrong>`; warnings as `warning: <text>` on stderr,
once. Structured output is an aligned key column (`block`) or a table sized to its content and the terminal (`table`).
Paths are shown relative to the working directory when inside it, else with `~` for the home directory (`short_path`).
Color only on a terminal without NO_COLOR: bold names, dim hints, yellow/red prefixes."""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import sys
from pathlib import Path

_CODES = {"bold": "1", "dim": "2", "yellow": "33", "red": "31"}
_warned: set[str] = set()


def colored(stream=None) -> bool:
    """Color for `stream` (stdout by default): a terminal, and NO_COLOR unset."""
    stream = sys.stdout if stream is None else stream
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def style(text: str, kind: str, stream=None) -> str:
    return f"\033[{_CODES[kind]}m{text}\033[0m" if colored(stream) else text


def debug() -> bool:
    return bool(os.environ.get("CAIRN_DEBUG"))


def hint(text: str, stream=None) -> str:
    """The indented second line of a message the user must act on."""
    return "  " + style(text, "dim", stream)


def warn(text: str, *, once: bool = True) -> None:
    """`warning: text` on stderr (once per process for the same text)."""
    if once:
        if text in _warned:
            return
        _warned.add(text)
    print(f"{style('warning:', 'yellow', sys.stderr)} {text}", file=sys.stderr, flush=True)


def error_line(text: str) -> str:
    """`cairn: text`, the form of every error the command line ends with (its paths shortened, see `short_paths`)."""
    for prefix in ("error: ", "ERROR: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = short_paths(text)
    text = text[len("cairn: "):] if text.startswith("cairn: ") else text
    return f"{style('cairn:', 'red', sys.stderr)} {text}"


def short_paths(text: str) -> str:
    """`text` with the absolute paths inside the working directory or the home directory in it shortened as
    `short_path` does (for a message made where nothing prints, such as the repo core's errors)."""
    try:
        here, home = Path.cwd(), Path.home()
    except (OSError, RuntimeError):
        return text
    for base, replacement in ((here, ""), (home, "~/")):
        for name in sorted({str(base), str(base.resolve())}, key=len, reverse=True):
            if name != "/":
                text = text.replace(name + "/", replacement)
    return text


def reason(text: str) -> str:
    """A client's `<url> not reachable: <cause>` as `remote not reachable (<cause>)`; other text as it is."""
    import re

    match = re.fullmatch(r"\S+ not reachable(?:: (.*)| \((.*)\))?", str(text))
    if not match:
        return str(text)
    detail = match.group(1) or match.group(2)
    return f"remote not reachable ({detail})" if detail else "remote not reachable"


def note(text: str) -> None:
    """A status line on stderr (where it does not mix with data on stdout)."""
    print(text, file=sys.stderr, flush=True)


def short_path(path) -> str:
    """`path` relative to the working directory when inside it, else absolute with `~` for the home directory."""
    try:
        target = Path(path).expanduser().absolute()
        here = Path.cwd()
    except (OSError, RuntimeError):
        return str(path)
    for base in {here, here.resolve()}:
        for candidate in {target, target.resolve() if target.exists() else target}:
            try:
                relative = candidate.relative_to(base)
            except ValueError:
                continue
            return str(relative) if str(relative) != "." else "."
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return str(target)
    for base in {home, home.resolve()}:
        try:
            return str(Path("~") / target.relative_to(base))
        except ValueError:
            continue
    return str(target)


def size(nbytes: float) -> str:
    """`12 KB`, `3.4 MB`, `0 B`."""
    value = float(nbytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            if unit == "B":
                return f"{value:.0f} B"
            return f"{value:.0f} {unit}" if value >= 10 else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def files(entries) -> str:
    """`1 file, 12 KB` of a list of entries with a `size` (attribute or key)."""
    entries = list(entries)
    total = sum((entry.get("size", 0) if isinstance(entry, dict) else getattr(entry, "size", 0)) or 0 for entry in entries)
    return f"{plural(len(entries), 'file')}, {size(total)}"


_CAUSES = (
    (ConnectionRefusedError, "connection refused"),
    (ConnectionResetError, "connection reset"),
    (ConnectionAbortedError, "connection aborted"),
    (socket.gaierror, "unknown host"),
    (TimeoutError, "timed out"),
    (socket.timeout, "timed out"),
    (ssl.SSLError, "TLS error"),
)


def cause(error: BaseException) -> str:
    """A few words for what went wrong (`connection refused`); the class and errno only with CAIRN_DEBUG=1."""
    if debug():
        return f"{type(error).__name__}: {error}"
    for kind, words in _CAUSES:
        if isinstance(error, kind):
            return words
    if isinstance(error, OSError) and error.strerror:
        return error.strerror[0].lower() + error.strerror[1:]
    text = str(error)
    return text or type(error).__name__


def block(rows, indent: str = "", gap: int = 2) -> list[str]:
    """`key  value` lines with the keys padded to one width (values are never wrapped)."""
    rows = [(str(key), "" if value is None else str(value)) for key, value in rows]
    width = max((len(key) for key, _ in rows), default=0)
    return [f"{indent}{key.ljust(width + gap)}{value}".rstrip() if value else f"{indent}{key}" for key, value in rows]


COLUMN = 15  # where the values of a startup block start (`Cairn server   ~/srv`, `  API          http://...`)


def field(key: str, value: str, column: int = COLUMN) -> str:
    """One line of a startup block: the key padded to `column`."""
    return f"{key.ljust(column - 1)} {value}"


def request_line(address: str, method: str, path: str, status) -> str:
    """The request log's line (the API's and the UI's): `HH:MM:SS  <ip>  GET /path  200`."""
    import time

    return f"{time.strftime('%H:%M:%S')}  {address}  {method} {path}  {status}"


def width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def ellipsize(text: str, room: int) -> str:
    return text if len(text) <= room else text[: max(room - 1, 1)] + "…"


def table(headers, rows, *, shrink=(), gap: int = 2) -> list[str]:
    """A table with columns sized to their content (an empty last column is left out). When it is wider than the terminal,
    the columns named in `shrink` (indexes of the safe ones to shorten with `…`, in that order) give up room, down to 8
    characters each."""
    headers = [str(item) for item in headers] if headers else None
    rows = [["" if cell is None else str(cell) for cell in row] for row in rows]
    lines = ([headers] if headers else []) + rows
    if not lines:
        return []
    count = max(len(line) for line in lines)
    while count > 1 and not any(len(row) >= count and row[count - 1] for row in rows):  # an empty last column
        count -= 1
        lines = [line[:count] for line in lines]
    sizes = [max((len(line[index]) for line in lines if index < len(line)), default=0) for index in range(count)]
    excess = sum(sizes) + gap * (count - 1) - width()
    for index in shrink:
        if excess <= 0:
            break
        if index < count and sizes[index] > 8:
            cut = min(excess, sizes[index] - 8)
            sizes[index] -= cut
            excess -= cut
    out = []
    for number, line in enumerate(lines):
        cells = [ellipsize(cell, sizes[index]) if index in shrink else cell for index, cell in enumerate(line)]
        text = (" " * gap).join(cell.ljust(sizes[index]) for index, cell in enumerate(cells)).rstrip()
        out.append(style(text, "bold") if headers and number == 0 else text)
    return out
