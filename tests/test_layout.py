"""Where Cairn keeps things: the repo (`.cairn`), the one folder of a project, and its layout."""

import json
from pathlib import Path

import pytest

import cairn
from cairn.client.open import open_repo, repo_root
from cairn.config import cairn_base, load_config
from cairn.runs.snapshot import _denied
from cairn.store import StoreError


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_DIR", raising=False)
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "pyproject.toml").write_text("")
    monkeypatch.chdir(folder)
    return folder.resolve()


# ------------------------------------------------------------------ resolution


def test_project_default_puts_everything_in_dot_cairn(proj):
    base = proj / ".cairn"
    assert cairn_base() == base and repo_root() == base
    config = load_config()
    assert (config.cairn_dir, config.output_dir) == (base, base / "build")


def test_the_repo_layout(proj, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    (data / "a.txt").write_text("1")
    version = cairn.connect().artifact("data").publish(data)
    folder = cairn.pull(f"data@v{version.number}")
    base = proj / ".cairn"
    assert folder == base / "versions" / version.uid / "files"
    names = {path.name for path in base.iterdir()}
    assert {"repo.json", "metadata.sqlite3", "blobs", "tmp", "versions", "drafts", ".gitignore"} <= names
    assert not names & {"store", "pending", "cache", "store.json", "projects"}  # the 0.13 layout is gone
    record = json.loads((base / "repo.json").read_text())
    assert record["layout"] == 1 and record["uid"]
    sha = version.files[0].sha256
    assert (base / "blobs" / sha[:2] / sha).is_file()  # blobs/<aa>/<sha256>
    assert (base.stat().st_mode & 0o777) == 0o700  # private to its owner


def test_base_resolution_order(proj, tmp_path, monkeypatch):
    (proj / "cairn.toml").write_text('[cairn]\ndir = "work/cairn"\n')
    assert cairn_base() == proj / "work" / "cairn"
    assert load_config().cairn_dir == proj / "work" / "cairn"
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / "elsewhere"))  # the environment beats cairn.toml
    assert cairn_base() == tmp_path / "elsewhere"
    assert load_config().output_dir == tmp_path / "elsewhere" / "build"
    (proj / "cairn.toml").write_text('output_dir = "site"\n')
    assert load_config().output_dir == proj / "site"


def test_outside_a_project_there_is_no_repo_unless_cairn_dir_names_one(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # no project marker above it
    assert cairn_base() is None
    with pytest.raises(StoreError, match="no repo"):
        repo_root()
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / "chosen"))  # CAIRN_DIR works anywhere
    assert repo_root() == tmp_path / "chosen"


def test_store_config_is_an_unknown_key(proj):
    (proj / "cairn.toml").write_text('[store]\nurl = "https://x.example.org"\n')
    with pytest.raises(ValueError, match=r"unknown keys \['store'\]"):
        load_config()


def test_old_config_keys_are_errors(proj):
    (proj / "cairn.toml").write_text('outputs_dir = "runs"\n')
    with pytest.raises(ValueError, match="unknown keys.*outputs_dir"):
        load_config()
    (proj / "cairn.toml").write_text('[hub]\nremote = "x"\n')
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


# ------------------------------------------------------------------ .gitignore


def test_gitignore_is_written_once_with_a_star(proj):
    repo_root()
    marker = proj / ".cairn" / ".gitignore"
    assert marker.read_text() == "*\n"
    marker.write_text("custom\n")
    repo_root()
    open_repo()
    assert marker.read_text() == "custom\n"  # never rewritten
    assert not (proj / ".gitignore").exists()  # the project's own file is never touched


def test_every_creator_writes_the_gitignore(proj):
    open_repo(load_config())
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
