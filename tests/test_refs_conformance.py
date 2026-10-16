"""One parser: web/src/refs.ts takes every reference apart exactly as store/refs.py does (invariant 6).

`tests/refs_vectors.json` holds a few hundred reference strings, each with what `refs.parse` makes of it (its form,
id, name, selector, and path) or its refusal. One test checks that the file is what refs.py says now; the other runs
refs.ts on the same strings in Node (`web/scripts/refs-conformance.mjs`, also run by `npm run build`) and requires the
same answer for every one. Regenerate the file after a change to refs.py with `python tests/test_refs_conformance.py`.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cairn.store import refs

HERE = Path(__file__).parent
VECTORS = HERE / "refs_vectors.json"
SCRIPT = HERE.parent / "web" / "scripts" / "refs-conformance.mjs"

UID = "01a144ef-bd78-72e6-a221-1439c21f9ea2"
IDS = ["1234", "123", "1234567", "abcdef0", "ABCDEF1", "AbCd", "0" * 64, "f" * 64, "F" * 64, "f" * 65, "0" * 63, "cafe"]
IDS += ["abcdef012345" + "0" * 52, "c" * 63 + "1", "1234567" + "9" * 57, "12345678901234567890", "deadbeef", "g123"]
NAMES = ["data", "datasets/crops", "a", "runs/x", "v3", "V3", "x/1234", "1234/x", "data-1234", "Name", "a..b"]
NAMES += ["a/../b", "a/", "/a", "-a", "a_b.c-d", "reports/descent-study", "dätä", "名前", "x" * 200, "x" * 201]
NAMES += [UID, UID.upper(), f"x/{UID}", "cafe/x", "beef", "deadbeef-1", "v-1", "a b", "a\tb", "café"]
SELECTORS = ["", "v0", "v1", "v12", "v01", "v00", "v-1", "v+1", "V3", "V0", "v3x", "v3.zip", "v2-final", "vfoo"]
SELECTORS += ["val", "v", "V", "latest", "best", "1234", "cafe", "abc", "a@b", "2026-10-16_12-00-00", "sweep/3"]
SELECTORS += ["cafe/1", "x/v1", "x/V2", "x/", "/x", "a b", UID, UID.upper(), "w" * 128, "w" * 129, "ü", "-x", ".x"]
SELECTORS += ["x/v-1", "deadbeef", "DEADBEEF", "beef-1", "abc:d", "x\n"]
PATHS = [None, "x", "figures/a.png", "", "a//b", "./a", "a/..", "a/./b", "a\\b", "a b.png", "ü.png", "a\x01", "a:b"]
PATHS += ["/a", "a/", ".hidden", "..x", "x" * 300]
WRAPPED = [" ", "\t", "\n", "\r", "\x0b", "\x0c", " ", " ", "\x00", "\x1f"]


def strings() -> list[str]:
    found: list[str] = ["", "@", "@v1", "//x", "//", "a@", "a@@b", "a@v1@v2", "p:data", "p:data@v1", "a@v1//", "abcd//"]
    found += ["a//b//c", "abcd//a//b", "a@v1///x", " ", " ", "data@v1//a ", "名前@v1", "data@名前"]
    for id in IDS:
        found += [id, f"{id}//x", f"{id}//figures/a.png", f"{id}@v1", f"data@{id}"]
    for name in NAMES:
        found.append(name)
        for selector in SELECTORS:
            found.append(f"{name}@{selector}")
    for path in PATHS:
        tail = "" if path is None else f"//{path}"
        for head in ("data", "data@v3", "data@best", "abcdef0", "datasets/crops@sweep/3", UID, "data@v01"):
            found.append(head + tail)
    for blank in WRAPPED:
        for text in ("data", "abcdef0", "data@v1", "data@v1//x"):
            found += [blank + text, text + blank, text[:2] + blank + text[2:]]
    return list(dict.fromkeys(found))


def answer(text: str) -> dict:
    """What refs.parse makes of `text`: {form, id, name, selector, path}, or {error: true}."""
    try:
        ref = refs.parse(text)
    except refs.InvalidRef:
        return {"error": True}
    if ref.id is not None:
        form = "id"
    elif ref.selector is None:
        form = "latest"
    elif ref.version is not None:
        form = "version"
    else:
        form = "word"
    return {"form": form, "id": ref.id, "name": ref.name, "selector": ref.selector, "path": ref.path}


def vectors() -> list[dict]:
    return [{"text": text, **answer(text)} for text in strings()]


def test_the_vectors_are_what_refs_py_says_now():
    assert json.loads(VECTORS.read_text()) == vectors(), "refs.py changed: python tests/test_refs_conformance.py"
    assert len(vectors()) >= 300
    assert sum(1 for item in vectors() if item.get("error")) > 50 and sum(1 for item in vectors() if "form" in item)


def _node() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    major, minor = (
        int(part)
        for part in subprocess.run([node, "--version"], capture_output=True, text=True)
        .stdout.strip()
        .lstrip("v")
        .split(".")[:2]
    )
    return node if (major, minor) >= (23, 6) else None  # Node runs TypeScript (type stripping) from 23.6


def test_refs_ts_parses_every_vector_as_refs_py_does():
    node = _node()
    if node is None:
        pytest.skip("node 23.6 or newer is not installed: refs.ts cannot be run here (the vectors are checked above)")
    done = subprocess.run([node, str(SCRIPT), str(VECTORS)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
    assert "all agree" in done.stdout


if __name__ == "__main__":
    VECTORS.write_text(json.dumps(vectors(), indent=1) + "\n")
    print(f"wrote {VECTORS} ({len(vectors())} vectors)")
