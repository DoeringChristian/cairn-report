"""Version identity: uids, store uids, references by uid, the upgrade of stores without uids, and the uid-keyed cache."""

import json
import multiprocessing
import socket
import threading
import time
from pathlib import Path

import pytest

import cairn
from cairn import LocalStore
from cairn.client.http import Client
from cairn.client.remote import RemoteStore
from cairn.server import make_server
from cairn.store.errors import Conflict
from cairn.store.tokens import init_root
from cairn.store import refs
from cairn.store.uids import UID, legacy, new_uid, uid_time

from test_store import env, folder, put  # noqa: F401


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    """A store server on a fixed port that can be stopped and started again (the same root and token)."""

    def __init__(self, root: Path, port: int | None = None):
        self.root, self.port = root, port or free_port()
        self.token = (root / "token").read_text().strip() if (root / "token").exists() else init_root(root).read_text().strip()
        self.url = f"http://127.0.0.1:{self.port}"
        self.httpd, self.requests = None, []

    def start(self) -> "Server":
        self.httpd = make_server(self.root, port=self.port)
        requests = self.requests
        self.httpd.log = requests.append
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def remote(self, project="study") -> RemoteStore:
        return RemoteStore(Client(self.url, self.token), project)

    def versions(self, name="data"):
        try:
            return cairn.LocalStore(self.root).versions("study", name)
        except cairn.NotFound:
            return []


# ---------------------------------------------------------------------- uids


def test_uids_are_unique_valid_and_time_ordered():
    made = [new_uid() for _ in range(20_000)]
    assert len(set(made)) == len(made)
    assert made == sorted(made)  # one process: strictly increasing, even within a millisecond
    assert all(UID.fullmatch(uid) for uid in made)
    assert abs(uid_time(made[-1]) - time.time()) < 5
    earlier = new_uid()
    time.sleep(0.01)
    assert new_uid() > earlier
    assert legacy("s/p/a/1", "2026-01-01T00:00:00+00:00") == legacy("s/p/a/1", "2026-01-01T00:00:00+00:00")
    assert legacy("s/p/a/1", None) != legacy("s/p/a/2", None) and UID.fullmatch(legacy("x", "bad date"))
    assert uid_time(legacy("x", "2026-01-01T00:00:00+00:00")) == 1767225600


def test_refs_accept_uids_and_reserve_their_form():
    uid = new_uid()
    ref = refs.parse(f"study:datasets/crops@{uid}//a.txt")
    assert (ref.project, ref.name, ref.uid, ref.version, ref.exact, ref.path) == ("study", "datasets/crops", uid, None, True, "a.txt")
    assert str(ref) == f"study:datasets/crops@{uid}//a.txt"
    assert refs.parse("a@v3").exact and not refs.parse("a@best").exact and refs.parse("a@best").uid is None
    for word in (uid, uid.upper()):
        with pytest.raises(ValueError, match="reserved"):
            refs.valid_word(word, "alias")
    with pytest.raises(ValueError):
        refs.parse(f"a@{uid.upper()}")
    refs.parse("a@2026-10-15_07-49-37")  # timestamps stay labels


# ------------------------------------------------------------------ the store


def test_store_uid_and_version_uids(tmp_path):
    local = LocalStore(tmp_path / "s")
    assert UID.fullmatch(local.uid) and json.loads((tmp_path / "s" / "store.json").read_text())["uid"] == local.uid
    assert LocalStore(tmp_path / "s").uid == local.uid and LocalStore(tmp_path / "t").uid != local.uid
    local.create_project("p")
    one = local.publish("p", "run", put(local, {"a.txt": "1"}))
    manifest = local.get_version("p", "run", one)
    assert UID.fullmatch(manifest["uid"]) and local.get_version("p", "run", manifest["uid"]) == manifest
    assert local.resolve(f"p:run@{manifest['uid']}")[2]["version"] == 1
    assert b"".join(local.read_file("p", "run", manifest["uid"], "a.txt").chunks) == b"1"
    assert local.read_file("p", "run", manifest["uid"], "a.txt").immutable


def test_client_supplied_uids_are_idempotent_and_conflicts_refused(tmp_path):
    local = LocalStore(tmp_path / "s")
    local.create_project("p")
    entries, uid = put(local, {"a.txt": "1"}), new_uid()
    assert local.publish("p", "run", entries, uid=uid, metadata={"k": 1}) == 1
    assert local.publish("p", "run", entries, uid=uid, metadata={"k": 1}) == 1  # sent again: the same version
    assert local.get_version("p", "run", 1)["uid"] == uid
    with pytest.raises(Conflict):
        local.publish("p", "run", put(local, {"a.txt": "other"}), uid=uid)
    running = new_uid()
    number = local.start_version("p", "run", label="live", uid=running)
    assert local.start_version("p", "run", label="live", uid=running) == number == 2
    with pytest.raises(Conflict):
        local.start_version("p", "run", label="other", uid=running)
    with pytest.raises(ValueError):
        local.start_version("p", "run", uid="not-a-uid")


def test_a_store_without_uids_is_upgraded_on_load(tmp_path, monkeypatch):
    root = tmp_path / "old"
    local = LocalStore(root)
    local.create_project("p")
    local.publish("p", "run", put(local, {"a.txt": "1"}))
    local.start_version("p", "run")  # a running version
    log = root / "projects/p/artifacts/run/versions.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    log.write_text("".join(json.dumps({key: value for key, value in row.items() if key != "uid"}) + "\n" for row in rows))
    (root / "store.json").unlink()  # as written before uids existed

    upgraded = LocalStore(root)
    assert (root / "store.json").is_file()
    first = {version["version"]: version["uid"] for version in upgraded.versions("p", "run")}
    assert all(UID.fullmatch(uid) for uid in first.values())
    persisted = [json.loads(line) for line in log.read_text().splitlines()]
    assert persisted[-1]["uid"] == first[1] and persisted[-1]["status"] == "completed"  # final versions keep their uid
    assert not any(row.get("uid") == first[2] for row in persisted)  # a running one is left to its writer
    assert {version["version"]: version["uid"] for version in LocalStore(root).versions("p", "run")} == first  # stable
    upgraded.complete_version("p", "run", 2)
    assert LocalStore(root).get_version("p", "run", 2)["uid"] == first[2]  # its writer kept the uid
    assert (cairn.pull(f"p:run@{first[1]}//a.txt", store=upgraded, record=False)).read_text() == "1"


# --------------------------------------------------------------------- cache


def test_cache_is_keyed_by_uid_and_two_stores_never_collide(env, tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_CACHE_DIR", str(tmp_path / "scratch"))
    paths = []
    for name, text in (("one", "first store"), ("two", "second store")):
        local = LocalStore(tmp_path / name, project="study")
        local.create_project("study")
        local.publish("study", "data", put(local, {"a.txt": text}))
        path = cairn.pull("data@v1", store=local, record=False)
        assert (path / "a.txt").read_text() == text
        assert path == tmp_path / "scratch" / "versions" / local.get_version("study", "data", 1)["uid"]
        paths.append(path)
    assert paths[0] != paths[1]
    assert (paths[0] / "a.txt").read_text() == "first store"  # the second pull did not touch the first


def _pull_in_a_process(url, token, ref, cache, barrier, results):
    import os

    os.environ["CAIRN_CACHE_DIR"] = cache
    import cairn
    from cairn.client.http import Client
    from cairn.client.remote import RemoteStore

    store = RemoteStore(Client(url, token), "study")
    barrier.wait()
    path = cairn.pull(ref, store=store, record=False)
    results.put({str(file.relative_to(path)): file.read_text() for file in sorted(path.rglob("*.txt"))})


def test_concurrent_pulls_into_one_cache(tmp_path, monkeypatch, env):
    """Seven processes pull the same version into one cache at the same moment (as Slurm jobs on NFS would)."""
    server = Server(tmp_path / "root").start()
    try:
        remote = server.remote()
        files = {f"part{index}/f{index}.txt": f"content {index} " * 200 for index in range(24)}
        cairn.publish("data", folder(tmp_path, "data", files), store=remote)
        cache = tmp_path / "shared-cache"
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(7), context.Queue()
        processes = [context.Process(target=_pull_in_a_process, args=(server.url, server.token, "data@v1", str(cache), barrier, results))
                     for _ in range(7)]
        for process in processes:
            process.start()
        answers = [results.get(timeout=120) for _ in processes]
        for process in processes:
            process.join(timeout=60)
            assert process.exitcode == 0
        assert all(answer == files for answer in answers)
        assert not [path for path in cache.rglob("*") if ".tmp-" in path.name]  # no partial folders or blobs remain
        [folder_] = list((cache / "versions").iterdir())
        assert folder_.name == remote.get_version("study", "data", 1)["uid"]
    finally:
        server.stop()


# -------------------------------------------------------------------- server


def test_server_keeps_client_uids_and_exposes_the_store_uid(tmp_path, env):
    import http.client

    server = Server(tmp_path / "root").start()
    try:
        remote = server.remote()

        def call(method, path, body=None, token=server.token):
            connection = http.client.HTTPConnection("127.0.0.1", server.port)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            if body is not None:
                body, headers["Content-Type"] = json.dumps(body), "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data.startswith(b"{") or data.startswith(b"[") else data

        assert call("GET", "/api/v2/store", token=None)[0] == 401
        status, answer = call("GET", "/api/v2/store")
        assert status == 200 and answer["uid"] == LocalStore(server.root).uid and answer["version"] == cairn.__version__
        assert remote.uid == answer["uid"]
        remote.create_project("study")
        sha = __import__("hashlib").sha256(b"x").hexdigest()
        remote.put_blob(sha, __import__("io").BytesIO(b"x"), 1)
        entries, uid = [{"path": "x.txt", "sha256": sha, "size": 1}], new_uid()
        path = "/api/v2/projects/study/artifacts/data/publish"
        status, first = call("POST", path, {"uid": uid, "entries": entries})
        assert status == 201 and first == {"version": 1, "uid": uid, "created": True}
        status, again = call("POST", path, {"uid": uid, "entries": entries})
        assert status == 200 and again == {"version": 1, "uid": uid, "created": False}
        assert call("POST", path, {"uid": uid, "entries": entries, "metadata": {"other": 1}})[0] == 409
        assert call("POST", path, {"uid": "nope", "entries": entries})[0] == 400
        running = new_uid()
        status, started = call("POST", "/api/v2/projects/study/artifacts/data/versions", {"uid": running, "label": "live"})
        assert status == 201 and started == {"version": 2, "uid": running}
        assert call("POST", "/api/v2/projects/study/artifacts/data/versions", {"uid": running, "label": "live"})[1]["version"] == 2
        assert call("GET", f"/api/v2/projects/study/artifacts/data/versions/{uid}")[1]["version"] == 1
        assert call("GET", f"/api/v2/resolve?ref=study:data@{uid}")[1]["version"]["uid"] == uid
        status, body = call("GET", f"/files/study/data@{uid}//x.txt")
        assert status == 200 and body == b"x"
        assert remote.get_version("study", "data", uid)["version"] == 1
    finally:
        server.stop()
