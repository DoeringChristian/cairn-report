"""`cairn server` and `cairn ui` with no arguments use the project's own store folder (.cairn/store), creating it
if needed; while a server serves that folder, the project's scripts write through it; both print a one-time
login link. Real processes, as a user runs them."""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

pytest.importorskip("uvicorn")

SCRIPT = """
import cairn
with cairn.draft("res") as result:
    result.add_json("x", {"v": 1})
print(result.version.ref)
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    for name in ("CAIRN_DIR", "CAIRN_DATA_DIR", "CAIRN_CACHE_DIR", "CAIRN_INPUTS", "CAIRN_STORE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "cairn.toml").write_text('[store]\nproject = "demo"\n')
    return tmp_path


def start(project, *args):
    """Start `cairn <args>` in the project; returns the process and its lines up to the login link."""
    process = subprocess.Popen([sys.executable, "-m", "cairn", *args], cwd=project, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    lines = []
    for line in process.stdout:
        lines.append(line.rstrip())
        if line.startswith("Log in:") or process.poll() is not None:
            break
    assert lines and lines[-1].startswith("Log in:"), lines
    return process, lines


def stop(process):
    process.terminate()
    process.wait(timeout=20)
    process.stdout.close()


def run(project, *command):
    return subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=120)


def opener():
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def projects(browser, base):
    request = urllib.request.Request(base + "ui-api/projects", headers={"X-Cairn": "1"})
    with browser.open(request) as answer:
        return [row["name"] for row in json.loads(answer.read())]


def link_of(lines):
    return re.search(r"Log in: (\S+)", lines[-1]).group(1)


def test_cairn_server_serves_the_project_store_and_scripts_write_through_it(project):
    store = project / ".cairn" / "store"
    server, lines = start(project, "server", "--port", "0")
    try:
        assert any(line.startswith("Created store") for line in lines)
        served = json.loads((store / "server.json").read_text())
        assert served["pid"] == server.pid and served["url"].startswith("http://127.0.0.1:")

        result = run(project, sys.executable, "-c", SCRIPT)  # no [store] url: through the server, not refused
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "demo:res@v1"
        listed = run(project, sys.executable, "-m", "cairn", "artifacts")
        assert listed.stdout.startswith("res\tv1"), listed

        link = link_of(lines)
        base = link.split("login?")[0]
        browser = opener()
        with browser.open(link) as answer:  # logs in, then the projects page
            assert answer.url == base
        assert projects(browser, base) == ["demo"]
        with opener().open(link) as again:  # the link works once
            assert again.url.endswith("/login")
    finally:
        stop(server)
    assert not (store / "server.json").exists()
    result = run(project, sys.executable, "-c", SCRIPT)  # no server: the folder directly again
    assert result.returncode == 0 and result.stdout.strip() == "demo:res@v2", result.stderr


def test_cairn_ui_creates_the_project_store_and_logs_in_with_a_link(project):
    ui, lines = start(project, "ui", "--no-browser", "--port", "0")
    try:
        assert any(line.startswith("Created store") for line in lines)
        assert (project / ".cairn" / "store" / "token").is_file()
        assert (project / ".cairn" / ".gitignore").read_text().strip() == "*"
        link = link_of(lines)
        browser = opener()
        browser.open(link)
        assert projects(browser, link.split("login?")[0]) == []
        with pytest.raises(urllib.error.HTTPError):  # without the session: 401 on the API
            projects(opener(), link.split("login?")[0])
    finally:
        stop(ui)


def test_cairn_server_refuses_a_project_with_a_store_url(project):
    (project / "cairn.toml").write_text('[store]\nurl = "https://store.example.org"\n')
    result = run(project, sys.executable, "-m", "cairn", "server")
    assert result.returncode == 1 and "this project's store is the server https://store.example.org" in result.stderr
