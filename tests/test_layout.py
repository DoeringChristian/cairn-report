"""Where Cairn keeps things: the repo (`.cairn`), the one folder of a project, and its layout."""

import json
from pathlib import Path

import pytest

import cairn
from cairn.client.open import open_repo, repo_root
from cairn.config import cairn_base, load_config
from cairn.runs.snapshot import _denied
from cairn.store import StoreError
from conftest import write_config


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_DIR", raising=False)
    folder = tmp_path / "proj"
    folder.mkdir()
    write_config(folder, "")
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
    write_config(proj, '[reports]\noutput_dir = "site"\n')
    assert load_config().output_dir == proj / "site"  # relative to the project root, the folder holding .cairn/
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / "elsewhere"))  # the environment beats .cairn, and its config is the config
    assert cairn_base() == tmp_path / "elsewhere"
    assert load_config().output_dir == tmp_path / "elsewhere" / "build"
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "config.toml").write_text('[repo]\ninterval = 3\n')
    assert load_config().repo == {"interval": 3} and load_config().project == proj


def test_the_cairn_dir_key_is_gone(proj):
    write_config(proj, '[cairn]\ndir = "work/cairn"\n')
    with pytest.raises(ValueError, match="unknown key 'cairn'"):
        load_config()


def test_cairn_dir_wins_over_everything(proj, tmp_path, monkeypatch):
    (proj / "sub" / ".cairn").mkdir(parents=True)
    (proj / "sub" / ".cairn" / "repo.json").write_text("{}")
    monkeypatch.chdir(proj / "sub")
    assert cairn_base() == proj / "sub" / ".cairn"  # an existing repo below the project root is found first
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / "chosen"))
    assert cairn_base() == repo_root() == load_config().cairn_dir == tmp_path / "chosen"
    monkeypatch.chdir(tmp_path)  # outside any project too
    assert repo_root() == tmp_path / "chosen" and not (tmp_path / ".cairn").exists()


def test_a_script_in_a_subfolder_finds_the_parents_repo(tmp_path):
    import subprocess
    import sys

    (tmp_path / "work" / "deep").mkdir(parents=True)
    assert subprocess.run([sys.executable, "-m", "cairn", "init"], cwd=tmp_path / "work", capture_output=True).returncode == 0
    script = "import cairn; run = cairn.init(name='x', system=False, code=False); print(run.path)"
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path / "work" / "deep", capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith(str((tmp_path / "work" / ".cairn" / "drafts").resolve()))
    assert not (tmp_path / "work" / "deep" / ".cairn").exists() and "created the repo" not in result.stderr


def test_outside_any_project_opening_creates_the_repo_here_and_looking_refuses(tmp_path):
    import subprocess
    import sys

    bare = tmp_path / "bare"
    bare.mkdir()
    status = subprocess.run([sys.executable, "-m", "cairn", "status"], cwd=bare, capture_output=True, text=True)
    assert status.returncode != 0 and "no repo here" in status.stderr and "cairn init" in status.stderr
    for command in ("verify", "prune", "sync"):
        assert subprocess.run([sys.executable, "-m", "cairn", command], cwd=bare, capture_output=True).returncode != 0
    assert not (bare / ".cairn").exists()  # nothing was created
    script = "import cairn; print(cairn.use.__name__); d = cairn.draft('x'); d.add_json('v', {'a': 1}); d.publish()"
    result = subprocess.run([sys.executable, "-c", script], cwd=bare, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (bare / ".cairn" / "repo.json").is_file()
    assert result.stderr.count("created the repo") == 1 and "created the repo .cairn " in result.stderr
    assert subprocess.run([sys.executable, "-m", "cairn", "status"], cwd=bare, capture_output=True).returncode == 0


def test_pyproject_and_pixi_files_do_not_mark_a_project(tmp_path):
    import subprocess
    import sys

    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "pixi.toml").write_text("")
    (tmp_path / "sub").mkdir()
    script = "import cairn; run = cairn.init(name='x', system=False, code=False); print(run.path)"
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path / "sub", capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "sub" / ".cairn" / "repo.json").is_file() and not (tmp_path / ".cairn").exists()
    assert "created the repo" in result.stderr


def test_cairn_init_is_idempotent_and_writes_only_the_repo(tmp_path, capsys):
    from cairn.cli import main

    (tmp_path / ".gitignore").write_text("mine\n")
    main(["init", str(tmp_path)])
    assert capsys.readouterr().out.startswith("created the repo")
    repo = tmp_path / ".cairn"
    assert json.loads((repo / "repo.json").read_text())["uid"] and (repo / ".gitignore").read_text() == "*\n"
    assert (repo.stat().st_mode & 0o777) == 0o700
    uid = json.loads((repo / "repo.json").read_text())["uid"]
    main(["init", str(tmp_path)])
    assert "exists already" in capsys.readouterr().out
    assert json.loads((repo / "repo.json").read_text())["uid"] == uid
    assert (tmp_path / ".gitignore").read_text() == "mine\n" and set(path.name for path in tmp_path.iterdir()) == {".gitignore", ".cairn"}
    template = (repo / "config.toml").read_text()
    assert load_config(tmp_path).repo == {} and "# [remote]" in template and "# exclude" in template  # commented out


def test_cairn_remote_shows_sets_and_removes_the_remote(proj, capsys):
    from cairn.cli import main

    file = proj / ".cairn" / "config.toml"
    file.write_text("# mine\n[repo]\nexclude = [\"*.pkl\"]  # keep\n")
    main(["remote"])
    assert capsys.readouterr().out == "no remote\n"
    main(["remote", "set", "http://box:8800/a/"])
    out = capsys.readouterr().out
    assert out.startswith("remote  http://box:8800/a\n") and "cairn sync" in out
    assert load_config().repo == {"exclude": ["*.pkl"], "remote": "http://box:8800/a"}
    assert file.read_text().startswith("# mine\n[repo]\nexclude = [\"*.pkl\"]  # keep\n")  # the rest as it was
    main(["remote", "set", "https://box/prefix/b"])
    assert load_config().repo["remote"] == "https://box/prefix/b" and file.read_text().count("[remote]") == 1
    main(["remote"])
    assert capsys.readouterr().out.endswith("https://box/prefix/b\n")
    for bad in ("ftp://box/a", "http://box:8800", "http://box/My Study", "box/a", "http://box/a?x=1"):
        with pytest.raises(SystemExit):
            main(["remote", "set", bad])
    main(["remote", "remove"])
    out = capsys.readouterr().out
    assert "removed the remote https://box/prefix/b" in out and "pending versions wait" in out
    assert "remote" not in load_config().repo and file.read_text() == "# mine\n[repo]\nexclude = [\"*.pkl\"]  # keep\n"
    main(["remote", "remove"])
    assert capsys.readouterr().out == "no remote\n"


def test_store_config_is_an_unknown_key(proj):
    write_config(proj, '[store]\nurl = "https://x.example.org"\n')
    with pytest.raises(ValueError, match=r"unknown key 'store'"):
        load_config()


def test_old_config_keys_are_errors(proj):
    for old in ('outputs_dir = "runs"', 'reports_dir = "reports"', '[hub]\nremote = "x"'):
        write_config(proj, old + "\n")
        with pytest.raises(ValueError, match="unknown key"):
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


def test_a_git_snapshot_of_a_project_in_a_subfolder_of_its_checkout(tmp_path):
    import subprocess

    from cairn.client.code import create_snapshot

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    project = tmp_path / "examples" / "demo"
    project.mkdir(parents=True)
    write_config(project, "")
    (project / "train.py").write_text("print('hi')\n")
    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (project / "train.py").write_text("print('changed')\n")
    create_snapshot(project, tmp_path / "out", "git", 2_000_000, 50_000_000)
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert "error" not in manifest
    assert "train.py" in json.dumps(manifest)
