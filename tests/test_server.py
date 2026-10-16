import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import cairn as store
from cairn.term import short_path
from cairn.cli import main
from cairn.client.http import Client
from cairn import BadRange, LocalRepo, Remote, StoreError
from cairn.store import LocalProject
from cairn.server import make_server, serve_store
from cairn.store.tokens import init_root

from test_store import env, folder  # noqa: F401  (the project fixture and folder helper)
from conftest import read_until, write_config


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A store server on a free port, in a thread, with its owner token."""
    root = tmp_path / "store-root"
    token = init_root(root).read_text().strip()
    httpd = make_server(root, port=0)
    requests = []
    httpd.log = requests.append
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    monkeypatch.setenv("CAIRN_TOKEN", token)
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", root=root, token=token, port=port, requests=requests)
    httpd.shutdown()
    httpd.server_close()


def call(server, method, path, body=None, headers=None, token="owner"):
    connection = http.client.HTTPConnection("127.0.0.1", server.port)
    headers = dict(headers or {})
    token = server.token if token == "owner" else token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if isinstance(body, (dict, list)):
        body, headers["Content-Type"] = json.dumps(body), "application/json"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, response.headers, data  # case-insensitive, like HTTP header names


def api(server, method, path, body=None, token="owner", expect=200):
    status, _, data = call(server, method, path, body, token=token)
    assert status == expect, (method, path, status, data[:300])
    return json.loads(data) if data else None


def remote(server, token=None, project="study") -> Remote:
    return Remote(Client(server.url, token or server.token), project)


def use_remote(env, server, project="study"):
    """The project `env` with the remote `<server>/<project>`; returns that remote project (a Remote)."""
    write_config(env, f'[remote]\nurl = "{server.url}/{project}"\n[repo]\ninterval = 1\n')
    return remote(server, project=project)


def blob_gets(server):
    return sum(line.startswith("GET /files/") for line in server.requests)


# --------------------------------------------------------------------- auth


def test_auth_required(server):
    assert call(server, "GET", "/api/v2/projects", token=None)[0] == 401
    assert call(server, "GET", "/api/v2/projects", token="x" * 40)[0] == 401
    assert call(server, "GET", "/api/v2/projects")[0] == 200
    assert call(server, "PUT", "/api/v2/blobs/" + "0" * 64, body=b"x", token=None)[0] == 401
    assert call(server, "POST", "/api/v2/blobs/missing", {"sha256": []}, token=None)[0] == 401
    assert call(server, "GET", "/api/v1/projects")[0] == 404  # the old hub API is gone
    assert call(server, "GET", "/files/study/data@v1//x.txt", token=None)[0] == 401


def test_the_server_has_no_pages_and_no_cookies(server, project):
    use_remote(project, server)
    store.publish("data", folder(project, "d", {"a.txt": "1"}))
    for path in ("/", "/login", "/logout", "/p/study/", "/p/study/a/data", "/p/study/r/alpha/", "/p/study/r/site.json", "/favicon.ico"):
        for token in ("owner", None):
            status, headers, body = call(server, "GET", path, token=token)
            assert status == 404 and "Set-Cookie" not in headers and b"<html" not in body, (path, status)
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    status, headers, _ = call(server, "POST", "/login", body=f"token={server.token}&next=/", token=None, headers=form)
    assert status in (401, 404) and "Set-Cookie" not in headers
    cookie = {"Cookie": "cairn_store=owner.abc"}
    assert call(server, "GET", "/api/v2/projects", token=None, headers=cookie)[0] == 401  # the API is bearer-only
    for path in ("/api/v2/projects", "/api/v2/projects/study", "/files/study/data@v1//a.txt"):
        assert "Set-Cookie" not in call(server, "GET", path)[1]
    assert call(server, "GET", "/api/v2/nope")[2].startswith(b"{")  # errors are JSON everywhere


def test_serve_creates_a_new_store_and_prints_its_token(tmp_path, capsys):
    import cairn.server.app as module

    original = module.StoreServer.serve_forever
    module.StoreServer.serve_forever = lambda self: self.server_close()
    try:
        root = tmp_path / "new-store"
        serve_store(root, port=0)
        token = (root / "token").read_text().strip()
        out = capsys.readouterr().out
        assert re.search(rf"\n  Token +{token} ", out) and "(created, with a new owner token)" in out
        assert re.search(r"\n  API +http://127.0.0.1:\d+/", out), out
        assert oct((root / "token").stat().st_mode & 0o777) == "0o600"
        serve_store(root, port=0)  # a second launch reuses it and prints the same token
        out = capsys.readouterr().out
        assert re.search(rf"\n  Token +{token} ", out) and "(created" not in out
        assert f"({short_path(root / 'token')})" in out  # the path at every launch
        serve_store(root, host="0.0.0.0", port=0)
        assert "warning: plain HTTP" in capsys.readouterr().err
    finally:
        module.StoreServer.serve_forever = original


# ------------------------------------------------------------- the one token


def seed(server):
    """Projects `alpha` and `beta`, each with an artifact `data` (one file), published with the owner token."""
    for project in ("alpha", "beta"):
        store.connect(f"{server.url}/{project}").artifact("data").publish(folder(server.root.parent, f"seed-{project}", {"x.txt": project}))


def test_one_token_reaches_every_project_and_nothing_else_does(server, env):
    seed(server)
    assert [p["name"] for p in api(server, "GET", "/api/v2/projects")] == ["alpha", "beta"]
    assert not (server.root / "tokens.jsonl").exists()
    wrong = "x" * 43
    for method, path, body in (("POST", "/api/v2/projects/alpha/artifacts/data/publish", {"entries": []}),
                               ("POST", "/api/v2/blobs/missing", {"sha256": []}),
                               ("PATCH", "/api/v2/projects/alpha", {"title": "t"}),
                               ("POST", "/api/v2/projects", {"name": "new"}),
                               ("GET", "/api/v2/projects", None),
                               ("GET", "/api/v2/projects/beta/artifacts", None),
                               ("GET", "/files/beta/data@v1//x.txt", None)):
        for token in (wrong, None):
            assert call(server, method, path, body, token=token)[0] == 401, (method, path, token)
    assert call(server, "PUT", "/api/v2/blobs/" + "a" * 64, body=b"x", token=wrong)[0] == 401
    assert call(server, "GET", "/files/alpha/data@v1//x.txt")[0] == 200
    # an input names a version of the same project: a reference with a project is refused
    status, _, data = call(server, "POST", "/api/v2/projects/alpha/artifacts/data/publish",
                           {"entries": [], "inputs": ["beta:data@v1"]})
    assert status == 400 and b"without a project" in data, data


# -------------------------------------------------------------- API details


def test_bad_uploads_and_entries(server):
    api(server, "POST", "/api/v2/projects", {"name": "p"}, expect=201)
    api(server, "POST", "/api/v2/projects", {"name": "p"}, expect=200)  # idempotent
    assert call(server, "PUT", "/api/v2/blobs/" + "a" * 64, body=b"not matching", headers={"Content-Length": "12"})[0] == 400
    assert not [f for f in (server.root / "blobs").rglob("*") if f.is_file()]  # nothing was stored
    publish = "/api/v2/projects/p/artifacts/a/publish"
    for entry in ({"path": "a/../../etc/passwd", "sha256": "a" * 64, "size": 1}, {"path": "/abs", "sha256": "a" * 64, "size": 1},
                  {"path": "x", "link": "../../etc"}, {"path": "x"}):
        assert call(server, "POST", publish, {"entries": [entry]})[0] == 400, entry
    status, _, data = call(server, "POST", publish, {"entries": [{"path": "x", "sha256": "b" * 64, "size": 1}]})
    assert status == 409 and json.loads(data)["missing"] == ["b" * 64]
    assert call(server, "POST", "/api/v2/projects/p/artifacts/Bad%2Fname/versions", {})[0] == 400
    assert call(server, "GET", "/api/v2/projects/nope")[0] == 404
    assert call(server, "GET", "/api/v2/resolve?ref=a")[0] == 404  # resolving is per project
    assert call(server, "GET", "/api/v2/projects/p/resolve?ref=other:a")[0] == 400  # a reference names no project
    assert call(server, "GET", "/api/v2/projects/p/ids?id=xyz")[0] == 400


def test_artifact_names_with_slashes_are_percent_encoded(server, env):
    r = use_remote(env, server)
    store.publish("datasets/crops", folder(env, "d", {"a.txt": "1"}))
    store.publish("versions", folder(env, "e", {"a.txt": "1"}))  # a name that is also a route word
    assert [s["name"] for s in api(server, "GET", "/api/v2/projects/study/artifacts")] == ["datasets/crops", "versions"]
    assert api(server, "GET", "/api/v2/projects/study/artifacts/datasets%2Fcrops/versions/1")["status"] == "completed"
    assert len(api(server, "GET", "/api/v2/projects/study/artifacts/versions/versions")) == 1
    assert call(server, "GET", "/files/study/datasets/crops@v1//a.txt")[2] == b"1"


def test_files_answer_range_etag_and_download(server, env):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"big.txt": "0123456789" * 10, "sub/page.html": "<b>x</b>"}))
    path = "/files/study/data@v1//big.txt"
    status, headers, whole = call(server, "GET", path)
    assert status == 200 and len(whole) == 100 and "immutable" in headers["Cache-Control"] and headers["ETag"]
    status, headers, data = call(server, "GET", path, headers={"Range": "bytes=5-14"})
    assert status == 206 and data == whole[5:15] and headers["Content-Range"] == "bytes 5-14/100"
    assert call(server, "GET", path, headers={"Range": "bytes=100-"})[0] == 416
    assert call(server, "GET", path, headers={"If-None-Match": headers["ETag"]})[0] == 304
    assert "attachment" in call(server, "GET", path + "?download=1")[1]["Content-Disposition"]
    status, headers, _ = call(server, "GET", "/files/study/data@latest//sub/page.html")  # alias: not immutable
    assert status == 200 and headers["Cache-Control"] == "no-cache" and headers["Content-Security-Policy"] == "sandbox"
    assert call(server, "GET", "/files/study/data@v1//nope.txt")[0] == 404
    assert call(server, "GET", "/files/study/data@v1//../x")[0] in (400, 404)
    assert call(server, "GET", "/files/study/data@v9//big.txt")[0] == 404


# ------------------------------------------------------- Remote round trip


def test_remote_store_round_trip(server, env):
    r = use_remote(env, server)
    data = folder(env, "crops", {"a.txt": "one", "sub/b.txt": "two"})
    (data / "latest").symlink_to("sub")
    assert store.publish("datasets/crops", data, label="first", aliases=["paper"], metadata={"n": 2}) == 1
    assert store.publish("datasets/crops", data, metadata={"n": 2}) == 1  # unchanged: no new version
    (data / "a.txt").write_text("changed")
    assert store.publish("datasets/crops", data, metadata={"n": 2}) == 2
    assert [v["version"] for v in store.versions("datasets/crops")] == [1, 2]
    assert r.resolve("datasets/crops@paper")[1]["version"] == 1
    assert r.resolve("datasets/crops")[1]["version"] == 2 and r.resolve("datasets/crops@first")[1]["metadata"] == {"n": 2}
    second = r.resolve("datasets/crops")[1]
    assert r.resolve(second["short_id"])[0] == "datasets/crops" and r.resolve(f"datasets/crops@{second['short_id'][:5]}")[1] == second
    assert [item["uid"] for item in r.ids(second["short_id"])] == [second["uid"]]
    store.alias("datasets/crops@v2", "best")
    assert r.aliases("datasets/crops") == {"latest": 2, "paper": 1, "best": 2}
    assert r.artifacts() == ["datasets/crops"] and r.record()["name"] == "study"
    with pytest.raises(store.NotFound, match=f"on the remote {server.url}/study"):
        r.resolve("datasets/crops@v7")
    with pytest.raises(StoreError, match="latest"):
        r.set_alias("datasets/crops", "latest", 1)
    # nothing is local: the data went through HTTP only
    assert not (env / ".store").exists()
    path = store.pull("datasets/crops@v1")
    assert (path / "a.txt").read_text() == "one" and (path / "sub" / "b.txt").read_text() == "two" and (path / "latest").is_symlink()
    assert not os.access(path / "a.txt", os.W_OK)
    assert store.pull("datasets/crops@v2//a.txt").read_text() == "changed"


def test_pull_materializes_once_and_never_downloads_a_blob_twice(server, env, tmp_path, monkeypatch):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"a.txt": "shared", "b.txt": "only in v1"}))
    store.publish("data", folder(env, "d2", {"a.txt": "shared", "c.txt": "only in v2"}))
    before = blob_gets(server)
    assert (store.pull("data@v1") / "b.txt").read_text() == "only in v1"  # publishing left the blobs in the repo
    assert blob_gets(server) == before
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / "other-machine"))  # another machine's repo downloads each blob once
    (tmp_path / "other-machine").mkdir()
    shutil.copyfile(env / ".cairn" / "config.toml", tmp_path / "other-machine" / "config.toml")  # $CAIRN_DIR's config
    first = store.pull("data@v1")
    assert blob_gets(server) - before == 2
    assert store.pull("data@v1") == first and blob_gets(server) - before == 2  # materialized already
    store.pull("data@v2")
    assert blob_gets(server) - before == 3  # only c.txt was new
    shutil.rmtree(tmp_path / "other-machine" / "versions")  # the read-only folders are made again from the blobs
    assert (store.pull("data@v1") / "b.txt").read_text() == "only in v1"
    assert blob_gets(server) - before == 3


def test_pull_records_inputs_like_the_local_store(server, env):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"a.txt": "1"}))
    store.connect().pull("data@latest")  # the object API outside a run: recorded in the working directory
    assert (env / ".cairn-inputs.jsonl").read_text().count('"data@v1"') == 1
    published = store.publish("fit", folder(env, "out", {"r.txt": "r", ".cairn-inputs.jsonl": (env / ".cairn-inputs.jsonl").read_text()}))
    assert store.versions("fit")[published - 1]["inputs"] == ["data@v1"]
    assert store.lineage("data@v1") == {"inputs": [], "consumers": ["fit@v1"], "runs": []}
    assert store.lineage("fit@v1", through_runs=False) == {"inputs": ["data@v1"], "consumers": []}


def test_cli_pull_prints_the_path_and_copies_into_a_folder(server, env, capsys):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"a.txt": "1", "sub/b.txt": "2"}))
    main(["pull", "data@v1", "--no-record"])
    path = capsys.readouterr().out.strip()
    assert (env / ".cairn" / "config.toml").is_file() and not (env / ".cairn-inputs.jsonl").exists()
    with open(os.path.join(path, "a.txt")) as handle:
        assert handle.read() == "1"
    main(["pull", "data@v1", "--into", str(env / "copy")])
    assert capsys.readouterr().out.strip() == str(env / "copy")
    assert (env / "copy" / "sub" / "b.txt").read_text() == "2" and not (env / "copy" / ".cairn-complete").exists()
    (env / "copy" / "a.txt").write_text("writable")  # a copy, not the read-only cache
    assert (env / ".cairn-inputs.jsonl").exists()  # recorded by default
    main(["pull", "data@v1//a.txt", "--into", str(env / "one"), "--no-record"])
    assert (env / "one" / "a.txt").read_text() == "1"
    with pytest.raises(SystemExit, match="not empty"):
        main(["pull", "data@v1", "--into", str(env / "copy")])


# --------------------------------------------------------------- publish --watch


def test_publish_watch_syncs_a_running_version_to_the_server(server, env, capsys):
    r = use_remote(env, server)
    out = env / "out"
    script = ("import pathlib, sys, time; out = pathlib.Path(sys.argv[1]); out.mkdir(exist_ok=True); "
              "(out / 'first.txt').write_text('1'); time.sleep(1.5); (out / 'second.txt').write_text('2'); time.sleep(0.5)")
    seen = []

    def poll():  # while the command runs, the server sees a running version that grows
        import time

        for _ in range(60):
            try:
                versions = api(server, "GET", "/api/v2/projects/study/artifacts/sweep/versions")
            except AssertionError:
                versions = []
            if versions and versions[0]["files"]:
                seen.append((versions[0]["status"], [f["path"] for f in versions[0]["files"]]))
                if len(versions[0]["files"]) == 2 or versions[0]["status"] != "running":
                    break
            time.sleep(0.1)

    watcher = threading.Thread(target=poll)
    watcher.start()
    with pytest.raises(SystemExit) as exit:
        main(["publish", str(out), "sweep", "--interval", "0.5", "--", sys.executable, "-c", script, str(out)])
    watcher.join()
    out = capsys.readouterr().out
    assert exit.value.code == 0 and "published sweep@v1 (" in out and ", 2 files" in out
    assert any(status == "running" and files == ["first.txt"] for status, files in seen), seen
    [version] = store.versions("sweep")
    assert version["status"] == "completed" and [f["path"] for f in version["files"]] == ["first.txt", "second.txt"]


def test_publish_watch_failure_keeps_latest(server, env, capsys):
    r = use_remote(env, server)
    store.publish("sweep", folder(env, "earlier", {"ok.txt": "good"}))
    with pytest.raises(SystemExit) as exit:
        main(["publish", str(env / "out2"), "sweep", "--interval", "0.5", "--", sys.executable, "-c",
              "import pathlib, sys; p = pathlib.Path(sys.argv[1]); p.mkdir(); (p / 'x.txt').write_text('x'); sys.exit(3)", str(env / "out2")])
    assert exit.value.code == 3 and "failed" in capsys.readouterr().err
    assert [v["status"] for v in store.versions("sweep")] == ["completed", "failed"]
    assert (store.pull("sweep@latest") / "ok.txt").read_text() == "good"


# ------------------------------------------------- the JSON the UI is built from


def test_overview_summary_and_file_list_routes(server, env):
    r = use_remote(env, server)
    store.publish("datasets/crops", folder(env, "d", {"a.txt": "1", "sub/b.txt": "22"}), metadata={"title": "Crops"}, aliases=["paper"])
    store.publish("other", folder(env, "o", {"x.txt": "x"}))
    [project] = api(server, "GET", "/api/v2/projects")
    assert project["name"] == "study" and project["artifacts"] == 2 and project["updated"] and "reports" not in project
    summaries = {item["name"]: item for item in api(server, "GET", "/api/v2/projects/study/artifacts")}
    assert summaries["datasets/crops"]["latest"] == 1 and summaries["datasets/crops"]["metadata"] == {"title": "Crops"}
    one = api(server, "GET", "/api/v2/projects/study/artifacts/datasets%2Fcrops")
    assert one["aliases"] == {"latest": 1, "paper": 1} and one["versions"] == 1
    assert one["latest_version"]["version"] == 1 and [f["path"] for f in one["latest_version"]["files"]] == ["a.txt", "sub/b.txt"]
    assert call(server, "GET", "/api/v2/projects/study/artifacts/nope")[0] == 404
    files = api(server, "GET", "/api/v2/projects/study/artifacts/datasets%2Fcrops/versions/1/files")["files"]
    assert [f["path"] for f in files] == ["a.txt", "sub/b.txt"] and files[1]["size"] == 2
    assert call(server, "GET", "/api/v2/projects/study/artifacts/datasets%2Fcrops/versions/9/files")[0] == 404


def test_remote_store_read_file_streams_ranges(server, env):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"big.txt": "0123456789" * 10}))
    local = LocalProject(LocalRepo(server.root), "study")
    for reader in (r, local):  # the same answers from both store kinds
        whole = reader.read_file("data", "v1", "big.txt")
        assert (whole.total, whole.size, whole.partial, whole.immutable) == (100, 100, False, True)
        assert b"".join(whole.chunks) == b"0123456789" * 10
        part = reader.read_file("data", "latest", "big.txt", "bytes=5-14")
        assert (part.first, part.last, part.total, part.partial, part.immutable) == (5, 14, 100, True, False)
        assert b"".join(part.chunks) == b"5678901234" and part.sha256 == whole.sha256
        with pytest.raises(BadRange) as raised:
            reader.read_file("data", "v1", "big.txt", "bytes=100-")
        assert raised.value.total == 100
        with pytest.raises(store.NotFound):
            reader.read_file("data", "v1", "nope.txt")


# ------------------------------------------------- the UI beside the store server


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def children_of(pid: int) -> list[tuple[int, str]]:
    rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True).stdout.splitlines()
    found = []
    for row in rows:
        fields = row.split(None, 2)
        if len(fields) == 3 and int(fields[1]) == pid:
            found.append((int(fields[0]), fields[2]))
    return found


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return not subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip().startswith("Z")


def launch(tmp_path, *flags):
    """`cairn server` as its own process; returns it with its startup output (up to the last line)."""
    root, port = tmp_path / "launch-store", free_port()
    process = subprocess.Popen([sys.executable, "-m", "cairn", "server", str(root), "--port", str(port), *flags],
                               stdout=subprocess.PIPE, text=True, cwd=tmp_path)
    from cairn.server.app import READY

    lines = read_until(process, lambda line: line == READY)
    return process, port, lines, root


def stop_process(process):
    process.terminate()
    process.wait(10)
    process.stdout.close()


def test_store_serve_starts_its_ui_as_a_child_with_the_token_in_its_environment_only(tmp_path):
    process, port, lines, root = launch(tmp_path)
    try:
        token = (root / "token").read_text().strip()
        text = "\n".join(lines)
        ui_port = port + 1
        assert re.search(rf"\n  API +http://127.0.0.1:{port}/\n", text) and re.search(rf"\n  Token +{token} ", text), text
        assert re.search(rf"\n  UI +http://127.0.0.1:{ui_port}/\n", text), text
    except Exception:
        stop_process(process)
        raise
    try:
        [(child, command)] = children_of(process.pid)
        assert "cairn" in command and "ui" in command and token not in command  # never on the command line
        form = {"Content-Type": "application/json", "X-Cairn": "1"}
        ui = SimpleNamespace(port=ui_port, token="")
        assert call(ui, "GET", "/", token=None)[0] == 303  # the UI asks for a login
        assert call(ui, "POST", "/ui-api/login", body=json.dumps({"token": "x" * 40}), token=None, headers=form)[0] == 401
        status, headers, _ = call(ui, "POST", "/ui-api/login", body=json.dumps({"token": token}), token=None, headers=form)
        assert status == 200 and token not in headers["Set-Cookie"]
        cookie = {"Cookie": headers["Set-Cookie"].split(";")[0]}
        page = call(ui, "GET", "/", token=None, headers=cookie)
        assert page[0] == 200 and b'<div id="root">' in page[2] and token.encode() not in page[2]
        projects = call(ui, "GET", "/ui-api/projects", token=None, headers={**cookie, "X-Cairn": "1"})
        assert projects[0] == 200 and token.encode() not in projects[2]
        # the child stops with the server
        stop_process(process)
        for _ in range(50):
            if not alive(child):
                break
            time.sleep(0.1)
        assert not alive(child)
    finally:
        if process.poll() is None:
            stop_process(process)


def test_store_serve_no_ui_and_ui_port(tmp_path):
    process, port, lines, root = launch(tmp_path, "--no-ui")
    try:
        assert not any(line.startswith("  UI ") for line in lines) and children_of(process.pid) == []
    finally:
        stop_process(process)
    tmp = tmp_path / "second"
    tmp.mkdir()
    process, port, lines, root = launch(tmp, "--ui-port", str(free_port()))
    try:
        [(child, command)] = children_of(process.pid)
        [shown] = [line for line in lines if line.startswith("  UI ")]
        assert re.fullmatch(r"  UI +http://127\.0\.0\.1:(\d+)/", shown), shown
        assert f"--port {shown.split(':')[2].split('/')[0]}" in command
    finally:
        stop_process(process)


def test_store_serve_no_auth_runs_the_ui_without_a_login(tmp_path):
    process, port, lines, root = launch(tmp_path, "--no-auth")
    try:
        assert not any(line.startswith("  Token ") for line in lines) and any("Auth" in line for line in lines)
        ui = SimpleNamespace(port=port + 1, token="")
        assert call(ui, "GET", "/", token=None)[0] == 200  # no login in front of the UI either
        [(child, command)] = children_of(process.pid)
        assert "--no-auth" in command
    finally:
        stop_process(process)


def test_the_server_keeps_running_when_its_ui_dies(tmp_path):
    process, port, lines, root = launch(tmp_path)
    try:
        [(child, _)] = children_of(process.pid)
        os.kill(child, 9)
        time.sleep(1.0)
        server = SimpleNamespace(port=port, token=(root / "token").read_text().strip())
        assert call(server, "GET", "/api/v2/projects")[0] == 200 and process.poll() is None
    finally:
        stop_process(process)
    assert not alive(child)


def test_the_spawned_ui_exits_when_the_server_is_killed(tmp_path):
    import signal
    import time
    root = tmp_path / "killed-store"
    server = subprocess.Popen([sys.executable, "-m", "cairn", "server", str(root), "--port", "0"],
                              stdout=subprocess.PIPE, text=True)
    try:
        lines = [server.stdout.readline() for _ in range(4)]
        ui = next(line for line in lines if line.startswith("  UI "))
        port = int(re.search(r":(\d+)/", ui).group(1))
        os.kill(server.pid, signal.SIGKILL)  # no handler in the server runs
        server.wait()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            probe = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                probe.request("GET", "/")
            except OSError:
                break
            finally:
                probe.close()
            time.sleep(0.5)
        else:
            raise AssertionError("the UI kept running after its server was killed")
    finally:
        server.kill()
        server.wait()
        server.stdout.close()


def test_the_server_extra_is_required(tmp_path, monkeypatch):
    import cairn.server.app as module

    monkeypatch.setattr(module, "MISSING", ImportError("No module named 'uvicorn'"))
    with pytest.raises(SystemExit, match=r'pip install "cairn\[server\]"'):
        make_server(tmp_path, port=0)
    with pytest.raises(SystemExit, match=r'pip install "cairn\[server\]"'):
        serve_store(tmp_path / "new", port=0)
    assert not (tmp_path / "new").exists()
