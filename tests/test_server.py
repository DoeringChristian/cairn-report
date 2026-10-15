import http.client
import json
import os
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from cairn_report import store
from cairn_report.cli import main
from cairn_report.push import Client
from cairn_report.store import RemoteStore, StoreError
from cairn_report.store.server import make_server, serve_store
from cairn_report.store.tokens import create_token, init_root, list_tokens, revoke_token

from conftest import make_data, write
from test_store import env, folder  # noqa: F401  (the project fixture and folder helper)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A store server on a free port, in a thread, with its owner token."""
    root = tmp_path / "store-root"
    token = init_root(root).read_text().strip()
    httpd = make_server(root, port=0, secure_cookie=False)
    requests = []
    httpd.RequestHandlerClass.log_message = lambda self, format, *args: requests.append(format % args)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    monkeypatch.setenv("CAIRN_STORE_TOKEN", token)
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
    return response.status, dict(response.getheaders()), data


def api(server, method, path, body=None, token="owner", expect=200):
    status, _, data = call(server, method, path, body, token=token)
    assert status == expect, (method, path, status, data[:300])
    return json.loads(data) if data else None


def remote(server, token=None, project="study") -> RemoteStore:
    return RemoteStore(Client(server.url, token or server.token), project)


def use_remote(env, server, project="study"):
    (env / "cairn-report.toml").write_text(f'[store]\nurl = "{server.url}"\nproject = "{project}"\ninterval = 1\n')
    return remote(server, project=project)


def blob_gets(server):
    return sum(line.startswith('"GET /files/') for line in server.requests)


# --------------------------------------------------------------------- auth


def test_auth_required(server):
    assert call(server, "GET", "/api/v2/projects", token=None)[0] == 401
    assert call(server, "GET", "/api/v2/projects", token="x" * 40)[0] == 401
    assert call(server, "GET", "/api/v2/projects")[0] == 200
    status, headers, _ = call(server, "GET", "/", token=None)
    assert status == 303 and headers["Location"].startswith("/login")
    assert call(server, "PUT", "/api/v2/blobs/" + "0" * 64, body=b"x", token=None)[0] == 401
    assert call(server, "POST", "/api/v2/blobs/missing", {"sha256": []}, token=None)[0] == 401
    assert call(server, "GET", "/api/v1/projects")[0] == 404  # the old hub API is gone


def test_login_cookie_works_for_pages_and_never_for_the_api(server):
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    status, headers, _ = call(server, "POST", "/login", body=f"token={'w' * 40}&next=/", token=None, headers=form)
    assert status == 401 and "Set-Cookie" not in headers
    status, headers, _ = call(server, "POST", "/login", body=f"token={server.token}&next=//evil.example", token=None, headers=form)
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"] and server.token not in cookie
    assert call(server, "GET", "/", token=None, headers={"Cookie": cookie})[0] == 200
    assert call(server, "GET", "/api/v2/projects", token=None, headers={"Cookie": cookie})[0] == 401
    forged = cookie.split("=")[0] + "=owner." + "0" * 64
    assert call(server, "GET", "/", token=None, headers={"Cookie": forged})[0] == 303


def test_serve_creates_a_new_store_and_prints_its_token(tmp_path, capsys):
    import cairn_report.store.server as module

    original = module.ThreadingHTTPServer.serve_forever
    module.ThreadingHTTPServer.serve_forever = lambda self: self.server_close()
    try:
        root = tmp_path / "new-store"
        serve_store(root, port=0)
        token = (root / "token").read_text().strip()
        out = capsys.readouterr().out
        assert f"Token: {token}" in out and "Created store" in out and "Store: http://127.0.0.1:" in out
        assert oct((root / "token").stat().st_mode & 0o777) == "0o600"
        serve_store(root, port=0)  # a second launch reuses it and prints the same token
        out = capsys.readouterr().out
        assert f"Token: {token}" in out and "Created store" not in out
        serve_store(root, host="0.0.0.0", port=0)
        assert "plain HTTP" in capsys.readouterr().out
    finally:
        module.ThreadingHTTPServer.serve_forever = original


# ------------------------------------------------------------- scoped tokens


def seed(server):
    """Projects `alpha` and `beta`, each with an artifact `data` (one file)."""
    for project in ("alpha", "beta"):
        store.publish(f"{project}:data", None or folder(server.root.parent, f"seed-{project}", {"x.txt": project}),
                      store=remote(server, project=project), config=SimpleNamespace(project=server.root.parent, store={}))


def test_scoped_tokens(server, env):
    seed(server)
    read = create_token(server.root, "alpha", name="reader")
    write_token = create_token(server.root, "alpha", write=True, name="writer")
    assert api(server, "GET", "/api/v2/projects/alpha", token=read)["name"] == "alpha"
    assert [p["name"] for p in api(server, "GET", "/api/v2/projects", token=read)] == ["alpha"]
    assert [p["name"] for p in api(server, "GET", "/api/v2/projects")] == ["alpha", "beta"]
    api(server, "GET", "/api/v2/resolve?ref=alpha:data@latest", token=read)
    # read-only: no writes, nothing outside its project
    entry = {"path": "y.txt", "sha256": "0" * 64, "size": 1}
    for method, path, body in (("POST", "/api/v2/projects/alpha/artifacts/data/publish", {"entries": []}),
                               ("POST", "/api/v2/projects/alpha/artifacts/data/versions", {}),
                               ("POST", "/api/v2/blobs/missing", {"sha256": []}),
                               ("POST", "/api/v2/projects/alpha/artifacts/data/aliases", {"alias": "x", "version": 1}),
                               ("PATCH", "/api/v2/projects/alpha", {"title": "t"}),
                               ("POST", "/api/v2/projects", {"name": "new"}),
                               ("GET", "/api/v2/projects/beta", None),
                               ("GET", "/api/v2/projects/beta/artifacts", None),
                               ("GET", "/api/v2/resolve?ref=beta:data@latest", None),
                               ("GET", "/api/v2/lineage?ref=beta:data@latest", None),
                               ("GET", "/files/beta/data@v1//x.txt", None),
                               ("GET", "/p/beta/", None)):
        assert call(server, method, path, body, token=read)[0] == 403, (method, path)
    assert call(server, "PUT", "/api/v2/blobs/" + "a" * 64, body=b"x", token=read)[0] == 403
    assert call(server, "GET", "/files/alpha/data@v1//x.txt", token=read)[0] == 200
    # a write token publishes to its own project only
    other = folder(server.root.parent, "more", {"z.txt": "more"})
    assert store.publish("alpha:data", other, store=remote(server, write_token), config=SimpleNamespace(project=other, store={})) == 2
    with pytest.raises(StoreError, match="HTTP 403"):
        store.publish("beta:data", other, store=remote(server, write_token, "beta"), config=SimpleNamespace(project=other, store={}))
    assert call(server, "POST", "/api/v2/projects/beta/artifacts/data/publish", {"entries": [entry]}, token=write_token)[0] == 403
    # ... and cannot register inputs from another project (that would write into its lineage)
    status, _, data = call(server, "POST", "/api/v2/projects/alpha/artifacts/data/publish",
                           {"entries": [], "inputs": ["beta:data@v1"]}, token=write_token)
    assert status == 403, data
    # revoke: it stops working at once, and the list never shows token values
    listing = list_tokens(server.root)
    assert {row["name"] for row in listing} == {"reader", "writer"} and read not in json.dumps(listing)
    revoke_token(server.root, "reader")
    assert call(server, "GET", "/api/v2/projects/alpha", token=read)[0] == 401
    assert call(server, "GET", "/api/v2/projects/alpha", token=write_token)[0] == 200
    assert [row["name"] for row in list_tokens(server.root)] == ["writer"]


def test_tokens_are_stored_hashed(server):
    import hashlib

    token = create_token(server.root, "alpha", write=True, name="w")
    text = (server.root / "tokens.jsonl").read_text()
    assert token not in text and hashlib.sha256(token.encode()).hexdigest() in text
    assert oct((server.root / "tokens.jsonl").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(SystemExit, match="already exists"):
        create_token(server.root, "alpha", name="w")
    with pytest.raises(SystemExit, match="no active token"):
        revoke_token(server.root, "nope")


def test_browser_login_with_a_scoped_token_is_scoped_and_revocable(server, env):
    seed(server)
    token = create_token(server.root, "alpha", name="viewer")
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    status, headers, _ = call(server, "POST", "/login", body=f"token={token}&next=/", token=None, headers=form)
    cookie = headers["Set-Cookie"].split(";")[0]
    assert status == 303 and token not in cookie
    cookies = {"Cookie": cookie}
    index = call(server, "GET", "/", token=None, headers=cookies)[2].decode()
    assert "/p/alpha/" in index and "/p/beta/" not in index
    assert call(server, "GET", "/p/alpha/", token=None, headers=cookies)[0] == 200
    assert call(server, "GET", "/p/beta/", token=None, headers=cookies)[0] == 403
    assert call(server, "GET", "/files/beta/data@v1//x.txt", token=None, headers=cookies)[0] == 403
    revoke_token(server.root, "viewer")
    assert call(server, "GET", "/p/alpha/", token=None, headers=cookies)[0] == 303


def test_token_commands(server, capsys):
    main(["store", "token", "create", str(server.root), "--project", "alpha", "--write", "--name", "ci"])
    out = capsys.readouterr().out
    token = re.search(r"Token: (\S+)", out).group(1)
    assert api(server, "GET", "/api/v2/projects/alpha", token=token)["name"] == "alpha"  # the project was created
    main(["store", "token", "list", str(server.root)])
    listing = capsys.readouterr().out
    assert "ci\talpha\twrite" in listing and token not in listing
    main(["store", "token", "revoke", str(server.root), "ci"])
    assert call(server, "GET", "/api/v2/projects/alpha", token=token)[0] == 401


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
    assert call(server, "GET", "/api/v2/resolve?ref=a")[0] == 400  # needs a project


def test_artifact_names_with_slashes_are_percent_encoded(server, env):
    r = use_remote(env, server)
    store.publish("datasets/crops", folder(env, "d", {"a.txt": "1"}), store=r)
    store.publish("versions", folder(env, "e", {"a.txt": "1"}), store=r)  # a name that is also a route word
    assert [s["name"] for s in api(server, "GET", "/api/v2/projects/study/artifacts")] == ["datasets/crops", "versions"]
    assert api(server, "GET", "/api/v2/projects/study/artifacts/datasets%2Fcrops/versions/1")["status"] == "completed"
    assert len(api(server, "GET", "/api/v2/projects/study/artifacts/versions/versions")) == 1
    assert call(server, "GET", "/files/study/datasets/crops@v1//a.txt")[2] == b"1"


def test_files_answer_range_etag_and_download(server, env):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"big.txt": "0123456789" * 10, "sub/page.html": "<b>x</b>"}), store=r)
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


# ------------------------------------------------------- RemoteStore round trip


def test_remote_store_round_trip(server, env):
    r = use_remote(env, server)
    data = folder(env, "crops", {"a.txt": "one", "sub/b.txt": "two"})
    (data / "latest").symlink_to("sub")
    assert store.publish("datasets/crops", data, label="first", aliases=["paper"], metadata={"n": 2}, store=r) == 1
    assert store.publish("datasets/crops", data, metadata={"n": 2}, store=r) == 1  # unchanged: no new version
    (data / "a.txt").write_text("changed")
    assert store.publish("datasets/crops", data, metadata={"n": 2}, store=r) == 2
    assert [v["version"] for v in store.versions("datasets/crops", store=r)] == [1, 2]
    assert r.resolve("datasets/crops@paper")[2]["version"] == 1
    assert r.resolve("datasets/crops")[2]["version"] == 2 and r.resolve("datasets/crops@first")[2]["metadata"] == {"n": 2}
    store.alias("datasets/crops@v2", "best", store=r)
    assert r.aliases("study", "datasets/crops") == {"latest": 2, "paper": 1, "best": 2}
    assert r.artifacts("study") == ["datasets/crops"] and r.get_project("study")["name"] == "study"
    with pytest.raises(store.NotFound):
        r.resolve("datasets/crops@v7")
    with pytest.raises(StoreError, match="latest"):
        r.set_alias("study", "datasets/crops", "latest", 1)
    # nothing is local: the data went through HTTP only
    assert not (env / ".store").exists()
    path = store.pull("datasets/crops@v1", store=r)
    assert (path / "a.txt").read_text() == "one" and (path / "sub" / "b.txt").read_text() == "two" and (path / "latest").is_symlink()
    assert not os.access(path / "a.txt", os.W_OK)
    assert store.pull("datasets/crops@v2//a.txt", store=r).read_text() == "changed"


def test_pull_materializes_once_and_never_downloads_a_blob_twice(server, env, tmp_path, monkeypatch):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"a.txt": "shared", "b.txt": "only in v1"}), store=r)
    store.publish("data", folder(env, "d2", {"a.txt": "shared", "c.txt": "only in v2"}), store=r)
    before = blob_gets(server)
    first = store.pull("data@v1", store=r)
    assert blob_gets(server) - before == 2
    assert store.pull("data@v1", store=r) == first and blob_gets(server) - before == 2  # materialized already
    store.pull("data@v2", store=r)
    assert blob_gets(server) - before == 3  # only c.txt was new
    # a fresh pull cache folder with the blob cache kept would not re-download either; a corrupt download is refused
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "other-machine"))
    assert (store.pull("data@v1", store=r) / "b.txt").read_text() == "only in v1"
    assert blob_gets(server) - before == 5


def test_pull_records_inputs_like_the_local_store(server, env):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"a.txt": "1"}), store=r)
    store.pull("data@latest", store=r)
    assert (env / ".cairn-inputs.jsonl").read_text().count("study:data@v1") == 1
    published = store.publish("fit", folder(env, "out", {"r.txt": "r", ".cairn-inputs.jsonl": (env / ".cairn-inputs.jsonl").read_text()}), store=r)
    assert store.versions("fit", store=r)[published - 1]["inputs"] == ["study:data@v1"]
    assert store.lineage("data@v1", store=r) == {"inputs": [], "consumers": ["study:fit@v1"]}
    assert store.lineage("fit@v1", store=r) == {"inputs": ["study:data@v1"], "consumers": []}


def test_cli_pull_prints_the_path_and_copies_into_a_folder(server, env, capsys):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"a.txt": "1", "sub/b.txt": "2"}), store=r)
    main(["pull", "data@v1", "--no-record"])
    path = capsys.readouterr().out.strip()
    assert (env / "cairn-report.toml").is_file() and not (env / ".cairn-inputs.jsonl").exists()
    assert open(os.path.join(path, "a.txt")).read() == "1"
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
    assert exit.value.code == 0 and "published sweep@v1 (2 files" in capsys.readouterr().out
    assert any(status == "running" and files == ["first.txt"] for status, files in seen), seen
    [version] = store.versions("sweep", store=r)
    assert version["status"] == "completed" and [f["path"] for f in version["files"]] == ["first.txt", "second.txt"]


def test_publish_watch_failure_keeps_latest(server, env, capsys):
    r = use_remote(env, server)
    store.publish("sweep", folder(env, "earlier", {"ok.txt": "good"}), store=r)
    with pytest.raises(SystemExit) as exit:
        main(["publish", str(env / "out2"), "sweep", "--interval", "0.5", "--", sys.executable, "-c",
              "import pathlib, sys; p = pathlib.Path(sys.argv[1]); p.mkdir(); (p / 'x.txt').write_text('x'); sys.exit(3)", str(env / "out2")])
    assert exit.value.code == 3 and "failed" in capsys.readouterr().err
    assert [v["status"] for v in store.versions("sweep", store=r)] == ["completed", "failed"]
    assert (store.pull("sweep@latest", store=r) / "ok.txt").read_text() == "good"


# -------------------------------------------------------------------- pages


def publish_report_to(env, server):
    """The conftest project (reports alpha and beta, input `descent`) with its store switched to the server."""
    r = use_remote(env, server)
    store.publish("descent", make_data(env.parent / "remote-data"), store=r)
    main(["publish", "reports/alpha"])
    main(["publish", "reports/beta"])
    return r


def test_pages(server, project, capsys):
    r = publish_report_to(project, server)
    store.publish("datasets/other", folder(project, "o", {"a.txt": "1"}), aliases=["best"], store=r)
    capsys.readouterr()
    index = call(server, "GET", "/")[2].decode()
    assert "/p/study/" in index and "<td>2</td>" in index  # two reports
    page = call(server, "GET", "/p/study/")[2].decode()
    assert "/p/study/r/alpha/" in page and "Alpha" in page and "active" in page and "The first report" in page
    assert page.index("Alpha") < page.index("datasets/other")  # reports first, then the other artifacts
    assert "/p/study/a/datasets/other" in page and "best" in page and "/p/study/a/descent" in page
    # the artifact page: versions, files, metadata, inputs, consumers
    art = call(server, "GET", "/p/study/a/descent")[2].decode()
    assert "/files/study/descent@v1//metrics.jsonl" in art and "/p/study/a/reports/alpha@v1" in art  # a consumer
    assert "completed" in art and "latest" in art
    rep = call(server, "GET", "/p/study/a/reports/alpha")[2].decode()
    assert "/p/study/a/descent@v1" in rep and "open report" in rep and "/p/study/r/alpha@v1/" in rep  # an input
    assert "&quot;title&quot;: &quot;Alpha&quot;" in rep or '"title": "Alpha"' in rep.replace("&quot;", '"')
    assert call(server, "GET", "/p/study/a/descent@v9")[0] == 404
    assert call(server, "GET", "/p/nope/")[0] == 404
    assert call(server, "GET", "/p/study")[0] == 301


def test_report_is_served_with_its_static_files_and_data_links(server, project, capsys):
    publish_report_to(project, server)
    status, _, body = call(server, "GET", "/p/study/r/alpha/")
    page = body.decode()
    assert status == 200 and "<title>" in page
    script = re.search(r'src="\.\./(_static/report\.[0-9a-f]+\.js)"', page).group(1)  # relative: /p/study/r/_static/...
    status, headers, js = call(server, "GET", f"/p/study/r/{script}", headers={"Accept-Encoding": "gzip"})
    assert status == 200 and headers["Content-Encoding"] == "gzip" and "immutable" in headers["Cache-Control"]
    plain = call(server, "GET", f"/p/study/r/{script}")
    assert plain[0] == 200 and b"showData" in plain[2] and b"/p/" in plain[2]
    css = re.search(r'href="\.\./(_static/report\.[0-9a-f]+\.css)"', page).group(1)
    assert call(server, "GET", f"/p/study/r/{css}")[0] == 200
    data = json.loads(call(server, "GET", "/p/study/r/alpha/report-data.json")[2])
    assert data["inputs"]["descent"]["resolved"] == "study:descent@v1"
    assert b"Result" in call(server, "GET", "/p/study/r/alpha/narrative.html")[2]
    assert call(server, "GET", "/p/study/r/alpha/assets/inputs/descent/figures/hist.png")[0] == 200
    assert call(server, "GET", "/p/study/r/alpha")[0] == 301
    assert call(server, "GET", "/p/study/r/alpha@v1/")[0] == 200 and call(server, "GET", "/p/study/r/alpha@v2/")[0] == 404
    site = json.loads(call(server, "GET", "/p/study/r/site.json")[2])  # the page's navigation
    assert [item["key"] for item in site["reports"]] == ["alpha", "beta"] and site["reports"][0]["href"] == "alpha/"
    assert call(server, "GET", "/p/study/r/", token=server.token)[0] == 303
    assert call(server, "GET", "/p/study/r/_static/nope.js")[0] == 404
    assert call(server, "GET", "/p/study/r/_static/..%2Fx")[0] == 404
    # the Data panel's link target is the artifact page of the exact reference
    assert call(server, "GET", "/p/study/a/descent@v1")[0] == 200


def test_report_versions_stay_viewable(server, project, capsys):
    publish_report_to(project, server)
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text().replace("It works", "It still works"))
    main(["publish", "reports/alpha"])
    assert b"It still works" in call(server, "GET", "/p/study/r/alpha/narrative.html")[2]
    assert b"It still works" not in call(server, "GET", "/p/study/r/alpha@v1/narrative.html")[2]
    assert call(server, "GET", "/p/study/r/alpha@latest/")[0] == 200
    assert "/p/study/r/alpha@v1/" in call(server, "GET", "/p/study/a/reports/alpha")[2].decode()  # the version picker


def test_the_report_script_links_the_data_panel_only_when_store_served():
    from importlib import resources

    script = (resources.files("cairn_report") / "template" / "report.js").read_text()
    assert "STORE_SERVED = !BUNDLE && /^\\/p\\/[^/]+\\/r\\//.test(location.pathname)" in script
    assert "STORE_SERVED && match ?" in script and "version.dataset.artifact = input.resolved" in script


# -------------------------------------------------------------- end to end


def test_end_to_end_two_machines_lineage(server, tmp_path, monkeypatch):
    def machine(name):
        root = tmp_path / name
        root.mkdir()
        (root / "pyproject.toml").write_text("")
        (root / "cairn-report.toml").write_text(f'[store]\nurl = "{server.url}"\nproject = "study"\ninterval = 1\n')
        monkeypatch.chdir(root)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / f"cache-{name}"))
        monkeypatch.delenv("CAIRN_INPUTS", raising=False)
        return root

    # a dataset, published from somewhere
    a = machine("machine-a")
    main(["publish", str(folder(a, "toy-data", {"x.csv": "1,2,3\n"})), "datasets/toy"])
    # machine A: a plain script (no Hydra) pulls datasets/toy@v1 and writes outputs; CAIRN_INPUTS points at them
    out = a / "outputs"
    out.mkdir()
    script = ("import pathlib, sys; from cairn_report import store; src = store.pull('datasets/toy@v1'); "
              "pathlib.Path(sys.argv[1], 'result.txt').write_text('fit of ' + (src / 'x.csv').read_text().strip())")
    subprocess.run([sys.executable, "-c", script, str(out)], check=True, cwd=a,
                   env=os.environ | {"CAIRN_INPUTS": str(out / ".cairn-inputs.jsonl")})
    main(["publish", str(out), "toy-fit"])
    # machine B: a report with `inputs: fit: toy-fit@latest`
    b = machine("machine-b")
    write(b / "reports" / "summary" / "report.md", (
        "---\ntitle: Summary\nstatus: active\nsummary: Fit of the toy data.\ninputs:\n  fit: toy-fit@latest\n---\n\n"
        "```python exec hide\nresult = (inputs.fit / 'result.txt').read_text()\n```\n\n## Result\n\nThe result is {{ result }}.\n"))
    main(["publish", "reports/summary"])

    chain = "study:datasets/toy@v1", "study:toy-fit@v1", "study:reports/summary@v1"
    lineage = api(server, "GET", "/api/v2/lineage?ref=study:reports%2Fsummary@latest")
    assert lineage == {"ref": chain[2], "inputs": [chain[1]], "consumers": []}
    middle = api(server, "GET", "/api/v2/lineage?ref=study:toy-fit@v1")
    assert middle["inputs"] == [chain[0]] and middle["consumers"] == [chain[2]]
    assert api(server, "GET", "/api/v2/lineage?ref=study:datasets%2Ftoy@v1")["consumers"] == [chain[1]]
    # the same chain on the artifact pages, both ways
    toy = call(server, "GET", "/p/study/a/datasets/toy")[2].decode()
    assert "/p/study/a/toy-fit@v1" in toy
    fit = call(server, "GET", "/p/study/a/toy-fit")[2].decode()
    assert "/p/study/a/datasets/toy@v1" in fit and "/p/study/a/reports/summary@v1" in fit
    report = call(server, "GET", "/p/study/a/reports/summary")[2].decode()
    assert "/p/study/a/toy-fit@v1" in report
    assert b"fit of 1,2,3" in call(server, "GET", "/p/study/r/summary/narrative.html")[2]
    # the report data records its exact input for the Data panel
    assert json.loads(call(server, "GET", "/p/study/r/summary/report-data.json")[2])["inputs"]["fit"]["resolved"] == chain[1]
