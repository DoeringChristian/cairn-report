"""The object API (`cairn.connect`, Repo, Artifact, Version) and the CLI commands built on it, against a
local store and a store server."""

import json

import pytest

import cairn
from cairn.cli import main
from cairn.store.ids import ID

from test_identity import Server
from test_store import env, folder  # noqa: F401
from conftest import write_config


@pytest.fixture(params=["local", "remote"])
def configured(request, env, tmp_path, monkeypatch):
    """.cairn/config.toml pointing at a local store folder or at a running store server."""
    if request.param == "local":
        write_config(env, "")
        yield None
        return
    server = Server(tmp_path / "store-root").start()
    monkeypatch.setenv("CAIRN_TOKEN", server.token)
    write_config(env, f'[remote]\nurl = "{server.url}/study"\n[repo]\n')
    yield server
    server.stop()


def test_object_api_end_to_end(configured, env, monkeypatch):
    store = cairn.connect()
    assert bool(store.remote) == (configured is not None) and ID.fullmatch(store.id)
    crops = store.artifact("datasets/crops")
    data = folder(env, "crops", {"a.txt": "one", "sub/b.txt": "two"})
    v1 = crops.publish(data, metadata={"n": 1}, aliases=["paper"], label="first")
    assert (v1.number, v1.ref, v1.label, v1.status, v1.metadata) == (
        1,
        "datasets/crops@v1",
        "first",
        "completed",
        {"n": 1},
    )
    assert ID.fullmatch(v1.id) and v1.created and v1.source["folder"] == str(data.resolve())
    assert [(f.path, f.size) for f in v1.files] == [("a.txt", 3), ("sub/b.txt", 3)] and v1.files[0].sha256
    assert crops.publish(data, metadata={"n": 1}) == v1  # unchanged: the same version
    (data / "a.txt").write_text("ONE")
    v2 = crops.publish(data)
    assert v2.number == 2 and crops.latest.number == 2 and crops.latest.id == v2.id

    for selector in (1, "v1", "paper", "first", v1.id):
        assert crops.version(selector).id == v1.id, selector
    assert crops.aliases == {"latest": 2, "paper": 1}
    crops.alias("keep", v2)
    crops.alias("old", 1)
    assert crops.aliases["keep"] == 2 and crops.aliases["old"] == 1
    assert [v.number for v in crops.versions()] == [1, 2]
    assert store.version(f"{v2.id}").number == 2
    assert store.version(v2.short_id).number == 2 and store.version(v1.short_id[:5]).number == 1
    with pytest.raises(cairn.InvalidRef, match="names a version alone"):
        store.version(f"datasets/crops@{v1.short_id[:5]}")  # an id is never written after a name

    folder(env, "other", {"x.txt": "x"})
    store.artifact("models/fit").publish(env / "other", inputs=[v1.ref])
    assert [a.name for a in store.artifacts()] == ["datasets/crops", "models/fit"]
    assert [a.name for a in store.artifacts(prefix="models/")] == ["models/fit"]
    fit = store.artifact("models/fit").latest
    assert crops.version(1).consumers == [fit.id] and fit.inputs == [v1.id]

    inputs = env / "inputs.jsonl"
    monkeypatch.setenv("CAIRN_INPUTS", str(inputs))
    with v1.open("sub/b.txt") as handle:
        assert handle.read() == b"two"
    with crops.latest.open("a.txt") as handle:
        assert handle.read(1) == b"O" and handle.read() == b"NE"
    with pytest.raises(cairn.NotFound):
        v1.open("nope.txt")
    path = v1.pull()
    assert path.parent.name == v1.id and path.name == "files" and (path / "a.txt").read_text() == "one"
    assert [json.loads(line)["ref"] for line in inputs.read_text().splitlines()] == [
        "datasets/crops@v1",
        "datasets/crops@v2",
        "datasets/crops@v1",
    ]

    with crops.publishing(env / "live", label="running") as pub:
        (env / "live").mkdir(exist_ok=True)
        (env / "live" / "r.txt").write_text("r")
        pub.sync()
        assert crops.version(pub.version).status == "running" and crops.version(pub.version).id == pub.id
    assert crops.latest.number == 3 and [f.path for f in crops.latest.files] == ["r.txt"]


def test_the_repo_names_no_project(configured, env):
    store = cairn.connect()
    assert not hasattr(store, "project") and not hasattr(store, "projects") and not hasattr(cairn, "Project")
    version = store.artifact("a").publish(folder(env, "a", {"f": "1"}))
    assert version.ref == "a@v1"
    with pytest.raises(cairn.InvalidRef, match="without a project"):
        store.version("study:a@v1")


def test_shortcuts_stay_compatible(configured, env):
    assert cairn.publish("data", folder(env, "d", {"a.txt": "1"}), aliases=["best"]) == 1
    assert [v["version"] for v in cairn.versions("data")] == [1] and ID.fullmatch(cairn.versions("data")[0]["id"])
    cairn.alias("data@v1", "paper")
    assert (cairn.pull("data@paper", record=False) / "a.txt").read_text() == "1"
    assert cairn.lineage("data") == {"inputs": [], "consumers": [], "runs": []}
    assert cairn.pull("data@v1//a.txt", record=False).read_text() == "1"


def test_connect_by_url_or_path(env, tmp_path, monkeypatch):
    local = cairn.connect(tmp_path / "folder-store")
    local.artifact("x").publish(folder(env, "x", {"a": "1"}))
    assert cairn.connect(str(tmp_path / "folder-store")).artifact("x").latest.number == 1
    server = Server(tmp_path / "root").start()
    try:
        remote = cairn.connect(f"{server.url}/p", token=server.token)
        assert remote.remote == f"{server.url}/p"
        assert remote.artifact("y").publish(folder(env, "y", {"b": "2"})).number == 1
        assert [a.name for a in remote.artifacts()] == ["y"]
        with pytest.raises(cairn.Unauthorized):
            cairn.connect(f"{server.url}/p", token="t" * 40).artifacts()
        with pytest.raises(SystemExit, match="names no project"):
            cairn.connect(server.url, token=server.token)
    finally:
        server.stop()


def run(capsys, *argv):
    main(list(argv))
    return capsys.readouterr().out


def test_cli_artifacts_versions_show(configured, env, capsys):
    cairn.publish(
        "datasets/crops", folder(env, "d", {"a.txt": "1", "b.txt": "22"}), metadata={"n": 2}, aliases=["paper"]
    )
    cairn.publish("models/fit", folder(env, "m", {"w.bin": "w"}), inputs=["datasets/crops@v1"])
    lines = run(capsys, "artifacts").splitlines()[1:]  # under the header
    assert [line.split()[0] for line in lines] == ["datasets/crops", "models/fit"] and "paper->v1" in lines[0]
    assert run(capsys, "artifacts", "--prefix", "models/").splitlines()[1].split()[:2] == ["models/fit", "v1"]
    assert run(capsys, "artifacts", "--prefix", "nothing/").strip() == "no artifacts starting with 'nothing/'"
    [_, line] = run(capsys, "versions", "datasets/crops").splitlines()
    crops = cairn.connect().version("datasets/crops@v1")
    version_id, short = crops.id, crops.short_id
    assert line.split()[:4] == ["v1", short, "completed", "d"] and "2 files" in line
    out = run(capsys, "show", version_id)
    assert "ref          datasets/crops@v1" in out and f"id           {version_id}" in out and '"n": 2' in out
    assert f"short id     {short}" in out and run(capsys, "show", short[:4]) == out
    fit = cairn.connect().version("models/fit@v1")
    assert (
        f"consumers    models/fit@v1 ({fit.short_id})" in out
        and "aliases      latest, paper" in out
        and "\n  b.txt  2 B\n" in out
    )
    assert f"inputs       datasets/crops@v1 ({short})" in run(capsys, "show", "models/fit")
    status = run(capsys, "status")
    assert ("nothing pending" if configured else "no remote") in status
    sync = run(capsys, "sync")
    assert ("nothing to sync" if configured else "nothing to sync") in sync
