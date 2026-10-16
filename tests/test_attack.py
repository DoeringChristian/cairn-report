"""Adversarial tests of the repo architecture (docs/architecture.md) with real processes: contention on one repo,
SIGKILL at random points, remote outages (refused, hung, erroring, slow, restarting), concurrent senders of one
outbox, a cluster's network filesystem, a remote added later, servers started and stopped, discovery, and odd input.

Every test asserts the spec's guarantees: no lost or duplicate versions, contiguous numbers in the source of truth,
`cairn verify` clean, contents byte-identical, `cairn status` accurate, no stray `.cairn/` folders. The numbers they
measure (latencies, WAL size, listing times, run-start delay) are printed (`pytest -s` shows them).

CAIRN_ATTACK_SECONDS sets how long the contention test runs (default 60)."""

from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import socket
import statistics
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import cairn
from cairn.store import LocalRepo

from test_identity import Server, free_port

pytest.importorskip("uvicorn")

SECONDS = float(os.environ.get("CAIRN_ATTACK_SECONDS", "60"))
BAD_WORDS = ("database is locked", "Traceback", "OperationalError", "Error:", "error:")


# ---------------------------------------------------------------------- helpers


def base_env(**extra) -> dict:
    env = os.environ.copy()
    env.setdefault("OMP_NUM_THREADS", "2")
    for name in ("CAIRN_DIR", "CAIRN_INPUTS", "CAIRN_RUN"):
        env.pop(name, None)
    env.update({key: str(value) for key, value in extra.items()})
    return env


def project_at(base: Path, name: str, remote: str | None = None, extra: str = "") -> Path:
    root = base / name
    root.mkdir(parents=True)
    lines = ["[repo]", 'project = "study"', "interval = 0.5"]
    if remote:
        lines.append(f'remote = "{remote}"')
    (root / "cairn.toml").write_text("\n".join(lines) + "\n" + extra)
    return root


def cli(project: Path, *args, env=None, timeout=300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "cairn", *args], cwd=project, capture_output=True, text=True,
                          timeout=timeout, env=env or base_env())


def script(project: Path, name: str, code: str) -> Path:
    path = project / name
    path.write_text(textwrap.dedent(code))
    return path


_LOGS = iter(range(10**9))


def spawn(project: Path, *argv, env=None, **kwargs) -> subprocess.Popen:
    """A process whose output goes to files (a pipe nobody reads would block it once full)."""
    number = next(_LOGS)
    logs = project / ".logs"
    logs.mkdir(exist_ok=True)
    out, err = logs / f"{number}.out", logs / f"{number}.err"
    with out.open("w") as stdout, err.open("w") as stderr:
        process = subprocess.Popen([sys.executable, *map(str, argv)], cwd=project, env=env or base_env(), stdout=stdout,
                                   stderr=stderr, text=True, **kwargs)
    process.logs = (out, err)
    return process


def finish(process: subprocess.Popen, timeout=600) -> tuple[str, str]:
    process.wait(timeout=timeout)
    return process.logs[0].read_text(), process.logs[1].read_text()


def bad_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if any(word in line for word in BAD_WORDS)]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stray_repos(base: Path, allowed: set[Path]) -> list[Path]:
    """Every `.cairn` folder under `base` that is not one of `allowed` (the drafts' own folders are inside a repo)."""
    found = []
    for directory, folders, _ in os.walk(base):
        here = Path(directory)
        if here.name == ".cairn":
            folders[:] = []
            continue
        for name in folders:
            if name == ".cairn" and (here / name).resolve() not in {path.resolve() for path in allowed}:
                found.append(here / name)
    return found


def check_numbers(repo: LocalRepo, project: str = "study") -> dict[str, list[dict]]:
    """Every artifact's versions are numbered 1..n without a gap or a duplicate, and every uid is unique."""
    everything, uids = {}, []
    for name in repo.artifacts(project) + _deleted_only(repo, project):
        versions = repo.versions(project, name)
        numbers = [version["version"] for version in versions]
        assert numbers == list(range(1, len(numbers) + 1)), (name, numbers)
        uids += [version["uid"] for version in versions]
        everything[name] = versions
    assert len(uids) == len(set(uids)), "a uid names two versions"
    return everything


def _deleted_only(repo: LocalRepo, project: str) -> list[str]:
    with repo._db.read() as connection:
        rows = connection.execute("SELECT name FROM artifacts WHERE project = ? AND versions = 0", (project,)).fetchall()
    return [row[0] for row in rows]


def verify_clean(project: Path, env=None) -> None:
    result = cli(project, "verify", env=env)
    assert result.returncode == 0, result.stdout + result.stderr


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] if ordered else float("nan")


def latency_table(samples: dict[str, list[float]]) -> str:
    lines = []
    for endpoint, values in sorted(samples.items()):
        lines.append(f"  {endpoint:<12} n={len(values):5d} p50={1000 * percentile(values, .5):7.1f} ms "
                     f"p95={1000 * percentile(values, .95):7.1f} ms max={1000 * max(values, default=0):7.1f} ms")
    return "\n".join(lines)


# ====================================================================== 1. contention on one repo

PUBLISHER = """
import hashlib, json, os, pathlib, random, sys, time, cairn
worker, seconds, big = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3] == "big"
rng = random.Random(worker)
log = open(f"published-{worker}.jsonl", "a")
artifacts = cairn.connect().project()
end, index = time.time() + seconds, 0
while time.time() < end:
    folder = pathlib.Path(f"work/p{worker}-{index}")
    (folder / "nested" / "deeper").mkdir(parents=True)
    files = {"value.txt": f"{worker}:{index}".encode(), "common.txt": b"the same in every version",
             "nested/deeper/x.json": json.dumps({"w": worker, "i": index}).encode(), "empty.txt": b""}
    if big and index % 4 == 1:
        files["big.bin"] = os.urandom(rng.randint(50, 200) << 20)
    for path, data in files.items():
        (folder / path).write_bytes(data)
    name = "shared" if index % 2 else f"own{worker}"
    began = time.time()
    version = artifacts.artifact(name).publish(folder, metadata={"worker": worker, "index": index})
    log.write(json.dumps({"name": name, "uid": version.uid, "number": version.number, "seconds": time.time() - began,
                          "files": {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}}) + "\\n")
    log.flush()
    for path in folder.rglob("*"):
        if path.is_file():
            path.unlink()
    index += 1
print("done", index)
"""

LIVE_RUN = """
import json, sys, time, cairn
index, seconds = int(sys.argv[1]), float(sys.argv[2])
run = cairn.init(name=f"live{index}", config={"i": index}, watch=True, system=0.5, code=False)
end, step = time.time() + seconds, 0
while time.time() < end:
    run.add({"loss": 1.0 / (step + 1), "step": step})
    print(f"step {step}", flush=True)
    step += 1
    time.sleep(0.01)
open(f"run-{index}.json", "w").write(json.dumps({"uid": run.uid, "steps": step}))
"""

MAINTENANCE = """
import subprocess, sys, time
seconds = float(sys.argv[1])
end, rounds = time.time() + seconds, 0
while time.time() < end:
    for command in (["gc"], ["prune", "--older-than", "0s"], ["gc", "--dry-run"], ["status"]):
        result = subprocess.run([sys.executable, "-m", "cairn", *command], capture_output=True, text=True)
        if result.returncode != 0:
            print("FAILED", command, result.returncode, result.stdout, result.stderr, flush=True)
        sys.stderr.write(result.stderr)
    rounds += 1
print("rounds", rounds)
"""

HAMMER = """
import http.client, json, random, sys, time
from urllib.parse import quote
port, seconds, out = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
samples, statuses, errors = {}, {}, []
rng = random.Random(0)

def get(kind, path, headers=None):
    began = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        connection.request("GET", path, headers={"X-Cairn": "1", **(headers or {})})
        response = connection.getresponse()
        body = response.read()
    except Exception as error:
        errors.append(f"{kind} {path}: {error!r}")
        return None, b""
    finally:
        connection.close()
    samples.setdefault(kind, []).append(time.perf_counter() - began)
    statuses.setdefault(kind, {}).setdefault(str(response.status), 0)
    statuses[kind][str(response.status)] += 1
    if response.status >= 500:
        errors.append(f"{kind} {path}: {response.status} {body[:300]!r}")
    return response.status, body

end = time.time() + seconds
while time.time() < end:
    get("projects", "/ui-api/projects")
    status, body = get("project", "/ui-api/projects/study")
    if status != 200:
        time.sleep(0.05)
        continue
    artifacts = json.loads(body)["artifacts"]
    if not artifacts:
        continue
    item = rng.choice(artifacts)
    name = quote(item["name"], safe="")
    status, body = get("artifact", f"/ui-api/projects/study/artifacts/{name}")
    if status != 200:
        continue
    versions = [version for version in json.loads(body)["versions"] if version.get("status") != "deleted"]
    if not versions:
        continue
    version = rng.choice(versions)
    get("version", f"/ui-api/projects/study/artifacts/{name}/versions/{version['version']}")
    path = quote(item["name"])
    if item["name"].startswith("runs/"):
        get("run", f"/ui-api/projects/study/artifacts/{name}/versions/{version['version']}/run")
        get("file-range", f"/files/study/{path}@v{version['version']}//metrics.jsonl", {"Range": "bytes=0-99"})
    else:
        get("file-range", f"/files/study/{path}@v{version['version']}//value.txt", {"Range": "bytes=0-2"})
        get("file", f"/files/study/{path}@v{version['version']}//nested/deeper/x.json")
        if item["name"] in ("own0", "own1"):
            get("big-range", f"/files/study/{path}@latest//big.bin", {"Range": "bytes=1000000-1999999"})
json.dump({"samples": samples, "statuses": statuses, "errors": errors}, open(out, "w"))
"""


def start_ui(project: Path, env=None) -> tuple[subprocess.Popen, int]:
    port = free_port()
    process = spawn(project, "-m", "cairn", "ui", "--no-browser", "--no-auth", "--port", port, env=env)
    wait_port(port, process)
    return process, port


def wait_port(port: int, process: subprocess.Popen | None = None, seconds: float = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(f"exited {process.returncode}: {process.logs[1].read_text()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"nothing listens on {port}")


def hammer(project: Path, port: int, seconds: float, out: str) -> dict:
    script(project, "hammer.py", HAMMER)
    process = spawn(project, "hammer.py", port, seconds, out)
    _, err = finish(process)
    assert process.returncode == 0, err
    return json.loads((project / out).read_text())


class WalWatch:
    """Samples the size of the repo's WAL file every 50 ms in a thread."""

    def __init__(self, repo: Path):
        self.path, self.sizes, self.stop = repo / "metadata.sqlite3-wal", [], threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(0.05):
            try:
                self.sizes.append(self.path.stat().st_size)
            except FileNotFoundError:
                self.sizes.append(0)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()


def test_contention_on_one_repo(tmp_path):
    """5 publishers (two with 50-200 MB files), 2 live runs, a gc/prune/status loop with a retention rule that deletes
    concurrently, and a client hammering the UI's API, all on one repo for CAIRN_ATTACK_SECONDS."""
    project = project_at(tmp_path, "busy")
    seed = tmp_path / "seed"
    (seed / "nested" / "deeper").mkdir(parents=True)
    (seed / "value.txt").write_text("seed")
    (seed / "nested" / "deeper" / "x.json").write_text("{}")
    assert cli(project, "publish", str(seed), "seed").returncode == 0
    keep = cli(project, "retention", "set", "study", "--prefix", "shared", "--keep-last", "5", "--keep-days", "0")
    assert keep.returncode == 0, keep.stderr
    for name, code in (("publisher.py", PUBLISHER), ("live.py", LIVE_RUN), ("maintenance.py", MAINTENANCE)):
        script(project, name, code)
    ui, port = start_ui(project)
    try:
        idle = hammer(project, port, 5, "idle.json")
        with WalWatch(project / ".cairn") as wal:
            processes = [spawn(project, "publisher.py", worker, SECONDS, "big" if worker < 2 else "small") for worker in range(5)]
            processes += [spawn(project, "live.py", index, SECONDS) for index in range(2)]
            processes += [spawn(project, "maintenance.py", SECONDS)]
            loaded = hammer(project, port, SECONDS, "load.json")
            outputs = [(process, *finish(process)) for process in processes]
        after = hammer(project, port, 5, "after.json")
    finally:
        ui.terminate()
        ui_out, ui_err = finish(ui, 30)

    problems = []
    for process, out, err in outputs:
        if process.returncode != 0 or "FAILED" in out:
            problems.append(f"{process.args}: exit {process.returncode}\n{out[-2000:]}\n{err[-4000:]}")
        problems += [f"{process.args[1]}: {line}" for line in bad_lines(err)]
    problems += [f"ui: {line}" for line in bad_lines(ui_err)]
    for phase in (idle, loaded, after):
        problems += phase["errors"]
    print("\nUI latency idle:\n" + latency_table(idle["samples"]))
    print("UI latency under load:\n" + latency_table(loaded["samples"]))
    print("UI latency after:\n" + latency_table(after["samples"]))
    print("UI statuses under load:", json.dumps(loaded["statuses"]))
    print(f"WAL max {max(wal.sizes) / 1e6:.2f} MB, final {wal.sizes[-1] / 1e6:.2f} MB, samples {len(wal.sizes)}")
    assert not problems, "\n".join(problems[:40])

    repo = LocalRepo(project / ".cairn")
    everything = check_numbers(repo)
    by_uid = {version["uid"]: (name, version) for name, versions in everything.items() for version in versions}
    published = [json.loads(line) for worker in range(5) for line in (project / f"published-{worker}.jsonl").read_text().splitlines()]
    timings = [row["seconds"] for row in published]
    print(f"publishes: {len(published)}, p50 {percentile(timings, .5):.2f} s, p95 {percentile(timings, .95):.2f} s, "
          f"max {max(timings):.2f} s; versions in the repo: {len(by_uid)}")
    assert len({row["uid"] for row in published}) == len(published)
    for row in published:
        name, version = by_uid[row["uid"]]  # never lost
        assert name == row["name"] and version["version"] == row["number"]
        if version["status"] == "deleted":
            assert name == "shared"  # only the retention rule deletes
            continue
        assert version["status"] == "completed"
        files = {entry["path"]: entry["sha256"] for entry in version["files"]}
        assert files == row["files"], row  # byte-identical: the hashes of what was written (verify re-hashes the blobs)
    for index in range(2):
        info = json.loads((project / f"run-{index}.json").read_text())
        name, version = by_uid[info["uid"]]
        assert version["status"] == "completed", version
        metrics = repo.read_file("study", name, f"v{version['version']}", "metrics.jsonl")
        lines = b"".join(metrics.chunks).decode().splitlines()
        assert len(lines) == info["steps"], (len(lines), info["steps"])
    assert sum(1 for version in everything["shared"] if version["status"] != "deleted") >= 5
    verify_clean(project)
    status = cli(project, "status")
    assert status.returncode == 0 and "no remote" in status.stdout
    assert not stray_repos(tmp_path, {project / ".cairn"})
    assert not list((project / ".cairn" / "heartbeats").glob("*")) if (project / ".cairn" / "heartbeats").is_dir() else True
    # the WAL is checkpointed: it never holds more than a few MB of pages (SQLite's autocheckpoint is 1000 pages)
    assert max(wal.sizes) < 64e6, max(wal.sizes)


# ---------------------------------------------------------------------- listings: many versions, many pending entries

FILL = """
import pathlib, sys, cairn
artifacts, per = int(sys.argv[1]), int(sys.argv[2])
project = cairn.connect().project()
for index in range(artifacts * per):
    folder = pathlib.Path(f"fill/{index}")
    folder.mkdir(parents=True)
    (folder / "value.txt").write_text(str(index))
    (folder / "nested").mkdir()
    (folder / "nested" / "x.json").write_text("{}")
    version = project.artifact(f"group{index % 10}/a{index % artifacts:03d}").publish(folder, metadata={"index": index})
print("filled")
"""


def timed_get(port: int, path: str) -> tuple[float, int]:
    import http.client

    began = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    connection.request("GET", path, headers={"X-Cairn": "1"})
    response = connection.getresponse()
    response.read()
    connection.close()
    return time.perf_counter() - began, response.status


def listing_times(project: Path, label: str, names: list[str], env=None) -> dict[str, float]:
    """Cold (first request of a new UI process) and warm listing times, and the CLI's."""
    from urllib.parse import quote

    times = {}
    began = time.perf_counter()
    result = cli(project, "artifacts", env=env)
    times["cli artifacts"] = time.perf_counter() - began
    assert result.returncode == 0, result.stderr
    began = time.perf_counter()
    result = cli(project, "versions", names[0], env=env)
    times["cli versions"] = time.perf_counter() - began
    assert result.returncode == 0, result.stderr
    ui, port = start_ui(project, env)
    try:
        times["ui project cold"], status = timed_get(port, "/ui-api/projects/study")
        assert status == 200
        warm = [timed_get(port, "/ui-api/projects/study")[0] for _ in range(5)]
        times["ui project warm"] = statistics.median(warm)
        times["ui artifact cold"], status = timed_get(port, f"/ui-api/projects/study/artifacts/{quote(names[0], safe='')}")
        assert status == 200
        times["ui artifact warm"] = statistics.median(
            timed_get(port, f"/ui-api/projects/study/artifacts/{quote(name, safe='')}")[0] for name in names[1:6])
        times["ui projects warm"] = statistics.median(timed_get(port, "/ui-api/projects")[0] for _ in range(5))
    finally:
        ui.terminate()
        finish(ui, 30)
    print(f"\nlistings, {label}:\n" + "\n".join(f"  {key:<18} {1000 * value:8.1f} ms" for key, value in times.items()))
    return times


def test_listings_with_3000_versions_and_500_pending_entries(tmp_path):
    project = project_at(tmp_path, "big")
    script(project, "fill.py", FILL)
    began = time.perf_counter()
    filled = cli(project, "fill.py", timeout=1800) if False else subprocess.run(
        [sys.executable, "fill.py", "300", "10"], cwd=project, capture_output=True, text=True, timeout=1800, env=base_env())
    assert filled.returncode == 0, filled.stderr
    print(f"\n3000 publishes in one process: {time.perf_counter() - began:.1f} s")
    names = [f"group{index % 10}/a{index:03d}" for index in range(6)]
    alone = listing_times(project, "3000 versions, 300 artifacts, no remote", names)
    assert alone["ui project warm"] < 0.5 and alone["ui artifact warm"] < 0.5

    # the same project with a remote that refuses connections, and 500 pending versions in the outbox
    port = free_port()
    offline = project_at(tmp_path, "offline", f"http://127.0.0.1:{port}")
    env = base_env(CAIRN_TOKEN="not-a-real-token")
    script(offline, "fill.py", FILL)
    began = time.perf_counter()
    filled = subprocess.run([sys.executable, "fill.py", "50", "10"], cwd=offline, capture_output=True, text=True, timeout=1800, env=env)
    assert filled.returncode == 0, filled.stderr
    print(f"500 offline publishes in one process: {time.perf_counter() - began:.1f} s")
    assert len(list((offline / ".cairn" / "outbox").iterdir())) == 500
    pending = listing_times(offline, "500 pending outbox entries, remote refuses", names, env)
    assert pending["ui project warm"] < 1.0 and pending["ui artifact warm"] < 1.0


# ====================================================================== remotes: a real `cairn server`, and a faulty one


class RealServer:
    """`cairn server <root> --port <port> --no-ui` as a process of its own (SIGKILL-able), started and stopped at will."""

    def __init__(self, root: Path, port: int | None = None):
        from cairn.store.tokens import init_root

        self.root, self.port = root, port or free_port()
        if not (root / "token").exists():
            init_root(root)
        self.token = (root / "token").read_text().strip()
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = None

    def start(self, env=None) -> "RealServer":
        self.process = spawn(self.root.parent, "-m", "cairn", "server", self.root, "--port", self.port, "--no-ui", env=env)
        wait_port(self.port, self.process)
        return self

    def stop(self, kill: bool = False) -> None:
        if self.process is not None:
            self.process.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
            self.process.wait(timeout=60)
            self.process = None

    def versions(self, name: str, project: str = "study") -> list[dict]:
        try:
            return LocalRepo(self.root).versions(project, name)
        except cairn.NotFound:
            return []


class FaultProxy:
    """A TCP proxy on its own port in front of `upstream` (a port), whose `mode` can change at any time:
    forward, hang (accept, read, never answer), error (HTTP 500), slow (wait `delay` s, then forward), refuse (close
    at once, like a server that restarts)."""

    def __init__(self, upstream: int | None = None, mode: str = "forward", delay: float = 5.0):
        self.upstream, self.mode, self.delay = upstream, mode, delay
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.held, self.connections, self.closed = [], 0, False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self.closed:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(client, self.mode), daemon=True).start()

    def _serve(self, client: socket.socket, mode: str) -> None:
        self.held.append(client)  # closed with the proxy at the latest
        if mode == "hang":
            return  # never answered
        if mode == "refuse":
            client.close()
            return
        if mode == "error":
            try:
                client.settimeout(5)
                client.recv(65536)
                client.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            except OSError:
                pass
            client.close()
            return
        if mode == "slow":
            time.sleep(self.delay)
        try:
            upstream = socket.create_connection(("127.0.0.1", self.upstream), timeout=5)
        except OSError:
            client.close()
            return
        self.held.append(upstream)
        upstream.settimeout(None)
        client.settimeout(None)

        def pipe(source, target):
            try:
                while data := source.recv(1 << 16):
                    target.sendall(data)
            except OSError:
                pass
            finally:
                for end in (source, target):
                    try:
                        end.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    end.close()

        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
        pipe(client, upstream)

    def close(self) -> None:
        self.closed = True
        self.listener.close()
        for client in self.held:
            client.close()


RUN_TIMING = """
import json, sys, time
began = time.perf_counter()
import cairn
imported = time.perf_counter()
run = cairn.init(name="timed", config={"x": 1}, code=False, system=False)
started = time.perf_counter()
for step in range(20):
    run.add({"loss": 1.0 / (step + 1), "step": step})
added = time.perf_counter()
with cairn.draft("result") as result:
    result.add_json("value", {"ok": True})
drafted = time.perf_counter()
run.finish()
finished = time.perf_counter()
print(json.dumps({"init": started - imported, "add": added - started, "draft": drafted - added, "finish": finished - drafted,
                  "uid": run.uid, "result": result.version.uid}))
"""


def timed_run(project: Path, env=None) -> dict:
    began = time.perf_counter()
    process = spawn(project, "timed.py", env=env)
    out, err = finish(process, 300)
    assert process.returncode == 0, err
    row = json.loads(out.strip().splitlines()[-1])
    row["process"] = time.perf_counter() - began
    return row


def timed_publish(project: Path, index: int, env=None) -> float:
    data = project / f"data-{index}"
    data.mkdir()
    (data / "a.txt").write_text(f"publish {index}")
    began = time.perf_counter()
    result = cli(project, "publish", str(data), "data", env=env)
    assert result.returncode == 0, result.stderr
    return time.perf_counter() - began


HANG_LATER = """
import json, pathlib, time, cairn
run = cairn.init(name="later", code=False, system=False)
pathlib.Path("started").write_text(run.uid)
while not pathlib.Path("go").exists():
    time.sleep(0.05)
run.add({"x": 1})
began = time.perf_counter()
with cairn.draft("out") as out:
    out.add_json("v", {"a": 1})
drafted = time.perf_counter()
run.finish()
print(json.dumps({"draft": drafted - began, "finish": time.perf_counter() - drafted, "uid": run.uid, "out": out.version.uid}))
"""


def test_remote_outages_do_not_slow_runs_and_everything_is_sent_once(tmp_path):
    """The remote refuses, hangs (connected, never answering), errors (500), is slow (1 s per connection), and stops
    answering while a run runs; then it recovers. Runs and publishes are timed against a repo without a remote, and
    every version is sent exactly once at the end."""
    baseline = project_at(tmp_path, "baseline")
    script(baseline, "timed.py", RUN_TIMING)
    base_runs = [timed_run(baseline) for _ in range(2)]
    base_publish = [timed_publish(baseline, index) for index in range(2)]

    server = RealServer(tmp_path / "remote").start()
    proxy = FaultProxy(server.port, delay=1.0)
    env = base_env(CAIRN_TOKEN=server.token)
    project = project_at(tmp_path, "study", proxy.url)
    for name, code in (("timed.py", RUN_TIMING), ("later.py", HANG_LATER)):
        script(project, name, code)
    report, runs, results = [], [], []
    try:
        for index, mode in enumerate(("forward", "refuse", "hang", "error", "slow", "forward")):
            proxy.mode = mode
            row = timed_run(project, env)
            runs.append(row["uid"])
            results.append(row["result"])
            report.append((mode, row, timed_publish(project, index, env)))
        proxy.mode = "forward"  # the remote stops answering while a run runs
        began = time.perf_counter()
        later = spawn(project, "later.py", env=env)
        while not (project / "started").exists():
            assert later.poll() is None and time.perf_counter() - began < 60, finish(later)
            time.sleep(0.05)
        proxy.mode = "hang"
        (project / "go").write_text("")
        began = time.perf_counter()
        out, err = finish(later, 600)
        assert later.returncode == 0, err
        ended = time.perf_counter() - began
        hung = json.loads(out.strip().splitlines()[-1])
        runs.append(hung["uid"])
        proxy.mode = "forward"
        synced = cli(project, "sync", env=env)
        assert synced.returncode == 0, synced.stdout + synced.stderr
    finally:
        proxy.close()
    lines = [f"  {'baseline':<8} init {base_runs[-1]['init']:6.2f} s  draft {base_runs[-1]['draft']:6.2f} s  finish "
             f"{base_runs[-1]['finish']:6.2f} s  process {base_runs[-1]['process']:6.2f} s  cli publish {base_publish[-1]:6.2f} s"]
    for mode, row, published in report:
        lines.append(f"  {mode:<8} init {row['init']:6.2f} s  draft {row['draft']:6.2f} s  finish {row['finish']:6.2f} s  "
                     f"process {row['process']:6.2f} s  cli publish {published:6.2f} s")
    lines.append(f"  hangs while running: draft {hung['draft']:.2f} s, finish {hung['finish']:.2f} s, the rest of the process {ended:.2f} s")
    print("\nruns and publishes while the remote misbehaves:\n" + "\n".join(lines))

    # everything was sent exactly once, numbered contiguously by the remote, and the repo agrees
    remote = LocalRepo(server.root)
    check_numbers(remote)
    on_remote = {version["uid"]: version for name in remote.artifacts("study") for version in remote.versions("study", name)}
    for uid in runs + results + [hung["out"]]:
        assert on_remote[uid]["status"] == "completed", on_remote[uid]
    assert sorted(version["uid"] for version in remote.versions("study", "runs/timed")) == sorted(runs[:-1])  # numbered as they arrived
    assert len(remote.versions("study", "data")) == 6
    assert not (project / ".cairn" / "outbox").exists() or not list((project / ".cairn" / "outbox").iterdir())
    status = cli(project, "status", env=env)
    assert "nothing pending" in status.stdout, status.stdout + status.stderr
    verify_clean(project, env)
    local = LocalRepo(project / ".cairn")
    for name in ("runs/timed", "data", "result"):
        assert [(v["version"], v["uid"]) for v in local.versions("study", name)] == \
            [(v["version"], v["uid"]) for v in remote.versions("study", name)]
    server.stop()
    timings = {mode: (row, published) for mode, row, published in report}
    for mode in ("refuse", "hang", "error"):  # a remote that does not answer costs a run one short look at most
        row, published = timings[mode]
        assert row["init"] < 4 and row["draft"] < 1 and row["finish"] < 1 and row["process"] < 6, (mode, row)
        assert published < 6, (mode, published)
    assert hung["draft"] < 4 and ended < 20, (hung, ended)


# ====================================================================== 6. a remote added later, with a version running

LONG_RUN = """
import pathlib, sys, time, cairn
run = cairn.init(name="train", config={"lr": 0.1}, code=False, system=False)
data = cairn.use("data@best")
run.add({"loss": 1.0, "step": 0})
pathlib.Path("started").write_text(run.uid)
while not pathlib.Path("go").exists():
    time.sleep(0.05)
with cairn.draft("model") as model:
    model.add_json("weights", {"w": [1, 2, 3]})
print(run.uid, model.version.uid, flush=True)
"""

MADE_LOCALLY = """
import json, pathlib, cairn
repo = cairn.connect()
data = repo.project().artifact("data")
made = {}
for index in range(4):
    folder = pathlib.Path(f"d{index}")
    folder.mkdir()
    (folder / "x.txt").write_text(f"data {index}")
    made[f"data{index}"] = data.publish(folder, aliases=["best"] if index == 2 else ()).uid
data.version(4).delete(reason="a bad one", force=True)
with cairn.draft("fit") as fit:
    fit.add_json("uses", {"data": cairn.use("data@v1").ref})
made["fit"] = fit.version.uid
print(json.dumps(made))
"""


def test_a_remote_added_while_a_version_runs_takes_over_everything_once(tmp_path):
    server = RealServer(tmp_path / "remote").start()
    env = base_env(CAIRN_TOKEN=server.token)
    other = project_at(tmp_path, "other", server.url)  # the remote already has versions of data and runs/train
    for index in range(2):
        data = other / f"o{index}"
        data.mkdir()
        (data / "x.txt").write_text(f"other {index}")
        assert cli(other, "publish", str(data), "data", env=env).returncode == 0
        assert cli(other, "publish", str(data), "runs/train", env=env).returncode == 0

    project = project_at(tmp_path, "study")  # no remote yet
    script(project, "made.py", MADE_LOCALLY)
    made = subprocess.run([sys.executable, "made.py"], cwd=project, capture_output=True, text=True, env=base_env())
    assert made.returncode == 0, made.stderr
    made = json.loads(made.stdout)
    script(project, "long.py", LONG_RUN)
    running = spawn(project, "long.py", env=env)  # started without a remote: its run is numbered by the repo
    deadline = time.time() + 60
    while not (project / "started").exists():
        assert time.time() < deadline and running.poll() is None, finish(running)
        time.sleep(0.05)
    with open(project / "cairn.toml", "a") as handle:  # now the project gets a remote, while the run runs
        handle.write(f'remote = "{server.url}"\n')
    first = cli(project, "sync", env=env)
    print("\nfirst sync:", first.returncode, first.stdout, first.stderr)
    status = cli(project, "status", env=env)
    assert status.stdout.startswith("running\tstudy:runs/train\t-\t"), status.stdout  # the run the remote has not yet
    (project / "go").write_text("")
    out, err = finish(running, 120)
    assert running.returncode == 0, err
    run_uid, model_uid = out.split()
    second = cli(project, "sync", env=env)
    print("second sync:", second.returncode, second.stdout, second.stderr)
    assert second.returncode == 0, second.stdout + second.stderr

    remote = LocalRepo(server.root)
    check_numbers(remote)
    data = {version["uid"]: version for version in remote.versions("study", "data")}
    assert [data[made[f"data{index}"]]["version"] for index in range(3)] == [3, 4, 5]  # oldest first, after other's
    assert made["data3"] not in data  # deleted before the remote came: its files are gone, never sent
    assert remote.aliases("study", "data")["best"] == 5
    runs = {version["uid"]: version for version in remote.versions("study", "runs/train")}
    assert runs[run_uid]["status"] == "completed" and runs[run_uid]["inputs"] == ["study:data@v5"]
    model = remote.get_version("study", "model", model_uid)
    # its draft opened after the remote was added, so it went to the remote while its run was the repo's: it says so
    assert model["inputs"] == [] and "gets no run input" in err
    fit = remote.get_version("study", "fit", made["fit"])
    [fit_run] = fit["inputs"]
    assert remote.resolve(fit_run)[2]["inputs"] == ["study:data@v3"]
    local = LocalRepo(project / ".cairn")
    assert local.local_versions() == []
    for name in ("data", "runs/train", "model", "fit"):  # the repo took the remote's numbers over
        mine = {v["uid"]: v["version"] for v in local.versions("study", name)}
        theirs = {v["uid"]: v["version"] for v in remote.versions("study", name)}
        assert all(theirs[uid] == number for uid, number in mine.items()), name
    assert "nothing pending" in cli(project, "status", env=env).stdout
    verify_clean(project, env)
    again = cli(project, "sync", env=env)
    assert again.returncode == 0 and len(remote.versions("study", "data")) == 5
    server.stop()
