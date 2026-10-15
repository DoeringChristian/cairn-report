"""Command line: `cairn init | build | serve | share | publish | pull | project | artifacts | versions | show | alias | delete | retention |
gc | sync | status | prune | verify | server | ui | token | export-metadata | finalize-study | drafts`.

Every command that uses the repo first tries to send what waits for its remote, and once more at its end (quietly,
and only when something is pending; nothing without a remote); `sync` does that by hand, says what happened, and
mirrors the remote's aliases (`--watch` keeps going, for a login node)."""

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
    from .client.publishing import parse_meta, publish_folder, watch_folder
    from .reports.publishing import is_report_folder, publish_report
    from .store import IncompleteRun

    config = with_options(load_config(args.project), args)
    meta, command = parse_meta(args.meta), args.command or None
    if args.folder is not None and is_report_folder(config, Path(args.folder)):
        if args.name or args.watch or command or args.label or args.alias or meta:
            raise SystemExit("a report folder publishes as reports/<slug> on its own: no NAME, --label, --alias, --meta, or --watch")
        return publish_report(config, Path(args.folder))
    if args.folder is None or args.name is None:
        raise SystemExit("publish takes FOLDER NAME (or a report folder reports/<slug>)")
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

    return replace(config, repo={**config.repo, "allow_http": True}) if getattr(args, "allow_http", False) else config


def cmd_pull(args) -> None:
    import shutil

    from .client.open import open_repo

    config = with_options(load_config(args.project), args)
    path = open_repo(config).pull(args.ref, record=not args.no_record, offline=args.offline)
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
    """The object API's Repo for the project's repo (no background uploader: commands send at their start and end)."""
    from .client.objects import Repo
    from .client.open import open_repo

    return Repo(open_repo(with_options(load_config(args.project), args)))


def report(line: str) -> None:
    print(f"cairn: {line}", file=sys.stderr, flush=True)


def sync_quietly(args) -> None:
    """Try to send what waits for the repo's remote (see the module doc); never fails the command."""
    from .client.open import sync_now

    try:
        config = with_options(load_config(args.project), args)
    except (SystemExit, ValueError):
        return
    sync_now(config, report)


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
        number = f"v{version.number}" if version.number is not None else "pending"
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
    metadata = version.metadata
    config, overrides = metadata.pop("config", None), metadata.pop("overrides", None)
    if not isinstance(config, dict):
        metadata, config = ({**metadata, "config": config} if config is not None else metadata), None
    print(f"metadata     {json.dumps(metadata, sort_keys=True)}")
    if overrides is not None:
        print(f"overrides    {' '.join(map(str, overrides)) if isinstance(overrides, list) else json.dumps(overrides)}")
    if config is not None:
        print("config")
        for key, value in _flatten(config):
            print(f"  {key} = {value}")
    print(f"inputs       {', '.join(version.inputs) or '-'}")
    if not version.is_run:
        try:
            run = version.run
            if run is not None:
                print(f"run          {run.ref} ({run.status})")
                print(f"data         {', '.join(item.ref for item in version.upstream()) or '-'}")
        except StoreError as error:
            print(f"run          (unknown: {error})")
    try:
        consumers = version.consumers
    except StoreError as error:
        consumers = [f"(unknown: {error})"]
    print(f"consumers    {', '.join(consumers) or '-'}")
    print(f"files        {_size(version.files)}")
    for item in version.files:
        print(f"  {item.path}\t{'-> ' + item.link if item.link else item.size}")


def _flatten(value, prefix: str = "") -> list[tuple[str, str]]:
    """A nested config as (`a.b.c`, JSON value) rows; lists of plain values stay one row."""
    import json

    if isinstance(value, dict) and value:
        return [row for key, item in value.items() for row in _flatten(item, f"{prefix}.{key}" if prefix else str(key))]
    if isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value):
        return [row for index, item in enumerate(value) for row in _flatten(item, f"{prefix}.{index}" if prefix else str(index))]
    return [(prefix or "(value)", json.dumps(value))]


def cmd_drafts(args) -> None:
    from .client import drafts

    action = getattr(args, "drafts_command", None) or "list"
    if action == "list":
        found = drafts.drafts()
        for item in found:
            print(drafts.describe(item))
        if not found:
            print(f"no drafts in {drafts.drafts_root()}")
        return
    draft = drafts.load(args.draft)
    if draft.trial:
        raise SystemExit(f"cairn: {args.draft} is inside the draft {draft.folder}: give the draft itself")
    if action == "discard":
        draft.discard()
        print(f"discarded the draft {draft.name}@{draft.uid}")
        return
    if draft.state == "open":
        raise SystemExit(f"cairn: the draft {draft.uid} is still open (its process {draft.record.get('owner')} may write to it)")
    version = draft.publish(allow_incomplete=args.allow_incomplete)
    if version is None:
        print(f"the draft {draft.uid} waits for its inputs")
    elif version.number is None:
        print(f"pending {version.ref}: the remote does not answer; `cairn status` lists it, `cairn sync` sends it")
    else:
        print(f"published {version.ref} ({_size(version.files)})")


def _store_errors(action):
    """Run `action`; a refusal from the repo ends the command with its message instead of a traceback."""
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


def cmd_alias(args) -> None:
    store = open_objects(args)
    version = _store_errors(lambda: store.version(args.ref))
    _store_errors(lambda: version.artifact.alias(args.alias, version))
    print(f"{version.artifact.name}@{args.alias} -> v{version.number}")


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
    from .store import LocalRepo
    from .store.gc import describe_bytes, parse_duration

    try:
        grace = parse_duration(args.grace)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if args.root is not None:
        if not (args.root / "repo.json").is_file():
            raise SystemExit(f"{args.root} is not a repo (no repo.json)")
        backend = LocalRepo(args.root)
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


def cmd_init(args) -> None:
    import os

    from .config import BASE_NAME, protect
    from .store.local import REPO_FILE, repo_uid

    if args.path is not None:
        repo = Path(args.path).expanduser().resolve() / BASE_NAME
    elif os.environ.get("CAIRN_DIR"):
        repo = Path(os.environ["CAIRN_DIR"]).expanduser().resolve()
    else:
        repo = Path.cwd().resolve() / BASE_NAME
    if (repo / REPO_FILE).is_file():
        return print(f"the repo {repo} exists already")
    protect(repo)
    repo.mkdir(parents=True, exist_ok=True)
    repo_uid(repo)  # repo.json; the folder becomes private to its owner
    print(f"created the repo {repo}")


def cmd_sync(args) -> None:
    import time

    store = open_objects(args)
    config = load_config(args.project)
    while True:
        result = store.sync(report=lambda line: print(line, flush=True))
        if result["remote"] is None:
            return print("this repo has no remote ([repo] remote): there is nothing to sync")
        waiting = [record for record in result["waiting"] if record["sync"] != "running"]
        running = [record for record in result["waiting"] if record["sync"] == "running"]
        if not args.watch:
            break
        time.sleep(float(config.repo.get("interval", 30)))
    if not (result["sent"] or result["rejected"] or result["waiting"]):
        print("nothing to sync: the remote has every version of this repo")
    if waiting:
        print(f"{len(waiting)} versions still pending: the remote at {result['remote']} does not answer", file=sys.stderr)
    if running:
        print(f"{len(running)} running versions are sent by the processes that run them", flush=True)
    if result.get("refreshed"):
        print(f"mirrored the aliases of {result['refreshed']} artifacts", flush=True)
    if waiting or result["rejected"]:
        raise SystemExit(1)


def _age(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.0f}{unit}"
    return f"{max(seconds, 0):.0f}s"


def cmd_status(args) -> None:
    import time

    from .store.outbox import epoch

    store = open_objects(args)
    status = store.backend.status()
    for ref, why in status["reaped"]:  # opening the repo fails the versions whose process died
        print(f"failed {ref}: {why}")
    if status["remote"] is None:
        print("this repo has no remote: it is the source of truth, and nothing waits")
        return
    now = time.time()
    for row in status["entries"]:
        files = row.get("files", [])
        size = sum(item.get("size", 0) for item in files)
        number = row.get("number")
        line = (f"{row['sync']}\t{row['project']}:{row['name']}\t{f'v{number}' if number else '-'}\t{row['uid']}\t{row['status']}\t"
                f"{(row.get('owner') or {}).get('host', '')}\t{_age(now - epoch(row.get('created')))}\t{len(files)} files, {size / 1e6:.1f} MB")
        if row["sync"] == "running" and row.get("heartbeat") is not None:
            line += f"\theartbeat {_age(now - row['heartbeat'])} ago"
        if row.get("error"):
            line += f"\t{row['error']}"
        print(line)
    if not status["entries"]:
        print(f"nothing pending: the remote {status['remote']} has every version of this repo")


def cmd_prune(args) -> None:
    from .store.prune import describe_size, parse_age, parse_size

    try:
        older = parse_age(args.older_than) if args.older_than else None
        size = parse_size(args.max_size) if args.max_size else None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if older is None and size is None:
        older = parse_age("30d")
    result = open_objects(args).backend.prune(older, size, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {len(result['versions'])} read-only folders and {result['blobs']} blobs ({describe_size(result['freed'])}); "
          f"the repo holds {describe_size(result['size'])}")


def cmd_verify(args) -> None:
    from .store import StoreError

    if args.ref and args.repo is not None:
        raise SystemExit("verify takes a REF or --repo FOLDER, not both")
    try:
        if args.repo is not None:  # a repo folder on this machine (a server's): no configuration needed
            if not (args.repo / "repo.json").is_file():
                raise SystemExit(f"{args.repo} is not a repo (no repo.json)")
            from .client.open import open_folder

            report = open_folder(args.repo).verify(None, repair=args.repair)
        else:
            report = open_objects(args).verify(args.ref, repair=args.repair)
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
    root = args.root
    if root is None:  # the project's own repo (.cairn by default)
        from .client.open import served_root

        root = served_root(load_config(args.project))
    serve_store(root, host=args.host, port=args.port, max_blob=int(args.max_blob_gb * 1024**3), no_auth=args.no_auth,
                ui_port=None if args.no_ui else args.ui_port if args.ui_port is not None else args.port + 1 if args.port else 0,
                allow_network_fs=args.allow_network_fs, gc_interval=interval, gc_grace=grace)


def cmd_ui(args) -> None:
    from .ui import open_backend, serve_ui

    if args.remote and args.repo:
        raise SystemExit("ui takes --remote URL or --repo FOLDER, not both")
    config = None
    try:
        config = with_options(load_config(args.project), args)
    except (SystemExit, ValueError):
        if not (args.remote or args.repo):
            raise
    backend = open_backend(args.remote, args.repo, config, no_auth=args.no_auth, allow_http=args.allow_http)
    login = getattr(backend, "owner", "")  # the token the one-time login link logs in with
    if not login and not args.no_auth:
        from .client.open import client

        try:
            login = client(config, args.remote, allow_http=args.allow_http).token  # CAIRN_TOKEN or [repo] token_file
        except SystemExit:
            login = ""
    serve_ui(backend, host=args.host, port=args.port, no_auth=args.no_auth, secure_cookie=not args.insecure_cookies,
             login_token=login, open_browser=not args.no_browser)


def cmd_export_metadata(args) -> None:
    from .store import LocalRepo

    root = args.root
    if not (root / "repo.json").is_file():
        raise SystemExit(f"{root} is not a repo (no repo.json)")
    counts = LocalRepo(root, allow_network_fs=True).export_metadata(args.folder)  # only reads
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

    study = Path(args.study).resolve()
    completed = finalize_study(study)
    done = "completed and published (a draft)" if not study.exists() else "completed; latest moved"
    print(f"{args.study}: {done if completed else 'failed (not every trial completed)'}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cairn", description="Cairn: a repo of versioned artifacts for research (runs, their results, reports built from pinned artifacts), with an optional remote, a server, and a UI.")
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    parser.add_argument("--project", type=Path, help="project root (default: the nearest folder upward with .cairn/repo.json or cairn.toml, else here)")
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

    drafts = commands.add_parser("drafts", help="the open and kept drafts (folders under .cairn/drafts/ that become versions)")
    drafts.set_defaults(run=cmd_drafts)
    drafts_commands = drafts.add_subparsers(dest="drafts_command")
    drafts_commands.add_parser("list", help="every draft: name, uid, state, age, size, path").set_defaults(run=cmd_drafts)
    drafts_publish = drafts_commands.add_parser("publish", help="publish a kept draft as the next version of its artifact")
    drafts_publish.add_argument("draft", help="the draft's folder or uid (or a unique start of it)")
    drafts_publish.add_argument("--allow-incomplete", action="store_true",
                                help="publish although its run.json says the run did not complete (records metadata.run_status)")
    drafts_publish.set_defaults(run=cmd_drafts)
    drafts_discard = drafts_commands.add_parser("discard", help="delete a draft without publishing it")
    drafts_discard.add_argument("draft", help="the draft's folder or uid (or a unique start of it)")
    drafts_discard.set_defaults(run=cmd_drafts)

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

    publish.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local remote (sends the token unencrypted)")
    pull = commands.add_parser("pull", help="materialize an artifact version locally and print its path")
    pull.add_argument("ref", help="[project:]name[@vN|@alias|@label][//file], e.g. datasets/crops@v3")
    pull.add_argument("--into", type=Path, help="copy it into this folder instead of printing the read-only path")
    pull.add_argument("--no-record", action="store_true", help="do not append the version to the inputs file ($CAIRN_INPUTS or .cairn-inputs.jsonl)")
    pull.add_argument("--offline", action="store_true", help="do not ask the remote: the repo's pending versions, its copies, and its mirrored aliases")
    pull.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local remote (sends the token unencrypted)")
    pull.set_defaults(run=cmd_pull)

    project = commands.add_parser("project", help="list, create, show, or edit the repo's projects")
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
    artifacts.add_argument("--project", dest="store_project", help="the project (default: [repo] project)")
    artifacts.set_defaults(run=cmd_artifacts)
    versions = commands.add_parser("versions", help="every version of an artifact: number, status, label, created, uid, size")
    versions.add_argument("name", help="[project:]name")
    versions.set_defaults(run=cmd_versions)
    show = commands.add_parser("show", help="one version: metadata, files, inputs, consumers")
    show.add_argument("ref", help="[project:]name[@vN|@uid|@alias|@label]")
    show.add_argument("--offline", action="store_true", help="do not ask the remote: answer from the repo only")
    show.set_defaults(run=cmd_show)
    alias = commands.add_parser("alias", help="point an alias (best, paper, ...) at a completed version; latest moves by itself")
    alias.add_argument("ref", help="[project:]name@vN (or @uid, @label, @another-alias)")
    alias.add_argument("alias", help="the alias to set or move")
    alias.set_defaults(run=cmd_alias)
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
        command.add_argument("store_project", metavar="project", help="the project")
        if verb != "show":
            command.add_argument("--prefix", default="", help="artifact names starting with this, e.g. runs/ (default: every artifact)")
        if verb == "set":
            command.add_argument("--keep-last", type=int, help="keep the N newest versions of each artifact")
            command.add_argument("--keep-days", type=float, help="keep versions younger than D days")
            command.add_argument("--no-keep-aliased", action="store_true",
                                 help="aliased versions take a --keep-last place like any other (they are still never deleted)")
        command.add_argument("--allow-http", action="store_true", help=argparse.SUPPRESS)
        command.set_defaults(run=cmd_retention)
    gc = commands.add_parser("gc", help="apply the retention rules and remove blobs no version names (the project's repo, its remote, or a repo folder)")
    gc.add_argument("root", nargs="?", type=Path, help="a repo folder on this machine (default: the project's repo, or its remote: the owner token)")
    gc.add_argument("--dry-run", action="store_true", help="only say what would be deleted and freed")
    gc.add_argument("--grace", default="24h", help="keep unreferenced blobs younger than this, e.g. 24h, 30m (default: 24h)")
    gc.set_defaults(run=cmd_gc)
    init = commands.add_parser("init", help="create a repo, .cairn/, in a folder (default: here; $CAIRN_DIR if set); "
                               "a repo that exists is left as it is")
    init.add_argument("path", nargs="?", type=Path, help="the folder to create .cairn/ in (default: the working directory)")
    init.set_defaults(run=cmd_init)
    sync = commands.add_parser("sync", help="send the repo's pending versions to its remote now, and mirror the remote's aliases")
    sync.add_argument("--watch", action="store_true", help="keep going every [repo] interval seconds (a login node's sender)")
    sync.set_defaults(run=cmd_sync)
    status = commands.add_parser("status", help="the repo's versions its remote does not have yet: pending, running, rejected")
    status.set_defaults(run=cmd_status)
    prune = commands.add_parser("prune", help="trim the repo's read-only folders, and contents only copies of the remote's versions need, by last access")
    prune.add_argument("--older-than", help="remove folders not pulled for this long, e.g. 30d, 12h (default: 30d)")
    prune.add_argument("--max-size", help="then remove the least recently pulled until the repo is under this, e.g. 20G")
    prune.add_argument("--dry-run", action="store_true", help="only say what would be removed")
    prune.set_defaults(run=cmd_prune)
    for command in (artifacts, versions, show, sync, status, delete, gc, prune):
        command.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local remote (sends the token unencrypted)")

    verify = commands.add_parser("verify", help="re-hash what the repo holds (blobs, versions, read-only folders, the outbox); exit 1 on any problem")
    verify.add_argument("ref", nargs="?", help="[project:]name[@vN|@uid|@alias|@label]: one version")
    verify.add_argument("--repo", type=Path, help="a repo folder on this machine instead of the project's (a server's)")
    verify.add_argument("--repair", action="store_true", help="remove what can be made again (broken read-only folders; broken contents of a remote's versions), never the only copy")
    verify.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local remote (sends the token unencrypted)")
    verify.set_defaults(run=cmd_verify)

    store_serve = commands.add_parser("server", help="serve a repo to other machines (a new one gets an owner token); put an HTTPS reverse proxy in front")
    store_serve.add_argument("root", type=Path, nargs="?", help="the repo folder (default: the project's, .cairn; created if new)")
    store_serve.add_argument("--host", default="127.0.0.1")
    store_serve.add_argument("--port", type=int, default=8800, help="the port; a taken port is an error (clients' [repo] remote names it)")
    store_serve.add_argument("--max-blob-gb", type=float, default=50, help="largest accepted file")
    store_serve.add_argument("--no-auth", action="store_true", help="no token, everyone acts as the owner (local testing; loopback hosts only)")
    store_serve.add_argument("--ui-port", type=int, help="port of the UI started beside the server (default: --port + 1)")
    store_serve.add_argument("--no-ui", action="store_true", help="do not start `cairn ui` beside the server")
    store_serve.add_argument("--allow-network-fs", action="store_true",
                             help="serve a repo on a network filesystem (NFS, Lustre, GPFS, SMB, ...): only if this server is its only writer")
    store_serve.add_argument("--gc-interval", help="collect garbage this often, e.g. 24h (retention rules, then unreferenced blobs; default: never)")
    store_serve.add_argument("--grace", default="24h", help="with --gc-interval: keep unreferenced blobs younger than this (default: 24h)")
    store_serve.set_defaults(run=cmd_server)
    export_metadata = commands.add_parser("export-metadata", help="write a repo's metadata as readable JSON Lines "
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

    ui = commands.add_parser("ui", help="explore the repo in a browser: its projects, reports, artifacts, versions (pending ones too), and lineage")
    ui.add_argument("--remote", help="a remote's URL instead of the project's repo (e.g. from a laptop)")
    ui.add_argument("--repo", type=Path, help="a repo folder on this machine instead of the project's (a server's)")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8801)
    ui.add_argument("--no-auth", action="store_true", help="no login: use CAIRN_TOKEN / [repo] token_file for a remote (loopback hosts only)")
    ui.add_argument("--allow-http", action="store_true", help="allow plain HTTP to a non-local remote (sends the token unencrypted)")
    ui.add_argument("--insecure-cookies", action="store_true", help="login over plain HTTP (SSH tunnel): the session cookie loses its Secure flag")
    ui.add_argument("--no-browser", action="store_true", help="do not open the one-time login link in a browser")
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
    if args.run in LOOKING_COMMANDS and not getattr(args, "repo", None):  # they look at a repo; they never make one
        from .client.open import existing_repo

        existing_repo(load_config(args.project))
    syncing = args.run in REPO_COMMANDS and not getattr(args, "offline", False)  # --offline: never ask the remote
    if syncing:
        sync_quietly(args)
    try:
        args.run(args)
    except Exception as error:  # an expected failure is one line; CAIRN_DEBUG=1 shows the traceback
        if os.environ.get("CAIRN_DEBUG") or not _expected(error):
            raise
        raise SystemExit(f"cairn: {error}") from None
    finally:
        if syncing:
            sync_quietly(args)


LOOKING_COMMANDS = {cmd_status, cmd_verify, cmd_prune, cmd_sync}
REPO_COMMANDS = {cmd_publish, cmd_pull, cmd_project, cmd_project_delete, cmd_artifacts, cmd_versions, cmd_show, cmd_alias, cmd_delete,
                 cmd_retention, cmd_gc, cmd_status, cmd_prune, cmd_verify, cmd_build, cmd_drafts, cmd_finalize_study}


def _expected(error: BaseException) -> bool:
    """Repo, client, and report-build errors are messages for the user, not crashes."""
    from .store.errors import StoreError

    if isinstance(error, (StoreError, OSError)):
        return True
    module = type(error).__module__
    return module.startswith(("cairn.client", "cairn.reports")) and type(error).__name__.endswith("Error")


if __name__ == "__main__":
    main()
