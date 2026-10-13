import http.client
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

from cairn_report.build import build_all
from cairn_report.cli import main
from cairn_report.config import load_config
from cairn_report.hub import make_hub
from cairn_report.logger import RunLogger
from cairn_report.push import Hub, Pusher, fetch, pull_comments, push

TOKEN = "t" * 40
needs_graphviz = pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz `dot` not installed")


@pytest.fixture
def hub(tmp_path):
    root = tmp_path / "hub"
    root.mkdir()
    server = make_hub(root, port=0, token=TOKEN, secure_cookie=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{server.server_address[1]}", "root": root, "port": server.server_address[1]}
    server.shutdown()


@pytest.fixture
def project(tmp_path, hub, monkeypatch):
    folder = tmp_path / "study"
    folder.mkdir()
    (folder / "pyproject.toml").write_text("")
    (folder / "cairn-report.toml").write_text(f'[hub]\nurl = "{hub["url"]}"\nproject = "study"\n')
    monkeypatch.setenv("CAIRN_HUB_TOKEN", TOKEN)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    main(["--project", str(folder), "init"])
    run = folder / "outputs" / "demo" / "2026-01-01_00-00-00"
    RunLogger(run).scalar("rmse", 0.5)
    (run / "run.json").write_text(json.dumps({"status": "completed"}))
    (run / ".hydra").mkdir()
    (run / ".hydra" / "config.yaml").write_text("seed: 0\n")
    (run.parent / "latest").symlink_to(run.name)
    return folder


def call(hub, method, path, body=None, headers=None, token=TOKEN):
    connection = http.client.HTTPConnection("127.0.0.1", hub["port"])
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), data


def tree(hub):
    status, _, data = call(hub, "GET", "/api/v1/projects/study/tree")
    assert status == 200
    return {entry["path"]: entry for entry in json.loads(data)}


def test_auth_required(hub, project):
    assert call(hub, "GET", "/api/v1/projects", token=None)[0] == 401
    assert call(hub, "GET", "/api/v1/projects", token="x" * 40)[0] == 401
    status, headers, _ = call(hub, "GET", "/", token=None)
    assert status == 303 and headers["Location"].startswith("/login")
    assert call(hub, "PUT", "/api/v1/blobs/" + "0" * 64, body=b"x", token=None)[0] == 401


def test_login_cookie(hub, project):
    status, headers, _ = call(hub, "POST", "/login", body=f"token={'w' * 40}&next=/", token=None,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 401 and "Set-Cookie" not in headers
    status, headers, _ = call(hub, "POST", "/login", body=f"token={TOKEN}&next=//evil.example", token=None,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert TOKEN not in cookie
    assert call(hub, "GET", "/", token=None, headers={"Cookie": cookie})[0] == 200


def test_push_is_incremental_and_keeps_history(hub, project):
    config = load_config(project)
    assert push(config) == 0
    files = tree(hub)
    assert files["outputs/demo/latest"]["link"] == "2026-01-01_00-00-00"
    assert "outputs/demo/2026-01-01_00-00-00/.hydra/config.yaml" in files
    assert "reports/report.md" in files
    pusher = Pusher(Hub(hub["url"], TOKEN), config, "study")
    assert pusher.once() == (0, 0)
    RunLogger(project / "outputs" / "demo" / "2026-01-01_00-00-00").scalar("rmse", 0.25)
    assert pusher.once()[0] == 1
    index = (hub["root"] / "projects" / "study" / "files.jsonl").read_text().splitlines()
    versions = [json.loads(line) for line in index if '"outputs/demo/2026-01-01_00-00-00/metrics.jsonl"' in line]
    assert len(versions) == 2  # the old version is still recorded
    status, _, data = call(hub, "GET", "/files/study/outputs/demo/latest/metrics.jsonl")
    assert status == 200 and data.count(b"\n") == 2


def test_rejects_bad_uploads_and_paths(hub, project):
    status, _, _ = call(hub, "PUT", "/api/v1/blobs/" + "a" * 64, body=b"not matching", headers={"Content-Length": "12"})
    assert status == 400
    assert not list((hub["root"] / "blobs").rglob("*"))
    for entry in ({"path": "outputs/../../etc/passwd", "sha256": "a" * 64, "size": 1},
                  {"path": "secrets/key", "sha256": "a" * 64, "size": 1},
                  {"path": "outputs/x/latest", "link": "../../../etc"},
                  {"path": "/abs/path", "sha256": "a" * 64, "size": 1}):
        status, _, _ = call(hub, "POST", "/api/v1/projects/study/tree", body=json.dumps({"entries": [entry]}),
                            headers={"Content-Type": "application/json"})
        assert status == 400, entry
    status, _, data = call(hub, "POST", "/api/v1/projects/study/tree",
                           body=json.dumps({"entries": [{"path": "outputs/x", "sha256": "b" * 64, "size": 1}]}),
                           headers={"Content-Type": "application/json"})
    assert status == 409 and json.loads(data)["missing"] == ["b" * 64]


def test_token_never_in_config(tmp_path):
    (tmp_path / "cairn-report.toml").write_text('[hub]\ntoken = "secret"\n')
    with pytest.raises(ValueError, match="never put the hub token"):
        load_config(tmp_path)


@needs_graphviz
def test_reports_served_and_comments_round_trip(hub, project):
    config = load_config(project)
    build_all(config)
    push(config)
    status, headers, page = call(hub, "GET", "/p/study/")
    assert status == 200 and b"<html" in page.lower()
    status, _, data = call(hub, "GET", "/p/study/api/reports")
    reports = json.loads(data)
    assert [report["slug"] for report in reports] == ["overview", "local-fitting"]
    assert reports[1]["href"] == "reports/local-fitting/"
    assert call(hub, "GET", "/p/study/reports/local-fitting/report-data.json")[0] == 200
    narrative = call(hub, "GET", "/p/study/reports/local-fitting/narrative.html")[2].decode()
    assert 'href="/reports/' not in narrative and 'href="../../' in narrative  # links work under /p/study/
    status, _, runs = call(hub, "GET", "/runs/study/")
    assert b"completed" in runs and b"latest" in runs

    login = call(hub, "POST", "/login", body=f"token={TOKEN}&next=/", token=None,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    cookie = login[1]["Set-Cookie"].split(";")[0]
    comment = json.dumps({"target": "section:abstract", "text": "Check the baseline.", "quote": "baseline"})
    api = "/p/study/api/reports/local-fitting/comments"
    assert call(hub, "POST", api, body=comment, token=None, headers={"Cookie": cookie, "Content-Type": "text/plain"})[0] == 401
    assert call(hub, "POST", api, body=comment, token=None,
                headers={"Cookie": cookie, "Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 401
    status, _, data = call(hub, "POST", api, body=comment, token=None,
                           headers={"Cookie": cookie, "Content-Type": "application/json"})
    assert status == 201
    folded = json.loads(call(hub, "GET", api)[2])
    assert any(row["text"] == "Check the baseline." for row in folded)

    assert pull_comments(config) == 1
    assert pull_comments(config) == 0
    log = project / "reports" / "local-fitting" / "knowledge" / "comments.jsonl"
    assert "Check the baseline." in log.read_text()
    push(config)  # the local log now holds the hub's record too; the merged view has no duplicate
    assert len(json.loads(call(hub, "GET", api)[2])) == len(folded)


@needs_graphviz
def test_fetch_restores_project(hub, project, tmp_path):
    config = load_config(project)
    build_all(config)
    push(config)
    restored = tmp_path / "restored"
    fetch(Hub(hub["url"], TOKEN), "study", restored)
    run = "outputs/demo/2026-01-01_00-00-00"
    assert (restored / run / "metrics.jsonl").read_bytes() == (project / run / "metrics.jsonl").read_bytes()
    assert (restored / "outputs/demo/latest").is_symlink()
    assert (restored / "generated/reports/overview/index.html").is_file()
    assert fetch(Hub(hub["url"], TOKEN), "study", restored) == 0  # nothing changed


def test_push_wraps_a_command(hub, project):
    script = "import pathlib; pathlib.Path('outputs/late.txt').write_text('done'); raise SystemExit(3)"
    import os

    previous = os.getcwd()
    os.chdir(project)
    try:
        code = push(load_config(project), interval=1, command=[sys.executable, "-c", script])
    finally:
        os.chdir(previous)
    assert code == 3
    assert "outputs/late.txt" in tree(hub)
