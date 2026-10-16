import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytest

from cairn.client.logger import Writer

RUN = "demo/2026-01-01_00-00-00"


def histogram(values):
    figure, axis = plt.subplots(figsize=(4, 2))
    axis.hist(values)
    return figure


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_data(base: Path) -> Path:
    """A small folder of results, as a run writes them: metrics, a figure, a JSON file."""
    run = base / "descent-run"
    log = Writer(run)
    log.add({"rmse": 0.5})
    log.add_figure("hist", histogram, None, [1, 2, 2, 3])
    log.add_json("summary", {"steps": 3})
    log.close()
    return run


def make_tree(project: Path) -> Path:
    """Two flat reports: alpha reads the artifact `descent` (declared as an input) and links to beta."""
    import cairn as store
    from cairn.config import load_config

    config = load_config(project)
    store.publish("descent", make_data(project.parent), config=config)
    reports = project / "reports"
    write(reports / "alpha" / "assets" / "strip.svg",
          '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="100"><rect width="400" height="100" fill="#dcebe5"/></svg>')
    write(reports / "alpha" / "report.md", (
        "---\ntitle: Alpha\nstatus: active\nsummary: The first report, error {{ scores['rmse'] }}.\n"
        "inputs:\n  descent: descent@latest\n---\n\n"
        "```python exec hide\nscores = read_scalars(inputs.descent)\n```\n\n"
        "## Result\n\n!!! finding promising \"It works with 3 numbers in 1.5 s\"\n    RMSE {{ scores['rmse'] }}.\n\n"
        "![Histogram](input:descent/figures/hist.png)\n![Strip](assets/strip.svg)\n\nMore in [beta](report:beta#detail).\n"))
    write(reports / "beta" / "report.md", "---\ntitle: Beta\n---\n\n## Detail\n\nBack to [alpha](report:alpha#result).\n")
    return reports


def make_run(project: Path, status: str = "completed") -> Path:
    run = project / "outputs" / RUN
    write(run / ".hydra" / "config.yaml", "seed: 0\n")
    log = Writer(run)
    log.add({"rmse": 0.5})
    log.add_figure("hist", histogram, None, [1, 2, 2, 3])
    (run / "run.json").write_text(json.dumps({"status": status}))
    if not (run.parent / "latest").is_symlink():
        (run.parent / "latest").symlink_to(run.name)
    log.close()
    return run


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    """Every test starts outside any project, with no inherited repo or token settings."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("CAIRN_DIR", raising=False)
    monkeypatch.delenv("CAIRN_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # ~/.config/cairn/token is a place a client looks for its token
    monkeypatch.chdir(tmp_path)  # no project root above it: tests that need one make it (the `project` fixture)
    monkeypatch.delenv("CAIRN_RUN", raising=False)
    from cairn.client import drafts, repo, runs
    from cairn.store import inputs

    repo._DOWN.clear()  # a remote one test found down is not down for the next

    monkeypatch.setattr(runs, "CAPTURE_DEFAULT", False)  # the runs pytest itself starts leave its output alone
    monkeypatch.setattr(runs, "SYSTEM_INTERVAL", 3600.0)
    inputs._SINKS.clear()
    drafts._ACTIVE.clear()
    yield
    runs._reset()  # this test's run (started by a pull or a draft) is not the next test's
    drafts._ACTIVE.clear()
    inputs._SINKS.clear()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose repo (no remote) holds `descent`, two reports, and a run under outputs/."""
    monkeypatch.delenv("CAIRN_INPUTS", raising=False)
    folder = tmp_path / "study"
    folder.mkdir()
    (folder / "cairn.toml").write_text("")  # what makes the folder a project
    (folder / "pyproject.toml").write_text("")  # a project file the report tests read
    monkeypatch.chdir(folder)
    make_tree(folder)
    make_run(folder)
    return folder
