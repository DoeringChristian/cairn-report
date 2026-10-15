"""The example project (examples/descent) runs end to end against a local store, as its README says: run_example.py
in a fresh interpreter (which also checks that `import cairn.reports; cairn.reports.publish(...)` works without
anything importing `cairn.reports.build` first)."""

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
    monkeypatch.delenv("CAIRN_DATA_DIR", raising=False)  # the project's own .cairn/pending, as for a user

    result = subprocess.run([sys.executable, str(project / "run_example.py")], cwd=tmp_path, capture_output=True, text=True,
                            env={**os.environ})
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "published descent-example:datasets/quadratic@v1" in out and "published descent-example:descent@v1" in out
    assert "  run        descent-example:runs/descent@v1 (completed, config" in out  # the result's one input: its run
    assert "  data       descent-example:datasets/quadratic@v1" in out  # the data the run used, through the run
    assert "consumers  descent-example:reports/descent-study@v1" in out  # and its consumer, the report
    assert "code, environment, logs, metrics.jsonl, system" in out  # what the run recorded
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
