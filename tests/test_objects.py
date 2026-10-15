"""The object API (`cairn.connect`, Store, Project, Artifact, Version) and the CLI commands built on it, against a
local store and a store server."""

import json

import pytest

import cairn
from cairn.cli import main
from cairn.store.uids import UID

from test_identity import Server
from test_store import env, folder  # noqa: F401


@pytest.fixture(params=["local", "remote"])
def configured(request, env, tmp_path, monkeypatch):
    """cairn.toml pointing at a local store folder or at a running store server."""
    if request.param == "local":
        (env / "cairn.toml").write_text('[store]\nproject = "study"\n')
        yield None
        return
    server = Server(tmp_path / "store-root").start()
    monkeypatch.setenv("CAIRN_STORE_TOKEN", server.token)
    (env / "cairn.toml").write_text(f'[store]\nurl = "{server.url}"\nproject = "study"\n')
    yield server
    server.stop()


def test_object_api_end_to_end(configured, env, monkeypatch):
    store = cairn.connect()
    assert store.remote == (configured is not None) and UID.fullmatch(store.uid)
    project = store.project()
    crops = project.artifact("datasets/crops")
    data = folder(env, "crops", {"a.txt": "one", "sub/b.txt": "two"})
    v1 = crops.publish(data, metadata={"n": 1}, aliases=["paper"], label="first")
    assert (v1.number, v1.ref, v1.label, v1.status, v1.metadata) == (1, "study:datasets/crops@v1", "first", "completed", {"n": 1})
    assert UID.fullmatch(v1.uid) and v1.created and v1.source["folder"] == str(data.resolve())
    assert [(f.path, f.size) for f in v1.files] == [("a.txt", 3), ("sub/b.txt", 3)] and v1.files[0].sha256
    assert crops.publish(data, metadata={"n": 1}) == v1  # unchanged: the same version
    (data / "a.txt").write_text("ONE")
    v2 = crops.publish(data)
    assert v2.number == 2 and crops.latest.number == 2 and crops.latest.uid == v2.uid

    for selector in (1, "v1", "paper", "first", v1.uid):
        assert crops.version(selector).uid == v1.uid, selector
    assert crops.aliases == {"latest": 2, "paper": 1}
    crops.alias("keep", v2)
    crops.alias("old", 1)
    assert crops.aliases["keep"] == 2 and crops.aliases["old"] == 1
    assert [v.number for v in crops.versions()] == [1, 2]
    assert store.version(f"datasets/crops@{v2.uid}").number == 2

    folder(env, "other", {"x.txt": "x"})
    project.artifact("models/fit").publish(env / "other", inputs=[v1.ref])
    assert [a.name for a in project.artifacts()] == ["datasets/crops", "models/fit"]
    assert [a.name for a in project.artifacts(prefix="models/")] == ["models/fit"]
    assert crops.version(1).consumers == ["study:models/fit@v1"]
    assert project.artifact("models/fit").latest.inputs == ["study:datasets/crops@v1"]

    inputs = env / "inputs.jsonl"
    monkeypatch.setenv("CAIRN_INPUTS", str(inputs))
    with v1.open("sub/b.txt") as handle:
        assert handle.read() == b"two"
    with crops.latest.open("a.txt") as handle:
        assert handle.read(1) == b"O" and handle.read() == b"NE"
    with pytest.raises(cairn.NotFound):
        v1.open("nope.txt")
    path = v1.pull()
    assert path.parent.name == v1.uid and path.name == "files" and (path / "a.txt").read_text() == "one"
    assert [json.loads(line)["ref"] for line in inputs.read_text().splitlines()] == [
        "study:datasets/crops@v1", "study:datasets/crops@v2", "study:datasets/crops@v1"]

    with crops.publishing(env / "live", label="running") as pub:
        (env / "live").mkdir(exist_ok=True)
        (env / "live" / "r.txt").write_text("r")
        pub.sync()
        assert crops.version(pub.version).status == "running" and crops.version(pub.version).uid == pub.uid
    assert crops.latest.number == 3 and [f.path for f in crops.latest.files] == ["r.txt"]


def test_projects(configured, env):
    store = cairn.connect()
    project = store.create_project("other", title="Other", description="d")
    assert (project.name, project.title, project.description) == ("other", "Other", "d")
    project.update(title="Renamed")
    assert store.project("other").title == "Renamed" and store.project("other").description == "d"
    store.project().artifact("a").publish(folder(env, "a", {"f": "1"}))
    assert {p.name for p in store.projects()} == {"other", "study"}
    with pytest.raises(cairn.NotFound):
        store.project("missing").title


def test_shortcuts_stay_compatible(configured, env):
    assert cairn.publish("data", folder(env, "d", {"a.txt": "1"}), aliases=["best"]) == 1
    assert [v["version"] for v in cairn.versions("data")] == [1] and UID.fullmatch(cairn.versions("data")[0]["uid"])
    cairn.alias("data@v1", "paper")
    assert (cairn.pull("data@paper", record=False) / "a.txt").read_text() == "1"
    assert cairn.lineage("data") == {"inputs": [], "consumers": []}
    store = cairn.connect()
    assert cairn.pull("data@v1//a.txt", store=store, record=False).read_text() == "1"


def test_connect_by_url_or_path(env, tmp_path, monkeypatch):
    local = cairn.connect(tmp_path / "folder-store", project="p")
    local.create_project("p")
    local.project().artifact("x").publish(folder(env, "x", {"a": "1"}))
    assert cairn.connect(str(tmp_path / "folder-store")).project("p").artifact("x").latest.number == 1
    server = Server(tmp_path / "root").start()
    try:
        remote = cairn.connect(server.url, token=server.token, project="p")
        remote.create_project("p")
        assert remote.project().artifact("y").publish(folder(env, "y", {"b": "2"})).number == 1
        assert [p.name for p in remote.projects()] == ["p"]
        with pytest.raises(cairn.Unauthorized):
            cairn.connect(server.url, token="t" * 40).projects()
    finally:
        server.stop()


def run(capsys, *argv):
    main(list(argv))
    return capsys.readouterr().out


def test_cli_projects_artifacts_versions_show(configured, env, capsys):
    out = run(capsys, "project", "create", "extra", "--title", "Extra", "--description", "more")
    assert out.strip() == "project extra: Extra"
    out = run(capsys, "project", "edit", "extra", "--description", "changed")
    assert "description  changed" in out and "title        Extra" in out
    cairn.publish("datasets/crops", folder(env, "d", {"a.txt": "1", "b.txt": "22"}), metadata={"n": 2}, aliases=["paper"])
    cairn.publish("models/fit", folder(env, "m", {"w.bin": "w"}), inputs=["datasets/crops@v1"])
    assert {line.split("\t")[0] for line in run(capsys, "project", "list").splitlines()} == {"extra", "study"}
    out = run(capsys, "project", "show", "study")
    assert "artifacts    2" in out and "  datasets/crops\tv1\t1 versions" in out
    lines = run(capsys, "artifacts").splitlines()
    assert [line.split("\t")[0] for line in lines] == ["datasets/crops", "models/fit"] and "paper->v1" in lines[0]
    assert run(capsys, "artifacts", "--prefix", "models/").splitlines()[0].startswith("models/fit\tv1")
    assert run(capsys, "artifacts", "--project", "extra").strip() == "no artifacts"
    [line] = run(capsys, "versions", "datasets/crops").splitlines()
    uid = cairn.versions("datasets/crops")[0]["uid"]
    assert line.startswith("v1\tcompleted\td\t") and uid in line and "2 files" in line
    out = run(capsys, "show", f"datasets/crops@{uid}")
    assert "ref          study:datasets/crops@v1" in out and f"uid          {uid}" in out and '"n": 2' in out
    assert "consumers    study:models/fit@v1" in out and "aliases      latest, paper" in out and "  b.txt\t2" in out
    assert "inputs       study:datasets/crops@v1" in run(capsys, "show", "models/fit")
    status = run(capsys, "status")
    assert ("nothing pending" if configured else "local folder") in status
    sync = run(capsys, "sync")
    assert ("nothing to sync" if configured else "needs no sync") in sync
