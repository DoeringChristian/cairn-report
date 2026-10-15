"""Benchmark a store server (`cairn server`), as a separate process, over HTTP.

    taskpolicy -b python bench/bench_store.py [--size-mb 256] [--clients 32] [--procs 4] [--json out.json]

Scenarios (each prints a row of the table at the end):

  publish/distinct, publish/same   `--publishes` publishes of 3 small files each from `--clients` concurrent
                                   clients (spread over `--procs` processes), each one the full protocol: POST
                                   blobs/missing, PUT the 3 blobs, POST publish; to distinct artifacts, then
                                   all to one artifact (every publish a new version)
  upload                           PUT one `--size-mb` random file (streamed, hash-verified by the server)
  pull/cold, pull/warm             GET that file from a freshly started server, then again (`--repeats` times);
                                   the OS page cache holds the file both times (dropping it needs root)
  pull/client                      the same file through the real client (`RemoteStore.blob_file`: download,
                                   verify, place in the cache)
  range                            `--ranges` random 64 KB Range requests on that file from `--meta-clients` clients
  meta/*                           the UI's metadata queries on a project of `--artifacts` artifacts with
                                   `--versions` versions each (seeded directly into the folder before the server
                                   starts): projects, the artifact listing, one summary, versions, lineage

Every client keeps one HTTP/1.1 connection (keep-alive). Latencies are per request (per publish for publish/*).
The script is not a test (pytest does not collect it); it uses only the standard library and cairn.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PROJECT = "bench"


# ------------------------------------------------------------------ helpers


class Connection:
    """One keep-alive connection; reconnects if the server closed it."""

    def __init__(self, port: int, token: str):
        self.port, self.token, self.connection = port, token, None

    def request(self, method: str, path: str, body=None, headers=None, sink: bool = False):
        headers = {"Authorization": f"Bearer {self.token}", **(headers or {})}
        for attempt in range(2):
            if self.connection is None:
                self.connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=600)
            try:
                self.connection.request(method, path, body=body, headers=headers)
                response = self.connection.getresponse()
                if sink:
                    size = 0
                    while chunk := response.read(1 << 20):
                        size += len(chunk)
                    data = size
                else:
                    data = response.read()
                if response.getheader("Connection", "").lower() == "close" or response.will_close:
                    self.connection.close()
                    self.connection = None
                return response.status, data
            except (ConnectionError, http.client.RemoteDisconnected, http.client.CannotSendRequest, BrokenPipeError):
                self.connection.close()
                self.connection = None
                if attempt or body is not None and not isinstance(body, (bytes, str)):
                    raise
        raise RuntimeError("unreachable")

    def json(self, method: str, path: str, value=None, expect=(200,)):
        body = None if value is None else json.dumps(value).encode()
        status, data = self.request(method, path, body, {"Content-Type": "application/json"} if body else None)
        if status not in expect:
            raise RuntimeError(f"{method} {path}: HTTP {status} {data[:300]!r}")
        return json.loads(data) if data else None


def summarize(name: str, latencies: list[float], elapsed: float, count: int | None = None, unit: str = "req/s",
              extra: str = "") -> dict:
    latencies = sorted(latencies)
    count = len(latencies) if count is None else count

    def quantile(q):
        if not latencies:
            return float("nan")
        return latencies[min(len(latencies) - 1, int(q * len(latencies)))] * 1000

    return {"name": name, "count": count, "seconds": elapsed, "rate": count / elapsed if elapsed else float("nan"),
            "unit": unit, "p50_ms": quantile(0.50), "p95_ms": quantile(0.95), "extra": extra}


# ------------------------------------------------------------------ server


def start_server(root: Path, port: int) -> subprocess.Popen:
    command = [sys.executable, "-m", "cairn", "server", str(root), "--port", str(port), "--no-ui"]
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=environment)
    deadline = time.time() + 60
    for line in process.stdout:
        if line.startswith("Token:") or time.time() > deadline:
            break
    threading.Thread(target=lambda: [None for _ in process.stdout], daemon=True).start()
    for _ in range(200):
        try:
            http.client.HTTPConnection("127.0.0.1", port, timeout=1).request("GET", "/")
            return process
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("the server did not start")


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------------ publishes


def _publish_worker(port: int, token: str, jobs: list[tuple[str, int]], threads: int, seed: str) -> list[float]:
    """Run `jobs` ((artifact, index) pairs) with `threads` clients; returns per-publish latencies."""
    local = threading.local()
    latencies: list[float] = []
    lock = threading.Lock()

    def one(job):
        artifact, index = job
        if not hasattr(local, "connection"):
            local.connection = Connection(port, token)
        connection = local.connection
        files = {f"file{k}.txt": f"{seed} {artifact} {index} {k} ".encode() * 40 for k in range(3)}
        entries = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for path, data in files.items()]
        started = time.perf_counter()
        missing = set(connection.json("POST", "/api/v2/blobs/missing", {"sha256": [e["sha256"] for e in entries]})["missing"])
        for entry, data in zip(entries, files.values()):
            if entry["sha256"] in missing:
                status, body = connection.request("PUT", f"/api/v2/blobs/{entry['sha256']}", data,
                                                  {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))})
                if status not in (200, 201):
                    raise RuntimeError(f"PUT blob: HTTP {status} {body[:200]!r}")
        connection.json("POST", f"/api/v2/projects/{PROJECT}/artifacts/{quote(artifact, safe='')}/publish",
                        {"entries": entries}, expect=(200, 201))
        elapsed = time.perf_counter() - started
        with lock:
            latencies.append(elapsed)

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(one, jobs))
    return latencies


def bench_publishes(port: int, token: str, args, same: bool) -> dict:
    count = args.publishes
    jobs = [("pub/same" if same else f"pub/distinct-{index:05d}", index) for index in range(count)]
    procs = max(1, min(args.procs, args.clients))
    per = [jobs[index::procs] for index in range(procs)]
    threads = [args.clients // procs + (1 if index < args.clients % procs else 0) for index in range(procs)]
    seed = f"{time.time_ns()}"
    started = time.perf_counter()
    with ProcessPoolExecutor(procs) as pool:
        results = list(pool.map(_publish_worker, [port] * procs, [token] * procs, per, threads, [seed] * procs))
    elapsed = time.perf_counter() - started
    latencies = [value for part in results for value in part]
    return summarize(f"publish/{'same' if same else 'distinct'}", latencies, elapsed, unit="pub/s",
                     extra=f"{args.clients} clients, {procs} procs, 3 files x 1 KB, 5 requests each")


# ------------------------------------------------------------------ large files


def make_big_file(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    block = os.urandom(1 << 20)
    with path.open("wb") as handle:
        written = 0
        while written < size:
            # vary each MB cheaply so nothing deduplicates, but avoid urandom for every byte
            chunk = (written.to_bytes(8, "little") + block)[: min(1 << 20, size - written)]
            handle.write(chunk)
            digest.update(chunk)
            written += len(chunk)
    return digest.hexdigest()


def upload_big(port: int, token: str, path: Path, sha: str) -> dict:
    size = path.stat().st_size
    connection = Connection(port, token)
    started = time.perf_counter()
    with path.open("rb") as handle:
        status, data = connection.request("PUT", f"/api/v2/blobs/{sha}", handle,
                                          {"Content-Type": "application/octet-stream", "Content-Length": str(size)})
    elapsed = time.perf_counter() - started
    if status not in (200, 201):
        raise RuntimeError(f"upload: HTTP {status} {data[:200]!r}")
    connection.json("POST", f"/api/v2/projects/{PROJECT}/artifacts/big/publish",
                    {"entries": [{"path": "big.bin", "sha256": sha, "size": size}]}, expect=(200, 201))
    return summarize("upload", [elapsed], elapsed, count=1, unit="MB/s",
                     extra=f"{size / 1e6:.0f} MB") | {"rate": size / 1e6 / elapsed}


def pull_big(port: int, token: str, size: int, label: str, repeats: int) -> dict:
    connection = Connection(port, token)
    times = []
    for _ in range(repeats):
        started = time.perf_counter()
        status, received = connection.request("GET", f"/files/{PROJECT}/big@v1//big.bin", sink=True)
        times.append(time.perf_counter() - started)
        if status != 200 or received != size:
            raise RuntimeError(f"pull: HTTP {status}, {received} of {size} bytes")
    best = statistics.median(times)
    return summarize(label, times, sum(times), count=len(times), unit="MB/s",
                     extra=f"{size / 1e6:.0f} MB, median of {len(times)}") | {"rate": size / 1e6 / best}


def pull_client(port: int, token: str, size: int, sha: str) -> dict:
    from cairn.client.http import Client
    from cairn.client.remote import RemoteStore

    store = RemoteStore(Client(f"http://127.0.0.1:{port}", token), PROJECT)
    cache = Path(tempfile.mkdtemp(prefix="cairn-bench-cache-"))
    try:
        started = time.perf_counter()
        store.blob_file(PROJECT, "big", 1, {"path": "big.bin", "sha256": sha, "size": size}, cache)
        elapsed = time.perf_counter() - started
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    return summarize("pull/client", [elapsed], elapsed, count=1, unit="MB/s",
                     extra="RemoteStore.blob_file: download + verify + place") | {"rate": size / 1e6 / elapsed}


def bench_ranges(port: int, token: str, size: int, args) -> dict:
    rng = random.Random(1)
    span = 64 * 1024
    starts = [rng.randrange(0, max(1, size - span)) for _ in range(args.ranges)]
    local = threading.local()
    latencies, lock = [], threading.Lock()

    def one(first):
        if not hasattr(local, "connection"):
            local.connection = Connection(port, token)
        began = time.perf_counter()
        status, data = local.connection.request("GET", f"/files/{PROJECT}/big@v1//big.bin",
                                                headers={"Range": f"bytes={first}-{first + span - 1}"})
        elapsed = time.perf_counter() - began
        if status != 206 or len(data) != span:
            raise RuntimeError(f"range: HTTP {status}, {len(data)} bytes")
        with lock:
            latencies.append(elapsed)

    started = time.perf_counter()
    with ThreadPoolExecutor(args.meta_clients) as pool:
        list(pool.map(one, starts))
    return summarize("range", latencies, time.perf_counter() - started, extra=f"64 KB, {args.meta_clients} clients")


# ------------------------------------------------------------------ metadata


def seed_metadata(root: Path, artifacts: int, versions: int) -> None:
    """`artifacts` artifacts with `versions` versions each, written straight into the folder (no server)."""
    from cairn.store import LocalStore
    from cairn.store.blobs import BlobStore

    store = LocalStore(root)
    blobs = BlobStore(root)
    store.create_project(PROJECT, "Bench")
    payload = b"x" * 100
    sha = hashlib.sha256(payload).hexdigest()
    import io

    blobs.put_blob(sha, io.BytesIO(payload), len(payload))
    def name(index: int) -> str:
        return f"group{index % 20:02d}/item-{index:05d}"

    for index in range(artifacts):
        for number in range(1, versions):
            store.publish(PROJECT, name(index), [{"path": f"f{number}.txt", "sha256": sha, "size": len(payload)}],
                          metadata={"index": index, "version": number, "loss": index / (number + 1)})
    # the last version of every artifact consumes the next artifact's v1 (lineage has consumers to find)
    for index in range(artifacts):
        other = [f"{PROJECT}:{name((index + 1) % artifacts)}@v1"]
        store.publish(PROJECT, name(index), [{"path": "f-final.txt", "sha256": sha, "size": len(payload)}],
                      metadata={"index": index, "final": True}, inputs=other)


def bench_metadata(port: int, token: str, args) -> list[dict]:
    target = "group01/item-00001"
    encoded = quote(target, safe="")
    queries = [
        ("meta/projects", "/api/v2/projects", args.meta_light),
        ("meta/artifacts", f"/api/v2/projects/{PROJECT}/artifacts", args.meta_heavy),
        ("meta/summary", f"/api/v2/projects/{PROJECT}/artifacts/{encoded}", args.meta_light),
        ("meta/versions", f"/api/v2/projects/{PROJECT}/artifacts/{encoded}/versions", args.meta_light),
        ("meta/lineage", f"/api/v2/lineage?ref={quote(f'{PROJECT}:{target}@v1', safe='')}", args.meta_light),
        ("meta/resolve", f"/api/v2/resolve?ref={quote(f'{PROJECT}:{target}', safe='')}", args.meta_light),
    ]
    rows = []
    for name, path, count in queries:
        local = threading.local()
        latencies, lock = [], threading.Lock()

        def one(_):
            if not hasattr(local, "connection"):
                local.connection = Connection(port, token)
            began = time.perf_counter()
            status, data = local.connection.request("GET", path)
            elapsed = time.perf_counter() - began
            if status != 200:
                raise RuntimeError(f"{path}: HTTP {status} {data[:200]!r}")
            with lock:
                latencies.append(elapsed)

        Connection(port, token).request("GET", path)  # one warm-up (the first may load an index)
        started = time.perf_counter()
        with ThreadPoolExecutor(args.meta_clients) as pool:
            list(pool.map(one, range(count)))
        size = len(Connection(port, token).request("GET", path)[1])
        rows.append(summarize(name, latencies, time.perf_counter() - started,
                              extra=f"{args.meta_clients} clients, {size / 1e3:.0f} KB answer"))
    return rows


def first_request(port: int, token: str) -> dict:
    """The artifact listing as the first request to a freshly started server (any index is cold)."""
    began = time.perf_counter()
    status, data = Connection(port, token).request("GET", f"/api/v2/projects/{PROJECT}/artifacts")
    elapsed = time.perf_counter() - began
    if status != 200:
        raise RuntimeError(f"listing: HTTP {status}")
    return summarize("meta/artifacts-cold", [elapsed], elapsed, count=1, extra="first request after start")


# ------------------------------------------------------------------ main


def table(rows: list[dict]) -> str:
    lines = ["| scenario | count | rate | p50 ms | p95 ms | notes |", "|---|---:|---:|---:|---:|---|"]
    for row in rows:
        lines.append(f"| {row['name']} | {row['count']} | {row['rate']:.1f} {row['unit']} | {row['p50_ms']:.2f} | "
                     f"{row['p95_ms']:.2f} | {row['extra']} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size-mb", type=int, default=256, help="size of the large file (MB)")
    parser.add_argument("--publishes", type=int, default=200)
    parser.add_argument("--clients", type=int, default=32, help="concurrent publish clients")
    parser.add_argument("--procs", type=int, default=4, help="client processes the publish clients are spread over")
    parser.add_argument("--meta-clients", type=int, default=4, help="concurrent clients for ranges and metadata")
    parser.add_argument("--ranges", type=int, default=1000)
    parser.add_argument("--artifacts", type=int, default=2000)
    parser.add_argument("--versions", type=int, default=3)
    parser.add_argument("--meta-light", type=int, default=500, help="requests per light metadata query")
    parser.add_argument("--meta-heavy", type=int, default=40, help="requests of the artifact listing")
    parser.add_argument("--repeats", type=int, default=3, help="warm pulls")
    parser.add_argument("--only", default="publish,large,meta", help="comma-separated: publish, large, meta")
    parser.add_argument("--json", type=Path, help="also write the rows here")
    parser.add_argument("--keep", action="store_true", help="keep the temporary store")
    args = parser.parse_args()
    only = set(args.only.split(","))

    from cairn.store.tokens import init_root

    work = Path(tempfile.mkdtemp(prefix="cairn-bench-"))
    root = work / "store"
    token = init_root(root).read_text().strip()
    rows: list[dict] = []
    try:
        if "meta" in only:
            started = time.perf_counter()
            seed_metadata(root, args.artifacts, args.versions)
            print(f"seeded {args.artifacts} artifacts in {time.perf_counter() - started:.1f} s", file=sys.stderr)
        port = free_port()
        server = start_server(root, port)
        try:
            if "meta" in only:
                rows.append(first_request(port, token))
            Connection(port, token).json("POST", "/api/v2/projects", {"name": PROJECT}, expect=(200, 201))
            if "publish" in only:
                rows.append(bench_publishes(port, token, args, same=False))
                print(rows[-1], file=sys.stderr)
                rows.append(bench_publishes(port, token, args, same=True))
                print(rows[-1], file=sys.stderr)
            if "large" in only:
                big = work / "big.bin"
                size = args.size_mb * 1024 * 1024
                sha = make_big_file(big, size)
                rows.append(upload_big(port, token, big, sha))
                big.unlink()
                print(rows[-1], file=sys.stderr)
        finally:
            stop_server(server)
        server = start_server(root, port)
        try:
            if "large" in only:
                rows.append(pull_big(port, token, size, "pull/cold", 1))
                rows.append(pull_big(port, token, size, "pull/warm", args.repeats))
                rows.append(pull_client(port, token, size, sha))
                rows.append(bench_ranges(port, token, size, args))
                print(rows[-4:], file=sys.stderr)
            if "meta" in only:
                rows.extend(bench_metadata(port, token, args))
        finally:
            stop_server(server)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    header = (f"cairn bench: commit {commit}, {platform.platform()}, {platform.machine()}, {os.cpu_count()} cores, "
              f"Python {platform.python_version()}, {args.clients} publish clients / {args.procs} procs")
    print(header)
    print(table(rows))
    if args.json:
        args.json.write_text(json.dumps({"header": header, "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
