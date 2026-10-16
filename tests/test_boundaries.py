"""The layers of the package may import only the layers they are allowed to: parse every module's imports with `ast`.

store/            the repo core: the only code that changes a repo folder; no HTTP, no remote, no configuration
api.py            Cairn's errors raised from the caller's call (a decorator over the store's error types)
client/http.py    HTTP and folder scanning; client/remote.py the repo interface over HTTP
client/sync.py    the sync engine: outbox -> remote -> the repo; pure mechanism (no configuration, no printing)
client/repo.py    the two compositions (without and with a remote) behind one interface
client/open.py    the source-of-truth policy: the only place that reads [remote] url / [repo] allow_network_fs and the
                  filesystem type, and composes the layers below
term.py           how Cairn prints (one-line messages, hints, warnings, aligned blocks and tables): a leaf
the rest          the object API, publishing, drafts, runs, CLI, UI, reports: through what open.py composes
"""

import ast
from pathlib import Path

import pytest

import cairn

PACKAGE = Path(cairn.__file__).parent
LEAVES = {"config", "ranges", "formats", "hosts", "term", "_version"}  # import nothing from cairn
ALLOWED = {
    "store": {"store", "ranges", "formats"},  # the repo core: no configuration, no client
    "api": {"store"},  # the error boundary of the public calls
    "client": {
        "client",
        "term",
        "store",
        "api",
        "config",
        "ranges",
        "formats",
        "_version",
    },  # formats: the run logger writes what it reads
    "server": {"server", "term", "store", "config", "ranges", "hosts", "_version"},
    "ui": {"ui", "term", "store", "client", "config", "ranges", "hosts", "_version"},
    "reports": {"reports", "term", "store", "client", "config", "ranges", "formats", "_version"},
    "runs": {"runs", "term", "store", "client", "config", "formats", "_version"},
    "__init__": {"store", "client", "api", "_version"},  # the API (client lazily)
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
            names = [
                alias.name.split(".")[1] if alias.name.startswith("cairn.") else ""
                for alias in node.names
                if alias.name == "cairn" or alias.name.startswith("cairn.")
            ]
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
                names = [
                    alias.name if alias.name in ALLOWED or alias.name in LEAVES else "__init__" for alias in node.names
                ]
        for name in names:
            found.add((name, node.lineno))
    return found


MODULES = sorted(PACKAGE.rglob("*.py"))


def test_every_module_belongs_to_a_known_part():
    unknown = {part_of(path) for path in MODULES} - set(ALLOWED) - LEAVES - {"__main__"}
    assert not unknown


@pytest.mark.parametrize(
    "path",
    [path for path in MODULES if part_of(path) in ALLOWED and part_of(path) != "cli"],
    ids=lambda path: str(path.relative_to(PACKAGE)),
)
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


# ------------------------------------------------------------------ the layers inside client/

CLIENT = PACKAGE / "client"
CLIENT_LAYERS = {  # module -> the cairn modules it may import (anything in store/ is `store`)
    "http": {"term", "_version"},  # _version: the API version the paths carry (/api/v3)
    "remote": {"store", "term", "client.http", "_version"},  # and the handshake checks
    "sync": {"store", "term"},
    "repo": {"store", "ranges", "term", "client.http", "client.remote", "client.sync"},
    "open": {"config", "store", "term", "client.http", "client.remote", "client.repo"},
}


def module_targets(path: Path) -> set[str]:
    """The cairn modules a module imports anywhere in it: `store` for anything in store/, `client.<name>` inside client/."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.ImportFrom) and node.level:
            parts = (["client"] if node.level == 1 else []) + (node.module.split(".") if node.module else [])
            if not node.module:
                found.update(f"client.{alias.name}" for alias in node.names)
                continue
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("cairn."):
            parts = node.module.split(".")[1:]
        else:
            continue
        if parts[0] == "store":
            found.add("store")
        elif parts[0] == "client" and len(parts) > 1:
            found.add(f"client.{parts[1]}")
        else:
            found.add(parts[0])
    return found


@pytest.mark.parametrize("name", sorted(CLIENT_LAYERS))
def test_the_layers_below_the_policy_import_only_what_they_build_on(name):
    imported = module_targets(CLIENT / f"{name}.py")
    assert imported <= CLIENT_LAYERS[name], f"client/{name}.py imports {sorted(imported - CLIENT_LAYERS[name])}"


def test_the_sync_engine_is_pure_mechanism():
    tree = ast.parse((CLIENT / "sync.py").read_text())
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert "print" not in calls and "input" not in calls  # what happened comes back as a report
    assert not imports & {"threading", "atexit", "sys", "argparse", "tomllib"}  # no threads, no CLI, no configuration


def _code(path: Path) -> str:
    """A module's source without its docstrings and comments (what it does, not what it says)."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0] = ast.Pass()
    return ast.unparse(tree)


POLICY = {"client/open.py"}
NETWORK_FS = POLICY | {"store/fs.py", "store/local.py"}  # the core defines the check and refuses a database there


def test_only_the_policy_module_decides_on_the_remote_and_the_filesystem():
    import re

    for path in MODULES:
        name = str(path.relative_to(PACKAGE))
        code = _code(path)
        if name not in NETWORK_FS:
            assert "network_filesystem" not in code, f"{name} looks at the filesystem type"
        if name not in POLICY:
            assert not re.search(
                r"""repo(\.get\(|\[)\s*['"](remote|allow_network_fs)['"]""", code
            ), f"{name} reads [repo] remote or allow_network_fs"
        if name not in POLICY | {"client/repo.py", "client/__init__.py"}:
            for word in ("SyncedRepo", "StandaloneRepo", "NoCopies"):
                assert word not in code, f"{name} knows the composition {word} (only client/open.py picks it)"


def _graph() -> dict[str, set[str]]:
    """Module-level imports between cairn modules (dotted names), as an adjacency map."""
    names = {
        ".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts).removesuffix(".__init__"): path
        for path in MODULES
    }
    graph = {}
    for name, path in names.items():
        package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        edges = set()
        for node in ast.parse(path.read_text()).body:  # module level only: function-level imports run later
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                    target = ".".join([*base, *(node.module.split(".") if node.module else [])])
                elif node.module and node.module.startswith("cairn"):
                    target = node.module
                else:
                    continue
                candidates = [f"{target}.{alias.name}" for alias in node.names] if True else []
                edges.update(item for item in candidates if item in names)
                if target in names:
                    edges.add(target)
            elif isinstance(node, ast.Import):
                edges.update(alias.name for alias in node.names if alias.name in names)
        graph[name] = {
            edge for edge in edges if edge != name and not package.startswith(edge)
        }  # a parent package is loaded first anyway
    return graph


def test_no_import_cycles():
    graph, state, cycle = _graph(), {}, []

    def visit(node, trail):
        state[node] = "open"
        for edge in sorted(graph.get(node, ())):
            if state.get(edge) == "open":
                cycle.append(trail[trail.index(edge) :] + [edge])
            elif edge not in state:
                visit(edge, trail + [edge])
        state[node] = "done"

    for node in sorted(graph):
        if node not in state:
            visit(node, [node])
    assert not cycle, cycle[0]


def test_local_project_and_remote_implement_the_repo_interface():
    from cairn.client.remote import Remote
    from cairn.store.interface import METHODS
    from cairn.store.project import LocalProject

    for cls in (LocalProject, Remote):
        missing = [name for name in METHODS if name != "id" and not callable(getattr(cls, name, None))]
        assert not missing, f"{cls.__name__} lacks {missing}"


def test_no_layer_above_the_core_names_a_project_in_a_reference():
    """References name no project (`name@v3`): nothing builds or parses `project:name` outside the UI's JSON, which
    the app reads that way, and the core has no default project."""
    import re

    for path in MODULES:
        name = str(path.relative_to(PACKAGE))
        code = _code(path)
        if name in ("ui/server.py",):
            continue
        assert not re.search(r"""f["'][^"']*\{project\}:\{""", code), f"{name} formats a project:name reference"
        assert "ref.project" not in code, f"{name} reads a project from a reference"


def test_only_the_policy_module_parses_a_remote_url():
    for path in MODULES:
        name = str(path.relative_to(PACKAGE))
        if name in ("client/open.py", "client/http.py", "ui/backends.py"):
            continue
        assert "split_remote" not in _code(path), f"{name} parses a remote URL (client/open.py does)"


# The one parser of references (docs/architecture.md, invariant 6): store/refs.py takes references apart, nothing else.
# store/upgrade.py reads Cairn 0.14's references (`name@<uid>`, `name@v<n>`) once, to convert them: the one piece of
# compatibility code. Each allowed `isdigit` below is about something that is not a reference (a pid, a trial number,
# an octal escape in /proc/mounts).
PARSER = {"store/refs.py", "store/upgrade.py"}
ALLOWED_ISDIGIT = {
    "store/fs.py": 1,  # an octal escape in a mount table
    "runs/hydra.py": 6,  # multirun trial folders and their numbers (0, 1, ...)
    "ui/server.py": 1,  # the parent's pid in CAIRN_UI_PARENT
}
SELECTOR_PARSING = [
    (r"""\.(r?split|r?partition|index|find|rfind|rindex)\(\s*["']@["']""", "takes a reference apart at '@'"),
    (r"""["']@["']\s+in\b""", "looks for '@' in a reference"),
    (r"\b(VERSION|ID|HEX|UID|NUMBERED|PREFIX|WORD|LABEL)\.(full)?match\(", "matches a reference's parts itself"),
    (r"""\bint\(\s*\w*(sel|selector|number|segment|text|ref)\w*\s*\[\s*1\s*:\s*\]""", "reads v<n> itself"),
    (r"""f["'][^"']*@v\{int\(""", "turns digits into v<n>"),
]


def test_only_refs_takes_a_reference_apart():
    """No module but store/refs.py splits a reference at '@', matches its parts with the reference patterns, turns
    `v<n>` or digits into a number, or reads digits as a version (`isdigit`): one parser, no shortcut."""
    import re

    for path in MODULES:
        name = str(path.relative_to(PACKAGE))
        if name in PARSER or name.startswith("store/ids.py"):
            continue
        code = _code(path)
        for pattern, what in SELECTOR_PARSING:
            found = re.search(pattern, code)
            assert not found, f"{name} {what}: {found.group(0)!r} (store/refs.py is the one parser)"
        count = code.count("isdigit(")
        assert count <= ALLOWED_ISDIGIT.get(name, 0), f"{name} calls isdigit {count} times: digits are never a version"


WEB = PACKAGE.parent.parent / "web" / "src"
WEB_PARSER = "refs.ts"
WEB_PARSING = [
    (r"""\.(split|indexOf|lastIndexOf|partition)\(\s*["'`]@["'`]""", "takes a reference apart at '@'"),
    (r"""/[^/\n]*\[\^[^\]]*@[^\]]*\][^/\n]*/""", "matches a reference with a regex"),
    (r"""/[^/\n*]*@\(?v\\d""", "reads @v<n> with a regex"),
    (r"""/\^?\\d\+\$/""", "reads digits as a number"),
]


def _web_sources() -> list[Path]:
    files = sorted(WEB.rglob("*.ts")) + sorted(WEB.rglob("*.tsx"))
    return [*files, PACKAGE / "reports" / "template" / "report.js"]


def test_only_one_web_module_takes_a_reference_apart():
    """The web code has one module that parses references (web/src/refs.ts, mirroring store/refs.py); the app's other
    modules and the report page's script build URLs from fields, never from a reference's text."""
    import re

    sources = _web_sources()
    assert any(path.name == WEB_PARSER for path in sources), f"web/src/{WEB_PARSER} is missing"
    for path in sources:
        if path.name == WEB_PARSER:
            continue
        code = path.read_text()
        for pattern, what in WEB_PARSING:
            found = re.search(pattern, code)
            assert not found, f"{path.name} {what}: {found.group(0)!r} (web/src/{WEB_PARSER} is the one parser)"
