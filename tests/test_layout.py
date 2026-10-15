"""Where Cairn keeps things: the base folder (`.cairn`) and the roots inside it."""

from pathlib import Path

import pytest

from cairn.client.open import open_store
from cairn.config import cairn_base, load_config
from cairn.runs.snapshot import _denied
from cairn.store import cache
from cairn.store.cache import cache_root, data_root


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_DATA_DIR")
    monkeypatch.delenv("CAIRN_DIR", raising=False)
    monkeypatch.delenv("CAIRN_CACHE_DIR", raising=False)
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "pyproject.toml").write_text("")
    monkeypatch.chdir(folder)
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


def test_old_config_keys_are_errors(proj):
    (proj / "cairn.toml").write_text('outputs_dir = "runs"\n')
    with pytest.raises(ValueError, match="unknown keys.*outputs_dir"):
        load_config()
    (proj / "cairn.toml").write_text('[hub]\nurl = "x"\n')
    with pytest.raises(ValueError, match="unknown keys.*hub"):
        load_config()


def test_cairn_report_toml_is_not_read(proj):
    (proj / "cairn-report.toml").write_text('reports_dir = "docs"\n')
    with pytest.raises(ValueError, match="rename it to cairn.toml"):
        load_config()
    (proj / "cairn.toml").write_text("")  # both: cairn.toml wins, the old file is just ignored
    assert load_config().reports_dir == proj / "reports"


def test_a_folder_with_only_the_old_config_is_still_a_project(tmp_path, monkeypatch):
    (tmp_path / "cairn-report.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="rename it to cairn.toml"):
        load_config()


def test_the_removed_state_dir_variable_does_nothing(proj, tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_STATE_DIR", str(tmp_path / "state-dir"))
    assert data_root() == proj / ".cairn" / "pending"


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
    for part in (".cairn", "generated"):
        assert _denied(Path(part) / "x.py")
    assert _denied(Path(".cairn") / ".gitignore")
