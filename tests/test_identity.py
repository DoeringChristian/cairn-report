"""Version identity: ids (64 random hex digits), repo ids, references by id, and the id-keyed read-only folders."""

import json
import multiprocessing
import socket
import threading
from pathlib import Path

import pytest

import cairn
from cairn import LocalRepo
from cairn.client.http import Client
from cairn.client.remote import Remote
from cairn.server import make_server
from cairn.store.errors import Conflict
from cairn.store.tokens import init_root
from cairn.store import refs
from cairn.store.errors import InvalidRef
from cairn.store.ids import ID, chosen, is_id, new_id, valid_id

from test_store import env, folder, put  # noqa: F401


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    """A store server on a fixed port that can be stopped and started again (the same root and token)."""

    def __init__(self, root: Path, port: int | None = None):
        self.root, self.port = root, port or free_port()
        self.token = (
            (root / "token").read_text().strip() if (root / "token").exists() else init_root(root).read_text().strip()
        )
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

    def remote(self, project="study") -> Remote:
        return Remote(Client(self.url, self.token), project)

    def versions(self, name="data"):
        try:
            return cairn.LocalRepo(self.root).versions("study", name)
        except cairn.NotFound:
            return []


# ---------------------------------------------------------------------- ids

A = "a" * 64
B = "0123456789abcdef" * 4
DIGITS = "1234" + "0" * 60  # an id made of decimal digits only is still an id


def test_ids_are_64_lowercase_hex_digits_random_and_carry_no_order():
    made = [new_id() for _ in range(20_000)]
    assert len(set(made)) == len(made)
    assert all(ID.fullmatch(id) and is_id(id) and valid_id(id) == id for id in made)
    assert made != sorted(made)  # random: no time and no order in the digits
    assert len({id[:4] for id in made}) > 10_000  # the leading digits are spread too, not a clock
    for bad in ("a" * 63, "a" * 65, A.upper(), "g" * 64, 12, None, f"{A} "):
        assert not is_id(bad)
        with pytest.raises(ValueError):
            valid_id(bad)


def test_chosen_hands_out_the_given_ids_first_then_random_ones():
    with chosen(A, B):
        assert new_id() == A and new_id() == B
        assert new_id() not in (A, B)
    with chosen(A, B):
        assert new_id() == A
    assert new_id() != B  # an id the block did not use is dropped when it ends
    with pytest.raises(ValueError):
        with chosen("not-an-id"):
            pass
    with chosen(DIGITS):
        assert new_id() == DIGITS


def test_refs_accept_ids_and_reserve_their_form():
    ref = refs.parse(f"{A}//a.txt")
    assert (ref.name, ref.id, ref.version, ref.whole, ref.exact, ref.path) == (None, A, None, True, True, "a.txt")
    assert str(ref) == f"{A}//a.txt"
    bare = refs.parse(A)
    assert bare.name is None and bare.id == A and bare.selector is None and bare.word is None
    assert refs.parse(A.upper()).id == A  # case-insensitive, kept lowercase
    prefix = refs.parse("1234")  # 4 digits or more: an id prefix, never a name
    assert prefix.id == "1234" and prefix.name is None and not prefix.whole and not prefix.exact
    assert refs.parse("abc").name == "abc"  # 3 hex digits are a name
    assert refs.parse("a@v3").exact and not refs.parse("a@best").exact and refs.parse("a@best").id is None
    for word in (A, A.upper(), "cafe", "1234"):
        with pytest.raises(InvalidRef, match="hex digits"):
            refs.valid_word(word, "alias")
    for text in (f"a@{A}", f"a@{A.upper()}", "a@3f9a2c1", f"{A}@v1", "a" * 65):
        with pytest.raises(InvalidRef):
            refs.parse(text)
    assert refs.parse("a@2026-10-15_07-49-37").word == "2026-10-15_07-49-37"  # timestamps stay labels


# ------------------------------------------------------------------ the store


def test_store_id_and_version_ids(tmp_path):
    local = LocalRepo(tmp_path / "s")
    assert ID.fullmatch(local.id) and json.loads((tmp_path / "s" / "repo.json").read_text())["id"] == local.id
    assert LocalRepo(tmp_path / "s").id == local.id and LocalRepo(tmp_path / "t").id != local.id
    local.create_project("p")
    with chosen(A):
        one = local.publish("p", "run", put(local, {"a.txt": "1"}))
    manifest = local.get_version("p", "run", one)
    assert manifest["id"] == A and local.get_version("p", "run", A) == manifest
    assert local.resolve("p", A)[1]["version"] == 1
    short = local.short_ids("p", [A])[A]
    assert short == A[: refs.SHORT]
    assert local.resolve("p", short) == ("run", manifest)
    assert b"".join(local.read_file("p", f"{A}//a.txt").chunks) == b"1"
    assert local.read_file("p", f"{A}//a.txt").immutable


def test_client_supplied_ids_are_idempotent_and_conflicts_refused(tmp_path):
    local = LocalRepo(tmp_path / "s")
    local.create_project("p")
    entries, version_id = put(local, {"a.txt": "1"}), new_id()
    assert local.publish("p", "run", entries, id=version_id, metadata={"k": 1}) == 1
    assert local.publish("p", "run", entries, id=version_id, metadata={"k": 1}) == 1  # sent again: the same version
    assert local.get_version("p", "run", 1)["id"] == version_id
    with pytest.raises(Conflict):
        local.publish("p", "run", put(local, {"a.txt": "other"}), id=version_id)
    running = new_id()
    number = local.start_version("p", "run", label="live", id=running)
    assert local.start_version("p", "run", label="live", id=running) == number == 2
    # started again with other metadata (its first answer was lost, the metadata changed since): a running version takes it
    assert local.start_version("p", "run", label="live", metadata={"k": 2}, id=running) == 2
    assert local.get_version("p", "run", 2)["metadata"] == {"k": 2}
    local.fail_version("p", "run", 2)
    with pytest.raises(Conflict):  # a final one does not
        local.start_version("p", "run", label="other", id=running)
    for bad in ("not-an-id", A.upper(), A[:10]):
        with pytest.raises(ValueError):
            local.start_version("p", "run", id=bad)


# --------------------------------------------------------------------- cache


def test_read_only_folders_are_keyed_by_id_and_two_repos_never_collide(env, tmp_path, monkeypatch):
    paths = []
    for name, text in (("one", "first store"), ("two", "second store")):
        repo = cairn.connect(tmp_path / name)
        local = repo.backend.local
        local.publish("data", put(local, {"a.txt": text}))
        path = repo.pull("data@v1", record=False)
        assert (path / "a.txt").read_text() == text
        assert path == tmp_path / name / "versions" / local.get_version("data", 1)["id"] / "files"
        paths.append(path)
    assert paths[0] != paths[1]
    assert (paths[0] / "a.txt").read_text() == "first store"  # the second pull did not touch the first


def _pull_in_a_process(url, token, ref, cache, barrier, results):
    import os

    os.environ["CAIRN_DIR"] = cache
    import cairn

    repo = cairn.connect(f"{url}/study", token=token)
    barrier.wait()
    path = repo.pull(ref, record=False)
    results.put({str(file.relative_to(path)): file.read_text() for file in sorted(path.rglob("*.txt"))})


def test_concurrent_pulls_into_one_repo(tmp_path, monkeypatch, env):
    """Seven processes pull the same version into one repo at the same moment (as Slurm jobs sharing a project would)."""
    server = Server(tmp_path / "root").start()
    try:
        remote = server.remote()
        files = {f"part{index}/f{index}.txt": f"content {index} " * 200 for index in range(24)}
        cairn.connect(f"{server.url}/study", token=server.token).artifact("data").publish(
            folder(tmp_path, "data", files)
        )
        cache = tmp_path / "shared-cache"
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(7), context.Queue()
        processes = [
            context.Process(
                target=_pull_in_a_process, args=(server.url, server.token, "data@v1", str(cache), barrier, results)
            )
            for _ in range(7)
        ]
        for process in processes:
            process.start()
        answers = [results.get(timeout=120) for _ in processes]
        for process in processes:
            process.join(timeout=60)
            assert process.exitcode == 0
        assert all(answer == files for answer in answers)
        assert not [path for path in cache.rglob("*") if ".tmp-" in path.name]  # no partial folders or blobs remain
        [folder_] = list((cache / "versions").iterdir())
        assert folder_.name == remote.get_version("data", 1)["id"]
    finally:
        server.stop()


# -------------------------------------------------------------------- server


def test_server_keeps_client_ids_and_exposes_the_repo_id(tmp_path, env):
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
            connection.close()
            return response.status, json.loads(data) if data.startswith(b"{") or data.startswith(b"[") else data

        assert call("GET", "/api/v3/repo", token=None)[0] == 401
        status, answer = call("GET", "/api/v3/repo")
        assert status == 200 and answer == {"id": LocalRepo(server.root).id, "cairn": cairn.__version__, "api": 3}
        assert answer["cairn"] == "0.15.0"
        assert remote.id == answer["id"]
        status, old = call("GET", "/api/v2/repo")  # a Cairn 0.14 client is told to upgrade
        assert status == 400 and "upgrad" in json.dumps(old)
        remote.create()
        sha = __import__("hashlib").sha256(b"x").hexdigest()
        remote.put_blob(sha, __import__("io").BytesIO(b"x"), 1)
        entries, version_id = [{"path": "x.txt", "sha256": sha, "size": 1}], new_id()
        path = "/api/v3/projects/study/artifacts/data/publish"
        status, first = call("POST", path, {"id": version_id, "entries": entries})
        assert status == 201 and first == {"version": 1, "id": version_id, "created": True}
        status, again = call("POST", path, {"id": version_id, "entries": entries})
        assert status == 200 and again == {"version": 1, "id": version_id, "created": False}
        assert call("POST", path, {"id": version_id, "entries": entries, "metadata": {"other": 1}})[0] == 409
        assert call("POST", path, {"id": "nope", "entries": entries})[0] == 400
        running = new_id()
        status, started = call(
            "POST", "/api/v3/projects/study/artifacts/data/versions", {"id": running, "label": "live"}
        )
        assert status == 201 and started == {"version": 2, "id": running}
        assert (
            call("POST", "/api/v3/projects/study/artifacts/data/versions", {"id": running, "label": "live"})[1][
                "version"
            ]
            == 2
        )
        by_id = call("GET", f"/api/v3/projects/study/artifacts/data/versions/{version_id}")[1]
        assert by_id["version"] == 1 and by_id["id"] == version_id
        assert call("GET", "/api/v3/projects/study/artifacts/data/versions/v1")[1] == by_id
        assert call("GET", "/api/v3/projects/study/artifacts/data/versions/1")[0] == 400  # v<n> or a whole id
        assert call("GET", f"/api/v3/projects/study/resolve?ref={version_id}")[1]["version"]["id"] == version_id
        short = remote.short_ids([version_id])[version_id]
        assert short == version_id[: refs.SHORT]
        assert call("POST", "/api/v3/projects/study/short-ids", {"ids": [version_id]})[1] == {
            "ids": {version_id: short}
        }
        assert call("GET", f"/api/v3/projects/study/resolve?ref={short}")[1] == {"name": "data", "version": by_id}
        status, body = call("GET", f"/files/study/{version_id}//x.txt")
        assert status == 200 and body == b"x"
        assert remote.get_version("data", version_id)["version"] == 1
    finally:
        server.stop()
