"""Drafts (`cairn.draft`): a folder managed by Cairn that becomes a version when it closes, as a context manager,
as a run logger, from Hydra (`${cairn.draft:...}`), and from the CLI (`cairn drafts`); against a local store and
a store server."""

import json
import os
import re
import subprocess
import sys
import textwrap
import time

import pytest

import cairn
from cairn.cli import main
from cairn.client import drafts
from cairn.formats import read_scalars

from test_objects import configured  # noqa: F401  (local store or store server, with cairn.toml)
from test_store import env, folder  # noqa: F401

LABEL = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")


def published_dataset(env, name="datasets/toy", files=None):
    data = folder(env, name.replace("/", "-"), files or {"x.txt": "1"})
    return cairn.connect().project().artifact(name).publish(data)


def test_a_draft_publishes_its_folder_on_exit(configured, env, monkeypatch):
    dataset = published_dataset(env)
    monkeypatch.setenv("CAIRN_INPUTS", str(env / "outer-inputs.jsonl"))
    with cairn.draft("descent", aliases=["best"], metadata={"seed": 0}) as run:
        assert run.path.is_dir() and not any(run.path.iterdir())  # fresh and empty
        assert run.path.parent.parent == env / ".cairn" / "drafts" and run.path.name == run.uid
        assert (env / ".cairn" / ".gitignore").read_text().strip() == "*"  # git-ignored by the folder's own .gitignore
        assert os.environ["CAIRN_INPUTS"] == str(run.path / ".cairn-inputs.jsonl")
        assert cairn.current_draft() is run
        cairn.pull("datasets/toy@latest")
        run.scalars({"loss": 0.5}, step=0)
        run.json("final", {"loss": 0.25})
        (run.path / "model.pt").write_bytes(b"weights")
        folder_ = run.path
    assert os.environ["CAIRN_INPUTS"] == str(env / "outer-inputs.jsonl")  # restored
    assert cairn.current_draft() is None
    version = run.version
    assert isinstance(version, cairn.Version) and version.number == 1 and version.ref == "study:descent@v1"
    assert version.uid == run.uid and LABEL.fullmatch(version.label) and version.metadata == {"seed": 0}
    assert version.inputs == [dataset.ref]
    assert sorted(f.path for f in version.files) == ["artifacts.jsonl", "data/final.json", "metrics.jsonl", "model.pt"]
    assert cairn.connect().project().artifact("descent").aliases == {"latest": 1, "best": 1}
    assert not folder_.exists() and not (env / ".cairn" / "drafts" / "descent").exists()  # removed, record too
    pulled = cairn.pull("descent@best")
    assert read_scalars(pulled)["loss"] == 0.5 and (pulled / "model.pt").read_bytes() == b"weights"
    assert dataset.consumers == ["study:descent@v1"]


def test_an_unchanged_draft_makes_no_new_version(configured, env):
    for _ in range(2):
        with cairn.draft("fit") as run:
            (run.path / "a.txt").write_text("same")
    assert run.version.number == 1 and [v.number for v in cairn.connect().project().artifact("fit").versions()] == [1]


def test_an_exception_keeps_the_folder_and_publishes_nothing(configured, env, capsys):
    with pytest.raises(RuntimeError, match="diverged"):
        with cairn.draft("descent") as run:
            (run.path / "partial.txt").write_text("half")
            raise RuntimeError("diverged")
    assert (run.path / "partial.txt").read_text() == "half"
    assert f"kept the draft {run.path}" in capsys.readouterr().err
    assert run.version is None and cairn.connect().project().artifact("descent").versions() == []
    assert "CAIRN_INPUTS" not in os.environ
    [kept] = drafts.drafts()
    assert (kept.uid, kept.state, kept.record["error"]) == (run.uid, "kept", "RuntimeError: diverged")


def test_a_draft_published_while_the_server_is_down_is_pending(configured, env):
    if configured is None:
        pytest.skip("a store server only")
    store = cairn.connect()
    configured.stop()
    with cairn.draft("offline-run") as run:
        (run.path / "a.txt").write_text("a")
    version = run.version
    assert version.pending and version.number is None and version.uid == run.uid
    assert not run.path.exists()  # its files are in the replica now
    assert (cairn.pull(version.ref) / "a.txt").read_text() == "a"
    configured.start()
    store.sync()
    assert store.project().artifact("offline-run").latest.uid == run.uid


def test_nested_drafts_restore_the_inputs_file_in_any_order(env, monkeypatch):
    monkeypatch.setenv("CAIRN_INPUTS", "before.jsonl")
    outer = cairn.draft("outer")
    inner = cairn.draft("inner")
    assert os.environ["CAIRN_INPUTS"] == str(inner.path / ".cairn-inputs.jsonl")
    with cairn.draft("innermost") as innermost:
        assert cairn.current_draft() is innermost
    assert os.environ["CAIRN_INPUTS"] == str(inner.path / ".cairn-inputs.jsonl")
    outer.discard()  # out of order: inner is still open, and now restores to what outer had
    assert os.environ["CAIRN_INPUTS"] == str(inner.path / ".cairn-inputs.jsonl")
    inner.discard()
    assert os.environ["CAIRN_INPUTS"] == "before.jsonl"
    assert not outer.path.exists() and not inner.path.exists()


def test_several_drafts_share_pulls_and_one_can_use_another(configured, env):
    dataset = published_dataset(env)
    cairn.pull("datasets/toy@v1")  # before any draft: an input of the drafts opened next
    store = cairn.connect()
    with cairn.draft("models/denoiser") as model:
        (model.path / "model.pt").write_bytes(b"w")
        with store.project().artifact("evals/denoiser").draft() as evaluation:
            used = evaluation.use(model)
            assert used == f"study:models/denoiser@{model.uid}"
            (evaluation.path / "scores.json").write_text("{}")
        assert evaluation.version is None  # it waits for its input
    assert model.version.number == 1 and evaluation.version.number == 1
    assert model.version.inputs == [dataset.ref]
    assert evaluation.version.inputs == [dataset.ref, "study:models/denoiser@v1"]  # the model's exact version
    assert model.version.consumers == ["study:evals/denoiser@v1"]
    assert drafts.drafts() == []


def test_a_draft_using_a_failed_draft_is_kept(env, capsys):
    (env / "cairn.toml").write_text('[store]\nproject = "study"\n')
    with pytest.raises(ValueError):
        with cairn.draft("model") as model:
            with cairn.draft("eval") as evaluation:
                evaluation.use(model)
            raise ValueError("no model")
    assert sorted(item.name for item in drafts.drafts()) == ["eval", "model"]
    assert "its input model@" in capsys.readouterr().err


def test_config_becomes_metadata(env, capsys):
    (env / "cairn.toml").write_text('[store]\nproject = "study"\n')
    with cairn.draft("fit", config={"lr": 0.1, "model": {"depth": 3, "widths": [8, 16]}}) as run:
        assert run.config == {"lr": 0.1, "model": {"depth": 3, "widths": [8, 16]}}
        (run.path / "a.txt").write_text("a")
    assert run.version.metadata == {"config": {"lr": 0.1, "model": {"depth": 3, "widths": [8, 16]}}}
    capsys.readouterr()
    main(["show", "fit@latest"])
    shown = capsys.readouterr().out
    assert "config\n  lr = 0.1\n  model.depth = 3\n  model.widths = [8, 16]\n" in shown


def test_watch_keeps_a_running_version_synced(configured, env):
    store = cairn.connect()
    with cairn.draft("live", watch=True, aliases=["now"]) as run:
        (run.path / "first.txt").write_text("1")
        assert run.sync() == 1
        running = store.project().artifact("live").version(run.uid)
        assert running.status == "running" and running.uid == run.uid
        (run.path / "second.txt").write_text("2")
    version = run.version
    assert version.status == "completed" and version.uid == run.uid
    assert [f.path for f in version.files] == ["first.txt", "second.txt"]
    assert store.project().artifact("live").aliases["now"] == version.number
    assert not run.path.exists()


def test_watch_marks_the_version_failed_on_an_exception(configured, env):
    with pytest.raises(KeyError):
        with cairn.draft("live", watch=True) as run:
            (run.path / "partial.txt").write_text("1")
            raise KeyError("oops")
    store = cairn.connect()
    if store.remote:
        store.sync()
    [version] = store.project().artifact("live").versions()
    assert version.status == "failed" and [f.path for f in version.files] == ["partial.txt"]
    assert run.path.is_dir()
    main(["drafts", "publish", run.uid])  # a new version (the failed one keeps the draft's uid)
    [failed, completed] = store.project().artifact("live").versions()
    assert (failed.status, completed.status) == ("failed", "completed") and completed.uid != run.uid


def test_watch_syncs_in_the_background(env):
    (env / "cairn.toml").write_text('[store]\nproject = "study"\n')
    store = cairn.connect()
    with cairn.draft("live", watch=True) as run:
        run._stop.set()  # restart the follower with a short interval
        run._watcher.join()
        import threading

        run._stop = threading.Event()
        run._watcher = threading.Thread(target=run._follow, args=(run._stop, 0.1), daemon=True)
        run._watcher.start()
        (run.path / "a.txt").write_text("a")
        deadline = time.time() + 10
        while time.time() < deadline and not store.backend.get_version("study", "live", run.uid)["files"]:
            time.sleep(0.05)
        assert [entry["path"] for entry in store.backend.get_version("study", "live", run.uid)["files"]] == ["a.txt"]


def test_cli_lists_publishes_and_discards_kept_drafts(configured, env, capsys):
    for name in ("first", "second", "third"):
        with pytest.raises(RuntimeError):
            with cairn.draft(f"runs/{name}") as run:
                (run.path / "out.txt").write_text(name)
                if name == "third":
                    (run.path / "run.json").write_text(json.dumps({"status": "failed"}))
                raise RuntimeError(name)
    capsys.readouterr()
    main(["drafts"])
    listed = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[0] for line in listed] == ["runs/first", "runs/second", "runs/third"]
    assert all(line.split("\t")[2] == "kept" and line.endswith(drafts.drafts()[i].uid) for i, line in enumerate(listed))
    first, second, third = drafts.drafts()

    main(["drafts", "publish", first.uid[:30]])
    assert capsys.readouterr().out.startswith("published study:runs/first@v1 (1 files")
    main(["drafts", "discard", str(second.folder)])
    assert not second.folder.exists()
    with pytest.raises(SystemExit, match="says failed"):
        main(["drafts", "publish", third.uid])
    assert third.folder.is_dir()
    main(["drafts", "publish", third.uid, "--allow-incomplete"])
    assert cairn.connect().project().artifact("runs/third").latest.metadata == {"run_status": "failed"}
    capsys.readouterr()
    main(["drafts", "list"])
    assert capsys.readouterr().out.startswith("no drafts in ")
    with pytest.raises(SystemExit, match="no draft"):
        main(["drafts", "publish", "nope"])


def test_outside_a_project_drafts_live_in_the_data_root(tmp_path):
    local = cairn.LocalStore(tmp_path / "store-root", "loose")
    with cairn.draft("loose", store=local) as run:
        assert run.path.parent.parent == tmp_path / "data" / "drafts"
        (run.path / "a.txt").write_text("a")
    assert run.version.ref == "loose:loose@v1" and not run.path.exists()


def test_a_local_store_shares_storage_with_the_published_blobs(env, monkeypatch):
    """Where the filesystem cannot clone, the blob is a hard link to the draft's file (never a copy); a draft
    kept after a failed publish gets its own copies back."""
    import cairn.store.fs

    monkeypatch.setattr(cairn.store.fs, "clone", lambda source, target: False)
    (env / "cairn.toml").write_text('[store]\nproject = "study"\n')
    with cairn.draft("big") as run:
        (run.path / "data.bin").write_bytes(os.urandom(1 << 16))
        inode = (run.path / "data.bin").stat().st_ino
    assert cairn.connect().backend.blob_path(run.version.files[0].sha256).stat().st_ino == inode
    linked = env / "shared.bin"
    os.link(cairn.connect().backend.blob_path(run.version.files[0].sha256), linked)
    drafts._unshare(env)
    assert linked.stat().st_ino != inode and linked.stat().st_nlink == 1


# ------------------------------------------------------------------ Hydra

CONFIG = """\
defaults:
  - _self_
run:
  name: demo
experiment:
  _target_: experiments.toy.Toy
  fail: false
  pull: null
hydra:
  run:
    dir: ${cairn.draft:demo}
  sweep:
    dir: ${cairn.draft:demo-sweep}
    subdir: ${hydra.job.num}
  callbacks:
    source_snapshot:
      _target_: cairn.runs.snapshot.SourceSnapshotCallback
    study_completion:
      _target_: cairn.runs.hydra.StudyCompletion
"""

RUN = """\
import sys
from pathlib import Path

import hydra
from omegaconf import DictConfig

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cairn.runs.hydra import run_experiment  # noqa: E402  (also registers ${cairn.draft:...})


@hydra.main(version_base=None, config_path="../configs", config_name="config")
def main(cfg: DictConfig) -> None:
    run_experiment(cfg)


if __name__ == "__main__":
    main()
"""

TOY = """\
import cairn


class Toy:
    def __init__(self, fail, pull=None):
        self.fail, self.pull = fail, pull

    def run(self, output_dir):
        if self.pull:
            cairn.pull(self.pull)
        draft = cairn.current_draft()
        assert draft is not None and draft.path == output_dir
        draft.scalar("score", 1.0)
        (output_dir / "result.txt").write_text("ok")
        if self.fail:
            raise RuntimeError("asked to fail")
"""


@pytest.fixture
def hydra_project(configured, env):
    pytest.importorskip("hydra")
    (env / "configs").mkdir()
    (env / "configs" / "config.yaml").write_text(CONFIG)
    (env / "experiments").mkdir()
    (env / "experiments" / "__init__.py").write_text("")
    (env / "experiments" / "run.py").write_text(RUN)
    (env / "experiments" / "toy.py").write_text(TOY)
    (env / ".gitignore").write_text(".cairn/\n")
    for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *command], cwd=env, check=True)
    return env


def hydra(project, *overrides):
    return subprocess.run([sys.executable, "experiments/run.py", *overrides], cwd=project, capture_output=True, text=True)


def test_hydra_single_run_publishes_its_draft(hydra_project):
    dataset = published_dataset(hydra_project)
    result = hydra(hydra_project, "experiment.pull=datasets/toy@latest")
    assert result.returncode == 0, result.stderr
    store = cairn.connect()
    if store.remote:
        store.sync()
    version = store.project().artifact("demo").latest
    paths = {f.path for f in version.files}
    assert {"result.txt", "run.json", "metrics.jsonl", "provenance/manifest.json", ".hydra/config.yaml"} <= paths
    assert version.inputs == [dataset.ref] and LABEL.fullmatch(version.label)
    assert version.metadata["config"]["experiment"]["pull"] == "datasets/toy@latest"
    assert version.metadata["overrides"] == ["experiment.pull=datasets/toy@latest"]
    assert json.loads(version.open("run.json").read())["status"] == "completed"
    assert drafts.drafts() == []  # one draft for every resolution of run.dir, and it is gone
    assert not (hydra_project / "outputs").exists() and not (hydra_project / "runs").exists()


def test_hydra_failed_run_keeps_its_draft(hydra_project):
    result = hydra(hydra_project, "experiment.fail=true")
    assert result.returncode != 0 and "kept the draft" in result.stderr
    [kept] = drafts.drafts()
    assert kept.name == "demo" and kept.state == "kept"
    assert json.loads((kept.folder / "run.json").read_text())["status"] == "failed"
    assert cairn.connect().project().artifact("demo").versions() == []


def test_hydra_multirun_publishes_one_sweep_draft(hydra_project):
    first = published_dataset(hydra_project, "datasets/a")
    second = published_dataset(hydra_project, "datasets/b")
    result = hydra(hydra_project, "-m", "experiment.pull=datasets/a@v1,datasets/b@v1")
    assert result.returncode == 0, result.stderr
    store = cairn.connect()
    if store.remote:
        store.sync()
    [version] = store.project().artifact("demo-sweep").versions()
    paths = {f.path for f in version.files}
    assert {"run.json", "multirun.yaml", "0/result.txt", "1/result.txt", "0/metrics.jsonl", "1/provenance/manifest.json"} <= paths
    assert version.inputs == [first.ref, second.ref]  # merged from the trials' own inputs files
    study = json.loads(version.open("run.json").read())
    assert study["kind"] == "study" and [t["status"] for t in study["trials"]] == ["completed", "completed"]
    assert version.metadata["overrides"] == ["experiment.pull=datasets/a@v1,datasets/b@v1"]
    assert drafts.drafts() == []  # one draft for the whole sweep, gone after publishing
    assert store.project().artifact("demo").versions() == []  # run.dir made no stray draft


def test_finalize_study_publishes_a_sweep_draft(env, capsys):
    pytest.importorskip("hydra")
    (env / "cairn.toml").write_text('[store]\nproject = "study"\n')
    sweep = drafts.Draft("sweep", activate=False)
    for trial in ("0", "1"):
        (sweep.folder / trial / ".hydra").mkdir(parents=True)
        (sweep.folder / trial / ".hydra" / "config.yaml").write_text("run:\n  name: sweep\n")
        (sweep.folder / trial / "run.json").write_text(json.dumps({"status": "completed", "started": "t0", "code_revision": "abc"}))
    main(["finalize-study", str(sweep.folder)])
    assert "completed and published" in capsys.readouterr().out
    version = cairn.connect().project().artifact("sweep").latest
    assert {"run.json", "0/run.json", "1/run.json"} <= {f.path for f in version.files} and not sweep.folder.exists()
