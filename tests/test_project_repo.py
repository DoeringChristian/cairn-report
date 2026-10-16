"""`cairn server` and `cairn ui` with no arguments use the project's repo (.cairn), creating it if needed, and print a
one-time login link. Processes on the machine always open the repo directly, so starting or stopping a server during
a run changes nothing. Real processes, as a user runs them."""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest
from conftest import read_until, write_config

pytest.importorskip("uvicorn")

SCRIPT = """
import cairn
with cairn.draft("res") as result:
    result.add_json("x", {"v": 1})
print(result.version.ref)
"""

# a run that starts, waits until the file `go` exists, then publishes its result
SLOW = """
import pathlib, time, cairn
run = cairn.init(name="slow")
run.add({"loss": 1.0, "step": 0})
while not pathlib.Path("go").exists():
    time.sleep(0.05)
with cairn.draft("slow-result") as result:
    result.add_json("x", {"v": 1})
print(result.version.ref)
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    for name in ("CAIRN_DIR", "CAIRN_INPUTS", "CAIRN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "demo"  # the UI shows the repo's one project under the folder's name
    folder.mkdir()
    write_config(folder, "")
    return folder


def start(project, *args):
    """Start `cairn <args>` in the project; returns the process and its lines up to the login link."""
    process = subprocess.Popen([sys.executable, "-m", "cairn", *args], cwd=project, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    lines = read_until(process, lambda line: line.startswith("  Log in "))
    assert lines and lines[-1].startswith("  Log in "), lines
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
    return re.search(r"Log in +(\S+)", lines[-1]).group(1)


def test_a_server_started_and_stopped_while_a_run_runs_changes_nothing_for_it(project):
    slow = subprocess.Popen([sys.executable, "-c", SLOW], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    store = project / ".cairn"
    for _ in range(400):  # the run is started (the repo exists) before any server
        if (store / "metadata.sqlite3").exists():
            break
        time.sleep(0.05)
    server, lines = start(project, "server", "--port", "0")
    try:
        assert any("(created, with a new owner token)" in line for line in lines)  # an existing repo gets its owner token
        (project / "go").touch()  # the run finishes while the server serves the repo
        out, err = slow.communicate(timeout=120)
        assert slow.returncode == 0, err
        assert out.strip() == "slow-result@v1"

        result = run(project, sys.executable, "-c", SCRIPT)  # a script beside the server: the repo directly
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "res@v1"
        listed = run(project, sys.executable, "-m", "cairn", "artifacts")
        assert re.search(r"\nres +v1 ", listed.stdout) and re.search(r"\nruns/slow +v1 ", listed.stdout), listed

        link = link_of(lines)
        base = link.split("login?")[0]
        browser = opener()
        with browser.open(link) as answer:  # logs in, then the projects page
            assert answer.url == base
        assert projects(browser, base) == ["demo"]  # the server's UI sees the direct writes
        with opener().open(link) as again:  # the link works once
            assert again.url.endswith("/login")

        port = re.search(r"API +http://127.0.0.1:(\d+)/", "\n".join(lines)).group(1)
        second = subprocess.Popen([sys.executable, "-m", "cairn", "server", str(project / "other-repo"), "--port", port,
                                   "--no-ui"], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        shown = read_until(second, lambda line: line.startswith("Serving;"))
        try:  # a taken port moves the second server to the next free one, and says so before its address
            text = "\n".join(shown)
            assert f"port {port} in use; using " in text, shown
            moved = re.search(r"API +http://127.0.0.1:(\d+)/", text).group(1)
            assert int(moved) > int(port) and f"cairn remote set http://127.0.0.1:{moved}/<project>" in text
        finally:
            stop(second)
    finally:
        stop(server)
    result = run(project, sys.executable, "-c", SCRIPT)
    assert result.returncode == 0 and result.stdout.strip() == "res@v2", result.stderr


def test_cairn_ui_creates_the_project_repo_and_logs_in_with_a_link(project):
    ui, lines = start(project, "ui", "--no-browser", "--port", "0")
    try:
        assert (project / ".cairn" / "repo.json").is_file()
        assert not (project / ".cairn" / "token").exists()  # never on disk: a client would send it to the remote
        assert (project / ".cairn" / ".gitignore").read_text().strip() == "*"
        link = link_of(lines)
        browser = opener()
        browser.open(link)
        assert projects(browser, link.split("login?")[0]) == ["demo"]
        with pytest.raises(urllib.error.HTTPError):  # without the session: 401 on the API
            projects(opener(), link.split("login?")[0])
    finally:
        stop(ui)


def test_cairn_server_refuses_a_project_with_a_remote(project):
    write_config(project, '[remote]\nurl = "https://cairn.example.org/demo"\n[repo]\n')
    result = run(project, sys.executable, "-m", "cairn", "server")
    assert result.returncode == 1 and "this project's repo syncs with the remote https://cairn.example.org/demo" in result.stderr
