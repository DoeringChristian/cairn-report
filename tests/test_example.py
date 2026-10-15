"""The example project (examples/descent) runs end to end against a local store, as its README says."""

import json
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
    shutil.copytree(EXAMPLE, project)
    monkeypatch.chdir(project)
    monkeypatch.delenv("CAIRN_DIR", raising=False)

    cairn("publish", "data/quadratic", "datasets/quadratic")
    subprocess.run([sys.executable, "descent.py"], check=True, capture_output=True, text=True)
    cairn("publish", "runs/descent/latest", "descent")
    cairn("publish", "reports/descent-study")
    capsys.readouterr()

    cairn("show", "descent@latest")
    shown = capsys.readouterr().out
    assert "descent-example:datasets/quadratic@v1" in shown  # the run's input, recorded by its pull
    assert "descent-example:reports/descent-study@v1" in shown  # and its consumer, the report

    built = (project / ".cairn" / "build" / "descent-study" / "narrative.html").read_text()
    assert "Final loss" in built and 'class="report-value"' in built
    manifest = json.loads((project / ".cairn" / "build" / "descent-study" / "manifest.json").read_text())
    assert manifest["inputs"]["descent"]["resolved"] == "descent-example:descent@v1"
    assert (project / ".cairn" / ".gitignore").read_text().strip() == "*"

    cairn("alias", "descent@v1", "best")
    assert capsys.readouterr().out.strip() == "descent@best -> v1"
    with pytest.raises(SystemExit, match="moves automatically"):
        cairn("alias", "descent@v1", "latest")
