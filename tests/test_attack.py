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
import re
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
from cairn.store import OWN, LocalRepo

from test_identity import Server, free_port
from conftest import write_config, config_file

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
    lines = [f'[remote]\nurl = "{remote}/study"'] if remote else []  # a server URL: the project on it is `study`
    lines += ["[repo]", "interval = 0.5"]
    write_config(root, "\n".join(lines) + "\n" + extra)
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
    """(`project`: "study" on a server's repo, OWN ("") in a project's own repo.)"""
    """Every artifact's versions are numbered 1..n without a gap or a duplicate, and every uid is unique."""
    everything, uids = {}, []
    try:
        names = repo.artifacts(project) + _deleted_only(repo, project)
    except cairn.NotFound:  # no project: nothing was ever made
        return {}
    for name in names:
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
artifacts = cairn.connect()
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
port, seconds, out, project = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3], sys.argv[4]
server, token = (int(sys.argv[5]), sys.argv[6]) if len(sys.argv) > 6 else (None, None)
samples, statuses, errors = {}, {}, []
rng = random.Random(0)

def get(kind, path, headers=None, to=None):
    began = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", to or port, timeout=60)
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
    status, body = get("project", f"/ui-api/projects/{project}")
    if status != 200:
        time.sleep(0.05)
        continue
    artifacts = json.loads(body)["artifacts"]
    if not artifacts:
        continue
    item = rng.choice(artifacts)
    name = quote(item["name"], safe="")
    status, body = get("artifact", f"/ui-api/projects/{project}/artifacts/{name}")
    if status != 200:
        continue
    versions = [version for version in json.loads(body)["versions"] if version.get("status") != "deleted"]
    if not versions:
        continue
    version = rng.choice(versions)
    get("version", f"/ui-api/projects/{project}/artifacts/{name}/versions/{version['version']}")
    path = quote(item["name"])
    if item["name"].startswith("runs/"):
        get("run", f"/ui-api/projects/{project}/artifacts/{name}/versions/{version['version']}/run")
        get("file-range", f"/files/{project}/{path}@v{version['version']}//metrics.jsonl", {"Range": "bytes=0-99"})
    else:
        get("file-range", f"/files/{project}/{path}@v{version['version']}//value.txt", {"Range": "bytes=0-2"})
        get("file", f"/files/{project}/{path}@v{version['version']}//nested/deeper/x.json")
        if item["name"] == "shared":  # every fourth publish of a big publisher has a 50-200 MB file
            get("big-range", f"/files/{project}/{path}@v{version['version']}//big.bin", {"Range": "bytes=1000000-1999999"})
    if server:  # `cairn server` on the same repo, as another machine's client sees it
        auth = {"Authorization": f"Bearer {token}"}
        get("srv-list", f"/api/v2/projects/{project}/artifacts", auth, server)
        get("srv-versions", f"/api/v2/projects/{project}/artifacts/{name}/versions", auth, server)
        get("srv-file", f"/files/{project}/{path}@v{version['version']}//"
            + ("metrics.jsonl" if item["name"].startswith("runs/") else "value.txt"), {**auth, "Range": "bytes=0-2"}, server)
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


def hammer(project: Path, port: int, seconds: float, out: str, server: "RealServer | None" = None) -> dict:
    script(project, "hammer.py", HAMMER)
    process = spawn(project, "hammer.py", port, seconds, out, project.name, *((server.port, server.token) if server else ()))
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
    """4 publishers (two with 50-200 MB files), 2 live runs, a gc/prune/status loop with a retention rule that deletes
    concurrently, `cairn server` serving the same repo, and a client hammering the UI's API and the server's, all on one
    repo for CAIRN_ATTACK_SECONDS."""
    project = project_at(tmp_path, "busy")
    seed = tmp_path / "seed"
    (seed / "nested" / "deeper").mkdir(parents=True)
    (seed / "value.txt").write_text("seed")
    (seed / "nested" / "deeper" / "x.json").write_text("{}")
    assert cli(project, "publish", str(seed), "seed").returncode == 0
    keep = cli(project, "retention", "set", "--prefix", "shared", "--keep-last", "5", "--keep-days", "0")
    assert keep.returncode == 0, keep.stderr
    for name, code in (("publisher.py", PUBLISHER), ("live.py", LIVE_RUN), ("maintenance.py", MAINTENANCE)):
        script(project, name, code)
    ui, port = start_ui(project)
    server = RealServer(project / ".cairn", own=True).start()  # just another process on the repo (rule 1)
    try:
        idle = hammer(project, port, 5, "idle.json", server)
        with WalWatch(project / ".cairn") as wal:
            processes = [spawn(project, "publisher.py", worker, SECONDS, "big" if worker < 2 else "small") for worker in range(4)]
            processes += [spawn(project, "live.py", index, SECONDS) for index in range(2)]
            processes += [spawn(project, "maintenance.py", SECONDS)]
            loaded = hammer(project, port, SECONDS, "load.json", server)
            outputs = [(process, *finish(process)) for process in processes]
        after = hammer(project, port, 5, "after.json", server)
    finally:
        ui.terminate()
        ui_out, ui_err = finish(ui, 30)
        server.stop()

    problems = []
    for process, out, err in outputs:
        if process.returncode != 0 or "FAILED" in out:
            problems.append(f"{process.args}: exit {process.returncode}\n{out[-2000:]}\n{err[-4000:]}")
        problems += [f"{process.args[1]}: {line}" for line in bad_lines(err)]
    problems += [f"ui: {line}" for line in bad_lines(ui_err)]
    problems += [f"server: {line}" for line in bad_lines(server.process_logs()[1])]
    for phase in (idle, loaded, after):
        problems += phase["errors"]
    print("\nUI latency idle:\n" + latency_table(idle["samples"]))
    print("UI latency under load:\n" + latency_table(loaded["samples"]))
    print("UI latency after:\n" + latency_table(after["samples"]))
    print("UI statuses under load:", json.dumps(loaded["statuses"]))
    print(f"WAL max {max(wal.sizes) / 1e6:.2f} MB, final {wal.sizes[-1] / 1e6:.2f} MB, samples {len(wal.sizes)}")
    assert not problems, "\n".join(problems[:40])

    repo = LocalRepo(project / ".cairn")
    everything = check_numbers(repo, OWN)
    by_uid = {version["uid"]: (name, version) for name, versions in everything.items() for version in versions}
    published = [json.loads(line) for worker in range(4) for line in (project / f"published-{worker}.jsonl").read_text().splitlines()]
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
        metrics = repo.read_file(OWN, name, f"v{version['version']}", "metrics.jsonl")
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
project = cairn.connect()
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


def listing_times(project: Path, label: str, names: list[str], env=None, shown: str | None = None) -> dict[str, float]:
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
    project = shown or project.name  # the UI's one project: the remote's project, else the folder's name
    try:
        times["ui project cold"], status = timed_get(port, f"/ui-api/projects/{project}")
        assert status == 200
        warm = [timed_get(port, f"/ui-api/projects/{project}")[0] for _ in range(5)]
        times["ui project warm"] = statistics.median(warm)
        times["ui artifact cold"], status = timed_get(port, f"/ui-api/projects/{project}/artifacts/{quote(names[0], safe='')}")
        assert status == 200
        times["ui artifact warm"] = statistics.median(
            timed_get(port, f"/ui-api/projects/{project}/artifacts/{quote(name, safe='')}")[0] for name in names[1:6])
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
    pending = listing_times(offline, "500 pending outbox entries, remote refuses", names, env, shown="study")
    assert pending["ui project warm"] < 0.03 and pending["ui artifact warm"] < 0.03 and pending["ui projects warm"] < 0.03


# ====================================================================== remotes: a real `cairn server`, and a faulty one


class RealServer:
    """`cairn server <root> --port <port> --no-ui` as a process of its own (SIGKILL-able), started and stopped at will."""

    def __init__(self, root: Path, port: int | None = None, own: bool = False):
        """`own`: `root` is the repo of the project folder around it, served as `cairn server` there serves it (its one
        project under the folder's name)."""
        from cairn.store.tokens import init_root

        self.root, self.port, self.own = root, port or free_port(), own
        if not (root / "token").exists():
            init_root(root)
        self.token = (root / "token").read_text().strip()
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = None

    def start(self, env=None) -> "RealServer":
        root = () if self.own else (self.root,)
        self.process = spawn(self.root.parent, "-m", "cairn", "server", *root, "--port", self.port, "--no-ui", env=env)
        self.logs = self.process.logs
        wait_port(self.port, self.process)
        return self

    def process_logs(self) -> tuple[str, str]:
        return self.logs[0].read_text(), self.logs[1].read_text()

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


METHODS = {b"GET", b"POST", b"PUT", b"PATCH", b"DELETE", b"HEAD"}


class FaultProxy:
    """A TCP proxy on its own port in front of `upstream` (a port), whose `mode` can change at any time:
    forward, hang (accept, read, never answer), error (HTTP 500), slow (wait `delay` s per connection, then forward),
    slow-request (wait `delay` s before each request), refuse (close at once, like a server that restarts), throttle
    (`rate` bytes per second towards the remote), stall (stop forwarding, keep the connection). A kept-alive connection
    follows the mode at each new request (throttle and stall at every piece of data)."""

    def __init__(self, upstream: int | None = None, mode: str = "forward", delay: float = 5.0, rate: float = 2e6):
        self.upstream, self.mode, self.delay, self.rate = upstream, mode, delay, rate
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 << 10)  # like a network path, not a loopback
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

        def pipe(source, target, requests=False):
            paced = [None, 0]  # when throttling began, bytes sent since (sleeps are coarse in the background: keep the average)
            try:
                while data := source.recv(1 << 16):
                    if requests and data.split(b" ", 1)[0] in METHODS:  # a new request on a kept-alive connection
                        mode = self.mode
                        if mode == "hang":
                            while not self.closed:
                                time.sleep(0.1)
                            return
                        if mode == "refuse":
                            return
                        if mode == "error":
                            source.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                            return
                        if mode == "slow-request":
                            time.sleep(self.delay)
                    if requests and self.mode == "stall":  # bytes stop moving, the connection stays open
                        while not self.closed:
                            time.sleep(0.1)
                        return
                    if requests and self.mode == "throttle":  # bytes move at `rate` bytes per second on average
                        if paced[0] is None:
                            paced[0] = time.monotonic()
                        target.sendall(data)
                        paced[1] += len(data)
                        time.sleep(max(0.0, paced[0] + paced[1] / self.rate - time.monotonic()))
                        continue
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
        pipe(client, upstream, requests=True)

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
    """The remote refuses, hangs (connected, never answering), errors (500), is slow (1 s per connection, then 1 s per
    request), and stops answering while a run runs; then it recovers. Runs and publishes are timed against a repo without a remote, and
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
        for index, mode in enumerate(("forward", "refuse", "hang", "error", "slow", "slow-request", "forward")):
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
    assert len(remote.versions("study", "data")) == 7
    assert not (project / ".cairn" / "outbox").exists() or not list((project / ".cairn" / "outbox").iterdir())
    status = cli(project, "status", env=env)
    assert "nothing pending" in status.stdout, status.stdout + status.stderr
    verify_clean(project, env)
    local = LocalRepo(project / ".cairn")
    for name in ("runs/timed", "data", "result"):
        assert [(v["version"], v["uid"]) for v in local.versions(OWN, name)] == \
            [(v["version"], v["uid"]) for v in remote.versions("study", name)]
    server.stop()
    timings = {mode: (row, published) for mode, row, published in report}
    for mode in ("refuse", "hang", "error"):  # a remote that does not answer costs a run one short look at most
        row, published = timings[mode]
        assert row["init"] < 4 and row["draft"] < 1 and row["finish"] < 1 and row["process"] < 6, (mode, row)
        assert published < 6, (mode, published)
    assert hung["draft"] < 4 and ended < 20, (hung, ended)
    baseline_process = base_runs[-1]["process"]
    for mode in ("slow", "slow-request"):  # a slow remote that answers: a run waits a few seconds at most
        row, published = timings[mode]
        assert row["process"] < baseline_process + 6.5, (mode, row)  # about baseline + 5 s, with some slack
        assert published < 13, (mode, published)  # about 12 s


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
data = repo.artifact("data")
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
    with open(config_file(project), "a") as handle:  # now the project gets a remote, while the run runs
        handle.write(f'[remote]\nurl = "{server.url}/study"\n')
    first = cli(project, "sync", env=env)
    print("\nfirst sync:", first.returncode, first.stdout, first.stderr)
    status = cli(project, "status", env=env)
    assert re.search(r"\nrunning +runs/train +- ", status.stdout), status.stdout  # the run the remote has not yet
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
    assert runs[run_uid]["status"] == "completed" and runs[run_uid]["inputs"] == ["data@v5"]
    model = remote.get_version("study", "model", model_uid)
    # its draft opened after the remote was added, so it went to the remote while its run was the repo's: it says so
    assert model["inputs"] == [] and "gets no run input" in err
    fit = remote.get_version("study", "fit", made["fit"])
    [fit_run] = fit["inputs"]
    assert remote.resolve("study", fit_run)[1]["inputs"] == ["data@v3"]
    local = LocalRepo(project / ".cairn")
    assert local.local_versions(OWN) == []
    for name in ("data", "runs/train", "model", "fit"):  # the repo took the remote's numbers over
        mine = {v["uid"]: v["version"] for v in local.versions(OWN, name)}
        theirs = {v["uid"]: v["version"] for v in remote.versions("study", name)}
        assert all(theirs[uid] == number for uid, number in mine.items()), name
    assert "nothing pending" in cli(project, "status", env=env).stdout
    verify_clean(project, env)
    again = cli(project, "sync", env=env)
    assert again.returncode == 0 and len(remote.versions("study", "data")) == 5
    server.stop()


# ====================================================================== 2. SIGKILL at chosen and at random points

KILL_HOOK = '''
import os, signal
point = os.environ.get("CAIRN_KILL_AT")
if point:
    import cairn.client.sync as sync
    import cairn.client.remote as remote
    import cairn.store.outbox as outbox
    import cairn.store.local as local
    import cairn.store.blobs as blobs
    import cairn.store.folders as folders

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    def after(module, name, check=lambda *a, **k: True):
        original = getattr(module, name)
        def patched(*args, **kwargs):
            result = original(*args, **kwargs)
            if check(*args, **kwargs):
                die()
            return result
        setattr(module, name, patched)

    def before(module, name):
        original = getattr(module, name)
        def patched(*args, **kwargs):
            die()
        setattr(module, name, patched)

    if point == "outbox-written":  # the entry's files are written under the scratch name, not renamed yet
        original_rename = os.rename
        def rename(source, target, *args, **kwargs):
            if ".tmp-" in str(source) and "/outbox/" in str(target):
                die()
            return original_rename(source, target, *args, **kwargs)
        os.rename = rename
    elif point == "staged":  # the first blob is in the repo, nothing else
        after(blobs.BlobStore, "stage_file")
    elif point == "blobs-sent":  # the blobs are on the remote, the manifest is not
        after(sync.Sync, "_upload")
    elif point == "answered":  # the remote numbered the version; the repo has not recorded it
        after(remote.Remote, "publish")
        after(remote.Remote, "complete_version")
    elif point == "recorded":  # the repo has the remote's number; the outbox entry is still there
        after(sync.Sync, "record")
    elif point == "removing":  # the entry is marked sent, its folder not removed yet
        original_remove = folders.remove
        def remove(folder):
            if "/outbox/" in str(folder):
                die()
            return original_remove(folder)
        outbox.remove = remove
    elif point == "committing":  # inside the repo's publish transaction
        after(local.LocalRepo, "_completed")
    elif point == "started":  # a running version was created on the remote
        after(remote.Remote, "start_version")
    elif point == "files-added":  # a running version's files were sent
        after(remote.Remote, "add_files")
'''

KILL_PUBLISH = """
import pathlib, sys, cairn
folder = pathlib.Path(sys.argv[1])
version = cairn.connect().artifact("data").publish(folder)
print(version.uid, version.number)
"""

KILL_RUN = """
import sys, cairn
run = cairn.init(name="victim", code=False, system=False)
for step in range(5):
    run.add({"loss": 1.0 / (step + 1), "step": step})
with cairn.draft("out") as out:
    out.add_json("v", {"step": 5})
run.finish()
print(run.uid, out.version.uid)
"""


def kill_env(tmp_path: Path, **extra) -> dict:
    hook = tmp_path / "kill-hook"
    hook.mkdir(exist_ok=True)
    (hook / "sitecustomize.py").write_text(KILL_HOOK)
    env = base_env(**extra)
    env["PYTHONPATH"] = os.pathsep.join([str(hook), env.get("PYTHONPATH", "")])
    return env


def outbox_of(project: Path) -> list[str]:
    folder = project / ".cairn" / "outbox"
    return sorted(path.name for path in folder.iterdir() if ".tmp-" not in path.name) if folder.is_dir() else []  # scratch: prune's


@pytest.mark.parametrize("point", ["outbox-written", "staged", "blobs-sent", "answered", "recorded", "removing", "started",
                                   "files-added"])
def test_sigkill_at_each_step_of_a_sync_loses_and_doubles_nothing(tmp_path, point):
    """A publish and a run (with a draft) are killed at one step of writing or sending; the next command recovers: the
    remote has every version that got that far exactly once, failed runs are failed, the outbox is empty, verify is clean."""
    server = RealServer(tmp_path / "remote").start()
    env = base_env(CAIRN_TOKEN=server.token)
    killing = kill_env(tmp_path, CAIRN_TOKEN=server.token, CAIRN_KILL_AT=point)
    project = project_at(tmp_path, "study", server.url)
    script(project, "publish.py", KILL_PUBLISH)
    script(project, "run.py", KILL_RUN)
    finished, killed = [], 0
    for index in range(3):
        data = project / f"d{index}"
        data.mkdir()
        (data / "a.txt").write_text(f"version {index}")
        (data / "big.bin").write_bytes(os.urandom(3 << 20))
        result = subprocess.run([sys.executable, "publish.py", str(data)], cwd=project, capture_output=True, text=True,
                                env=killing, timeout=120)
        if result.returncode == -signal.SIGKILL:
            killed += 1
        else:
            assert result.returncode == 0, result.stderr
            finished.append(result.stdout.split()[0])
        run = subprocess.run([sys.executable, "run.py"], cwd=project, capture_output=True, text=True, env=killing, timeout=120)
        if run.returncode == -signal.SIGKILL:
            killed += 1
        else:
            assert run.returncode == 0, run.stderr
            finished += run.stdout.split()
    assert killed, f"nothing was killed at {point}"
    status = cli(project, "status", env=env)  # any command recovers
    assert status.returncode == 0, status.stderr
    synced = cli(project, "sync", env=env)
    assert synced.returncode == 0, synced.stdout + synced.stderr
    left = outbox_of(project)
    if left:  # only folders a crash kept from being removed after they were sent: prune removes them
        assert point == "removing" and all(json.loads((project / ".cairn" / "outbox" / uid / "manifest.json").read_text())["sync"]
                                           == "synced" for uid in left), (point, left)
        assert cli(project, "prune", env=env).returncode == 0
    assert outbox_of(project) == [], (point, outbox_of(project), cli(project, "status", env=env).stdout)
    assert "nothing pending" in cli(project, "status", env=env).stdout

    remote = LocalRepo(server.root)
    everything = check_numbers(remote)
    on_remote = {version["uid"]: version for versions in everything.values() for version in versions}
    for uid in finished:  # nothing finished is lost
        assert on_remote[uid]["status"] == "completed", (point, on_remote.get(uid))
    assert not [v for v in on_remote.values() if v["status"] == "running"], point  # killed runs are failed, not running
    assert len({version["uid"] for version in on_remote.values()}) == len(on_remote)
    data = everything.get("data", [])
    assert len({tuple(sorted((f["path"], f["sha256"]) for f in v["files"])) for v in data}) == len(data)  # no double version
    local = LocalRepo(project / ".cairn")
    for name, versions in everything.items():
        mine = {v["uid"]: v["version"] for v in local.versions(OWN, name)} if name in local.artifacts(OWN) else {}
        assert all(on_remote[uid]["version"] == number for uid, number in mine.items()), name
    verify_clean(project, env)
    assert cli(project, "verify", "--repo", str(server.root)).returncode == 0
    assert not stray_repos(tmp_path, {project / ".cairn", server.root})
    server.stop()


RANDOM_PUBLISH = """
import os, pathlib, sys, cairn
folder = pathlib.Path(sys.argv[1])
folder.mkdir(parents=True)
(folder / "a.txt").write_text(sys.argv[1])
(folder / "big.bin").write_bytes(os.urandom(int(sys.argv[2]) << 20))
version = cairn.connect().artifact(sys.argv[3]).publish(folder)
print("DONE", version.uid, flush=True)
"""

RANDOM_RUN = """
import sys, time, cairn
run = cairn.init(name="victim", code=False, system=False, watch=True)
for step in range(int(sys.argv[1])):
    run.add({"loss": 1.0 / (step + 1), "step": step})
    time.sleep(0.01)
with cairn.draft("out") as out:
    out.add_json("v", {"steps": sys.argv[1]})
run.finish()
print("DONE", run.uid, out.version.uid, flush=True)
"""

KILLS = int(os.environ.get("CAIRN_ATTACK_KILLS", "25"))


@pytest.mark.parametrize("remote", [False, True], ids=["no-remote", "remote"])
def test_random_sigkills_of_concurrent_writers_gc_syncs_and_the_server(tmp_path, remote):
    """KILLS rounds: two or three of (a publish with a 1-30 MB file, a live run with a draft, `cairn gc --grace 0`,
    `cairn sync`, `cairn prune`) run at once and a random subset is SIGKILLed after a random delay; with a remote, the
    server is SIGKILLed and restarted at random too. Afterwards the repo (and remote) hold every finished version once,
    numbers are contiguous, no version stays running, and verify is clean."""
    rng = random.Random(1234 + remote)
    server = RealServer(tmp_path / "remote").start() if remote else None
    env = base_env(**({"CAIRN_TOKEN": server.token} if server else {}))
    project = project_at(tmp_path, "study", server.url if server else None)
    script(project, "publish.py", RANDOM_PUBLISH)
    script(project, "run.py", RANDOM_RUN)
    finished, outputs, counter = [], [], 0
    for round_ in range(KILLS):
        kinds = rng.sample(["publish", "publish", "run", "gc", "sync", "prune"], rng.randint(2, 3))
        processes = []
        for kind in kinds:
            counter += 1
            if kind == "publish":
                argv = ["publish.py", f"w/{counter}", rng.randint(1, 30), rng.choice(["data", "other"])]
            elif kind == "run":
                argv = ["run.py", rng.randint(5, 200)]
            elif kind == "gc":
                argv = ["-m", "cairn", "gc", "--grace", "0"]
            elif kind == "sync":
                argv = ["-m", "cairn", "sync"]
            else:
                argv = ["-m", "cairn", "prune", "--older-than", "0s"]
            processes.append((kind, spawn(project, *argv, env=env)))
        time.sleep(rng.uniform(0.05, 0.8))
        if server and rng.random() < 0.3:
            server.stop(kill=True)
            time.sleep(rng.uniform(0, 0.5))
            server.start()
        for kind, process in processes:
            if rng.random() < 0.6:
                process.send_signal(signal.SIGKILL)
        for kind, process in processes:
            out, err = finish(process, 300)
            outputs.append((kind, process.returncode, out, err))
            if process.returncode == 0:
                for line in out.splitlines():
                    if line.startswith("DONE "):
                        finished += line.split()[1:]
            elif process.returncode != -signal.SIGKILL and kind in ("publish", "run"):
                raise AssertionError(f"{kind} failed without being killed: {err[-3000:]}")
    problems = [f"{kind}: {line}" for kind, code, out, err in outputs if code == 0 for line in bad_lines(err) if "Traceback" in line
                or "locked" in line]
    assert not problems, "\n".join(problems[:20])
    status = cli(project, "status", env=env)
    assert status.returncode == 0, status.stderr
    if server:
        synced = cli(project, "sync", env=env)
        assert synced.returncode == 0, synced.stdout + synced.stderr
        assert cli(project, "prune", env=env).returncode == 0  # outbox folders whose removal a kill interrupted
        assert outbox_of(project) == [], cli(project, "status", env=env).stdout
        assert "nothing pending" in cli(project, "status", env=env).stdout
    truth = LocalRepo(server.root if server else project / ".cairn")
    everything = check_numbers(truth, "study" if server else OWN)
    known = {version["uid"]: version for versions in everything.values() for version in versions}
    for uid in finished:
        assert known[uid]["status"] == "completed", known.get(uid)
    running = [version for version in known.values() if version["status"] == "running"]
    assert not running, running
    print(f"\n{KILLS} rounds ({'remote' if server else 'no remote'}): {len(finished)} finished, "
          f"{sum(1 for _, code, _, _ in outputs if code == -signal.SIGKILL)} killed, {len(known)} versions in the source of truth, "
          f"{sum(1 for v in known.values() if v['status'] == 'failed')} failed")
    verify_clean(project, env)
    if server:
        assert cli(project, "verify", "--repo", str(server.root)).returncode == 0
        server.stop()
    assert not stray_repos(tmp_path, {project / ".cairn", tmp_path / "remote"})


# ====================================================================== 7. servers started and stopped, two servers, a taken port

LIVE_UNTIL = """
import pathlib, sys, time, cairn
run = cairn.init(name=sys.argv[1], code=False, system=False, watch=True)
pathlib.Path(f"{sys.argv[1]}.started").write_text(run.uid)
step = 0
while not pathlib.Path("stop").exists():
    run.add({"step": step})
    step += 1
    time.sleep(0.05)
with cairn.draft(f"{sys.argv[1]}-out") as out:
    out.add_json("steps", {"steps": step})
run.finish()
print("DONE", run.uid, out.version.uid, step, flush=True)
"""


def test_a_server_stopped_and_killed_mid_run_changes_nothing_for_runs(tmp_path):
    """A client's live run and a run on the server's own machine (the server's repo opened directly: its own, unnamed
    project, beside the served project `study`) keep going while the server is stopped, SIGKILLed, and started again;
    both complete, nothing is failed, every step is kept."""
    server = RealServer(tmp_path / "remote").start()
    env = base_env(CAIRN_TOKEN=server.token)
    client = project_at(tmp_path, "client", server.url, extra="heartbeat_timeout = 600\n")
    beside = project_at(tmp_path, "beside")  # its repo is the server's ($CAIRN_DIR)
    for project in (client, beside):
        script(project, "live.py", LIVE_UNTIL)
    remote_run = spawn(client, "live.py", "remote-run", env=env)
    local_run = spawn(beside, "live.py", "local-run", env={**env, "CAIRN_DIR": str(server.root)})
    for project, name, process in ((client, "remote-run", remote_run), (beside, "local-run", local_run)):
        deadline = time.time() + 60
        while not (project / f"{name}.started").exists():
            assert time.time() < deadline and process.poll() is None, finish(process)
            time.sleep(0.05)
    time.sleep(1.5)  # the client's run is on the server, running, with files
    server.stop()  # SIGTERM
    time.sleep(1.5)
    server.start()
    time.sleep(1.5)
    server.stop(kill=True)
    time.sleep(1.5)
    server.start()
    time.sleep(2)
    for project in (client, beside):
        (project / "stop").write_text("")
    results = {}
    for name, process in (("remote-run", remote_run), ("local-run", local_run)):
        out, err = finish(process, 300)
        assert process.returncode == 0, err
        assert not [line for line in bad_lines(err) if "Traceback" in line], err
        _, run_uid, out_uid, steps = out.split()
        results[name] = (run_uid, out_uid, int(steps))
    synced = cli(client, "sync", env=env)
    assert synced.returncode == 0, synced.stdout + synced.stderr
    remote = LocalRepo(server.root)
    check_numbers(remote)
    check_numbers(remote, OWN)
    for name, (run_uid, out_uid, steps) in results.items():
        project = "study" if name == "remote-run" else OWN
        run = remote.get_version(project, f"runs/{name}", run_uid)
        assert run["status"] == "completed", (name, run.get("error"))
        lines = b"".join(remote.read_file(project, f"runs/{name}", run_uid, "metrics.jsonl").chunks).decode().splitlines()
        assert len(lines) == steps, (name, len(lines), steps)
        assert remote.get_version(project, f"{name}-out", out_uid)["inputs"] == [f"runs/{name}@v{run['version']}"]
    assert outbox_of(client) == []
    verify_clean(client, env)
    assert cli(client, "verify", "--repo", str(server.root)).returncode == 0

    second = spawn(tmp_path, "-m", "cairn", "server", server.root, "--port", free_port(), "--no-ui")
    out, err = finish(second, 60)
    assert second.returncode != 0 and "already served" in out + err, out + err  # two servers on one repo: refused
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    other = project_at(tmp_path, "other")
    port = taken.getsockname()[1]
    third = spawn(other, "-m", "cairn", "server", tmp_path / "other-remote", "--port", port, "--no-ui")
    try:  # a taken port moves the server to the next free one, and its startup block says so
        deadline = time.time() + 60
        while "Serving;" not in third.logs[0].read_text() and third.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        out = third.logs[0].read_text()
        assert f"port {port}" in out and "in use" in out and "Serving;" in out, out + third.logs[1].read_text()
    finally:
        third.terminate()
        third.wait(timeout=20)
        taken.close()
    server.stop()


# ====================================================================== 4. many senders of one outbox, two machines, one remote

OFFLINE_WORK = """
import json, pathlib, sys, cairn
tag, count = sys.argv[1], int(sys.argv[2])
made = []
project = cairn.connect()
for index in range(count):
    folder = pathlib.Path(f"work-{tag}/{index}")
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text(f"{tag} {index}")
    made.append(project.artifact("shared" if index % 2 else f"{tag}-own").publish(folder).uid)
print(json.dumps(made))
"""

CONSUMER = """
import json, sys, cairn
run = cairn.init(name=f"consume-{sys.argv[1]}", code=False, system=False)
data = cairn.use(sys.argv[2])
with cairn.draft(f"derived-{sys.argv[1]}") as out:
    out.add_json("from", {"ref": data.ref, "uid": data.uid, "text": (data.path / "a.txt").read_text()})
run.finish()
print(json.dumps({"run": run.uid, "out": out.version.uid, "used": data.uid}))
"""


def test_many_senders_of_one_outbox_and_two_machines_number_everything_once(tmp_path):
    server = RealServer(tmp_path / "remote").start()
    proxy = FaultProxy(server.port, mode="refuse")
    env = base_env(CAIRN_TOKEN=server.token)
    a = project_at(tmp_path, "machine-a", proxy.url, extra="heartbeat_timeout = 600\n")
    b = project_at(tmp_path, "machine-b", server.url)
    for project in (a, b):
        script(project, "work.py", OFFLINE_WORK)
        script(project, "consume.py", CONSUMER)
    made_b = json.loads(subprocess.run([sys.executable, "work.py", "b", "6"], cwd=b, capture_output=True, text=True, env=env,
                                       check=True).stdout)
    proxy.mode = "forward"  # A uses B's version while it reaches the remote, then works offline
    used = subprocess.run([sys.executable, "consume.py", "a1", "b-own@latest"], cwd=a, capture_output=True, text=True, env=env)
    assert used.returncode == 0, used.stderr
    proxy.mode = "refuse"
    made_a = json.loads(subprocess.run([sys.executable, "work.py", "a", "30"], cwd=a, capture_output=True, text=True, env=env,
                                       check=True).stdout)
    offline_use = subprocess.run([sys.executable, "consume.py", "a2", f"a-own@{made_a[0]}"], cwd=a, capture_output=True,
                                 text=True, env=env)
    assert offline_use.returncode == 0, offline_use.stderr
    assert len(outbox_of(a)) == 32  # 30 publishes, the offline run and its output
    proxy.mode = "forward"  # now everyone sends the same outbox at once, while B keeps publishing to `shared`
    senders = [spawn(a, "-m", "cairn", "sync", env=env) for _ in range(4)]
    senders.append(spawn(a, "-m", "cairn", "versions", "shared", env=env))  # any command sends at its start
    watcher = spawn(a, "-m", "cairn", "sync", "--watch", env=env)
    more_b = spawn(b, "work.py", "b2", "20", env=env)
    for process in senders:
        finish(process, 300)
    out, err = finish(more_b, 300)
    assert more_b.returncode == 0, err
    made_b += json.loads(out)
    deadline = time.time() + 120
    while outbox_of(a) and time.time() < deadline:
        time.sleep(0.2)
    watcher.send_signal(signal.SIGINT)
    finish(watcher, 60)
    assert outbox_of(a) == [], cli(a, "status", env=env).stdout
    b_reads = subprocess.run([sys.executable, "consume.py", "b1", f"a-own@{made_a[2]}"], cwd=b, capture_output=True,
                             text=True, env=env)
    assert b_reads.returncode == 0, b_reads.stderr
    assert json.loads(b_reads.stdout)["used"] == made_a[2]

    remote = LocalRepo(server.root)
    everything = check_numbers(remote)
    on_remote = {version["uid"]: version for versions in everything.values() for version in versions}
    for uid in made_a + made_b:
        assert on_remote[uid]["status"] == "completed"
    assert len(everything["shared"]) == 15 + 3 + 10  # every publish once
    for project in (a, b):  # each repo's copies carry the remote's numbers
        local = LocalRepo(project / ".cairn")
        for name in local.artifacts(OWN):
            for version in local.versions(OWN, name):
                assert on_remote[version["uid"]]["version"] == version["version"], (project.name, name)
        verify_clean(project, env)
    derived = remote.versions("study", "derived-a2")[0]
    [run_ref] = derived["inputs"]
    assert remote.resolve("study", run_ref)[1]["inputs"] == [f"a-own@v{on_remote[made_a[0]]['version']}"]  # pending input renumbered
    proxy.close()
    server.stop()


# ====================================================================== 5. a cluster: the repo on a network filesystem

NFS_HOOK = """
import cairn.store.fs as fs
import cairn.store.local as local
import cairn.client.open as open_module
fs.network_filesystem = local.network_filesystem = open_module.network_filesystem = lambda path: "nfs4"
"""

NFS_JOB = """
import json, os, sys, time, cairn
index, steps = int(sys.argv[1]), int(sys.argv[2])
run = cairn.init(name="job", config={"index": index}, code=False, system=False)
for step in range(steps):
    run.add({"loss": 1.0 / (step + 1), "step": step})
    time.sleep(0.02)
with cairn.draft("result") as result:
    result.add_json("value", {"index": index})
    (result.path / "big.bin").write_bytes(os.urandom(2 << 20))
run.finish()
print("DONE", run.uid, result.version.uid, flush=True)
"""


def nfs_env(tmp_path: Path, **extra) -> dict:
    hook = tmp_path / "nfs-hook"
    hook.mkdir(exist_ok=True)
    (hook / "sitecustomize.py").write_text(NFS_HOOK)
    env = base_env(**extra)
    env["PYTHONPATH"] = os.pathsep.join([str(hook), env.get("PYTHONPATH", "")])
    return env


def test_cluster_jobs_on_a_network_filesystem_with_a_login_node_and_a_flaky_remote(tmp_path):
    """Jobs on "NFS" (the check patched) write runs and drafts while the remote comes and goes (some jobs reach it,
    others do not); some are SIGKILLed; a login node runs `cairn sync --watch` all along and races the jobs that reach
    the remote for the same entries. No database is ever made on the network filesystem, and the remote ends with
    every finished version once and no run left running."""
    server = RealServer(tmp_path / "remote").start()
    proxy = FaultProxy(server.port, mode="refuse")
    env = nfs_env(tmp_path, CAIRN_TOKEN=server.token)
    project = project_at(tmp_path, "cluster", proxy.url)
    script(project, "job.py", NFS_JOB)
    database = project / ".cairn" / "metadata.sqlite3"
    seen_database = []
    stop = threading.Event()

    def watch_database():
        while not stop.wait(0.05):
            if database.exists() or (project / ".cairn" / "metadata.sqlite3-wal").exists():
                seen_database.append(time.time())

    threading.Thread(target=watch_database, daemon=True).start()
    rng = random.Random(7)
    login = spawn(project, "-m", "cairn", "sync", "--watch", env=env)
    finished, jobs, killed = [], [], 0
    try:
        for wave in range(4):
            proxy.mode = rng.choice(["refuse", "forward", "hang", "forward"])
            jobs = [spawn(project, "job.py", wave * 10 + index, rng.randint(5, 60), env=env) for index in range(6)]
            time.sleep(rng.uniform(0.3, 1.5))
            proxy.mode = rng.choice(["refuse", "forward"])
            for job in jobs:
                if rng.random() < 0.3:
                    job.send_signal(signal.SIGKILL)
            for job in jobs:
                out, err = finish(job, 300)
                if job.returncode == -signal.SIGKILL:
                    killed += 1
                    continue
                assert job.returncode == 0, err
                finished += out.split()[1:3]
        proxy.mode = "forward"
        deadline = time.time() + 180
        while outbox_of(project) and time.time() < deadline:
            time.sleep(0.3)
        left = cli(project, "status", env=env).stdout
    finally:
        login.send_signal(signal.SIGINT)
        finish(login, 60)
        stop.set()
    assert not seen_database and not database.exists()  # never a database on the network filesystem
    assert outbox_of(project) == [], (left, {uid: sorted(path.name for path in (project / ".cairn" / "outbox" / uid).iterdir())
                                             for uid in outbox_of(project)})
    remote = LocalRepo(server.root)
    everything = check_numbers(remote)
    on_remote = {version["uid"]: version for versions in everything.values() for version in versions}
    for uid in finished:
        assert on_remote[uid]["status"] == "completed", on_remote.get(uid)
    assert not [v for v in on_remote.values() if v["status"] == "running"]
    runs = everything["runs/job"]
    print(f"\ncluster: {len(finished) // 2} jobs finished, {killed} killed; the remote has {len(runs)} runs "
          f"({sum(v['status'] == 'failed' for v in runs)} failed) and {len(everything.get('result', []))} results")
    assert len(runs) >= len(finished) // 2 and sum(v["status"] == "failed" for v in runs) <= killed
    for result in everything.get("result", []):  # each result names its own run, by the remote's number
        [run_ref] = result["inputs"]
        assert remote.resolve("study", run_ref)[1]["metadata"]["kind"] == "run"
    verify_clean(project, env)
    assert not database.exists()
    proxy.close()
    server.stop()


def test_late_writes_never_bring_a_sent_entry_back(tmp_path):
    """A sender's save, a heartbeat, or a claim refresh that comes after another sender removed the entry (it was sent)
    must not make the folder again: as a pending entry it would be sent again, as a folder of leftovers it is junk that
    `cairn status` does not explain."""
    from cairn.store.outbox import Outbox
    from cairn.store.uids import new_uid

    outbox = Outbox(tmp_path)
    record = {"uid": new_uid(), "sync": "running", "status": "running", "files": []}
    outbox.create(record, running=True)
    claim = outbox.claim(record["uid"], 600)
    outbox.remove(record)  # another sender sent it
    outbox.beat(record["uid"])
    outbox.refresh_claim(claim)
    assert outbox.save({**record, "sync": "pending", "status": "completed"}) is False
    assert not outbox.folder_of(record["uid"]).exists() and outbox.records() == []


# ====================================================================== 8. finding the repo

def publish_here(where: Path, name: str, text: str, env=None) -> subprocess.CompletedProcess:
    data = where / f"data-{name}-{abs(hash(text)) % 10**8}"
    data.mkdir(parents=True)
    (data / "a.txt").write_text(text)
    return cli(where, "publish", str(data), name, env=env)


def test_finding_the_repo_from_anywhere(tmp_path):
    outer = project_at(tmp_path, "outer")
    inner = outer / "sub" / "inner"
    inner.mkdir(parents=True)
    write_config(inner, "")
    deep = inner / "a" / "b"
    deep.mkdir(parents=True)
    assert publish_here(deep, "x", "inner").returncode == 0  # nested: the nearest project wins
    assert LocalRepo(inner / ".cairn").versions(OWN, "x")[0]["version"] == 1
    assert publish_here(outer / "sub", "x", "outer").returncode == 0
    assert LocalRepo(outer / ".cairn").versions(OWN, "x")[0]["version"] == 1

    link = tmp_path / "link"  # a symlinked project folder: one repo, whichever path a process takes
    link.symlink_to(outer)
    writers = []
    for index in range(6):
        data = outer / "sub" / f"d{index}"
        data.mkdir()
        (data / "a.txt").write_text(str(index))
        writers.append(spawn(link / "sub" if index % 2 else outer, "-m", "cairn", "publish", str(data), "shared"))
    for writer in writers:
        out, err = finish(writer, 120)
        assert writer.returncode == 0, err
    assert [v["version"] for v in LocalRepo(outer / ".cairn").versions(OWN, "shared")] == [1, 2, 3, 4, 5, 6]

    hydra = outer / "outputs" / "2026-10-16" / "01-02-03"  # a Hydra job's fresh folder
    hydra.mkdir(parents=True)
    run = subprocess.run([sys.executable, "-c", "import cairn; r = cairn.init(name='h', code=False, system=False); r.finish(); print(r.uid)"],
                         cwd=hydra, capture_output=True, text=True, env=base_env())
    assert run.returncode == 0, run.stderr
    assert LocalRepo(outer / ".cairn").get_version(OWN, "runs/h", run.stdout.split()[-1])["status"] == "completed"

    drafting = subprocess.run([sys.executable, "-c", textwrap.dedent("""
        import os, subprocess, sys, cairn
        with cairn.draft("inside") as draft:
            os.chdir(draft.path)  # a job that works inside its draft folder
            (draft.path / "a.txt").write_text("x")
            result = subprocess.run([sys.executable, "-m", "cairn", "versions", "shared"], capture_output=True, text=True)
            assert result.returncode == 0 and result.stdout.count("completed") == 6, result.stdout + result.stderr
        print(draft.version.ref)
    """)], cwd=outer, capture_output=True, text=True, env=base_env())
    assert drafting.returncode == 0, drafting.stderr
    assert drafting.stdout.strip().startswith("inside@v1")

    elsewhere = tmp_path / "elsewhere"  # CAIRN_DIR: that folder is the repo, wherever the command runs
    elsewhere.mkdir()
    named = tmp_path / "named-repo"
    env = base_env(CAIRN_DIR=named)
    assert cli(elsewhere, "init", env=env).returncode == 0
    assert publish_here(elsewhere, "y", "named", env=env).returncode == 0
    assert LocalRepo(named).versions(OWN, "y")[0]["version"] == 1
    assert publish_here(deep, "z", "named too", env=env).returncode == 0  # inside a project: still CAIRN_DIR's repo
    assert LocalRepo(named).versions(OWN, "z")[0]["version"] == 1

    nowhere = tmp_path / "nowhere"  # read-only commands outside any repo refuse, and make nothing
    nowhere.mkdir()
    for argv in (["status"], ["verify"], ["artifacts"], ["versions", "x"], ["show", "x"], ["pull", "x"], ["drafts", "list"],
                 ["prune"], ["sync"]):
        result = cli(nowhere, *argv)
        assert result.returncode == 1 and "no repo here" in result.stderr, (argv, result.stderr)
    assert not (nowhere / ".cairn").exists()

    racing = tmp_path / "racing"  # many `cairn init` at once: one repo, one uid
    racing.mkdir()
    inits = [spawn(racing, "-m", "cairn", "init") for _ in range(8)]
    for process in inits:
        out, err = finish(process, 60)
        assert process.returncode == 0, err
    uid = json.loads((racing / ".cairn" / "repo.json").read_text())["uid"]
    opened = [spawn(racing, "-c", "import cairn; print(cairn.connect().backend.uid)") for _ in range(8)]
    assert {finish(process, 60)[0].strip() for process in opened} == {uid}
    assert sorted(path.name for path in (racing / ".cairn").iterdir() if path.name.startswith("repo.json")) == ["repo.json"]
    assert not stray_repos(tmp_path, {outer / ".cairn", inner / ".cairn", named, racing / ".cairn", tmp_path / "remote"})


# ====================================================================== 9. odd input


def test_odd_names_empty_files_and_symlinks_publish_and_read_back_byte_identical(tmp_path):
    project = project_at(tmp_path, "study-odd")
    data = tmp_path / "odd"
    (data / "empty-folder").mkdir(parents=True)
    (data / "sub").mkdir()
    files = {"empty.txt": b"", "sub/ünïcødé ✓ 文件 (1).txt": "größe".encode(), "sub/" + "x" * 240 + ".bin": os.urandom(1000),
             "sub/space name.txt": b"spaces", "sub/new​line.txt": b"zero width"}
    for path, content in files.items():
        (data / path).write_bytes(content)
    (data / "sub" / "inside").symlink_to("../empty.txt")
    (data / "sub" / "self").symlink_to("self")
    (data / "folder-link").symlink_to("sub")
    (data / "sub" / "outside").symlink_to("../../somewhere-else")
    (data / "sub" / "absolute").symlink_to("/etc/hosts")
    result = cli(project, "publish", str(data), "odd")
    assert result.returncode == 0, result.stderr
    repo = LocalRepo(project / ".cairn")
    version = repo.versions(OWN, "odd")[0]
    entries = {entry["path"]: entry for entry in version["files"]}
    assert {path: entries[path]["sha256"] for path in files} == {path: sha(content) for path, content in files.items()}
    assert entries["sub/inside"]["link"] == "../empty.txt" and entries["sub/self"]["link"] == "self"
    assert entries["folder-link"]["link"] == "sub"
    assert "sub/outside" not in entries and "sub/absolute" not in entries  # links out of the folder are left out
    pulled = Path(cli(project, "pull", "odd@v1", "--no-record").stdout.strip())
    for path, content in files.items():
        assert (pulled / path).read_bytes() == content
    assert (pulled / "folder-link" / "space name.txt").read_bytes() == b"spaces"
    verify_clean(project)


GROWING = """
import sys, time
with open(sys.argv[1], "ab") as handle:
    end = time.time() + float(sys.argv[2])
    while time.time() < end and handle.tell() < 400 << 20:
        handle.write(b"x" * 65536)
        handle.flush()
        time.sleep(0.0005)
"""


def test_files_that_change_while_published_and_files_that_cannot_be_read(tmp_path):
    project = project_at(tmp_path, "study-change")
    data = tmp_path / "changing"
    data.mkdir()
    (data / "log.txt").write_bytes(b"start\n" * 1000)
    (data / "stable.txt").write_text("stable")
    script(project, "grow.py", GROWING)
    writer = spawn(project, "grow.py", data / "log.txt", 8)
    numbers = []
    for _ in range(5):
        result = cli(project, "publish", str(data), "changing")
        assert result.returncode == 0, result.stderr
    finish(writer)
    repo = LocalRepo(project / ".cairn")
    for version in repo.versions(OWN, "changing"):  # each version has what was staged, consistently
        for entry in version["files"]:
            content = b"".join(repo.read_file(OWN, "changing", f"v{version['version']}", entry["path"]).chunks)
            assert sha(content) == entry["sha256"] and len(content) == entry["size"], (version["version"], entry["path"])
        numbers.append(version["version"])
    assert numbers == list(range(1, len(numbers) + 1))
    verify_clean(project)

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "a.txt").write_text("a")
    (locked / "secret.txt").write_text("secret")
    os.chmod(locked / "secret.txt", 0)
    try:
        result = cli(project, "publish", str(locked), "locked")
    finally:
        os.chmod(locked / "secret.txt", 0o644)
    assert result.returncode == 1 and "Permission denied" in result.stderr and "Traceback" not in result.stderr
    assert "locked" not in repo.artifacts(OWN)
    verify_clean(project)


def test_a_full_disk_fails_cleanly_and_the_repo_recovers(tmp_path):
    """A 40 MB disk image (macOS hdiutil) holds the project: a publish too big for it, and a publish when the database
    cannot grow, fail with one line; nothing partial is left, and once there is room again publishing works."""
    import shutil

    if sys.platform != "darwin" or not shutil.which("hdiutil"):
        pytest.skip("needs hdiutil (macOS) for a small filesystem")
    image, mount = tmp_path / "full.dmg", tmp_path / "mnt"
    made = subprocess.run(["hdiutil", "create", "-size", "40m", "-fs", "APFS", "-volname", "cairnfull", str(image), "-quiet"],
                          capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"hdiutil create failed: {made.stderr}")
    mount.mkdir()
    attached = subprocess.run(["hdiutil", "attach", str(image), "-mountpoint", str(mount), "-nobrowse", "-quiet"],
                              capture_output=True, text=True)
    if attached.returncode != 0:
        pytest.skip(f"hdiutil attach failed: {attached.stderr}")
    try:
        project = project_at(mount, "p")
        sources = tmp_path / "sources"
        for name, size in (("one", 10 << 20), ("two", 60 << 20)):
            (sources / name).mkdir(parents=True)
            (sources / name / "data.bin").write_bytes(os.urandom(size))
        assert cli(project, "publish", str(sources / "one"), "one").returncode == 0
        too_big = cli(project, "publish", str(sources / "two"), "two")
        assert too_big.returncode == 1 and "No space left" in too_big.stderr and "Traceback" not in too_big.stderr
        assert not list((project / ".cairn" / "tmp").glob("*.*"))  # no partial upload left
        free = shutil.disk_usage(mount).free
        filler = mount / "filler"
        with filler.open("wb") as handle:  # leave a few kB: the next write to the database fails
            handle.truncate(max(free - 8192, 0))
            handle.write(b"\0")
        small = sources / "small"
        small.mkdir()
        failed = 0
        for index in range(3):
            (small / "c.txt").write_text(f"small {index}")
            result = cli(project, "publish", str(small), "small")
            failed += result.returncode != 0
            assert "Traceback" not in result.stderr, result.stderr
        filler.unlink()
        (small / "c.txt").write_text("after")
        assert cli(project, "publish", str(small), "small").returncode == 0
        repo = LocalRepo(project / ".cairn")
        check_numbers(repo, OWN)
        assert "two" not in repo.artifacts(OWN)
        verify_clean(project)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force", "-quiet"], capture_output=True)


def test_a_file_that_grows_between_its_listing_and_its_hash_is_recorded_as_kept(tmp_path):
    """The folder is listed (size from stat), then the file grows, then it is hashed and staged: the entry must have the
    size of the bytes kept, and the hash cache must not remember the hash under the old size."""
    from cairn.client.http import entries_for, scan
    from cairn.store.blobs import BlobStore
    from cairn.store.hashes import HashCache

    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "log.txt").write_bytes(b"a" * 1000)
    files = scan(folder, "", tmp_path)
    with (folder / "log.txt").open("ab") as handle:  # written after the listing, before the hash
        handle.write(b"b" * 500)
    cache = HashCache(tmp_path / "hashes.json")
    [entry], _ = entries_for(files, cache)
    BlobStore(tmp_path / "repo").stage_file(entry, folder / "log.txt")
    kept = (tmp_path / "repo" / "blobs" / entry["sha256"][:2] / entry["sha256"]).read_bytes()
    assert sha(kept) == entry["sha256"] and len(kept) == entry["size"] == 1500
    assert str(folder / "log.txt") not in cache.entries


SWEPT_RUN = """
import pathlib, time, cairn
import cairn.store.local as local

original, swept = local.LocalRepo.add_files, []
def add_files(self, *args, **kwargs):  # a `cairn gc` sweep lands between the staging of a file and its version's write
    if not swept and args[3]:
        swept.append(self.gc(dry_run=False, grace=0))
    return original(self, *args, **kwargs)
local.LocalRepo.add_files = add_files

run = cairn.init(name="swept", code=False, system=False, watch=True)
for step in range(40):
    run.add({"step": step})
    (run.path / "files").mkdir(exist_ok=True)
    (run.path / "files" / f"f{step}.txt").write_text(str(step))
    time.sleep(0.2)
run.finish()
print("DONE", run.uid, swept[0]["blobs"] if swept else None)
"""


def test_a_gc_sweep_between_staging_and_a_live_sync_does_not_lose_the_run(tmp_path):
    """A live run's sync whose staged blob a concurrent sweep removed (MissingBlobs) stages again; the run keeps its
    heartbeat, so a short heartbeat timeout does not fail it while it runs."""
    project = project_at(tmp_path, "study-swept", extra="heartbeat_timeout = 2\n")
    script(project, "swept.py", SWEPT_RUN)
    run = spawn(project, "swept.py")
    while run.poll() is None:  # other processes open the repo meanwhile, and fail runs whose heartbeat stopped
        cli(project, "status")
        time.sleep(0.5)
    out, err = finish(run)
    assert run.returncode == 0, err
    _, uid, swept = out.split()
    assert swept != "None" and int(swept) >= 1  # the sweep did remove a staged blob
    version = LocalRepo(project / ".cairn").get_version(OWN, "runs/swept", uid)
    assert version["status"] == "completed", version.get("error")
    assert len([entry for entry in version["files"] if entry["path"].startswith("files/")]) == 40
    verify_clean(project)


def test_a_slow_remote_with_a_backlog_holds_a_command_up_for_a_bounded_time(tmp_path):
    """20 versions wait in the outbox; then the remote answers, but slowly (1 s per connection). A command's start and
    end syncs send what fits in their time budget and leave the rest, instead of sending the whole backlog first."""
    server = RealServer(tmp_path / "remote").start()
    proxy = FaultProxy(server.port, mode="refuse", delay=1.0)
    env = base_env(CAIRN_TOKEN=server.token)
    project = project_at(tmp_path, "study", proxy.url)
    script(project, "work.py", OFFLINE_WORK)
    made = subprocess.run([sys.executable, "work.py", "x", "20"], cwd=project, capture_output=True, text=True, env=env)
    assert made.returncode == 0, made.stderr
    proxy.mode = "slow"
    began = time.perf_counter()
    result = cli(project, "artifacts", env=env)
    took = time.perf_counter() - began
    assert result.returncode == 0, result.stderr
    print(f"\n`cairn artifacts` with 20 pending versions and a remote that takes 1 s per connection: {took:.1f} s")
    assert took < 45, took
    proxy.mode = "forward"
    assert cli(project, "sync", env=env).returncode == 0
    assert outbox_of(project) == []
    assert len(LocalRepo(server.root).versions("study", "shared")) == 10
    proxy.close()
    server.stop()


TWO_PUBLISHES = """
import json, pathlib, sys, time, cairn
artifact = cairn.connect().artifact("kept")
numbers = []
for index in range(2):
    folder = pathlib.Path(f"k{index}")
    folder.mkdir()
    (folder / "a.txt").write_text(str(index))
    numbers.append(artifact.publish(folder).number)
    if index == 0:
        pathlib.Path("first").write_text("")
        while not pathlib.Path("again").exists():
            time.sleep(0.05)
print(json.dumps(numbers))
"""


def test_a_process_keeps_its_connection_and_survives_a_server_restart(tmp_path):
    """A run (with a draft) talks to the remote over one or two kept-alive connections, not one per request; a
    connection the remote closed (the server restarted) is noticed and the request goes out again on a new one."""
    server = RealServer(tmp_path / "remote").start()
    proxy = FaultProxy(server.port)
    env = base_env(CAIRN_TOKEN=server.token)
    project = project_at(tmp_path, "study", proxy.url)
    script(project, "timed.py", RUN_TIMING)
    script(project, "two.py", TWO_PUBLISHES)
    row = timed_run(project, env)
    print(f"\na run with a draft opened {proxy.connections} connections")
    assert proxy.connections <= 3, proxy.connections
    process = spawn(project, "two.py", env=env)
    while not (project / "first").exists():
        assert process.poll() is None, finish(process)
        time.sleep(0.05)
    server.stop()  # its kept-alive connections close
    server.start()
    (project / "again").write_text("")
    out, err = finish(process, 120)
    assert process.returncode == 0, err
    assert json.loads(out) == [1, 2], (out, err)  # both numbered at once: the second went out on a new connection
    remote = LocalRepo(server.root)
    assert remote.get_version("study", "runs/timed", row["uid"])["status"] == "completed"
    proxy.close()
    server.stop()


def test_the_outbox_listing_cache_sees_changes_and_removals_at_once(tmp_path):
    """Listings parse a manifest again only when it changed; a change written by another process, even one that keeps
    the file's size, and a removal show at the next listing."""
    from cairn.store.outbox import Outbox
    from cairn.store.uids import new_uid

    outbox = Outbox(tmp_path)
    records = [{"uid": new_uid(), "sync": "pending", "status": "completed", "label": "aaaa", "files": [], "created": "x"}
               for _ in range(3)]
    for record in records:
        outbox.create(record, running=False)
    assert [record["label"] for record in outbox.records()] == ["aaaa"] * 3
    changed = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
        from cairn.store.outbox import Outbox
        outbox = Outbox({str(tmp_path)!r})
        record = outbox.load({records[1]['uid']!r})
        record["label"] = "bbbb"  # the same size
        outbox.save(record)
        outbox.remove(outbox.load({records[2]['uid']!r}))
    """)], capture_output=True, text=True)
    assert changed.returncode == 0, changed.stderr
    listed = outbox.records()
    assert [record["label"] for record in listed] == ["aaaa", "bbbb"]
    listed[0]["label"] = "edited by a caller"  # a caller's change does not reach the next listing
    assert outbox.records()[0]["label"] == "aaaa"


BIG_RUN = """
import json, os, sys, time, cairn
began = time.perf_counter()
run = cairn.init(name="big", code=False, system=False)
(run.path / "files").mkdir(exist_ok=True)
(run.path / "files" / "model.bin").write_bytes(os.urandom(20 << 20))
with open("written", "w"):
    pass
run.finish()
print(json.dumps({"uid": run.uid, "finish": time.perf_counter() - began}))
"""


def test_a_run_ends_only_once_its_large_output_is_on_a_healthy_remote(tmp_path):
    """A run's 20 MB output goes to a remote that accepts 2 MB/s: the run's end waits for the upload while bytes flow
    (about 10 s, well over its 4 s budget for waiting), says so once, and exits with the version numbered on the remote.
    When the bytes stop moving partway, the wait ends after the stall timeout, the version stays pending, and `cairn sync`
    sends it later."""
    server = RealServer(tmp_path / "remote").start()
    proxy = FaultProxy(server.port, mode="forward", rate=2e6)
    env = base_env(CAIRN_TOKEN=server.token)
    project = project_at(tmp_path, "study", proxy.url)
    script(project, "big.py", BIG_RUN)
    proxy.mode = "throttle"
    began = time.perf_counter()
    process = spawn(project, "big.py", env=env)
    out, err = finish(process, 300)
    took = time.perf_counter() - began
    assert process.returncode == 0, err
    row = json.loads(out.strip().splitlines()[-1])
    print(f"\n20 MB at 2 MB/s: the run's process took {took:.1f} s; stderr: {err.strip()!r}")
    assert outbox_of(project) == []  # synced and numbered before the process ended
    remote = LocalRepo(server.root)
    assert remote.get_version("study", "runs/big", row["uid"])["status"] == "completed"
    assert err.count("cairn: sending") == 1 and "MB of runs/big" in err, err
    assert 8 < took < 60, took

    proxy.mode = "throttle"  # now the bytes stop moving partway through
    (project / "written").unlink()
    began = time.perf_counter()
    process = spawn(project, "big.py", env=env)
    while not (project / "written").exists():
        assert process.poll() is None, finish(process)
        time.sleep(0.05)
    time.sleep(3)
    proxy.mode = "stall"
    stalled = time.perf_counter()
    out, err = finish(process, 300)
    ended = time.perf_counter() - stalled
    assert process.returncode == 0, err
    row = json.loads(out.strip().splitlines()[-1])
    print(f"stalled partway: the process ended {ended:.1f} s after the stall; stderr: {err.strip()!r}")
    assert len(outbox_of(project)) == 1 and remote.get_version("study", "runs/big", row["uid"])["status"] == "running"  # pending
    assert ended < 25, ended  # the stall timeout (10 s) and a little, not the whole budget of a transfer
    proxy.mode = "forward"
    synced = cli(project, "sync", env=env)
    assert synced.returncode == 0, synced.stdout + synced.stderr
    assert outbox_of(project) == [] and remote.get_version("study", "runs/big", row["uid"])["status"] == "completed"
    verify_clean(project, env)
    proxy.close()
    server.stop()
