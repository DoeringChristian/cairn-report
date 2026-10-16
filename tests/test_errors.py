"""Cairn's own errors are raised from the public call the caller made (cairn/api.py): the traceback shows the caller's
frames, then exactly one Cairn frame (the function or method called), then a message that says what was looked for
and where. Any other exception (a bug) keeps its full traceback."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import write_config


def run_script(folder: Path, code: str, **env) -> subprocess.CompletedProcess:
    script = folder / "script.py"
    script.write_text(textwrap.dedent(code))
    environment = {**os.environ, **env}
    environment.pop("CAIRN_DEBUG", None)
    return subprocess.run([sys.executable, str(script)], cwd=folder, capture_output=True, text=True, timeout=120, env=environment)


@pytest.fixture
def study(tmp_path, monkeypatch):
    folder = tmp_path / "study"
    folder.mkdir()
    write_config(folder, "")
    (folder / "data").mkdir()
    (folder / "data" / "a.txt").write_text("a")
    monkeypatch.chdir(folder)
    return folder


def frames(traceback: str) -> list[str]:
    return [line.strip() for line in traceback.splitlines() if line.strip().startswith("File ")]


def test_an_unknown_artifact_raises_from_cairn_use(study):
    result = run_script(study, """
        import cairn
        cairn.use("nope@latest")
        """)
    assert result.returncode == 1
    error = result.stderr
    listed = frames(error)
    assert any("script.py" in line and "<module>" in line for line in listed), error
    cairn_frames = [line for line in listed if "script.py" not in line]
    assert len(cairn_frames) == 1 and cairn_frames[0].endswith("in cairn.use"), error
    assert "cairn/client" not in error and "cairn/store" not in error
    assert "During handling" not in error and "direct cause" not in error
    assert f"NotFound: no artifact nope in the repo {study / '.cairn'} (found from the working directory)" in error
    assert "cairn: run failed; folder kept (cairn drafts)" in error  # the run's end: one line


def test_a_method_raises_from_itself(study):
    result = run_script(study, """
        import cairn
        repo = cairn.connect()
        repo.artifact("data").publish("data")
        artifact = repo.artifact("missing")
        artifact.latest.files
        """)
    error = result.stderr
    cairn_frames = [line for line in frames(error) if "script.py" not in line]
    assert len(cairn_frames) == 1 and cairn_frames[0].endswith("in Version.files"), error
    assert "cairn/client" not in error and "cairn/store" not in error
    assert "NotFound: no artifact missing in the repo" in error


def test_an_ambiguous_or_unknown_short_id_says_so_from_the_call(study):
    result = run_script(study, """
        import cairn
        cairn.connect().version("ffff9999")
        """)
    error = result.stderr
    cairn_frames = [line for line in frames(error) if "script.py" not in line]
    assert len(cairn_frames) == 1 and cairn_frames[0].endswith("in Repo.version"), error
    assert "NotFound: no version with the id ffff9999 in the repo" in error


def test_an_internal_bug_keeps_its_full_traceback(study):
    result = run_script(study, """
        import cairn
        import cairn.client.objects as objects

        def broken(self, *args, **kwargs):
            raise ValueError("a bug")

        objects.Repo.artifacts.__wrapped__  # the public wrapper keeps the original
        objects.ProjectRepo.stage = broken
        cairn.connect().artifact("data").publish("data")
        """)
    error = result.stderr
    assert "ValueError: a bug" in error
    assert any("cairn/client/objects.py" in line for line in frames(error)), error  # the inner frames are kept
    assert any("in broken" in line for line in frames(error)), error


def test_cairn_debug_shows_every_frame(study):
    result = run_script(study, """
        import os
        os.environ["CAIRN_DEBUG"] = "1"
        import cairn
        cairn.use("nope@latest")
        """)
    assert "cairn/store/local.py" in result.stderr


def test_no_run_raises_from_cairn_run(study):
    result = run_script(study, """
        import cairn
        cairn.run.add({"loss": 1.0})
        """)
    error = result.stderr
    cairn_frames = [line for line in frames(error) if "script.py" not in line]
    assert len(cairn_frames) == 1 and cairn_frames[0].endswith("in cairn.run"), error
    assert "NoRun: no run in this process: call cairn.init() first" in error
