import sys
import textwrap

import pytest

import cairn as store
from cairn.cli import main

from test_store import env, folder  # noqa: F401  (the project fixture and folder helper)


def run(capsys, *argv):
    main(list(argv))
    return capsys.readouterr().out


def test_publish_folder_records_label_alias_and_meta(env, capsys):
    data = folder(env, "data", {"a.txt": "one", "sub/b.txt": "two"})
    out = run(capsys, "publish", str(data), "crops", "--label", "first", "--alias", "paper", "--alias", "keep",
              "--meta", "psnr=31.2", "--meta", "note=hello")
    assert out.startswith("published crops@v1 (2 files, ")
    [version] = store.versions("crops")
    assert version["label"] == "first" and version["metadata"] == {"psnr": 31.2, "note": "hello"}
    assert store.pull("crops@paper") == store.pull("crops@keep") == store.pull("crops@first")


def test_unchanged_republish_creates_no_version(env, capsys):
    data = folder(env, "data", {"a.txt": "one"})
    run(capsys, "publish", str(data), "crops")
    assert run(capsys, "publish", str(data), "crops").strip() == "crops@v1 unchanged"
    (data / "a.txt").write_text("changed")
    assert run(capsys, "publish", str(data), "crops").startswith("published crops@v2")
    assert [v["version"] for v in store.versions("crops")] == [1, 2]


def test_symlinked_folder_is_followed_and_labelled_by_target(env, capsys):
    folder(env, "runs/2026-10-15_07-49-37", {"metrics.csv": "x"})
    (env / "runs" / "latest").symlink_to("2026-10-15_07-49-37")
    run(capsys, "publish", "runs/latest", "descent")
    [version] = store.versions("descent")
    assert version["label"] == "2026-10-15_07-49-37"
    assert [entry["path"] for entry in version["files"]] == ["metrics.csv"]


def test_mappings_from_config(env, capsys):
    folder(env, "runs/a", {"a.txt": "1"})
    (env / "cairn.toml").write_text(
        '[store]\npublish = [{ folder = "runs/a", name = "alpha" }, { folder = "runs/missing", name = "beta" }]\n')
    out = run(capsys, "publish")
    assert "published alpha@v1" in out and "skipping beta" in out
    assert run(capsys, "publish").count("unchanged") == 1
    assert store.versions("beta") == []


def test_no_arguments_and_no_mappings_is_an_error(env):
    with pytest.raises(SystemExit, match="nothing to publish"):
        main(["publish"])


def test_old_logger_table_is_refused(env):
    (env / "cairn.toml").write_text('[logger]\nbackends = ["mlflow"]\n')
    with pytest.raises(ValueError, match=r"\[logger\] was removed"):
        main(["publish"])


WRITER = textwrap.dedent("""
    import sys, time, pathlib
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "first.txt").write_text("first")
    (out / "log.txt").write_text("early")
    time.sleep(1.2)
    (out / "second.txt").write_text("second")
    (out / "log.txt").write_text("final")
    time.sleep(0.8)
    sys.exit(int(sys.argv[2]))
""")


def watch(capsys, target, code):
    (target.parent / "writer.py").write_text(WRITER)
    with pytest.raises(SystemExit) as exit:
        main(["publish", str(target), "sweep", "--interval", "0.5", "--",
              sys.executable, str(target.parent / "writer.py"), str(target), str(code)])
    return exit.value.code, capsys.readouterr()


def test_watch_command_makes_one_completed_version(env, capsys):
    code, output = watch(capsys, env / "out", 0)
    assert code == 0 and "published sweep@v1 (3 files" in output.out
    [version] = store.versions("sweep")
    assert version["status"] == "completed" and [e["path"] for e in version["files"]] == ["first.txt", "log.txt", "second.txt"]
    assert "synced" in output.out  # synced while the command still ran
    root = store.pull("sweep@latest")
    assert (root / "log.txt").read_text() == "final" and (root / "second.txt").read_text() == "second"


def test_failing_command_marks_version_failed_and_keeps_latest(env, capsys):
    folder(env, "earlier", {"ok.txt": "good"})
    main(["publish", "earlier", "sweep"])
    capsys.readouterr()
    code, output = watch(capsys, env / "out", 3)
    assert code == 3 and "failed" in output.err
    first, second = store.versions("sweep")
    assert (first["status"], second["status"]) == ("completed", "failed")
    assert (store.pull("sweep@latest") / "ok.txt").read_text() == "good"  # latest did not move


def test_inputs_pulled_by_the_command_are_recorded(env, capsys, monkeypatch):
    data = folder(env, "data", {"x.txt": "1"})
    main(["publish", str(data), "datasets/toy"])
    capsys.readouterr()
    out = env / "out"
    monkeypatch.delenv("CAIRN_INPUTS", raising=False)  # publish --watch points the command at the folder itself
    script = (f"import cairn as store; import pathlib; pathlib.Path({str(out)!r}).mkdir(exist_ok=True); "
              "store.pull('datasets/toy@v1'); (pathlib.Path(" + repr(str(out)) + ") / 'r.txt').write_text('r')")
    with pytest.raises(SystemExit) as exit:
        main(["publish", str(out), "fit", "--interval", "0.5", "--", sys.executable, "-c", script])
    assert exit.value.code == 0
    [version] = store.versions("fit")
    assert version["inputs"] == [f"{store.open_store().project}:datasets/toy@v1"]
    assert [e["path"] for e in version["files"]] == ["r.txt"]


def test_publication_context_manager(env):
    out = folder(env, "out", {"a.txt": "1"})
    with store.Publication("job", out) as pub:
        assert pub.sync() == 1 and pub.sync() == 0
        (out / "a.txt").write_text("2")
        assert pub.sync() == 1
    assert store.versions("job")[0]["status"] == "completed"
    with pytest.raises(RuntimeError):
        with store.Publication("job", out):
            raise RuntimeError
    assert [v["status"] for v in store.versions("job")] == ["completed", "failed"]
