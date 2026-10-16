"""One project per repo, remotes at `<server>/<project>`, and short ids.

A project's repo holds one project and no reference names one; a server holds many in one repo folder (one database,
one content-addressed blobs/), each served as if it were its own repo. Every version has a short id: the first 7 hex
digits of its id, more when needed to tell it apart in the project, the same in the repo and on the remote."""

import hashlib
import json
import threading
import urllib.request
from pathlib import Path

import pytest

import cairn
from cairn.client.http import split_remote
from cairn.client.open import remote_parts
from cairn.store import LocalRepo, refs
from cairn.store.ids import chosen
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


def made(prefix: str) -> str:
    """A whole id starting with `prefix` (for `ids.chosen`): tests construct their ids, never search random ones."""
    return prefix + "0" * (64 - len(prefix))


# ------------------------------------------------------------------ references and the CLI name no project


def test_references_have_no_project():
    with pytest.raises(cairn.InvalidRef, match="without a project"):
        refs.parse("other:data@v1")
    assert str(refs.parse("datasets/crops@v3//a.txt")) == "datasets/crops@v3//a.txt"
    bare = refs.parse("3f9a2c1")
    assert bare.name is None and bare.id == "3f9a2c1"
    with pytest.raises(cairn.InvalidRef, match="names a version alone"):
        refs.parse("data@3f9a")  # an id is never a selector


def test_the_cli_has_no_project_command_and_shows_no_project(study, capsys):
    from cairn.cli import main

    data = folder_with(study, "data", {"a.txt": "a"})
    main(["publish", str(data), "data"])
    out = capsys.readouterr().out
    version = cairn.connect().artifact("data").latest
    assert out.strip() == f"published data@v1 ({version.short_id}, 1 file, 1 B)"
    with pytest.raises(SystemExit):
        main(["project", "list"])
    capsys.readouterr()
    main(["versions", "data"])
    assert capsys.readouterr().out.splitlines()[1].split()[:2] == ["v1", version.short_id]
    main(["artifacts"])
    assert ":" not in capsys.readouterr().out.splitlines()[1].split()[0]


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


def test_a_short_id_names_a_version_bare(study):
    repo = cairn.connect()
    with chosen(made("3f9a2c1e"), made("8b04d77f")):
        first = repo.artifact("data").publish(folder_with(study, "one", {"a.txt": "1"}))
        second = repo.artifact("data").publish(folder_with(study, "two", {"a.txt": "2"}))
    assert (first.id, second.id) == (made("3f9a2c1e"), made("8b04d77f"))
    assert first.short_id == "3f9a2c1" and second.short_id == "8b04d77"
    for text in (first.short_id, "3f9a", first.id, first.id.upper(), "3F9A2C"):
        assert repo.version(text) == first, text
    assert cairn.use("8b04d7") == second
    assert cairn.pull(f"{first.short_id}//a.txt").read_text() == "1"
    with pytest.raises(cairn.InvalidRef, match="names a version alone"):
        repo.version(f"data@{first.short_id}")  # the 0.14 form name@<short id> is refused
    with pytest.raises(cairn.NotFound, match="no version with the id"):
        repo.version("0000000000")


def test_an_ambiguous_prefix_lists_its_candidates(study):
    one, two = made("35a1" + "0"), made("35a1" + "f")
    repo = cairn.connect()
    with chosen(one, two):
        a = repo.artifact("a").publish(folder_with(study, "one", {"x": "1"}))
        b = repo.artifact("b").publish(folder_with(study, "two", {"x": "2"}))
    assert (a.short_id, b.short_id) == ("35a1000", "35a1f00")
    with pytest.raises(cairn.Ambiguous) as caught:
        repo.version("35a1")
    message = str(caught.value)
    assert "a@v1 (35a1000)" in message and "b@v1 (35a1f00)" in message
    assert {(item["id"], item["short_id"]) for item in caught.value.candidates} == {(one, "35a1000"), (two, "35a1f00")}
    assert repo.version(a.short_id) == a and repo.version(b.short_id) == b


def test_a_short_id_gets_longer_when_needed_and_an_outgrown_one_is_refused(study):
    one, two = made("abcdef01" + "2"), made("abcdef01" + "9")  # the same first 8 digits
    repo = cairn.connect()
    with chosen(one, two):
        first = repo.artifact("a").publish(folder_with(study, "one", {"x": "1"}))
        assert first.short_id == "abcdef0"  # 7 digits while it is alone
        later = repo.artifact("b").publish(folder_with(study, "two", {"x": "2"}))
    assert later.short_id == "abcdef019"  # 9 digits: the shortest prefix no other id starts with
    assert repo.artifact("a").latest.short_id == "abcdef012"  # shown longer now: unique in the project
    assert repo.version("abcdef012") == first and repo.version("abcdef019") == later
    with pytest.raises(cairn.Ambiguous) as caught:  # the 7 digits shown before name two versions now: refused
        repo.version("abcdef0")
    assert {item["id"] for item in caught.value.candidates} == {one, two}


def test_names_aliases_and_labels_of_hex_digits_alone_are_refused(study):
    """An artifact name, an alias, or a label of 4 or more hex digits alone would read as an id: refused when it is
    created, so a bare hex word is always an id (no precedence decides between the two readings)."""
    repo = cairn.connect()
    data = repo.artifact("data")
    version = data.publish(folder_with(study, "one", {"x": "1"}))
    for word in ("abcd", "1234", "DEADBEEF", "cafe", "0123456789abcdef" * 4):
        with pytest.raises(cairn.InvalidRef, match="hex digits"):
            data.publish(folder_with(study, f"label-{word[:8]}", {"x": word}), label=word)
        with pytest.raises(cairn.InvalidRef, match="hex digits"):
            data.alias(word, version)
    for name in ("abcd", "1234", "deadbeef", "cafe"):
        with pytest.raises(cairn.InvalidRef, match="hex digits"):
            repo.artifact(name).publish(folder_with(study, f"name-{name}", {"x": name}))
    assert [v.number for v in data.versions()] == [1] and set(data.aliases) == {"latest"}  # nothing was made
    # three digits, or hex digits with anything else, are words and names
    labelled = data.publish(folder_with(study, "two", {"x": "2"}), label="abc")
    assert repo.version("data@abc") == labelled
    data.alias("cafe-1", labelled)
    assert repo.version("data@cafe-1") == labelled
    named = repo.artifact("data-abcd").publish(folder_with(study, "three", {"x": "3"}))
    assert repo.version("data-abcd") == named
    with pytest.raises(cairn.NotFound, match="no version with the id"):
        repo.version("abcd")  # a bare hex word is always an id, never a name


def test_an_alias_and_a_label_of_an_artifact_never_share_a_word(study):
    repo = cairn.connect()
    data = repo.artifact("data")
    labelled = data.publish(folder_with(study, "one", {"x": "1"}), label="best")
    with pytest.raises(cairn.InvalidRef, match="never share a word"):
        data.alias("best", labelled)
    data.alias("paper", labelled)
    with pytest.raises(cairn.InvalidRef, match="never share a word"):
        data.publish(folder_with(study, "two", {"x": "2"}), label="paper")


def test_a_bare_id_names_a_running_version(study):
    draft = cairn.draft("model", watch=True)
    running = cairn.connect().version(draft.id[: refs.SHORT])
    assert running.status == "running" and running.id == draft.id
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

    for function in (
        cairn.use,
        cairn.pull,
        cairn.publish,
        cairn.draft,
        cairn.alias,
        cairn.versions,
        cairn.lineage,
        cairn.init,
    ):
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
    assert alpha["id"] != beta["id"]
    monkeypatch.chdir(tmp_path / "beta")
    with pytest.raises(cairn.NotFound, match=f"on the remote {server.url}/beta"):
        cairn.use("nothing@latest")
    beta_short = local.short_ids("beta", [beta["id"]])[beta["id"]]
    found = cairn.connect().version(beta_short)
    assert found.id == beta["id"] and found.short_id == beta_short  # the same id and short id there


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
        request = urllib.request.Request(f"{url}/api/v3/projects", headers={"Authorization": f"Bearer {token}"})
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
        answer = ui_get(httpd, "/ui-api/projects/study/version?ref=model@v1")
        version = answer["version"]
        assert answer["ref"] == "model@v1" and version["id"] == model.version.id
        assert version["short_id"] == model.version.short_id
        assert version["inputs"] and all(  # inputs are ids; nodes name each (the run), with no project
            answer["nodes"][item]["name"].startswith("runs/") for item in version["inputs"]
        )
        rows = ui_get(httpd, "/ui-api/projects/study/artifacts/data")["versions"]
        assert rows[0]["short_id"] == data.short_id and rows[0]["id"] == data.id
        graph = ui_get(httpd, "/ui-api/projects/study/lineage?ref=model@v1&depth=2")
        assert graph["ref"] == model.version.id and all(":" not in item for item in graph["nodes"])
        assert graph["nodes"][model.version.id]["short_id"] == model.version.short_id
        by_id = ui_get(httpd, f"/ui-api/projects/study/version?ref={data.short_id}")["version"]
        assert by_id["id"] == data.id
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
        assert ui_get(httpd, "/ui-api/projects/beta/version?ref=data@v1")["version"]["short_id"]
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


def test_a_short_id_made_of_decimal_digits_is_an_id_never_a_version_number(study):
    from cairn.ui import make_ui_server, open_backend

    repo = cairn.connect()
    artifact = repo.artifact("digits")
    artifact.publish(folder_with(study, "first", {"x": "first"}))
    with chosen(made("8798636")):  # an id whose short id is all 0-9
        version = artifact.publish(folder_with(study, "second", {"x": "second"}))
    assert version.short_id == "8798636" and version.number == 2
    httpd = make_ui_server(open_backend(no_auth=True), port=0, no_auth=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        found = ui_get(httpd, f"/ui-api/projects/study/version?ref={version.short_id}")["version"]
        assert found["id"] == version.id
        assert ui_get(httpd, "/ui-api/projects/study/version?ref=8798")["version"]["id"] == version.id
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert repo.version(version.short_id).id == version.id  # the API resolves it the same way
    with pytest.raises(cairn.InvalidRef):
        repo.version(f"digits@{version.short_id}")  # never a selector, neither a number nor an id
