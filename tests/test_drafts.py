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

from test_objects import configured  # noqa: F401  (local store or store server, with .cairn/config.toml)
from test_store import env, folder  # noqa: F401
from conftest import write_config

LABEL = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")


def published_dataset(env, name="datasets/toy", files=None):
    data = folder(env, name.replace("/", "-"), files or {"x.txt": "1"})
    return cairn.connect().artifact(name).publish(data)


def test_a_draft_publishes_its_folder_on_exit(configured, env, monkeypatch):
    dataset = published_dataset(env)
    monkeypatch.setenv("CAIRN_INPUTS", str(env / "outer-inputs.jsonl"))
    assert not cairn.run
    with cairn.draft("descent", aliases=["best"], metadata={"seed": 0}) as model:
        run = cairn.client.runs.current()  # opening the draft started the process's run
        assert isinstance(run, cairn.Run) and run.name.startswith("runs/") and run.record["metadata"]["kind"] == "run"
        assert model.path.is_dir() and not any(model.path.iterdir())  # fresh and empty
        assert model.path.parent.parent == env / ".cairn" / "drafts" and model.path.name == model.id
        assert (env / ".cairn" / ".gitignore").read_text().strip() == "*"  # git-ignored by the folder's own .gitignore
        assert os.environ["CAIRN_INPUTS"] == str(run.path / ".cairn-inputs.jsonl")  # pulls are the run's inputs
        assert cairn.current_draft() is model and model.run_ref == run.ref
        cairn.pull("datasets/toy@latest")
        model.add({"loss": 0.5, "step": 0})
        model.add_json("final", {"loss": 0.25})
        (model.path / "model.pt").write_bytes(b"weights")
        folder_ = model.path
    assert cairn.current_draft() is None and cairn.run == run  # the run goes on until the process ends
    version = model.version
    assert isinstance(version, cairn.Version) and version.number == 1 and version.ref == "descent@v1"
    assert version.id == model.id and LABEL.fullmatch(version.label) and version.metadata == {"seed": 0}
    running = cairn.connect().version(run.ref)
    assert running.status == "running" and running.id == run.id and version.inputs == [run.id]  # exactly the run
    assert sorted(f.path for f in version.files) == ["artifacts.jsonl", "data/final.json", "metrics.jsonl", "model.pt"]
    assert cairn.connect().artifact("descent").aliases == {"latest": 1, "best": 1}
    assert not folder_.exists() and not (env / ".cairn" / "drafts" / "descent").exists()  # removed, record too
    pulled = cairn.pull("descent@best")
    assert read_scalars(pulled)["loss"] == 0.5 and (pulled / "model.pt").read_bytes() == b"weights"
    finished = run.finish()
    assert os.environ["CAIRN_INPUTS"] == str(env / "outer-inputs.jsonl")  # restored
    assert finished.status == "completed" and finished.inputs == [dataset.id, version.id]  # what it pulled
    assert dataset.consumers == [finished.id] and finished.consumers == [version.id]
    assert cairn.lineage("descent@v1") == {"inputs": [dataset.ref], "consumers": [], "runs": [finished.ref]}
    assert cairn.lineage("descent@v1", through_runs=False)["inputs"] == [finished.ref]
    assert [v.ref for v in dataset.downstream()] == ["descent@v1"] and version.run == finished


def test_an_unchanged_draft_makes_no_new_version(configured, env):
    for _ in range(2):
        with cairn.draft("fit") as run:
            (run.path / "a.txt").write_text("same")
    assert run.version.number == 1 and [v.number for v in cairn.connect().artifact("fit").versions()] == [1]


def test_an_exception_keeps_the_folder_and_publishes_nothing(configured, env, capsys):
    with pytest.raises(RuntimeError, match="diverged"):
        with cairn.draft("descent") as run:
            (run.path / "partial.txt").write_text("half")
            raise RuntimeError("diverged")
    assert (run.path / "partial.txt").read_text() == "half"
    assert "cairn: draft descent failed; folder kept (cairn drafts)\n" in capsys.readouterr().err
    assert run.version is None and cairn.connect().artifact("descent").versions() == []
    assert os.environ["CAIRN_INPUTS"] == str(cairn.run.path / ".cairn-inputs.jsonl")  # the run goes on
    [kept] = [item for item in drafts.drafts() if item.record.get("kind") != "run"]
    assert (kept.id, kept.state, kept.record["error"]) == (run.id, "kept", "RuntimeError: diverged")


def test_a_draft_published_while_the_server_is_down_is_pending(configured, env):
    if configured is None:
        pytest.skip("a store server only")
    store = cairn.connect()
    configured.stop()
    with cairn.draft("offline-run") as run:
        (run.path / "a.txt").write_text("a")
    version = run.version
    assert version.pending and version.number is None and version.id == run.id
    assert not run.path.exists()  # its files are in the repo's blobs now
    assert (cairn.pull(version.ref) / "a.txt").read_text() == "a"
    configured.start()
    store.sync()
    assert store.artifact("offline-run").latest.id == run.id


def test_nested_drafts_are_outputs_of_the_one_run(env):
    write_config(env, "")
    outer = cairn.draft("outer")
    inner = cairn.draft("inner")
    with cairn.draft("innermost") as innermost:
        assert cairn.current_draft() is innermost
        (innermost.path / "a.txt").write_text("a")
    assert cairn.current_draft() is inner
    outer.discard()  # out of order
    assert cairn.current_draft() is inner
    inner.discard()
    assert cairn.current_draft() is None and not outer.path.exists() and not inner.path.exists()
    run = cairn.client.runs.current()
    assert {outer.run_ref, inner.run_ref, innermost.run_ref} == {run.ref}
    assert innermost.version.inputs == [run.id]
    assert cairn.connect().version(run.id).ref == f"{run.name}@v1"


def test_drafts_get_exactly_the_run_and_pulls_inside_go_to_the_run(configured, env):
    dataset = published_dataset(env)
    other = published_dataset(env, "datasets/other")
    cairn.pull("datasets/toy@v1")  # before any draft
    store = cairn.connect()
    with cairn.draft("models/denoiser") as model:
        (model.path / "model.pt").write_bytes(b"w")
        with store.artifact("evals/denoiser").draft() as evaluation:
            assert cairn.use("datasets/other@latest") == other  # inside a draft: still the run's input
            (evaluation.path / "scores.json").write_text("{}")
        assert evaluation.version.number == 1
    assert not hasattr(model, "use")  # no explicit linking any more
    run = cairn.client.runs.current()
    assert model.version.inputs == evaluation.version.inputs == [run.id]
    finished = run.finish()
    assert finished.inputs == [dataset.id, other.id]
    assert sorted(finished.consumers) == sorted([evaluation.version.id, model.version.id])
    assert sorted(store.version(id).ref for id in finished.consumers) == ["evals/denoiser@v1", "models/denoiser@v1"]
    assert [v.ref for v in evaluation.version.upstream()] == [dataset.ref, other.ref]
    assert drafts.drafts() == []


def test_a_failed_run_keeps_the_edge_to_its_outputs(configured, env):
    with cairn.draft("model") as model:
        (model.path / "w.txt").write_text("w")
    run = cairn.client.runs.current()
    run.finish(RuntimeError("diverged"))
    store = cairn.connect()
    if store.remote:
        store.sync()
    failed = store.version(run.id)
    assert failed.status == "failed" and failed.consumers == [model.version.id]
    assert model.version.refresh().inputs == [failed.id]
    assert "RuntimeError: diverged" in (run.folder / "logs" / "error.txt").read_text()  # the folder is kept
    lineage = store.backend.lineage(failed.ref, marks=True)
    assert lineage["id"] == failed.id and lineage["consumers"] == [model.version.id]
    assert lineage["nodes"][failed.id] == {
        "name": run.name,
        "version": failed.number,
        "status": "failed",
        "kind": "run",
        "short_id": failed.short_id,
    }


def test_config_becomes_metadata(env, capsys):
    write_config(env, "")
    with cairn.draft("fit", config={"lr": 0.1, "model": {"depth": 3, "widths": [8, 16]}}) as fit:
        assert fit.config == {"lr": 0.1, "model": {"depth": 3, "widths": [8, 16]}}
        (fit.path / "a.txt").write_text("a")
    assert fit.version.metadata == {"config": {"lr": 0.1, "model": {"depth": 3, "widths": [8, 16]}}}
    capsys.readouterr()
    main(["show", "fit@latest"])
    shown = capsys.readouterr().out
    assert re.search(r"config\n  lr += 0.1\n  model.depth += 3\n  model.widths += \[8, 16\]\n", shown), shown


def test_watch_keeps_a_running_version_synced(configured, env):
    store = cairn.connect()
    with cairn.draft("live", watch=True, aliases=["now"]) as run:
        (run.path / "first.txt").write_text("1")
        assert run.sync() == 1
        running = store.artifact("live").version(run.id)
        assert running.status == "running" and running.id == run.id
        (run.path / "second.txt").write_text("2")
    version = run.version
    assert version.status == "completed" and version.id == run.id
    assert [f.path for f in version.files] == ["first.txt", "second.txt"]
    assert store.artifact("live").aliases["now"] == version.number
    assert not run.path.exists()


def test_watch_marks_the_version_failed_on_an_exception(configured, env):
    with pytest.raises(KeyError):
        with cairn.draft("live", watch=True) as run:
            (run.path / "partial.txt").write_text("1")
            raise KeyError("oops")
    store = cairn.connect()
    if store.remote:
        store.sync()
    [version] = store.artifact("live").versions()
    assert version.status == "failed" and [f.path for f in version.files] == ["partial.txt"]
    assert run.path.is_dir()
    main(["drafts", "publish", run.id])  # a new version (the failed one keeps the draft's id)
    [failed, completed] = store.artifact("live").versions()
    assert (failed.status, completed.status) == ("failed", "completed") and completed.id != run.id


def test_watch_syncs_in_the_background(env):
    write_config(env, "")
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
        while time.time() < deadline and not store.backend.get_version("live", run.id)["files"]:
            time.sleep(0.05)
        assert [entry["path"] for entry in store.backend.get_version("live", run.id)["files"]] == ["a.txt"]


def test_cli_lists_publishes_and_discards_kept_drafts(configured, env, capsys):
    for name in ("first", "second", "third"):
        with pytest.raises(RuntimeError):
            with cairn.draft(f"jobs/{name}") as run:
                (run.path / "out.txt").write_text(name)
                if name == "third":
                    (run.path / "run.json").write_text(json.dumps({"status": "failed"}))
                raise RuntimeError(name)
    capsys.readouterr()
    cairn.run.finish()  # this process's run: it is a draft too, until it ends
    capsys.readouterr()
    main(["drafts"])
    listed = capsys.readouterr().out.splitlines()[1:]  # under the header
    assert [line.split()[0] for line in listed] == ["jobs/first", "jobs/second", "jobs/third"]
    found = drafts.drafts()
    assert all(
        line.split()[1] == found[i].short_id
        and line.split()[2].rstrip("…") in found[i].id
        and line.split()[3] == "kept"
        and line.endswith(found[i].id)
        for i, line in enumerate(listed)
    )
    first, second, third = drafts.drafts()

    main(["drafts", "publish", first.id[:30]])
    assert capsys.readouterr().out.startswith(f"published jobs/first@v1 ({first.short_id}, 1 file,")
    main(["drafts", "discard", str(second.folder)])
    assert not second.folder.exists()
    with pytest.raises(SystemExit, match="says failed"):
        main(["drafts", "publish", third.id])
    assert third.folder.is_dir()
    main(["drafts", "publish", third.id, "--allow-incomplete"])
    assert cairn.connect().artifact("jobs/third").latest.metadata == {"run_status": "failed"}
    [run_id] = cairn.connect().artifact("jobs/third").latest.inputs
    assert cairn.connect().version(run_id).artifact.name.startswith("runs/")  # its run, completed
    capsys.readouterr()
    main(["drafts", "list"])
    assert capsys.readouterr().out.startswith("no drafts in ")
    with pytest.raises(SystemExit, match="no draft"):
        main(["drafts", "publish", "nope"])


def test_outside_a_project_drafts_live_in_the_repo_made_here(tmp_path, capsys):
    with cairn.draft("loose") as run:
        assert run.path.parent.parent == tmp_path / ".cairn" / "drafts"
        (run.path / "a.txt").write_text("a")
    assert run.version.ref == "loose@v1" and not run.path.exists()
    assert (tmp_path / ".cairn" / "repo.json").is_file()
    assert "created the repo" in capsys.readouterr().err


def test_a_local_store_shares_storage_with_the_published_blobs(env, monkeypatch):
    """Where the filesystem cannot clone, the blob is a hard link to the draft's file (never a copy); a draft
    kept after a failed publish gets its own copies back."""
    import cairn.store.fs

    monkeypatch.setattr(cairn.store.fs, "clone", lambda source, target: False)
    write_config(env, "")
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
        draft.add({"score": 1.0})
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
    for command in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *command], cwd=env, check=True)
    return env


def hydra(project, *overrides):
    return subprocess.run(
        [sys.executable, "experiments/run.py", *overrides], cwd=project, capture_output=True, text=True
    )


def test_hydra_single_run_publishes_its_draft(hydra_project):
    dataset = published_dataset(hydra_project)
    result = hydra(hydra_project, "experiment.pull=datasets/toy@latest")
    assert result.returncode == 0, result.stderr
    store = cairn.connect()
    if store.remote:
        store.sync()
    version = store.artifact("demo").latest
    paths = {f.path for f in version.files}
    assert {"result.txt", "run.json", "metrics.jsonl", "provenance/manifest.json", ".hydra/config.yaml"} <= paths
    run = store.version("runs/demo@v1")
    assert version.inputs == [run.id] and LABEL.fullmatch(version.label)  # the job's run
    assert version.metadata["config"]["experiment"]["pull"] == "datasets/toy@latest"
    assert version.metadata["overrides"] == ["experiment.pull=datasets/toy@latest"]
    with version.open("run.json") as handle:
        assert json.loads(handle.read())["status"] == "completed"
    assert run.status == "completed" and run.inputs == [dataset.id] and run.consumers == [version.id]
    assert run.metadata["kind"] == "run" and run.metadata["config"]["run"]["name"] == "demo"
    assert run.metadata["overrides"] == ["experiment.pull=datasets/toy@latest"]
    assert drafts.drafts() == []  # one draft for every resolution of run.dir, and it is gone; the run too
    assert not (hydra_project / "outputs").exists() and not (hydra_project / "runs").exists()


def test_hydra_failed_run_keeps_its_draft(hydra_project):
    result = hydra(hydra_project, "experiment.fail=true")
    assert result.returncode != 0 and "cairn: run failed; folder kept (cairn drafts)" in result.stderr
    kept = {item.name: item for item in drafts.drafts()}
    assert set(kept) == {"demo", "runs/demo"} and kept["demo"].state == kept["runs/demo"].state == "kept"
    assert json.loads((kept["demo"].folder / "run.json").read_text())["status"] == "failed"
    assert "asked to fail" in (kept["runs/demo"].folder / "logs" / "error.txt").read_text()
    store = cairn.connect()
    if store.remote:
        store.sync()
    assert store.artifact("demo").versions() == []
    assert [v.status for v in store.artifact("runs/demo").versions()] == ["failed"]


def test_hydra_multirun_publishes_one_sweep_draft(hydra_project):
    first = published_dataset(hydra_project, "datasets/a")
    second = published_dataset(hydra_project, "datasets/b")
    result = hydra(hydra_project, "-m", "experiment.pull=datasets/a@v1,datasets/b@v1")
    assert result.returncode == 0, result.stderr
    store = cairn.connect()
    if store.remote:
        store.sync()
    [version] = store.artifact("demo-sweep").versions()
    paths = {f.path for f in version.files}
    assert {
        "run.json",
        "multirun.yaml",
        "0/result.txt",
        "1/result.txt",
        "0/metrics.jsonl",
        "1/provenance/manifest.json",
    } <= paths
    assert [store.version(id).ref for id in version.inputs] == ["runs/demo@v1", "runs/demo@v2"]  # the trials' runs
    assert [store.version(id).inputs for id in version.inputs] == [[first.id], [second.id]]
    assert [v.ref for v in version.upstream()] == [first.ref, second.ref]
    with version.open("run.json") as handle:
        study = json.loads(handle.read())
    assert study["kind"] == "study" and [t["status"] for t in study["trials"]] == ["completed", "completed"]
    assert version.metadata["overrides"] == ["experiment.pull=datasets/a@v1,datasets/b@v1"]
    assert drafts.drafts() == []  # one draft for the whole sweep, gone after publishing
    assert store.artifact("demo").versions() == []  # run.dir made no stray draft


def test_finalize_study_publishes_a_sweep_draft(env, capsys):
    pytest.importorskip("hydra")
    write_config(env, "")
    sweep = drafts.Draft("sweep", activate=False)
    for trial in ("0", "1"):
        (sweep.folder / trial / ".hydra").mkdir(parents=True)
        (sweep.folder / trial / ".hydra" / "config.yaml").write_text("run:\n  name: sweep\n")
        (sweep.folder / trial / "run.json").write_text(
            json.dumps({"status": "completed", "started": "t0", "code_revision": "abc"})
        )
    main(["finalize-study", str(sweep.folder)])
    assert "completed and published" in capsys.readouterr().out
    version = cairn.connect().artifact("sweep").latest
    assert {"run.json", "0/run.json", "1/run.json"} <= {f.path for f in version.files} and not sweep.folder.exists()
