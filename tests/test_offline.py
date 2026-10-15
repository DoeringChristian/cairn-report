"""Working while the store server is down: the outbox, `cairn sync` and `cairn status`, `--watch`, and offline pulls."""

import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

import cairn
from cairn.cli import main
from cairn.store.uids import UID

from test_identity import Server
from test_store import env, folder  # noqa: F401


@pytest.fixture
def down(env, tmp_path, monkeypatch):
    """A configured store server that is not running yet (start it with `down.start()`)."""
    server = Server(tmp_path / "store-root")
    monkeypatch.setenv("CAIRN_STORE_TOKEN", server.token)
    (env / "cairn.toml").write_text(f'[store]\nurl = "{server.url}"\nproject = "study"\n')
    import cairn.client.outbox as outbox

    monkeypatch.setattr(outbox, "start_uploader", lambda *args, **kwargs: None)  # tests flush by hand
    yield server
    server.stop()


def run(capsys, *argv, code=None):
    if code is None:
        main(list(argv))
    else:
        with pytest.raises(SystemExit) as raised:
            main(list(argv))
        assert raised.value.code == code
    captured = capsys.readouterr()
    return captured.out, captured.err


# -------------------------------------------------------------------- outbox


def test_publish_while_down_is_queued_and_synced_with_its_uid(down, env, capsys):
    data = folder(env, "d", {"a.txt": "as published", "sub/b.txt": "2"})
    version = cairn.connect().project().artifact("data").publish(data, metadata={"n": 1}, aliases=["paper"])
    assert version.number is None and version.status == "pending" and UID.fullmatch(version.uid)
    assert version.ref == f"study:data@{version.uid}" and "queued" in capsys.readouterr().err
    entry = json.loads(next((Path(env.parent / "cache" / "cairn" / "outbox")).glob("*/*.json")).read_text())
    assert entry["uid"] == version.uid and entry["state"] == "pending" and entry["aliases"] == ["paper"]

    (data / "a.txt").write_text("changed after publishing")  # the queued version keeps what was published
    out, _ = run(capsys, "status")
    assert "pending\tstudy:data\t-\t" + version.uid in out
    assert cairn.publish("data", data) is None  # a second one, queued behind the first
    capsys.readouterr()

    down.start()
    out, _ = run(capsys, "sync")
    assert out.count("uploaded study:data@v") == 2 and version.uid in out
    first, second = down.versions()
    assert first["uid"] == version.uid and first["metadata"] == {"n": 1} and first["status"] == "completed"
    assert cairn.LocalStore(down.root).aliases("study", "data")["paper"] == 1
    assert (cairn.pull(f"data@{version.uid}//a.txt", record=False)).read_text() == "as published"
    assert (cairn.pull("data@v2//a.txt", record=False)).read_text() == "changed after publishing"
    assert run(capsys, "sync")[0].strip() == "nothing to sync"  # a second sync is a no-op
    assert "nothing queued" in run(capsys, "status")[0]
    assert len(down.versions()) == 2


def test_resending_an_entry_creates_no_second_version(down, env, capsys):
    """A crash after the server's answer leaves the entry behind: sending it again must be harmless."""
    store = cairn.connect()
    version = store.artifact("data").publish(folder(env, "d", {"a.txt": "1"}))
    [file] = list((env.parent / "cache" / "cairn" / "outbox").glob("*/*.json"))
    saved = file.read_text()
    down.start()
    assert [entry["uid"] for entry in store.sync()["sent"]] == [version.uid]
    assert not [blob for blob in (env.parent / "cache" / "cairn" / "blobs").rglob("*") if blob.is_file()]  # staged blobs dropped
    file.write_text(saved)
    assert len(store.sync()["sent"]) == 1
    assert [v["uid"] for v in down.versions()] == [version.uid]
    assert version.refresh().number == 1 and version.ref == "study:data@v1"


def test_any_cli_command_flushes_the_outbox_first(down, env, capsys):
    cairn.publish("data", folder(env, "d", {"a.txt": "1"}))
    capsys.readouterr()
    down.start()
    out, err = run(capsys, "versions", "data")
    assert "uploaded study:data@v1" in err and out.startswith("v1\tcompleted")


def test_background_uploader_sends_when_the_server_answers(env, tmp_path, monkeypatch, capsys):
    server = Server(tmp_path / "store-root")
    monkeypatch.setenv("CAIRN_STORE_TOKEN", server.token)
    (env / "cairn.toml").write_text(f'[store]\nurl = "{server.url}"\nproject = "study"\n')
    version = cairn.connect().artifact("data").publish(folder(env, "d", {"a.txt": "1"}))
    assert version.number is None
    server.start()
    try:
        for _ in range(100):
            if server.versions():
                break
            time.sleep(0.1)
        assert [v["uid"] for v in server.versions()] == [version.uid]
    finally:
        server.stop()


def test_rejected_entries_are_kept_for_status_and_skipped(down, env, capsys):
    store = cairn.connect()
    store.artifact("data").publish(folder(env, "d", {"a.txt": "1"}), inputs=["study:missing@v1"])  # the server refuses
    capsys.readouterr()
    down.start()
    out, _ = run(capsys, "sync", code=1)
    assert "rejected study:data@" in out
    out, _ = run(capsys, "status")
    assert out.startswith("rejected\tstudy:data") and "missing" in out
    store.artifact("data").publish(folder(env, "e", {"a.txt": "2"}))
    assert down.versions()[0]["status"] == "completed"  # a rejected entry does not hold up the next


# ------------------------------------------------------------ publish --watch


SCRIPT = ("import pathlib, sys, time; out = pathlib.Path(sys.argv[1]); out.mkdir(exist_ok=True); "
          "(out / 'first.txt').write_text('1'); time.sleep(float(sys.argv[2])); (out / 'second.txt').write_text('2'); time.sleep(0.3)")


def test_watch_starts_while_down_and_ends_on_the_server_once_it_answers(down, env, capsys):
    out = env / "out"
    threading.Timer(1.0, down.start).start()  # the server comes up while the job runs
    stdout, _ = run(capsys, "publish", str(out), "sweep", "--interval", "0.3", "--", sys.executable, "-c", SCRIPT, str(out), "2.5", code=0)
    assert "in the outbox" in stdout and "published sweep@v1 (2 files" in stdout
    [version] = down.versions("sweep")
    assert version["status"] == "completed" and [f["path"] for f in version["files"]] == ["first.txt", "second.txt"]
    assert "nothing queued" in run(capsys, "status")[0]


def test_watch_while_down_throughout_queues_the_completion(down, env, capsys):
    out = env / "out"
    stdout, _ = run(capsys, "publish", str(out), "sweep", "--interval", "0.3", "--", sys.executable, "-c", SCRIPT, str(out), "0.2", code=0)
    assert "queued sweep@" in stdout
    status = run(capsys, "status")[0]
    assert status.startswith("completed\tstudy:sweep\t-\t")
    down.start()
    assert "uploaded study:sweep@v1" in run(capsys, "sync")[0]
    [version] = down.versions("sweep")
    assert version["status"] == "completed" and version["uid"] in status and len(version["files"]) == 2


def test_watch_failure_while_down_is_queued_as_failed(down, env, capsys):
    out = env / "out"
    run(capsys, "publish", str(out), "sweep", "--interval", "0.3", "--", sys.executable, "-c",
        "import pathlib, sys; p = pathlib.Path(sys.argv[1]); p.mkdir(); (p / 'x.txt').write_text('x'); sys.exit(3)", str(out), code=3)
    down.start()
    run(capsys, "sync")
    assert [v["status"] for v in down.versions("sweep")] == ["failed"]


def test_a_dead_watch_job_is_failed_not_left_running(down, env, capsys):
    from cairn.client.outbox import Outbox

    store = cairn.connect()
    outbox = Outbox(store.backend)
    entry = outbox.add("running", "study", "sweep", label="job")
    entry["owner"]["pid"] = 2**22 + 12345  # a process that no longer exists
    outbox.save(entry)
    down.start()
    run(capsys, "sync")
    assert [(v["status"], v["uid"]) for v in down.versions("sweep")] == [("failed", entry["uid"])]


# ------------------------------------------------------------- offline pulls


def test_offline_pulls_of_exact_and_alias_refs(down, env, capsys, monkeypatch):
    down.start()
    store = cairn.connect()
    store.artifact("data").publish(folder(env, "d", {"a.txt": "one"}), aliases=["best"])
    v1 = store.artifact("data").version(1)
    assert (cairn.pull("data@best//a.txt", record=False)).read_text() == "one"
    before = len(down.requests)
    assert (cairn.pull("data@v1", record=False) / "a.txt").read_text() == "one"
    assert len(down.requests) == before  # an exact version in the cache needs no server
    down.stop()

    inputs = env / "inputs.jsonl"
    monkeypatch.setenv("CAIRN_INPUTS", str(inputs))
    assert (cairn.pull(f"data@{v1.uid}//a.txt")).read_text() == "one"
    assert (cairn.pull("data@best") / "a.txt").read_text() == "one"
    err = capsys.readouterr().err
    assert "does not answer" in err and "last known resolution of study:data@best: study:data@v1" in err
    assert [json.loads(line)["ref"] for line in inputs.read_text().splitlines()] == ["study:data@v1", "study:data@v1"]
    with pytest.raises(cairn.Unreachable):
        cairn.pull("data@v9")
    with pytest.raises(cairn.Unreachable):
        cairn.pull("data@never-pulled")

    down.start()  # --offline: the fallback even though the server answers
    store.artifact("data").publish(folder(env, "d2", {"a.txt": "two"}), aliases=["best"])
    capsys.readouterr()
    before = len(down.requests)
    out, err = run(capsys, "--project", str(env), "pull", "data@best", "--offline", "--no-record")
    assert (Path(out.strip()) / "a.txt").read_text() == "one" and "offline; using the last known resolution" in err
    assert len(down.requests) == before
    assert (cairn.pull("data@best//a.txt", record=False)).read_text() == "two"  # online, it asks again


def test_report_inputs_fall_back_while_down(down, env, capsys):
    from cairn.config import load_config
    from cairn.reports.build import build_all

    down.start()
    cairn.publish("data", folder(env, "d", {"n.txt": "41"}), aliases=["best"])
    report = env / "reports" / "r" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("---\ntitle: R\ninputs:\n  data: data@best\n---\n\n"
                      "```python exec hide\nn = int((inputs.data / 'n.txt').read_text()) + 1\n```\n\nThe answer is {{ n }}.\n")
    build_all(load_config(env))
    down.stop()
    capsys.readouterr()
    build_all(load_config(env))
    assert "last known resolution of study:data@best: study:data@v1" in capsys.readouterr().err
    narrative = (env / "generated" / "reports" / "r" / "narrative.html").read_text()
    assert "42" in narrative
    shutil.rmtree(env / "generated")
