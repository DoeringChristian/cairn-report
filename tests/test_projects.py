"""One project per repo, remotes at `<server>/<project>`, and short ids.

A project's repo holds one project and no reference names one; a server holds many in one repo folder (one database,
one content-addressed blobs/), each served as if it were its own repo. Every version has a short id: the first 7 hex
digits of the SHA-256 of its uid, more when needed, the same in the repo and on the remote."""

import hashlib
import json
import threading
import urllib.request
from pathlib import Path

import pytest

import cairn
from cairn.client.http import split_remote
from cairn.client.open import remote_parts
from cairn.store import LocalRepo, new_uid, refs
from conftest import write_config


def folder_with(base: Path, name: str, files: dict) -> Path:
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    for key, value in files.items():
        (path / key).write_text(value)
    return path


@pytest.fixture
def study(tmp_path, monkeypatch):
    folder = tmp_path / "study"
    folder.mkdir()
    write_config(folder, "")
    monkeypatch.chdir(folder)
    monkeypatch.delenv("CAIRN_INPUTS", raising=False)
    return folder


def short(uid: str, digits: int = 7) -> str:
    return hashlib.sha256(uid.encode()).hexdigest()[:digits]


def uid_with_prefix(prefix: str) -> str:
    while True:
        uid = new_uid()
        if short(uid, len(prefix)) == prefix:
            return uid


def colliding_uids(digits: int) -> tuple[str, str]:
    """Two uids whose hashes share the first `digits` hex digits."""
    seen: dict[str, str] = {}
    while True:
        uid = new_uid()
        key = short(uid, digits)
        if key in seen:
            return seen[key], uid
        seen[key] = uid


# ------------------------------------------------------------------ references and the CLI name no project


def test_references_have_no_project():
    with pytest.raises(cairn.InvalidRef, match="without a project"):
        refs.parse("other:data@v1")
    assert str(refs.parse("datasets/crops@v3//a.txt")) == "datasets/crops@v3//a.txt"
    assert refs.parse("3f9a2c1").bare and refs.parse("data@3f9a").id == "3f9a"


def test_the_cli_has_no_project_command_and_shows_no_project(study, capsys):
    from cairn.cli import main

    data = folder_with(study, "data", {"a.txt": "a"})
    main(["publish", str(data), "data"])
    out = capsys.readouterr().out
    version = cairn.connect().artifact("data").latest
    assert out.strip() == f"published data@v1 ({version.short_id}; 1 files, 0.0 MB)"
    with pytest.raises(SystemExit):
        main(["project", "list"])
    capsys.readouterr()
    main(["versions", "data"])
    assert capsys.readouterr().out.split("\t")[:2] == ["v1", version.short_id]
    main(["artifacts"])
    assert ":" not in capsys.readouterr().out.split("\t")[0]


def test_remote_urls_name_their_project_in_the_last_segment():
    assert split_remote("https://host/my-study") == ("https://host", "my-study")
    assert split_remote("https://host/prefix/my-study/") == ("https://host/prefix", "my-study")
    assert split_remote("https://host:8800") == ("https://host:8800", None)
    with pytest.raises(SystemExit, match="names no project"):
        remote_parts("https://host")
    with pytest.raises(SystemExit, match="not a project name"):
        remote_parts("https://host/My Study")


def test_a_remote_without_a_project_is_a_config_error(study):
    write_config(study, '[remote]\nurl = "http://127.0.0.1:9"\n[repo]\n')
    with pytest.raises(SystemExit, match="names no project"):
        cairn.connect()


def test_the_project_key_is_gone_from_the_config(study):
    write_config(study, '[repo]\nproject = "x"\n')
    with pytest.raises(ValueError, match="unknown \\[repo\\] keys"):
        cairn.connect()


# ------------------------------------------------------------------ short ids


def test_a_short_id_names_a_version_by_name_or_bare(study):
    repo = cairn.connect()
    first = repo.artifact("data").publish(folder_with(study, "one", {"a.txt": "1"}))
    second = repo.artifact("data").publish(folder_with(study, "two", {"a.txt": "2"}))
    assert first.short_id == short(first.uid) and len(first.short_id) == 7
    for text in (first.short_id, first.short_id[:4], first.uid, f"data@{first.short_id[:5]}", f"data@{first.uid}"):
        assert repo.version(text) == first, text
    assert cairn.use(second.short_id[:6]) == second
    assert cairn.pull(f"{first.short_id}//a.txt").read_text() == "1"
    with pytest.raises(cairn.NotFound, match="no version with the id"):
        repo.version("0000000000")


def test_an_ambiguous_prefix_lists_its_candidates(study):
    one, two = colliding_uids(4)
    repo = cairn.connect()
    a = repo.artifact("a").publish(folder_with(study, "one", {"x": "1"}), _uid=one)
    b = repo.artifact("b").publish(folder_with(study, "two", {"x": "2"}), _uid=two)
    with pytest.raises(cairn.Ambiguous) as caught:
        repo.version(short(one, 4))
    message = str(caught.value)
    assert f"a@v1 ({a.short_id})" in message and f"b@v1 ({b.short_id})" in message
    assert {item["uid"] for item in caught.value.candidates} == {one, two}
    assert repo.version(a.short_id) == a and repo.version(b.short_id) == b


def test_a_short_id_gets_longer_when_needed_and_never_changes(study):
    one, two = colliding_uids(7)
    repo = cairn.connect()
    first = repo.artifact("a").publish(folder_with(study, "one", {"x": "1"}), _uid=one)
    later = repo.artifact("b").publish(folder_with(study, "two", {"x": "2"}), _uid=two)
    full_one, full_two = short(one, 64), short(two, 64)
    needed = next(length for length in range(7, 65) if not full_one.startswith(full_two[:length]))
    assert needed >= 8 and first.short_id == short(one) and later.short_id == full_two[:needed]
    assert repo.version(first.short_id) == first  # its own digits still name it (an exact short id wins)
    assert repo.version(later.short_id) == later
    assert repo.artifact("a").latest.short_id == short(one)  # fixed when the version was made


def test_names_aliases_and_labels_come_before_short_ids(study):
    repo = cairn.connect()
    labelled = repo.artifact("data").publish(folder_with(study, "one", {"x": "1"}), label="abcd")
    other = repo.artifact("data").publish(folder_with(study, "two", {"x": "2"}), _uid=uid_with_prefix("abcd"))
    assert repo.version("data@abcd") == labelled  # the label of hex digits wins
    assert repo.version("abcd") == other  # no artifact is named abcd: the bare id
    named = repo.artifact("abcd").publish(folder_with(study, "three", {"x": "3"}))
    assert repo.version("abcd") == named  # an artifact of that name wins over the id
    repo.artifact("data").alias("abcdef", other)
    assert repo.version("data@abcdef") == other


def test_a_bare_id_names_a_running_version(study):
    draft = cairn.draft("model", watch=True)
    running = cairn.connect().version(refs.default_short_id(draft.uid))
    assert running.status == "running" and running.uid == draft.uid
    draft.publish()


# ------------------------------------------------------------------ no cross-project access


def test_another_repo_never_feeds_inputs_into_this_projects_run(study, tmp_path):
    other = tmp_path / "other" / ".cairn"
    foreign = cairn.connect(other)
    foreign.artifact("data").publish(folder_with(tmp_path, "theirs", {"x": "1"}))
    inputs = study / "inputs.jsonl"
    import os

    os.environ["CAIRN_INPUTS"] = str(inputs)
    try:
        foreign.artifact("data").latest.pull()
        foreign.pull("data@latest")
        assert not inputs.exists()
        cairn.connect().artifact("mine").publish(folder_with(study, "mine", {"x": "1"}))
        cairn.pull("mine@latest")
        assert "mine@v1" in inputs.read_text()
    finally:
        del os.environ["CAIRN_INPUTS"]


def test_the_public_functions_take_no_store():
    import inspect

    for function in (cairn.use, cairn.pull, cairn.publish, cairn.draft, cairn.alias, cairn.versions, cairn.lineage, cairn.init):
        assert "store" not in inspect.signature(function).parameters, function


# ------------------------------------------------------------------ a server's projects


@pytest.fixture
def server(tmp_path, monkeypatch):
    pytest.importorskip("uvicorn")
    from cairn.server import make_server
    from cairn.store.tokens import init_root

    root = tmp_path / "remote"
    token = init_root(root).read_text().strip()
    httpd = make_server(root, port=0)
    httpd.log = None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("CAIRN_TOKEN", token)
    yield type("S", (), {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "root": root, "token": token})
    httpd.shutdown()
    httpd.server_close()


def project_with_remote(base: Path, name: str, remote: str) -> Path:
    folder = base / name
    folder.mkdir()
    write_config(folder, f'[remote]\nurl = "{remote}"\n[repo]\n')
    return folder


def test_projects_on_a_server_share_its_blobs_and_stay_apart(server, tmp_path, monkeypatch):
    for project in ("alpha", "beta"):
        folder = project_with_remote(tmp_path, project, f"{server.url}/{project}")
        monkeypatch.chdir(folder)
        data = folder_with(folder, "data", {"big.bin": "the same contents " * 1000, "own.txt": project})
        version = cairn.connect().artifact("datasets/crops").publish(data)
        assert version.number == 1 and version.ref == "datasets/crops@v1"
    local = LocalRepo(server.root)
    assert {item["name"] for item in local.projects()} == {"alpha", "beta"}
    shared = hashlib.sha256(("the same contents " * 1000).encode()).hexdigest()
    blobs = [path.name for path in (server.root / "blobs").rglob("*") if path.is_file()]
    assert blobs.count(shared) == 1 and len(blobs) == 3  # the shared file once, each project's own file
    alpha = local.get_version("alpha", "datasets/crops", 1)
    beta = local.get_version("beta", "datasets/crops", 1)
    assert alpha["uid"] != beta["uid"]
    monkeypatch.chdir(tmp_path / "beta")
    with pytest.raises(cairn.NotFound, match=f"on the remote {server.url}/beta"):
        cairn.use("nothing@latest")
    assert cairn.connect().version(f"datasets/crops@{beta['short_id']}").short_id == beta["short_id"]  # the same id there


def test_the_token_creates_a_project_on_its_first_sync(server, tmp_path, monkeypatch):
    folder = project_with_remote(tmp_path, "study", f"{server.url}/fresh")
    monkeypatch.chdir(folder)
    monkeypatch.setenv("CAIRN_TOKEN", server.token)
    assert "fresh" not in {item["name"] for item in LocalRepo(server.root).projects()}
    assert cairn.connect().artifact("data").publish(folder_with(folder, "data", {"x": "1"})).number == 1
    assert "fresh" in {item["name"] for item in LocalRepo(server.root).projects()}


def test_a_server_serving_a_projects_own_repo_names_its_project_after_the_folder(tmp_path, monkeypatch):
    pytest.importorskip("uvicorn")
    from cairn.server import make_server
    from cairn.store.tokens import init_root

    study = tmp_path / "my-study"
    study.mkdir()
    write_config(study, "")
    monkeypatch.chdir(study)
    cairn.connect().artifact("data").publish(folder_with(study, "data", {"x": "1"}))
    token = init_root(study / ".cairn").read_text().strip()
    httpd = make_server(study / ".cairn", port=0, own="my-study")
    httpd.log = None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        request = urllib.request.Request(f"{url}/api/v2/projects", headers={"Authorization": f"Bearer {token}"})
        listed = json.loads(urllib.request.urlopen(request).read())
        assert [item["name"] for item in listed] == ["my-study"]
        laptop = project_with_remote(tmp_path, "laptop", f"{url}/my-study")
        monkeypatch.chdir(laptop)
        monkeypatch.setenv("CAIRN_TOKEN", token)
        assert cairn.use("data@latest").number == 1  # the same project's versions
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ the UI


def ui_get(server, path: str):
    request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}", headers={"X-Cairn": "1"})
    return json.loads(urllib.request.urlopen(request).read())


def test_a_local_ui_lists_one_project_named_after_the_folder(study):
    from cairn.ui import make_ui_server, open_backend

    repo = cairn.connect()
    data = repo.artifact("data").publish(folder_with(study, "data", {"x": "1"}))
    with cairn.draft("model") as model:
        model.add_text("note", "hi")
    backend = open_backend(no_auth=True)
    httpd = make_ui_server(backend, port=0, no_auth=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        me = ui_get(httpd, "/ui-api/me")
        assert me["mode"] == "local" and me["home"] == "study"
        assert [item["name"] for item in ui_get(httpd, "/ui-api/projects")] == ["study"]
        version = ui_get(httpd, "/ui-api/projects/study/artifacts/model/versions/1")["version"]
        assert version["short_id"] == model.version.short_id
        assert all(item.startswith("study:runs/") for item in version["inputs"])  # the app reads project:name@sel
        rows = ui_get(httpd, "/ui-api/projects/study/artifacts/data")["versions"]
        assert rows[0]["short_id"] == data.short_id
        graph = ui_get(httpd, "/ui-api/lineage?ref=study:model@v1&depth=2")
        assert graph["ref"] == "study:model@v1" and all(ref.startswith("study:") for ref in graph["nodes"])
        assert graph["nodes"]["study:model@v1"]["short_id"] == model.version.short_id
        by_id = ui_get(httpd, f"/ui-api/projects/study/artifacts/data/versions/{data.short_id}")["version"]
        assert by_id["uid"] == data.uid
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_ui_on_a_server_lists_its_projects(server, tmp_path, monkeypatch):
    from cairn.ui import make_ui_server, open_backend

    for project in ("alpha", "beta"):
        folder = project_with_remote(tmp_path, project, f"{server.url}/{project}")
        monkeypatch.chdir(folder)
        cairn.connect().artifact("data").publish(folder_with(folder, "data", {"x": project}))
    backend = open_backend(remote=server.url, no_auth=True)
    httpd = make_ui_server(backend, port=0, no_auth=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert ui_get(httpd, "/ui-api/me")["mode"] == "server"
        assert sorted(item["name"] for item in ui_get(httpd, "/ui-api/projects")) == ["alpha", "beta"]
        assert ui_get(httpd, "/ui-api/projects/beta/artifacts/data/versions/1")["version"]["short_id"]
    finally:
        httpd.shutdown()
        httpd.server_close()
    one = open_backend(remote=f"{server.url}/alpha", no_auth=True)
    httpd = make_ui_server(one, port=0, no_auth=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert ui_get(httpd, "/ui-api/me")["mode"] == "local"
        assert [item["name"] for item in ui_get(httpd, "/ui-api/projects")] == ["alpha"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_serving_the_projects_own_repo_names_it_after_the_folder(study, tmp_path):
    from cairn.client.open import own_project

    cairn.connect()
    assert own_project(None, study / ".cairn") == "study"  # `cairn server .cairn`
    assert own_project(None, tmp_path / "srv") is None  # a server's repo folder has no own project


def test_cairn_remote_set_checks_the_token_and_asks_for_it(server, tmp_path, monkeypatch, capsys):
    from cairn.cli import main

    folder = tmp_path / "client"
    folder.mkdir()
    monkeypatch.chdir(folder)
    main(["init"])
    main(["remote", "set", f"{server.url}/demo"])  # $CAIRN_TOKEN holds the server's token
    assert "token   accepted (from $CAIRN_TOKEN)" in capsys.readouterr().out
    monkeypatch.setenv("CAIRN_TOKEN", "wrong")
    main(["remote", "set", f"{server.url}/demo"])
    assert "$CAIRN_TOKEN wins over every token file" in capsys.readouterr().out
    monkeypatch.delenv("CAIRN_TOKEN")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no ~/.config/cairn/token
    main(["remote", "set", f"{server.url}/demo"])  # not a terminal: says what to do, saves nothing
    out = capsys.readouterr().out
    assert f"no token for {server.url}" in out and "Copy the server's token file" in out
    assert not (folder / ".cairn" / "token").exists()
    answers = iter(["nope", server.token])  # on a terminal: a refused try, then the right token
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))
    main(["remote", "set", f"{server.url}/demo"])
    out = capsys.readouterr().out
    assert "refused; try again" in out and "token   accepted, saved to .cairn/token" in out
    saved = folder / ".cairn" / "token"
    assert saved.read_text().strip() == server.token and oct(saved.stat().st_mode & 0o777) == "0o600"
    main(["remote", "set", f"{server.url}/demo"])
    assert "token   accepted (from .cairn/token)" in capsys.readouterr().out
