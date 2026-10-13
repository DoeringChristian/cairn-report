"""Command line: `cairn-report init | new | build | serve | mlflow | push | fetch | hub`."""

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


def cmd_push(args) -> None:
    from .push import push

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    raise SystemExit(push(load_config(args.project), watch=args.watch, interval=args.interval,
                          command=command or None, url=args.url, allow_http=args.allow_http))


def cmd_fetch(args) -> None:
    from .push import connect, fetch

    config = None
    try:
        config = load_config(args.project)
    except ValueError:
        pass
    fetch(connect(config, args.url, args.allow_http), args.name, args.destination)


def cmd_mlflow(args) -> None:
    from .mlflow_backend import start_server, stop_on_sigterm, stop_server, tracking_uri

    stop_on_sigterm()
    config = load_config(args.project)
    settings = config.logger["mlflow"]
    if not settings.get("serve"):
        raise SystemExit("set [logger.mlflow] serve = true in cairn-report.toml to run the project's own MLflow server")
    process = start_server(settings, config.project, host=args.host)
    if process is None:
        raise SystemExit(f"an MLflow server already answers on {tracking_uri(settings)}")
    print(f"MLflow: {tracking_uri(settings)} (data in {config.project / settings.get('store', 'mlflow')}); Ctrl-C stops it", flush=True)
    try:
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_server(process)


def cmd_hub_init(args) -> None:
    from .hub import init_root

    token_file = init_root(args.root)
    print(f"Hub root: {args.root.resolve()}")
    print(f"Token: {token_file} (owner-only). Give clients CAIRN_HUB_TOKEN or a copy of this file.")
    print(f"Serve it with: cairn-report hub serve {args.root}")


def cmd_hub_serve(args) -> None:
    from .hub import serve_hub

    serve_hub(args.root, host=args.host, port=args.port, token_file=args.token_file,
              max_blob=int(args.max_blob_gb * 1024**3), secure_cookie=not args.insecure_cookies)


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

    tracker = commands.add_parser("mlflow", help="run the project's own MLflow tracking server ([logger.mlflow] serve = true)")
    tracker.add_argument("--host", default="127.0.0.1")
    tracker.set_defaults(run=cmd_mlflow)

    def hub_client(command):
        command.add_argument("--url", help="hub URL (default: [hub] url or CAIRN_HUB_URL)")
        command.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local hub (sends the token unencrypted)")
        return command

    push = hub_client(commands.add_parser("push", help="push runs and reports to the hub"))
    push.add_argument("--watch", action="store_true", help="keep pushing every --interval seconds until stopped")
    push.add_argument("--interval", type=float, help="seconds between pushes (default: [hub] interval or 30)")
    push.add_argument("command", nargs=argparse.REMAINDER, help="-- <command...>: run it, push while it runs and after it exits")
    push.set_defaults(run=cmd_push)
    fetch = hub_client(commands.add_parser("fetch", help="download a project's latest files from the hub"))
    fetch.add_argument("name", help="project name on the hub")
    fetch.add_argument("destination", type=Path)
    fetch.set_defaults(run=cmd_fetch)

    hub = commands.add_parser("hub", help="run a hub that stores many projects' runs and reports")
    hub_commands = hub.add_subparsers(dest="hub_command", required=True)
    hub_init = hub_commands.add_parser("init", help="create a hub data folder with a fresh token")
    hub_init.add_argument("root", type=Path)
    hub_init.set_defaults(run=cmd_hub_init)
    hub_serve = hub_commands.add_parser("serve", help="serve a hub (put an HTTPS reverse proxy in front)")
    hub_serve.add_argument("root", type=Path)
    hub_serve.add_argument("--host", default="127.0.0.1")
    hub_serve.add_argument("--port", type=int, default=8800)
    hub_serve.add_argument("--token-file", type=Path, help="default: <root>/token (or CAIRN_HUB_TOKEN)")
    hub_serve.add_argument("--max-blob-gb", type=float, default=50, help="largest accepted file")
    hub_serve.add_argument("--insecure-cookies", action="store_true", help="browser login over plain HTTP (SSH tunnel)")
    hub_serve.set_defaults(run=cmd_hub_serve)

    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
