import html
import json
import re
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytest

pytest.importorskip("mlflow")
from mlflow.tracking import MlflowClient

from cairn_report.build import build_all
from cairn_report.config import load_config
from cairn_report.logger import RunLogger

RUN = "outputs/demo/2026-01-01_00-00-00"


def histogram(values):
    figure, axis = plt.subplots()
    axis.hist(values)
    return figure


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    uri = f"file://{tmp_path / 'mlruns'}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    return MlflowClient(tracking_uri=uri)


def make_project(tmp_path, backends, body):
    project = tmp_path / "study"
    (project / "reports").mkdir(parents=True)
    (project / "pyproject.toml").write_text("")
    (project / "cairn-report.toml").write_text(f"[logger]\nbackends = {json.dumps(backends)}\n")
    (project / "reports" / "report.md").write_text(f"---\ntitle: Study\n---\n\n## Results\n\n{body}\n")
    return project


def run_experiment(project, status="completed"):
    run = project / RUN
    (run / ".hydra").mkdir(parents=True)
    (run / ".hydra" / "config.yaml").write_text("seed: 3\nmodel:\n  degree: 12\n")
    log = RunLogger(run)
    log.scalar("rmse", 0.125)
    log.scalar("loss", 1.0, step=1)
    log.table("cases", [{"case": "a", "error": 1}])
    log.figure("hist", histogram, [1, 2, 2, 3])
    (run / "run.json").write_text(json.dumps({"status": status, "code_revision": "abc123"}))
    (run.parent / "latest").symlink_to(run.name)
    log.close()
    return run


def figure_traces(project):
    narrative = (project / "generated" / "reports" / "overview" / "narrative.html").read_text()
    return [json.loads(html.unescape(trace)) for trace in re.findall(r'data-trace="([^"]*)"', narrative)], narrative


def mlflow_runs(store):
    experiment = store.get_experiment_by_name("study")
    return store.search_runs([experiment.experiment_id])


def test_mlflow_only_backend(tmp_path, store):
    project = make_project(tmp_path, ["mlflow"], '![Error histogram](run:demo/figures/hist.png)\n\n'
                           '```python exec hide\nscores = read_scalars(run_dir("demo"))\n```\n\nRMSE {{ scores["rmse"] }}.')
    run = run_experiment(project)
    assert not (run / "metrics.jsonl").exists() and not (run / "figures").exists()  # evidence lives in MLflow only
    (logged,) = mlflow_runs(store)
    assert logged.info.status == "FINISHED"
    assert logged.data.metrics["rmse"] == 0.125
    assert logged.data.params["model.degree"] == "12"
    assert logged.data.tags["cairn.run_dir"] == RUN

    build_all(load_config(project))
    traces, narrative = figure_traces(project)
    (trace,) = traces
    assert trace["run"] == RUN and trace["path"] == "figures/hist.png"
    assert trace["producer"]["function"] == "histogram"
    assert trace["revision"] == "abc123"
    assert trace["mlflow"]["run_id"] == logged.info.run_id
    assert f"Source · {RUN}/figures/hist.png" in narrative
    assert ">0.125</span>" in narrative
    manifest = json.loads((project / "generated" / "reports" / "overview" / "manifest.json").read_text())
    assert manifest["mlflow_runs"][0]["run_id"] == logged.info.run_id
    assert any(source.startswith("generated/mlflow/") for source in manifest["sources"])


def test_both_backends_link_local_figures_to_mlflow(tmp_path, store):
    project = make_project(tmp_path, ["local", "mlflow"], "![Error histogram](run:demo/figures/hist.png)")
    run = run_experiment(project)
    assert (run / "figures" / "hist.png").is_file() and (run / "metrics.jsonl").is_file()
    (logged,) = mlflow_runs(store)
    build_all(load_config(project))
    (trace,), _ = figure_traces(project)
    assert trace["run"] == RUN  # read from the local run directory
    assert trace["mlflow"]["run_id"] == logged.info.run_id  # and linked to its MLflow copy
    assert not (project / "generated" / "mlflow").exists()


def test_plain_mlflow_run_by_id(tmp_path, store):
    experiment = store.create_experiment("study")
    run = store.create_run(experiment)
    store.log_metric(run.info.run_id, "accuracy", 0.9, step=0)
    image = Path(tempfile.mkdtemp()) / "curve.png"
    figure = histogram([1, 2])
    figure.savefig(image)
    plt.close(figure)
    store.log_artifact(run.info.run_id, str(image), "plots")
    store.set_terminated(run.info.run_id, "FINISHED")
    project = make_project(tmp_path, ["mlflow"], f"![Curve](mlflow:{run.info.run_id}/plots/curve.png)\n\n"
                           f'```python exec hide\nacc = read_scalars(mlflow_run(run_id="{run.info.run_id}"))\n```\n\n'
                           'Accuracy {{ acc["accuracy"] }}.')
    build_all(load_config(project))
    (trace,), narrative = figure_traces(project)
    assert trace["mlflow"]["run_id"] == run.info.run_id
    assert ">0.9</span>" in narrative


def test_failed_run_is_not_evidence(tmp_path, store):
    project = make_project(tmp_path, ["mlflow"], "![Error histogram](run:demo/figures/hist.png)")
    run_experiment(project, status="failed")
    (logged,) = mlflow_runs(store)
    assert logged.info.status == "FAILED"
    with pytest.raises(ValueError):
        build_all(load_config(project))


def test_http_tracking_server_gets_ui_link():
    from cairn_report.build import mlflow_link

    link = mlflow_link({"run_id": "r1", "experiment_id": "7", "tracking_uri": "https://mlflow.example.org/"}, "figures/a.png")
    assert link["url"] == "https://mlflow.example.org/#/experiments/7/runs/r1/artifacts"
    assert "url" not in mlflow_link({"run_id": "r1", "experiment_id": "7", "tracking_uri": "file:///x"}, "a.png")
