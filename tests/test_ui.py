import json
import os
import re
import subprocess
import sys
import threading
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

import cairn as store
from cairn.cli import main
from cairn import LocalStore, RemoteStore
from cairn.tokens import create_token, revoke_token
from cairn.ui import server as ui_server
from cairn.ui.server import LocalBackend, RemoteBackend, make_ui_server

from conftest import make_data, write
from test_server import api, call, remote, seed, server, use_remote  # noqa: F401  (the store server in a thread, helpers)
from test_store import env, folder  # noqa: F401

X = {"X-Cairn": "1"}
JSON = {**X, "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def quick_logins(monkeypatch):
    monkeypatch.setattr(ui_server, "LOGIN_DELAY", 0)


def start(backend, **options):
    httpd = make_ui_server(backend, port=0, **options)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, SimpleNamespace(port=httpd.server_address[1], token="")


@pytest.fixture(params=["folder", "server"])
def ui(request, server):
    """A UI (login required) over the store folder itself, and over the store server in its thread."""
    backend = LocalBackend(server.root, no_auth=False) if request.param == "folder" else RemoteBackend(server.url, allow_http=False)
    httpd, stack = start(backend, secure_cookie=False)
    stack.kind, stack.server = request.param, server
    yield stack
    httpd.shutdown()
    httpd.server_close()


def login(ui, token=None):
    """A session cookie header for a token (the owner's by default)."""
    status, headers, _ = call(ui, "POST", "/ui-api/login", body=json.dumps({"token": token or ui.server.token}), token=None, headers=JSON)
    assert status == 200, status
    return {"Cookie": headers["Set-Cookie"].split(";")[0]}


def get(ui, path, cookie, **headers):
    return call(ui, "GET", path, token=None, headers={**cookie, **headers})


def data(ui, path, cookie, expect=200):
    """A /ui-api/ call as the app makes it (session cookie and the X-Cairn header), decoded."""
    status, _, body = get(ui, "/ui-api/" + path, cookie, **X)
    assert status == expect, (path, status, body[:200])
    return json.loads(body)


def publish_reports(project, server):
    """The conftest project (reports alpha and beta, input `descent`) with its store switched to the server."""
    r = use_remote(project, server)
    store.publish("descent", make_data(project.parent / "remote-data"), store=r)
    main(["publish", "reports/alpha"])
    main(["publish", "reports/beta"])
    return r


# --------------------------------------------------------------------- login


def test_login_is_required_and_the_token_stays_server_side(ui):
    token = ui.server.token
    for path in ("/", "/p/study/", "/p/study/a/data", "/p/study/r/site.json"):
        status, headers, _ = call(ui, "GET", path, token=None)
        assert status == 303 and headers["Location"].startswith("/login"), path
    assert call(ui, "GET", "/files/study/data@v1//x", token=None)[0] == 401
    assert call(ui, "GET", "/ui-api/me", token=None, headers=X)[0] == 401
    assert call(ui, "GET", "/", token="owner", headers={})[0] == 303  # a bearer header is not a login
    status, _, page = call(ui, "GET", "/login?next=/p/study/", token=None)  # the app's login route (a static page)
    assert status == 200 and b'<div id="root">' in page
    # a wrong token is rejected and sets nothing
    status, headers, body = call(ui, "POST", "/ui-api/login", body=json.dumps({"token": "w" * 40}), token=None, headers=JSON)
    assert status == 401 and "Set-Cookie" not in headers and b"not valid" in body
    assert call(ui, "POST", "/ui-api/login", body='{"token": ""}', token=None, headers=JSON)[0] == 401
    assert call(ui, "POST", "/ui-api/login", body="nonsense", token=None, headers=JSON)[0] == 400
    assert call(ui, "POST", "/ui-api/login", body=json.dumps({"token": token}), token=None, headers={"Content-Type": "application/json"})[0] == 403  # no X-Cairn
    assert call(ui, "POST", "/login", body=f"token={token}", token=None, headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 404
    # the right one gives an opaque, HttpOnly, SameSite=Strict session id
    status, headers, body = call(ui, "POST", "/ui-api/login", body=json.dumps({"token": token}), token=None, headers=JSON)
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and token not in cookie and "Secure" not in cookie  # secure_cookie=False
    assert token.encode() not in body
    session = {"Cookie": cookie.split(";")[0]}
    for path in ("/", "/p/study/", "/ui-api/me"):
        status, headers, body = get(ui, path, session, **X)
        assert status in (200, 404) and token.encode() not in body and token not in json.dumps(dict(headers))
    assert get(ui, "/", session)[0] == 200
    assert get(ui, "/login?next=/p/study/", session)[1]["Location"] == "/p/study/"  # logged in already
    assert get(ui, "/login?next=//evil.example", session)[1]["Location"] == "/"  # open redirects are refused
    forged = {"Cookie": "cairn_ui=" + "a" * 43}
    assert get(ui, "/", forged)[0] == 303 and get(ui, "/ui-api/me", forged, **X)[0] == 401
    status, headers, _ = get(ui, "/logout", session)
    assert status == 303 and headers["Location"] == "/login" and "Max-Age=0" in headers["Set-Cookie"]
    assert get(ui, "/", session)[0] == 303  # the session is gone


def test_the_api_needs_the_x_cairn_header(ui):
    cookie = login(ui)
    for path in ("me", "projects", "projects/study", "lineage?ref=study:x@v1"):
        assert get(ui, "/ui-api/" + path, cookie)[0] == 403, path  # the session alone is not enough
        assert get(ui, "/ui-api/" + path, cookie, **{"X-Cairn": "0"})[0] == 403
    assert get(ui, "/ui-api/me", {}, **X)[0] == 401  # and the header alone is not a login
    assert get(ui, "/ui-api/nope", cookie, **X)[0] == 404
    assert data(ui, "me", cookie) == {"scope": "owner" if ui.kind == "folder" else "store", "project": None, "auth": True}


def test_the_cookie_is_secure_by_default(server):
    httpd, ui = start(RemoteBackend(server.url, allow_http=False))
    try:
        status, headers, _ = call(ui, "POST", "/ui-api/login", body=json.dumps({"token": server.token}), token=None, headers=JSON)
        assert status == 200 and "Secure" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_scoped_token_sees_only_its_project_and_revocation_ends_the_session(ui, env):
    seed(ui.server)
    token = create_token(ui.server.root, "alpha", name="viewer")
    cookie = login(ui, token)
    me = data(ui, "me", cookie)
    assert me["scope"] == ("project" if ui.kind == "folder" else "store") and (me["project"] == "alpha" or ui.kind == "server")
    assert [p["name"] for p in data(ui, "projects", cookie)] == ["alpha"]
    assert data(ui, "projects/alpha", cookie)["project"]["name"] == "alpha"
    assert data(ui, "projects/alpha/artifacts/data", cookie)["name"] == "data"
    assert data(ui, "projects/alpha/artifacts/data/versions/1", cookie)["version"]["version"] == 1
    assert data(ui, "lineage?ref=alpha:data@v1", cookie)["ref"] == "alpha:data@v1"
    for path in ("projects/beta", "projects/beta/artifacts/data", "projects/beta/artifacts/data/versions/1", "lineage?ref=beta:data@v1"):
        data(ui, path, cookie, expect=403)
    assert get(ui, "/p/beta/r/site.json", cookie)[0] == 403
    assert get(ui, "/files/beta/data@v1//x.txt", cookie)[0] == 403
    assert get(ui, "/files/alpha/data@v1//x.txt", cookie)[2] == b"alpha"
    assert get(ui, "/p/beta/", cookie)[0] == 200  # the app route itself; its data calls are refused above
    revoke_token(ui.server.root, "viewer")
    status, headers, _ = get(ui, "/p/alpha/", cookie)
    if ui.kind == "folder":  # a folder's tokens are checked at once; a store server answers 401 on the next call
        assert status == 303 and headers["Location"].startswith("/login")
    else:
        assert get(ui, "/ui-api/projects", cookie, **X)[0] == 401
    assert call(ui, "POST", "/ui-api/login", body=json.dumps({"token": token}), token=None, headers=JSON)[0] == 401  # and cannot log in again


def test_a_scoped_token_hides_other_projects_in_inputs_and_consumers(ui, env):
    seed(ui.server)
    r = remote(ui.server, project="beta")
    store.publish("beta:derived", folder(env, "d", {"r.txt": "r"}), inputs=["alpha:data@v1"], store=r)
    cookie = login(ui, create_token(ui.server.root, "alpha", name="viewer"))
    assert data(ui, "projects/alpha/artifacts/data/versions/1", cookie)["consumers"] == ["beta:derived@v1"] or ui.kind == "server"
    lineage = data(ui, "lineage?ref=alpha:data@v1&depth=2", cookie)
    assert "beta:derived@v1" not in json.dumps(lineage)  # a consumer in a project this token cannot read
    owner = data(ui, "lineage?ref=alpha:data@v1", login(ui))
    assert owner["nodes"]["alpha:data@v1"]["consumers"] == ["beta:derived@v1"]


def test_no_auth_ui_is_loopback_only_and_needs_no_login(server):
    with pytest.raises(SystemExit, match="loopback"):
        make_ui_server(RemoteBackend(server.url, allow_http=True), host="0.0.0.0", port=0, no_auth=True)
    for backend in (LocalBackend(server.root, no_auth=True), RemoteBackend(server.url, False, token=server.token)):
        httpd, ui = start(backend, no_auth=True)
        try:
            status, _, body = call(ui, "GET", "/", token=None)
            assert status == 200 and b'<div id="root">' in body
            assert call(ui, "GET", "/ui-api/me", token=None)[0] == 403  # the header is needed without a login too
            assert json.loads(call(ui, "GET", "/ui-api/me", token=None, headers=X)[2])["auth"] is False
            assert call(ui, "GET", "/login", token=None)[0] == 404
            assert call(ui, "POST", "/ui-api/login", body=json.dumps({"token": server.token}), token=None, headers=JSON)[0] == 404
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_no_auth_ui_over_a_no_auth_store_uses_no_token(tmp_path, env, monkeypatch):
    from cairn.server import make_server
    from cairn.tokens import init_root

    root = tmp_path / "open-store"
    init_root(root)
    store_httpd = make_server(root, port=0, no_auth=True)
    threading.Thread(target=store_httpd.serve_forever, daemon=True).start()
    monkeypatch.delenv("CAIRN_STORE_TOKEN", raising=False)
    url = f"http://127.0.0.1:{store_httpd.server_address[1]}"
    (env / "cairn.toml").write_text(f'[store]\nurl = "{url}"\nproject = "study"\n')
    store.publish("open", folder(env, "o", {"a.txt": "x"}))
    httpd, ui = start(RemoteBackend(url, False), no_auth=True)
    try:
        status, _, body = call(ui, "GET", "/ui-api/projects/study/artifacts/open", token=None, headers=X)
        assert status == 200 and json.loads(body)["name"] == "open"
    finally:
        httpd.shutdown()
        store_httpd.shutdown()
        httpd.server_close()
        store_httpd.server_close()


def test_a_remote_ui_refuses_plain_http_to_a_non_local_store():
    with pytest.raises(SystemExit, match="plain HTTP"):
        RemoteBackend("http://store.example.org:8800", allow_http=False)
    RemoteBackend("http://store.example.org:8800", allow_http=True)


# --------------------------------------------------------------------- pages


def test_the_app_routes_serve_the_built_index(ui):
    cookie = login(ui)
    index = (Path(ui_server.__file__).parent / "static" / "index.html").read_bytes()
    for path in ("/", "/p/study/", "/p/study/a/data", "/p/study/a/reports/alpha@v1", "/p/anything/a/a/b/c@best", "/login"):
        status, headers, body = get(ui, path, cookie)
        if path == "/login":
            assert status == 303  # logged in already
            status, headers, body = call(ui, "GET", path, token=None)
        assert status == 200 and body == index and headers["Cache-Control"] == "no-cache", path
    assert get(ui, "/p/study", cookie)[0] == 301
    assert get(ui, "/nope", cookie)[0] == 404
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.decode())
    assert len(assets) == 2
    for asset in assets:  # served without a session, cached forever
        status, headers, body = call(ui, "GET", asset, token=None)
        assert status == 200 and "immutable" in headers["Cache-Control"] and body
    assert call(ui, "GET", "/assets/..%2Findex.html", token=None)[0] == 404
    assert call(ui, "GET", "/assets/nope.js", token=None)[0] == 404


def test_api_returns_what_the_pages_need(ui, project, capsys):
    r = publish_reports(project, ui.server)
    store.publish("datasets/other", folder(project, "o", {"a.txt": "1"}), aliases=["best"], store=r)
    capsys.readouterr()
    cookie = login(ui)
    projects = data(ui, "projects", cookie)
    assert [p["name"] for p in projects] == ["study"] and projects[0]["artifacts"] == 4  # descent, two reports, datasets/other
    assert {"title", "description", "updated"} <= set(projects[0])
    page = data(ui, "projects/study", cookie)
    assert page["project"]["name"] == "study"
    by_name = {a["name"]: a for a in page["artifacts"]}
    assert set(by_name) == {"descent", "reports/alpha", "reports/beta", "datasets/other"}
    alpha = by_name["reports/alpha"]
    assert alpha["latest"] == 1 and alpha["metadata"]["title"] == "Alpha" and alpha["metadata"]["status"] == "active" and alpha["metadata"]["summary"].startswith("The first report")
    assert by_name["datasets/other"]["aliases"]["best"] == 1 and alpha["updated"]
    # an artifact: summary, versions (without file lists), aliases; the name may be one encoded segment or several
    art = data(ui, "projects/study/artifacts/datasets%2Fother", cookie)
    assert art == data(ui, "projects/study/artifacts/datasets/other", cookie)
    assert art["summary"]["latest"] == 1 and art["aliases"] == {"latest": 1, "best": 1}
    row = art["versions"][0]
    assert row["version"] == 1 and row["status"] == "completed" and row["file_count"] == 1 and row["size"] == 1 and "files" not in row and "created" in row
    # a version: the manifest with files, metadata, inputs and consumers; by number, v<n>, alias
    for selector in ("1", "v1", "best", "latest"):
        v = data(ui, f"projects/study/artifacts/datasets%2Fother/versions/{selector}", cookie)
        assert v["version"]["version"] == 1 and [f["path"] for f in v["version"]["files"]] == ["a.txt"] and v["consumers"] == []
    descent = data(ui, "projects/study/artifacts/descent/versions/1", cookie)
    paths = {f["path"] for f in descent["version"]["files"]}
    assert "metrics.jsonl" in paths and descent["consumers"] == ["study:reports/alpha@v1"]
    report = data(ui, "projects/study/artifacts/reports%2Falpha/versions/1", cookie)
    assert report["version"]["inputs"] == ["study:descent@v1"] and report["version"]["metadata"]["title"] == "Alpha"
    assert any(f["path"].startswith("site/") for f in report["version"]["files"])
    # errors are JSON
    for path in ("projects/study/artifacts/descent/versions/9", "projects/study/artifacts/nope", "projects/nope", "projects/study/artifacts/descent/versions/zzz"):
        assert "error" in data(ui, path, cookie, expect=404), path
    assert data(ui, "lineage?ref=nope", cookie, expect=400)["error"]
    assert data(ui, "lineage?ref=study:descent@v1&depth=9", cookie, expect=400)["error"]


def test_lineage_levels(ui, tmp_path, env):
    r = use_remote(env, ui.server)
    store.publish("a", folder(env, "a", {"f": "1"}), store=r)
    store.publish("b", folder(env, "b", {"f": "2"}), inputs=["study:a@v1"], store=r)
    store.publish("c", folder(env, "c", {"f": "3"}), inputs=["study:b@v1"], store=r)
    store.publish("d", folder(env, "d", {"f": "4"}), inputs=["study:c@v1"], store=r)
    cookie = login(ui)
    one = data(ui, "lineage?ref=study:b@latest", cookie)  # depth 1, an alias resolves to the exact version
    assert one["ref"] == "study:b@v1" and one["depth"] == 1 and not one["truncated"]
    assert one["nodes"] == {"study:b@v1": {"inputs": ["study:a@v1"], "consumers": ["study:c@v1"]},
                            "study:a@v1": {"inputs": None, "consumers": None}, "study:c@v1": {"inputs": None, "consumers": None}}
    two = data(ui, "lineage?ref=study:b@v1&depth=2", cookie)["nodes"]
    assert two["study:a@v1"] == {"inputs": [], "consumers": None}  # ancestors expand through inputs, descendants through consumers
    assert two["study:c@v1"] == {"inputs": None, "consumers": ["study:d@v1"]}
    assert two["study:d@v1"] == {"inputs": None, "consumers": None}
    deep = data(ui, "lineage?ref=study:d@v1&depth=3", cookie)["nodes"]
    assert deep["study:a@v1"]["inputs"] is None and deep["study:b@v1"]["inputs"] == ["study:a@v1"]  # three levels reach a, not beyond


def test_lineage_is_capped(ui, env, monkeypatch):
    monkeypatch.setattr(ui_server, "MAX_LINEAGE_NODES", 4)
    r = use_remote(env, ui.server)
    store.publish("root", folder(env, "root", {"f": "0"}), store=r)
    for number in range(6):
        store.publish(f"leaf{number}", folder(env, f"leaf{number}", {"f": str(number)}), inputs=["study:root@v1"], store=r)
    lineage = data(ui, "lineage?ref=study:root@v1", login(ui))
    assert lineage["truncated"] and len(lineage["nodes"]) == 4


def test_report_is_served_with_its_static_files(ui, project, capsys):
    publish_reports(project, ui.server)
    cookie = login(ui)
    status, _, body = get(ui, "/p/study/r/alpha/", cookie)
    page = body.decode()
    assert status == 200 and "<title>" in page
    script = re.search(r'src="\.\./(_static/report\.[0-9a-f]+\.js)"', page).group(1)  # relative: /p/study/r/_static/...
    status, headers, js = get(ui, f"/p/study/r/{script}", cookie, **{"Accept-Encoding": "gzip"})
    assert status == 200 and headers["Content-Encoding"] == "gzip" and "immutable" in headers["Cache-Control"]
    plain = get(ui, f"/p/study/r/{script}", cookie)
    assert plain[0] == 200 and b"showData" in plain[2] and b"/p/" in plain[2]
    css = re.search(r'href="\.\./(_static/report\.[0-9a-f]+\.css)"', page).group(1)
    assert get(ui, f"/p/study/r/{css}", cookie)[0] == 200
    data = json.loads(get(ui, "/p/study/r/alpha/report-data.json", cookie)[2])
    assert data["inputs"]["descent"]["resolved"] == "study:descent@v1"
    assert b"Result" in get(ui, "/p/study/r/alpha/narrative.html", cookie)[2]
    assert get(ui, "/p/study/r/alpha/assets/inputs/descent/figures/hist.png", cookie)[0] == 200
    assert get(ui, "/p/study/r/alpha", cookie)[0] == 301
    assert get(ui, "/p/study/r/alpha@v1/", cookie)[0] == 200 and get(ui, "/p/study/r/alpha@v2/", cookie)[0] == 404
    site = json.loads(get(ui, "/p/study/r/site.json", cookie)[2])  # the page's navigation
    assert [item["key"] for item in site["reports"]] == ["alpha", "beta"] and site["reports"][0]["href"] == "alpha/"
    assert site["title"] == "study" and site["reports"][0]["title"] == "Alpha" and site["reports"][0]["status"] == "active"
    assert get(ui, "/p/study/r/", cookie)[0] == 303
    assert get(ui, "/p/study/r/_static/nope.js", cookie)[0] == 404
    assert get(ui, "/p/study/r/_static/..%2Fx", cookie)[0] == 404
    assert get(ui, "/p/study/r/nope/", cookie)[0] == 404


def test_static_files_need_the_session_token(ui, project, capsys):
    publish_reports(project, ui.server)
    script = re.search(r'src="\.\./(_static/report\.[0-9a-f]+\.js)"', get(ui, "/p/study/r/alpha/", login(ui))[2].decode()).group(1)
    other = create_token(ui.server.root, "unrelated", name="o")
    # the UI remembers where a shared file lives, not its bytes: another session still goes through the store's access check
    assert get(ui, f"/p/study/r/{script}", login(ui, other))[0] == 403


def test_report_versions_stay_viewable(ui, project, capsys):
    publish_reports(project, ui.server)
    cookie = login(ui)
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text().replace("It works", "It still works"))
    main(["publish", "reports/alpha"])
    assert b"It still works" in get(ui, "/p/study/r/alpha/narrative.html", cookie)[2]
    assert b"It still works" not in get(ui, "/p/study/r/alpha@v1/narrative.html", cookie)[2]
    assert get(ui, "/p/study/r/alpha@latest/", cookie)[0] == 200
    versions = data(ui, "projects/study/artifacts/reports%2Falpha", cookie)["versions"]  # the version picker's data
    assert [v["version"] for v in versions] == [1, 2] and all(v["status"] == "completed" for v in versions)


def test_data_panel_links_point_at_the_artifact_pages(ui, project, capsys):
    publish_reports(project, ui.server)
    cookie = login(ui)
    script = resources.files("cairn").joinpath("template", "report.js").read_text()
    assert "STORE_SERVED = !BUNDLE && /^\\/p\\/[^/]+\\/r\\//.test(location.pathname)" in script
    assert "STORE_SERVED && match ?" in script and "version.dataset.artifact = input.resolved" in script
    assert "/p/${encodeURIComponent(match[1])}/a/" in script  # /p/<project>/r/... -> /p/<project>/a/<name>@vN
    report_data = json.loads(get(ui, "/p/study/r/alpha/report-data.json", cookie)[2])
    exact = report_data["inputs"]["descent"]["resolved"]
    project_name, name_version = exact.split(":", 1)
    name, version = name_version.split("@")
    target = f"/p/{project_name}/a/{name}@{version}"  # what the script builds from the exact reference
    assert get(ui, target, cookie)[2] == (Path(ui_server.__file__).parent / "static" / "index.html").read_bytes()  # an app route
    detail = data(ui, f"projects/{project_name}/artifacts/{name}/versions/{version[1:]}", cookie)  # and the app's data for it
    assert "metrics.jsonl" in {f["path"] for f in detail["version"]["files"]}


# --------------------------------------------------------------------- files


def test_files_passthrough_answers_range_etag_and_download(ui, env):
    r = use_remote(env, ui.server)
    store.publish("data", folder(env, "d", {"big.txt": "0123456789" * 10, "sub/page.html": "<b>x</b>"}), store=r)
    cookie = login(ui)
    path = "/files/study/data@v1//big.txt"
    status, headers, whole = get(ui, path, cookie)
    assert status == 200 and len(whole) == 100 and "immutable" in headers["Cache-Control"] and headers["ETag"]
    assert headers["Accept-Ranges"] == "bytes" and headers["Content-Security-Policy"] == "sandbox"
    status, headers, data = get(ui, path, cookie, Range="bytes=5-14")
    assert status == 206 and data == whole[5:15] and headers["Content-Range"] == "bytes 5-14/100" and headers["Content-Length"] == "10"
    assert get(ui, path, cookie, Range="bytes=90-")[2] == whole[90:]
    assert get(ui, path, cookie, Range="bytes=-5")[2] == whole[-5:]
    assert get(ui, path, cookie, Range="bytes=100-")[0] == 416
    assert get(ui, path, cookie, **{"If-None-Match": headers["ETag"]})[0] == 304
    assert "attachment" in get(ui, path + "?download=1", cookie)[1]["Content-Disposition"]
    status, headers, _ = get(ui, "/files/study/data@latest//sub/page.html", cookie)  # alias: not immutable
    assert status == 200 and headers["Cache-Control"] == "no-cache" and headers["Content-Type"].startswith("text/html")
    assert get(ui, "/files/study/data@v1//nope.txt", cookie)[0] == 404
    assert get(ui, "/files/study/data@v9//big.txt", cookie)[0] == 404
    assert get(ui, "/files/study/data@v1/big.txt", cookie)[0] == 404  # no //
    assert get(ui, "/files/nope/data@v1//big.txt", cookie)[0] == 404


def test_files_passthrough_serves_large_ranges_exactly(ui, env):
    big = bytes(range(256)) * 9000  # 2.3 MB: more than one read chunk
    root = env / "big"
    root.mkdir()
    (root / "movie.mp4").write_bytes(big)
    store.publish("video", root, store=use_remote(env, ui.server))
    cookie = login(ui)
    status, headers, data = get(ui, "/files/study/video@v1//movie.mp4", cookie, Range="bytes=1048000-1049999")
    assert status == 206 and data == big[1048000:1050000] and headers["Content-Range"] == f"bytes 1048000-1049999/{len(big)}"
    assert get(ui, "/files/study/video@v1//movie.mp4", cookie)[2] == big


# ------------------------------------------------------------ separation


def test_the_ui_uses_only_the_store_interface_and_the_server_has_no_ui_code():
    source = Path(__file__).parent.parent / "src" / "cairn"
    for file in (source / "ui").glob("*.py"):
        text = file.read_text()
        for forbidden in ("blob_path", "versions.jsonl", "aliases.jsonl", "consumers.jsonl", "project.json", "_blobs"):
            assert forbidden not in text, (file.name, forbidden)
    store_files = [source / f"{name}.py" for name in ("refs", "local", "remote", "cache", "blobs", "tokens", "migrate")]
    for file in [*store_files, source / "server" / "__init__.py"]:
        text = file.read_text()
        assert "import ui" not in text and "from ..ui" not in text and "from .pages" not in text and "cairn.ui" not in text, file.name
    assert not (source / "server" / "pages.py").exists()
    server_text = (source / "server" / "__init__.py").read_text()
    for gone in ("text/html", "cookie", "Cookie", "is_report", "site.json", "_static", "/login"):
        assert gone not in server_text.replace("there are no cookies", "").replace("--insecure-cookies", ""), gone


def test_ui_command_wires_the_configured_store(env, server, monkeypatch, capsys):
    (env / "cairn.toml").write_text(f'[store]\nurl = "{server.url}"\nproject = "study"\n')
    seen = {}
    monkeypatch.setattr("cairn.ui.serve_ui", lambda backend, **options: seen.update(backend=backend, **options))
    main(["ui"])
    assert isinstance(seen["backend"], RemoteBackend) and seen["backend"].url == server.url and seen["port"] == 8801
    assert seen["no_auth"] is False and seen["secure_cookie"] is True
    main(["ui", "--store", str(server.root), "--port", "9999", "--insecure-cookies"])
    assert isinstance(seen["backend"], LocalBackend) and seen["port"] == 9999 and seen["secure_cookie"] is False
    (env / "cairn.toml").write_text('[store]\npath = "mystore"\n')
    LocalStore(env / "mystore")
    main(["ui", "--no-auth"])
    assert seen["backend"].root == (env / "mystore").resolve() and seen["no_auth"] is True
    with pytest.raises(SystemExit, match="no store folder"):
        main(["ui", "--store", str(env / "missing")])


def test_serve_ui_prints_where_it_runs(server, capsys, monkeypatch):
    monkeypatch.setattr(ui_server.ThreadingHTTPServer, "serve_forever", lambda self: self.server_close())
    ui_server.serve_ui(RemoteBackend(server.url, False), port=0)
    out = capsys.readouterr().out
    assert re.fullmatch(rf"UI: http://127\.0\.0\.1:\d+/ \(store: {re.escape(server.url)}\)\n", out) and server.token not in out


# -------------------------------------------------------------- end to end


def test_end_to_end_two_machines_lineage_through_the_ui(ui, tmp_path, monkeypatch):
    server = ui.server

    def machine(name):
        root = tmp_path / name
        root.mkdir()
        (root / "pyproject.toml").write_text("")
        (root / "cairn.toml").write_text(f'[store]\nurl = "{server.url}"\nproject = "study"\ninterval = 1\n')
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
    script = ("import pathlib, sys; import cairn as store; src = store.pull('datasets/toy@v1'); "
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
    # the same chain in the UI's data, both ways
    cookie = login(ui)

    def version(name):
        return data(ui, f"projects/study/artifacts/{name.replace('/', '%2F')}/versions/1", cookie)

    assert version("datasets/toy")["consumers"] == [chain[1]]
    assert version("toy-fit")["version"]["inputs"] == [chain[0]] and version("toy-fit")["consumers"] == [chain[2]]
    assert version("reports/summary")["version"]["inputs"] == [chain[1]]
    graph = data(ui, "lineage?ref=study:reports%2Fsummary@v1&depth=2", cookie)["nodes"]
    assert set(graph) == set(chain) and graph[chain[1]]["inputs"] == [chain[0]]
    assert b"fit of 1,2,3" in get(ui, "/p/study/r/summary/narrative.html", cookie)[2]
    # the report data records its exact input for the Data panel
    assert json.loads(get(ui, "/p/study/r/summary/report-data.json", cookie)[2])["inputs"]["fit"]["resolved"] == chain[1]
    assert b"1,2,3" in get(ui, "/files/study/datasets/toy@v1//x.csv", cookie)[2]


# ------------------------------------------------------------- the built app


def test_the_built_app_ships_with_the_package():
    static = Path(ui_server.__file__).parent / "static"
    index = (static / "index.html").read_text()
    scripts = re.findall(r'<script[^>]+src="/assets/([^"]+\.js)"', index)
    styles = re.findall(r'<link[^>]+href="/assets/([^"]+\.css)"', index)
    assert len(scripts) == 1 and len(styles) == 1 and '<div id="root">' in index
    for name in (*scripts, *styles):
        assert (static / "assets" / name).stat().st_size > 1000, name
    assert sorted(path.name for path in (static / "assets").iterdir()) == sorted([*scripts, *styles])  # no stale builds
    css = (static / "assets" / styles[0]).read_text()
    for token in (".rail", ".hero", ".figure-card", ".image-copy", ".data-row", "--accent", "prefers-color-scheme:dark", ".graph-node"):
        assert token in css, token  # report.css, report-extra.css and app.css are all in it
    assert "/ui-api/" in (static / "assets" / scripts[0]).read_text()
