"""The parts of the package may import only the parts they are allowed to: parse every module's imports with `ast`."""

import ast
from pathlib import Path

import pytest

import cairn

PACKAGE = Path(cairn.__file__).parent
LEAVES = {"config", "ranges", "formats", "_version"}  # import nothing from cairn
ALLOWED = {
    "store": {"store"} | LEAVES,
    "client": {"client", "store", "config", "ranges", "formats", "_version"},  # formats: the run logger writes what it reads
    "server": {"server", "store", "config", "ranges", "_version"},
    "ui": {"ui", "store", "client", "config", "ranges", "_version"},
    "reports": {"reports", "store", "client", "config", "ranges", "formats", "_version"},
    "runs": {"runs", "store", "client", "config", "formats", "_version"},
    "migrate": {"migrate", "store", "formats", "_version"},  # the one-off hub conversion; knows the old report layout
    "__init__": {"store", "client", "_version"},  # the API (client lazily)
    "cli": set(),  # argument parsing: anything, lazily; checked separately
}


def part_of(path: Path) -> str:
    relative = path.relative_to(PACKAGE)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def targets(path: Path) -> set[tuple[str, int]]:
    """The cairn parts a module imports, as (part, line)."""
    relative = path.relative_to(PACKAGE).with_suffix("")
    package = list(relative.parts[:-1]) if relative.name != "__init__" else list(relative.parts[:-1])
    found = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[1] if alias.name.startswith("cairn.") else "" for alias in node.names
                     if alias.name == "cairn" or alias.name.startswith("cairn.")]
            names = [name or "__init__" for name in names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                module = [*base, *(node.module.split(".") if node.module else [])]
            elif node.module and (node.module == "cairn" or node.module.startswith("cairn.")):
                module = node.module.split(".")[1:]
            else:
                continue
            if module:
                names = [module[0]]
            else:  # `from . import x` / `from cairn import x`: x is a part or an attribute of the root package
                names = [alias.name if alias.name in ALLOWED or alias.name in LEAVES else "__init__" for alias in node.names]
        for name in names:
            found.add((name, node.lineno))
    return found


MODULES = sorted(PACKAGE.rglob("*.py"))


def test_every_module_belongs_to_a_known_part():
    unknown = {part_of(path) for path in MODULES} - set(ALLOWED) - LEAVES - {"__main__"}
    assert not unknown


@pytest.mark.parametrize("path", [path for path in MODULES if part_of(path) in ALLOWED and part_of(path) != "cli"],
                         ids=lambda path: str(path.relative_to(PACKAGE)))
def test_imports_stay_inside_the_allowed_parts(path):
    part = part_of(path)
    bad = sorted((name, line) for name, line in targets(path) if name not in ALLOWED[part])
    assert not bad, f"{path.relative_to(PACKAGE)} (part {part!r}) imports {bad}; allowed: {sorted(ALLOWED[part])}"


@pytest.mark.parametrize("path", [path for path in MODULES if part_of(path) in LEAVES], ids=lambda path: path.name)
def test_leaf_modules_import_nothing_from_cairn(path):
    assert not targets(path)


def test_cli_imports_the_parts_lazily():
    tree = ast.parse((PACKAGE / "cli.py").read_text())
    top = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    eager = {name for node in top for name, _ in targets_of(node)}
    assert eager <= {"_version", "config"}, eager


def targets_of(node):
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    out = set()
    if isinstance(node, ast.ImportFrom) and node.level and node.module:
        out.add((node.module.split(".")[0], node.lineno))
    elif isinstance(node, ast.ImportFrom) and node.level:
        out.update((alias.name, node.lineno) for alias in node.names)
    return out
