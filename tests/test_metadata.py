"""The metadata database: importing a JSON Lines root, exporting it again, whole-or-nothing publishes, and one
writer per served root."""

import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import threading

import pytest

import cairn.store.db as db_module
from cairn import LocalStore, StoreError
from cairn.cli import main
from cairn.server import make_server
from cairn.store.errors import Conflict
from cairn.store.tokens import create_token, init_root
from cairn.store.uids import legacy, new_uid


def blob(root, text: str) -> dict:
    data = text.encode()
    sha = hashlib.sha256(data).hexdigest()
    (root / "blobs" / sha[:2]).mkdir(parents=True, exist_ok=True)
    (root / "blobs" / sha[:2] / sha[2:]).write_bytes(data)
    return {"path": f"{text}.txt", "sha256": sha, "size": len(data), "mtime": None}


def lines(path, rows, torn: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + torn)


def legacy_store(root):
    """Two projects as the JSON Lines layout wrote them: versions (one with a uid, one without, a running and a
    failed one, a label), alias moves, and a consumer across projects."""
    one, two = blob(root, "one"), blob(root, "two")
    uid = new_uid()
    base = {"label": None, "metadata": {}, "inputs": [], "source": None}
    for project in ("data", "models"):
        lines(root / "projects" / project / "project.json", [])
        (root / "projects" / project / "project.json").write_text(json.dumps(
            {"name": project, "title": project.title(), "description": "", "created": "2026-01-01T00:00:00+00:00"}))
    lines(root / "projects/data/artifacts/sets/crops/versions.jsonl", [
        {**base, "version": 1, "uid": uid, "status": "running", "created": "2026-01-02T00:00:00+00:00", "files": []},
        {**base, "version": 1, "uid": uid, "status": "completed", "created": "2026-01-02T00:00:00+00:00",
         "completed": "2026-01-02T00:01:00+00:00", "files": [one], "metadata": {"n": 1}},
        {**base, "version": 2, "status": "completed", "created": "2026-01-03T00:00:00+00:00", "label": "big",
         "completed": "2026-01-03T00:01:00+00:00", "files": [two], "metadata": {"n": 2}},
        {**base, "version": 3, "status": "failed", "created": "2026-01-04T00:00:00+00:00", "completed": "2026-01-04T00:00:01+00:00",
         "files": []},
        {**base, "version": 4, "status": "running", "created": "2026-01-05T00:00:00+00:00", "files": []},
    ], torn='{"version": 5, "stat')  # a writer died mid-line
    lines(root / "projects/data/artifacts/sets/crops/aliases.jsonl", [
        {"alias": "latest", "version": 1, "time": "a"}, {"alias": "best", "version": 1, "time": "b"},
        {"alias": "latest", "version": 2, "time": "c"}])
    lines(root / "projects/models/artifacts/fit/versions.jsonl", [
        {**base, "version": 1, "status": "completed", "created": "2026-02-01T00:00:00+00:00", "completed": "2026-02-01T00:00:01+00:00",
         "files": [one], "inputs": ["data:sets/crops@v2"]}])
    lines(root / "projects/models/artifacts/fit/aliases.jsonl", [{"alias": "latest", "version": 1, "time": "d"}])
    lines(root / "projects/data/consumers.jsonl", [{"consumer": "models:fit@v1", "input": "data:sets/crops@v2", "time": "e"}] * 2)
    return uid


def test_a_json_lines_root_is_imported_once(tmp_path):
    root = tmp_path / "root"
    uid = legacy_store(root)
    store = LocalStore(root)
    assert [p["name"] for p in store.projects()] == ["data", "models"]
    versions = store.versions("data", "sets/crops")
    assert [(v["version"], v["status"]) for v in versions] == [(1, "completed"), (2, "completed"), (3, "failed"), (4, "running")]
    assert versions[0]["uid"] == uid and versions[1]["uid"] == legacy(f"{store.uid}/data/sets/crops/2", "2026-01-03T00:00:00+00:00")
    assert store.aliases("data", "sets/crops") == {"latest": 2, "best": 1}
    assert [(row["alias"], row["version"]) for row in store.alias_log("data", "sets/crops")] == [("latest", 1), ("best", 1), ("latest", 2)]
    assert store.resolve("data:sets/crops@big")[2]["version"] == 2
    assert store.lineage("data:sets/crops@v2") == {"inputs": [], "consumers": ["models:fit@v1"]}
    assert store.lineage("models:fit") == {"inputs": ["data:sets/crops@v2"], "consumers": []}
    summary = {item["name"]: item for item in store.summaries("data")}["sets/crops"]
    assert (summary["latest"], summary["versions"], summary["metadata"]) == (2, 4, {"n": 2})
    assert summary["updated"] == "2026-01-05T00:00:00+00:00"
    assert {item["name"]: item["artifacts"] for item in store.overviews()} == {"data": 1, "models": 1}

    left = sorted(path.relative_to(root).as_posix() for path in (root / "projects").rglob("*") if path.is_file())
    assert left and all(path.endswith(".migrated") for path in left)  # kept, renamed, not deleted
    again = LocalStore(root)  # opened again: nothing imported twice
    assert len(again.alias_log("data", "sets/crops")) == 3 and len(again.versions("data", "sets/crops")) == 4
    store.complete_version("data", "sets/crops", 4)
    entry = blob(root, "three")
    assert store.publish("data", "sets/crops", [entry]) == 5  # numbering continues after the imported versions


def test_an_import_that_fails_leaves_nothing_half_done(tmp_path, monkeypatch):
    root = tmp_path / "root"
    legacy_store(root)
    original, calls = db_module._import_project, []

    def failing(connection, store_uid, folder):
        calls.append(folder.name)
        if len(calls) == 2:
            raise RuntimeError("disk on fire")
        original(connection, store_uid, folder)

    monkeypatch.setattr(db_module, "_import_project", failing)
    with pytest.raises(RuntimeError):
        LocalStore(root)
    with sqlite3.connect(root / "metadata.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 0  # rolled back whole
    assert (root / "projects/data/artifacts/sets/crops/versions.jsonl").is_file()  # nothing renamed
    monkeypatch.setattr(db_module, "_import_project", original)
    assert len(LocalStore(root).versions("data", "sets/crops")) == 4


def test_export_metadata(tmp_path, capsys):
    root = tmp_path / "root"
    legacy_store(root)
    LocalStore(root)
    main(["export-metadata", str(root), str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert "versions.jsonl  5 rows" in out and "aliases.jsonl  4 rows" in out
    versions = [json.loads(line) for line in (tmp_path / "out" / "versions.jsonl").read_text().splitlines()]
    assert [(v["project"], v["name"], v["version"]) for v in versions][:2] == [("data", "sets/crops", 1), ("data", "sets/crops", 2)]
    projects = [json.loads(line) for line in (tmp_path / "out" / "projects.jsonl").read_text().splitlines()]
    assert [p["name"] for p in projects] == ["data", "models"]
    consumers = [json.loads(line) for line in (tmp_path / "out" / "consumers.jsonl").read_text().splitlines()]
    assert consumers == [{"input": "data:sets/crops@v2", "consumer": "models:fit@v1", "time": "e"}]
    with pytest.raises(SystemExit):
        main(["export-metadata", str(tmp_path / "nothing"), str(tmp_path / "out2")])


def fresh(tmp_path):
    store = LocalStore(tmp_path / "root")
    store.create_project("p")
    data = b"payload"
    sha = hashlib.sha256(data).hexdigest()
    store.put_blob(sha, io.BytesIO(data), len(data))
    return store, [{"path": "a.txt", "sha256": sha, "size": len(data)}]


def test_a_failure_inside_a_publish_stores_none_of_it(tmp_path, monkeypatch):
    store, entries = fresh(tmp_path)
    assert store.publish("p", "x", entries) == 1

    def broken(*args, **kwargs):
        raise RuntimeError("crash between the version and its alias")

    monkeypatch.setattr(LocalStore, "_completed", broken)
    with pytest.raises(RuntimeError):
        store.publish("p", "x", entries, metadata={"n": 2})
    monkeypatch.undo()
    assert [v["version"] for v in store.versions("p", "x")] == [1]  # no half version (not even a failed one)
    assert store.summary("p", "x")["versions"] == 1 and store.aliases("p", "x") == {"latest": 1}
    assert store.publish("p", "x", entries, metadata={"n": 2}) == 2


def test_a_process_killed_inside_a_publish_stores_none_of_it(tmp_path):
    store, entries = fresh(tmp_path)
    store.publish("p", "x", entries)
    script = ("import os, sys, json; from cairn import LocalStore\n"
              "def die(*args, **kwargs): os._exit(3)  # killed: no rollback, no cleanup\n"
              "LocalStore._completed = die\n"
              "LocalStore(sys.argv[1]).publish('p', 'x', json.loads(sys.argv[2]), metadata={'n': 2})\n")
    result = subprocess.run([sys.executable, "-c", script, str(store.root), json.dumps(entries)])
    assert result.returncode == 3
    again = LocalStore(store.root)
    assert [v["version"] for v in again.versions("p", "x")] == [1] and again.aliases("p", "x") == {"latest": 1}
    with sqlite3.connect(store.root / "metadata.sqlite3") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert again.publish("p", "x", entries, metadata={"n": 2}) == 2


def test_a_uid_names_one_version_in_the_whole_store(tmp_path):
    store, entries = fresh(tmp_path)
    uid = new_uid()
    assert store.publish("p", "x", entries, uid=uid) == 1
    assert store.publish("p", "x", entries, uid=uid) == 1  # sent again: the same version
    with pytest.raises(Conflict):
        store.publish("p", "y", entries, uid=uid)
    with pytest.raises(Conflict):
        store.start_version("p", "y", uid=uid)


def test_a_served_root_has_one_writer(tmp_path, capsys):
    root = tmp_path / "root"
    init_root(root)
    server = make_server(root, port=0)
    server.log = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        beside = LocalStore(root)
        with pytest.raises(StoreError, match="served by `cairn server`"):
            beside.create_project("p")
        assert beside.projects() == []  # reading is fine
        with pytest.raises(SystemExit, match="already served"):
            make_server(root, port=0)  # one server per root
        create_token(root, "p", write=True)  # an owner command on the server's host may still write
        assert [p["name"] for p in beside.projects()] == ["p"]
    finally:
        server.shutdown()
        server.server_close()
    LocalStore(root).create_project("q")  # no server: writes are fine again
