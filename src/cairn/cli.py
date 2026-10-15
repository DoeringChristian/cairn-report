"""Command line: `cairn build | serve | share | publish | pull | project | artifacts | versions | show | delete | retention |
gc | sync | status | prune | verify | server | ui | token | migrate | export-metadata | finalize-study`.

Every command that talks to the store first tries to send this machine's pending versions for it (quietly,
and only when something is pending); `sync` does that by hand, says what happened, and refreshes what the
replica knows about aliases."""

from __future__ import annotations

import argparse
import os
import re
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
    from .store import IncompleteRun

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
        return publish_mappings(config, args.label, args.alias, meta, args.allow_incomplete)
    folder = Path(args.folder)
    if args.watch or command:
        raise SystemExit(watch_folder(config, folder, args.name, args.label, args.alias, meta, command, args.interval))
    try:
        publish_folder(config, folder, args.name, args.label, args.alias, meta, args.allow_incomplete)
    except IncompleteRun as error:
        raise SystemExit(f"error: {error}")


def with_options(config, args):
    """The config with the command's `--allow-http` applied."""
    from dataclasses import replace

    return replace(config, store={**config.store, "allow_http": True}) if getattr(args, "allow_http", False) else config


def cmd_pull(args) -> None:
    import shutil

    from .client.offline import pull
    from .client.open import open_store

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
    shutil.copytree(path, target, symlinks=True, copy_function=shutil.copyfile, dirs_exist_ok=True)
    print(target)


def open_objects(args):
    """The object API's Store for the configured store (no background uploader: commands flush synchronously)."""
    from .client.objects import Store
    from .client.open import open_store

    return Store(open_store(with_options(load_config(args.project), args)))


def report(line: str) -> None:
    print(f"cairn: {line}", file=sys.stderr, flush=True)


def flush_first(args) -> None:
    """Try to send this machine's pending versions for the configured store server first (see the module doc)."""
    from .client.sync import flush_configured

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
    print(f"digest       {version.digest or '-'}")
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


def _store_errors(action):
    """Run `action`; a refusal from the store ends the command with its message instead of a traceback."""
    from .store import StoreError

    try:
        return action()
    except StoreError as error:
        raise SystemExit(f"cairn: {error}") from None


def cmd_delete(args) -> None:
    store = open_objects(args)
    if (args.ref is None) == (args.artifact is None):
        raise SystemExit("delete takes a REF (one version) or --artifact NAME (every version of it)")
    if args.artifact:
        result = _store_errors(lambda: store.artifact(args.artifact).delete(force=args.force, reason=args.reason))
    else:
        result = _store_errors(lambda: store.version(args.ref).delete(force=args.force, reason=args.reason))
    for ref in result["deleted"]:
        print(f"deleted {ref}")


def cmd_project_delete(args) -> None:
    store = open_objects(args)
    result = _store_errors(lambda: store.project(args.name).delete(force=args.force, reason=args.reason))
    for ref in result["deleted"]:
        print(f"deleted {ref}")
    print(f"deleted project {args.name}")


def _rule_text(rule: dict) -> str:
    parts = [f"keep_last={rule['keep_last']}" if rule.get("keep_last") is not None else "",
             f"keep_days={rule['keep_days']:g}" if rule.get("keep_days") is not None else "",
             "" if rule.get("keep_aliased", True) else "keep_aliased=false"]
    return f"{rule['prefix'] or '(every artifact)'}\t" + " ".join(part for part in parts if part)


def cmd_retention(args) -> None:
    store = open_objects(args)
    project = store.project(args.store_project)
    if args.retention_command == "set":
        if args.keep_last is None and args.keep_days is None:
            raise SystemExit("set takes --keep-last and/or --keep-days")
        _store_errors(lambda: project.set_retention(args.prefix or "", keep_last=args.keep_last, keep_days=args.keep_days,
                                                    keep_aliased=not args.no_keep_aliased))
    elif args.retention_command == "clear":
        rules = _store_errors(lambda: project.retention)
        _store_errors(lambda: setattr(project, "retention", [rule for rule in rules if rule["prefix"] != (args.prefix or "")]))
    rules = _store_errors(lambda: project.retention)
    for rule in rules:
        print(_rule_text(rule))
    if not rules:
        print(f"no retention rules in {project.name}: nothing is deleted by `cairn gc` but blobs no version names")


def cmd_gc(args) -> None:
    from .store import LocalStore
    from .store.gc import describe_bytes, parse_duration

    try:
        grace = parse_duration(args.grace)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if args.root is not None:
        if not (args.root / "store.json").is_file():
            raise SystemExit(f"{args.root} is not a store root (no store.json)")
        backend = LocalStore(args.root)
        result = _store_errors(lambda: backend.gc(dry_run=args.dry_run, grace=grace))
    else:
        store = open_objects(args)
        result = _store_errors(lambda: store.gc(dry_run=args.dry_run, grace=grace))
    verb = "would delete" if args.dry_run else "deleted"
    for ref in result["deleted"]:
        print(f"{verb} {ref}")
    for item in result["skipped"]:
        print(f"kept {item['ref']} (protected: {', '.join(item['reasons'])})")
    print(f"{verb} {len(result['deleted'])} versions; {'would remove' if args.dry_run else 'removed'} {result['blobs']} blobs "
          f"({describe_bytes(result['bytes'])} {'to free' if args.dry_run else 'freed'}) and {result['temporary']} stale uploads "
          f"({describe_bytes(result['temporary_bytes'])}); grace {grace / 3600:g} h")


def cmd_sync(args) -> None:
    store = open_objects(args)
    if not store.remote:
        return print("a local store needs no sync: publishing writes it directly")
    result = store.sync(report=lambda line: print(line, flush=True))
    waiting = [record for record in result["waiting"] if record["sync"] != "running"]
    running = [record for record in result["waiting"] if record["sync"] == "running"]
    if not (result["sent"] or result["rejected"] or result["waiting"]):
        print("nothing to sync")
    if waiting:
        print(f"{len(waiting)} versions still pending: the store at {store.backend.url} does not answer", file=sys.stderr)
    if running:
        print(f"{len(running)} running versions are sent by the jobs that run them", flush=True)
    if result.get("refreshed"):
        print(f"refreshed the aliases of {result['refreshed']} artifacts", flush=True)
    if waiting or result["rejected"]:
        raise SystemExit(1)


def _age(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.0f}{unit}"
    return f"{max(seconds, 0):.0f}s"


def cmd_status(args) -> None:
    import time

    from .client import replica

    config = load_config(args.project)
    url = (config.store.get("url") or "").rstrip("/")
    if not url and not args.all:
        print("the configured store is a local folder: nothing is ever pending (--all shows every server's versions)")
        return
    uid = replica.known_uid(url) if url else None
    rows = replica.records(all_roots=True) + (replica.synced_records() if args.all else [])
    rows = [row for row in rows if args.all or replica.belongs(row, url, uid)]
    now = time.time()
    for row in sorted(rows, key=lambda row: row["uid"]):
        if row["sync"] == "synced":
            manifest = row.get("manifest") or {}
            number, status, files = row.get("version"), manifest.get("status", "completed"), manifest.get("files", [])
            host, age = "", now - (row.get("accessed_at") or replica.epoch(row.get("accessed")))
        else:
            number, status, files = row.get("number"), row["status"], row.get("files", [])
            host, age = (row.get("owner") or {}).get("host", ""), now - replica.epoch(row.get("created"))
        size = sum(item.get("size", 0) for item in files)
        line = (f"{row['sync']}\t{row['project']}:{row['name']}\t{f'v{number}' if number else '-'}\t{row['uid']}\t{status}\t"
                f"{host}\t{_age(age)}\t{len(files)} files, {size / 1e6:.1f} MB")
        if row["sync"] == "running":
            line += f"\theartbeat {_age(now - replica.last_beat(row))} ago"
        if args.all:
            line += f"\t{row.get('url') or row.get('store') or ''}"
        if row.get("error"):
            line += f"\t{row['error']}"
        print(line)
    if not rows:
        print("nothing pending: the server has every version of this machine")


def cmd_prune(args) -> None:
    from .client.prune import describe_size, parse_age, parse_size, prune

    try:
        older = parse_age(args.older_than) if args.older_than else None
        size = parse_size(args.max_size) if args.max_size else None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if older is None and size is None:
        older = parse_age("30d")
    result = prune(older, size, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {len(result['versions'])} versions and {result['blobs']} blobs ({describe_size(result['freed'])}); "
          f"the cache holds {describe_size(result['size'])}")


def cmd_verify(args) -> None:
    from .store import LocalStore, StoreError

    if sum(bool(item) for item in (args.ref, args.cache, args.store is not None, args.all)) > 1:
        raise SystemExit("verify takes one of REF, --cache, --store, --all")
    if args.repair and (args.store is not None or args.ref):
        raise SystemExit("--repair only repairs the cache (--cache or --all)")
    try:
        if args.store:  # a store root given: no configuration needed
            if not (args.store / "store.json").is_file():
                raise SystemExit(f"{args.store} is not a store root (no store.json)")
            report = LocalStore(args.store, network_warning=False).verify()
        else:
            report = open_objects(args).verify(args.ref, cache=args.cache, store=args.store is not None, all=args.all, repair=args.repair)
    except (StoreError, ValueError) as error:
        raise SystemExit(f"error: {error}") from None
    for line in report.lines():
        print(line)
    print(report.summary(), file=sys.stderr if not report.ok else sys.stdout)
    if not report.ok:
        raise SystemExit(1)


def cmd_server(args) -> None:
    from .server import serve_store
    from .store.gc import parse_duration

    try:
        interval = parse_duration(args.gc_interval) if args.gc_interval else None
        grace = parse_duration(args.grace)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if interval is not None and interval <= 0:
        raise SystemExit("--gc-interval must be longer than 0")
    serve_store(args.root, host=args.host, port=args.port, max_blob=int(args.max_blob_gb * 1024**3), no_auth=args.no_auth,
                ui_port=None if args.no_ui else args.ui_port if args.ui_port is not None else args.port + 1 if args.port else 0,
                allow_network_fs=args.allow_network_fs, gc_interval=interval, gc_grace=grace)


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


def cmd_export_metadata(args) -> None:
    from .store import LocalStore

    root = args.root
    if not (root / "store.json").is_file():
        raise SystemExit(f"{root} is not a store root (no store.json)")
    counts = LocalStore(root).export_metadata(args.folder)
    for file, count in counts.items():
        print(f"{args.folder / file}  {count} rows")


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
    publish.add_argument("--allow-incomplete", action="store_true",
                         help="publish a folder whose run.json says the run is not completed (records metadata.run_status)")
    publish.add_argument("--interval", type=float, help=argparse.SUPPRESS)
    publish.set_defaults(run=cmd_publish)

    publish.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local store server (sends the token unencrypted)")
    pull = commands.add_parser("pull", help="materialize an artifact version locally and print its path")
    pull.add_argument("ref", help="[project:]name[@vN|@alias|@label][//file], e.g. datasets/crops@v3")
    pull.add_argument("--into", type=Path, help="copy it into this folder instead of printing the read-only path")
    pull.add_argument("--no-record", action="store_true", help="do not append the version to the inputs file ($CAIRN_INPUTS or .cairn-inputs.jsonl)")
    pull.add_argument("--offline", action="store_true", help="do not ask the store server: pending versions of this machine, exact versions from the cache, aliases by their last known resolution")
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
    project_delete = project_commands.add_parser("delete", help="delete a project (refused while it has versions, unless --force)")
    project_delete.add_argument("name")
    project_delete.add_argument("--force", action="store_true", help="delete its versions first, protected ones too (owner token)")
    project_delete.add_argument("--reason", help="why (kept in the tombstones)")
    project_delete.add_argument("--allow-http", action="store_true", help=argparse.SUPPRESS)
    project_delete.set_defaults(run=cmd_project_delete)

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
    delete = commands.add_parser("delete", help="delete a version (a tombstone: its number and uid are never used again) or a whole artifact")
    delete.add_argument("ref", nargs="?", help="[project:]name@vN (or @uid, @alias, @label)")
    delete.add_argument("--artifact", metavar="NAME", help="delete every version of this artifact ([project:]name)")
    delete.add_argument("--force", action="store_true",
                        help="delete protected versions too (aliased, inputs of other versions, running); owner token on a server")
    delete.add_argument("--reason", help="why (kept in the tombstone)")
    delete.set_defaults(run=cmd_delete)
    retention = commands.add_parser("retention", help="the retention rules `cairn gc` applies, per project or artifact prefix")
    retention_commands = retention.add_subparsers(dest="retention_command", required=True)
    for verb, text in (("show", "a project's rules"), ("set", "add or replace the rule for a prefix"), ("clear", "remove the rule for a prefix")):
        command = retention_commands.add_parser(verb, help=text)
        command.add_argument("store_project", metavar="project", help="the store project")
        if verb != "show":
            command.add_argument("--prefix", default="", help="artifact names starting with this, e.g. runs/ (default: every artifact)")
        if verb == "set":
            command.add_argument("--keep-last", type=int, help="keep the N newest versions of each artifact")
            command.add_argument("--keep-days", type=float, help="keep versions younger than D days")
            command.add_argument("--no-keep-aliased", action="store_true",
                                 help="aliased versions take a --keep-last place like any other (they are still never deleted)")
        command.add_argument("--allow-http", action="store_true", help=argparse.SUPPRESS)
        command.set_defaults(run=cmd_retention)
    gc = commands.add_parser("gc", help="apply the retention rules and remove blobs no version names (a store root, or the configured store)")
    gc.add_argument("root", nargs="?", type=Path, help="a store root on this machine (default: the configured store; a server needs the owner token)")
    gc.add_argument("--dry-run", action="store_true", help="only say what would be deleted and freed")
    gc.add_argument("--grace", default="24h", help="keep unreferenced blobs younger than this, e.g. 24h, 30m (default: 24h)")
    gc.set_defaults(run=cmd_gc)
    sync = commands.add_parser("sync", help="send this machine's pending versions to the store server now, and refresh known aliases")
    sync.set_defaults(run=cmd_sync)
    status = commands.add_parser("status", help="this machine's versions the store server does not have yet: pending, running, rejected")
    status.add_argument("--all", action="store_true", help="every version in the replica (synced ones in the cache too), of every server")
    status.set_defaults(run=cmd_status)
    prune = commands.add_parser("prune", help="trim the cache root by last access (pending and running versions are never touched)")
    prune.add_argument("--older-than", help="remove synced versions not pulled for this long, e.g. 30d, 12h (default: 30d)")
    prune.add_argument("--max-size", help="then remove the least recently pulled until the cache is under this, e.g. 20G")
    prune.add_argument("--dry-run", action="store_true", help="only say what would be removed")
    prune.set_defaults(run=cmd_prune)
    for command in (sync, status):
        command.add_argument("--data-dir", type=Path, help="the data root to use (default: $CAIRN_DATA_DIR, else <project>/.cairn/pending, else ~/.local/share/cairn): "
                             "send the pending versions kept there")
    for command in (artifacts, versions, show, sync, status, delete, gc):
        command.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local store server (sends the token unencrypted)")

    verify = commands.add_parser("verify", help="re-hash cached and pending versions, or a local store's blobs and versions; exit 1 on any problem")
    verify.add_argument("ref", nargs="?", help="[project:]name[@vN|@uid|@alias|@label]: one version")
    verify.add_argument("--cache", action="store_true", help="the pull cache and the pending versions (the default)")
    verify.add_argument("--store", nargs="?", const="", default=None, type=Path,
                        help="a local store: its blobs and versions (give the store root, or none for the configured local store)")
    verify.add_argument("--all", action="store_true", help="the cache, the pending versions, and the configured local store")
    verify.add_argument("--repair", action="store_true", help="cache only: remove broken cached versions and blobs, so the next pull downloads them again")
    verify.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local store server (sends the token unencrypted)")
    verify.set_defaults(run=cmd_verify)

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
    store_serve.add_argument("--gc-interval", help="collect garbage this often, e.g. 24h (retention rules, then unreferenced blobs; default: never)")
    store_serve.add_argument("--grace", default="24h", help="with --gc-interval: keep unreferenced blobs younger than this (default: 24h)")
    store_serve.set_defaults(run=cmd_server)
    migrate = commands.add_parser("migrate", help="copy an old hub root into a store (the old root is not modified)")
    migrate.add_argument("old_root", type=Path)
    migrate.add_argument("new_root", type=Path, help="a new folder or an existing store")
    migrate.set_defaults(run=cmd_migrate)
    export_metadata = commands.add_parser("export-metadata", help="write a store root's metadata as readable JSON Lines "
                                          "(projects, versions, the alias log, consumers), for backups and inspection")
    export_metadata.add_argument("root", type=Path)
    export_metadata.add_argument("folder", type=Path)
    export_metadata.set_defaults(run=cmd_export_metadata)
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
    from .store import cache

    cache.use_data_dir(getattr(args, "data_dir", None))
    if args.run in (cmd_publish, cmd_pull, cmd_project, cmd_artifacts, cmd_versions, cmd_show, cmd_status, cmd_build):
        flush_first(args)
    try:
        args.run(args)
    except Exception as error:  # an expected failure is one line; CAIRN_DEBUG=1 shows the traceback
        if os.environ.get("CAIRN_DEBUG") or not _expected(error):
            raise
        raise SystemExit(f"cairn: {error}") from None


def _expected(error: BaseException) -> bool:
    """Store, client, and report-build errors are messages for the user, not crashes."""
    from .store.errors import StoreError

    if isinstance(error, (StoreError, OSError)):
        return True
    module = type(error).__module__
    return module.startswith(("cairn.client", "cairn.reports")) and type(error).__name__.endswith("Error")


if __name__ == "__main__":
    main()
