"""The repo and its remote under the conditions they are built for, with real processes: offline work that syncs
when the remote comes back (also after a sync killed halfway), two machines sharing one remote, a remote added to a
repo that already numbered versions, a cluster's network filesystem (an outbox, no database, a login node that
sends), many writers on one repo, and processes killed with SIGKILL."""

import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import cairn
from cairn.store import OWN, LocalProject, LocalRepo
from cairn.store.ids import ID

from test_identity import Server
from test_store import folder
from conftest import write_config, config_file

pytest.importorskip("uvicorn")


def project_at(base: Path, name: str, remote: str | None = None, extra: str = "") -> Path:
    """A project folder (its own repo), with the project `study` of the server `remote` as its remote if given."""
    root = base / name
    root.mkdir(parents=True)
    lines = [f'[remote]\nurl = "{remote}/study"'] if remote else []
    lines += ["[repo]", "interval = 0.5"]
    write_config(root, "\n".join(lines) + "\n" + extra)
    return root


def cairn_cli(project: Path, *args, env=None, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cairn", *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env or os.environ.copy(),
    )


def python(project: Path, code: str, env=None, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env or os.environ.copy(),
    )


def outbox(project: Path) -> list[str]:
    base = project / ".cairn" / "outbox"
    return sorted(path.name for path in base.iterdir()) if base.is_dir() else []


def remote_versions(server: Server, name: str) -> list[dict]:
    try:
        return LocalRepo(server.root).versions("study", name)
    except cairn.NotFound:
        return []


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A remote, not started yet (`server.start()`); its token in CAIRN_TOKEN for every process."""
    started = Server(tmp_path / "remote")
    monkeypatch.setenv("CAIRN_TOKEN", started.token)
    for name in ("CAIRN_DIR", "CAIRN_INPUTS", "CAIRN_RUN"):
        monkeypatch.delenv(name, raising=False)
    yield started
    started.stop()


# ------------------------------------------------------------------ offline, then the remote comes back

KILL_DURING = """
import os, signal, sys
import cairn.client.http as http
from cairn.cli import main

where = sys.argv[1]
if where == "upload":  # killed after the first blob reached the remote, before the version was sent
    original = http.Client.upload
    def upload(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os.kill(os.getpid(), signal.SIGKILL)
    http.Client.upload = upload
else:  # killed after the remote took the version, before the repo recorded its number
    import cairn.client.sync as sync
    def done(self, record):
        os.kill(os.getpid(), signal.SIGKILL)
    sync.Sync._done = done
main(["sync"])
"""


PUBLISH_AS = """
import sys
from cairn.cli import main
from cairn.store.ids import chosen
with chosen(sys.argv[1]):  # `cairn publish <folder> data` whose version gets this id
    main(["publish", sys.argv[2], "data"])
"""


def test_offline_publishes_wait_and_the_next_command_syncs_them_once_even_after_killed_syncs(server, tmp_path):
    project = project_at(tmp_path, "study", server.url)
    (project / "kill.py").write_text(KILL_DURING)
    (project / "publish_as.py").write_text(PUBLISH_AS)
    assert cairn_cli(project, "status").returncode == 0 and (project / ".cairn" / "repo.json").is_file()
    ids = [digit * 8 + os.urandom(28).hex() for digit in "f80"]  # each later publish has the smaller id
    for index in range(3):
        data = folder(tmp_path, f"d{index}", {"a.txt": f"version {index}", "big.bin": f"{index}" * 200_000})
        result = subprocess.run(
            [sys.executable, "publish_as.py", ids[index], str(data)], cwd=project, capture_output=True, text=True
        )
        assert result.returncode == 0 and "pending data" in result.stdout, result.stderr
    pending = outbox(project)
    assert pending == sorted(ids)
    status = cairn_cli(project, "status").stdout
    assert len(re.findall(r"^pending +data +- ", status, re.M)) == 3, status
    listing = cairn_cli(project, "versions", "data").stdout  # offline: the repo's pending versions, by short id
    assert len(re.findall(r"^pending +[0-9a-f]{7} +pending ", listing, re.M)) == 3, listing

    server.start()
    killed = subprocess.run(
        [sys.executable, "kill.py", "upload"], cwd=project, capture_output=True, text=True, timeout=120
    )
    assert killed.returncode == -signal.SIGKILL
    assert remote_versions(server, "data") == [] and outbox(project) == pending  # a blob went up, no version
    killed = subprocess.run(
        [sys.executable, "kill.py", "recorded"], cwd=project, capture_output=True, text=True, timeout=120
    )
    assert killed.returncode == -signal.SIGKILL
    assert (
        len(remote_versions(server, "data")) == 1 and outbox(project) == pending
    )  # the remote has it, the repo not yet

    result = cairn_cli(project, "versions", "data")  # the next command sends everything first
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("sent data@v") == 3
    on_remote = remote_versions(server, "data")
    assert [v["version"] for v in on_remote] == [1, 2, 3]
    assert [v["id"] for v in on_remote] == ids  # sent in publish order (`created`), not by id; no duplicates
    assert outbox(project) == []
    assert [line.split()[0] for line in result.stdout.splitlines()[1:]] == ["v1", "v2", "v3"]
    repo = LocalRepo(project / ".cairn")
    assert [(v["version"], v["id"]) for v in repo.versions(OWN, "data")] == [(v["version"], v["id"]) for v in on_remote]
    ids = [v["id"] for v in on_remote]
    assert repo.short_ids(OWN, ids) == LocalRepo(server.root).short_ids("study", ids)  # the same short ids
    assert repo.aliases(OWN, "data") == {"latest": 3}
    for number in (1, 2, 3):  # every version reads back exactly as published
        text = cairn_cli(project, "pull", f"data@v{number}//a.txt", "--no-record").stdout.strip()
        assert Path(text).read_text() == f"version {number - 1}"
    assert "nothing pending" in cairn_cli(project, "status").stdout
    verify = cairn_cli(project, "verify")
    assert verify.returncode == 0, verify.stdout + verify.stderr


# ------------------------------------------------------------------ two machines, one remote


def test_two_machines_with_their_own_repos_share_one_remote(server, tmp_path):
    server.start()
    a = project_at(tmp_path, "machine-a", server.url)
    b = project_at(tmp_path, "machine-b", server.url)
    assert cairn_cli(a, "publish", str(folder(tmp_path, "a1", {"x.txt": "from a"})), "data").returncode == 0
    assert cairn_cli(b, "publish", str(folder(tmp_path, "b1", {"x.txt": "from b"})), "data").returncode == 0
    used = python(
        b,
        """
        import cairn
        version = cairn.use("data@v1")             # machine A's version, used on machine B
        print(version.ref, (version.path / "x.txt").read_text())
        print(cairn.use("data@latest").ref)
    """,
    )
    assert used.returncode == 0, used.stderr
    assert used.stdout.split("\n")[:2] == ["data@v1 from a", "data@v2"]
    copied = LocalRepo(b / ".cairn").get_version(OWN, "data", 1)  # copied into B's repo with the remote's number
    assert (
        copied["origin"] == f"{LocalRepo(server.root).id}/study"
        and copied["id"] == remote_versions(server, "data")[0]["id"]
    )

    server.stop()  # both work offline: each keeps its versions pending, by id
    for machine, text in ((a, "a2"), (b, "b2"), (a, "a3")):
        assert "pending" in cairn_cli(machine, "publish", str(folder(tmp_path, text, {"x.txt": text})), "data").stdout
    offline = python(
        b,
        """
        import cairn
        print((cairn.pull("data@v1//x.txt", record=False)).read_text())  # from B's repo, no remote
    """,
    )
    assert offline.stdout.strip() == "from a", offline.stderr
    server.start()
    for machine in (b, a):
        assert cairn_cli(machine, "sync").returncode == 0
    on_remote = remote_versions(server, "data")
    assert [v["version"] for v in on_remote] == [1, 2, 3, 4, 5] and len({v["id"] for v in on_remote}) == 5
    for machine in (a, b):
        assert outbox(machine) == []
        repo = LocalRepo(machine / ".cairn")
        assert {v["id"]: v["version"] for v in repo.versions(OWN, "data")}.items() <= {
            v["id"]: v["version"] for v in on_remote
        }.items()
    latest = cairn_cli(a, "pull", "data@latest//x.txt", "--no-record").stdout.strip()
    assert Path(latest).read_text() == "a3"  # A's last one was sent last


# ------------------------------------------------------------------ a remote added later


def test_a_remote_added_to_a_repo_takes_over_its_versions_oldest_first(server, tmp_path):
    server.start()
    other = project_at(tmp_path, "other", server.url)  # the remote already has a version of data
    assert cairn_cli(other, "publish", str(folder(tmp_path, "o1", {"x.txt": "other"})), "data").returncode == 0

    project = project_at(tmp_path, "study")  # no remote yet: the repo numbers its versions
    made = python(
        project,
        """
        import secrets, cairn
        from cairn.store.ids import chosen
        repo = cairn.connect()
        repo.artifact("data").versions()  # the repo (and its own id) first
        with chosen("f" * 8 + secrets.token_hex(28), "0" * 8 + secrets.token_hex(28)):  # the later, the smaller id
            one = repo.artifact("data").publish("d1", aliases=["paper"])
            two = repo.artifact("data").publish("d2")
        with cairn.draft("fit") as fit:
            fit.add_json("uses", {"data": cairn.use("data@v1").ref})
        print(one.ref, one.id, two.ref, two.id, fit.version.ref, fit.version.id)
    """.replace('"d1"', repr(str(folder(tmp_path, "d1", {"x.txt": "one"})))).replace(
            '"d2"', repr(str(folder(tmp_path, "d2", {"x.txt": "two"})))
        ),
    )
    assert made.returncode == 0, made.stderr
    one_ref, one_id, two_ref, two_id, fit_ref, fit_id = made.stdout.split()
    assert (one_ref, two_ref, fit_ref) == ("data@v1", "data@v2", "fit@v1")
    assert one_id.startswith("f" * 8) and two_id.startswith("0" * 8)  # by digits, v2 would go first

    with open(config_file(project), "a") as handle:  # now the project gets a remote
        handle.write(f'[remote]\nurl = "{server.url}/study"\n')
    synced = cairn_cli(project, "sync")  # the first command that reaches the remote
    assert synced.returncode == 0, synced.stdout + synced.stderr
    assert "adopted 4 versions this repo numbered itself" in synced.stdout  # data v1, v2, the run, fit
    on_remote = {v["id"]: v for v in remote_versions(server, "data")}
    assert (
        on_remote[one_id]["version"] == 2 and on_remote[two_id]["version"] == 3
    )  # oldest first (by number, not by id), after the remote's v1
    remote = LocalRepo(server.root)
    assert remote.aliases("study", "data")["paper"] == 2  # its alias went along
    fit = remote.get_version("study", "fit", fit_id)
    [run_ref] = fit["inputs"]
    run_name, run = remote.resolve("study", run_ref)
    assert run["metadata"]["kind"] == "run" and run["inputs"] == [one_id]  # the run's input: the same version, by id
    assert remote.get_version("study", "data", one_id)["version"] == 2  # renumbered by the remote
    repo = LocalRepo(project / ".cairn")
    assert (
        repo.local_versions(OWN) == [] and repo.get_version(OWN, "data", one_id)["version"] == 2
    )  # numbers taken over
    assert outbox(project) == []
    shown = cairn_cli(project, "show", "fit@v1")
    shown_input = re.search(r"^inputs +(\S+) \(([0-9a-f]+)\)$", shown.stdout, re.M)  # the run, renumbered
    assert shown_input and shown_input[1] == f"{run_name}@v{run['version']}", shown.stdout + shown.stderr
    assert run_ref.startswith(shown_input[2])
    again = cairn_cli(project, "sync")
    assert "adopted" not in again.stdout and len(remote_versions(server, "data")) == 3  # nothing twice


# ------------------------------------------------------------------ a cluster: the repo on a network filesystem

NFS = """
import cairn.store.fs as fs
import cairn.store.local as local
fs.network_filesystem = local.network_filesystem = lambda path: "nfs4"
"""


def nfs_env(tmp_path: Path) -> dict:
    """An environment whose processes see every path on NFS (a sitecustomize module patches the check)."""
    hook = tmp_path / "nfs-hook"
    hook.mkdir(exist_ok=True)
    (hook / "sitecustomize.py").write_text(NFS)
    return os.environ | {"PYTHONPATH": os.pathsep.join([str(hook), os.environ.get("PYTHONPATH", "")])}


JOB = """
import sys, cairn
run = cairn.init(name="job", config={"n": int(sys.argv[1])})
run.add({"loss": 1.0 / (1 + int(sys.argv[1])), "step": 0})
with cairn.draft("result") as result:
    result.add_json("value", {"n": int(sys.argv[1])})
print(result.version.ref)
"""


def test_on_a_network_filesystem_jobs_keep_an_outbox_and_a_login_node_sends_it(server, tmp_path):
    env = nfs_env(tmp_path)
    alone = project_at(tmp_path, "no-remote")
    refused = cairn_cli(alone, "publish", str(folder(tmp_path, "x", {"a": "1"})), "x", env=env)
    assert (
        refused.returncode == 1
        and "network filesystem (nfs4)" in refused.stderr
        and "cairn remote set" in refused.stderr
    )
    assert not (alone / ".cairn" / "metadata.sqlite3").exists()

    project = project_at(tmp_path, "cluster", server.url)
    (project / "job.py").write_text(JOB)
    jobs = [
        subprocess.Popen(
            [sys.executable, "job.py", str(index)],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(4)
    ]  # compute nodes; the remote is down
    for job in jobs:
        out, err = job.communicate(timeout=180)
        assert job.returncode == 0, err
        assert ID.fullmatch(out.strip())  # pending: its id, no number
    assert len(outbox(project)) == 8  # every run and every result, one folder each
    assert not (project / ".cairn" / "metadata.sqlite3").exists()  # never a database on the network filesystem
    status = cairn_cli(project, "status", env=env)
    assert len(re.findall(r"^pending ", status.stdout, re.M)) == 8, status.stderr
    down = cairn_cli(project, "versions", "result", env=env)
    assert (
        down.returncode == 0 and len(re.findall(r"^pending +[0-9a-f]{7} +pending ", down.stdout, re.M)) == 4
    )  # the outbox

    server.start()
    login = cairn_cli(project, "sync", env=env)  # the login node
    assert login.returncode == 0, login.stderr
    assert outbox(project) == [] and not (project / ".cairn" / "metadata.sqlite3").exists()
    results, runs = remote_versions(server, "result"), remote_versions(server, "runs/job")
    assert [v["version"] for v in results] == [1, 2, 3, 4] and [v["status"] for v in runs] == ["completed"] * 4
    remote = LocalRepo(server.root)
    for result in results:  # each result's input is its own run, by id
        [run_id] = result["inputs"]
        assert remote.resolve("study", run_id)[1]["metadata"]["kind"] == "run"
    pulled = cairn_cli(project, "pull", "result@v2//data/value.json", "--no-record", env=env)
    assert pulled.returncode == 0 and json.loads(Path(pulled.stdout.strip()).read_text())["n"] in range(4)
    server.stop()
    offline = cairn_cli(project, "versions", "result", env=env)
    assert (
        offline.returncode == 1 and "keeps no copies" in offline.stderr
    )  # with no database only the remote can answer


def test_allow_network_fs_keeps_the_database(server, tmp_path):
    project = project_at(tmp_path, "single-writer", extra="allow_network_fs = true\n")
    result = cairn_cli(project, "publish", str(folder(tmp_path, "x", {"a": "1"})), "x", env=nfs_env(tmp_path))
    assert result.returncode == 0 and "published x@v1" in result.stdout, result.stderr
    assert (project / ".cairn" / "metadata.sqlite3").exists()


# ------------------------------------------------------------------ many writers on one repo

WRITER = """
import sys, pathlib, cairn
worker = int(sys.argv[1])
artifact = cairn.connect().artifact("shared")
for index in range(20):
    data = pathlib.Path(f"w{worker}-{index}")
    data.mkdir()
    (data / "value.txt").write_text(f"{worker}:{index}")
    (data / "common.txt").write_text("the same in every version")
    artifact.publish(data, metadata={"worker": worker, "index": index})
print("done")
"""


def test_many_writer_processes_on_one_repo_number_every_version_once(tmp_path):
    project = project_at(tmp_path, "busy")
    (project / "writer.py").write_text(WRITER)
    writers = [
        subprocess.Popen(
            [sys.executable, "writer.py", str(worker)],
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for worker in range(8)
    ]
    for writer in writers:
        out, err = writer.communicate(timeout=300)
        assert writer.returncode == 0 and out.strip() == "done", err
    repo = LocalRepo(project / ".cairn")
    versions = repo.versions(OWN, "shared")
    assert [v["version"] for v in versions] == list(range(1, 161))  # contiguous, none twice
    assert len({v["id"] for v in versions}) == 160
    assert sorted((v["metadata"]["worker"], v["metadata"]["index"]) for v in versions) == [
        (w, i) for w in range(8) for i in range(20)
    ]
    assert repo.aliases(OWN, "shared")["latest"] in range(1, 161)
    assert len(set(repo.short_ids(OWN, [v["id"] for v in versions]).values())) == 160  # one short id each
    for version in versions:  # every version's files are there and are its own
        entry = next(item for item in version["files"] if item["path"] == "value.txt")
        assert (
            repo.blob_path(entry["sha256"]).read_text()
            == f"{version['metadata']['worker']}:{version['metadata']['index']}"
        )
    report = repo.verify()
    assert report.ok, report.lines()
    assert cairn_cli(project, "verify").returncode == 0


# ------------------------------------------------------------------ SIGKILL

SLOW_PUBLISH = """
import os, signal, sys, cairn
import cairn.store.local as local
original = local.LocalRepo._completed
def completed(self, *args, **kwargs):  # inside the publish transaction, after the version row was written
    os.kill(os.getpid(), signal.SIGKILL)
local.LocalRepo._completed = completed
cairn.connect().artifact("data").publish(sys.argv[1])
"""

SLOW_OUTBOX = """
import os, signal, sys, cairn
import cairn.store.outbox as outbox
original = outbox.Outbox.create
def create(self, record, running):  # the entry is being written, not yet renamed into place
    os.kill(os.getpid(), signal.SIGKILL)
outbox.Outbox.create = create
cairn.connect().artifact("data").publish(sys.argv[1])
"""


def test_a_publish_killed_halfway_leaves_no_partial_version(server, tmp_path):
    project = project_at(tmp_path, "local")
    (project / "kill.py").write_text(SLOW_PUBLISH)
    data = folder(tmp_path, "data", {"a.txt": "a", "b.txt": "b"})
    killed = subprocess.run(
        [sys.executable, "kill.py", str(data)], cwd=project, capture_output=True, text=True, timeout=120
    )
    assert killed.returncode == -signal.SIGKILL
    repo = LocalRepo(project / ".cairn")
    assert repo.versions(OWN, "data") == []  # the transaction rolled back: nothing half there
    assert repo.verify().ok
    result = cairn_cli(project, "publish", str(data), "data")
    assert result.returncode == 0 and re.search(
        r"published data@v1 \([0-9a-f]{7}, 2 files", result.stdout
    ), result.stderr

    synced = project_at(tmp_path, "synced", server.url)
    (synced / "kill.py").write_text(SLOW_OUTBOX)
    killed = subprocess.run(
        [sys.executable, "kill.py", str(data)], cwd=synced, capture_output=True, text=True, timeout=120
    )
    assert killed.returncode == -signal.SIGKILL
    assert (
        outbox(synced) == [] and "nothing pending" in cairn_cli(synced, "status").stdout
    )  # no entry, no pending version


RUN_FOREVER = """
import pathlib, time, cairn
run = cairn.init(name="forever", system=False, code=False)
run.add({"loss": 1.0, "step": 0})
pathlib.Path("started").write_text(run.id)
while True:
    time.sleep(0.1)
"""


def test_a_run_killed_with_sigkill_is_marked_failed_with_a_remote_too(server, tmp_path):
    server.start()
    project = project_at(tmp_path, "study", server.url, extra="heartbeat_timeout = 600\n")
    (project / "forever.py").write_text(RUN_FOREVER)
    process = subprocess.Popen(
        [sys.executable, "forever.py"], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    deadline = time.time() + 60
    while not (project / "started").exists() and time.time() < deadline:
        time.sleep(0.05)
    run_id = (project / "started").read_text()
    for _ in range(100):  # created on the remote at once (it answers), and synced while it runs
        if [v["status"] for v in remote_versions(server, "runs/forever")] == ["running"]:
            break
        time.sleep(0.1)
    assert [(v["status"], v["id"]) for v in remote_versions(server, "runs/forever")] == [("running", run_id)]
    process.kill()
    process.communicate(timeout=60)
    status = cairn_cli(project, "status")  # the next command on its host: the process is gone, so it is failed
    assert [(v["status"], v["id"]) for v in remote_versions(server, "runs/forever")] == [
        ("failed", run_id)
    ], status.stderr
    assert outbox(project) == []


IMPLICIT = """
import secrets, cairn
from cairn.store.ids import chosen
cairn.connect().artifact("out").versions()  # the repo (and its own id) exists before the chosen ids are handed out
low, high = "0" * 8 + secrets.token_hex(28), "f" * 8 + secrets.token_hex(28)
with chosen(low, high):              # the draft opens first and takes the smaller id; the run it starts the larger
    with cairn.draft("out") as out:
        out.add_json("x", {"v": 1})
assert (out.version.id, out.record["run"]) == (low, high), (out.version.id, out.record["run"])
print(out.version.ref, out.record["run"])
"""


def test_an_output_whose_id_is_smaller_than_its_runs_is_sent_after_it(server, tmp_path):
    """Sending in id order would send the output first, naming a run the remote does not have yet: the order comes
    from the run->output relationship, never from the digits."""
    project = project_at(tmp_path, "study", server.url)
    made = python(project, IMPLICIT)  # the remote is down: both wait in the outbox
    assert made.returncode == 0, made.stderr
    out_ref, run_id = made.stdout.split()
    assert ID.fullmatch(out_ref) and out_ref < run_id  # pending: ids; by digits the output would come first
    server.start()
    synced = cairn_cli(project, "sync")
    assert synced.returncode == 0 and "rejected" not in synced.stdout, synced.stdout
    assert synced.stdout.index("sent runs/interactive@v1") < synced.stdout.index("sent out@v1"), synced.stdout
    [run] = remote_versions(server, "runs/interactive")
    [out] = remote_versions(server, "out")
    assert run["id"] == run_id and out["id"] == out_ref and out["inputs"] == [run_id]


# ------------------------------------------------------------------ the sync engine alone, against a stand-in remote


class LocalRemote(LocalProject):
    """A project of a LocalRepo standing in for a remote: the sync engine needs only the repo interface (and `upload`)."""

    url = "http://stand-in"
    calls = 0
    fail_after: int | None = None

    @property
    def _id(self):
        return self.id

    def _tick(self):
        type(self).calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise cairn.Unreachable("the stand-in remote went away")

    def upload(self, sources, cache, quiet=False, what=None):
        for sha, (path, size) in sources.items():
            self._tick()
            with open(path, "rb") as handle:
                self.put_blob(sha, handle, size)

    def publish(self, *args, **kwargs):
        self._tick()
        return super().publish(*args, **kwargs)


def test_the_sync_engine_against_a_stand_in_remote_is_idempotent_by_id(tmp_path):
    from cairn.client.sync import Sync
    from cairn.store.outbox import Outbox
    from cairn.store.blobs import BlobStore
    from cairn.store.ids import new_id

    remote = LocalRemote(LocalRepo(tmp_path / "remote"), "study")
    remote.create()
    root = tmp_path / "repo"
    copies = LocalProject(LocalRepo(root))
    copies.create()
    outbox = Outbox(root)
    entries = []
    for name, text in (("a.txt", "a"), ("b.txt", "b")):
        source = tmp_path / name
        source.write_text(text)
        import hashlib

        entry = {"path": name, "sha256": hashlib.sha256(text.encode()).hexdigest(), "size": 1}
        BlobStore(root).stage_file(entry, source)
        entries.append(entry)
    version_id = new_id()
    record = {
        "id": version_id,
        "kind": "publish",
        "sync": "pending",
        "status": "completed",
        "url": remote.url,
        "store": None,
        "name": "data",
        "label": "x",
        "metadata": {},
        "inputs": [],
        "aliases": ["best"],
        "source": None,
        "files": entries,
        "created": "2026-10-15T00:00:00+00:00",
        "owner": {"host": "h", "pid": 1},
        "number": None,
        "pushed": {},
        "error": None,
    }
    outbox.create(record, running=False)
    sync = Sync(outbox, remote, copies, root, 600)
    LocalRemote.fail_after = 1  # the first blob goes up, then the remote is gone
    result = sync.flush()
    assert result["waiting"] and remote.versions("data") == [] and outbox.load(version_id)
    LocalRemote.fail_after = None
    result = sync.flush()
    assert [entry["id"] for entry in result["sent"]] == [version_id] and outbox.load(version_id) is None
    assert copies.get_version("data", 1)["id"] == version_id and copies.aliases("data") == {"latest": 1, "best": 1}
    assert copies.meta("remote") == f"{remote.id}/study"  # the repo is bound to the server and the project
    outbox.create({**record, "pushed": {}}, running=False)  # the same entry again (a crash after the remote's answer)
    assert len(sync.flush()["sent"]) == 1 and [v["id"] for v in remote.versions("data")] == [version_id]


# ------------------------------------------------------------------ cairn sync --watch (a login node), the UI, changes offline


def test_sync_watch_sends_what_jobs_leave_once_the_remote_answers(server, tmp_path):
    project = project_at(tmp_path, "study", server.url)
    assert "pending" in cairn_cli(project, "publish", str(folder(tmp_path, "d", {"a": "1"})), "data").stdout
    watcher = subprocess.Popen(
        [sys.executable, "-m", "cairn", "sync", "--watch"],
        cwd=project,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(1.0)
        assert watcher.poll() is None and remote_versions(server, "data") == []  # keeps going while the remote is down
        server.start()
        assert cairn_cli(project, "publish", str(folder(tmp_path, "e", {"a": "2"})), "data").returncode == 0
        for _ in range(200):
            if len(remote_versions(server, "data")) == 2:
                break
            time.sleep(0.1)
        assert [v["version"] for v in remote_versions(server, "data")] == [1, 2] and outbox(project) == []
    finally:
        watcher.terminate()
        watcher.communicate(timeout=30)


def test_the_ui_of_a_repo_with_a_remote_shows_pending_versions(server, tmp_path, monkeypatch):
    from cairn.config import load_config
    from cairn.ui.backends import open_backend

    server.start()
    project = project_at(tmp_path, "study", server.url)
    monkeypatch.chdir(project)
    cairn.publish("data", folder(tmp_path, "d1", {"a.txt": "one"}))
    server.stop()
    pending = cairn.connect().artifact("data").publish(folder(tmp_path, "d2", {"a.txt": "two"}))
    assert pending.pending and pending.number is None
    projects = open_backend(config=load_config(project), no_auth=True).store
    assert [item["name"] for item in projects.overviews()] == ["study"]  # one project: the remote's
    store = projects.get("study")
    rows = store.versions("data")  # the repo's copy of v1, then the pending one
    assert [(row["version"], row.get("pending", False)) for row in rows] == [(1, False), (None, True)]
    assert rows[-1]["id"] == pending.id and rows[-1]["status"] == "pending"
    manifest = store.get_version("data", pending.id)
    assert manifest["pending"] and b"".join(store.read_file(f"{pending.id}//a.txt").chunks) == b"two"
    assert b"".join(store.read_file("data@v1//a.txt").chunks) == b"one"  # from the repo: the remote is down
    assert any(item["name"] == "data" for item in store.summaries())


def test_changes_to_the_source_of_truth_fail_clearly_offline(server, tmp_path, monkeypatch):
    server.start()
    project = project_at(tmp_path, "study", server.url)
    monkeypatch.chdir(project)
    cairn.publish("data", folder(tmp_path, "d1", {"a.txt": "one"}))
    server.stop()
    for argv in (["alias", "data@v1", "best"], ["delete", "data@v1", "--force"]):
        result = cairn_cli(project, *argv)
        assert result.returncode == 1 and "needs the remote" in result.stderr and "not reachable" in result.stderr, argv
    server.start()
    assert cairn_cli(project, "alias", "data@v1", "best").returncode == 0
    assert LocalRepo(project / ".cairn").aliases(OWN, "data")["best"] == 1  # set there, then mirrored here
    assert cairn_cli(project, "delete", "data@v1", "--force", "--reason", "test").returncode == 0
    assert LocalRepo(project / ".cairn").get_version(OWN, "data", 1)["status"] == "deleted"  # the tombstone, mirrored


def test_a_remote_that_drops_packets_does_not_hold_up_reads_or_publishes(tmp_path, monkeypatch):
    """A remote host that never answers (connected, then silent, like a hung server or dropped packets): the first look
    gives up after a few seconds, and the repo then stops asking it for a while, so reads answer from the repo and
    publishes are pending at once."""
    import socket

    import cairn.client.http as http_module
    import cairn.client.repo as repo_module

    listener = socket.socket()  # accepts no connection: its backlog is full, so connecting hangs like a dropped packet
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    fillers = []
    for _ in range(8):
        filler = socket.socket()
        filler.setblocking(False)
        filler.connect_ex(listener.getsockname())
        fillers.append(filler)
    monkeypatch.setattr(http_module, "CONNECT_TIMEOUT", 2.5)
    monkeypatch.setattr(repo_module, "PROBE", 2.5)
    monkeypatch.setattr(repo_module, "start_uploader", lambda *args, **kwargs: None)
    monkeypatch.setenv("CAIRN_TOKEN", "t" * 40)
    project = project_at(tmp_path, "study", f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.chdir(project)
    try:
        repo = cairn.connect()
        began = time.monotonic()
        first = repo.artifact("data").publish(folder(tmp_path, "d1", {"a.txt": "1"}))
        assert first.pending and time.monotonic() - began < 10
        began = time.monotonic()
        second = repo.artifact("data").publish(folder(tmp_path, "d2", {"a.txt": "2"}))
        assert second.pending and time.monotonic() - began < 2  # not asked again: pending at once
        began = time.monotonic()
        assert [v.id for v in repo.artifact("data").versions()] == [first.id, second.id]
        assert time.monotonic() - began < 2
    finally:
        for filler in fillers:
            filler.close()
        listener.close()


def test_many_processes_opening_a_new_repo_at_once(tmp_path):
    """A job array starting together on a project without a repo yet: every process opens (and creates) it."""
    code = "import cairn; cairn.connect().artifact('x').versions(); print('ok')"
    for round in range(5):
        project = project_at(tmp_path, f"fresh-{round}")
        starts = [
            subprocess.Popen(
                [sys.executable, "-c", code], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            for _ in range(8)
        ]
        for start in starts:
            out, err = start.communicate(timeout=120)
            assert start.returncode == 0 and out.strip() == "ok", err


# ------------------------------------------------------------------ what a review of the sync found (each fixed)


@pytest.fixture
def online(server, tmp_path, monkeypatch):
    """A project in this process whose remote is the server; background uploads off (the tests send by hand)."""
    import cairn.client.repo as repo_module

    monkeypatch.setattr(repo_module, "start_uploader", lambda *args, **kwargs: None)
    project = project_at(tmp_path, "study", server.url)
    monkeypatch.chdir(project)
    return project


def test_a_pending_version_the_remote_finds_unchanged_hands_its_consumers_the_remotes_version(
    server, online, tmp_path, monkeypatch
):
    server.start()
    other = project_at(tmp_path, "other", server.url)
    assert cairn_cli(other, "publish", str(folder(tmp_path, "same", {"x.txt": "same"})), "a").returncode == 0  # a@v1
    server.stop()
    repo = cairn.connect()  # this repo never saw a@v1: its publish of the same files is pending
    pending = repo.artifact("a").publish(folder(tmp_path, "same2", {"x.txt": "same"}))
    consumer = repo.artifact("b").publish(folder(tmp_path, "b", {"y.txt": "b"}), inputs=[pending.ref])
    assert pending.pending and consumer.inputs == [pending.id]
    server.start()
    result = repo.sync()
    assert not result["rejected"], result["lines"]
    assert [v["version"] for v in remote_versions(server, "a")] == [1]  # the remote kept its own version
    [a] = remote_versions(server, "a")
    [b] = remote_versions(server, "b")
    assert a["id"] != pending.id and b["inputs"] == [a["id"]] and outbox(online) == []  # the remote's version, by id


def test_a_direct_publish_sends_the_pending_versions_it_names_first(server, online, tmp_path):
    repo = cairn.connect()
    first = repo.artifact("a").publish(folder(tmp_path, "a", {"x.txt": "a"}))
    assert first.pending
    server.start()
    second = cairn.connect().artifact("b").publish(folder(tmp_path, "b", {"y.txt": "b"}), inputs=[first.ref])
    assert second.number == 1 and second.inputs == [first.id]
    assert [v["id"] for v in remote_versions(server, "a")] == [first.id] and outbox(online) == []


def test_a_refused_token_leaves_versions_pending_not_rejected(server, online, tmp_path, monkeypatch):
    server.start()
    monkeypatch.setenv("CAIRN_TOKEN", "x" * 43)
    with pytest.raises(cairn.Unauthorized, match="refused"):
        cairn.connect().artifact("a").publish(folder(tmp_path, "a", {"x.txt": "a"}))
    status = cairn_cli(online, "status")
    assert re.search(r"\npending +a +- ", status.stdout), status.stdout  # pending, not rejected
    monkeypatch.setenv("CAIRN_TOKEN", server.token)
    assert cairn_cli(online, "sync").returncode == 0 and [v["version"] for v in remote_versions(server, "a")] == [1]


def test_an_output_of_a_run_that_failed_before_anything_was_sent_is_kept(server, online, tmp_path):
    made = python(
        online,
        """
        import cairn
        run = cairn.init(name="train", system=False, code=False)
        with cairn.draft("checkpoint") as checkpoint:
            checkpoint.add_json("state", {"step": 1})
        raise SystemExit(2)                             # the run fails after its output was published
    """,
    )
    assert made.returncode == 2
    server.start()
    synced = cairn_cli(online, "sync")
    assert synced.returncode == 0 and "rejected" not in synced.stdout, synced.stdout
    [run] = remote_versions(server, "runs/train")
    [checkpoint] = remote_versions(server, "checkpoint")
    assert run["status"] == "failed" and run["version"] == 1 and checkpoint["inputs"] == [run["id"]]


KILLED_AFTER_ITS_OUTPUT = """
import os, pathlib, signal, time, cairn
run = cairn.init(name="train", system=False, code=False)
pathlib.Path("started").write_text(run.id)
while not pathlib.Path("go").exists():  # meanwhile the remote went away
    time.sleep(0.05)
with cairn.draft("checkpoint") as checkpoint:
    checkpoint.add_json("state", {"step": 1})
pathlib.Path("published").write_text(checkpoint.version.id)
os.kill(os.getpid(), signal.SIGKILL)  # the job dies after its output: no exit handler, the run stays running
"""


def test_an_output_of_a_run_killed_after_it_is_sent_before_the_run_is_failed(server, tmp_path):
    """A run the remote has (running), killed after its output was published offline: the next sync fails the run
    (its process is gone) and sends the output first, while the run is still running on the remote, then the failure.
    Failing the run first would make the remote refuse the output for good (an input that failed)."""
    server.start()
    project = project_at(tmp_path, "study", server.url)
    (project / "job.py").write_text(KILLED_AFTER_ITS_OUTPUT)
    process = subprocess.Popen(
        [sys.executable, "job.py"], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    deadline = time.time() + 60
    while not (project / "started").exists() and time.time() < deadline:
        time.sleep(0.05)
    run_id = (project / "started").read_text()
    for _ in range(100):  # created on the remote at once: it has its number
        if [v["status"] for v in remote_versions(server, "runs/train")] == ["running"]:
            break
        time.sleep(0.1)
    assert [v["status"] for v in remote_versions(server, "runs/train")] == ["running"]
    server.stop()
    (project / "go").write_text("1")
    process.communicate(timeout=60)
    assert process.returncode == -signal.SIGKILL
    output_id = (project / "published").read_text()
    assert sorted(outbox(project)) == sorted([run_id, output_id])
    server.start()
    synced = cairn_cli(project, "sync")
    assert synced.returncode == 0 and "rejected" not in synced.stdout + synced.stderr, synced.stdout + synced.stderr
    [run] = remote_versions(server, "runs/train")
    [checkpoint] = remote_versions(server, "checkpoint")
    assert run["status"] == "failed" and checkpoint["status"] == "completed" and checkpoint["inputs"] == [run_id]
    assert outbox(project) == []


def test_a_failed_run_waits_while_another_sender_has_its_output(server, tmp_path):
    """Two senders of one outbox: while another one holds the output's claim, the failed run is not sent (the remote
    would then refuse the output that names it); once the output is on the remote, the run's failure follows."""
    import socket

    server.start()
    project = project_at(tmp_path, "study", server.url)
    (project / "job.py").write_text(KILLED_AFTER_ITS_OUTPUT)
    process = subprocess.Popen(
        [sys.executable, "job.py"], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    deadline = time.time() + 60
    while not (project / "started").exists() and time.time() < deadline:
        time.sleep(0.05)
    run_id = (project / "started").read_text()
    for _ in range(100):
        if [v["status"] for v in remote_versions(server, "runs/train")] == ["running"]:
            break
        time.sleep(0.1)
    server.stop()
    (project / "go").write_text("1")
    process.communicate(timeout=60)
    output_id = (project / "published").read_text()
    claim = project / ".cairn" / "outbox" / output_id / f".flushing-{socket.gethostname()}-{os.getpid()}"
    claim.write_text(json.dumps({"time": time.time(), "host": socket.gethostname(), "pid": os.getpid()}))  # alive
    server.start()
    first = cairn_cli(project, "sync")
    assert "rejected" not in first.stdout + first.stderr, first.stdout + first.stderr
    assert [v["status"] for v in remote_versions(server, "runs/train")] == ["running"]  # not failed before its output
    assert remote_versions(server, "checkpoint") == []
    claim.unlink()  # the other sender is done (here: it went away without sending)
    second = cairn_cli(project, "sync")
    assert second.returncode == 0 and "rejected" not in second.stdout + second.stderr, second.stdout + second.stderr
    [run] = remote_versions(server, "runs/train")
    [checkpoint] = remote_versions(server, "checkpoint")
    assert run["status"] == "failed" and checkpoint["status"] == "completed" and checkpoint["inputs"] == [run_id]
    assert outbox(project) == []


LOST_ANSWER = """
import sys, cairn
import cairn.client.open as open_module
import cairn.client.remote as remote
import cairn.client.sync as sync
import cairn.store.fs as fs
import cairn.store.local as local
from cairn.store.errors import Unreachable

fs.network_filesystem = local.network_filesystem = open_module.network_filesystem = lambda path: "nfs4"
gone = []
call, send = remote.Remote._call, sync.Sync.send


def unreachable_once_gone(self, *args, **kwargs):
    if gone:
        raise Unreachable(f"{self.url} not reachable: connection reset")
    return call(self, *args, **kwargs)


def sent_by_another_sender(self, record, claim=True):
    sent = send(self, record, claim)  # the version reaches the remote and leaves the outbox (a login node sent it)
    if record["name"] == "result":
        gone.append(True)  # and then the remote stops answering this job: its own answer is lost
        raise Unreachable(f"{self.remote.url} not reachable: connection reset")
    return sent


remote.Remote._call, sync.Sync.send = unreachable_once_gone, sent_by_another_sender
run = cairn.init(name="job", code=False, system=False)
with cairn.draft("result") as result:
    result.add_json("value", {"n": 1})
print(result.version.id)
"""


def test_a_job_whose_version_another_sender_sent_while_the_remote_went_away_does_not_fail(server, tmp_path):
    """On a network filesystem (no database, so no copies): a job's publish loses the remote's answer, while the
    version did reach the remote and left the outbox (the login node sent it); the remote then stops answering. The
    job must go on with its version (the remote has it), not fail with Unreachable."""
    server.start()
    project = project_at(tmp_path, "study", server.url)
    (project / "job.py").write_text(LOST_ANSWER)
    done = subprocess.run([sys.executable, "job.py"], cwd=project, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert not (project / ".cairn" / "metadata.sqlite3").exists()
    [result] = remote_versions(server, "result")
    assert result["id"] == done.stdout.split()[-1] and result["status"] == "completed"


def test_a_script_adopts_the_repos_own_versions_before_it_reads(server, tmp_path, monkeypatch):
    import cairn.client.repo as repo_module

    monkeypatch.setattr(repo_module, "start_uploader", lambda *args, **kwargs: None)
    project = project_at(tmp_path, "study")
    monkeypatch.chdir(project)
    local = cairn.connect().artifact("a").publish(folder(tmp_path, "a", {"x.txt": "mine"}))
    assert local.number == 1
    with open(config_file(project), "a") as handle:
        handle.write(f'[remote]\nurl = "{server.url}/study"\n')
    server.start()
    used = cairn.connect().version("a@latest")  # first contact: the repo's own versions are sent first
    assert used.id == local.id and used.number == 1 and not used.pending
    assert [v["id"] for v in remote_versions(server, "a")] == [local.id]


def test_claims_work_where_hard_links_do_not(server, online, tmp_path, monkeypatch):
    import cairn.store.outbox as outbox_module

    def no_links(*args, **kwargs):
        raise PermissionError("no hard links here")

    monkeypatch.setattr(outbox_module.os, "link", no_links)
    server.start()
    version = cairn.connect().artifact("a").publish(folder(tmp_path, "a", {"x.txt": "a"}))
    assert version.number == 1 and outbox(online) == []


def test_the_remote_fails_a_running_version_no_client_syncs_any_more(server, tmp_path):
    from cairn.server.app import reap_schedule

    server.start()
    remote = server.remote()
    remote.create()
    number = remote.start_version("runs/lost", label="x")  # a job that started, then its node went away
    remote.keep_alive("runs/lost", number)
    stop = reap_schedule(server.httpd.app, every=0.1, timeout=0.5, say=lambda line: None)
    try:
        for _ in range(100):
            if remote_versions(server, "runs/lost")[0]["status"] == "failed":
                break
            time.sleep(0.05)
    finally:
        stop.set()
    [lost] = remote_versions(server, "runs/lost")
    assert lost["status"] == "failed" and "no heartbeat" in lost["error"]


def test_prune_removes_an_outbox_folder_whose_removal_a_crash_interrupted(tmp_path):
    from cairn.store.local import outbox_blobs
    from cairn.store.prune import prune

    root = tmp_path / "repo"
    leftover = root / "outbox" / "0190" / "manifest.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_text(
        json.dumps({"id": "0190", "sync": "synced", "files": [{"path": "a", "sha256": "f" * 64, "size": 1}]})
    )
    assert outbox_blobs(root) == set()  # sent: its blobs are not held for it
    prune(root, 1e9)
    assert not leftover.parent.exists()
