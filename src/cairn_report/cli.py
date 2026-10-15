"""Command line: `cairn-report build | serve | share | publish | push | pull | hub`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import load_config

def cmd_build(args) -> None:
    from .build import build_all

    build_all(load_config(args.project), report=args.report)


def cmd_serve(args) -> None:
    from .serve import serve

    serve(load_config(args.project), host=args.host, port=args.port, strict_port=args.strict_port, build=not args.no_build)


def cmd_share(args) -> None:
    from .share import share

    share(load_config(args.project), report=args.report, build=not args.no_build, out=args.out)


def cmd_push(args) -> None:
    from .push import push

    raise SystemExit(push(load_config(args.project), what=args.what, watch=args.watch, interval=args.interval,
                          command=args.command or None, url=args.url, allow_http=args.allow_http))


def cmd_publish(args) -> None:
    from .publish import is_report_folder, parse_meta, publish_folder, publish_mappings, publish_report, watch_folder

    config = load_config(args.project)
    meta, command = parse_meta(args.meta), args.command or None
    if args.folder is not None and is_report_folder(config, Path(args.folder)):
        if args.name or args.watch or command or args.label or args.alias or meta:
            raise SystemExit("a report folder publishes as reports/<slug> on its own: no NAME, --label, --alias, --meta, or --watch")
        return publish_report(config, Path(args.folder))
    if (args.folder is None) != (args.name is None):
        raise SystemExit("publish takes FOLDER NAME together, or neither to use [store] publish")
    if args.folder is None:
        if args.watch or command:
            raise SystemExit("--watch and -- command need FOLDER NAME")
        return publish_mappings(config, args.label, args.alias, meta)
    folder = Path(args.folder)
    if args.watch or command:
        raise SystemExit(watch_folder(config, folder, args.name, args.label, args.alias, meta, command, args.interval))
    publish_folder(config, folder, args.name, args.label, args.alias, meta)


def cmd_pull(args) -> None:
    from dataclasses import replace

    from .push import connect, project_name, pull

    config = load_config(args.project)
    hub = connect(config, args.url, args.allow_http)
    name = args.name or project_name(config)
    if args.into:  # restore into a separate folder, laid out like a project
        root = args.into.resolve()
        config = replace(config, project=root, reports_dir=root / "reports", output_dir=root / "generated" / "reports",
                         outputs_dir=root / (config.outputs_dir.relative_to(config.project)
                                             if config.outputs_dir.is_relative_to(config.project) else "outputs"))
    runs = None if args.runs is None else [run for run in args.runs if run]
    pull(hub, name, config, version=args.version, runs=runs, with_report=not args.runs_only, force=args.force)


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


def cmd_finalize_study(args) -> None:
    from .runs import finalize_study

    completed = finalize_study(args.study)
    print(f"{args.study}: {'completed; latest moved' if completed else 'failed (not every trial completed)'}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cairn-report", description="Static reports from Markdown and run outputs, a run logger, and an artifact hub.")
    parser.add_argument("--version", action="version", version=f"cairn-report {__version__}")
    parser.add_argument("--project", type=Path, help="project root (default: nearest folder with cairn-report.toml, pixi.toml, or pyproject.toml)")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build every report (or one)")
    build.add_argument("--report", help="build only this report (its slug)")
    build.set_defaults(run=cmd_build)
    serve = commands.add_parser("serve", help="build and serve the reports")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000, help="preferred port; the next free one is used if it is taken")
    serve.add_argument("--strict-port", action="store_true", help="fail instead of moving to the next free port")
    serve.add_argument("--no-build", action="store_true", help="serve the existing build output")
    serve.set_defaults(run=cmd_serve)

    share = commands.add_parser("share", help="write the reports as one self-contained page to publish unchanged (claude.ai artifacts)")
    share.add_argument("--report", help="share only this report (its slug)")
    share.add_argument("--out", type=Path, help="where to write the page (default: <output_dir>/../share/index.html)")
    share.add_argument("--no-build", action="store_true", help="share the existing build output")
    share.set_defaults(run=cmd_share)

    finalize = commands.add_parser("finalize-study", help="complete a multirun study whose launcher died, once every trial ended")
    finalize.add_argument("study", type=Path, help="the study folder, outputs/<run-name>/<timestamp>")
    finalize.set_defaults(run=cmd_finalize_study)

    publish = commands.add_parser("publish", help="store a folder as a new artifact version (or follow a running job's folder)")
    publish.add_argument("folder", nargs="?", help="the folder to publish (a symlink such as runs/x/latest is followed); a report folder reports/<slug> is built and published as reports/<slug>")
    publish.add_argument("name", nargs="?", help="artifact name, e.g. descent or datasets/crops")
    publish.add_argument("--label", help="version label (default: the resolved folder's name)")
    publish.add_argument("--alias", action="append", default=[], help="alias to point at the version (repeatable)")
    publish.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE", help="metadata (repeatable)")
    publish.add_argument("--watch", action="store_true", help="sync the folder into one running version until stopped, then complete it")
    publish.add_argument("--interval", type=float, help=argparse.SUPPRESS)
    publish.set_defaults(run=cmd_publish)

    def hub_client(command):
        command.add_argument("--url", help="hub URL (default: [hub] url or CAIRN_HUB_URL)")
        command.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local hub (sends the token unencrypted)")
        return command

    push = hub_client(commands.add_parser("push", help="push the report (a new version) and runs to the hub"))
    push.add_argument("--watch", action="store_true", help="keep pushing every --interval seconds until stopped")
    push.add_argument("--interval", type=float, help="seconds between pushes (default: [hub] interval or 30)")
    push.add_argument("what", nargs="?", choices=("all", "report", "runs"), default="all",
                      help="push the report and runs (all, the default), or only one of them")
    push.set_defaults(run=cmd_push)
    pull = hub_client(commands.add_parser("pull", help="bring the report (and runs) back from the hub"))
    pull.add_argument("--runs", nargs="*", metavar="RUN", help="also pull runs (all, or these run names / run paths)")
    pull.add_argument("--runs-only", action="store_true", help="pull runs only, not the report")
    pull.add_argument("--version", type=int, help="report version to pull (default: the latest)")
    pull.add_argument("--name", help="project name on the hub (default: [hub] project)")
    pull.add_argument("--into", type=Path, help="restore into this folder instead of the project")
    pull.add_argument("--force", action="store_true", help="replace local files that differ from the hub")
    pull.set_defaults(run=cmd_pull)

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

    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in argv:  # `push|publish ... -- <command...>`: everything after -- is the command to run
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    args.command = command
    if command and args.run not in (cmd_push, cmd_publish):
        parser.error("only `push` and `publish` take a command after --")
    args.run(args)


if __name__ == "__main__":
    main()
