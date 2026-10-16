"""Loose performance guards for the store and its server: generous limits that catch a return of the old costs
(a listing that re-reads every artifact, publishes that serialize, downloads held in memory), not benchmarks.
The real numbers come from bench/bench_store.py (see bench/RESULTS.md)."""

import hashlib
import http.client
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cairn import LocalRepo
from cairn.store.tokens import init_root

from test_server import server  # noqa: F401  (a store server in a thread)


def request(port, token, method, path, body=None, headers=None, connection=None):
    own = connection is None
    connection = connection or http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    headers = {"Authorization": f"Bearer {token}", **(headers or {})}
    if isinstance(body, (dict, list)):
        body, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    if own:
        connection.close()
    return response.status, response.headers, data


def publish_small(port, token, connection, artifact, index):
    """The client's protocol for one publish of three small files: missing, PUT each, publish."""
    files = {f"f{k}.txt": f"{artifact} {index} {k} ".encode() * 30 for k in range(3)}
    entries = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for path, data in files.items()]
    status, _, data = request(port, token, "POST", "/api/v2/blobs/missing", {"sha256": [e["sha256"] for e in entries]},
                              connection=connection)
    assert status == 200
    missing = set(json.loads(data)["missing"])
    for entry, body in zip(entries, files.values()):
        if entry["sha256"] in missing:
            assert request(port, token, "PUT", f"/api/v2/blobs/{entry['sha256']}", body, connection=connection)[0] in (200, 201)
    status, _, data = request(port, token, "POST", f"/api/v2/projects/perf/artifacts/{artifact.replace('/', '%2F')}/publish",
                              {"entries": entries}, connection=connection)
    assert status in (200, 201), data
    return json.loads(data)["version"]


@pytest.mark.parametrize("same", [True, False], ids=["one-artifact", "distinct-artifacts"])
def test_concurrent_small_publishes(server, same):
    request(server.port, server.token, "POST", "/api/v2/projects", {"name": "perf"})
    local, opened = threading.local(), []

    def one(index):
        if not hasattr(local, "connection"):
            local.connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=120)
            opened.append(local.connection)
        return publish_small(server.port, server.token, local.connection, "shared" if same else f"a/n{index:03d}", index)

    started = time.perf_counter()
    with ThreadPoolExecutor(16) as pool:
        numbers = list(pool.map(one, range(100)))
    for connection in opened:
        connection.close()
    elapsed = time.perf_counter() - started
    assert elapsed < 60, f"100 small publishes took {elapsed:.1f} s"
    if same:
        assert sorted(numbers) == list(range(1, 101))  # every publish got its own number
        assert [v["version"] for v in LocalRepo(server.root).versions("perf", "shared")] == list(range(1, 101))
    else:
        assert set(numbers) == {1} and len(LocalRepo(server.root).artifacts("perf")) == 100


def seed_artifacts(root: Path, project: str, count: int) -> None:
    """`count` artifacts with one completed version each (as the server's owner: the server holds the root)."""
    import io

    store = LocalRepo(root)
    store.create_project(project)
    data = b"x"
    sha = hashlib.sha256(data).hexdigest()
    store.put_blob(sha, io.BytesIO(data), 1)
    for index in range(count):
        store.publish(project, f"group{index % 10}/item{index:04d}", [{"path": "x", "sha256": sha, "size": 1}], metadata={"index": index})


def test_listing_two_thousand_artifacts(server):
    seed_artifacts(server.root, "big", 2000)
    started = time.perf_counter()
    status, headers, data = request(server.port, server.token, "GET", "/api/v2/projects/big/artifacts")
    cold = time.perf_counter() - started
    assert status == 200 and len(json.loads(data)) == 2000
    assert cold < 30, f"the first listing took {cold:.1f} s"
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=60)
    times = []
    for _ in range(5):
        started = time.perf_counter()
        assert request(server.port, server.token, "GET", "/api/v2/projects/big/artifacts", connection=connection)[0] == 200
        times.append(time.perf_counter() - started)
    assert sorted(times)[2] < 1.0, f"a warm listing of 2000 artifacts took {sorted(times)[2]:.2f} s"  # was about 4 s
    status, again, _ = request(server.port, server.token, "GET", "/api/v2/projects/big/artifacts",
                               headers={"If-None-Match": headers["ETag"]}, connection=connection)
    assert status == 304 and again["ETag"] == headers["ETag"]
    connection.close()
    status, _, data = request(server.port, server.token, "GET", "/api/v2/projects")
    assert status == 200 and next(item for item in json.loads(data) if item["name"] == "big")["artifacts"] == 2000


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rss_kb(pid: int) -> int:
    return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() or 0)


@pytest.mark.parametrize("sendfile", ["1", "0"], ids=["sendfile", "pieces"])
def test_a_large_download_streams_without_loading_into_memory(tmp_path, sendfile):
    root, port = tmp_path / "root", free_port()
    token = init_root(root).read_text().strip()
    size = 50 * 1024 * 1024
    big = tmp_path / "big.bin"
    digest, block = hashlib.sha256(), os.urandom(1 << 20)
    with big.open("wb") as handle:
        for index in range(50):
            chunk = index.to_bytes(4, "little") + block[4:]
            handle.write(chunk)
            digest.update(chunk)
    sha = digest.hexdigest()
    environment = {**os.environ, "CAIRN_SENDFILE": sendfile}
    process = subprocess.Popen([sys.executable, "-m", "cairn", "server", str(root), "--port", str(port), "--no-ui"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=environment)
    try:
        for line in process.stdout:
            if line.startswith("Token:"):
                break
        assert request(port, token, "POST", "/api/v2/projects", {"name": "p"})[0] == 201
        with big.open("rb") as handle:  # streamed up, hashed while it arrives
            assert request(port, token, "PUT", f"/api/v2/blobs/{sha}", handle, {"Content-Length": str(size)})[0] == 201
        entries = [{"path": "big.bin", "sha256": sha, "size": size}]
        assert request(port, token, "POST", "/api/v2/projects/p/artifacts/big/publish", {"entries": entries})[0] == 201
        time.sleep(0.2)
        before, peak, done = rss_kb(process.pid), [0], threading.Event()

        def watch():
            while not done.is_set():
                peak[0] = max(peak[0], rss_kb(process.pid))
                time.sleep(0.01)

        watcher = threading.Thread(target=watch)
        watcher.start()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        connection.request("GET", "/files/p/big@v1//big.bin", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        received, check = 0, hashlib.sha256()
        while chunk := response.read(256 * 1024):
            received += len(chunk)
            check.update(chunk)
            time.sleep(0.0005)  # a slowish client: a server that buffered the file would hold it all now
        done.set()
        watcher.join()
        connection.close()
        assert response.status == 200 and received == size and check.hexdigest() == sha
        growth = (peak[0] - before) / 1024
        assert growth < 25, f"the server grew by {growth:.0f} MB while sending a 50 MB file"
    finally:
        process.terminate()
        process.wait(10)
        process.stdout.close()


def test_writes_of_a_second_process_are_seen_at_once(tmp_path):
    import io

    root = tmp_path / "root"
    store = LocalRepo(root)
    store.create_project("p")
    data = b"one"
    sha = hashlib.sha256(data).hexdigest()
    store.put_blob(sha, io.BytesIO(data), len(data))
    entries = [{"path": "a.txt", "sha256": sha, "size": len(data)}]
    store.publish("p", "x", entries, metadata={"n": 1})
    assert [item["name"] for item in store.summaries("p")] == ["x"]  # the listing is cached now
    script = (
        "import sys; from cairn import LocalRepo; s = LocalRepo(sys.argv[1]); e = [{'path': 'a.txt', 'sha256': sys.argv[2], 'size': 3}]\n"
        "s.publish('p', 'x', e, metadata={'n': 2}); s.publish('p', 'y/z', e); s.set_alias('p', 'x', 'best', 1)\n")
    subprocess.run([sys.executable, "-c", script, str(root), sha], check=True)
    assert [v["metadata"]["n"] for v in store.versions("p", "x")] == [1, 2]
    assert store.resolve("p", "x")[1]["version"] == 2 and store.resolve("p", "x@best")[1]["version"] == 1
    summaries = {item["name"]: item for item in store.summaries("p")}
    assert set(summaries) == {"x", "y/z"} and summaries["x"]["versions"] == 2 and summaries["x"]["aliases"] == {"latest": 2, "best": 1}
    assert summaries["x"]["metadata"] == {"n": 2}


def _start_versions(root, count, barrier, results):
    from cairn import LocalRepo

    store = LocalRepo(root)
    barrier.wait()
    results.put([store.start_version("p", "shared") for _ in range(count)])


def test_version_numbers_stay_unique_across_processes(tmp_path):
    root = tmp_path / "root"
    LocalRepo(root).create_project("p")
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(3), context.Queue()
    processes = [context.Process(target=_start_versions, args=(str(root), 15, barrier, results)) for _ in range(3)]
    for process in processes:
        process.start()
    numbers = [number for _ in processes for number in results.get(timeout=120)]
    for process in processes:
        process.join(60)
        assert process.exitcode == 0
    assert sorted(numbers) == list(range(1, 46))  # no number handed out twice, and none lost
    assert sorted(v["version"] for v in LocalRepo(root).versions("p", "shared")) == list(range(1, 46))


def test_caching_headers(server):
    request(server.port, server.token, "POST", "/api/v2/projects", {"name": "perf"})
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=60)
    assert publish_small(server.port, server.token, connection, "c", 0) == 1
    status, headers, _ = request(server.port, server.token, "GET", "/api/v2/projects/perf/artifacts/c/versions/1")
    assert status == 200 and "immutable" in headers["Cache-Control"]  # a completed version never changes
    status, headers, _ = request(server.port, server.token, "GET", "/files/perf/c@v1//f0.txt")
    assert status == 200 and "immutable" in headers["Cache-Control"]
    status, headers, _ = request(server.port, server.token, "GET", "/files/perf/c@latest//f0.txt")
    assert status == 200 and headers["Cache-Control"] == "no-cache"  # an alias moves
    status, headers, _ = request(server.port, server.token, "GET", "/api/v2/projects/perf/artifacts/c")
    assert status == 200 and headers["Cache-Control"] == "private, no-cache" and headers["ETag"]
    assert request(server.port, server.token, "GET", "/api/v2/projects/perf/artifacts/c",
                   headers={"If-None-Match": headers["ETag"]})[0] == 304
    publish_small(server.port, server.token, connection, "c", 1)
    assert request(server.port, server.token, "GET", "/api/v2/projects/perf/artifacts/c",
                   headers={"If-None-Match": headers["ETag"]})[0] == 200  # changed: a new ETag
    connection.close()


@pytest.mark.skipif(not hasattr(os, "sendfile"), reason="no os.sendfile here")
def test_downloads_use_sendfile_when_enabled(tmp_path, monkeypatch):
    from cairn.server import make_server

    root = tmp_path / "root"
    token = init_root(root).read_text().strip()
    httpd = make_server(root, port=0)
    httpd.log = None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        data = os.urandom(3 << 20)
        sha = hashlib.sha256(data).hexdigest()
        request(port, token, "POST", "/api/v2/projects", {"name": "p"})
        assert request(port, token, "PUT", f"/api/v2/blobs/{sha}", data, {"Content-Length": str(len(data))})[0] == 201
        request(port, token, "POST", "/api/v2/projects/p/artifacts/f/publish", {"entries": [{"path": "d.bin", "sha256": sha, "size": len(data)}]})
        for setting, zero_copy in (("1", 1), ("0", 0)):
            monkeypatch.setenv("CAIRN_SENDFILE", setting)
            before = httpd.app.zero_copy_sends
            status, headers, body = request(port, token, "GET", "/files/p/f@v1//d.bin")
            assert status == 200 and body == data and httpd.app.zero_copy_sends - before == zero_copy
            status, _, body = request(port, token, "GET", "/files/p/f@v1//d.bin", headers={"Range": "bytes=1000000-2999999"})
            assert status == 206 and body == data[1000000:3000000]
    finally:
        httpd.shutdown()
        httpd.server_close()
