"""`cairn upgrade`: repos made by the real Cairn 0.14.0 (installed from the tag into a virtual environment of its own)
are converted, and everything they named still names the same version.

The 0.14 side builds, with 0.14's own CLI and Python API: a server repo with two projects (alpha and beta); a
project repo without a remote (datasets, a run with metrics, a draft output whose input is the run, a kept draft, a
report with pinned inputs, aliases, a deleted version); and a client of the server whose outbox holds a version the
server never got (it was down). Then each repo is upgraded with this Cairn and compared with what 0.14 said.
"""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cairn
from cairn import LocalRepo
from cairn.cli import main
from cairn.store import OWN, StoreError
from cairn.store.db import ServerLock
from cairn.store.upgrade import BACKUP, layout, legacy_id, upgrade

from conftest import read_until, write

TAG = "v0.14.0"
VENV = Path(os.environ.get("CAIRN_014_VENV", "/tmp/cairn-0.14.0-venv"))


def _source() -> Path:
    """The git repository this checkout belongs to (a worktree's main repository)."""
    here = Path(__file__).resolve().parent
    common = subprocess.run(
        ["git", "-C", str(here), "rev-parse", "--git-common-dir"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return (here / common).resolve().parent if not Path(common).is_absolute() else Path(common).parent


@pytest.fixture(scope="session")
def old():
    """The `cairn` and `python` of Cairn 0.14.0, installed once into VENV (skipped where that is impossible)."""
    python, command = VENV / "bin" / "python", VENV / "bin" / "cairn"
    version = subprocess.run([str(command), "--version"], capture_output=True, text=True) if command.exists() else None
    if version is None or "0.14.0" not in version.stdout:
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv is needed to install Cairn 0.14.0")
        shutil.rmtree(VENV, ignore_errors=True)
        made = subprocess.run([uv, "venv", "-q", str(VENV), "--python", f"{sys.version_info[0]}.{sys.version_info[1]}"])
        spec = f"cairn[server] @ git+file://{_source()}@{TAG}"
        installed = subprocess.run(
            [uv, "pip", "install", "-q", "--python", str(python), spec], capture_output=True, text=True
        )
        if made.returncode or installed.returncode:
            pytest.skip(f"could not install Cairn 0.14.0: {installed.stderr[-500:]}")
    return {"python": str(python), "cairn": str(command)}


def run(old, *args, cwd, check=True, env=None):
    """A command of Cairn 0.14 (`cairn ...` or `python -c ...`) in `cwd`."""
    environment = {**os.environ, "PYTHONPATH": "", **(env or {})}
    for name in ("CAIRN_DIR", "CAIRN_INPUTS", "CAIRN_RUN", "CAIRN_TOKEN"):
        if name not in (env or {}):
            environment.pop(name, None)
    command = [old["python"], "-c", args[1]] if args[0] == "python" else [old["cairn"], *args]
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=environment, timeout=180)
    if check and done.returncode:
        raise AssertionError(f"0.14 {args}: {done.returncode}\n{done.stdout}\n{done.stderr}")
    return done


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Served:
    """A `cairn server` process (0.14's or this one's) on a repo folder; always stopped by the test."""

    def __init__(self, command: list[str], root: Path, port: int):
        self.root, self.port, self.url = root, port, f"http://127.0.0.1:{port}"
        self.process = subprocess.Popen(
            [*command, "server", str(root), "--port", str(port), "--no-ui"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={key: value for key, value in os.environ.items() if not key.startswith("CAIRN_")} | {"PYTHONPATH": ""},
        )
        lines = read_until(self.process, lambda line: "Serving" in line, timeout=60)
        assert any("Serving" in line for line in lines), lines

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(10)
        if self.process.stdout:
            self.process.stdout.close()


SNAPSHOT = r"""
import json, sqlite3, sys
connection = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "metadata.sqlite3")
rows = connection.execute("SELECT project, name, number, uid, label, status, manifest FROM versions").fetchall()
versions = {}
for project, name, number, uid, label, status, text in rows:
    manifest = json.loads(text)
    versions[f"{project}|{name}@v{number}"] = {
        "uid": uid, "short_id": manifest.get("short_id"), "label": label, "status": status,
        "digest": manifest.get("digest"), "inputs": sorted(manifest.get("inputs") or []),
        "files": sorted((entry["path"], entry.get("sha256")) for entry in manifest.get("files") or []),
        "metadata": manifest.get("metadata"),
    }
aliases = {f"{p}|{n}@{a}": number for p, n, a, number in connection.execute("SELECT project, name, alias, number FROM aliases")}
consumers = sorted(f"{p}|{i} -> {c}" for p, i, c in connection.execute("SELECT project, input, consumer FROM consumers"))
print(json.dumps({"versions": versions, "aliases": aliases, "consumers": consumers}))
"""


def snapshot_014(old, root: Path) -> dict:
    """What a 0.14 repo's database says (read with 0.14's Python, straight from SQLite)."""
    done = run(old, "python", SNAPSHOT.replace("sys.argv[1] if len(sys.argv) > 1 else ", ""), cwd=root)
    return json.loads(done.stdout)


def by_number(local: LocalRepo) -> dict[str, tuple[str, str, int]]:
    """id -> (project, name, number) of every version of an upgraded repo."""
    with local._db.read() as connection:
        return {
            id: (project, name, number)
            for project, name, number, id in connection.execute("SELECT project, name, number, id FROM versions")
        }


def assert_same_versions(before: dict, root: Path, dropped: set[str] = frozenset()) -> None:
    """Every version, alias, input, and consumer edge of the 0.14 repo names the same version after the upgrade, by
    number and content; the version's id is `legacy_id(uid)`, and its 0.14 short id still names it."""
    local = LocalRepo(root, where="")
    known = by_number(local)
    for key, old_version in before["versions"].items():
        project, ref = key.split("|")
        if ref in dropped:
            continue
        name, version = local.find(project, ref)
        assert version["id"] == legacy_id(old_version["uid"]), key
        assert (version["label"], version["status"], version.get("digest")) == (
            old_version["label"],
            old_version["status"],
            old_version["digest"],
        ), key
        assert sorted([entry["path"], entry.get("sha256")] for entry in version["files"]) == old_version["files"]
        inputs = sorted(f"{known[id][1]}@v{known[id][2]}" for id in version["inputs"])
        assert inputs == old_version["inputs"], key  # 0.14 stored inputs as name@v<n>
        if old_version["short_id"]:
            assert local.find(project, old_version["short_id"])[1]["id"] == version["id"], key
        metadata = old_version["metadata"] or {}
        if isinstance(metadata.get("sweep"), dict) and metadata["sweep"].get("uid"):
            assert version["metadata"]["sweep"]["id"] == legacy_id(metadata["sweep"]["uid"])
    for key, number in before["aliases"].items():
        project, ref = key.split("|")
        if any(ref.startswith(item.split("@")[0] + "@") for item in dropped):
            continue
        assert local.find(project, ref)[1]["version"] == number, key
    after = set()
    for project, name, number in known.values():
        lineage = local.lineage(project, f"{name}@v{number}")
        for consumer in lineage["consumers"]:
            after.add(f"{project}|{name}@v{number} -> {known[consumer][1]}@v{known[consumer][2]}")
    assert after == {edge for edge in before["consumers"] if not any(item in edge for item in dropped)}


def make_project(old, folder: Path, remote: str | None = None) -> Path:
    """A 0.14 project: datasets (data v1, v2 labelled, v3 deleted), aliases, a run that pulls data@best and writes
    metrics, a draft output of the run (model), a kept draft, a report with pinned inputs."""
    folder.mkdir(parents=True)
    run(old, "init", cwd=folder)
    if remote:
        run(old, "remote", "set", remote, cwd=folder)
    write(folder / "d1" / "a.txt", "one")
    write(folder / "d2" / "a.txt", "two")
    write(folder / "d3" / "a.txt", "three")
    run(old, "publish", "d1", "data", cwd=folder)
    run(old, "publish", "d2", "data", "--label", "second", "--alias", "paper", cwd=folder)
    run(old, "alias", "data@v1", "best", cwd=folder)
    run(old, "publish", "d3", "data", cwd=folder)
    run(old, "delete", "data@v3", "--reason", "bad", cwd=folder)
    script = (
        "import cairn\n"
        "run = cairn.init(name='train', config={'lr': 0.1})\n"
        "data = cairn.use('data@best')\n"
        "for step in range(3):\n"
        "    run.add({'loss': 1.0 / (step + 1), 'step': step})\n"
        "with cairn.draft('model') as model:\n"
        "    model.add_json('weights', {'w': [1, 2]})\n"
        "print(model.version.ref)\n"
    )
    run(old, "python", script, cwd=folder)
    kept = (
        "import cairn\n"
        "try:\n"
        "    with cairn.draft('model', label='kept') as model:\n"
        "        model.add_json('weights', {'w': [3]})\n"
        "        raise RuntimeError('diverged')\n"
        "except RuntimeError:\n"
        "    pass\n"
    )
    run(old, "python", kept, cwd=folder)
    write(
        folder / "reports" / "pinned" / "report.md",
        "---\ntitle: Pinned\ninputs:\n  data: data@v1\n  model: model@v1\n---\n\nThe data and the model.\n",
    )
    run(old, "publish", "reports/pinned", cwd=folder)
    return folder / ".cairn"


@pytest.fixture
def old_repos(old, tmp_path):
    """The 0.14 repos: a server repo with projects alpha and beta (the server stopped again), a project without a
    remote, and a client of beta whose outbox holds a version the server never got."""
    server_root = tmp_path / "server-repo"
    port = free_port()
    served = Served([old["cairn"]], server_root, port)
    clients = {}
    try:
        env = {"CAIRN_TOKEN": (server_root / "token").read_text().strip()}
        for project in ("alpha", "beta"):
            folder = tmp_path / f"client-{project}"
            folder.mkdir()
            run(old, "init", cwd=folder)
            (folder / ".cairn" / "token").write_text(env["CAIRN_TOKEN"] + "\n")
            os.chmod(folder / ".cairn" / "token", 0o600)
            run(old, "remote", "set", f"{served.url}/{project}", cwd=folder)
            write(folder / "d" / "a.txt", project)
            run(old, "publish", "d", "data", "--alias", "first", cwd=folder)
            write(folder / "d" / "a.txt", project + "2")
            run(old, "publish", "d", "data", cwd=folder)
            run(old, "pull", "data@v1", cwd=folder)  # a read-only folder, named by uid
            clients[project] = folder
    finally:
        served.stop()
    beta = clients["beta"]  # the server is down now: this version waits in the outbox, its input a copy
    write(beta / "o" / "r.txt", "result")
    pending = run(
        old,
        "python",
        "import os, cairn\n"
        "repo = cairn.connect()\n"
        "repo.pull('data@v1')\n"  # outside a run: recorded in the working directory's inputs file
        "os.rename('.cairn-inputs.jsonl', 'o/.cairn-inputs.jsonl')\n"
        "v = repo.artifact('result').publish('o')\n"
        "print(v.manifest['uid'])\n",
        cwd=beta,
    ).stdout.split()[-1]
    standalone = make_project(old, tmp_path / "standalone")
    return {
        "server": server_root,
        "standalone": standalone,
        "beta": beta / ".cairn",
        "alpha": clients["alpha"] / ".cairn",
        "pending": pending,
        "port": port,
    }


def test_upgrading_real_014_repos_keeps_every_reference(old, old_repos, tmp_path, monkeypatch, capsys):
    server_root, standalone, beta = old_repos["server"], old_repos["standalone"], old_repos["beta"]
    before = {name: snapshot_014(old, old_repos[name]) for name in ("server", "standalone", "beta")}
    drafts_before = sorted(path.name for path in (standalone / "drafts").rglob("*.draft.json"))
    assert layout(server_root) == layout(standalone) == layout(beta) == 1

    # every other command refuses a 0.14 repo, naming `cairn upgrade`
    monkeypatch.chdir(standalone.parent)
    with pytest.raises(SystemExit, match=r"this repo was made by Cairn 0.14: run `cairn upgrade`"):
        main(["versions", "data"])
    with pytest.raises(StoreError, match="run `cairn upgrade`"):
        LocalRepo(server_root)
    with pytest.raises(SystemExit, match="run `cairn upgrade`"):
        main(["verify", "--repo", str(server_root)])

    # refused while a server holds the lock
    lock = ServerLock(server_root)
    lock.acquire()
    try:
        with pytest.raises(StoreError, match="cairn server"):
            upgrade(server_root)
    finally:
        lock.release()
    assert layout(server_root) == 1 and not (server_root / BACKUP).exists()

    # the upgrades
    for name in ("server", "standalone", "beta"):
        main(["upgrade", str(old_repos[name])])
        out = capsys.readouterr().out
        assert "upgraded" in out and "no problems" in out, out
        assert layout(old_repos[name]) == 2 and (old_repos[name] / BACKUP).is_file()
        backup = sqlite3.connect(old_repos[name] / BACKUP)
        assert backup.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0] == "1"
        backup.close()
    record = json.loads((server_root / "repo.json").read_text())
    assert "uid" not in record and record["layout"] == 2 and len(record["id"]) == 64

    # a second time: it is a 0.15 repo already
    with pytest.raises(SystemExit, match="already"):
        main(["upgrade", str(server_root)])

    # every reference, alias, input, and lineage edge names the same version; verify is clean
    assert_same_versions(before["server"], server_root)
    assert_same_versions(before["standalone"], standalone)
    assert_same_versions(before["beta"], beta)
    for name in ("server", "standalone", "beta"):
        main(["verify", "--repo", str(old_repos[name])])
        assert "no problems" in capsys.readouterr().out
    assert {"alpha", "beta"} <= {project["name"] for project in LocalRepo(server_root).projects()}

    # the drafts: renamed to the id, their run input by id; the kept one publishes
    monkeypatch.chdir(standalone.parent)
    drafts_after = sorted(path.name for path in (standalone / "drafts").rglob("*.draft.json"))
    assert drafts_after == sorted(f"{legacy_id(name[: -len('.draft.json')])}.draft.json" for name in drafts_before)
    main(["drafts"])
    listing = capsys.readouterr().out
    assert "model" in listing and "kept" in listing
    kept = next(path for path in (standalone / "drafts").rglob("*.draft.json") if '"kept"' in path.read_text())
    record = json.loads(kept.read_text())
    assert "uid" not in record and record["id"] == kept.name[: -len(".draft.json")]
    run_of_kept = cairn.connect().version(record["run"])  # the run's id (it was `runs/interactive@<uid>`)
    assert run_of_kept.artifact.name == "runs/interactive" and run_of_kept.status == "completed"
    main(["drafts", "publish", record["id"][:12]])
    model = cairn.connect().version("model@v2")
    assert model.inputs == [run_of_kept.id] and model.label == "kept" and model.id == record["id"]

    # the report rebuilds, pinned to the same versions
    from cairn.config import load_config
    from cairn.reports.build import build_all

    build = build_all(load_config(standalone.parent), report="pinned")[0]
    assert {name: item["resolved"] for name, item in build.inputs.items()} == {"data": "data@v1", "model": "model@v1"}
    assert build.inputs["data"]["id"] == legacy_id(before["standalone"]["versions"]["|data@v1"]["uid"])

    # the client's pending version syncs to the upgraded server, its input the same version
    served = Served([str(Path(sys.executable).parent / "cairn")], server_root, old_repos["port"])
    try:
        monkeypatch.chdir(beta.parent)
        monkeypatch.setenv("CAIRN_TOKEN", (server_root / "token").read_text().strip())
        assert [path.name for path in (beta / "outbox").iterdir()] == [legacy_id(old_repos["pending"])]
        main(["sync"])
        out = capsys.readouterr().out
        assert "sent result@v1" in out, out
        assert not list((beta / "outbox").iterdir())
        server = LocalRepo(server_root)
        sent = server.get_version("beta", "result", 1)
        assert sent["id"] == legacy_id(old_repos["pending"])
        assert [server.find("beta", id)[1]["version"] for id in sent["inputs"]] == [1]
        assert server.find("beta", sent["inputs"][0])[0] == "data"
        assert cairn.connect().version("result").number == 1

        # a 0.14 client and this server refuse each other, with a message saying so
        refused = run(
            old, "sync", cwd=old_repos["alpha"].parent, check=False, env={"CAIRN_TOKEN": served_token(server_root)}
        )
        assert "upgrade" in (refused.stdout + refused.stderr) and "0.15" in (refused.stdout + refused.stderr)
    finally:
        served.stop()


def served_token(root: Path) -> str:
    return (root / "token").read_text().strip()


def test_a_new_client_refuses_an_old_server(old, tmp_path, monkeypatch, capsys):
    root = tmp_path / "old-server"
    served = Served([old["cairn"]], root, free_port())
    try:
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.chdir(project)
        main(["init"])
        monkeypatch.setenv("CAIRN_TOKEN", served_token(root))
        with pytest.raises(SystemExit, match=r"runs Cairn 0\.14.*upgrade together"):
            main(["remote", "set", f"{served.url}/study"])
    finally:
        served.stop()


def test_the_upgrade_refuses_while_a_version_runs_or_a_draft_is_open(old, tmp_path):
    folder = tmp_path / "busy"
    folder.mkdir()
    run(old, "init", cwd=folder)
    script = (
        "import cairn, pathlib, time\n"
        "run = cairn.init(name='slow')\n"
        "with cairn.draft('out') as out:\n"
        "    pathlib.Path('started').write_text('1')\n"
        "    while not pathlib.Path('go').exists():\n"
        "        time.sleep(0.05)\n"
    )
    process = subprocess.Popen(
        [old["python"], "-c", script], cwd=folder, env={**os.environ, "PYTHONPATH": ""}, stdout=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 60
        while not (folder / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        with pytest.raises(StoreError, match="in use") as refused:
            upgrade(folder / ".cairn")
        assert "draft" in str(refused.value) and "heartbeat" in str(refused.value)
        assert layout(folder / ".cairn") == 1
    finally:
        (folder / "go").write_text("1")
        process.wait(60)
    result = upgrade(folder / ".cairn")
    assert result["counts"]["versions"] == 2 and result["report"].ok  # the run and its output


# ------------------------------------------------------------------ an upgrade that stopped half way runs again


UID_A = "01a144ef-bd78-72e6-a221-1439c21f9ea2"  # 0.14 uids (UUIDv7)
UID_B = "01a144ef-bd79-7000-8000-000000000001"


def test_a_read_only_folder_converted_before_a_stop_survives_the_next_attempt(tmp_path):
    """`_versions` writes a folder's converted record, then renames the folder: a stop between the two leaves a record by
    ids under the uid's name, and the next attempt converts it again (its inputs are ids now), never removes it."""
    from cairn.store import upgrade as module

    root = tmp_path / ".cairn"
    folder = root / "versions" / UID_B
    folder.mkdir(parents=True)
    manifest = {"uid": UID_B, "version": 2, "status": "completed", "files": [], "inputs": ["data@v1", f"data@{UID_A}"]}
    (folder / "manifest.json").write_text(json.dumps({"uid": UID_B, "name": "out", "manifest": manifest}))
    mapping = module._Mapping([("", "data", 1, UID_A)])
    counts = {"read-only folders": 0, "read-only folders removed": 0}
    real_rename = os.rename

    def stop(source, target):
        raise KeyboardInterrupt  # the upgrade stopped after writing the record, before the rename

    module.os.rename = stop
    try:
        with pytest.raises(KeyboardInterrupt):
            module._versions(root, mapping, counts)
    finally:
        module.os.rename = real_rename
    assert json.loads((folder / "manifest.json").read_text())["manifest"]["inputs"] == [legacy_id(UID_A)]
    module._versions(root, mapping, counts)  # the next attempt
    target = root / "versions" / legacy_id(UID_B)
    assert counts == {"read-only folders": 1, "read-only folders removed": 0}
    record = json.loads((target / "manifest.json").read_text())
    assert record["id"] == legacy_id(UID_B) and record["manifest"]["inputs"] == [legacy_id(UID_A)]


def test_a_draft_renamed_before_a_stop_is_converted_by_the_next_attempt(tmp_path):
    """`_drafts` renames a draft's folder and its record: a stop between the two leaves `<uid>.draft.json` beside
    `<id>/`; the next attempt finds it, converts it, and the draft is whole again (and `in_use` sees it)."""
    from cairn.store import upgrade as module

    root = tmp_path / ".cairn"
    base = root / "drafts" / "out"
    (base / UID_B).mkdir(parents=True)
    (base / UID_B / "a.txt").write_text("kept")
    record = {"uid": UID_B, "name": "out", "state": "open", "run": f"runs/x@{UID_A}", "inputs": [UID_A]}
    record["owner"] = {"host": socket.gethostname(), "pid": 2**22 + 12345}  # a process that is gone
    (base / f"{UID_B}.draft.json").write_text(json.dumps(record))
    real_rename = os.rename

    def stop(source, target):
        if str(source).endswith(".draft.json"):
            raise KeyboardInterrupt  # stopped after the folder was renamed, before the record
        return real_rename(source, target)

    module.os.rename = stop
    try:
        with pytest.raises(KeyboardInterrupt):
            module._drafts(root, {"drafts": 0})
    finally:
        module.os.rename = real_rename
    assert (base / legacy_id(UID_B)).is_dir() and (base / f"{UID_B}.draft.json").is_file()
    assert module._draft_records(root) == [base / f"{UID_B}.draft.json"]
    counts = {"drafts": 0}
    module._drafts(root, counts)
    assert counts == {"drafts": 1}
    assert sorted(path.name for path in base.iterdir()) == [legacy_id(UID_B), f"{legacy_id(UID_B)}.draft.json"]
    converted = json.loads((base / f"{legacy_id(UID_B)}.draft.json").read_text())
    assert converted["id"] == legacy_id(UID_B) and "uid" not in converted and "metadata" not in converted
    assert converted["run"] == legacy_id(UID_A) and converted["inputs"] == [legacy_id(UID_A)]
    assert (base / legacy_id(UID_B) / "a.txt").read_text() == "kept"
