import json
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("hydra")

from cairn.runs.hydra import finalize_study

CONFIG = """\
defaults:
  - _self_
run:
  name: demo
  description: A test run.
experiment:
  _target_: experiments.toy.Toy
  fail: false
hydra:
  run:
    dir: outputs/${run.name}/${now:%Y-%m-%d_%H-%M-%S-%f}
  sweep:
    dir: outputs/${run.name}/${now:%Y-%m-%d_%H-%M-%S-%f}
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # flat imports from the project root

from cairn.runs.hydra import run_experiment  # noqa: E402
from experiments.base import Experiment  # noqa: E402


@hydra.main(version_base=None, config_path="../configs", config_name="config")
def main(cfg: DictConfig) -> None:
    run_experiment(cfg, Experiment)


if __name__ == "__main__":
    main()
"""


@pytest.fixture
def hydra_project(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "config.yaml").write_text(CONFIG)
    (tmp_path / "experiments").mkdir()
    (tmp_path / "experiments" / "__init__.py").write_text("")
    (tmp_path / "experiments" / "run.py").write_text(RUN)
    (tmp_path / "experiments" / "base.py").write_text(textwrap.dedent("""\
        class Experiment:
            def run(self, output_dir):
                raise NotImplementedError
        """))
    (tmp_path / "experiments" / "toy.py").write_text(textwrap.dedent("""\
        from experiments.base import Experiment

        class Toy(Experiment):
            def __init__(self, fail):
                self.fail = fail

            def run(self, output_dir):
                if self.fail:
                    raise RuntimeError("asked to fail")
                (output_dir / "result.txt").write_text("ok")
        """))
    for command in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *command], cwd=tmp_path, check=True)
    return tmp_path


def run(project, *overrides):
    return subprocess.run(
        [sys.executable, "experiments/run.py", *overrides], cwd=project, capture_output=True, text=True
    )


def test_single_run_records_status_snapshot_and_latest(hydra_project):
    result = run(hydra_project)
    assert result.returncode == 0, result.stderr
    latest = hydra_project / "outputs" / "demo" / "latest"
    record = json.loads((latest / "run.json").read_text())
    assert record["status"] == "completed" and len(record["code_revision"]) == 40
    assert (latest / "result.txt").read_text() == "ok"
    assert (latest / "provenance" / "manifest.json").is_file()


def test_failed_run_never_becomes_latest(hydra_project):
    assert run(hydra_project, "experiment.fail=true").returncode != 0
    (folder,) = [p for p in (hydra_project / "outputs" / "demo").iterdir()]
    record = json.loads((folder / "run.json").read_text())
    assert record["status"] == "failed" and "asked to fail" in record["error"]
    assert not (hydra_project / "outputs" / "demo" / "latest").exists()


def test_study_completes_only_when_every_trial_completed(hydra_project):
    assert run(hydra_project, "-m", "experiment.fail=false,false").returncode == 0
    latest = hydra_project / "outputs" / "demo" / "latest"
    study = json.loads((latest / "run.json").read_text())
    assert study["kind"] == "study" and study["status"] == "completed"
    assert [t["status"] for t in study["trials"]] == ["completed", "completed"]
    run(hydra_project, "-m", "experiment.fail=false,true")
    studies = sorted(p for p in (hydra_project / "outputs" / "demo").iterdir() if p.name != "latest")
    failed = studies[-1] / "run.json"  # the basic launcher stops at the failing trial: no record, incomplete
    assert not failed.exists() or json.loads(failed.read_text())["status"] == "failed"
    assert latest.resolve() == studies[0]  # the failed study did not move latest


def test_finalize_study_after_a_dead_launcher(tmp_path):
    study = tmp_path / "outputs" / "sweep" / "2026-01-01_00-00-00"
    for trial in ("0", "1"):
        (study / trial / ".hydra").mkdir(parents=True)
        (study / trial / ".hydra" / "config.yaml").write_text("run:\n  name: sweep\n")
        (study / trial / "run.json").write_text(
            json.dumps({"status": "completed", "started": "t0", "code_revision": "abc"})
        )
    (study / ".submitit").mkdir()
    (study / ".submitit" / "123_1").mkdir()
    assert finalize_study(study) is True
    record = json.loads((study / "run.json").read_text())
    assert record["name"] == "sweep" and record["finalized_by"] == "cairn finalize-study"
    assert (tmp_path / "outputs" / "sweep" / "latest").resolve() == study.resolve()
    with pytest.raises(SystemExit):
        finalize_study(study)  # already finalized


def test_a_fresh_folder_gets_one_repo_at_the_launch_folder(hydra_project):
    """No `.cairn/` yet: a Hydra job (and a 2-trial multirun) whose job folder is the run's makes
    exactly one repo, in the folder it was launched from, and publishes its runs there."""
    from cairn.store import LocalRepo

    config = CONFIG.replace(
        """  run:
    dir: outputs/${run.name}/${now:%Y-%m-%d_%H-%M-%S-%f}
  sweep:
    dir: outputs/${run.name}/${now:%Y-%m-%d_%H-%M-%S-%f}
    subdir: ${hydra.job.num}""",
        """  run:
    dir: ${cairn.run:}
  sweep:
    dir: ${cairn.run:}
    subdir: ${hydra.job.num}""",
    )
    (hydra_project / "configs" / "config.yaml").write_text(config)
    assert not (hydra_project / ".cairn").exists()
    single = run(hydra_project)
    assert single.returncode == 0, single.stderr
    assert single.stderr.count("created the repo") == 1 and "created the repo .cairn " in single.stderr
    sweep = run(hydra_project, "-m", "experiment.fail=false,false")
    assert sweep.returncode == 0, sweep.stderr
    assert "created the repo" not in sweep.stderr  # found: the launch folder holds .cairn/repo.json now
    assert [path for path in hydra_project.rglob(".cairn") if path.is_dir()] == [hydra_project / ".cairn"]
    assert list(hydra_project.rglob("repo.json")) == [hydra_project / ".cairn" / "repo.json"]
    repo = LocalRepo(hydra_project / ".cairn")
    [project] = [item["name"] for item in repo.projects()]
    assert project == ""  # a project's repo holds one project, unnamed
    runs = repo.versions(project, "runs/demo")
    assert [v["status"] for v in runs] == ["completed"] * 4  # the single job, two trials, and the sweep's summary
    assert len([v for v in runs if (v["metadata"].get("sweep") or {}).get("trials")]) == 1
