"""A running version mirrors its folder: a file that leaves the folder (deleted, or excluded) leaves the version at the
next live sync and always when the run ends, and a file caught mid-write ends with its final contents. Against the
repo, a store server online, and a store server that goes down mid-run (then `cairn sync`); whole processes."""

import hashlib
import os
import subprocess
import sys
import time

import pytest

import cairn
from cairn.store.blobs import clean_entries, merged_files
from cairn.store.errors import StoreError

from cairn.store.digest import manifest_digest
from test_identity import Server
from test_store import env, local, put  # noqa: F401
from conftest import write_config

SHA = "a" * 64


# ------------------------------------------------------------------ the entries


def test_merged_files_applies_removals_in_order():
    files = [{"path": "a", "sha256": SHA, "size": 1}, {"path": "b", "sha256": SHA, "size": 2}]
    entries = clean_entries(
        [
            {"path": "a", "removed": True},
            {"path": "missing", "removed": True},  # not there: no change
            {"path": "c", "sha256": SHA, "size": 3},
            {"path": "b", "removed": True},
            {"path": "b/inner", "sha256": SHA, "size": 4},  # a file that became a folder
        ],
        removals=True,
    )
    assert [entry["path"] for entry in merged_files(files, entries)] == ["b/inner", "c"]
    assert merged_files(files, clean_entries([{"path": "x", "removed": True}], removals=True)) == files


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "a", "removed": False},
        {"path": "a", "removed": "yes"},
        {"path": "a", "removed": 1},
        {"path": "a", "removed": True, "sha256": SHA, "size": 1},
        {"path": "a", "removed": True, "link": "b"},
        {"path": "../a", "removed": True},
        {"path": "", "removed": True},
    ],
)
def test_invalid_removals_are_refused(entry):
    with pytest.raises(ValueError):
        clean_entries([entry], removals=True)


def test_a_publish_takes_no_removals(local):
    with pytest.raises(ValueError, match="running version"):
        clean_entries([{"path": "a", "removed": True}])
    with pytest.raises(ValueError):
        local.publish("p", "x", [{"path": "a", "removed": True}])


def test_the_repo_drops_removed_files_of_a_running_version(local):
    number = local.start_version("p", "x")
    local.add_files("p", "x", number, put(local, {"a": "one", "b": "two", "c/d": "three"}))
    local.add_files("p", "x", number, [{"path": "a", "removed": True}, {"path": "zz", "removed": True}])
    assert _paths(local.get_version("p", "x", number)) == {"b", "c/d"}
    for bad in ({"path": "b", "removed": "yes"}, {"path": "b", "removed": True, "size": 3}):
        with pytest.raises(ValueError):
            local.add_files("p", "x", number, [bad])
    local.add_files("p", "x", number, [{"path": "c/d", "removed": True}, *put(local, {"c": "a file now"})])
    local.complete_version("p", "x", number)
    version = local.get_version("p", "x", number)
    assert _paths(version) == {"b", "c"} and version["digest"] == manifest_digest(version["files"])
    with pytest.raises(StoreError):
        local.add_files("p", "x", number, [{"path": "b", "removed": True}])


# ------------------------------------------------------------------ whole processes

JOB = """\
import os, shutil, sys, time
from pathlib import Path
import cairn

run = cairn.init(name="sweep", system=False, code=False, capture=False, watch=True)
here = Path(run.path)
Path("ref.txt").write_text(run.id)
(here / "light_sweep.mp4").write_bytes(os.urandom(3 << 20))
(here / "light_sweep.webp").write_bytes(b"")             # caught mid-write, then deleted
(here / "growing.txt").write_text("a" * 10)              # caught mid-write, then finished
(here / "scratch.log").write_text("excluded later")
(here / "notes.txt").write_text("a skipped name later")
Path("phase1").write_text("")
while not Path("go").exists():
    time.sleep(0.05)
with cairn.draft("results/sweep") as result:
    result.add_file(here / "light_sweep.mp4")
(here / "light_sweep.mp4").unlink()
(here / "light_sweep.webp").unlink()
(here / "scratch.log").rename(here / "scratch.skip")      # excluded now
(here / "notes.txt").rename(here / "notes.tmp")          # a name never published
with open(here / "growing.txt", "a") as out:
    out.write("b" * 5000)
"""

GONE = {"light_sweep.mp4", "light_sweep.webp", "scratch.log", "scratch.skip", "notes.txt", "notes.tmp"}


def _wait(predicate, what, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _paths(manifest):
    return {entry["path"] for entry in manifest["files"]}


@pytest.fixture(params=["repo", "remote", "offline"])
def mode(request, env, tmp_path, monkeypatch):
    if request.param == "repo":
        write_config(env, '[repo]\ninterval = 0.1\nexclude = ["*.skip"]\n')
        yield request.param, None
        return
    server = Server(tmp_path / "store-root").start()
    monkeypatch.setenv("CAIRN_TOKEN", server.token)
    write_config(env, f'[remote]\nurl = "{server.url}/study"\n[repo]\ninterval = 0.1\nexclude = ["*.skip"]\n')
    yield request.param, server
    server.stop()


def test_a_run_ends_with_the_files_its_folder_ends_with(mode, env):
    kind, server = mode
    (env / "job.py").write_text(JOB)
    environ = {key: value for key, value in os.environ.items() if not key.startswith("SLURM_")}
    process = subprocess.Popen(
        [sys.executable, "job.py"], cwd=env, env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        _wait(lambda: (env / "phase1").exists() or process.poll() is not None, "the job to write its files")
        assert process.poll() is None, process.communicate()
        run_id = (env / "ref.txt").read_text()
        early = {"light_sweep.mp4", "light_sweep.webp", "growing.txt", "scratch.log", "notes.txt"}
        if server is None:
            repo = cairn.connect().backend.local
            look = lambda: repo.get_version("runs/sweep", run_id)  # noqa: E731
        else:
            remote = server.remote()
            look = lambda: remote.get_version("runs/sweep", run_id)  # noqa: E731

        def synced():
            try:
                return early <= _paths(look())
            except StoreError:
                return False

        _wait(synced, "a live sync of the run's files")
        sizes = {entry["path"]: entry["size"] for entry in look()["files"]}
        assert sizes["light_sweep.webp"] == 0 and sizes["growing.txt"] == 10
        if kind == "offline":
            server.stop()
        (env / "go").write_text("")
        out, err = process.communicate(timeout=120)
        assert process.returncode == 0, err
    finally:
        if process.poll() is None:
            process.kill()
    if kind == "offline":
        server.start()
        done = subprocess.run([sys.executable, "-m", "cairn", "sync"], cwd=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    elif kind == "remote":
        cairn.connect().sync()
    store = cairn.connect()
    manifest = look() if server is not None else None
    run = store.version(run_id)
    assert run.status == "completed" and run.number == 1
    files = {entry.path: entry for entry in run.files}
    assert not GONE & set(files), sorted(files)
    final = ("a" * 10 + "b" * 5000).encode()
    assert files["growing.txt"].size == len(final)
    assert files["growing.txt"].sha256 == hashlib.sha256(final).hexdigest()
    if manifest is not None:  # what the remote itself holds
        assert manifest["status"] == "completed" and not GONE & _paths(manifest)
        sizes = {entry["path"]: entry["size"] for entry in manifest["files"]}
        assert sizes["growing.txt"] == len(final)
    result = store.artifact("results/sweep").latest
    [video] = [entry for entry in result.files if entry.path.endswith("light_sweep.mp4")]
    assert video.size == 3 << 20 and result.inputs == [run.id]
