import json
import os
import stat
import subprocess
import sys

import pytest

import cairn as store
from cairn import LocalStore, NotFound, StoreError
from cairn.store import refs
from cairn.store.cache import materialize


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A project folder with a configured local store, an isolated cache, and cwd inside the project."""
    monkeypatch.setenv("CAIRN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CAIRN_INPUTS", raising=False)
    project = tmp_path / "study"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    monkeypatch.chdir(project)
    return project


def folder(base, name, files):
    root = base / name
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


# --------------------------------------------------------------------- refs


@pytest.mark.parametrize("text,expected", [
    ("descent", (None, "descent", None, None)),
    ("descent@latest//figures/a.png", (None, "descent", "latest", "figures/a.png")),
    ("datasets/crops@v3", (None, "datasets/crops", "v3", None)),
    ("other:models/denoiser@best", ("other", "models/denoiser", "best", None)),
    ("a@2026-10-15_07-49-37", (None, "a", "2026-10-15_07-49-37", None)),
])
def test_ref_roundtrip(text, expected):
    ref = refs.parse(text)
    assert (ref.project, ref.name, ref.selector, ref.path) == expected
    assert str(ref) == text


def test_ref_version_property():
    assert refs.parse("a@v12").version == 12
    assert refs.parse("a@best").version is None


@pytest.mark.parametrize("text", ["", " a", "A", "a/../b", "a//", "a//x/../y", "a/", "/a", "a b", "a@", "a@v03",
                                  "a@v3x@y", "a@-x", "P:a", ":a", "a@b c", "a/.hidden", "a//x//y", "a..b/c/../d"])
def test_ref_rejects(text):
    with pytest.raises(ValueError):
        refs.parse(text)


# --------------------------------------------------------------- local store


def put(local, files):
    """Blob-store `files` ({path: text}) and return entries."""
    import hashlib
    import io

    entries = []
    for path, text in files.items():
        data = text.encode()
        sha = hashlib.sha256(data).hexdigest()
        local.put_blob(sha, io.BytesIO(data), len(data))
        entries.append({"path": path, "sha256": sha, "size": len(data)})
    return entries


@pytest.fixture
def local(tmp_path):
    s = LocalStore(tmp_path / "s", project="p")
    s.create_project("p")
    return s


def test_publish_and_resolve(local):
    v1 = local.publish("p", "run", put(local, {"a.txt": "1"}), label="first", aliases=["best"])
    v2 = local.publish("p", "run", put(local, {"a.txt": "2"}), label="second")
    assert (v1, v2) == (1, 2)
    assert local.resolve("run")[2]["version"] == 2
    assert local.resolve("run@latest")[2]["version"] == 2
    assert local.resolve("run@v1")[2]["version"] == 1
    assert local.resolve("run@best")[2]["version"] == 1
    assert local.resolve("p:run@first")[2]["version"] == 1
    assert local.artifacts("p") == ["run"]
    with pytest.raises(NotFound):
        local.resolve("run@nope")
    with pytest.raises(NotFound):
        local.resolve("run@v9")


def test_unchanged_publish_creates_no_version(local):
    entries = put(local, {"a.txt": "1"})
    assert local.publish("p", "run", entries) == 1
    assert local.publish("p", "run", entries) == 1
    assert len(local.versions("p", "run")) == 1
    assert local.publish("p", "run", entries, metadata={"x": 1}) == 2  # metadata differs
    assert local.publish("p", "run", entries, metadata={"x": 1}, aliases=["keep"]) == 2
    assert local.resolve("run@keep")[2]["version"] == 2


def test_completed_versions_are_immutable(local):
    version = local.publish("p", "run", put(local, {"a.txt": "1"}))
    for call in (lambda: local.add_files("p", "run", version, put(local, {"b.txt": "2"})),
                 lambda: local.complete_version("p", "run", version),
                 lambda: local.fail_version("p", "run", version)):
        with pytest.raises(StoreError):
            call()
    assert [f["path"] for f in local.resolve("run@v1")[2]["files"]] == ["a.txt"]


def test_running_and_failed_versions_do_not_resolve(local):
    local.publish("p", "run", put(local, {"a.txt": "1"}))
    running = local.start_version("p", "run", label="live")
    local.add_files("p", "run", running, put(local, {"b.txt": "2"}))
    assert local.resolve("run")[2]["version"] == 1
    with pytest.raises(NotFound):
        local.resolve("run@live")
    with pytest.raises(StoreError):
        local.resolve(f"run@v{running}")
    local.fail_version("p", "run", running)
    assert local.resolve("run")[2]["version"] == 1
    with pytest.raises(StoreError):
        local.resolve(f"run@v{running}")
    with pytest.raises(StoreError):
        local.add_files("p", "run", running, put(local, {"c.txt": "3"}))
    assert [v["status"] for v in local.versions("p", "run")] == ["completed", "failed"]


def test_running_version_grows_then_completes(local):
    n = local.start_version("p", "run")
    local.add_files("p", "run", n, put(local, {"a.txt": "1"}))
    local.add_files("p", "run", n, put(local, {"b.txt": "2", "a.txt": "1b"}))
    local.complete_version("p", "run", n)
    files = {f["path"] for f in local.resolve("run")[2]["files"]}
    assert files == {"a.txt", "b.txt"}


def test_add_files_validation(local):
    n = local.start_version("p", "run")
    with pytest.raises(ValueError):
        local.add_files("p", "run", n, [{"path": "../x", "sha256": "0" * 64, "size": 1}])
    with pytest.raises(StoreError):  # blob never uploaded
        local.add_files("p", "run", n, [{"path": "x", "sha256": "0" * 64, "size": 1}])
    with pytest.raises(ValueError):  # a link may not leave the artifact
        local.add_files("p", "run", n, [{"path": "x", "link": "../../y"}])
    local.add_files("p", "run", n, [{"path": "latest", "link": "a"}])


def test_alias_rules(local):
    local.publish("p", "run", put(local, {"a": "1"}))
    local.publish("p", "run", put(local, {"a": "2"}))
    with pytest.raises(StoreError):
        local.set_alias("p", "run", "latest", 1)
    with pytest.raises(ValueError):
        local.set_alias("p", "run", "v3", 1)
    running = local.start_version("p", "run")
    with pytest.raises(StoreError):
        local.set_alias("p", "run", "best", running)
    with pytest.raises(StoreError):
        local.set_alias("p", "run", "best", 99)
    local.set_alias("p", "run", "best", 1)
    local.set_alias("p", "run", "best", 2)
    assert local.aliases("p", "run") == {"latest": 2, "best": 2}
    log = local.alias_log("p", "run")  # every move is kept
    assert [(r["alias"], r["version"]) for r in log] == [("latest", 1), ("latest", 2), ("best", 1), ("best", 2)]


def test_lineage_both_directions(local):
    d = local.publish("p", "data", put(local, {"x": "d"}))
    local.publish("p", "fit", put(local, {"y": "f"}), inputs=["data@v1"])
    local.publish("p", "reports/r", put(local, {"report.md": "r"}), inputs=["fit"])
    assert local.lineage("fit") == {"inputs": ["p:data@v1"], "consumers": ["p:reports/r@v1"]}
    assert local.lineage("data") == {"inputs": [], "consumers": ["p:fit@v1"]}
    with pytest.raises(NotFound):
        local.publish("p", "bad", put(local, {"z": "z"}), inputs=["missing@v1"])
    assert d == 1


def test_project_records(local):
    assert local.get_project("p")["title"] == "p"
    assert [p["name"] for p in local.projects()] == ["p"]
    with pytest.raises(NotFound):
        local.get_project("other")
    with pytest.raises(ValueError):
        local.create_project("Bad Name")


# -------------------------------------------------------------- cache / pull


def test_materialize_read_only_and_idempotent(local, tmp_path):
    entries = put(local, {"a.txt": "1", "sub/b.txt": "2"}) + [{"path": "ln", "link": "a.txt"}]
    local.publish("p", "run", entries)
    cache = tmp_path / "c"
    folder_ = materialize(local, "run", cache)
    assert folder_ == cache / "versions" / local.get_version("p", "run", 1)["uid"] / "files"
    assert (folder_ / "sub/b.txt").read_text() == "2"
    assert (folder_ / "ln").is_symlink() and (folder_ / "ln").read_text() == "1"
    assert not os.access(folder_ / "a.txt", os.W_OK)
    assert stat.S_IMODE((folder_ / "a.txt").stat().st_mode) == 0o444
    inode = (folder_ / "a.txt").stat().st_ino
    assert materialize(local, "run@v1", cache) == folder_
    assert (folder_ / "a.txt").stat().st_ino == inode
    assert materialize(local, "run//sub/b.txt", cache) == folder_ / "sub/b.txt"
    with pytest.raises(NotFound):
        materialize(local, "run//nope.txt", cache)


def test_pull_records_inputs_in_cwd_and_env(env, tmp_path, monkeypatch):
    s = LocalStore(tmp_path / "s", project="study")
    s.create_project("study")
    s.publish("study", "datasets/crops", put(s, {"a.txt": "1"}))
    path = store.pull("datasets/crops@v1", store=s)
    assert (path / "a.txt").read_text() == "1"
    rows = [json.loads(line) for line in (env / ".cairn-inputs.jsonl").read_text().splitlines()]
    assert [r["ref"] for r in rows] == ["study:datasets/crops@v1"]
    target = tmp_path / "elsewhere.jsonl"
    monkeypatch.setenv("CAIRN_INPUTS", str(target))
    assert store.pull("datasets/crops//a.txt", store=s).read_text() == "1"
    assert json.loads(target.read_text())["ref"] == "study:datasets/crops@v1"
    assert len((env / ".cairn-inputs.jsonl").read_text().splitlines()) == 1


# ------------------------------------------------------------------ light API


def test_configured_store_pull_publish_lineage(env, tmp_path):
    (env / "cairn.toml").write_text('[store]\nexclude = ["*.pkl"]\n')
    data = folder(tmp_path, "crops", {"a.txt": "1", "skip.pkl": "x"})
    assert store.publish("datasets/crops", data, aliases=["paper"]) == 1
    assert (env / ".cairn" / "store").is_dir()
    assert "skip.pkl" not in {f["path"] for f in store.versions("datasets/crops")[0]["files"]}

    work = env / "work"
    work.mkdir()
    os.chdir(work)
    crops = store.pull("datasets/crops@paper")
    out = folder(work, "out", {"res.txt": (crops / "a.txt").read_text() + "!"})
    (work / ".cairn-inputs.jsonl").rename(out / ".cairn-inputs.jsonl")
    assert store.publish("fit", out, metadata={"loss": 0.1}) == 1
    version = store.versions("fit")[0]
    assert version["inputs"] == ["study:datasets/crops@v1"]
    assert version["label"] == "out" and version["metadata"] == {"loss": 0.1}
    assert version["source"]["folder"] == str(out.resolve())
    assert ".cairn-inputs.jsonl" not in {f["path"] for f in version["files"]}
    assert store.lineage("fit") == {"inputs": ["study:datasets/crops@v1"], "consumers": []}
    assert store.lineage("datasets/crops")["consumers"] == ["study:fit@v1"]
    assert store.publish("fit", out, metadata={"loss": 0.1}) == 1  # unchanged
    store.alias("fit@v1", "best")
    assert store.pull("fit@best", store=None).joinpath("res.txt").read_text() == "1!"


def test_publish_follows_symlinked_folder_and_labels_by_target(env, tmp_path):
    run = folder(tmp_path, "runs/2026-10-15_07-49-37", {"r.txt": "x"})
    (run.parent / "latest").symlink_to(run.name)
    store.publish("descent", run.parent / "latest")
    assert store.versions("descent")[0]["label"] == "2026-10-15_07-49-37"
    assert store.pull("descent@2026-10-15_07-49-37//r.txt").read_text() == "x"


def test_remote_store_without_a_token_and_plain_http(env, monkeypatch):
    monkeypatch.delenv("CAIRN_STORE_TOKEN", raising=False)
    (env / "cairn.toml").write_text('[store]\nurl = "https://store.example.org"\n')
    assert store.open_store().client.token == ""  # only a --no-auth server answers; others say a token is needed
    monkeypatch.setenv("CAIRN_STORE_TOKEN", "t" * 40)
    (env / "cairn.toml").write_text('[store]\nurl = "http://store.example.org"\n')
    with pytest.raises(SystemExit, match="plain HTTP"):
        store.open_store()
    (env / "cairn.toml").write_text('[store]\nurl = "http://store.example.org"\nallow_http = true\n')
    assert isinstance(store.open_store(), store.RemoteStore)


def test_old_hub_table_points_to_the_store(env):
    (env / "cairn.toml").write_text('[hub]\nurl = "https://hub.example.org"\n')
    with pytest.raises(ValueError, match=r"\[hub\] was removed.*\[store\] url"):
        store.open_store()


def test_store_config_validation(env):
    from cairn.config import load_config

    (env / "cairn.toml").write_text('[store]\npath = "s"\nproject = "p"\npublish = [{ folder = "r", name = "n" }]\n')
    assert load_config().store["publish"] == [{"folder": "r", "name": "n"}]
    for bad in ('token = "x"', 'oops = 1', 'exclude = "a"', 'publish = [{ folder = "r" }]'):
        (env / "cairn.toml").write_text(f"[store]\n{bad}\n")
        with pytest.raises(ValueError):
            load_config()


# ------------------------------------------------------------ light import, config rename


def test_import_cairn_stays_light():
    script = ("import sys, cairn; heavy = ['cairn.server', 'cairn.ui', 'cairn.reports', 'cairn.runs', 'matplotlib', 'plotly', 'hydra', 'imageio', 'yaml']; "
              "print([name for name in heavy if name in sys.modules])")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"
    assert subprocess.run([sys.executable, "-c", "import cairn; import cairn.client.publishing; assert callable(cairn.publish) and callable(cairn.pull) and cairn.Publication and cairn.__version__"]).returncode == 0


def test_config_file_cairn_toml_and_deprecated_name(tmp_path, capsys):
    from cairn import config
    from cairn.config import load_config

    (tmp_path / "cairn-report.toml").write_text('[store]\nproject = "old"\n')
    config._WARNED.clear()
    assert load_config(tmp_path).store["project"] == "old"
    err = capsys.readouterr().err
    assert "cairn-report.toml is deprecated" in err and err.count("\n") == 1
    (tmp_path / "cairn.toml").write_text('[store]\nproject = "new"\n')
    with pytest.raises(ValueError, match="both cairn.toml and cairn-report.toml"):
        load_config(tmp_path)
    (tmp_path / "cairn-report.toml").unlink()
    assert load_config(tmp_path).store["project"] == "new" and capsys.readouterr().err == ""
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert config.find_project(nested) == tmp_path.resolve()
