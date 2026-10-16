"""Order never comes from ids (docs/architecture.md, "Identity and references", and invariant 3). Every place that once
relied on UUIDv7 order (time-sortable uids) now goes by `created`, the version number, or the run->output edge; each
is checked here with ids constructed to sort the opposite way (the later version gets the smaller id)."""

import json
import os
import socket
import time

import pytest

import cairn
from cairn import LocalRepo
from cairn.client.sync import Sync
from cairn.store.ids import chosen
from cairn.store.outbox import Outbox

from conftest import write, write_config
from test_store import env, put  # noqa: F401

LATE, EARLY = "0" * 64, "f" * 64  # the later version gets the id that sorts first


@pytest.fixture
def down(env):
    """The project `env` with a remote that never answers (a closed port)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    write_config(env, f'[remote]\nurl = "http://127.0.0.1:{port}/study"\n')
    return env


def test_the_outbox_lists_its_entries_by_created_not_by_id(down):
    store = cairn.connect()
    with chosen(EARLY, LATE):
        first = store.artifact("data").publish(write(down / "a" / "x.txt", "1").parent)
        second = store.artifact("data").publish(write(down / "b" / "x.txt", "2").parent)
    assert (first.id, second.id) == (EARLY, LATE)
    records = Outbox(down / ".cairn").records()
    assert [record["id"] for record in records] == [EARLY, LATE]  # sorted by id it would be the other way round
    assert records[0]["created"] < records[1]["created"]


def test_the_send_order_is_when_each_became_final_and_inputs_first():
    """`Sync.ordered`: by completion (then start), every entry after the pending entries it names as inputs (a run's
    outputs after their run, though the outputs complete first); never by id."""

    def record(id, created, completed=None, inputs=(), kind="publish", status="completed"):
        return {
            "id": id,
            "name": id[:1],
            "created": created,
            "completed": completed or created,
            "inputs": list(inputs),
            "kind": kind,
            "status": status,
        }

    run = record("a" * 64, "2026-01-01T00:00:01", "2026-01-01T00:00:09", kind="running")
    output = record("0" * 64, "2026-01-01T00:00:02", "2026-01-01T00:00:03", inputs=[run["id"]])
    other = record("f" * 64, "2026-01-01T00:00:04")
    order = [item["id"][:1] for item in Sync.ordered([other, output, run])]
    assert order == ["a", "0", "f"]  # the run before its output; then the next to become final
    failed = {**run, "status": "failed"}
    assert [item["id"][:1] for item in Sync.ordered([failed, output, other])] == ["0", "f", "a"]  # a failed run last


def test_latest_among_pending_versions_is_the_one_sent_last(down):
    store = cairn.connect()
    with chosen(EARLY, LATE):
        store.artifact("data").publish(write(down / "a" / "x.txt", "1").parent)
        store.artifact("data").publish(write(down / "b" / "x.txt", "2").parent)
    assert store.version("data").id == LATE
    assert store.version("data", offline=True).id == LATE


def test_latest_is_the_highest_completed_number_not_the_last_completed(tmp_path):
    local = LocalRepo(tmp_path / "repo")
    local.create_project("p")
    with chosen(EARLY, LATE):
        one = local.start_version("p", "run")
        two = local.start_version("p", "run")
    local.complete_version("p", "run", two)
    local.complete_version("p", "run", one)
    assert local.resolve("p", "run")[1]["id"] == LATE  # v2 (the smaller id): the highest number
    assert local.summary("p", "run")["latest"] == 2


def test_adoption_and_its_listing_go_by_created(tmp_path):
    """A repo that numbered versions itself hands them to its new remote oldest first: by `created`."""
    local = LocalRepo(tmp_path / "repo")
    local.create_project("")
    with chosen(EARLY, LATE):
        local.publish("", "b", put(local, {"x": "1"}))
        local.publish("", "a", put(local, {"x": "2"}))
    assert [state["id"] for _, state in local.local_versions("")] == [EARLY, LATE]


def test_drafts_are_listed_oldest_first_by_created(env):
    from cairn.client.drafts import drafts

    cairn.init(name="order")  # the run (a draft of its own) first, so the two drafts get the chosen ids
    with chosen(EARLY, LATE):
        first = cairn.draft("one", watch=False)
        time.sleep(0.01)
        second = cairn.draft("two", watch=False)
    listed = [item.id for item in drafts() if item.name in ("one", "two")]
    assert listed == [first.id, second.id] == [EARLY, LATE]
    for item in (first, second):
        item.discard()


def test_prune_goes_by_last_access_then_created(tmp_path):
    from cairn.store.prune import prune

    root = tmp_path / "repo"
    local = LocalRepo(root)
    local.create_project("")
    with chosen(EARLY, LATE):
        local.publish("", "old", put(local, {"x": "1" * 1000}))
        local.publish("", "new", put(local, {"y": "2" * 1000}))
    from cairn.store.folders import materialize_version, version_folder
    from cairn.store.project import LocalProject

    project = LocalProject(local, "")
    for name, accessed in (("old", 100.0), ("new", 200.0)):
        _, manifest = project.resolve(name)
        materialize_version(project, name, manifest, root)
        record_file = version_folder(root, manifest["id"]) / "manifest.json"
        record = json.loads(record_file.read_text())
        record_file.write_text(json.dumps({**record, "accessed_at": accessed}))
    result = prune(root, older_than=None, max_size=0, dry_run=True)
    assert result["versions"][:2] == [EARLY, LATE]  # least recently used first (also the older one here)
    for name, accessed in (("old", 300.0), ("new", 200.0)):
        _, manifest = project.resolve(name)
        record_file = version_folder(root, manifest["id"]) / "manifest.json"
        record_file.write_text(json.dumps({**json.loads(record_file.read_text()), "accessed_at": accessed}))
    assert prune(root, older_than=None, max_size=0, dry_run=True)["versions"][:2] == [LATE, EARLY]


def test_versions_while_the_remote_is_down_go_by_number(down, tmp_path):
    """The repo's copies, listed offline, go by number; its pending versions follow, by `created`."""
    from cairn.client.repo import masked

    store = cairn.connect()
    local = LocalRepo(down / ".cairn")
    local.create_project("")
    with chosen(EARLY, LATE):
        for number, id in ((1, EARLY), (2, LATE)):
            entries = put(local, {"x": str(number)})
            manifest = {
                "version": number,
                "id": id,
                "label": None,
                "status": "completed",
                "created": f"2026-01-0{number}T00:00:00",
                "completed": f"2026-01-0{number}T00:00:00",
                "files": entries,
                "metadata": {},
                "inputs": [],
                "source": None,
            }
            local.put_copy("", "data", manifest, "remote/study")
    local.set_meta("remote", "remote/study")
    assert [masked(state)["version"] for state in local.versions("", "data")] == [1, 2]
    assert [version.number for version in store.artifact("data").versions()] == [1, 2]
