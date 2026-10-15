"""Where Cairn keeps things: the base folder (`.cairn`), the roots inside it, and the migrations from earlier layouts."""

import json
from pathlib import Path

import pytest

from cairn import config as config_module
from cairn.client import replica
from cairn.client.open import open_store
from cairn.config import cairn_base, load_config
from cairn.runs.snapshot import _denied
from cairn.store import cache
from cairn.store.cache import cache_root, data_roots, data_root


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_DATA_DIR")
    monkeypatch.delenv("CAIRN_DIR", raising=False)
    monkeypatch.delenv("CAIRN_CACHE_DIR", raising=False)
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "pyproject.toml").write_text("")
    monkeypatch.chdir(folder)
    monkeypatch.setattr(config_module, "_NOTED", set())
    monkeypatch.setattr(replica, "_checked", set())
    return folder.resolve()


# ------------------------------------------------------------------ resolution


def test_project_default_puts_everything_in_dot_cairn(proj):
    base = proj / ".cairn"
    assert cairn_base() == base
    assert (data_root(), cache_root()) == (base / "pending", base / "cache")
    config = load_config()
    assert (config.store_path, config.output_dir) == (base / "store", base / "build")


def test_base_resolution_order(proj, tmp_path, monkeypatch):
    (proj / "cairn.toml").write_text('[cairn]\ndir = "work/cairn"\n')
    assert cairn_base() == proj / "work" / "cairn"
    assert data_root() == proj / "work" / "cairn" / "pending"
    assert load_config().store_path == proj / "work" / "cairn" / "store"
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / "elsewhere"))  # the environment beats cairn.toml
    assert cairn_base() == tmp_path / "elsewhere"
    assert (data_root(), cache_root()) == (tmp_path / "elsewhere" / "pending", tmp_path / "elsewhere" / "cache")
    assert load_config().output_dir == tmp_path / "elsewhere" / "build"


def test_each_root_has_its_own_overrides(proj, tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAIRN_CACHE_DIR", str(tmp_path / "scratch"))
    assert (data_root(), cache_root()) == (tmp_path / "data", tmp_path / "scratch")
    monkeypatch.delenv("CAIRN_DATA_DIR")
    monkeypatch.setenv("CAIRN_STATE_DIR", str(tmp_path / "state-dir"))
    monkeypatch.setattr(cache, "_warned", set())
    assert data_root() == tmp_path / "state-dir"
    cache.use_data_dir(tmp_path / "flag")  # --data-dir beats the environment
    assert data_root() == tmp_path / "flag"
    (proj / "cairn.toml").write_text('output_dir = "site"\n[store]\npath = "mine"\n')
    config = load_config()
    assert (config.output_dir, config.store_path) == (proj / "site", proj / "mine")


def test_outside_a_project_the_xdg_folders_are_used(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_DATA_DIR")
    monkeypatch.delenv("CAIRN_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # no project marker above it
    assert cairn_base() is None
    assert data_root() == tmp_path / "xdg-data" / "cairn"
    assert cache_root() == tmp_path / "cache" / "cairn"
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / "chosen"))  # CAIRN_DIR works anywhere
    assert data_root() == tmp_path / "chosen" / "pending"


def test_outputs_dir_is_ignored_with_one_warning(proj, capsys):
    (proj / "cairn.toml").write_text('outputs_dir = "runs"\n')
    load_config()
    load_config()
    err = capsys.readouterr().err
    assert err.count("outputs_dir is no longer used") == 1


# ------------------------------------------------------------------ .gitignore


def test_gitignore_is_written_once_with_a_star(proj):
    cache_root()
    marker = proj / ".cairn" / ".gitignore"
    assert marker.read_text() == "*\n"
    marker.write_text("custom\n")
    cache_root()
    data_root()
    assert marker.read_text() == "custom\n"  # never rewritten
    assert not (proj / ".gitignore").exists()  # the project's own file is never touched


def test_every_creator_writes_the_gitignore(proj):
    open_store(load_config())
    assert (proj / ".cairn" / ".gitignore").read_text() == "*\n"


# ------------------------------------------------------------------ migrations


def test_old_store_moves_into_the_base_once(proj, capsys):
    (proj / ".store").mkdir()
    (proj / ".store" / "marker").write_text("x")
    open_store(load_config())
    assert (proj / ".cairn" / "store" / "marker").read_text() == "x"
    assert not (proj / ".store").exists()
    assert "moved the local store" in capsys.readouterr().err
    open_store(load_config())  # idempotent: nothing more to move, nothing printed
    assert capsys.readouterr().err == ""


def test_old_store_is_never_overwritten(proj, capsys):
    (proj / ".store").mkdir()
    (proj / ".store" / "marker").write_text("old")
    (proj / ".cairn" / "store").mkdir(parents=True)
    (proj / ".cairn" / "store" / "marker").write_text("new")
    open_store(load_config())
    assert (proj / ".cairn" / "store" / "marker").read_text() == "new"
    assert (proj / ".store" / "marker").read_text() == "old"
    assert "exists already" in capsys.readouterr().err


def test_an_explicit_store_path_is_left_alone(proj):
    (proj / "cairn.toml").write_text('[store]\npath = ".store"\n')
    (proj / ".store").mkdir()
    open_store(load_config())
    assert (proj / ".store").is_dir() and not (proj / ".cairn" / "store").exists()


def pending_record(root: Path, uid: str, folder: Path, sync: str = "pending") -> None:
    target = root / "versions" / uid
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(json.dumps({
        "uid": uid, "sync": sync, "status": "completed", "project": "p", "name": "n", "url": "http://x",
        "source": {"host": "h", "folder": str(folder)}, "files": [], "created": "2026-01-01T00:00:00+00:00"}))


def test_global_pending_moves_by_source_folder(proj, tmp_path, capsys):
    old = tmp_path / "xdg-data" / "cairn"
    pending_record(old, "uid-in", proj / "runs" / "a")
    pending_record(old, "uid-out", tmp_path / "other-project" / "runs")
    pending_record(old, "uid-running", proj / "runs" / "b", sync="running")
    replica.ensure_layout()
    new = proj / ".cairn" / "pending" / "versions"
    assert (new / "uid-in" / "manifest.json").is_file() and not (old / "versions" / "uid-in").exists()
    assert (old / "versions" / "uid-out").is_dir() and (old / "versions" / "uid-running").is_dir()
    assert "moved 1 pending versions" in capsys.readouterr().err
    replica._checked.clear()
    replica.ensure_layout()  # idempotent
    assert capsys.readouterr().err == ""
    assert [record["uid"] for record in replica.records()] == ["uid-in"]
    assert [record["uid"] for record in replica.records(all_roots=True)] == ["uid-in", "uid-out", "uid-running"]
    assert data_roots() == [proj / ".cairn" / "pending", old]


def test_global_pending_never_overwrites(proj, tmp_path):
    old = tmp_path / "xdg-data" / "cairn"
    pending_record(old, "uid-1", proj)
    pending_record(proj / ".cairn" / "pending", "uid-1", proj, sync="rejected")
    replica.ensure_layout()
    assert (old / "versions" / "uid-1").is_dir()
    assert replica.load("uid-1")["sync"] == "rejected"


def test_nothing_moves_when_the_data_root_is_chosen(proj, tmp_path, monkeypatch):
    old = tmp_path / "xdg-data" / "cairn"
    pending_record(old, "uid-1", proj)
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "data"))
    replica.ensure_layout()
    assert (old / "versions" / "uid-1").is_dir()
    assert data_roots() == [tmp_path / "data"]


# ------------------------------------------------------------------ build output and snapshots


def test_reports_build_into_the_base(proj):
    (proj / "reports" / "a").mkdir(parents=True)
    (proj / "reports" / "a" / "report.md").write_text("---\ntitle: A\n---\n\n## One\n\ntext\n")
    from cairn.reports import build

    build.configure(load_config())
    build.build_all()
    assert (proj / ".cairn" / "build" / "a" / "index.html").is_file()
    assert (proj / ".cairn" / ".gitignore").read_text() == "*\n"
    assert not (proj / "generated").exists()


def test_snapshots_skip_the_cairn_folder():
    for part in (".cairn", ".store", "generated"):
        assert _denied(Path(part) / "x.py")
    assert _denied(Path(".cairn") / ".gitignore")
