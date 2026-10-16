"""Project configuration and finding the repo, as git finds `.git` and reads `.git/config`.

The project root is the nearest folder upward with a repo, `.cairn/` (it holds `repo.json`). Every setting lives in the
repo's `config.toml` (`.cairn/config.toml`, or `$CAIRN_DIR/config.toml`); everything has a default, so the file may
be empty. Paths in it are relative to the project root (the folder containing `.cairn/`):

    [remote]
    url = "https://cairn.example.org/my-study"   # a project on a `cairn server` (<server>/<project>), the source of
                                       # truth for numbers and aliases; `cairn remote set|remove` edits it

    [repo]
    exclude = ["*.pkl"]                # files never published: gitignore-style globs on project paths
    interval = 30                      # seconds between live syncs of a running version
    heartbeat_timeout = 600            # seconds without a heartbeat before a running version counts as dead
    allow_network_fs = false           # a repo on NFS/Lustre/...: only if one process on one host ever writes it

    [reports]
    dir = "reports"                    # one folder per report: reports/<slug>/report.md
    output_dir = ".cairn/build"        # build output, one folder per report slug (default: <repo>/build)

    [style]
    css = ["reports/style.css"]        # extra stylesheets, applied after the default style
    template_dir = "reports/template"  # files here replace the packaged page files by name

The token is never in the config: a client looks for $CAIRN_TOKEN, then `<repo>/token`, then `~/.config/cairn/token`
(client/open.py `find_token`).

Finding the repo is one rule (`locate`), which every caller uses:

    $CAIRN_DIR set        that folder is the repo, and its config.toml the config (the project root is still found
                          as below, else it is the working directory)
    otherwise             walk from the working directory upward; the first folder that holds `.cairn/repo.json` is
                          the project root, and `<root>/.cairn` its repo
    nothing found         the working directory is the project root and `<cwd>/.cairn` its repo: opening the repo
                          creates it (and says so); read-only commands refuse instead (`cairn init` makes one)

The repo ignores itself in git (a `.gitignore` of `*`; the project's own `.gitignore` is never edited) and holds the
metadata, the blobs, the outbox, drafts, and report builds (store/local.py lists the layout).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "config.toml"  # inside the repo
BASE_ENV = "CAIRN_DIR"
BASE_NAME = ".cairn"
SECTIONS = {
    "remote": {"url"},
    "repo": {"interval", "heartbeat_timeout", "allow_network_fs", "exclude"},
    "reports": {"dir", "output_dir"},
    "style": {"css", "template_dir"},
}
TEMPLATE = """\
# Cairn settings for this project; paths are relative to the project folder (the one holding .cairn/).
# Every key is optional. The token never goes here: CAIRN_TOKEN, .cairn/token, or ~/.config/cairn/token.

# [remote]                           # `cairn remote set <url>` writes this
# url = "https://cairn.example.org/my-study"

# [repo]
# exclude = ["*.pkl", "data/raw/"]   # files never published (gitignore-style)
"""


@dataclass(frozen=True)
class Config:
    project: Path
    reports_dir: Path
    output_dir: Path
    cairn_dir: Path  # the repo
    extra_css: tuple[Path, ...] = field(default_factory=tuple)
    template_dir: Path | None = None
    repo: dict = field(default_factory=dict)  # the [repo] table, and `remote`: [remote] url
    found: bool = True  # False: nothing was found, so the repo is the working directory's `.cairn`, made when opened
    located: str = (
        ""  # how the repo was found, for messages ("found from the working directory", "named by $CAIRN_DIR")
    )

    @property
    def source(self) -> Path:
        """The config file (it may not exist)."""
        return self.cairn_dir / CONFIG_FILE


@dataclass(frozen=True)
class Location:
    """Where the repo is: `root` the project root, `repo` the repo folder, `marked` whether the walk found a project
    root, `found` whether there is a repo to use without creating one in the working directory (found by the walk, or
    named by $CAIRN_DIR), `how` how it was found (for messages)."""

    root: Path
    repo: Path
    marked: bool
    found: bool
    how: str = ""


def _marked(folder: Path) -> bool:
    return (folder / BASE_NAME / "repo.json").is_file()


def locate(start: Path | str | None = None, root: Path | str | None = None) -> Location:
    """The one rule for finding the repo (see the module doc), from `start` (default: the working directory), or for
    the project root `root` when it is given (`--project`)."""
    here = start is None
    start = Path(start or Path.cwd()).resolve()
    if root is not None:
        folder, marked, how = Path(root).resolve(), True, "of the project folder given"
    else:
        folder = next((candidate for candidate in [start, *start.parents] if _marked(candidate)), None)
        marked = folder is not None
        how = (
            ("found from the working directory" if here else f"found from {start}")
            if marked
            else "the working directory's: no project above it"
        )
        folder = folder or start
    if os.environ.get(BASE_ENV):
        return Location(folder, Path(os.environ[BASE_ENV]).expanduser().resolve(), marked, True, "named by $CAIRN_DIR")
    return Location(folder, folder / BASE_NAME, marked, marked, how)


def find_project_root(start: Path | None = None) -> Path | None:
    """The project root the walk finds from `start` (default: the working directory), else None (`locate`)."""
    found = locate(start)
    return found.root if found.marked else None


def find_project(start: Path | None = None) -> Path:
    """The project root (`locate`): the one found, else the working directory."""
    return locate(start).root


def cairn_base(start: Path | None = None) -> Path | None:
    """The repo for the working directory (`locate`); None when there is none to use yet (nothing found)."""
    found = locate(start)
    return found.repo if found.found else None


def write_template(repo: Path) -> None:
    """The commented `config.toml` of a new repo (an existing file is left alone)."""
    try:
        with open(Path(repo) / CONFIG_FILE, "x") as handle:
            handle.write(TEMPLATE)
    except OSError:
        pass


def protect(base: Path | None, *paths: Path) -> None:
    """Make the base folder and write its `.gitignore` (`*`) if it lacks one; does nothing unless every given
    path lies inside the base. Called wherever a folder inside the base is about to be used. Never raises."""
    if base is None:
        return
    base = Path(base)
    if any(base not in [Path(path), *Path(path).parents] for path in paths):
        return
    try:
        marker = base / ".gitignore"
        if marker.exists():
            return
        base.mkdir(parents=True, exist_ok=True)
        with open(marker, "x") as handle:
            handle.write("*\n")
    except OSError:  # includes a racing writer: the file exists then
        pass


def load_config(project: Path | str | None = None) -> Config:
    """Read the repo's config.toml (if any) and resolve every path against the project root."""
    location = locate(root=project) if project else locate()
    root, base = location.root, location.repo
    source = base / CONFIG_FILE
    data = tomllib.loads(source.read_text()) if source.is_file() else {}
    for name, value in data.items():
        if name not in SECTIONS:
            raise ValueError(
                f"{source}: unknown key {name!r} (it takes the tables {', '.join(f'[{key}]' for key in SECTIONS)})"
            )
        if not isinstance(value, dict):
            raise ValueError(f"{source}: {name} must be a table, [{name}]")
        unknown = set(value) - SECTIONS[name]
        if unknown:
            raise ValueError(
                f"{source}: unknown [{name}] keys {sorted(unknown)} (it takes {', '.join(sorted(SECTIONS[name]))})"
            )
    remote, table = data.get("remote", {}), data.get("repo", {})
    reports, style = data.get("reports", {}), data.get("style", {})
    if not isinstance(remote.get("url", ""), str):
        raise ValueError(f"{source}: [remote] url must be text")
    if not isinstance(table.get("allow_network_fs", False), bool):
        raise ValueError(f"{source}: [repo] allow_network_fs must be true or false")
    if not isinstance(table.get("exclude", []), list) or not all(
        isinstance(item, str) for item in table.get("exclude", [])
    ):
        raise ValueError(f"{source}: [repo] exclude must be a list of glob patterns")
    for key, default in (("interval", 30), ("heartbeat_timeout", 600)):
        value = table.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{source}: [repo] {key} must be a positive number of seconds")

    def path(value: str | None, default: str | None) -> Path | None:
        if value is None and default is None:
            return None
        if not isinstance(value if value is not None else default, str):
            raise ValueError(f"{source}: {value!r} must be a path (text)")
        resolved = (root / (value if value is not None else default)).resolve()
        if root not in [resolved, *resolved.parents]:
            raise ValueError(f"{source}: {value!r} points outside the project")
        return resolved

    css = tuple(path(item, None) for item in style.get("css", []))
    for sheet in css:
        if not sheet.is_file() or sheet.suffix != ".css":
            raise ValueError(f"{source}: [style] css {sheet} is not a .css file")
    template_dir = path(style.get("template_dir"), None)
    if template_dir is not None and not template_dir.is_dir():
        raise ValueError(f"{source}: [style] template_dir {template_dir} is not a folder")
    settings = dict(table)
    if remote.get("url", "").strip():
        settings["remote"] = remote["url"].strip()
    return Config(
        project=root,
        reports_dir=path(reports.get("dir"), "reports"),
        output_dir=path(reports.get("output_dir"), None) or base / "build",
        cairn_dir=base,
        found=location.found,
        located=location.how,
        extra_css=css,
        template_dir=template_dir,
        repo=settings,
    )


# ------------------------------------------------------------------ `cairn remote set|remove`: editing [remote] url

_TABLE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]")
_URL = re.compile(r"^\s*url\s*=")


def set_remote(file: Path, url: str | None) -> None:
    """Write `[remote] url = "<url>"` into a config file (None: remove it), changing only those lines: other keys and
    comments stay as they are. A commented `# [remote]` of the template becomes the table."""
    file = Path(file)
    lines = file.read_text().splitlines() if file.is_file() else []
    start = next(
        (index for index, line in enumerate(lines) if (m := _TABLE.match(line)) and m.group(1) == "remote"), None
    )
    if start is None:
        if url is not None:
            commented = next((index for index, line in enumerate(lines) if re.match(r"^#\s*\[remote\]", line)), None)
            if commented is not None:
                lines[commented] = "[remote]"
                after = commented + 1
                if after < len(lines) and re.match(r"^#\s*url\s*=", lines[after]):
                    lines[after] = f"url = {_quote(url)}"
                else:
                    lines.insert(after, f"url = {_quote(url)}")
            else:
                lines += ([""] if lines and lines[-1].strip() else []) + ["[remote]", f"url = {_quote(url)}"]
    else:
        end = next((index for index in range(start + 1, len(lines)) if _TABLE.match(lines[index])), len(lines))
        keys = [index for index in range(start + 1, end) if _URL.match(lines[index])]
        if url is not None:
            if keys:
                lines[keys[0]] = f"url = {_quote(url)}"
            else:
                lines.insert(start + 1, f"url = {_quote(url)}")
        else:
            for index in reversed(keys):
                del lines[index]
                end -= 1
            if all(not line.strip() or line.lstrip().startswith("#") for line in lines[start + 1 : end]):
                del lines[start]
    tidy: list[str] = []
    for line in lines:  # no runs of blank lines where a table was taken out
        if line.strip() or (tidy and tidy[-1].strip()):
            tidy.append(line)
    text = "\n".join(tidy).rstrip("\n") + "\n" if any(line.strip() for line in tidy) else ""
    parsed = tomllib.loads(text)  # never write a file that does not read back as meant
    if (parsed.get("remote") or {}).get("url") != url:
        raise ValueError(f"{file}: could not write [remote] url; edit the file by hand")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
