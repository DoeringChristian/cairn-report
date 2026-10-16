"""Hard-linked read-only folders (a filesystem without copy-on-write clones): a process running as root gets copies,
and a linked file written to anyway is found at its next use (by its stat, no hashing), refused, and repaired from
the remote; a file only touched is accepted."""

import os
from pathlib import Path

import pytest

import cairn
import cairn.store.folders as folders
from cairn.cli import main
from cairn.store import StoreError
from cairn.store.blobs import blob_path

from test_repo_sync import online, server  # noqa: F401
from test_store import env, folder  # noqa: F401


@pytest.fixture
def no_clones(monkeypatch):
    """The repo's filesystem cannot clone (ext4, tmpfs): read-only folders are hard links of the blobs."""
    monkeypatch.setattr(folders, "clone", lambda source, target: False)


def test_a_pull_hard_links_where_it_cannot_clone_and_records_the_blob(env, no_clones):
    cairn.connect().artifact("data").publish(folder(env, "d", {"a.txt": "one"}))
    path = cairn.pull("data@v1") / "a.txt"
    blob = blob_path(env / ".cairn", cairn.connect().version("data@v1").manifest["files"][0]["sha256"])
    assert os.stat(path).st_ino == os.stat(blob).st_ino  # a hard link
    assert (env / ".cairn" / folders.LINKS / f"{blob.name}.json").is_file()


def test_as_root_a_pull_copies_never_links(env, no_clones, monkeypatch):
    monkeypatch.setattr(folders.os, "geteuid", lambda: 0)  # root ignores the read-only mode of a hard link
    cairn.connect().artifact("data").publish(folder(env, "d", {"a.txt": "one"}))
    path = cairn.pull("data@v1") / "a.txt"
    blob = blob_path(env / ".cairn", cairn.connect().version("data@v1").manifest["files"][0]["sha256"])
    assert os.stat(path).st_ino != os.stat(blob).st_ino and os.stat(path).st_nlink == 1
    assert path.read_text() == "one" and not (env / ".cairn" / folders.LINKS).exists()


def test_a_linked_file_written_to_is_refused_at_its_next_use_and_repaired_from_the_remote(server, online, no_clones):
    server.start()
    version = cairn.connect().artifact("data").publish(folder(online, "d", {"a.txt": "one"}))
    assert version.number == 1  # on the remote: it has the contents too
    path = cairn.pull("data@v1") / "a.txt"
    os.chmod(path, 0o644)  # `chmod u+w`, then an edit: through the hard link, into the blob
    path.write_text("ONE")
    with pytest.raises(StoreError, match=r"written to through a hard link.*data@v1"):
        cairn.pull("data@v1")
    with pytest.raises(SystemExit):
        main(["verify"])  # lists it
    with pytest.raises(StoreError, match="hard link"):
        cairn.connect().version("data@v1").path
    main(["verify", "--repair"])  # drops the damaged contents and the folder, and downloads the contents again
    blob = blob_path(online / ".cairn", version.manifest["files"][0]["sha256"])
    assert blob.read_text() == "one"
    assert (cairn.pull("data@v1") / "a.txt").read_text() == "one"
    main(["verify"])


def test_a_linked_file_only_touched_is_accepted(env, no_clones):
    cairn.connect().artifact("data").publish(folder(env, "d", {"a.txt": "one"}))
    path = cairn.pull("data@v1") / "a.txt"
    index = env / ".cairn" / folders.LINKS / f"{os.path.basename(os.path.realpath(path))}.json"
    before = os.stat(path).st_mtime_ns
    os.utime(path, ns=(before + 10**9, before + 10**9))  # a touch: new mtime, same contents
    assert (cairn.pull("data@v1") / "a.txt").read_text() == "one"
    main(["verify"])
    recorded = [Path(item) for item in (env / ".cairn" / folders.LINKS).iterdir()]
    assert len(recorded) == 1 and str(before + 10**9) in recorded[0].read_text()  # the record follows the touch
