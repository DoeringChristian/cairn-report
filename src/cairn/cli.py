"""Command line: `cairn build | serve | share | publish | pull | project | artifacts | versions | show | sync | status |
server | ui | token | migrate | finalize-study`.

Every command that talks to the store first tries to send what the outbox holds for it (quietly, and only
when something is queued); `sync` does that by hand and says what happened."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._version import __version__
from .config import load_config

def cmd_build(args) -> None:
    from .reports.build import build_all

    build_all(load_config(args.project), report=args.report)


def cmd_serve(args) -> None:
    from .reports.serve import serve

    serve(load_config(args.project), host=args.host, port=args.port, strict_port=args.strict_port, build=not args.no_build)


def cmd_share(args) -> None:
    from .reports.share import share

    share(load_config(args.project), report=args.report, build=not args.no_build, out=args.out)


def cmd_publish(args) -> None:
    from .client.publishing import parse_meta, publish_folder, publish_mappings, watch_folder
    from .reports.publishing import is_report_folder, publish_report

    config = with_options(load_config(args.project), args)
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


def with_options(config, args):
    """The config with the command's `--allow-http` applied."""
    from dataclasses import replace

    return replace(config, store={**config.store, "allow_http": True}) if getattr(args, "allow_http", False) else config


def cmd_pull(args) -> None:
    import shutil

    from .client.offline import pull
    from .client.open import open_store
    from .store import MARKER

    config = with_options(load_config(args.project), args)
    path = pull(open_store(config), args.ref, record=not args.no_record, offline=args.offline)
    if args.into is None:
        return print(path)
    target = args.into
    if path.is_file():
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target / path.name)
        return print(target / path.name)
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"{target} is not empty")
    shutil.copytree(path, target, symlinks=True, copy_function=shutil.copyfile, ignore=shutil.ignore_patterns(MARKER), dirs_exist_ok=True)
    print(target)


def open_objects(args):
    """The object API's Store for the configured store (no background uploader: commands flush synchronously)."""
    from .client.objects import Store
    from .client.open import open_store

    return Store(open_store(with_options(load_config(args.project), args)))


def report(line: str) -> None:
    print(f"cairn: {line}", file=sys.stderr, flush=True)


def flush_first(args) -> None:
    """Try to send the outbox of the configured store server before a command (see the module doc)."""
    from .client.outbox import flush_configured

    try:
        config = with_options(load_config(args.project), args)
    except (SystemExit, ValueError):
        return
    flush_configured(config, report)


def cmd_project(args) -> None:
    store = open_objects(args)
    if args.project_command == "list":
        rows = store.backend.overviews()
        for row in rows:
            print(f"{row['name']}\t{row.get('title') or row['name']}\t{row.get('artifacts', 0)} artifacts\t{(row.get('updated') or '')[:19]}")
        if not rows:
            print("no projects")
        return
    if args.project_command == "create":
        project = store.create_project(args.name, args.title, args.description or "")
        return print(f"project {project.name}: {project.title}")
    project = store.project(args.name)
    if args.project_command == "edit":
        if args.title is None and args.description is None:
            raise SystemExit("edit takes --title and/or --description")
        project.update(title=args.title, description=args.description)
    print(f"project      {project.name}")
    print(f"title        {project.title}")
    print(f"description  {project.description}")
    if args.project_command == "show":
        artifacts = project.artifacts()
        print(f"artifacts    {len(artifacts)}")
        for artifact in artifacts:
            latest = artifact.summary.get("latest")
            print(f"  {artifact.name}\t{f'v{latest}' if latest else '-'}\t{artifact.summary.get('versions', 0)} versions")


def cmd_artifacts(args) -> None:
    store = open_objects(args)
    artifacts = store.project(args.store_project).artifacts(prefix=args.prefix)
    for artifact in artifacts:
        summary = artifact.summary
        latest = summary.get("latest")
        aliases = ", ".join(f"{name}->v{number}" for name, number in sorted(summary.get("aliases", {}).items()) if name != "latest")
        print(f"{artifact.name}\t{f'v{latest}' if latest else '-'}\t{summary.get('versions', 0)} versions\t"
              f"{(summary.get('updated') or '')[:19]}" + (f"\t{aliases}" if aliases else ""))
    if not artifacts:
        print("no artifacts" + (f" starting with {args.prefix!r}" if args.prefix else ""))


def _size(files) -> str:
    return f"{len(files)} files, {sum(item.size for item in files) / 1e6:.1f} MB"


def cmd_versions(args) -> None:
    store = open_objects(args)
    versions = store.artifact(args.name).versions()
    for version in versions:
        number = f"v{version.number}" if version.number is not None else "-"
        print(f"{number}\t{version.status}\t{version.label or ''}\t{(version.created or '')[:19]}\t{version.uid}\t{_size(version.files)}")
    if not versions:
        print(f"no versions of {args.name}")


def cmd_show(args) -> None:
    import json

    from .store import StoreError

    store = open_objects(args)
    version = store.version(args.ref, offline=args.offline)
    aliases = [] if version.number is None else sorted(name for name, number in version.artifact.aliases.items() if number == version.number)
    print(f"ref          {version.ref}")
    print(f"uid          {version.uid}")
    print(f"status       {version.status}")
    print(f"label        {version.label or ''}")
    print(f"created      {version.created or ''}")
    if aliases:
        print(f"aliases      {', '.join(aliases)}")
    if version.source:
        print(f"source       {version.source.get('host', '')}:{version.source.get('folder', '')}")
    print(f"metadata     {json.dumps(version.metadata, sort_keys=True)}")
    print(f"inputs       {', '.join(version.inputs) or '-'}")
    try:
        consumers = version.consumers
    except StoreError as error:
        consumers = [f"(unknown: {error})"]
    print(f"consumers    {', '.join(consumers) or '-'}")
    print(f"files        {_size(version.files)}")
    for item in version.files:
        print(f"  {item.path}\t{'-> ' + item.link if item.link else item.size}")


def cmd_sync(args) -> None:
    from .client.outbox import Outbox

    store = open_objects(args)
    if not store.remote:
        return print("a local store has no outbox: publishing writes it directly")
    result = Outbox(store.backend).flush(report=lambda line: print(line, flush=True))
    waiting = [entry for entry in result["waiting"] if entry["state"] != "running"]
    running = [entry for entry in result["waiting"] if entry["state"] == "running"]
    if not (result["sent"] or result["rejected"] or result["waiting"]):
        return print("nothing to sync")
    if waiting:
        print(f"{len(waiting)} versions still queued: the store at {store.backend.url} does not answer", file=sys.stderr)
    if running:
        print(f"{len(running)} running versions are sent by the jobs that run them", flush=True)
    if waiting or result["rejected"]:
        raise SystemExit(1)


def cmd_status(args) -> None:
    from .client.outbox import all_entries

    config = load_config(args.project)
    url = (config.store.get("url") or "").rstrip("/")
    entries = [entry for entry in all_entries() if args.all or not url or entry.get("url") == url]
    if not url and not args.all:
        print("the configured store is a local folder: nothing is ever queued (--all shows every server's outbox)")
        return
    for entry in entries:
        number = f"v{entry['number']}" if entry.get("number") else "-"
        size = sum(item.get("size", 0) for item in entry.get("files", []))
        line = (f"{entry['state']}\t{entry['project']}:{entry['name']}\t{number}\t{entry['uid']}\t{entry['created'][:19]}\t"
                f"{len(entry.get('files', []))} files, {size / 1e6:.1f} MB")
        if args.all:
            line += f"\t{entry['url']}"
        if entry.get("error"):
            line += f"\t{entry['error']}"
        print(line)
    if not entries:
        print("nothing queued: every version is on the server")


def cmd_server(args) -> None:
    from .server import serve_store

    serve_store(args.root, host=args.host, port=args.port, max_blob=int(args.max_blob_gb * 1024**3), no_auth=args.no_auth,
                ui_port=None if args.no_ui else args.ui_port if args.ui_port is not None else args.port + 1 if args.port else 0,
                allow_network_fs=args.allow_network_fs)


def cmd_ui(args) -> None:
    from .ui import open_backend, serve_ui

    config = None
    try:
        config = with_options(load_config(args.project), args)
    except (SystemExit, ValueError):
        if args.store is None:
            raise
    backend = open_backend(args.store, config, no_auth=args.no_auth, allow_http=args.allow_http)
    serve_ui(backend, host=args.host, port=args.port, no_auth=args.no_auth, secure_cookie=not args.insecure_cookies)


def cmd_migrate(args) -> None:
    from .migrate import migrate

    for line in migrate(args.old_root, args.new_root):
        print(line)


def cmd_token_create(args) -> None:
    from .store.tokens import create_token

    token = create_token(args.root, args.project, write=args.write, name=args.name)
    print(f"Token: {token}")
    print(f"  {'read and write' if args.write else 'read-only'} access to project {args.project!r}; shown only now, stored hashed.")


def cmd_token_list(args) -> None:
    from .store.tokens import list_tokens

    rows = list_tokens(args.root)
    for row in rows:
        print(f"{row['name']}\t{row['project']}\t{'write' if row['write'] else 'read'}\t{row['created'][:19]}")
    if not rows:
        print("no scoped tokens")


def cmd_token_revoke(args) -> None:
    from .store.tokens import revoke_token

    revoke_token(args.root, args.name)
    print(f"revoked {args.name}")


def cmd_finalize_study(args) -> None:
    from .runs.hydra import finalize_study

    completed = finalize_study(args.study)
    print(f"{args.study}: {'completed; latest moved' if completed else 'failed (not every trial completed)'}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cairn", description="Cairn: an artifact store with a server and a UI, static reports from Markdown and run outputs, and a run logger.")
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    parser.add_argument("--project", type=Path, help="project root (default: nearest folder with cairn.toml, pixi.toml, or pyproject.toml)")
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

    publish.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local store server (sends the token unencrypted)")
    pull = commands.add_parser("pull", help="materialize an artifact version locally and print its path")
    pull.add_argument("ref", help="[project:]name[@vN|@alias|@label][//file], e.g. datasets/crops@v3")
    pull.add_argument("--into", type=Path, help="copy it into this folder instead of printing the cache path")
    pull.add_argument("--no-record", action="store_true", help="do not append the version to the inputs file ($CAIRN_INPUTS or .cairn-inputs.jsonl)")
    pull.add_argument("--offline", action="store_true", help="do not ask the store server: exact versions from the cache, aliases by their last known resolution")
    pull.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local store server (sends the token unencrypted)")
    pull.set_defaults(run=cmd_pull)

    project = commands.add_parser("project", help="list, create, show, or edit the store's projects")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("list", help="every project with its artifact count").set_defaults(run=cmd_project)
    for verb, text in (("create", "create a project (an existing one is left as is)"), ("show", "a project and its artifacts"),
                       ("edit", "change a project's title or description")):
        command = project_commands.add_parser(verb, help=text)
        command.add_argument("name")
        if verb != "show":
            command.add_argument("--title")
            command.add_argument("--description")
        command.add_argument("--allow-http", action="store_true", help=argparse.SUPPRESS)
        command.set_defaults(run=cmd_project)

    artifacts = commands.add_parser("artifacts", help="the artifacts of a project (latest version, versions, aliases)")
    artifacts.add_argument("--prefix", help="only names starting with this, e.g. reports/")
    artifacts.add_argument("--project", dest="store_project", help="the store project (default: [store] project)")
    artifacts.set_defaults(run=cmd_artifacts)
    versions = commands.add_parser("versions", help="every version of an artifact: number, status, label, created, uid, size")
    versions.add_argument("name", help="[project:]name")
    versions.set_defaults(run=cmd_versions)
    show = commands.add_parser("show", help="one version: metadata, files, inputs, consumers")
    show.add_argument("ref", help="[project:]name[@vN|@uid|@alias|@label]")
    show.add_argument("--offline", action="store_true", help="answer from the cache only")
    show.set_defaults(run=cmd_show)
    sync = commands.add_parser("sync", help="send the versions queued in the outbox to the store server now")
    sync.set_defaults(run=cmd_sync)
    status = commands.add_parser("status", help="the versions waiting in the outbox for the store server")
    status.add_argument("--all", action="store_true", help="every server's outbox, not only the configured one")
    status.set_defaults(run=cmd_status)
    for command in (artifacts, versions, show, sync, status):
        command.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local store server (sends the token unencrypted)")

    store_serve = commands.add_parser("server", help="serve a store folder (a new one gets an owner token); put an HTTPS reverse proxy in front")
    store_serve.add_argument("root", type=Path)
    store_serve.add_argument("--host", default="127.0.0.1")
    store_serve.add_argument("--port", type=int, default=8800)
    store_serve.add_argument("--max-blob-gb", type=float, default=50, help="largest accepted file")
    store_serve.add_argument("--no-auth", action="store_true", help="no token, everyone acts as the owner (local testing; loopback hosts only)")
    store_serve.add_argument("--ui-port", type=int, help="port of the UI started beside the store (default: --port + 1)")
    store_serve.add_argument("--no-ui", action="store_true", help="do not start `cairn ui` beside the store")
    store_serve.add_argument("--allow-network-fs", action="store_true",
                             help="serve a root on a network filesystem (NFS, Lustre, GPFS, SMB, ...): only if this server is its only writer")
    store_serve.set_defaults(run=cmd_server)
    migrate = commands.add_parser("migrate", help="copy an old hub root into a store (the old root is not modified)")
    migrate.add_argument("old_root", type=Path)
    migrate.add_argument("new_root", type=Path, help="a new folder or an existing store")
    migrate.set_defaults(run=cmd_migrate)
    token = commands.add_parser("token", help="project-scoped tokens").add_subparsers(dest="token_command", required=True)
    token_create = token.add_parser("create", help="print a new token for one project (shown once, stored hashed)")
    token_create.add_argument("root", type=Path)
    token_create.add_argument("--project", required=True)
    token_create.add_argument("--write", action="store_true", help="allow publishing (default: read-only)")
    token_create.add_argument("--name", help="a name to list and revoke it by")
    token_create.set_defaults(run=cmd_token_create)
    token_list = token.add_parser("list", help="list the scoped tokens (never their values)")
    token_list.add_argument("root", type=Path)
    token_list.set_defaults(run=cmd_token_list)
    token_revoke = token.add_parser("revoke", help="stop a token from working")
    token_revoke.add_argument("root", type=Path)
    token_revoke.add_argument("name")
    token_revoke.set_defaults(run=cmd_token_revoke)

    ui = commands.add_parser("ui", help="explore a store in a browser: its projects, reports, artifacts, versions, and lineage")
    ui.add_argument("--store", help="a store server URL or a store folder (default: [store] url or path in cairn.toml)")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8801)
    ui.add_argument("--no-auth", action="store_true", help="no login: use CAIRN_STORE_TOKEN / [store] token_file (loopback hosts only)")
    ui.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local store server (sends the token unencrypted)")
    ui.add_argument("--insecure-cookies", action="store_true", help="login over plain HTTP (SSH tunnel): the session cookie loses its Secure flag")
    ui.set_defaults(run=cmd_ui)

    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in argv:  # `publish ... -- <command...>`: everything after -- is the command to run
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    args.command = command
    if command and args.run is not cmd_publish:
        parser.error("only `publish` takes a command after --")
    if args.run in (cmd_publish, cmd_pull, cmd_project, cmd_artifacts, cmd_versions, cmd_show, cmd_status, cmd_build):
        flush_first(args)
    args.run(args)


if __name__ == "__main__":
    main()
