"""Deleting versions (tombstones), protection rules, retention rules, and garbage collection of blobs: on a local
store, through the store server (auth), with the object API and the CLI, and racing concurrent publishes."""

import hashlib
import io
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

import cairn
from cairn import Conflict, Deleted, LocalStore, MissingBlobs, NotFound, Protected, RemoteStore
from cairn.cli import main
from cairn.client.http import Client
from cairn.store.gc import DAY, parse_duration

from test_server import api, call, remote, server, use_remote  # noqa: F401  (the store server in a thread, helpers)
from test_store import env, folder  # noqa: F401

LATER = 2 * DAY  # a clock this far ahead sees every blob past the default grace period


def blob(store, text: str) -> dict:
    data = text.encode()
    sha = hashlib.sha256(data).hexdigest()
    store.put_blob(sha, io.BytesIO(data), len(data))
    return {"path": "f.txt", "sha256": sha, "size": len(data)}


def publish(store, project, name, text, **options) -> int:
    return store.publish(project, name, [blob(store, text)], **options)


@pytest.fixture
def local(tmp_path):
    store = LocalStore(tmp_path / "root")
    for project in ("p", "q"):
        store.create_project(project)
    return store


def statuses(store, project, name):
    return [(version["version"], version["status"]) for version in store.versions(project, name)]


# ------------------------------------------------------------------ tombstones


def test_a_deleted_version_is_a_tombstone(local):
    for text in ("one", "two", "three"):
        publish(local, "p", "run", text, metadata={"t": text}, label=text)
    uid = local.get_version("p", "run", 3)["uid"]
    result = local.delete_version("p", "run", 3, reason="bad data", by="tester")
    assert result == {"deleted": ["p:run@v3"], "skipped": []}
    tomb = local.get_version("p", "run", 3)
    assert tomb["status"] == "deleted" and tomb["files"] == [] and tomb["was"] == "completed"
    assert (tomb["reason"], tomb["deleted_by"], tomb["uid"], tomb["label"], tomb["metadata"]) == ("bad data", "tester", uid, "three", {"t": "three"})
    assert tomb["deleted_at"] and tomb["file_count"] == 1 and tomb["size"] == 5
    assert local.get_version("p", "run", uid)["status"] == "deleted"  # by uid too
    for ref in ("p:run@v3", f"p:run@{uid}"):  # exact references fail clearly
        with pytest.raises(Deleted, match=r"p:run@v3 was deleted \(.* by tester\): bad data"):
            local.resolve(ref)
    with pytest.raises(Deleted):
        local.read_file("p", "run", "v3", "f.txt")
    with pytest.raises(Deleted):
        local.publish("q", "x", [blob(local, "x")], inputs=["p:run@v3"])  # not usable as an input
    with pytest.raises(NotFound):  # the label resolves to completed versions only
        local.resolve("p:run@three")
    assert local.aliases("p", "run") == {"latest": 2} and local.resolve("p:run")[2]["version"] == 2  # latest moved back
    assert local.summary("p", "run")["versions"] == 2 and local.summary("p", "run")["metadata"] == {"t": "two"}
    # the number and the uid are never used again
    assert publish(local, "p", "run", "four") == 4
    with pytest.raises(Conflict, match="deleted"):
        local.publish("p", "run", [blob(local, "five")], uid=uid)
    with pytest.raises(Conflict, match="deleted"):
        local.start_version("p", "run", uid=uid)
    with pytest.raises(Deleted):
        local.delete_version("p", "run", 3)  # already deleted
    assert local.alias_log("p", "run")[-1] == {"alias": "latest", "version": 4, "time": local.alias_log("p", "run")[-1]["time"]}


def test_deleting_every_version_hides_the_artifact_and_keeps_the_tombstones(local):
    publish(local, "p", "run", "one")
    publish(local, "p", "run", "two")
    assert local.delete_artifact("p", "run", reason="gone")["deleted"] == ["p:run@v1", "p:run@v2"]
    assert statuses(local, "p", "run") == [(1, "deleted"), (2, "deleted")]
    assert local.aliases("p", "run") == {} and local.summary("p", "run")["latest"] is None
    with pytest.raises(NotFound):
        local.resolve("p:run")
    assert local.summaries("p") == [] and local.artifacts("p") == []
    assert [item["artifacts"] for item in local.overviews() if item["name"] == "p"] == [0]
    with pytest.raises(Deleted):
        local.delete_artifact("p", "run")
    assert publish(local, "p", "run", "three") == 3  # numbering goes on after the tombstones


def test_latest_moves_back_to_the_newest_completed_version(local):
    publish(local, "p", "run", "one")
    running = local.start_version("p", "run")
    local.fail_version("p", "run", running)  # v2 failed
    publish(local, "p", "run", "three")
    local.delete_version("p", "run", 3)
    assert local.aliases("p", "run")["latest"] == 1  # not the failed v2


# ------------------------------------------------------------------ protection


def test_protected_versions_are_refused_without_force(local):
    publish(local, "p", "data", "one", aliases=["best"])
    publish(local, "p", "data", "two")
    publish(local, "p", "data", "three")
    publish(local, "q", "fit", "fit", inputs=["p:data@v2"])  # used as an input in another project
    publish(local, "q", "reports/summary", "report", inputs=["p:data@v3"])  # a report's pinned input
    for number, reason in ((1, "alias best"), (2, "input of q:fit@v1"), (3, "input of q:reports/summary@v1")):
        with pytest.raises(Protected, match=reason) as caught:
            local.delete_version("p", "data", number)
        assert caught.value.protected == [{"ref": f"p:data@v{number}", "reasons": [reason]}]
    with pytest.raises(Protected):  # an artifact: all of it, or nothing
        local.delete_artifact("p", "data")
    assert statuses(local, "p", "data") == [(1, "completed"), (2, "completed"), (3, "completed")]
    running = local.start_version("p", "data")
    with pytest.raises(Protected, match="running"):
        local.delete_version("p", "data", running)
    # once the consumer is deleted, its input is not protected any more
    local.delete_version("q", "fit", 1)
    local.delete_version("p", "data", 2)
    # force deletes protected versions: the alias goes, the consumer keeps its input edge
    local.delete_version("p", "data", 1, force=True)
    assert "best" not in local.aliases("p", "data")
    local.delete_version("p", "data", 3, force=True)
    assert local.get_version("q", "reports/summary", 1)["inputs"] == ["p:data@v3"]
    marked = local.lineage("q:reports/summary@v1", marks=True)
    assert marked == {"inputs": ["p:data@v3"], "consumers": [], "deleted": ["p:data@v3"]}
    assert local.lineage("p:data@v3", marks=True) == {"inputs": [], "consumers": ["q:reports/summary@v1"], "deleted": ["p:data@v3"]}
    assert local.lineage("p:data@v2", marks=True)["deleted"] == ["p:data@v2", "q:fit@v1"]


def test_an_artifact_deletes_its_own_chain_and_a_project_only_when_empty(local):
    publish(local, "p", "chain", "one")
    publish(local, "p", "chain", "two", inputs=["p:chain@v1"])  # consumed inside the artifact being deleted: fine
    with pytest.raises(Protected, match="versions that are not deleted"):
        local.delete_project("p")
    assert local.delete_artifact("p", "chain")["deleted"] == ["p:chain@v1", "p:chain@v2"]
    assert local.delete_project("p")["project"] == "p"
    assert "p" not in [item["name"] for item in local.projects()]
    with pytest.raises(NotFound):
        local.delete_project("p")
    local.create_project("p")
    assert publish(local, "p", "chain", "three") == 3  # tombstones outlive their project: no number comes back
    publish(local, "q", "uses", "u", inputs=["p:chain@v3"])
    with pytest.raises(Protected):
        local.delete_project("p")
    assert local.delete_project("p", force=True)["deleted"] == ["p:chain@v3"]  # force deletes protected versions too


# ------------------------------------------------------------------ retention rules


def test_retention_keep_last_skips_protected_versions(local):
    for number in range(1, 7):
        publish(local, "p", "runs/a", f"a{number}")
    local.set_alias("p", "runs/a", "best", 1)
    publish(local, "q", "fit", "fit", inputs=["p:runs/a@v3"])
    publish(local, "p", "other", "o1")
    publish(local, "p", "other", "o2")
    rules = local.set_retention("p", [{"keep_last": 5}, {"prefix": "runs/", "keep_last": 2}])
    assert rules == [{"prefix": "", "keep_last": 5, "keep_days": None, "keep_aliased": True},
                     {"prefix": "runs/", "keep_last": 2, "keep_days": None, "keep_aliased": True}]
    assert local.retention("p") == rules and local.retention("q") == []
    before = local.versions("p", "runs/a")
    dry = local.gc(dry_run=True)
    assert dry["dry_run"] and dry["deleted"] == ["p:runs/a@v4", "p:runs/a@v2"]  # v6 latest and v5 kept, v1 aliased
    assert dry["skipped"] == [{"ref": "p:runs/a@v3", "reasons": ["input of q:fit@v1"]}]
    assert local.versions("p", "runs/a") == before  # a dry run changes nothing
    done = local.gc(dry_run=False)
    assert (done["deleted"], done["skipped"]) == (dry["deleted"], dry["skipped"])
    assert statuses(local, "p", "runs/a") == [(1, "completed"), (2, "deleted"), (3, "completed"), (4, "deleted"),
                                              (5, "completed"), (6, "completed")]
    assert local.get_version("p", "runs/a", 2)["reason"] == "retention rule"
    assert statuses(local, "p", "other") == [(1, "completed"), (2, "completed")]  # the longest prefix wins
    local.set_retention("p", [{"prefix": "runs/", "keep_last": 1, "keep_aliased": False}])
    result = local.gc(dry_run=False)
    assert result["deleted"] == ["p:runs/a@v5"]  # v1 took a place, and is still never deleted
    assert {item["ref"]: item["reasons"] for item in result["skipped"]} == {"p:runs/a@v3": ["input of q:fit@v1"], "p:runs/a@v1": ["alias best"]}
    with pytest.raises(ValueError):
        local.set_retention("p", [{"keep_last": 0}])
    with pytest.raises(ValueError):
        local.set_retention("p", [{"prefix": "x"}])
    assert local.set_retention("p", []) == [] and local.retention("p") == []


def test_retention_keep_days_and_running_versions(local):
    for number in range(1, 4):
        publish(local, "p", "x", f"x{number}")
    running = local.start_version("p", "x")
    local.set_retention("p", [{"keep_days": 5}])
    assert local.gc(now=time.time() + DAY)["deleted"] == []  # everything is younger than 5 days
    result = local.gc(dry_run=False, now=time.time() + 10 * DAY)
    assert result["deleted"] == ["p:x@v2", "p:x@v1"]  # v3 is latest
    assert result["skipped"] == [{"ref": f"p:x@v{running}", "reasons": ["running"]}]
    assert local.get_version("p", "x", running)["status"] == "running"
    local.set_retention("p", [{"keep_days": 5, "keep_last": 1}])  # either rule keeping a version is enough
    assert local.gc(now=time.time())["deleted"] == []


def test_durations():
    assert [parse_duration(text) for text in ("24h", "30m", "90s", "7d", "1w", 15, "0")] == [86400, 1800, 90, 7 * DAY, 7 * DAY, 15, 0]
    for bad in ("soon", "-1h", True, "1y"):
        with pytest.raises(ValueError):
            parse_duration(bad)


# ------------------------------------------------------------------ blob garbage collection


def blob_file(store, entry):
    return store.blob_path(entry["sha256"])


def test_blobs_are_removed_only_unreferenced_and_after_the_grace_period(local):
    shared = blob(local, "shared")
    only = blob(local, "only-here")
    local.publish("p", "a", [shared, {**only, "path": "g.txt"}])
    local.publish("q", "b", [shared])  # the same content in another project
    local.publish("p", "a", [blob(local, "next")])
    uploaded = blob(local, "uploaded, version not committed yet")
    local.delete_version("p", "a", 1)
    assert local.gc(dry_run=False)["blobs"] == 0  # every blob is younger than the grace period
    dry = local.gc(dry_run=True, now=time.time() + LATER)
    assert (dry["blobs"], dry["bytes"]) == (2, only["size"] + uploaded["size"])
    assert blob_file(local, only).is_file() and blob_file(local, uploaded).is_file()  # the dry run removed nothing
    (local.root / "tmp" / "dead-upload").write_bytes(b"x" * 10)
    old = time.time() - LATER
    os.utime(local.root / "tmp" / "dead-upload", (old, old))
    done = local.gc(dry_run=False, now=time.time() + LATER)
    assert (done["blobs"], done["bytes"], done["temporary"], done["temporary_bytes"]) == (2, dry["bytes"], 1, 10)
    assert not blob_file(local, only).exists() and not blob_file(local, uploaded).exists()
    assert blob_file(local, shared).is_file()  # still named by q:b@v1
    assert b"".join(local.read_file("q", "b", "v1", "f.txt").chunks) == b"shared"
    assert local.missing([only]) == [only["sha256"]]  # a swept blob is reported missing: a client uploads it again
    assert local.gc(dry_run=False, now=time.time() + LATER)["blobs"] == 0
    # with grace 0 a fresh unreferenced blob goes at once; a referenced one never
    loose = blob(local, "loose")
    assert local.gc(dry_run=False, grace=0)["blobs"] == 1 and not blob_file(local, loose).exists()


def test_a_sweep_between_the_check_and_the_commit_fails_the_publish_cleanly(local, monkeypatch):
    entry = blob(local, "garbage")
    local.publish("p", "g", [entry])
    local.delete_version("p", "g", 1)
    original = LocalStore.missing

    def check_then_sweep(self, entries):
        found = original(self, entries)
        if not swept:
            swept.append(self.gc(dry_run=False, grace=0))  # the sweep lands after the check, before the transaction
        return found

    swept = []
    monkeypatch.setattr(LocalStore, "missing", check_then_sweep)
    with pytest.raises(MissingBlobs) as caught:
        local.publish("p", "x", [entry])
    assert caught.value.missing == [entry["sha256"]] and swept[0]["blobs"] == 1
    monkeypatch.undo()
    with pytest.raises(NotFound):
        local.resolve("p:x")  # nothing was committed
    blob(local, "garbage")
    assert local.publish("p", "x", [entry]) == 1


def age(root, version, seconds=LATER):
    """Make a version's blobs look old (past the grace period)."""
    for item in version.files:
        path = root / "blobs" / item.sha256[:2] / item.sha256[2:]
        os.utime(path, (time.time() - seconds,) * 2)


def test_publishes_racing_sweeps_end_complete_and_readable(env, tmp_path):
    """Threads: one sweeps over and over while another publishes folders whose contents are old garbage blobs
    (deleted versions' leftovers) through the object API, which re-stages on MissingBlobs."""
    root = tmp_path / "race-root"
    store = cairn.connect(root, project="p")
    backend = store.backend
    backend.create_project("p")
    stop, errors, sweeps = threading.Event(), [], [0]

    def sweeper():
        while not stop.is_set():
            try:
                sweeps[0] += backend.gc(dry_run=False, grace=3600)["blobs"]
            except Exception as error:  # noqa: BLE001
                errors.append(error)

    versions = []
    thread = threading.Thread(target=sweeper)
    thread.start()
    try:
        for number in range(25):
            data = folder(tmp_path, f"race-{number}", {"a.txt": f"content {number}", "b.txt": f"more {number}"})
            garbage = store.project("p").artifact("garbage").publish(data)
            age(root, garbage)  # before deleting it: a tombstone has no files
            garbage.delete()
            time.sleep(0.02 * (number % 3))  # sometimes the sweep surely lands first, sometimes they race
            versions.append(store.project("p").artifact("x").publish(data))
    finally:
        stop.set()
        thread.join()
    assert not errors
    for version in versions:
        assert version.status == "completed"
        for item in version.files:
            assert hashlib.sha256(backend.blob_path(item.sha256).read_bytes()).hexdigest() == item.sha256
    assert sweeps[0] > 0  # the sweeper did remove garbage meanwhile


# ------------------------------------------------------------------ the server: routes and auth


def seed_two(server):
    for project in ("alpha", "beta"):
        r = remote(server, project=project)
        r.create_project(project)
        for text in ("one", "two", "three"):
            entry = {"path": "f.txt", "sha256": hashlib.sha256(text.encode()).hexdigest(), "size": len(text)}
            r.put_blob(entry["sha256"], io.BytesIO(text.encode()), entry["size"])
            r.publish(project, "data", [entry])


def test_deleting_through_the_server_needs_the_right_token(server):
    from cairn.store.tokens import create_token

    seed_two(server)
    reader = create_token(server.root, "alpha", name="reader")
    writer = create_token(server.root, "alpha", write=True, name="writer")
    path = "/api/v2/projects/alpha/artifacts/data/versions/3"
    assert call(server, "DELETE", path, token=reader)[0] == 403
    assert call(server, "DELETE", "/api/v2/projects/beta/artifacts/data/versions/3", token=writer)[0] == 403  # other project
    assert call(server, "DELETE", path + "?force=1", token=writer)[0] == 403  # force: the owner only
    assert api(server, "DELETE", path + "?reason=wrong%20crop", token=writer) == {"deleted": ["alpha:data@v3"], "skipped": []}
    tomb = api(server, "GET", path)
    assert (tomb["status"], tomb["reason"], tomb["deleted_by"]) == ("deleted", "wrong crop", "writer")
    assert call(server, "DELETE", path, token=writer)[0] == 410
    assert call(server, "GET", "/files/alpha/data@v3//f.txt")[0] == 410
    status, _, body = call(server, "GET", "/api/v2/resolve?ref=alpha:data@v3")
    assert status == 410 and b"was deleted" in body
    assert api(server, "GET", "/api/v2/lineage?ref=alpha:data@v3")["deleted"] == ["alpha:data@v3"]
    assert api(server, "GET", "/api/v2/projects/alpha/artifacts/data/aliases") == {"latest": 2}
    # protected: 409 with the reasons; the owner may force
    api(server, "POST", "/api/v2/projects/alpha/artifacts/data/aliases", {"alias": "best", "version": 1}, token=writer)
    status, _, body = call(server, "DELETE", "/api/v2/projects/alpha/artifacts/data/versions/1", token=writer)
    assert status == 409 and json.loads(body)["protected"] == [{"ref": "alpha:data@v1", "reasons": ["alias best"]}]
    assert api(server, "DELETE", "/api/v2/projects/alpha/artifacts/data/versions/1?force=1")["deleted"] == ["alpha:data@v1"]
    # an artifact, then its project
    assert call(server, "DELETE", "/api/v2/projects/alpha", token=writer)[0] == 409  # not empty
    assert api(server, "DELETE", "/api/v2/projects/alpha/artifacts/data", token=writer)["deleted"] == ["alpha:data@v2"]
    assert api(server, "DELETE", "/api/v2/projects/alpha", token=writer)["project"] == "alpha"
    assert call(server, "DELETE", "/api/v2/projects/beta?force=1", token=writer)[0] == 403
    assert [item["name"] for item in api(server, "GET", "/api/v2/projects")] == ["beta"]


def test_retention_and_gc_routes(server):
    from cairn.store.tokens import create_token

    seed_two(server)
    reader = create_token(server.root, "alpha", name="reader")
    writer = create_token(server.root, "alpha", write=True, name="writer")
    rules = {"rules": [{"prefix": "", "keep_last": 1}]}
    assert call(server, "PUT", "/api/v2/projects/alpha/retention", rules, token=reader)[0] == 403
    assert call(server, "PUT", "/api/v2/projects/beta/retention", rules, token=writer)[0] == 403
    assert call(server, "PUT", "/api/v2/projects/alpha/retention", {"rules": [{"keep_last": -1}]}, token=writer)[0] == 400
    assert api(server, "PUT", "/api/v2/projects/alpha/retention", rules, token=writer)["rules"][0]["keep_last"] == 1
    assert api(server, "GET", "/api/v2/projects/alpha/retention", token=reader)["rules"][0]["keep_last"] == 1
    for token in (reader, writer):
        assert call(server, "POST", "/api/v2/gc", {"dry_run": True}, token=token)[0] == 403
    dry = api(server, "POST", "/api/v2/gc", {})  # a dry run by default
    assert dry["dry_run"] and dry["deleted"] == ["alpha:data@v2", "alpha:data@v1"] and dry["blobs"] == 0
    assert [v["status"] for v in api(server, "GET", "/api/v2/projects/alpha/artifacts/data/versions")] == ["completed"] * 3
    done = api(server, "POST", "/api/v2/gc", {"dry_run": False, "grace": 0})
    assert done["deleted"] == dry["deleted"] and done["blobs"] == 0  # beta still names the same three blobs
    assert [v["status"] for v in api(server, "GET", "/api/v2/projects/alpha/artifacts/data/versions")] == ["deleted", "deleted", "completed"]
    api(server, "DELETE", "/api/v2/projects/beta/artifacts/data/versions/1")
    assert api(server, "POST", "/api/v2/gc", {"dry_run": False, "grace": "0s"})["blobs"] == 1  # "one" is garbage now
    assert call(server, "POST", "/api/v2/gc", {"grace": "soon"})[0] == 400


def test_a_publish_racing_a_server_sweep_uploads_again(server, env, monkeypatch):
    """The blob is on the server (garbage) when the client checks, so it is not uploaded; a sweep removes it before
    the publish arrives: the server answers 409 + missing, and the client uploads it and sends the version again."""
    r = use_remote(env, server)
    store = cairn.connect()
    project = store.project("study")
    data = folder(env, "data", {"a.txt": "garbage soon"})
    project.artifact("garbage").publish(data).delete()
    original, swept = RemoteStore.publish, []

    def sweep_first(self, *args, **kwargs):
        if not swept:
            swept.append(RemoteStore(Client(server.url, server.token)).gc(dry_run=False, grace=0))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RemoteStore, "publish", sweep_first)
    version = project.artifact("x").publish(data)
    assert swept[0]["blobs"] == 1 and version.status == "completed"
    assert call(server, "GET", "/files/study/x@v1//a.txt")[2] == b"garbage soon"
    assert r.missing([{"sha256": version.files[0].sha256}]) == []


def test_publishes_racing_server_sweeps_end_complete_and_downloadable(server, env, tmp_path):
    use_remote(env, server)
    store = cairn.connect()
    owner = RemoteStore(Client(server.url, server.token))
    stop, errors, sweeps = threading.Event(), [], [0]

    def sweeper():
        while not stop.is_set():
            try:
                sweeps[0] += owner.gc(dry_run=False, grace=3600)["blobs"]
            except Exception as error:  # noqa: BLE001
                errors.append(error)

    thread = threading.Thread(target=sweeper)
    thread.start()
    versions = []
    try:
        for number in range(12):
            data = folder(tmp_path, f"race-{number}", {"a.txt": f"payload {number}"})
            garbage = store.project("study").artifact("garbage").publish(data)
            age(server.root, garbage)
            garbage.delete()
            time.sleep(0.05 * (number % 3))
            versions.append(store.project("study").artifact("x").publish(data))
    finally:
        stop.set()
        thread.join()
    assert not errors
    for number, version in enumerate(versions):
        assert version.status == "completed" and not version.pending
        assert call(server, "GET", f"/files/study/x@v{version.number}//a.txt")[2] == f"payload {number}".encode()
    assert sweeps[0] > 0


# ------------------------------------------------------------------ the object API and the CLI


@pytest.fixture(params=["local", "remote"])
def configured(request, env, tmp_path, monkeypatch):
    if request.param == "local":
        (env / "cairn.toml").write_text('[store]\nproject = "study"\n')
        yield SimpleNamespace(root=env / ".store", remote=False)
        return
    from test_identity import Server

    started = Server(tmp_path / "store-root").start()
    monkeypatch.setenv("CAIRN_STORE_TOKEN", started.token)
    (env / "cairn.toml").write_text(f'[store]\nurl = "{started.url}"\nproject = "study"\n')
    yield SimpleNamespace(root=tmp_path / "store-root", remote=True)
    started.stop()


def test_object_api(configured, env):
    store = cairn.connect()
    project = store.project()
    crops = project.artifact("crops")
    v1 = crops.publish(folder(env, "c1", {"a.txt": "1"}), aliases=["paper"])
    v2 = crops.publish(folder(env, "c2", {"a.txt": "2"}))
    with pytest.raises(Protected):
        v1.delete()
    result = v2.delete(reason="oops")
    assert result["deleted"] == ["study:crops@v2"] and v2.deleted and v2.manifest["reason"] == "oops"
    assert crops.latest.number == 1
    with pytest.raises(Deleted):
        store.version("crops@v2")
    project.retention = [{"keep_last": 3}]
    assert project.retention == [{"prefix": "", "keep_last": 3, "keep_days": None, "keep_aliased": True}]
    assert project.set_retention("runs/", keep_days=7)[1] == {"prefix": "runs/", "keep_last": None, "keep_days": 7, "keep_aliased": True}
    report = store.gc(dry_run=True, grace=0)
    assert report["dry_run"] and report["blobs"] == 1  # v2's blob
    assert store.gc(dry_run=False, grace=0)["blobs"] == 1
    assert crops.delete(force=True)["deleted"] == ["study:crops@v1"]
    assert [v.status for v in crops.versions()] == ["deleted", "deleted"]


def test_cli_delete_retention_gc(configured, env, capsys):
    store = cairn.connect()
    data = store.project().artifact("data")
    for number in range(1, 5):
        data.publish(folder(env, f"d{number}", {"a.txt": str(number)}))
    capsys.readouterr()
    main(["delete", "data@v4", "--reason", "bad"])
    assert capsys.readouterr().out == "deleted study:data@v4\n"
    data.alias("best", 1)
    with pytest.raises(SystemExit, match="alias best"):
        main(["delete", "data@v1"])
    main(["retention", "set", "study", "--keep-last", "1"])
    assert capsys.readouterr().out == "(every artifact)\tkeep_last=1\n"
    main(["retention", "set", "study", "--prefix", "runs/", "--keep-days", "7"])
    main(["retention", "show", "study"])
    assert capsys.readouterr().out.splitlines()[-2:] == ["(every artifact)\tkeep_last=1", "runs/\tkeep_days=7"]
    main(["gc", "--dry-run", "--grace", "0"])
    out = capsys.readouterr().out
    assert "would delete study:data@v2\n" in out and "would delete 1 versions; would remove 2 blobs" in out
    assert [v.status for v in data.versions()] == ["completed", "completed", "completed", "deleted"]
    main(["gc", "--grace", "0"])
    out = capsys.readouterr().out
    assert "deleted study:data@v2\n" in out and "removed 2 blobs" in out and "freed" in out
    main(["delete", "--artifact", "data", "--force"])
    assert capsys.readouterr().out == "deleted study:data@v1\ndeleted study:data@v3\n"
    main(["retention", "clear", "study"])
    assert capsys.readouterr().out == "runs/\tkeep_days=7\n"
    if not configured.remote:
        main(["gc", str(configured.root), "--grace", "0"])
        assert "removed 2 blobs" in capsys.readouterr().out


def test_cli_gc_on_a_served_root_is_refused(server, capsys):
    with pytest.raises(SystemExit, match="served by `cairn server`"):
        main(["gc", str(server.root)])
    main(["gc", str(server.root), "--dry-run"])  # reading is fine
    assert "would delete 0 versions" in capsys.readouterr().out


def test_the_server_collects_garbage_on_a_schedule(tmp_path):
    from cairn.server.app import gc_schedule

    store = LocalStore(tmp_path / "root")
    store.create_project("p")
    publish(store, "p", "x", "one")
    store.delete_version("p", "x", 1)
    lines = []
    stop = gc_schedule(SimpleNamespace(gc=store.gc), 0.05, grace=0, say=lines.append)
    try:
        deadline = time.time() + 10
        while not lines and time.time() < deadline:
            time.sleep(0.02)
    finally:
        stop.set()
    assert lines and lines[0].startswith("gc: deleted 0 versions") and "removed 1 blobs" in lines[0]


# ------------------------------------------------------------------ the UI shows tombstones

from test_ui import data, login, quick_logins, ui  # noqa: E402,F401  (the UI over the folder and over the server)


def test_the_ui_shows_tombstones(ui, env):
    r = use_remote(env, ui.server)
    cairn.publish("a", folder(env, "a", {"f": "1"}), store=r)
    cairn.publish("a", folder(env, "a2", {"f": "2"}), store=r)
    cairn.publish("b", folder(env, "b", {"f": "3"}), inputs=["study:a@v1"], store=r)
    r.delete_version("study", "a", 1, reason="leaked labels", force=True)
    cookie = login(ui)
    rows = data(ui, "projects/study/artifacts/a", cookie)["versions"]
    assert [(row["version"], row["status"], row.get("reason")) for row in rows] == [(1, "deleted", "leaked labels"), (2, "completed", None)]
    assert rows[0]["file_count"] == 1 and rows[0]["size"] == 1 and rows[0]["deleted_by"] == "owner"
    gone = data(ui, "projects/study/artifacts/a/versions/1", cookie)
    assert gone["version"]["status"] == "deleted" and gone["consumers"] == ["study:b@v1"] and gone["deleted"] == ["study:a@v1"]
    assert data(ui, "projects/study/artifacts/b/versions/1", cookie)["deleted"] == ["study:a@v1"]
    nodes = data(ui, "lineage?ref=study:b@v1", cookie)["nodes"]
    assert nodes["study:a@v1"] == {"inputs": None, "consumers": None, "deleted": True} and "deleted" not in nodes["study:b@v1"]
    assert data(ui, "lineage?ref=study:a@v1", cookie)["nodes"]["study:a@v1"]["deleted"] is True
    status = call(ui, "GET", "/files/study/a@v1//f", token=None, headers=cookie)[0]
    assert status == 410
