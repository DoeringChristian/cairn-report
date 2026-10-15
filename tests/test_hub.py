import http.client
import json
import os
import re
import shutil
import sys
import time
from dataclasses import replace

import pytest

from conftest import RUN, TOKEN
from cairn_report.build import build_all
from cairn_report.config import load_config
from cairn_report.logger import RunLogger
from cairn_report.push import Hub, RunPusher, pull, push


@pytest.fixture
def client(project, hub, monkeypatch, tmp_path):
    (project / "cairn-report.toml").write_text(f'[hub]\nurl = "{hub["url"]}"\nproject = "study"\ninterval = 1\n')
    monkeypatch.setenv("CAIRN_HUB_TOKEN", TOKEN)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return project


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


def runs(hub, allow_missing=False):
    status, _, data = call(hub, "GET", "/api/v1/projects/study/runs")
    if allow_missing and status == 404:
        return {}
    assert status == 200
    return {entry["path"]: entry for entry in json.loads(data)}


def test_auth_required(hub, client):
    push(load_config(client))
    assert call(hub, "GET", "/api/v1/projects", token=None)[0] == 401
    assert call(hub, "GET", "/api/v1/projects", token="x" * 40)[0] == 401
    status, headers, _ = call(hub, "GET", "/p/study/", token=None)
    assert status == 303 and headers["Location"].startswith("/login")
    assert call(hub, "PUT", "/api/v1/blobs/" + "0" * 64, body=b"x", token=None)[0] == 401


def test_login_cookie(hub, client):
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    status, headers, _ = call(hub, "POST", "/login", body=f"token={'w' * 40}&next=/", token=None, headers=form)
    assert status == 401 and "Set-Cookie" not in headers
    status, headers, _ = call(hub, "POST", "/login", body=f"token={TOKEN}&next=//evil.example", token=None, headers=form)
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"] and TOKEN not in cookie
    assert call(hub, "GET", "/", token=None, headers={"Cookie": cookie})[0] == 200


def test_report_versions(hub, client):
    config = load_config(client)
    build_all(config)
    assert push(config, what="report") == 0
    assert push(config, what="report") == 0  # unchanged: still version 1
    versions = json.loads(call(hub, "GET", "/api/v1/projects/study/report/versions")[2])
    assert [v["version"] for v in versions] == [1]
    root = client / "reports" / "report.md"
    root.write_text(root.read_text().replace("See [the result]", "Start here. See [the result]"))
    build_all(config)
    push(config, what="report")
    versions = json.loads(call(hub, "GET", "/api/v1/projects/study/report/versions")[2])
    assert [v["version"] for v in versions] == [1, 2]
    assert b"Start here" in call(hub, "GET", "/p/study/narrative.html")[2]
    assert b"Start here" not in call(hub, "GET", "/p/study@1/narrative.html")[2]  # old versions stay viewable


def test_report_served_with_caching(hub, client):
    config = load_config(client)
    build_all(config)
    push(config, what="report")
    page = call(hub, "GET", "/p/study/alpha/beta/")[2].decode()
    script = re.search(r'src="\.\./\.\./(_static/report\.[0-9a-f]+\.js)"', page).group(1)
    status, headers, _ = call(hub, "GET", f"/p/study/{script}", headers={"Accept-Encoding": "gzip"})
    assert status == 200 and headers["Content-Encoding"] == "gzip" and "immutable" in headers["Cache-Control"]
    versioned = re.search(r'data-narrative="([^"]+)"', page).group(1)
    assert "immutable" in call(hub, "GET", f"/p/study/alpha/beta/{versioned}")[1]["Cache-Control"]
    assert call(hub, "GET", "/p/study/alpha")[0] == 301
    assert json.loads(call(hub, "GET", "/p/study/site.json")[2])["title"] == "Root"


def test_runs_are_incremental_with_history(hub, client):
    config = load_config(client)
    push(config, what="runs")
    files = runs(hub)
    assert files["demo/latest"]["link"] == "2026-01-01_00-00-00"
    assert f"{RUN}/.hydra/config.yaml" in files and f"{RUN}/figures/hist.png" in files
    pusher = RunPusher(Hub(hub["url"], TOKEN), config, "study")
    assert pusher.once() == (0, 0)
    RunLogger(client / "outputs" / RUN).scalar("rmse", 0.25)
    assert pusher.once()[0] == 1
    lines = (hub["root"] / "projects" / "study" / "runs.jsonl").read_text().splitlines()
    assert sum(f'"{RUN}/metrics.jsonl"' in line for line in lines) == 2  # the old version is still recorded
    status, _, data = call(hub, "GET", "/files/study/runs/demo/latest/metrics.jsonl")
    assert status == 200 and data.count(b"\n") == 2
    assert b"completed" in call(hub, "GET", "/runs/study/")[2]


def test_rejects_bad_uploads_and_paths(hub, client):
    status, _, _ = call(hub, "PUT", "/api/v1/blobs/" + "a" * 64, body=b"not matching", headers={"Content-Length": "12"})
    assert status == 400 and not list((hub["root"] / "blobs").rglob("*"))
    json_headers = {"Content-Type": "application/json"}
    for kind, entry in (("runs", {"path": "a/../../etc/passwd", "sha256": "a" * 64, "size": 1}),
                        ("runs", {"path": "/abs", "sha256": "a" * 64, "size": 1}),
                        ("runs", {"path": "a/latest", "link": "../../etc"}),
                        ("report", {"path": "elsewhere/x", "sha256": "a" * 64, "size": 1}),
                        ("report", {"path": "site/x", "link": "../source/y"})):
        status, _, _ = call(hub, "POST", f"/api/v1/projects/study/{kind}", body=json.dumps({"entries": [entry]}), headers=json_headers)
        assert status == 400, entry
    status, _, data = call(hub, "POST", "/api/v1/projects/study/runs",
                           body=json.dumps({"entries": [{"path": "a/x", "sha256": "b" * 64, "size": 1}]}), headers=json_headers)
    assert status == 409 and json.loads(data)["missing"] == ["b" * 64]


def test_exclude_patterns(hub, client):
    submitit = client / "outputs" / RUN / ".submitit" / "1_0"
    submitit.mkdir(parents=True)
    (submitit / "1_0_submitted.pkl").write_bytes(b"pickle")
    (submitit / "1_0_log.out").write_text("log")
    config_file = client / "cairn-report.toml"
    config_file.write_text(config_file.read_text() + 'exclude = [".submitit/**/*.pkl"]\n')
    push(load_config(client), what="runs")
    files = runs(hub)
    assert f"{RUN}/.submitit/1_0/1_0_log.out" in files and not any(path.endswith(".pkl") for path in files)


def test_token_never_in_config(tmp_path):
    (tmp_path / "cairn-report.toml").write_text('[hub]\ntoken = "secret"\n')
    with pytest.raises(ValueError, match="never put the hub token"):
        load_config(tmp_path)


def test_pull_restores_and_protects_local_changes(hub, client, tmp_path):
    config = load_config(client)
    build_all(config)
    push(config)
    root = tmp_path / "restored"
    restored = replace(config, project=root, reports_dir=root / "reports", output_dir=root / "generated" / "reports",
                       outputs_dir=root / "outputs")
    pull(Hub(hub["url"], TOKEN), "study", restored, runs=[])
    assert (root / "reports" / "alpha" / "report.md").read_text() == (client / "reports" / "alpha" / "report.md").read_text()
    assert (root / "generated" / "reports" / "alpha" / "beta" / "index.html").is_file()
    assert (root / "outputs" / RUN / "metrics.jsonl").read_bytes() == (client / "outputs" / RUN / "metrics.jsonl").read_bytes()
    assert (root / "outputs" / "demo" / "latest").is_symlink()
    (root / "reports" / "report.md").write_text("local edit\n")
    with pytest.raises(SystemExit, match="would be replaced"):
        pull(Hub(hub["url"], TOKEN), "study", restored)
    pull(Hub(hub["url"], TOKEN), "study", restored, force=True)
    assert (root / "reports" / "report.md").read_text() != "local edit\n"


def test_push_wraps_a_command(hub, client):
    script = "import pathlib; pathlib.Path('outputs/late.txt').write_text('done'); raise SystemExit(3)"
    previous = os.getcwd()
    os.chdir(client)
    try:
        code = push(load_config(client), what="runs", interval=1, command=[sys.executable, "-c", script])
    finally:
        os.chdir(previous)
    assert code == 3 and "late.txt" in runs(hub)


def test_push_arguments_parse_in_any_order(monkeypatch):
    import cairn_report.cli as cli
    import cairn_report.push as push_module

    calls = []
    monkeypatch.setattr(push_module, "push", lambda config, **kw: calls.append(kw) or 0)
    for argv, expected in ((["push", "runs", "--watch", "--", "bash", "-c", "x"], ("runs", True, ["bash", "-c", "x"])),
                           (["push", "--watch", "--", "echo"], ("all", True, ["echo"])),
                           (["push", "report"], ("report", False, None))):
        with pytest.raises(SystemExit):
            cli.main(argv)
        assert (calls[-1]["what"], calls[-1]["watch"], calls[-1]["command"]) == expected


def test_files_answer_range_requests(hub, client):
    config = load_config(client)
    build_all(config)
    push(config, what="report")
    path = "/p/study/site.json"
    whole = call(hub, "GET", path)[2]
    status, headers, data = call(hub, "GET", path, headers={"Range": "bytes=5-14"})
    assert status == 206 and data == whole[5:15] and headers["Content-Range"] == f"bytes 5-14/{len(whole)}"
    assert call(hub, "GET", path, headers={"Range": f"bytes={len(whole)}-"})[0] == 416


def test_serve_creates_a_new_hub_and_prints_its_token(tmp_path, monkeypatch, capsys):
    from cairn_report import hub as hub_module

    monkeypatch.delenv("CAIRN_HUB_TOKEN", raising=False)
    monkeypatch.setattr(hub_module.ThreadingHTTPServer, "serve_forever", lambda self: self.server_close())
    root = tmp_path / "new-hub"
    hub_module.serve_hub(root, port=0)
    token = (root / "token").read_text().strip()
    out = capsys.readouterr().out
    assert f"Token: {token}" in out and "Created hub" in out
    assert oct((root / "token").stat().st_mode & 0o777) == "0o600"
    hub_module.serve_hub(root, port=0)  # a second launch reuses it and prints the same token
    out = capsys.readouterr().out
    assert f"Token: {token}" in out and "Created hub" not in out


def test_push_refuses_a_partial_build(hub, client, capsys):
    config = load_config(client)
    build_all(config)
    shutil.rmtree(client / "generated" / "reports" / "alpha" / "beta")  # as after a build that failed midway
    assert push(config, what="report") == 1
    assert "no built pages: alpha/beta" in capsys.readouterr().err
    assert call(hub, "GET", "/api/v1/projects/study/report")[0] == 404  # no version was created
