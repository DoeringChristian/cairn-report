import json
import threading
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytest

from cairn_report.hub import make_hub
from cairn_report.logger import RunLogger

TOKEN = "t" * 40
RUN = "demo/2026-01-01_00-00-00"


def histogram(values):
    figure, axis = plt.subplots(figsize=(4, 2))
    axis.hist(values)
    return figure


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_tree(project: Path) -> Path:
    """A root report, a child with a figure from a run and an asset, and a grandchild."""
    reports = project / "reports"
    write(reports / "report.md", "---\ntitle: Root\n---\n\n## Start\n\nSee [the result](report:alpha#finding-it-works-with-3-numbers-in-1-5-s).\n\n```reports\n```\n")
    write(reports / "alpha" / "assets" / "strip.svg",
          '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="100"><rect width="400" height="100" fill="#dcebe5"/></svg>')
    write(reports / "alpha" / "report.md", (
        "---\ntitle: Alpha\nstatus: active\nsummary: The first child, error {{ scores['rmse'] }}.\n---\n\n"
        "```python exec hide\nscores = read_scalars(run_dir('demo'))\n```\n\n"
        "## Result\n\n!!! finding promising \"It works with 3 numbers in 1.5 s\"\n    RMSE {{ scores['rmse'] }}.\n\n"
        "![Histogram](run:demo/figures/hist.png)\n![Strip](assets/strip.svg)\n\nBack to [the root](report:/#start).\n"))
    write(reports / "alpha" / "beta" / "report.md", "---\ntitle: Beta\n---\n\n## Detail\n\nUp to [alpha](report:alpha#result).\n")
    return reports


def make_run(project: Path, status: str = "completed") -> Path:
    run = project / "outputs" / RUN
    write(run / ".hydra" / "config.yaml", "seed: 0\n")
    log = RunLogger(run)
    log.scalar("rmse", 0.5)
    log.figure("hist", histogram, [1, 2, 2, 3])
    (run / "run.json").write_text(json.dumps({"status": status}))
    if not (run.parent / "latest").is_symlink():
        (run.parent / "latest").symlink_to(run.name)
    log.close()
    return run


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "study"
    folder.mkdir()
    (folder / "pyproject.toml").write_text("")
    make_tree(folder)
    make_run(folder)
    return folder


@pytest.fixture
def hub(tmp_path):
    root = tmp_path / "hub"
    root.mkdir()
    server = make_hub(root, port=0, token=TOKEN, secure_cookie=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{server.server_address[1]}", "root": root, "port": server.server_address[1]}
    server.shutdown()
