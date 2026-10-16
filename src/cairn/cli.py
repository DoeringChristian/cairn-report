"""Command line: `cairn init | remote | build | serve | share | publish | pull | artifacts | versions | show | alias | delete |
retention | gc | sync | status | prune | verify | server | ui | export-metadata | finalize-study | drafts`.

A repo holds one project, so no command names one. Settings live in the repo's `.cairn/config.toml` (config.py).
References are `name[@vN|@uid|@<short id>|@alias|@label]`, or a bare short id; versions are shown with their short id.

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

    build_all(load_config(args.directory), report=args.report)


def cmd_serve(args) -> None:
    from .reports.serve import serve

    serve(
        load_config(args.directory),
        host=args.host,
        port=args.port,
        strict_port=args.strict_port,
        build=not args.no_build,
    )


def cmd_share(args) -> None:
    from .reports.share import share

    share(load_config(args.directory), report=args.report, build=not args.no_build, out=args.out)


def cmd_publish(args) -> None:
    from .client.publishing import parse_meta, publish_folder, watch_folder
    from .reports.publishing import is_report_folder, publish_report
    from .store import IncompleteRun

    config = load_config(args.directory)
    meta, command = parse_meta(args.meta), args.command or None
    if args.folder is not None and is_report_folder(config, Path(args.folder)):
        if args.name or args.watch or command or args.label or args.alias or meta:
            raise SystemExit(
                "cairn: a report folder publishes as reports/<slug>: no NAME, --label, --alias, --meta, or --watch"
            )
        return publish_report(config, Path(args.folder))
    if args.folder is None or args.name is None:
        raise SystemExit("cairn: publish takes FOLDER NAME (or a report folder reports/<slug>)")
    folder = Path(args.folder)
    if args.watch or command:
        raise SystemExit(watch_folder(config, folder, args.name, args.label, args.alias, meta, command, args.interval))
    try:
        publish_folder(config, folder, args.name, args.label, args.alias, meta, args.allow_incomplete)
    except IncompleteRun as error:
        raise SystemExit(f"cairn: {error}")


def cmd_pull(args) -> None:
    import shutil

    from .client.open import open_repo

    config = load_config(args.directory)
    path = open_repo(config).pull(args.ref, record=not args.no_record, offline=args.offline)
    if args.into is None:
        return print(path)
    target = args.into
    if path.is_file():
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target / path.name)
        return print(target / path.name)
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"cairn: {term().short_path(target)} is not empty")
    shutil.copytree(path, target, symlinks=True, copy_function=shutil.copyfile, dirs_exist_ok=True)
    print(target)


def open_objects(args):
    """The object API's Repo for the project's repo (no background uploader: commands send at their start and end)."""
    from .client.objects import Repo
    from .client.open import open_repo

    return Repo(open_repo(load_config(args.directory)))


def term():
    """How Cairn prints (term.py), imported when a command prints."""
    from . import term as module

    return module


def report(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def sync_quietly(args) -> None:
    """Try to send what waits for the repo's remote (see the module doc); never fails the command."""
    from .client.open import sync_now

    try:
        config = load_config(args.directory)
    except (SystemExit, ValueError):
        return
    sync_now(config, report)


def cmd_artifacts(args) -> None:
    store = open_objects(args)
    artifacts = store.artifacts(prefix=args.prefix)
    rows = []
    for artifact in artifacts:
        summary = artifact.summary
        latest = summary.get("latest")
        aliases = ", ".join(
            f"{name}->v{number}" for name, number in sorted(summary.get("aliases", {}).items()) if name != "latest"
        )
        rows.append(
            [
                artifact.name,
                f"v{latest}" if latest else "-",
                summary.get("versions", 0),
                _when(summary.get("updated")),
                aliases,
            ]
        )
    for line in term().table(["NAME", "LATEST", "VERSIONS", "UPDATED", "ALIASES"], rows, shrink=(4,)) if rows else []:
        print(line)
    if not artifacts:
        print("no artifacts" + (f" starting with {args.prefix!r}" if args.prefix else ""))


def _size(files) -> str:
    return term().files(files)


def _when(stamp) -> str:
    """A time as `2026-10-16 11:01` (the repo's ISO time, shortened)."""
    return (stamp or "")[:16].replace("T", " ")


def cmd_versions(args) -> None:
    store = open_objects(args)
    versions = store.artifact(args.name).versions()
    rows = []
    for version in versions:
        files = version.files
        rows.append(
            [
                f"v{version.number}" if version.number is not None else "pending",
                version.short_id,
                version.status,
                version.label or "",
                _when(version.created),
                term().plural(len(files), "file"),
                term().size(sum(item.size for item in files)),
                version.uid,
            ]
        )
    for line in (
        term().table(["VERSION", "ID", "STATUS", "LABEL", "CREATED", "FILES", "SIZE", "UID"], rows, shrink=(7, 3))
        if rows
        else []
    ):
        print(line)
    if not versions:
        print(f"no versions of {args.name}")


def cmd_show(args) -> None:
    import json

    from .store import StoreError

    store = open_objects(args)
    version = store.version(args.ref, offline=args.offline)
    aliases = (
        []
        if version.number is None
        else sorted(name for name, number in version.artifact.aliases.items() if number == version.number)
    )
    rows = [
        ("ref", version.ref),
        ("id", version.short_id),
        ("uid", version.uid),
        ("status", version.status),
        ("digest", version.digest or "-"),
        ("label", version.label or ""),
        ("created", version.created or ""),
    ]
    if aliases:
        rows.append(("aliases", ", ".join(aliases)))
    if version.source:
        folder = version.source.get("folder", "")
        rows.append(("source", f"{version.source.get('host', '')}:{term().short_path(folder) if folder else ''}"))
    metadata = version.metadata
    config, overrides = metadata.pop("config", None), metadata.pop("overrides", None)
    if not isinstance(config, dict):
        metadata, config = ({**metadata, "config": config} if config is not None else metadata), None
    rows.append(("metadata", json.dumps(metadata, sort_keys=True)))
    if overrides is not None:
        rows.append(
            ("overrides", " ".join(map(str, overrides)) if isinstance(overrides, list) else json.dumps(overrides))
        )
    tail = [("inputs", ", ".join(version.inputs) or "-")]
    if not version.is_run:
        try:
            run = version.run
            if run is not None:
                tail.append(("run", f"{run.ref} ({run.short_id}, {run.status})"))
                tail.append(("data", ", ".join(item.ref for item in version.upstream()) or "-"))
        except StoreError as error:
            tail.append(("run", f"(unknown: {error})"))
    try:
        consumers = version.consumers
    except StoreError as error:
        consumers = [f"(unknown: {error})"]
    tail.append(("consumers", ", ".join(consumers) or "-"))
    tail.append(("files", _size(version.files)))
    keys = rows + tail + [("overrides", "")]
    column = max(len(key) for key, _ in keys) + 4  # one value column for the whole block
    for key, value in rows:
        print(term().field(key, value, column).rstrip())
    if config is not None:
        print("config")
        for line in term().block([(f"  {key}", f"= {value}") for key, value in _flatten(config)], gap=1):
            print(line)
    for key, value in tail:
        print(term().field(key, value, column).rstrip())
    listed = [(f"  {item.path}", "-> " + item.link if item.link else term().size(item.size)) for item in version.files]
    for line in term().block(listed):
        print(line)


def _flatten(value, prefix: str = "") -> list[tuple[str, str]]:
    """A nested config as (`a.b.c`, JSON value) rows; lists of plain values stay one row."""
    import json

    if isinstance(value, dict) and value:
        return [row for key, item in value.items() for row in _flatten(item, f"{prefix}.{key}" if prefix else str(key))]
    if isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value):
        return [
            row
            for index, item in enumerate(value)
            for row in _flatten(item, f"{prefix}.{index}" if prefix else str(index))
        ]
    return [(prefix or "(value)", json.dumps(value))]


def cmd_drafts(args) -> None:
    from .client import drafts

    action = getattr(args, "drafts_command", None) or "list"
    if action == "list":
        found = drafts.drafts()
        rows = [drafts.describe(item).split("\t") for item in found]
        for row in rows:
            row[6] = term().short_path(row[6])
        for line in (
            term().table(["NAME", "ID", "UID", "STATE", "AGE", "SIZE", "FOLDER"], rows, shrink=(2,)) if rows else []
        ):
            print(line)
        if not found:
            print(f"no drafts in {term().short_path(drafts.drafts_root())}")
        return
    draft = drafts.load(args.draft)
    if draft.trial:
        raise SystemExit(
            f"cairn: {args.draft} is inside the draft {term().short_path(draft.folder)}: give the draft itself"
        )
    if action == "discard":
        draft.discard()
        print(f"discarded the draft {draft.name} ({draft.short_id})")
        return
    if draft.state == "open":
        raise SystemExit(
            f"cairn: the draft {draft.name} ({draft.short_id}) is still open (its process {draft.record.get('owner')} "
            "may write to it)"
        )
    version = draft.publish(allow_incomplete=args.allow_incomplete)
    if version is None:
        print(f"waiting {draft.name} ({draft.short_id}): its inputs are not published yet")
    elif version.number is None:
        print(
            f"pending {version.artifact.name} ({version.short_id}): remote not reachable\n"
            + term().hint("run `cairn sync` to send it now")
        )
    else:
        print(f"published {version.ref} ({version.short_id}, {_size(version.files)})")


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
        raise SystemExit("cairn: delete takes a REF (one version) or --artifact NAME (every version of it)")
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
    print(f"{version.artifact.name}@{args.alias} -> v{version.number} ({version.short_id})")


def _rule_text(rule: dict) -> tuple[str, str]:
    parts = [
        f"keep_last={rule['keep_last']}" if rule.get("keep_last") is not None else "",
        f"keep_days={rule['keep_days']:g}" if rule.get("keep_days") is not None else "",
        "" if rule.get("keep_aliased", True) else "keep_aliased=false",
    ]
    return rule["prefix"] or "(every artifact)", " ".join(part for part in parts if part)


def cmd_retention(args) -> None:
    repo = open_objects(args)
    if args.retention_command == "set":
        if args.keep_last is None and args.keep_days is None:
            raise SystemExit("cairn: set takes --keep-last and/or --keep-days")
        _store_errors(
            lambda: repo.set_retention(
                args.prefix or "",
                keep_last=args.keep_last,
                keep_days=args.keep_days,
                keep_aliased=not args.no_keep_aliased,
            )
        )
    elif args.retention_command == "clear":
        rules = _store_errors(lambda: repo.retention)
        _store_errors(
            lambda: setattr(repo, "retention", [rule for rule in rules if rule["prefix"] != (args.prefix or "")])
        )
    rules = _store_errors(lambda: repo.retention)
    for line in term().block([_rule_text(rule) for rule in rules]):
        print(line)
    if not rules:
        print("no retention rules (`cairn gc` removes only blobs no version names)")


def cmd_gc(args) -> None:
    from .store import LocalRepo
    from .store.gc import describe_bytes, parse_duration

    try:
        grace = parse_duration(args.grace)
    except ValueError as error:
        raise SystemExit(f"cairn: {error}") from None
    if args.root is not None:
        if not (args.root / "repo.json").is_file():
            raise SystemExit(f"cairn: {term().short_path(args.root)} is not a repo (no repo.json)")
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
    print(
        f"{verb} {len(result['deleted'])} versions; {'would remove' if args.dry_run else 'removed'} {result['blobs']} blobs "
        f"({describe_bytes(result['bytes'])} {'to free' if args.dry_run else 'freed'}), {result['temporary']} stale uploads "
        f"({describe_bytes(result['temporary_bytes'])}); grace {grace / 3600:g} h"
    )


def make_repo(repo: Path) -> None:
    """A new repo folder: `.gitignore`, `repo.json` (the folder becomes private to its owner), the config template."""
    from .config import protect, write_template
    from .store.local import repo_uid

    protect(repo)
    repo.mkdir(parents=True, exist_ok=True)
    repo_uid(repo)
    write_template(repo)


def cmd_init(args) -> None:
    import os

    from .config import BASE_NAME
    from .store.local import REPO_FILE

    if args.path is not None:
        repo = Path(args.path).expanduser().resolve() / BASE_NAME
    elif os.environ.get("CAIRN_DIR"):
        repo = Path(os.environ["CAIRN_DIR"]).expanduser().resolve()
    else:
        repo = Path.cwd().resolve() / BASE_NAME
    if (repo / REPO_FILE).is_file():
        return print(f"the repo {term().short_path(repo)} exists already")
    make_repo(repo)
    print(f"created the repo {term().short_path(repo)} (settings: {term().short_path(repo / 'config.toml')})")


def cmd_remote(args) -> None:
    from .client.open import remote_url, valid_remote
    from .config import set_remote

    config = load_config(args.directory)
    action = getattr(args, "remote_command", None)
    current = remote_url(config)
    if action is None:
        return print(f"remote  {current}" if current else "no remote")
    if action == "set":
        url = valid_remote(args.url)
        if not (config.cairn_dir / "repo.json").is_file():
            make_repo(config.cairn_dir)
            print(f"created the repo {term().short_path(config.cairn_dir)}")
        set_remote(config.source, url)
        print(f"remote  {url}")
        _check_token(load_config(args.directory), url)
        if url != current:
            print(term().hint("run `cairn sync` to sync now (versions numbered here get the remote's numbers)"))
        return
    if current is None:
        return print("no remote")
    set_remote(config.source, None)
    print(f"removed the remote {current}; pending versions wait until one is set again")


def _check_token(config, url: str) -> None:
    """After `cairn remote set`: ask the remote whether the token is accepted; when there is none or it is refused,
    ask for it (on a terminal) and keep it in the repo's token file (owner-only), where the next commands find it."""
    import getpass

    from .client.http import ClientError
    from .client.open import client, find_token, remote_parts

    server = remote_parts(url)[0]
    token, source, _ = find_token(config)
    hint, short = term().hint, term().short_path

    def answer(candidate: str) -> str:
        try:
            client(config, server, token=candidate, timeout=10).request("GET", "/api/v2/repo")
            return "ok"
        except ClientError as error:
            if error.unreachable:
                return "unreachable"
            if error.status in (401, 403):
                return "refused"
            raise SystemExit(f"cairn: the remote answered {error.status}: {error}") from None

    def where(place) -> str:
        return place if place is None or place.startswith("$") else short(place)

    state = answer(token)
    if state == "ok":
        return print("token   accepted" + (f" (from {where(source)})" if source else " (none needed)"))
    if state == "unreachable":
        return print(f"token   not checked: {server} not reachable (the next command checks it)")
    told = f"token from {where(source)} refused by {server}" if token else f"no token for {server}"
    if source == "$CAIRN_TOKEN":
        return print(
            f"warning: {told}\n"
            + hint("$CAIRN_TOKEN wins over every token file: set it to the server's token or unset it")
        )
    target = Path(config.cairn_dir) / "token"
    if not sys.stdin.isatty():
        return print(
            f"warning: {told}\n" + hint(f"Copy the server's token file to {short(target)}, or set CAIRN_TOKEN")
        )
    print(f"{told}")
    for _ in range(3):
        candidate = getpass.getpass(f"Token for {server} (empty skips): ").strip()
        if not candidate:
            return print(f"skipped\n" + hint(f"copy the server's token to {short(target)} later, or set CAIRN_TOKEN"))
        state = answer(candidate)
        if state == "ok":
            temporary = target.with_name(f".token.{os.getpid()}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(candidate + "\n")
            os.replace(temporary, target)
            return print(f"token   accepted, saved to {short(target)} (owner-only)")
        if state == "unreachable":
            return print(f"warning: {server} not reachable; nothing saved")
        print("refused; try again")
    raise SystemExit(f"cairn: no token accepted by {server}; nothing saved")


def cmd_sync(args) -> None:
    import time

    store = open_objects(args)
    config = load_config(args.directory)
    while True:
        result = store.sync(report=lambda line: print(line, flush=True))
        if result["remote"] is None:
            return print("no remote: nothing to sync\n" + term().hint("`cairn remote set <server>/<project>` sets one"))
        waiting = [record for record in result["waiting"] if record["sync"] != "running"]
        running = [record for record in result["waiting"] if record["sync"] == "running"]
        if not args.watch:
            break
        time.sleep(float(config.repo.get("interval", 30)))
    if not (result["sent"] or result["rejected"] or result["waiting"]):
        print("nothing to sync")
    if waiting:
        print(f"pending {term().plural(len(waiting), 'version')}: {result['remote']} not reachable", file=sys.stderr)
    if running:
        print(f"running {term().plural(len(running), 'version')} (their processes send them)", flush=True)
    if result.get("refreshed"):
        print(f"mirrored the aliases of {term().plural(result['refreshed'], 'artifact')}", flush=True)
    if waiting or result["rejected"]:
        raise SystemExit(1)


def _age(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.0f}{unit}"
    return f"{max(seconds, 0):.0f}s"


def cmd_status(args) -> None:
    import time

    from .store import refs
    from .store.outbox import epoch

    store = open_objects(args)
    status = store.backend.status()
    for ref, why in status["reaped"]:  # opening the repo fails the versions whose process died
        print(f"failed {ref}: {why}")
    if status["remote"] is None:
        print("no remote: this repo is the source of truth; nothing waits")
        return
    now = time.time()
    rows = []
    for row in status["entries"]:
        files = row.get("files", [])
        number = row.get("number")
        note = []
        if row["sync"] == "running" and row.get("heartbeat") is not None:
            note.append(f"heartbeat {_age(now - row['heartbeat'])} ago")
        if row.get("error"):
            note.append(str(row["error"]))
        host = (row.get("owner") or {}).get("host", "")
        rows.append(
            [
                row["sync"],
                row["name"],
                f"v{number}" if number else "-",
                refs.default_short_id(row["uid"]),
                row["status"],
                host.split(".")[0],
                _age(now - epoch(row.get("created"))),
                term().files(files),
                "; ".join(note),
            ]
        )
    for line in (
        term().table(["SYNC", "NAME", "VERSION", "ID", "STATUS", "HOST", "AGE", "FILES", "NOTE"], rows, shrink=(5, 1))
        if rows
        else []
    ):
        print(line)
    if not status["entries"]:
        print(f"nothing pending ({status['remote']} has every version)")


def cmd_prune(args) -> None:
    from .store.prune import describe_size, parse_age, parse_size

    try:
        older = parse_age(args.older_than) if args.older_than else None
        size = parse_size(args.max_size) if args.max_size else None
    except ValueError as error:
        raise SystemExit(f"cairn: {error}") from None
    if older is None and size is None:
        older = parse_age("30d")
    result = open_objects(args).backend.prune(older, size, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    print(
        f"{verb} {len(result['versions'])} read-only folders and {result['blobs']} blobs ({describe_size(result['freed'])}); "
        f"the repo holds {describe_size(result['size'])}"
    )


def cmd_verify(args) -> None:
    from .store import StoreError

    if args.ref and args.repo is not None:
        raise SystemExit("cairn: verify takes a REF or --repo FOLDER, not both")
    try:
        if args.repo is not None:  # a repo folder on this machine (a server's): no configuration needed
            if not (args.repo / "repo.json").is_file():
                raise SystemExit(f"cairn: {term().short_path(args.repo)} is not a repo (no repo.json)")
            from .client.open import open_folder

            report = open_folder(args.repo).verify(None, repair=args.repair)
        else:
            report = open_objects(args).verify(args.ref, repair=args.repair)
    except (StoreError, ValueError) as error:
        raise SystemExit(f"cairn: {error}") from None
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
        raise SystemExit(f"cairn: {error}") from None
    if interval is not None and interval <= 0:
        raise SystemExit("cairn: --gc-interval must be longer than 0")
    from .client.open import own_project, served_root

    root, own = args.root, None
    if root is None:  # the project's own repo (.cairn by default), its project served under the folder's name
        root, own = served_root(load_config(args.directory))
    else:
        own = own_project(args.directory, root)
    serve_store(
        root,
        host=args.host,
        port=args.port,
        max_blob=int(args.max_blob_gb * 1024**3),
        no_auth=args.no_auth,
        ui_port=None if args.no_ui else args.ui_port if args.ui_port is not None else args.port + 1 if args.port else 0,
        allow_network_fs=args.allow_network_fs,
        gc_interval=interval,
        gc_grace=grace,
        own=own,
    )


def cmd_ui(args) -> None:
    from .ui import open_backend, serve_ui

    if args.remote and args.repo:
        raise SystemExit("cairn: ui takes --remote URL or --repo FOLDER, not both")
    config = None
    try:
        config = load_config(args.directory)
    except (SystemExit, ValueError):
        if not (args.remote or args.repo):
            raise
    backend = open_backend(args.remote, args.repo, config, no_auth=args.no_auth)
    login = getattr(backend, "owner", "")  # the token the one-time login link logs in with
    if not login and not args.no_auth:
        from .client.open import client

        try:
            login = client(config, args.remote).token  # the first token open.find_token finds
        except SystemExit:
            login = ""
    serve_ui(
        backend,
        host=args.host,
        port=args.port,
        no_auth=args.no_auth,
        login_token=login,
        open_browser=not args.no_browser,
    )


def cmd_export_metadata(args) -> None:
    from .store import LocalRepo

    root = args.root
    if not (root / "repo.json").is_file():
        raise SystemExit(f"cairn: {term().short_path(root)} is not a repo (no repo.json)")
    counts = LocalRepo(root, allow_network_fs=True).export_metadata(args.folder)  # only reads
    for line in term().block(
        [(term().short_path(args.folder / file), f"{count} rows") for file, count in counts.items()]
    ):
        print(line)


def cmd_finalize_study(args) -> None:
    from .runs.hydra import finalize_study

    study = Path(args.study).resolve()
    completed = finalize_study(study)
    done = "completed and published (a draft)" if not study.exists() else "completed; latest moved"
    print(f"{term().short_path(args.study)}: {done if completed else 'failed (not every trial completed)'}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="cairn",
        description="Cairn: a repo of versioned artifacts for research (runs, their results, reports built from pinned artifacts), with an optional remote, a server, and a UI.",
    )
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    parser.add_argument(
        "-C",
        "--directory",
        type=Path,
        help="the project folder (default: the nearest folder upward with .cairn/repo.json, else here)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build every report (or one)")
    build.add_argument("--report", help="build only this report (its slug)")
    build.set_defaults(run=cmd_build)
    serve = commands.add_parser("serve", help="build and serve the reports")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument(
        "--port", type=int, default=8000, help="preferred port; the next free one is used if it is taken"
    )
    serve.add_argument("--strict-port", action="store_true", help="fail instead of moving to the next free port")
    serve.add_argument("--no-build", action="store_true", help="serve the existing build output")
    serve.set_defaults(run=cmd_serve)

    share = commands.add_parser(
        "share", help="write the reports as one self-contained page to publish unchanged (claude.ai artifacts)"
    )
    share.add_argument("--report", help="share only this report (its slug)")
    share.add_argument("--out", type=Path, help="where to write the page (default: <output_dir>/../share/index.html)")
    share.add_argument("--no-build", action="store_true", help="share the existing build output")
    share.set_defaults(run=cmd_share)

    finalize = commands.add_parser(
        "finalize-study", help="complete a multirun study whose launcher died, once every trial ended"
    )
    finalize.add_argument("study", type=Path, help="the study folder, outputs/<run-name>/<timestamp>")
    finalize.set_defaults(run=cmd_finalize_study)

    drafts = commands.add_parser(
        "drafts", help="the open and kept drafts (folders under .cairn/drafts/ that become versions)"
    )
    drafts.set_defaults(run=cmd_drafts)
    drafts_commands = drafts.add_subparsers(dest="drafts_command")
    drafts_commands.add_parser("list", help="every draft: name, uid, state, age, size, path").set_defaults(
        run=cmd_drafts
    )
    drafts_publish = drafts_commands.add_parser(
        "publish", help="publish a kept draft as the next version of its artifact"
    )
    drafts_publish.add_argument("draft", help="the draft's folder or uid (or a unique start of it)")
    drafts_publish.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="publish although its run.json says the run did not complete (records metadata.run_status)",
    )
    drafts_publish.set_defaults(run=cmd_drafts)
    drafts_discard = drafts_commands.add_parser("discard", help="delete a draft without publishing it")
    drafts_discard.add_argument("draft", help="the draft's folder or uid (or a unique start of it)")
    drafts_discard.set_defaults(run=cmd_drafts)

    publish = commands.add_parser(
        "publish", help="store a folder as a new artifact version (or follow a running job's folder)"
    )
    publish.add_argument(
        "folder",
        nargs="?",
        help="the folder to publish (a symlink such as runs/x/latest is followed); a report folder reports/<slug> is built and published as reports/<slug>",
    )
    publish.add_argument("name", nargs="?", help="artifact name, e.g. descent or datasets/crops")
    publish.add_argument("--label", help="version label (default: the resolved folder's name)")
    publish.add_argument("--alias", action="append", default=[], help="alias to point at the version (repeatable)")
    publish.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE", help="metadata (repeatable)")
    publish.add_argument(
        "--watch", action="store_true", help="sync the folder into one running version until stopped, then complete it"
    )
    publish.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="publish a folder whose run.json says the run is not completed (records metadata.run_status)",
    )
    publish.add_argument("--interval", type=float, help=argparse.SUPPRESS)
    publish.set_defaults(run=cmd_publish)

    pull = commands.add_parser("pull", help="materialize an artifact version locally and print its path")
    pull.add_argument(
        "ref", help="name[@vN|@<short id>|@alias|@label][//file], or a bare short id, e.g. datasets/crops@v3"
    )
    pull.add_argument("--into", type=Path, help="copy it into this folder instead of printing the read-only path")
    pull.add_argument(
        "--no-record",
        action="store_true",
        help="do not append the version to the inputs file ($CAIRN_INPUTS or .cairn-inputs.jsonl)",
    )
    pull.add_argument(
        "--offline",
        action="store_true",
        help="do not ask the remote: the repo's pending versions, its copies, and its mirrored aliases",
    )
    pull.set_defaults(run=cmd_pull)

    artifacts = commands.add_parser("artifacts", help="the repo's artifacts (latest version, versions, aliases)")
    artifacts.add_argument("--prefix", help="only names starting with this, e.g. reports/")
    artifacts.set_defaults(run=cmd_artifacts)
    versions = commands.add_parser(
        "versions", help="every version of an artifact: number, short id, status, label, created, uid, size"
    )
    versions.add_argument("name", help="the artifact name")
    versions.set_defaults(run=cmd_versions)
    show = commands.add_parser("show", help="one version: short id, metadata, files, inputs, consumers")
    show.add_argument("ref", help="name[@vN|@uid|@<short id>|@alias|@label], or a bare short id (3f9a2c1)")
    show.add_argument("--offline", action="store_true", help="do not ask the remote: answer from the repo only")
    show.set_defaults(run=cmd_show)
    alias = commands.add_parser(
        "alias", help="point an alias (best, paper, ...) at a completed version; latest moves by itself"
    )
    alias.add_argument("ref", help="name@vN (or @uid, @<short id>, @label, @another-alias), or a bare short id")
    alias.add_argument("alias", help="the alias to set or move")
    alias.set_defaults(run=cmd_alias)
    delete = commands.add_parser(
        "delete", help="delete a version (a tombstone: its number and uid are never used again) or a whole artifact"
    )
    delete.add_argument("ref", nargs="?", help="name@vN (or @uid, @<short id>, @alias, @label), or a bare short id")
    delete.add_argument("--artifact", metavar="NAME", help="delete every version of this artifact")
    delete.add_argument(
        "--force",
        action="store_true",
        help="delete protected versions too (aliased, inputs of other versions, running); owner token on a server",
    )
    delete.add_argument("--reason", help="why (kept in the tombstone)")
    delete.set_defaults(run=cmd_delete)
    retention = commands.add_parser(
        "retention", help="the retention rules `cairn gc` applies, per artifact-name prefix"
    )
    retention_commands = retention.add_subparsers(dest="retention_command", required=True)
    for verb, text in (
        ("show", "the rules"),
        ("set", "add or replace the rule for a prefix"),
        ("clear", "remove the rule for a prefix"),
    ):
        command = retention_commands.add_parser(verb, help=text)
        if verb != "show":
            command.add_argument(
                "--prefix", default="", help="artifact names starting with this, e.g. runs/ (default: every artifact)"
            )
        if verb == "set":
            command.add_argument("--keep-last", type=int, help="keep the N newest versions of each artifact")
            command.add_argument("--keep-days", type=float, help="keep versions younger than D days")
            command.add_argument(
                "--no-keep-aliased",
                action="store_true",
                help="aliased versions take a --keep-last place like any other (they are still never deleted)",
            )
        command.set_defaults(run=cmd_retention)
    gc = commands.add_parser(
        "gc",
        help="apply the retention rules and remove blobs no version names (the project's repo, its remote, or a repo folder)",
    )
    gc.add_argument(
        "root",
        nargs="?",
        type=Path,
        help="a repo folder on this machine (default: the project's repo, or its remote: the owner token)",
    )
    gc.add_argument("--dry-run", action="store_true", help="only say what would be deleted and freed")
    gc.add_argument(
        "--grace", default="24h", help="keep unreferenced blobs younger than this, e.g. 24h, 30m (default: 24h)"
    )
    gc.set_defaults(run=cmd_gc)
    init = commands.add_parser(
        "init",
        help="create a repo, .cairn/ with a commented config.toml, in a folder (default: here; "
        "$CAIRN_DIR if set); a repo that exists is left as it is",
    )
    init.add_argument(
        "path", nargs="?", type=Path, help="the folder to create .cairn/ in (default: the working directory)"
    )
    init.set_defaults(run=cmd_init)
    remote = commands.add_parser(
        "remote", help="show the repo's remote ([remote] url in .cairn/config.toml), or set or remove it"
    )
    remote.set_defaults(run=cmd_remote)
    remote_commands = remote.add_subparsers(dest="remote_command")
    remote_set = remote_commands.add_parser(
        "set", help="sync with a project on a `cairn server`: http(s)://host[:port][/prefix]/<project>"
    )
    remote_set.add_argument("url")
    remote_set.set_defaults(run=cmd_remote)
    remote_commands.add_parser("remove", help="stop syncing: the repo becomes the source of truth").set_defaults(
        run=cmd_remote
    )
    sync = commands.add_parser(
        "sync", help="send the repo's pending versions to its remote now, and mirror the remote's aliases"
    )
    sync.add_argument(
        "--watch", action="store_true", help="keep going every [repo] interval seconds (a login node's sender)"
    )
    sync.set_defaults(run=cmd_sync)
    status = commands.add_parser(
        "status", help="the repo's versions its remote does not have yet: pending, running, rejected"
    )
    status.set_defaults(run=cmd_status)
    prune = commands.add_parser(
        "prune",
        help="trim the repo's read-only folders, and contents only copies of the remote's versions need, by last access",
    )
    prune.add_argument("--older-than", help="remove folders not pulled for this long, e.g. 30d, 12h (default: 30d)")
    prune.add_argument(
        "--max-size", help="then remove the least recently pulled until the repo is under this, e.g. 20G"
    )
    prune.add_argument("--dry-run", action="store_true", help="only say what would be removed")
    prune.set_defaults(run=cmd_prune)

    verify = commands.add_parser(
        "verify",
        help="re-hash what the repo holds (blobs, versions, read-only folders, the outbox); exit 1 on any problem",
    )
    verify.add_argument(
        "ref", nargs="?", help="name[@vN|@uid|@<short id>|@alias|@label], or a bare short id: one version"
    )
    verify.add_argument("--repo", type=Path, help="a repo folder on this machine instead of the project's (a server's)")
    verify.add_argument(
        "--repair",
        action="store_true",
        help="remove what can be made again (broken read-only folders; broken contents of a remote's versions), never the only copy",
    )
    verify.set_defaults(run=cmd_verify)

    store_serve = commands.add_parser(
        "server",
        help="serve a repo to other machines (a new one gets an owner token); put an HTTPS reverse proxy in front",
    )
    store_serve.add_argument(
        "root", type=Path, nargs="?", help="the repo folder (default: the project's, .cairn; created if new)"
    )
    store_serve.add_argument("--host", default="127.0.0.1")
    store_serve.add_argument(
        "--port",
        type=int,
        default=8800,
        help="the port (default 8800); a taken one moves the server to the next free port, shown at start",
    )
    store_serve.add_argument("--max-blob-gb", type=float, default=50, help="largest accepted file")
    store_serve.add_argument(
        "--no-auth",
        action="store_true",
        help="no token, everyone acts as the owner (local testing; loopback hosts only)",
    )
    store_serve.add_argument(
        "--ui-port", type=int, help="port of the UI started beside the server (default: --port + 1)"
    )
    store_serve.add_argument("--no-ui", action="store_true", help="do not start `cairn ui` beside the server")
    store_serve.add_argument(
        "--allow-network-fs",
        action="store_true",
        help="serve a repo on a network filesystem (NFS, Lustre, GPFS, SMB, ...): only if this server is its only writer",
    )
    store_serve.add_argument(
        "--gc-interval",
        help="collect garbage this often, e.g. 24h (retention rules, then unreferenced blobs; default: never)",
    )
    store_serve.add_argument(
        "--grace", default="24h", help="with --gc-interval: keep unreferenced blobs younger than this (default: 24h)"
    )
    store_serve.set_defaults(run=cmd_server)
    export_metadata = commands.add_parser(
        "export-metadata",
        help="write a repo's metadata as readable JSON Lines "
        "(projects, versions, the alias log, consumers), for backups and inspection",
    )
    export_metadata.add_argument("root", type=Path)
    export_metadata.add_argument("folder", type=Path)
    export_metadata.set_defaults(run=cmd_export_metadata)

    ui = commands.add_parser(
        "ui", help="explore the repo in a browser: its reports, artifacts, versions (pending ones too), and lineage"
    )
    ui.add_argument(
        "--remote",
        help="a server's URL (its projects) or a project on it, <server>/<project>, instead of the project's repo",
    )
    ui.add_argument("--repo", type=Path, help="a repo folder on this machine instead of the project's (a server's)")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8801)
    ui.add_argument(
        "--no-auth",
        action="store_true",
        help="no login: use the client's token (CAIRN_TOKEN, .cairn/token, ~/.config/cairn/token) for a remote (loopback hosts only)",
    )
    ui.add_argument("--no-browser", action="store_true", help="do not open the one-time login link in a browser")
    ui.set_defaults(run=cmd_ui)

    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in argv:  # `publish ... -- <command...>`: everything after -- is the command to run
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1 :]
    args = parser.parse_args(argv)
    args.command = command
    if command and args.run is not cmd_publish:
        parser.error("only `publish` takes a command after --")
    if args.run in LOOKING_COMMANDS and not getattr(args, "repo", None):  # they look at a repo; they never make one
        from .client.open import existing_repo

        existing_repo(load_config(args.directory))
    elif args.run in READING_COMMANDS:
        from .client.open import existing_repo

        config = load_config(args.directory)
        if not config.found:
            existing_repo(config)
    syncing = args.run in REPO_COMMANDS and not getattr(args, "offline", False)  # --offline: never ask the remote
    from contextlib import nullcontext

    from .client.http import budget

    bounded = args.run in BOUNDED_COMMANDS and not getattr(args, "watch", False)  # a command waits so long in all
    with budget(COMMAND_BUDGET) if bounded else nullcontext():
        if syncing:
            sync_quietly(args)
        try:
            args.run(args)
        except SystemExit as exit:  # a message ends the command as `cairn: <message>`
            if isinstance(exit.code, str):
                raise SystemExit(term().error_line(exit.code)) from None
            raise
        except Exception as error:  # an expected failure is one line; CAIRN_DEBUG=1 shows the traceback
            if os.environ.get("CAIRN_DEBUG") or not _expected(error):
                raise
            raise SystemExit(f"cairn: {_describe(error)}") from None
        finally:
            if syncing:
                sync_quietly(args)
                from .client import repo

                repo._exit_flush.clear()  # a command sends at its start and end; it makes no third attempt at exit


LOOKING_COMMANDS = {cmd_status, cmd_verify, cmd_prune, cmd_sync}
# the commands whose waits for the remote (transfers of blobs aside) add up to COMMAND_BUDGET seconds at most: what
# could not be sent then stays pending; `cairn sync`, gc, deletions, `publish --watch`, the servers take their time
BOUNDED_COMMANDS = {cmd_publish, cmd_pull, cmd_artifacts, cmd_versions, cmd_show, cmd_status, cmd_drafts, cmd_alias}
COMMAND_BUDGET = 10.0
READING_COMMANDS = {
    cmd_artifacts,
    cmd_versions,
    cmd_show,
    cmd_pull,
    cmd_alias,
    cmd_delete,
    cmd_drafts,
}  # need versions: no repo
# found (nothing above the working directory, no $CAIRN_DIR) is refused, never made in a random folder
REPO_COMMANDS = {
    cmd_publish,
    cmd_pull,
    cmd_artifacts,
    cmd_versions,
    cmd_show,
    cmd_alias,
    cmd_delete,
    cmd_retention,
    cmd_gc,
    cmd_status,
    cmd_prune,
    cmd_verify,
    cmd_build,
    cmd_drafts,
    cmd_finalize_study,
}


def _describe(error: BaseException) -> str:
    """An expected error in a few words: an OSError as its reason and its path (no errno, no class name)."""
    if isinstance(error, OSError) and error.strerror:
        where = f": {term().short_path(error.filename)}" if error.filename else ""
        return f"{error.strerror}{where}"
    return term().short_paths(str(error))


def _expected(error: BaseException) -> bool:
    """Repo, client, and report-build errors are messages for the user, not crashes."""
    import sqlite3

    from .store.errors import StoreError

    if isinstance(error, (StoreError, OSError, sqlite3.OperationalError)):  # also a full disk, a read-only repo
        return True
    module = type(error).__module__
    return module.startswith(("cairn.client", "cairn.reports")) and type(error).__name__.endswith("Error")


if __name__ == "__main__":
    main()
