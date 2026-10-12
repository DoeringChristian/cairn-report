"""Command line: `cairn-report init | new | build | serve`."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

from . import __version__
from .config import load_config

SAMPLES = Path(__file__).resolve().parent / "samples"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
RESERVED = {"overview", "knowledge", "fragments", "media"}
SAMPLE_DIRECTION = "local-fitting"  # the sample overview links to this slug


def copy_writable(source: Path, destination: Path) -> None:
    """Copy contents only, so files stay writable even from a read-only installation."""
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            if child.name != "__pycache__":
                copy_writable(child, destination / child.name)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def add_direction(reports: Path, slug: str) -> Path:
    if not SLUG.fullmatch(slug) or slug in RESERVED:
        raise SystemExit(f"invalid direction slug {slug!r}: lowercase letters, digits, hyphens; not {sorted(RESERVED)}")
    destination = reports / slug
    if destination.exists():
        raise SystemExit(f"{destination} already exists; reports are never overwritten")
    copy_writable(SAMPLES / "direction", destination)
    return destination


def cmd_init(args) -> None:
    config = load_config(args.project)
    reports = config.reports_dir
    if (reports / "report.md").exists():
        raise SystemExit(f"{reports / 'report.md'} already exists; use `cairn-report new <slug>` to add a direction")
    for name in ("report.md", "knowledge"):
        copy_writable(SAMPLES / "overview" / name, reports / name)
    direction = add_direction(reports, SAMPLE_DIRECTION)
    print(f"Overview: {reports / 'report.md'}")
    print(f"Sample direction: {direction / 'report.md'} (rename the folder and replace the sample content)")


def cmd_new(args) -> None:
    config = load_config(args.project)
    direction = add_direction(config.reports_dir, args.slug)
    print(f"Research direction: {direction / 'report.md'} (replace the sample content and summary)")


def cmd_build(args) -> None:
    from .build import build_all

    build_all(load_config(args.project), report=args.report)


def cmd_serve(args) -> None:
    from .serve import serve

    serve(load_config(args.project), host=args.host, port=args.port, strict_port=args.strict_port, build=not args.no_build)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cairn-report", description="Commentable research reports from Markdown and run outputs.")
    parser.add_argument("--version", action="version", version=f"cairn-report {__version__}")
    parser.add_argument("--project", type=Path, help="project root (default: nearest folder with cairn-report.toml, pixi.toml, or pyproject.toml)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="create the overview and a sample direction in the reports folder").set_defaults(run=cmd_init)
    new = commands.add_parser("new", help="add a research direction report")
    new.add_argument("slug")
    new.set_defaults(run=cmd_new)
    build = commands.add_parser("build", help="build every report (or one)")
    build.add_argument("--report", help="build only this report slug")
    build.set_defaults(run=cmd_build)
    serve = commands.add_parser("serve", help="build and serve the reports with commenting")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000, help="preferred port; the next free one is used if it is taken")
    serve.add_argument("--strict-port", action="store_true", help="fail instead of moving to the next free port")
    serve.add_argument("--no-build", action="store_true", help="serve the existing build output")
    serve.set_defaults(run=cmd_serve)

    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
