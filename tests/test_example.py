"""The example project (examples/descent) runs end to end against its own repo, as its README says: the dataset,
the run (descent.py, a fresh interpreter), and the report, each a command run from the example's folder."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cairn.cli import main

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "descent"

pytest.importorskip("matplotlib")


def cairn(*args: str) -> None:
    main(list(args))


def test_the_example_runs_dataset_run_report(tmp_path, monkeypatch, capsys):
    project = tmp_path / "descent"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns(".cairn", "__pycache__"))
    monkeypatch.delenv("CAIRN_DIR", raising=False)
    monkeypatch.delenv("CAIRN_INPUTS", raising=False)

    def step(*args: str) -> str:
        result = subprocess.run([sys.executable, *args], cwd=project, capture_output=True, text=True, env={**os.environ})
        assert result.returncode == 0, result.stderr
        return result.stdout

    out = step("-m", "cairn", "publish", "data/quadratic", "datasets/quadratic")
    assert "published datasets/quadratic@v1" in out
    assert "published descent-example:descent@v1" in step("descent.py")
    assert "reports/descent-study@v1" in step("-m", "cairn", "publish", "reports/descent-study")
    shown = step("-m", "cairn", "show", "descent@latest")
    assert "descent-example:runs/descent@v1" in shown  # the result's one input: its run
    assert "descent-example:datasets/quadratic@v1" in shown  # the data the run used, through the run
    assert "descent-example:reports/descent-study@v1" in shown  # and its consumer, the report
    drafts = project / ".cairn" / "drafts"
    assert not drafts.exists() or not any(drafts.rglob("*.draft.json"))  # the draft and run folders are gone
    assert not (project / "runs").exists() and not (project / ".cairn-inputs.jsonl").exists()

    monkeypatch.chdir(project)
    capsys.readouterr()
    cairn("show", "descent@latest")
    shown = capsys.readouterr().out
    assert "run          descent-example:runs/descent@v1" in shown
    assert "data         descent-example:datasets/quadratic@v1" in shown and "descent-example:reports/descent-study@v1" in shown
    cairn("show", "runs/descent@latest")
    shown = capsys.readouterr().out
    assert "config\n  dataset = \"datasets/quadratic@latest\"\n  learning_rate = 0.15\n  steps = 60\n" in shown
    assert "inputs       descent-example:datasets/quadratic@v1" in shown and "logs/stdout.txt" in shown

    built = (project / ".cairn" / "build" / "descent-study" / "narrative.html").read_text()
    assert "Final loss" in built and 'class="report-value"' in built
    manifest = json.loads((project / ".cairn" / "build" / "descent-study" / "manifest.json").read_text())
    assert manifest["inputs"]["descent"]["resolved"] == "descent-example:descent@v1"
    assert manifest["inputs"]["run"]["resolved"] == "descent-example:runs/descent@v1"
    assert (project / ".cairn" / ".gitignore").read_text().strip() == "*"

    cairn("alias", "descent@v1", "best")
    assert capsys.readouterr().out.strip() == "descent@best -> v1"
    with pytest.raises(SystemExit, match="moves automatically"):
        cairn("alias", "descent@v1", "latest")
