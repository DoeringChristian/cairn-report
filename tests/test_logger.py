import json
import math
import shutil

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytest

from cairn_report.build import build_all
from cairn_report.config import load_config
from cairn_report.logger import RunLogger, read_artifacts, read_jsonl, read_scalars


def histogram(values):
    figure, axis = plt.subplots()
    axis.hist(values)
    return figure


def test_round_trip(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    run = tmp_path / "outputs" / "demo" / "t0"
    log = RunLogger(run)
    log.scalar("rmse", 0.5)
    log.scalar("rmse", 0.25)
    log.scalar("loss", 1.0, step=3)
    log.scalar("bad", math.nan)
    log.table("cases", [{"case": "a", "error": 1}, {"case": "b", "error": 2}])
    log.figure("hist", histogram, [1, 2, 2, 3])
    assert read_scalars(run) == {"rmse": 0.25, "loss": 1.0, "bad": None}
    artifacts = read_artifacts(run)
    assert set(artifacts) == {"tables/cases.csv", "figures/hist.png"}
    assert artifacts["figures/hist.png"]["producer"]["function"] == "histogram"
    assert artifacts["tables/cases.csv"]["origin"]["function"] == "test_round_trip"
    with (run / "metrics.jsonl").open("a") as handle:
        handle.write('{"name": "torn"')  # crash mid-write
    assert len(read_jsonl(run / "metrics.jsonl")) == 4


def test_rejects_bad_names_and_values(tmp_path):
    log = RunLogger(tmp_path / "run")
    with pytest.raises(ValueError):
        log.scalar("Bad/Name", 1)
    with pytest.raises(TypeError):
        log.scalar("flag", True)


def test_report_reads_logged_run(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    run = tmp_path / "outputs" / "demo" / "2026-01-01_00-00-00"
    RunLogger(run).scalar("rmse", 0.125)
    (run / "run.json").write_text(json.dumps({"status": "completed"}))
    (run.parent / "latest").symlink_to(run.name)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "report.md").write_text(
        '---\ntitle: T\n---\n\n```python exec hide\nscores = read_scalars(run_dir("demo"))\n```\n\nLogged error {{ scores["rmse"] }}.\n')
    build_all(load_config(tmp_path))
    out = tmp_path / "generated" / "reports"
    assert ">0.125</span>" in (out / "narrative.html").read_text()
    manifest = json.loads((out / "manifest.json").read_text())
    assert "outputs/demo/2026-01-01_00-00-00/metrics.jsonl" in manifest["sources"]


def test_origin_capture_is_cheap(tmp_path):
    import time

    log = RunLogger(tmp_path / "run", backends=["local"])
    start = time.perf_counter()
    for step in range(2000):
        log.scalar("loss", 1.0 / (step + 1), step=step)
    per_call = (time.perf_counter() - start) / 2000
    assert per_call < 0.001, f"{per_call * 1e6:.0f} µs per scalar"
    assert read_jsonl(tmp_path / "run" / "metrics.jsonl")[0]["origin"]["function"] == "test_origin_capture_is_cheap"
