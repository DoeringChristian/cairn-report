"""The manifest digest: one hash that names the content of a version.

    digest = "sha256:" + hex(SHA-256(canonical))

`canonical` is the UTF-8 encoding of a JSON array with one object per entry of the version's file list:

- a file:     {"path": <path>, "sha256": <64 lowercase hex>, "size": <integer bytes>}
- a symlink:  {"link": <target as stored>, "path": <path>}

The array is sorted by `path` (compared as strings of Unicode code points, which is the same order as their
UTF-8 bytes). Each object lists its keys in sorted order (as above), the whole value has no whitespace
(separators `,` and `:` only), and non-ASCII characters are written as they are (UTF-8), not as `\\uXXXX`;
control characters, `"` and `\\` are escaped as JSON requires (`\\n`, `\\t`, ..., else `\\u00xx` in lowercase
hex). In Python:

    json.dumps(sorted(items, key=lambda item: item["path"]), sort_keys=True, separators=(",", ":"),
               ensure_ascii=False).encode("utf-8")

Nothing else enters the digest: not the metadata, label, inputs, source, aliases, version number, id, or any
timestamp (including a file's `mtime`). Two versions with the same files therefore have the same digest,
wherever and whenever they were made. The empty file list hashes `[]`.

Example: one file `a.txt` holding "hi" (sha256 8f434346...) is

    [{"path":"a.txt","sha256":"8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4","size":2}]

A store computes the digest when a version completes and keeps it in the version's manifest as `digest`; a
client computes it for a version it is about to upload and the server checks it. A version that completed
before digests existed gets its digest when its store is first opened by this release. A running or failed
version has none.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PREFIX = "sha256:"


def canonical(entries) -> bytes:
    """The canonical bytes of a file list (see the module doc)."""
    items = []
    for entry in entries:
        if "link" in entry:
            items.append({"path": entry["path"], "link": entry["link"]})
        else:
            items.append({"path": entry["path"], "sha256": entry["sha256"], "size": entry["size"]})
    items.sort(key=lambda item: item["path"])
    return json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_digest(entries) -> str:
    """`sha256:<hex>` of a file list: entries as in a manifest (`path` with `sha256` and `size`, or `link`)."""
    return PREFIX + hashlib.sha256(canonical(entries)).hexdigest()


def sha256_file(path: Path | str) -> tuple[str, int]:
    """(hex SHA-256, size in bytes) of a file, read in 4 MB pieces."""
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while chunk := handle.read(4 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
