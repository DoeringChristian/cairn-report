"""The local replica: durable state apart from the throwaway cache, pending versions usable before upload, promotion,
heartbeats across hosts, concurrent flushers on one shared data root, `cairn prune`, and the old layout's migration."""

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
from cairn.client import replica
from cairn.store import cache as cache_module
from cairn.store.cache import cache_root, data_root

from test_identity import Server
from test_offline import down, run  # noqa: F401
from test_store import env, folder  # noqa: F401


def data_versions():
    base = data_root() / "versions"
    return sorted(path.name for path in base.iterdir()) if base.is_dir() else []


def cache_versions():
    base = cache_root() / "versions"
    return sorted(path.name for path in base.iterdir()) if base.is_dir() else []


# --------------------------------------------------------------- the two roots


def test_roots_follow_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "durable"))
    monkeypatch.setenv("CAIRN_CACHE_DIR", str(tmp_path / "scratch"))
    assert (data_root(), cache_root()) == (tmp_path / "durable", tmp_path / "scratch")
    monkeypatch.delenv("CAIRN_CACHE_DIR")
    assert cache_root() == tmp_path / "xdg-cache" / "cairn"


def test_data_root_resolution_order(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("CAIRN_DATA_DIR")
    assert data_root() == tmp_path / "xdg" / "cairn"  # never inside a project, even with cwd in one
    monkeypatch.delenv("XDG_DATA_HOME")
    assert data_root() == Path.home() / ".local" / "share" / "cairn"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "env"))
    assert data_root() == tmp_path / "env"  # the environment beats XDG
    capsys.readouterr()

    monkeypatch.delenv("CAIRN_DATA_DIR")  # the deprecated name still works, with one warning
    monkeypatch.setenv("CAIRN_STATE_DIR", str(tmp_path / "legacy"))
    monkeypatch.setattr(cache_module, "_warned", set())
    assert data_root() == tmp_path / "legacy" and data_root() == tmp_path / "legacy"
    assert capsys.readouterr().err.count("CAIRN_STATE_DIR is deprecated") == 1
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "env"))
    assert data_root() == tmp_path / "env"  # the new name wins when both are set
    cache_module.use_data_dir(tmp_path / "flag")  # `--data-dir` beats everything
    assert data_root() == tmp_path / "flag"


def test_pending_survives_deleting_the_cache_and_lives_in_the_project_folder(down, env, capsys, monkeypatch):
    monkeypatch.delenv("CAIRN_DATA_DIR")
    version = cairn.connect().artifact("data").publish(folder(env, "d", {"a.txt": "kept"}))
    here = env / ".cairn" / "pending"  # the project's own folder, ignored by git through its own .gitignore
    assert data_root() == here and (here / "versions" / version.uid / "manifest.json").is_file()
    assert [path for path in (here / "blobs").rglob("*") if path.is_file()]
    assert (env / ".cairn" / ".gitignore").read_text() == "*\n"
    shutil.rmtree(cache_root(), ignore_errors=True)
    capsys.readouterr()
    down.start()
    assert f"uploaded study:data@v1 (uid {version.uid})" in run(capsys, "sync")[0]
    assert data_versions() == []


def _old_state(env, tmp_path, monkeypatch, versions):
    """Publish while down with the data root at tmp/state/cairn, where releases before the data root kept it."""
    old = tmp_path / "state" / "cairn"
    monkeypatch.setenv("CAIRN_DATA_DIR", str(old))
    published = [cairn.connect().artifact("data").publish(folder(env, f"d{index}", {"a.txt": str(index)})) for index in range(versions)]
    return old, published


def test_pending_versions_move_from_the_old_state_folder_to_the_data_root(down, env, tmp_path, capsys, monkeypatch):
    old, (first, second) = _old_state(env, tmp_path, monkeypatch, 2)
    (old / "stores").mkdir(exist_ok=True)
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "new-data"))
    capsys.readouterr()

    out, err = run(capsys, "status")
    assert first.uid in out and second.uid in out and "moved 2 pending versions" in err
    new = tmp_path / "new-data"
    assert sorted(path.name for path in (new / "versions").iterdir()) == sorted([first.uid, second.uid])
    assert not (old / "versions").exists() and not (old / "blobs").exists()
    down.start()
    out = run(capsys, "sync")[0]  # their contents came along
    assert "uploaded study:data@v1" in out and "uploaded study:data@v2" in out


def test_the_old_state_folder_is_left_alone_when_it_is_the_data_root(down, env, tmp_path, capsys, monkeypatch):
    old, (version,) = _old_state(env, tmp_path, monkeypatch, 1)
    monkeypatch.delenv("CAIRN_DATA_DIR")
    monkeypatch.setenv("CAIRN_STATE_DIR", str(old))  # the deprecated variable pointed there
    out, err = run(capsys, "status")
    assert version.uid in out and "deprecated" in err and "moved" not in err
    assert (old / "versions" / version.uid).is_dir()


def test_sync_and_status_use_the_data_root_or_data_dir(down, env, tmp_path, capsys, monkeypatch):
    elsewhere = tmp_path / "persistent"
    monkeypatch.setenv("CAIRN_DATA_DIR", str(elsewhere))
    version = cairn.connect().artifact("data").publish(folder(env, "d", {"a.txt": "x"}))
    capsys.readouterr()
    assert version.uid in run(capsys, "status")[0]  # the default: $CAIRN_DATA_DIR
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "other"))
    assert version.uid not in run(capsys, "status")[0]
    assert version.uid in run(capsys, "status", "--data-dir", str(elsewhere))[0]  # a login node pointing at it
    down.start()
    out = run(capsys, "sync", "--data-dir", str(elsewhere))[0]
    assert f"uploaded study:data@v1 (uid {version.uid})" in out
    assert not (elsewhere / "versions" / version.uid).exists()


def test_pending_survives_deleting_the_cache_and_sync_promotes_it_with_its_uid(down, env, capsys):
    data = folder(env, "d", {"a.txt": "kept", "sub/b.txt": "2"})
    version = cairn.connect().artifact("data").publish(data, aliases=["best"])
    assert version.number is None and data_versions() == [version.uid]
    assert not (cache_root() / "blobs").exists() and not (cache_root() / "versions").exists()  # nothing of it in the cache
    shutil.rmtree(cache_root(), ignore_errors=True)  # node-local scratch wiped
    capsys.readouterr()

    down.start()
    out, _ = run(capsys, "sync")
    assert f"uploaded study:data@v1 (uid {version.uid})" in out
    assert data_versions() == [] and cache_versions() == [version.uid]
    record = json.loads((cache_root() / "versions" / version.uid / "manifest.json").read_text())
    assert (record["sync"], record["version"], record["manifest"]["uid"]) == ("synced", 1, version.uid)
    assert [v["uid"] for v in down.versions()] == [version.uid]
    before = len(down.requests)
    path = cairn.pull(f"data@{version.uid}", record=False)  # promoted: no download, no request
    assert path == cache_root() / "versions" / version.uid / "files" and (path / "a.txt").read_text() == "kept"
    assert len(down.requests) == before
    assert version.refresh().number == 1 and version.ref == "study:data@v1"


def test_pending_version_is_usable_before_upload(down, env, capsys, monkeypatch):
    from cairn.config import load_config
    from cairn.reports.build import build_all

    store = cairn.connect()
    data = store.artifact("data").publish(folder(env, "d", {"n.txt": "41"}), aliases=["best"], label="first")
    uid = data.uid
    capsys.readouterr()

    path = cairn.pull(f"data@{uid}", record=False)  # by uid
    assert path == data_root() / "versions" / uid / "files" and (path / "n.txt").read_text() == "41"
    assert not os.access(path / "n.txt", os.W_OK) and stat.S_IMODE((path / "n.txt").stat().st_mode) == 0o444
    assert "still pending" in capsys.readouterr().err
    inputs = env / "job" / ".cairn-inputs.jsonl"
    inputs.parent.mkdir()
    monkeypatch.setenv("CAIRN_INPUTS", str(inputs))
    for ref in ("data@best", "data@first", "data", "data@latest"):  # by the alias the publish set, its label, latest
        assert (cairn.pull(ref) / "n.txt").read_text() == "41", ref
    assert "study:data@best is study:data@" in capsys.readouterr().err
    assert {json.loads(line)["ref"] for line in inputs.read_text().splitlines()} == {f"study:data@{uid}"}  # uid-exact lineage
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
    assert fit.inputs == [f"study:data@{uid}"]
    out, _ = run(capsys, "status")
    assert out.count("pending\tstudy:") == 2 and uid in out

    down.start()
    run(capsys, "sync")
    assert down.versions("fit")[0]["inputs"] == ["study:data@v1"]  # the server turned the uid input into the number
    assert cairn.LocalStore(down.root).lineage("study:data@v1")["consumers"] == ["study:fit@v1"]
    assert cairn.LocalStore(down.root).aliases("study", "data")["best"] == 1


def test_a_running_version_is_not_usable(down, env, capsys):
    from cairn.client.sync import Replica

    store = cairn.connect()
    record = Replica(store.backend).add("running", "study", "live")
    with pytest.raises(cairn.StoreError, match="running"):
        store.artifact("live").version(record["uid"]).pull()


# ------------------------------------------------------------------ heartbeats


def test_heartbeat_expiry_fails_a_running_version_from_another_host(down, env, capsys, monkeypatch):
    import cairn.client.replica as replica_module
    import cairn.store.cache as cache_module
    from cairn.client.sync import Replica

    (env / "cairn.toml").write_text((env / "cairn.toml").read_text() + "heartbeat_timeout = 30\n")
    store = cairn.connect()
    record = Replica(store.backend).add("running", "study", "sweep", label="job")
    for module in (replica_module, cache_module):  # from now on, this process is a login node elsewhere
        monkeypatch.setattr(module, "hostname", lambda: "login-node")
    down.start()
    out, _ = run(capsys, "sync")  # a fresh heartbeat from another host: left alone
    assert "running versions are sent by the jobs that run them" in out
    assert down.versions("sweep") == [] and data_versions() == [record["uid"]]
    status = run(capsys, "status")[0]
    assert status.startswith("running\tstudy:sweep\t-\t") and re.search(r"\theartbeat \d+s ago", status)

    heartbeat = data_root() / "versions" / record["uid"] / "heartbeat"
    beat = json.loads(heartbeat.read_text())
    assert beat["host"] != "login-node" and beat["pid"] == os.getpid()
    heartbeat.write_text(json.dumps({**beat, "time": time.time() - 31}))  # its job went silent
    out, _ = run(capsys, "sync")
    assert "failing study:sweep@" in out and "no heartbeat from" in out and "failed study:sweep@v1" in out
    assert [(v["status"], v["uid"]) for v in down.versions("sweep")] == [("failed", record["uid"])]
    assert data_versions() == []


def test_a_publication_beats_and_its_owner_learns_it_was_failed(down, env, monkeypatch):
    (env / "cairn.toml").write_text((env / "cairn.toml").read_text() + "heartbeat_timeout = 0.5\n")
    live = env / "live"
    live.mkdir()
    pub = cairn.Publication("sweep", live)
    pub.start()
    heartbeat = data_root() / "versions" / pub.uid / "heartbeat"
    first = json.loads(heartbeat.read_text())["time"]
    time.sleep(0.4)
    assert json.loads(heartbeat.read_text())["time"] > first  # the heartbeat thread rewrites it
    record = replica.load(pub.uid)
    replica.save({**record, "sync": "pending", "status": "failed", "error": "failed by login-node: no heartbeat"})
    (live / "x.txt").write_text("x")
    with pytest.raises(cairn.StoreError, match="was failed by another flusher"):
        pub.sync()


# ------------------------------------------------------------------ concurrency


def _flush_in_a_process(url, token, state, cache, barrier, results):
    os.environ["CAIRN_DATA_DIR"], os.environ["CAIRN_CACHE_DIR"] = state, cache
    from cairn.client.http import Client
    from cairn.client.remote import RemoteStore
    from cairn.client.sync import Replica

    replica_ = Replica(RemoteStore(Client(url, token), "study"))
    barrier.wait()
    result = replica_.flush()
    results.put(sorted(record["uid"] for record in result["sent"]))


def test_concurrent_flushers_on_one_data_root_send_each_version_once(down, env, tmp_path, capsys, monkeypatch):
    """Five processes (as jobs on several hosts sharing one home) flush the same pending versions at once."""
    store = cairn.connect()
    uids = [store.artifact(f"data{index % 3}").publish(folder(env, f"d{index}", {"a.txt": f"{index}" * 5000})).uid
            for index in range(8)]
    assert len(data_versions()) == 8
    down.start()
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(5), context.Queue()
    caches = [str(tmp_path / f"node-scratch-{index}") for index in range(5)]  # each job its own node-local cache
    processes = [context.Process(target=_flush_in_a_process, args=(down.url, down.token, str(data_root()), cache, barrier, results))
                 for cache in caches]
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
    assert data_versions() == []
    assert not [path for path in data_root().rglob("*") if ".tmp-" in path.name or ".flushing-" in path.name]


def test_a_live_claim_is_respected_and_a_stale_one_ignored(down, env, capsys):
    store = cairn.connect()
    version = store.artifact("data").publish(folder(env, "d", {"a.txt": "1"}))
    claim = data_root() / "versions" / version.uid / ".flushing-other-host-4242"
    claim.write_text(json.dumps({"time": time.time(), "host": "other-host", "pid": 4242}))
    down.start()
    assert store.sync()["sent"] == [] and down.versions() == []
    claim.write_text(json.dumps({"time": time.time() - 3600, "host": "other-host", "pid": 4242}))  # its flusher died
    assert [record["uid"] for record in store.sync()["sent"]] == [version.uid]


# ------------------------------------------------------------------ sync, status


def test_sync_refreshes_alias_knowledge_and_status_all_lists_everything(down, env, capsys, monkeypatch):
    down.start()
    store = cairn.connect()
    store.artifact("data").publish(folder(env, "d", {"a.txt": "one"}), aliases=["best"])
    with monkeypatch.context() as other:  # another machine publishes v2 and moves best
        other.setenv("CAIRN_DATA_DIR", str(env.parent / "other-data"))
        other.setenv("CAIRN_CACHE_DIR", str(env.parent / "other-cache"))
        cairn.connect(down.url, token=down.token, project="study").artifact("data").publish(
            folder(env, "d2", {"a.txt": "two"}), aliases=["best"])
    cairn.pull("data@v2", record=False)  # in the cache, but this machine still thinks best is v1
    out, err = run(capsys, "pull", "data@best", "--offline", "--no-record")
    assert (Path(out.strip()) / "a.txt").read_text() == "one"
    assert "refreshed the aliases of 1 artifacts" in run(capsys, "sync")[0]
    out, err = run(capsys, "pull", "data@best", "--offline", "--no-record")
    assert (Path(out.strip()) / "a.txt").read_text() == "two" and "study:data@v2" in err
    known = json.loads((data_root() / "stores" / f"{store.uid}.json").read_text())
    assert known["url"] == down.url and known["artifacts"]["study:data"]["best"]["version"] == 2 and known["contact"]

    out = run(capsys, "status", "--all")[0]
    assert [line.split("\t")[0] for line in out.splitlines()] == ["synced", "synced"] and store.uid in out
    assert "nothing pending" in run(capsys, "status")[0]


# ------------------------------------------------------------------------ prune


def _age(uid: str, days: float) -> None:
    path = cache_root() / "versions" / uid / "manifest.json"
    record = json.loads(path.read_text())
    record["accessed_at"] = time.time() - days * 86400
    path.write_text(json.dumps(record))


def test_prune_removes_synced_versions_by_last_access_and_never_pending_ones(down, env, capsys):
    down.start()
    store = cairn.connect()
    old = store.artifact("data").publish(folder(env, "d", {"a.txt": "old" * 1000}))
    new = store.artifact("data").publish(folder(env, "e", {"a.txt": "new" * 1000}))
    down.stop()
    pending = store.artifact("data").publish(folder(env, "f", {"a.txt": "pending" * 1000}))
    capsys.readouterr()
    assert cache_versions() == sorted([old.uid, new.uid]) and data_versions() == [pending.uid]
    _age(old.uid, 40)
    _age(new.uid, 3)
    for blob in (cache_root() / "blobs").rglob("*"):
        if blob.is_file():  # blobs written in the last hour stay (a pull may be linking them)
            os.utime(blob, (time.time() - 7200, time.time() - 7200))
    out, _ = run(capsys, "prune", "--dry-run")
    assert out.startswith("would remove 1 versions") and len(cache_versions()) == 2
    out, _ = run(capsys, "prune")  # default: not pulled for 30 days
    assert out.startswith("removed 1 versions") and cache_versions() == [new.uid]
    assert (cairn.pull(f"data@{pending.uid}//a.txt", record=False)).read_text().startswith("pending")
    run(capsys, "prune", "--max-size", "0")
    assert cache_versions() == [] and data_versions() == [pending.uid]
    assert not [path for path in (cache_root() / "blobs").rglob("*") if path.is_file()]
    assert (cairn.pull(f"data@{pending.uid}", record=False) / "a.txt").is_file()  # pending data lives in the data root only


def test_prune_with_one_root_for_both_keeps_pending_versions_and_their_blobs(down, env, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "one-root"))
    monkeypatch.setenv("CAIRN_CACHE_DIR", str(tmp_path / "one-root"))
    down.start()
    store = cairn.connect()
    synced = store.artifact("data").publish(folder(env, "d", {"a.txt": "synced"}))
    down.stop()
    pending = store.artifact("data").publish(folder(env, "e", {"a.txt": "pending"}))
    capsys.readouterr()
    assert sorted(path.name for path in (tmp_path / "one-root" / "versions").iterdir()) == sorted([synced.uid, pending.uid])
    run(capsys, "prune", "--older-than", "0s")
    assert [path.name for path in (tmp_path / "one-root" / "versions").iterdir()] == [pending.uid]
    shutil.rmtree(tmp_path / "one-root" / "versions" / pending.uid / "files", ignore_errors=True)
    assert (cairn.pull(f"data@{pending.uid}//a.txt", record=False)).read_text() == "pending"  # its blob was kept
    down.start()
    store.sync()
    assert down.versions()[-1]["uid"] == pending.uid


# -------------------------------------------------------------------- migration


def test_the_outbox_layout_is_migrated_on_first_use(down, env, capsys):
    """A machine upgraded with versions in the old outbox, old hints, and an old-layout cache."""
    import hashlib

    from cairn.store.uids import new_uid

    cache = cache_root()
    down.start()
    store_uid = cairn.LocalStore(down.root).uid
    down.stop()
    contents = b"queued before the upgrade"
    sha = hashlib.sha256(contents).hexdigest()
    (cache / "blobs" / sha[:2]).mkdir(parents=True)
    (cache / "blobs" / sha[:2] / sha).write_bytes(contents)
    uid, cached_uid = new_uid(), new_uid()
    entry = {"uid": uid, "kind": "publish", "state": "pending", "url": down.url, "project": "study", "name": "data",
             "label": "old", "metadata": {"k": 1}, "inputs": [], "aliases": ["best"], "source": None,
             "files": [{"path": "a.txt", "sha256": sha, "size": len(contents), "mtime": 0}], "staged": [sha],
             "created": "2026-10-15T10:00:00+00:00", "owner": {"host": "h", "pid": 1}, "number": None, "pushed": {}, "error": None}
    (cache / "outbox" / store_uid).mkdir(parents=True)
    (cache / "outbox" / store_uid / f"{uid}.json").write_text(json.dumps(entry))
    (cache / "stores.json").write_text(json.dumps({down.url: store_uid}))
    (cache / "resolved").mkdir()
    (cache / "resolved" / f"{store_uid}.json").write_text(json.dumps({"study:old@best": {"uid": cached_uid, "version": 7},
                                                                      "study:old@v7": {"uid": cached_uid, "version": 7}}))
    old_folder = cache / "versions" / cached_uid  # a pulled version as the old cache kept it
    old_folder.mkdir(parents=True)
    (old_folder / "x.txt").write_text("cached before")
    manifest = {"version": 7, "uid": cached_uid, "status": "completed", "files": [{"path": "x.txt", "sha256": "0" * 64, "size": 13}]}
    (old_folder / ".cairn-complete").write_text(json.dumps({"store": store_uid, "project": "study", "name": "old", "version": 7,
                                                            "uid": cached_uid, "manifest": manifest}))

    out, _ = run(capsys, "status")
    assert f"pending\tstudy:data\t-\t{uid}\tcompleted\th\t" in out
    assert not (cache / "outbox").exists() and not (cache / "resolved").exists() and not (cache / "stores.json").exists()
    assert (data_root() / "blobs" / sha[:2] / sha).read_bytes() == contents
    assert json.loads((data_root() / "stores" / f"{store_uid}.json").read_text())["url"] == down.url
    path = cairn.pull("old@best", record=False, offline=True)  # the old hints and the old cache folder still answer
    assert path == old_folder / "files" and (path / "x.txt").read_text() == "cached before"
    assert not (path / ".cairn-complete").exists()

    down.start()
    assert "uploaded study:data@v1" in run(capsys, "sync")[0]
    [version] = down.versions()
    assert (version["uid"], version["label"], version["metadata"]) == (uid, "old", {"k": 1})
    assert cairn.LocalStore(down.root).aliases("study", "data")["best"] == 1
