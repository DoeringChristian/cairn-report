import json
import math
import shutil

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytest

from cairn.reports.build import build_all
from cairn.config import load_config
from cairn.client.logger import Writer
from cairn.formats import read_artifacts, read_jsonl, read_scalars


def histogram(values):
    figure, axis = plt.subplots()
    axis.hist(values)
    return figure


def test_round_trip(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    run = tmp_path / "outputs" / "demo" / "t0"
    log = Writer(run)
    log.add({"rmse": 0.5})
    log.add({"rmse": 0.25})
    log.add({"loss": 1.0, "step": 3})
    log.add({"bad": math.nan})
    log.add_table("cases", [{"case": "a", "error": 1}, {"case": "b", "error": 2}])
    log.add_figure("hist", histogram, None, [1, 2, 2, 3])
    assert read_scalars(run) == {"rmse": 0.25, "loss": 1.0, "bad": None}
    artifacts = read_artifacts(run)
    assert set(artifacts) == {"tables/cases.csv", "figures/hist.png"}
    assert artifacts["figures/hist.png"]["producer"]["function"] == "histogram"
    assert artifacts["tables/cases.csv"]["origin"]["function"] == "test_round_trip"
    with (run / "metrics.jsonl").open("a") as handle:
        handle.write('{"name": "torn"')  # crash mid-write
    assert len(read_jsonl(run / "metrics.jsonl")) == 4


def test_rejects_bad_names_and_values(tmp_path):
    log = Writer(tmp_path / "run")
    with pytest.raises(ValueError):
        log.add_text("Bad/Name", "x")
    with pytest.raises(TypeError):
        log.add({1: 2})
    with pytest.raises(TypeError):
        log.add({})


def test_report_reads_logged_run(tmp_path, monkeypatch):
    import cairn as store

    monkeypatch.setenv("CAIRN_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    run = tmp_path / "outputs" / "demo" / "2026-01-01_00-00-00"
    Writer(run).add({"rmse": 0.125})
    store.publish("demo", run)
    (tmp_path / "reports" / "main").mkdir(parents=True)
    (tmp_path / "reports" / "main" / "report.md").write_text(
        '---\ntitle: T\ninputs:\n  demo: demo\n---\n\n```python exec hide\nscores = read_scalars(inputs.demo)\n```\n\nLogged error {{ scores["rmse"] }}.\n')
    build_all(load_config(tmp_path))
    out = tmp_path / ".cairn" / "build" / "main"
    assert ">0.125</span>" in (out / "narrative.html").read_text()
    manifest = json.loads((out / "manifest.json").read_text())
    assert "input:demo/metrics.jsonl" in manifest["sources"] and manifest["inputs"]["demo"]["resolved"] == f"{tmp_path.name}:demo@v1"


def test_origin_capture_is_cheap(tmp_path):
    import time

    log = Writer(tmp_path / "run")
    start = time.perf_counter()
    for step in range(2000):
        log.add({"loss": 1.0 / (step + 1), "step": step})
    per_call = (time.perf_counter() - start) / 2000
    assert per_call < 0.001, f"{per_call * 1e6:.0f} µs per scalar"
    assert read_jsonl(tmp_path / "run" / "metrics.jsonl")[0]["origin"]["function"] == "test_origin_capture_is_cheap"


# ------------------------------------------------------------------ the writer API (add, add_*)


def test_add_and_add_star_with_metadata(tmp_path):
    from cairn.formats import read_series

    (tmp_path / "pyproject.toml").write_text("")
    log = Writer(tmp_path / "run")
    log.add({"loss": 0.5, "psnr": 20.0, "step": 0})
    log.add({"loss": math.inf, "lr": 1e-3, "step": 1, "nested": {"a": [1, 2]}})
    log.add({"note": "warmup done"})
    rows = read_jsonl(tmp_path / "run" / "metrics.jsonl")
    assert rows[0]["loss"] == 0.5 and rows[0]["psnr"] == 20.0 and rows[0]["step"] == 0 and "time" in rows[0]
    assert rows[0]["origin"]["function"] == "test_add_and_add_star_with_metadata"
    assert rows[1]["loss"] is None and rows[1]["nonfinite"] == {"loss": "inf"}
    assert read_scalars(tmp_path / "run") == {"loss": None, "psnr": 20.0, "lr": 1e-3, "nested": {"a": [1, 2]}, "note": "warmup done"}
    assert [(row["step"], row["value"]) for row in read_series(tmp_path / "run", "loss")] == [(0, 0.5), (1, None)]
    with pytest.raises(TypeError):
        log.add({})
    with pytest.raises(ValueError):
        log.add({"origin": 1})

    log.add_figure("hist", histogram, {"step": 3, "loss": 0.25}, [1, 2, 2, 3])
    log.add_table("cases", [{"case": "a", "error": 1}], {"split": "val"})
    log.add_json("final", {"loss": 0.1}, {"best": True})
    log.add_text("notes", "plain text", {"kind": "summary"})
    log.add_plotly("curve", {"data": [{"y": [1, 2]}]}, {"step": 3})
    (tmp_path / "model.pt").write_bytes(b"weights")
    log.add_file(tmp_path / "model.pt", metadata={"val_loss": 0.2})
    log.add_file(b"other", "other.bin")
    (tmp_path / "ckpt" / "sub").mkdir(parents=True)
    (tmp_path / "ckpt" / "sub" / "w.bin").write_bytes(b"w")
    log.add_dir(tmp_path / "ckpt", metadata={"epoch": 2})
    log.add_image("pixels", [[0.0, 1.0], [1.0, 0.0]], {"step": 3})
    entries = read_artifacts(tmp_path / "run")
    assert set(entries) == {"figures/hist.png", "tables/cases.csv", "data/final.json", "text/notes.txt", "figures/curve.plotly.json",
                            "files/model.pt", "files/other.bin", "files/ckpt", "images/pixels.png"}
    assert entries["figures/hist.png"]["metadata"] == {"step": 3, "loss": 0.25}
    assert entries["figures/hist.png"]["producer"]["function"] == "histogram"
    assert entries["files/model.pt"]["metadata"] == {"val_loss": 0.2} and entries["files/ckpt"]["kind"] == "dir"
    assert "metadata" not in entries["files/other.bin"]
    assert (tmp_path / "run" / "files" / "ckpt" / "sub" / "w.bin").read_bytes() == b"w"
    assert (tmp_path / "run" / "text" / "notes.txt").read_text() == "plain text"
    with pytest.raises(TypeError):
        log.add_json("bad", 1, metadata=["not", "a", "dict"])
    from cairn.client.drafts import Draft

    assert not any(hasattr(Draft, old) for old in ("scalar", "scalars", "figure", "plotly", "table", "image", "video", "artifact", "text", "use"))


def test_report_readers_take_cairn_report_lines_and_multi_key_entries(tmp_path):
    from cairn.formats import fold_scalars, fold_series

    old = [{"name": "loss", "value": 1.0, "step": 0, "time": "t", "origin": {}},
           {"name": "bad", "value": None, "step": None, "time": "t", "origin": {}, "nonfinite": "nan"}]
    new = [{"loss": 0.5, "psnr": 30.0, "step": 1, "time": "t", "origin": {}}, {"loss": 0.25, "step": 2, "time": "t", "origin": {}}]
    assert fold_scalars(old + new) == {"loss": 0.25, "bad": None, "psnr": 30.0}
    assert [(row["step"], row["value"]) for row in fold_series(old + new, "loss")] == [(0, 1.0), (1, 0.5), (2, 0.25)]
    assert fold_series(old + new, "psnr") == [{"value": 30.0, "step": 1, "time": "t"}]
