"""The outbox: versions on their way to the remote, kept in the repo, usable before they are sent; their numbers
recorded in the repo once the remote takes them; heartbeats across hosts; concurrent senders on one shared outbox;
`cairn prune`."""

import json
import multiprocessing
import os
import re
import shutil
import stat
import time
from pathlib import Path

import pytest

import cairn
from cairn.cli import main

from test_identity import Server
from test_offline import down, run  # noqa: F401
from test_store import env, folder  # noqa: F401


def outbox_versions(env):
    base = env / ".cairn" / "outbox"
    return sorted(path.name for path in base.iterdir()) if base.is_dir() else []


def folder_versions(env):
    base = env / ".cairn" / "versions"
    return sorted(path.name for path in base.iterdir()) if base.is_dir() else []


# --------------------------------------------------------------- pending versions


def test_pending_lives_in_the_repo_and_survives_removing_the_read_only_folders(down, env, capsys):
    version = cairn.connect().artifact("data").publish(folder(env, "d", {"a.txt": "kept"}))
    repo = env / ".cairn"
    assert (repo / "outbox" / version.uid / "manifest.json").is_file()
    assert [path for path in (repo / "blobs").rglob("*") if path.is_file()]
    assert (repo / ".gitignore").read_text() == "*\n"
    cairn.pull(f"data@{version.uid}", record=False)
    shutil.rmtree(repo / "versions")  # derived from the blobs: removing it loses nothing
    capsys.readouterr()
    down.start()
    assert f"uploaded data@v1 ({version.short_id})" in run(capsys, "sync")[0]
    assert outbox_versions(env) == []


def test_a_sent_version_gets_its_number_in_the_repo_under_the_same_uid(down, env, capsys):
    data = folder(env, "d", {"a.txt": "kept", "sub/b.txt": "2"})
    store = cairn.connect()
    version = store.artifact("data").publish(data, aliases=["best"])
    assert version.number is None and outbox_versions(env) == [version.uid]
    capsys.readouterr()

    down.start()
    out, _ = run(capsys, "sync")
    assert f"uploaded data@v1 ({version.short_id})" in out
    assert outbox_versions(env) == []
    copy = store.backend.copies.get_version("data", 1)
    assert (copy["uid"], copy["status"], copy["origin"]) == (version.uid, "completed", f"{store.uid}/study")
    assert copy["short_id"] == version.short_id  # the remote's short id, the same as the repo's
    assert store.backend.copies.aliases("data") == {"latest": 1, "best": 1}  # the remote's aliases, mirrored
    assert [v["uid"] for v in down.versions()] == [version.uid]
    before = len(down.requests)
    path = cairn.pull(f"data@{version.uid}", record=False)  # in the repo: no download, no request
    assert path == env / ".cairn" / "versions" / version.uid / "files" and (path / "a.txt").read_text() == "kept"
    assert down.requests[before:] == []
    assert version.refresh().number == 1 and version.ref == "data@v1"


def test_pending_version_is_usable_before_upload(down, env, capsys, monkeypatch):
    from cairn.config import load_config
    from cairn.reports.build import build_all

    store = cairn.connect()
    data = store.artifact("data").publish(folder(env, "d", {"n.txt": "41"}), aliases=["best"], label="first")
    uid = data.uid
    capsys.readouterr()

    path = cairn.pull(f"data@{uid}", record=False)  # by uid
    assert path == env / ".cairn" / "versions" / uid / "files" and (path / "n.txt").read_text() == "41"
    assert not os.access(path / "n.txt", os.W_OK) and stat.S_IMODE((path / "n.txt").stat().st_mode) == 0o444
    assert "the remote does not have yet" in capsys.readouterr().err
    inputs = env / "job" / ".cairn-inputs.jsonl"
    inputs.parent.mkdir()
    monkeypatch.setenv("CAIRN_INPUTS", str(inputs))
    for ref in ("data@best", "data@first", "data", "data@latest"):  # by the alias the publish set, its label, latest
        assert (cairn.pull(ref) / "n.txt").read_text() == "41", ref
    assert "data@best is data@" in capsys.readouterr().err
    assert {json.loads(line)["ref"] for line in inputs.read_text().splitlines()} == {f"data@{uid}"}  # uid-exact lineage
    assert (cairn.pull(data.short_id, record=False) / "n.txt").read_text() == "41"  # a pending version by its short id
    with store.artifact("data").version(uid).open("n.txt") as handle:
        assert handle.read() == b"41"
    assert store.version("data@best").pending and store.version("data@best").status == "pending"
    assert [v.uid for v in store.artifact("data").versions()] == [uid]

    report = env / "reports" / "r" / "report.md"  # a report's inputs: resolve the pending version too
    report.parent.mkdir(parents=True)
    report.write_text("---\ntitle: R\ninputs:\n  data: data@best\n---\n\n"
                      "```python exec hide\nn = int((inputs.data / 'n.txt').read_text()) + 1\n```\n\nThe answer is {{ n }}.\n")
    build_all(load_config(env))
    assert "42" in (env / ".cairn" / "build" / "r" / "narrative.html").read_text()

    (env / "job" / "fit.txt").write_text("fit")  # a consumer published while down, with the pending input
    fit = store.artifact("fit").publish(env / "job")
    assert fit.inputs == [f"data@{uid}"]
    out, _ = run(capsys, "status")
    assert out.count("pending\t") == 2 and uid in out

    down.start()
    run(capsys, "sync")
    assert down.versions("fit")[0]["inputs"] == ["data@v1"]  # the server turned the uid input into the number
    assert cairn.LocalRepo(down.root).lineage("study", "data@v1")["consumers"] == ["fit@v1"]
    assert cairn.LocalRepo(down.root).aliases("study", "data")["best"] == 1


def test_a_running_version_is_not_usable(down, env, capsys):
    store = cairn.connect()
    record = store.backend._new_entry("running", "live")
    with pytest.raises(cairn.StoreError, match="running"):
        store.artifact("live").version(record["uid"]).pull()


# ------------------------------------------------------------------ heartbeats


def test_heartbeat_expiry_fails_a_running_version_from_another_host(down, env, capsys, monkeypatch):
    import cairn.client.sync as sync_module
    import cairn.store.folders as folders_module
    import cairn.store.outbox as outbox_module

    (env / "cairn.toml").write_text((env / "cairn.toml").read_text() + "heartbeat_timeout = 30\n")
    store = cairn.connect()
    record = store.backend._new_entry("running", "sweep", label="job")
    for module in (outbox_module, folders_module, sync_module):  # from now on, this process is a login node elsewhere
        monkeypatch.setattr(module, "hostname", lambda: "login-node")
    down.start()
    out, _ = run(capsys, "sync")  # a fresh heartbeat from another host: left alone
    assert "running versions are sent by the processes that run them" in out
    assert down.versions("sweep") == [] and outbox_versions(env) == [record["uid"]]
    status = run(capsys, "status")[0]
    assert status.startswith("running\tsweep\t-\t") and re.search(r"\theartbeat \d+s ago", status)

    heartbeat = env / ".cairn" / "outbox" / record["uid"] / "heartbeat"
    beat = json.loads(heartbeat.read_text())
    assert beat["host"] != "login-node" and beat["pid"] == os.getpid()
    heartbeat.write_text(json.dumps({**beat, "time": time.time() - 31}))  # its job went silent
    out, _ = run(capsys, "sync")
    assert "failing sweep@" in out and "no heartbeat from" in out and "failed sweep@v1" in out
    assert [(v["status"], v["uid"]) for v in down.versions("sweep")] == [("failed", record["uid"])]
    assert outbox_versions(env) == []


def test_a_publication_beats_and_its_owner_learns_it_was_failed(down, env, monkeypatch):
    (env / "cairn.toml").write_text((env / "cairn.toml").read_text() + "heartbeat_timeout = 0.5\n")
    live = env / "live"
    live.mkdir()
    pub = cairn.Publication("sweep", live)
    pub.start()
    heartbeat = env / ".cairn" / "outbox" / pub.uid / "heartbeat"
    first = json.loads(heartbeat.read_text())["time"]
    time.sleep(0.4)
    assert json.loads(heartbeat.read_text())["time"] > first  # the heartbeat thread rewrites it
    outbox = pub.store.outbox
    record = outbox.load(pub.uid)
    outbox.save({**record, "sync": "pending", "status": "failed", "error": "failed by login-node: no heartbeat"})
    (live / "x.txt").write_text("x")
    with pytest.raises(cairn.StoreError, match="was failed meanwhile"):
        pub.sync()


# ------------------------------------------------------------------ concurrency


def _flush_in_a_process(project, token, barrier, results):
    os.chdir(project)
    os.environ["CAIRN_TOKEN"] = token
    from cairn.client.open import open_repo

    repo = open_repo()
    barrier.wait()
    result = repo.engine.flush()
    results.put(sorted(record["uid"] for record in result["sent"]))


def test_concurrent_senders_on_one_outbox_send_each_version_once(down, env, tmp_path, capsys, monkeypatch):
    """Five processes (jobs and a login node sharing one project) send the same pending versions at once."""
    store = cairn.connect()
    uids = [store.artifact(f"data{index % 3}").publish(folder(env, f"d{index}", {"a.txt": f"{index}" * 5000})).uid
            for index in range(8)]
    assert len(outbox_versions(env)) == 8
    down.start()
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(5), context.Queue()
    processes = [context.Process(target=_flush_in_a_process, args=(str(env), down.token, barrier, results)) for _ in range(5)]
    for process in processes:
        process.start()
    sent = [uid for _ in processes for uid in results.get(timeout=120)]
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0
    assert len(sent) == len(set(sent))  # no version was sent by two flushers
    store.sync()  # whatever two flushers both backed off from (a claim race) goes now
    publishes = [line for line in down.requests if "/publish " in line]
    assert len(publishes) == 8  # one publish request per version, across all flushers
    assert sorted(v["uid"] for name in ("data0", "data1", "data2") for v in down.versions(name)) == sorted(uids)
    assert outbox_versions(env) == []
    assert not [path for path in (env / ".cairn").rglob("*") if ".tmp-" in path.name or ".flushing-" in path.name]
    numbers = sorted((copy["version"], name) for name in ("data0", "data1", "data2")
                     for copy in store.backend.copies.versions(name))
    assert len(numbers) == 8  # every one recorded in the repo with the remote's number


def test_a_live_claim_is_respected_and_a_stale_one_ignored(down, env, capsys):
    store = cairn.connect()
    version = store.artifact("data").publish(folder(env, "d", {"a.txt": "1"}))
    claim = env / ".cairn" / "outbox" / version.uid / ".flushing-other-host-4242"
    claim.write_text(json.dumps({"time": time.time(), "host": "other-host", "pid": 4242}))
    down.start()
    assert store.sync()["sent"] == [] and down.versions() == []
    claim.write_text(json.dumps({"time": time.time() - 3600, "host": "other-host", "pid": 4242}))  # its flusher died
    assert [record["uid"] for record in store.sync()["sent"]] == [version.uid]


# ------------------------------------------------------------------ sync, status


def test_sync_mirrors_the_remotes_aliases_into_the_repo(down, env, capsys, monkeypatch):
    down.start()
    store = cairn.connect()
    store.artifact("data").publish(folder(env, "d", {"a.txt": "one"}), aliases=["best"])
    with monkeypatch.context() as other:  # another machine publishes v2 and moves best
        other.setenv("CAIRN_DIR", str(env.parent / "other-repo"))
        cairn.connect(f"{down.url}/study", token=down.token).artifact("data").publish(
            folder(env, "d2", {"a.txt": "two"}), aliases=["best"])
    cairn.pull("data@v2", record=False)  # copied into the repo, but the repo still says best is v1
    out, err = run(capsys, "pull", "data@best", "--offline", "--no-record")
    assert (Path(out.strip()) / "a.txt").read_text() == "one"
    assert "mirrored the aliases of 1 artifacts" in run(capsys, "sync")[0]
    out, err = run(capsys, "pull", "data@best", "--offline", "--no-record")
    assert (Path(out.strip()) / "a.txt").read_text() == "two" and "data@v2" in err
    assert store.backend.copies.aliases("data") == {"latest": 2, "best": 2}
    known = json.loads((env / ".cairn" / "repo.json").read_text())["remote"]
    assert known == {"url": f"{down.url}/study", "uid": store.uid}
    assert "nothing pending" in run(capsys, "status")[0]


# ------------------------------------------------------------------------ prune


def _age(env, uid: str, days: float) -> None:
    path = env / ".cairn" / "versions" / uid / "manifest.json"
    record = json.loads(path.read_text())
    record["accessed_at"] = time.time() - days * 86400
    path.write_text(json.dumps(record))


def test_prune_removes_read_only_folders_by_last_access_and_never_pending_contents(down, env, capsys):
    down.start()
    store = cairn.connect()
    old = store.artifact("data").publish(folder(env, "d", {"a.txt": "old" * 1000}))
    new = store.artifact("data").publish(folder(env, "e", {"a.txt": "new" * 1000}))
    cairn.pull("data@v1", record=False)
    cairn.pull("data@v2", record=False)
    down.stop()
    pending = store.artifact("data").publish(folder(env, "f", {"a.txt": "pending" * 1000}))
    capsys.readouterr()
    assert folder_versions(env) == sorted([old.uid, new.uid]) and outbox_versions(env) == [pending.uid]
    _age(env, old.uid, 40)
    _age(env, new.uid, 3)
    for blob in (env / ".cairn" / "blobs").rglob("*"):
        if blob.is_file():  # blobs written in the last hour stay (a pull may be linking them)
            os.utime(blob, (time.time() - 7200, time.time() - 7200))
    out, _ = run(capsys, "prune", "--dry-run")
    assert out.startswith("would remove 1 read-only folders") and len(folder_versions(env)) == 2
    out, _ = run(capsys, "prune")  # default: not pulled for 30 days
    assert out.startswith("removed 1 read-only folders and 1 blobs") and folder_versions(env) == [new.uid]
    run(capsys, "prune", "--max-size", "0")
    assert folder_versions(env) == [] and outbox_versions(env) == [pending.uid]
    blobs = [path for path in (env / ".cairn" / "blobs").rglob("*") if path.is_file()]
    assert len(blobs) == 1  # only the pending version's: the remote has the others
    assert (cairn.pull(f"data@{pending.uid}", record=False) / "a.txt").read_text().startswith("pending")
    assert store.backend.copies.get_version("data", 1)["uid"] == old.uid  # the copies' metadata stays
    down.start()
    assert (cairn.pull("data@v1//a.txt", record=False)).read_text().startswith("old")  # downloaded again
    store.sync()
    assert down.versions()[-1]["uid"] == pending.uid


def test_prune_never_removes_the_blobs_of_a_repo_without_a_remote(env, capsys):
    store = cairn.connect()
    version = store.artifact("data").publish(folder(env, "d", {"a.txt": "mine"}))
    cairn.pull("data@v1", record=False)
    for blob in (env / ".cairn" / "blobs").rglob("*"):
        if blob.is_file():
            os.utime(blob, (time.time() - 7200, time.time() - 7200))
    run(capsys, "prune", "--max-size", "0")
    assert folder_versions(env) == [] and [path for path in (env / ".cairn" / "blobs").rglob("*") if path.is_file()]
    assert (cairn.pull(f"data@{version.uid}//a.txt", record=False)).read_text() == "mine"
