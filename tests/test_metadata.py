"""The metadata database: exporting it as JSON Lines, whole-or-nothing publishes, and one
writer per served root."""

import hashlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import threading

import pytest

from cairn import LocalRepo, StoreError
from cairn.client.remote import Server
from cairn.cli import main
from cairn.server import make_server
from cairn.store.errors import Conflict
from cairn.client.http import Client
from cairn.store.tokens import init_root, read_token
from cairn.store.uids import new_uid


def blob(root, text: str) -> dict:
    data = text.encode()
    sha = hashlib.sha256(data).hexdigest()
    (root / "blobs" / sha[:2]).mkdir(parents=True, exist_ok=True)
    (root / "blobs" / sha[:2] / sha).write_bytes(data)
    return {"path": f"{text}.txt", "sha256": sha, "size": len(data), "mtime": None}


def test_export_metadata(tmp_path, capsys):
    root = tmp_path / "root"
    store = LocalRepo(root)
    for project in ("data", "models"):
        store.create_project(project)
    one, two = blob(root, "one"), blob(root, "two")
    store.publish("data", "sets/crops", [one], metadata={"n": 1})
    store.publish("data", "sets/crops", [two], metadata={"n": 2}, label="big")
    store.publish("data", "fit", [one], inputs=["sets/crops@v2"])  # inputs are of the same project
    store.publish("models", "fit", [two])
    main(["export-metadata", str(root), str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert re.search(r"versions.jsonl +4 rows", out) and re.search(r"aliases.jsonl +4 rows", out), out
    versions = [json.loads(line) for line in (tmp_path / "out" / "versions.jsonl").read_text().splitlines()]
    assert [(v["project"], v["name"], v["version"]) for v in versions] == [("data", "fit", 1), ("data", "sets/crops", 1),
                                                                        ("data", "sets/crops", 2), ("models", "fit", 1)]
    projects = [json.loads(line) for line in (tmp_path / "out" / "projects.jsonl").read_text().splitlines()]
    assert [p["name"] for p in projects] == ["data", "models"]
    consumers = [json.loads(line) for line in (tmp_path / "out" / "consumers.jsonl").read_text().splitlines()]
    assert [(c["project"], c["input"], c["consumer"]) for c in consumers] == [("data", "sets/crops@v2", "fit@v1")]
    with pytest.raises(SystemExit):
        main(["export-metadata", str(tmp_path / "nothing"), str(tmp_path / "out2")])


def fresh(tmp_path):
    store = LocalRepo(tmp_path / "root")
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

    monkeypatch.setattr(LocalRepo, "_completed", broken)
    with pytest.raises(RuntimeError):
        store.publish("p", "x", entries, metadata={"n": 2})
    monkeypatch.undo()
    assert [v["version"] for v in store.versions("p", "x")] == [1]  # no half version (not even a failed one)
    assert store.summary("p", "x")["versions"] == 1 and store.aliases("p", "x") == {"latest": 1}
    assert store.publish("p", "x", entries, metadata={"n": 2}) == 2


def test_a_process_killed_inside_a_publish_stores_none_of_it(tmp_path):
    store, entries = fresh(tmp_path)
    store.publish("p", "x", entries)
    script = ("import os, sys, json; from cairn import LocalRepo\n"
              "def die(*args, **kwargs): os._exit(3)  # killed: no rollback, no cleanup\n"
              "LocalRepo._completed = die\n"
              "LocalRepo(sys.argv[1]).publish('p', 'x', json.loads(sys.argv[2]), metadata={'n': 2})\n")
    result = subprocess.run([sys.executable, "-c", script, str(store.root), json.dumps(entries)])
    assert result.returncode == 3
    again = LocalRepo(store.root)
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


def test_processes_on_the_host_write_a_served_root_directly(tmp_path, capsys):
    root = tmp_path / "root"
    init_root(root)
    server = make_server(root, port=0)
    server.log = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        beside = LocalRepo(root)  # a script on the server's host: the folder itself, not the server's URL
        beside.create_project("p")
        with pytest.raises(SystemExit, match="already served"):
            make_server(root, port=0)  # one server per root
        init_root(tmp_path / "other")
        taken = server.server_address[1]
        moved = make_server(tmp_path / "other", port=taken)  # a taken port moves the second server to a free one
        try:
            assert moved.moved_from == taken and moved.server_address[1] != taken
        finally:
            moved.server_close()
        client = Client(f"http://127.0.0.1:{server.server_address[1]}", read_token(root / "token"))
        assert [p["name"] for p in Server(client).projects()] == ["p"]  # the server sees the direct write
    finally:
        server.shutdown()
        server.server_close()
    LocalRepo(root).create_project("q")
