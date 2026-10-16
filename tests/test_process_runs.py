"""Runs (`cairn.init`, cairn/client/runs.py): every process that reads or writes artifacts is a run, a version of
`runs/<name>`. Its inputs are what the process used, every draft it publishes has the run as its one input, and its
folder records the output, the machine, the code, and the packages. Against a local store and a store server; the
end-of-process behaviour in subprocesses (kept short)."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

import cairn
from cairn.store import OWN
from cairn.client import drafts, runs
from cairn.formats import read_jsonl

from test_drafts import published_dataset
from test_objects import configured  # noqa: F401  (local store or store server, with .cairn/config.toml)
from test_store import env, folder  # noqa: F401
from conftest import write_config, config_file


def finish_and_sync(run):
    version = run.finish()
    store = cairn.connect()
    if store.remote:
        store.sync()
    return version


# ------------------------------------------------------------------ in this process


def test_the_first_use_pull_or_draft_starts_the_run(configured, env):
    dataset = published_dataset(env)
    assert not cairn.run
    used = cairn.use("datasets/toy@latest")
    run = cairn.client.runs.current()
    assert isinstance(run, cairn.Run) and run.name == f"runs/{runs.default_name()}"
    assert isinstance(used, cairn.Version) and used == dataset and used.ref == "datasets/toy@v1"
    assert (used.path / "x.txt").read_text() == "1"  # materialized on first use
    store = cairn.connect()
    running = store.version(run.ref)
    assert running.status == "running" and running.metadata == {"kind": "run"} and running.uid == run.uid
    assert cairn.pull("datasets/toy@v1").is_dir() and cairn.run == run  # one run
    version = finish_and_sync(run)
    assert version.status == "completed" and version.inputs == [dataset.ref] and not cairn.run
    assert dataset.consumers == [version.ref]


def test_one_run_per_process(env):
    write_config(env, "")
    run = cairn.init(name="train", config={"lr": 0.1}, metadata={"note": "first"}, system=False, code=False)
    assert run.name == "runs/train" and cairn.init() is run and cairn.init(name="train") is run and cairn.run == run
    with pytest.raises(cairn.StoreError, match="one run per process"):
        cairn.init(name="other")
    with cairn.draft("model") as model:
        model.add_text("w", "w")
    assert model.run_ref == run.ref
    version = run.finish()
    assert version.metadata == {"kind": "run", "note": "first", "config": {"lr": 0.1}} and version.label == run.label
    assert not cairn.run
    implicit = cairn.draft("again")  # after finish(), a new run starts
    assert cairn.run != run
    cairn.init(config={"lr": 0.2}, metadata={"note": "late"})  # the run the draft started takes them now
    assert cairn.connect().version(cairn.run.ref).metadata == {"kind": "run", "config": {"lr": 0.2}, "note": "late"}
    assert cairn.run.config == {"lr": 0.2}
    implicit.discard()


def test_init_after_an_implicit_start_updates_the_running_run(configured, env):
    dataset = published_dataset(env)
    cairn.use("datasets/toy@latest")  # starts the run implicitly
    run = cairn.client.runs.current()
    assert cairn.connect().version(run.ref).metadata == {"kind": "run"}
    assert cairn.init(config={"lr": 0.1}, metadata={"note": "late"}) is run
    assert cairn.init(metadata={"extra": 1}) is run and run.config == {"lr": 0.1}
    with pytest.raises(cairn.StoreError, match="one run per process"):  # only another name is refused
        cairn.init(name="other", config={"lr": 0.2})
    running = {"kind": "run", "config": {"lr": 0.1}, "note": "late", "extra": 1}
    if cairn.connect().remote:  # the server hears of it with the next sync
        cairn.connect().sync()
    assert cairn.connect().version(run.ref).metadata == running
    version = finish_and_sync(run)
    assert version.status == "completed" and version.metadata == running and version.inputs == [dataset.ref]


def test_use_refuses_versions_that_are_not_completed(env):
    write_config(env, "")
    run = cairn.init(name="producer", system=False, code=False)
    with pytest.raises(cairn.StoreError, match="running"):
        cairn.use(run.ref)
    run.finish()


def test_running_inputs_by_uid_are_accepted_failed_and_unknown_ones_refused(configured, env):
    store = cairn.connect()
    backend = store.backend
    started = backend.start_version("runs/job", label="t", metadata={"kind": "run"})
    uid = started["uid"]
    data = folder(env, "out", {"a.txt": "a"})
    out = store.artifact("out").publish(data, inputs=[f"runs/job@{uid}"])
    assert out.inputs == ["runs/job@v1"]  # stored by number, like any exact input
    assert store.backend.lineage(f"runs/job@{uid}")["consumers"] == [out.ref]
    with pytest.raises(cairn.StoreError, match="running"):  # by number or alias, a running version is no input
        store.artifact("out2").publish(data, inputs=["runs/job@v1"])
    from cairn.store.uids import new_uid

    with pytest.raises(cairn.StoreError):
        store.artifact("out3").publish(data, inputs=[f"runs/job@{new_uid()}"])
    backend.fail_version("runs/job", uid)
    with pytest.raises(cairn.StoreError, match="failed"):
        store.artifact("out4").publish(data, inputs=[f"runs/job@{uid}"])
    marks = store.backend.lineage(out.ref, marks=True)
    assert marks["inputs"] == ["runs/job@v1"] and marks["nodes"]["runs/job@v1"] == {"status": "failed", "kind": "run",
                                                                                  "short_id": store.version("runs/job@v1").short_id}
    with pytest.raises(cairn.Protected):  # the failed run is still an input of a version: protected as before
        store.version("runs/job@v1").delete()


def test_lineage_looks_through_runs(env):
    write_config(env, "")
    data = published_dataset(env, "datasets/a")
    first = cairn.init(name="train", system=False, code=False)
    cairn.use("datasets/a@v1")
    with cairn.draft("models/m") as model:
        model.add_text("w", "w")
    first.finish()
    second = cairn.init(name="evaluate", system=False, code=False)
    cairn.use("models/m@v1")
    with cairn.draft("evals/m") as evaluation:
        evaluation.add_json("score", 1)
    second.finish()
    store = cairn.connect()
    evals = store.version("evals/m@v1")
    assert evals.inputs == ["runs/evaluate@v1"] and evals.run.ref == "runs/evaluate@v1"
    assert [v.ref for v in evals.upstream()] == ["models/m@v1"]
    assert [v.ref for v in data.downstream()] == ["models/m@v1"]
    assert cairn.lineage("models/m@v1") == {"inputs": [data.ref], "consumers": ["evals/m@v1"],
                                            "runs": ["runs/train@v1", "runs/evaluate@v1"]}
    assert cairn.lineage("models/m@v1", through_runs=False) == {"inputs": ["runs/train@v1"],
                                                                "consumers": ["runs/evaluate@v1"]}
    run = store.version("runs/train@v1")
    assert [v.ref for v in run.upstream()] == [data.ref] and [v.ref for v in run.downstream()] == ["models/m@v1"]


def test_open_drafts_are_published_with_the_run(env):
    write_config(env, "")
    run = cairn.init(name="loose", system=False, code=False)
    left_open = cairn.draft("forgotten")
    left_open.add({"x": 1})
    run.finish()
    assert left_open.version.number == 1 and left_open.version.inputs == ["runs/loose@v1"]
    assert drafts.drafts() == []


def test_a_subprocess_of_a_run_records_in_that_run(env):
    write_config(env, "")
    data = published_dataset(env)
    run = cairn.init(name="parent", system=False, code=False)
    child = "import cairn; cairn.use('datasets/toy@v1'); d = cairn.draft('child-out'); d.add({'x': 1}); d.publish(); assert not cairn.run"
    subprocess.run([sys.executable, "-c", child], check=True, cwd=env, env=dict(os.environ))
    version = run.finish()
    assert version.inputs == [data.ref] and version.consumers == ["child-out@v1"]
    assert [item.name for item in cairn.connect().artifacts(prefix="runs/")] == ["runs/parent"]


def test_watch_syncs_a_running_run_to_the_server(configured, env):
    if configured is None:
        pytest.skip("a store server only (a local store syncs the run when it ends, unless watch=True)")
    write_config(env, config_file(env).read_text().replace("[repo]\n", "[repo]\ninterval = 0.2\n"))
    run = cairn.init(name="live", system=False, code=False)
    assert run.watch  # the default with a store server
    run.add({"loss": 1.0, "step": 0})
    store = cairn.connect()
    deadline = time.time() + 20
    files = []
    while time.time() < deadline:
        try:
            files = [entry.path for entry in store.version(run.ref).refresh().files]
        except cairn.StoreError:
            files = []
        if "metrics.jsonl" in files:
            break
        time.sleep(0.1)
    assert "metrics.jsonl" in files and store.version(run.ref).status == "running"
    outbox = store.backend.outbox
    assert outbox.load(run.uid)["sync"] == "running" and (outbox.folder_of(run.uid) / "heartbeat").is_file()
    version = finish_and_sync(run)
    assert version.status == "completed" and "metrics.jsonl" in [f.path for f in version.files]


def test_a_run_and_its_output_published_while_the_server_is_down(configured, env):
    if configured is None:
        pytest.skip("a store server only")
    store = cairn.connect()
    configured.stop()
    run = cairn.init(name="offline", system=False, code=False)
    with cairn.draft("result") as result:
        result.add({"x": 1})
    assert result.version.pending and result.version.inputs == [run.ref]
    run.finish()
    configured.start()
    store.sync()
    out = store.artifact("result").latest
    assert out.inputs == ["runs/offline@v1"] and store.version("runs/offline@v1").status == "completed"


# ------------------------------------------------------------------ whole processes

SCRIPT = """\
import os, sys, time
import cairn

run = cairn.init(name="job", config={"a": 1}, system=0.1)
print("hello out", flush=True)
print("hello err", file=sys.stderr, flush=True)
os.system("echo from a subprocess")
run.add({"loss": 1.0, "step": 0})
cairn.use("datasets/toy@v1")
with cairn.draft("out") as out:
    out.add_text("note", "x")
mode = sys.argv[1]
if mode == "wait":
    open("started", "w").close()
    for _ in range(600):
        time.sleep(0.05)
time.sleep(0.3)
if mode == "fail":
    raise ValueError("boom")
"""


@pytest.fixture
def script_project(env):
    write_config(env, "")
    (env / "job.py").write_text(SCRIPT)
    (env / "secrets.env").write_text("TOKEN=x")
    (env / "data").mkdir()
    (env / "data" / "big.json").write_text("{}")
    (env / ".gitignore").write_text(".cairn/\n")
    for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *command], cwd=env, check=True)
    (env / "untracked.py").write_text("x = 1\n")
    published_dataset(env)
    return env


def job(project, mode, **kwargs):
    environ = {key: value for key, value in os.environ.items() if not key.startswith("SLURM_")}
    environ |= {"SLURM_JOB_ID": "123", "SLURM_ARRAY_TASK_ID": "4", "CUDA_VISIBLE_DEVICES": "0", "MY_SECRET": "hidden"}
    return subprocess.Popen([sys.executable, "job.py", mode], cwd=project, env=environ, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, **kwargs)


def test_a_script_is_a_run_that_completes_at_exit(script_project):
    process = job(script_project, "ok")
    out, err = process.communicate(timeout=120)
    assert process.returncode == 0, err
    assert "hello out" in out and "from a subprocess" in out and "hello err" in err  # still printed
    store = cairn.connect()
    run = store.version("runs/job@latest")
    assert run.status == "completed" and run.metadata == {"kind": "run", "config": {"a": 1}}
    assert run.inputs == ["datasets/toy@v1"] and run.consumers == ["out@v1"]
    assert store.version("out@v1").inputs == [run.ref]
    folder_ = run.pull()
    assert "hello out" in (folder_ / "logs" / "stdout.txt").read_text()
    assert "from a subprocess" in (folder_ / "logs" / "stdout.txt").read_text()  # the file descriptor is teed
    assert "hello err" in (folder_ / "logs" / "stderr.txt").read_text()
    samples = read_jsonl(folder_ / "system" / "metrics.jsonl")
    assert len(samples) >= 2 and all("time" in row and "disk_percent" in row for row in samples)
    assert any("cpu_percent" in row or "load_1m" in row for row in samples) and any("memory_percent" in row or "process_max_rss_gb" in row for row in samples)
    info = json.loads((folder_ / "system" / "info.json").read_text())
    assert info["host"] and info["argv"] == ["job.py", "ok"] and info["cwd"] == str(script_project) and info["pid"]
    assert info["slurm"] == {"job_id": "123", "array_task_id": "4"} and info["run"].startswith("runs/job@")
    assert info["environment"] == {"CUDA_VISIBLE_DEVICES": "0", "SLURM_ARRAY_TASK_ID": "4", "SLURM_JOB_ID": "123"}
    assert "packages" not in info and "cairn==" in (folder_ / "environment" / "pip-freeze.txt").read_text()
    manifest = json.loads((folder_ / "code" / "manifest.json").read_text())
    assert manifest["mode"] == "git" and len(manifest["commit"]) == 40
    assert (folder_ / "code" / "git-tree.tar.gz").is_file() and (folder_ / "code" / "untracked" / "untracked.py").is_file()
    assert "secrets.env" not in manifest["working_files"] and "data/big.json" not in manifest["working_files"]
    assert not (folder_ / "run.json").exists()  # the version's status is the record
    assert [row["loss"] for row in read_jsonl(folder_ / "metrics.jsonl")] == [1.0]
    assert drafts.drafts() == []


def test_an_unhandled_exception_fails_the_run(script_project):
    process = job(script_project, "fail")
    out, err = process.communicate(timeout=120)
    assert process.returncode == 1 and "ValueError: boom" in err
    store = cairn.connect()
    [run] = store.artifact("runs/job").versions()
    assert run.status == "failed" and "logs/error.txt" in [f.path for f in run.files]
    assert run.consumers == ["out@v1"] and store.version("out@v1").inputs == ["runs/job@v1"]  # the edge stays
    [kept] = drafts.drafts()
    assert kept.uid == run.uid and kept.state == "kept"
    assert "ValueError: boom" in (kept.folder / "logs" / "error.txt").read_text()
    assert "ValueError: boom" in (kept.folder / "logs" / "stderr.txt").read_text()


def test_sigterm_fails_the_run(script_project):
    process = job(script_project, "wait")
    deadline = time.time() + 60
    while not (script_project / "started").exists() and time.time() < deadline:
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=120)
    assert process.returncode == 143
    [run] = cairn.connect().artifact("runs/job").versions()
    assert run.status == "failed"
    [kept] = drafts.drafts()
    assert "terminated by signal SIGTERM" in (kept.folder / "logs" / "error.txt").read_text()


EXITS = [("sys.exit(3)", 3), ("raise SystemExit(4)", 4), ("sys.exit('boom')", 1), ("raise SystemExit('boom')", 1),
         ("def main():\n    sys.exit(5)\nmain()", 5), ("sys.exit(0)", 0), ("sys.exit()", 0),
         ("try:\n    sys.exit(2)\nexcept SystemExit:\n    pass", 0)]


@pytest.mark.parametrize("statement, code", EXITS, ids=[statement.split("\n")[0] for statement, _ in EXITS])
def test_a_non_zero_exit_fails_the_run(env, statement, code):
    """`sys.exit(n)` and `raise SystemExit(n)` that leave the script: n != 0 (or a message) fails the run."""
    write_config(env, "")
    (env / "exit.py").write_text("import sys, cairn\ncairn.init(name='exit', system=False, code=False)\n" + statement + "\n")
    result = subprocess.run([sys.executable, "exit.py"], cwd=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == code, result.stderr
    version = cairn.connect().version("runs/exit@latest" if code == 0 else "runs/exit@v1")
    if code == 0:
        assert version.status == "completed" and "exit_code" not in version.metadata
        return
    assert version.status == "failed" and version.metadata["exit_code"] == code
    [kept] = drafts.drafts()
    assert (kept.folder / "logs" / "error.txt").read_text() == f"the process exited with code {code}\n"


def test_an_unhandled_exception_keeps_its_traceback_and_records_no_exit_code(env):
    write_config(env, "")
    (env / "boom.py").write_text("import cairn\ncairn.init(name='boom', system=False, code=False)\nraise ValueError('x')\n")
    assert subprocess.run([sys.executable, "boom.py"], cwd=env, capture_output=True).returncode == 1
    [kept] = drafts.drafts()
    assert "ValueError: x" in (kept.folder / "logs" / "error.txt").read_text()
    assert "exit_code" not in cairn.connect().version("runs/boom@v1").metadata


def test_a_sigkilled_run_does_not_stay_running(script_project, capsys):
    from cairn.cli import main

    process = job(script_project, "wait")
    deadline = time.time() + 60
    while not (script_project / "started").exists() and time.time() < deadline:
        time.sleep(0.05)
    store = cairn.LocalRepo(script_project / ".cairn")
    [run] = store.versions(OWN, "runs/job")
    assert run["status"] == "running" and (store.root / "heartbeats" / run["uid"]).is_file()
    process.kill()  # SIGKILL: no handler, no atexit
    process.communicate(timeout=60)
    assert process.returncode == -signal.SIGKILL
    main(["status"])  # `cairn status` opens the store, which fails the version whose process is gone
    assert "failed runs/job@v1: the process died (no heartbeat since " in capsys.readouterr().out
    after = cairn.LocalRepo(store.root).get_version(OWN, "runs/job", 1)
    assert after["status"] == "failed" and after["error"].startswith("the process died (no heartbeat since ")
    assert not (store.root / "heartbeats" / run["uid"]).exists()
    assert cairn.connect().version("runs/job@v1").status == "failed"  # also through the object API
    assert cairn.LocalRepo(store.root).aliases(OWN, "runs/job") == {}  # `latest` never moved to it


def test_a_local_version_is_failed_by_age_or_dead_owner_and_never_while_alive(tmp_path):
    from cairn.store.uids import new_uid

    store = cairn.LocalRepo(tmp_path / "store")
    store.create_project("p")  # a server's repo: a named project
    mine, stale, foreign, bare = new_uid(), new_uid(), new_uid(), new_uid()
    for uid in (mine, stale, foreign, bare):
        store.start_version("p", "run", label=uid[-8:], uid=uid)
    store.beat(mine)  # this process: alive
    beats = tmp_path / "store" / "heartbeats"
    now = time.time()
    (beats / stale).write_text(json.dumps({"time": now - 10_000, "host": "other-host", "pid": 1}))  # too old
    (beats / foreign).write_text(json.dumps({"time": now - 5, "host": "other-host", "pid": 2 ** 22}))  # recent, elsewhere
    reaped = dict(cairn.LocalRepo(tmp_path / "store", heartbeat_timeout=600).reaped)
    assert list(reaped) == ["run@v2 (project p)"] and "no heartbeat since" in reaped["run@v2 (project p)"]
    states = {version["uid"]: version["status"] for version in store.versions("p", "run")}
    assert states == {mine: "running", stale: "failed", foreign: "running", bare: "running"}  # `bare` never had a heartbeat
    time.sleep(0.05)
    assert {ref for ref, _ in store.reap(0.01)} == {"run@v1 (project p)", "run@v3 (project p)"}  # a tiny timeout: whoever has not beaten lately
    assert {v["status"] for v in store.versions("p", "run")} == {"failed", "running"}
    store.complete_version("p", "run", 4)
    assert not (beats / bare).exists()


def test_capture_and_system_can_be_turned_off(env):
    write_config(env, "")
    code = "import cairn; run = cairn.init(name='quiet', capture=False, system=False, code=False); print('hi')"
    result = subprocess.run([sys.executable, "-c", code], cwd=env, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == "hi\n", result.stderr
    version = cairn.connect().version("runs/quiet@latest")
    assert version.files == []


def test_the_default_name_follows_the_script(env):
    write_config(env, "")
    (env / "pkg").mkdir()
    (env / "pkg" / "__init__.py").write_text("")
    (env / "pkg" / "Train_Model.py").write_text("import cairn\nprint(cairn.init(system=False, code=False).name)\n")
    by_module = subprocess.run([sys.executable, "-m", "pkg.Train_Model"], cwd=env, capture_output=True, text=True)
    by_path = subprocess.run([sys.executable, "pkg/Train_Model.py"], cwd=env, capture_output=True, text=True)
    by_code = subprocess.run([sys.executable, "-c", "import cairn; print(cairn.init(system=False, code=False).name)"], cwd=env,
                             capture_output=True, text=True)
    assert [by_module.stdout.strip(), by_path.stdout.strip(), by_code.stdout.strip()] == \
        ["runs/pkg.train_model", "runs/train_model", "runs/interactive"], by_module.stderr + by_path.stderr


# ------------------------------------------------------------------ Hydra: ${cairn.run:}

CONFIG = """\
defaults:
  - _self_
run:
  name: demo
experiment:
  _target_: experiments.toy.Toy
  pull: null
  fail: false
hydra:
  run:
    dir: ${cairn.run:}
  sweep:
    dir: ${cairn.run:}
    subdir: ${hydra.job.num}
  callbacks:
    source_snapshot:
      _target_: cairn.runs.snapshot.SourceSnapshotCallback
    study_completion:
      _target_: cairn.runs.hydra.StudyCompletion
"""

TOY = """\
import cairn


class Toy:
    def __init__(self, pull=None, fail=False):
        self.pull, self.fail = pull, fail

    def run(self, output_dir):
        if self.pull:
            cairn.use(self.pull)
        run = cairn.client.runs.current()
        assert run is not None and run.path == output_dir
        print("training", flush=True)
        run.add({"score": 1.0, "step": 0})
        (output_dir / "result.txt").write_text("ok")
        with cairn.draft("models/toy") as model:
            model.add_text("weights", "w")
        if self.fail:
            raise RuntimeError("asked to fail")
"""


@pytest.fixture
def run_project(configured, env):
    pytest.importorskip("hydra")
    from test_drafts import RUN

    (env / "configs").mkdir()
    (env / "configs" / "config.yaml").write_text(CONFIG)
    (env / "experiments").mkdir()
    (env / "experiments" / "__init__.py").write_text("")
    (env / "experiments" / "run.py").write_text(RUN)
    (env / "experiments" / "toy.py").write_text(TOY)
    (env / ".gitignore").write_text(".cairn/\n")
    for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *command], cwd=env, check=True)
    return env


def hydra(project, *overrides):
    return subprocess.run([sys.executable, "experiments/run.py", *overrides], cwd=project, capture_output=True, text=True)


def synced():
    store = cairn.connect()
    if store.remote:
        store.sync()
    return store


def test_a_hydra_job_is_a_run_in_its_job_folder(run_project):
    dataset = published_dataset(run_project)
    result = hydra(run_project, "experiment.pull=datasets/toy@latest")
    assert result.returncode == 0, result.stderr
    store = synced()
    run = store.version("runs/demo@latest")
    paths = {f.path for f in run.files}
    assert {"result.txt", "metrics.jsonl", ".hydra/config.yaml", "provenance/manifest.json", "logs/stdout.txt",
            "system/info.json", "system/metrics.jsonl"} <= paths and "run.json" not in paths
    assert run.status == "completed" and run.inputs == [dataset.ref] and run.consumers == ["models/toy@v1"]
    assert run.metadata["kind"] == "run" and run.metadata["config"]["experiment"]["pull"] == "datasets/toy@latest"
    assert run.metadata["overrides"] == ["experiment.pull=datasets/toy@latest"]
    with run.open("logs/stdout.txt") as handle:
        assert "training" in handle.read().decode()
    assert store.version("models/toy@v1").inputs == [run.ref]
    assert drafts.drafts() == []


def test_a_failed_hydra_job_fails_its_run(run_project):
    result = hydra(run_project, "experiment.fail=true")
    assert result.returncode != 0
    store = synced()
    [run] = store.artifact("runs/demo").versions()
    assert run.status == "failed" and store.version("models/toy@v1").inputs == ["runs/demo@v1"]
    [kept] = drafts.drafts()
    assert "asked to fail" in (kept.folder / "logs" / "error.txt").read_text()


def test_a_hydra_sweep_is_a_run_per_trial_and_a_summary_run(run_project):
    first = published_dataset(run_project, "datasets/a")
    second = published_dataset(run_project, "datasets/b")
    result = hydra(run_project, "-m", "experiment.pull=datasets/a@v1,datasets/b@v1")
    assert result.returncode == 0, result.stderr
    store = synced()
    trial0, trial1, summary = store.artifact("runs/demo").versions()
    assert [trial0.metadata["sweep"]["num"], trial1.metadata["sweep"]["num"]] == [0, 1]
    assert trial0.label == f"{summary.label}/0" and trial1.label == f"{summary.label}/1"
    assert trial0.metadata["sweep"]["uid"] == summary.uid and trial0.metadata["config"]["experiment"]["pull"] == "datasets/a@v1"
    assert trial0.inputs == [first.ref] and trial1.inputs == [second.ref]
    assert {".hydra/config.yaml", "result.txt", "logs/stdout.txt"} <= {f.path for f in trial0.files}
    assert summary.metadata["kind"] == "run" and summary.inputs == [trial0.ref, trial1.ref]
    assert [row["status"] for row in summary.metadata["sweep"]["trials"]] == ["completed", "completed"]
    assert summary.metadata["overrides"] == ["experiment.pull=datasets/a@v1,datasets/b@v1"]
    assert {"multirun.yaml", "trials.jsonl"} <= {f.path for f in summary.files}
    assert not any(f.path.startswith("0/") for f in summary.files)  # each trial is its own version
    assert [v.ref for v in summary.upstream()] == [first.ref, second.ref]
    assert [v.inputs for v in store.artifact("models/toy").versions()] == [[trial0.ref], [trial1.ref]]
    assert drafts.drafts() == []


def test_cairn_run_is_the_current_run_from_anywhere(env):
    from cairn import run as imported_early

    assert not cairn.run and repr(cairn.run).startswith("<cairn.run: no run")
    with pytest.raises(RuntimeError, match=r"call cairn.init\(\) first"):
        cairn.run.add({"loss": 1.0})

    def deep_inside_the_training_code(step):
        cairn.run.add({"loss": 1.0 / (step + 1), "step": step})  # no run passed in

    run = cairn.init(name="anywhere")
    assert cairn.run == run and imported_early == run and cairn.run.name == "runs/anywhere"
    for step in range(3):
        deep_inside_the_training_code(step)
    imported_early.add({"loss": 0.1, "step": 3})
    cairn.run.finish()
    assert not cairn.run
    version = cairn.connect().version(run.ref)
    assert version.status == "completed" and "metrics.jsonl" in {f.path for f in version.files}
