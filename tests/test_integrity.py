"""Integrity: the manifest digest, verified and copy-on-write pulls, and `cairn verify`."""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat

import pytest

import cairn as store
from cairn import LocalRepo, StoreError
from cairn.store import cache as cache_module
from cairn.store.cache import cache_root
from cairn.store.digest import canonical, manifest_digest
from cairn.store.errors import Conflict
from cairn.store.fs import clone

from test_offline import down, run  # noqa: F401
from test_server import api, server, use_remote  # noqa: F401
from test_store import env, folder  # noqa: F401


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def entries_of(files: dict) -> list[dict]:
    return [{"path": path, "sha256": sha(text), "size": len(text.encode()), "mtime": 123.0} for path, text in files.items()]


# --------------------------------------------------------------------- the digest


def test_digest_canonical_form_golden_value():
    entries = [{"path": "b/ü.txt", "sha256": sha("two"), "size": 3, "mtime": 5.0},
               {"path": "a.txt", "sha256": sha("hi"), "size": 2},
               {"path": "latest", "link": "a.txt"}]
    assert canonical(entries) == (
        '[{"path":"a.txt","sha256":"8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4","size":2},'
        f'{{"path":"b/ü.txt","sha256":"{sha("two")}","size":3}},'
        '{"link":"a.txt","path":"latest"}]').encode("utf-8")
    assert manifest_digest(entries) == "sha256:" + hashlib.sha256(canonical(entries)).hexdigest()
    assert manifest_digest(entries) == "sha256:8a186c75adc6d60b6de7608066077091572872de4e538e80fa784dbe09929af5"
    assert manifest_digest([]) == "sha256:" + hashlib.sha256(b"[]").hexdigest()
    assert manifest_digest(list(reversed(entries))) == manifest_digest(entries)  # the order of the list does not matter


def test_local_digest_depends_on_files_only(env):
    local = LocalRepo(env / ".store", "study")
    for text in ("1", "2"):
        local.put_blob(sha(text), io.BytesIO(text.encode()), len(text))
    local.create_project("study")
    a = local.publish("study", "a", entries_of({"x.txt": "1"}), label="first", metadata={"n": 1}, inputs=[])
    b = local.publish("study", "b", entries_of({"x.txt": "1"}), label="other", metadata={"n": 2})
    c = local.publish("study", "c", entries_of({"x.txt": "2"}))
    digest_a, digest_b, digest_c = (local.get_version("study", name, 1)["digest"] for name in "abc")
    assert (a, b, c) == (1, 1, 1)
    assert digest_a == digest_b == manifest_digest(entries_of({"x.txt": "1"}))  # same files: same digest, whatever the rest
    assert digest_c != digest_a
    # a publish of the same files with other metadata is a new version with the same digest
    assert local.publish("study", "a", entries_of({"x.txt": "1"}), metadata={"n": 99}) == 2
    assert local.get_version("study", "a", 2)["digest"] == digest_a
    assert local.publish("study", "a", entries_of({"x.txt": "1"}), metadata={"n": 99}) == 2  # unchanged: no third version
    # a running version gets its digest when it completes
    number = local.start_version("study", "r")
    assert "digest" not in local.get_version("study", "r", number)
    local.add_files("study", "r", number, entries_of({"x.txt": "1"}))
    local.complete_version("study", "r", number)
    assert local.get_version("study", "r", number)["digest"] == digest_a


def test_object_api_cli_and_pull_see_the_digest(env, capsys):
    data = folder(env, "d", {"a.txt": "hi", "sub/b.txt": "two"})
    (data / "latest").symlink_to("sub")
    version = store.connect().artifact("data").publish(data)
    expected = manifest_digest([{"path": "a.txt", "sha256": sha("hi"), "size": 2}, {"path": "sub/b.txt", "sha256": sha("two"), "size": 3},
                                {"path": "latest", "link": "sub"}])
    assert version.digest == expected
    out, _ = run(capsys, "show", "data@v1")
    assert f"digest       {expected}" in out


def test_server_rejects_a_wrong_client_digest(server, env):
    r = use_remote(env, server)
    r.create_project("study")
    local = LocalRepo(server.root)
    entries = entries_of({"a.txt": "hi"})
    r.put_blob(sha("hi"), io.BytesIO(b"hi"), 2)
    good = manifest_digest(entries)
    wrong = "sha256:" + "0" * 64
    route = "/api/v2/projects/study/artifacts/data"
    body = {"entries": entries, "digest": wrong}
    api(server, "POST", f"{route}/publish", body, expect=409)
    assert local.artifacts("study") == []
    api(server, "POST", f"{route}/publish", {"entries": entries, "digest": "nonsense"}, expect=400)
    published = api(server, "POST", f"{route}/publish", {"entries": entries, "digest": good}, expect=201)
    assert api(server, "GET", f"{route}/versions/{published['version']}")["digest"] == good
    # the step-wise path checks it on complete
    number = api(server, "POST", f"{route}/versions", {"label": "x"}, expect=201)["version"]
    api(server, "POST", f"{route}/versions/{number}/files", {"entries": entries})
    api(server, "POST", f"{route}/versions/{number}/complete", {"digest": wrong}, expect=409)
    assert api(server, "GET", f"{route}/versions/{number}")["status"] == "running"
    api(server, "POST", f"{route}/versions/{number}/complete", {"digest": good})
    assert api(server, "GET", f"{route}/versions/{number}")["digest"] == good
    with pytest.raises(Conflict):
        local.publish("study", "data", entries, digest=wrong, metadata={"other": 1})


def test_clients_send_the_digest_and_the_pending_version_knows_it(server, env):
    r = use_remote(env, server)
    version = store.connect().artifact("data").publish(folder(env, "d", {"a.txt": "hi"}))
    assert version.digest == manifest_digest(entries_of({"a.txt": "hi"}))
    sent = api(server, "GET", "/api/v2/projects/study/artifacts/data/versions/1")
    assert sent["digest"] == version.digest
    with store.connect().artifact("run").publishing(folder(env, "r", {"x.txt": "1"})) as pub:
        pub.sync()
    assert store.connect().artifact("run").version(1).digest == manifest_digest(entries_of({"x.txt": "1"}))
    assert r.get_version("study", "run", 1)["digest"] == manifest_digest(entries_of({"x.txt": "1"}))


def test_pending_version_has_its_digest_before_upload(down, env):
    version = store.connect().artifact("data").publish(folder(env, "d", {"a.txt": "hi"}))
    assert version.pending and version.digest == manifest_digest(entries_of({"a.txt": "hi"}))
    down.start()
    store.connect().sync()
    assert store.connect().artifact("data").version(1).digest == version.digest


def test_existing_versions_get_digests_when_the_store_opens(tmp_path):
    root = tmp_path / "old"
    local = LocalRepo(root, "study")
    local.create_project("study")
    local._blobs.put_blob(sha("hi"), io.BytesIO(b"hi"), 2)
    local.publish("study", "a", entries_of({"x.txt": "hi"}))
    local.publish("study", "a", entries_of({"y.txt": "hi"}))
    running = local.start_version("study", "a")
    # make the root look like one written before digests existed
    connection = sqlite3.connect(root / "metadata.sqlite3")
    for number, text in connection.execute("SELECT number, manifest FROM versions").fetchall():
        state = json.loads(text)
        state.pop("digest", None)
        connection.execute("UPDATE versions SET manifest = ? WHERE number = ?", (json.dumps(state), number))
    connection.execute("DELETE FROM meta WHERE key = 'digests'")
    connection.commit()
    connection.close()
    reopened = LocalRepo(root, "study")
    assert [version.get("digest") for version in reopened.versions("study", "a")] == [
        manifest_digest(entries_of({"x.txt": "hi"})), manifest_digest(entries_of({"y.txt": "hi"})), None]
    assert "digest" not in reopened.get_version("study", "a", running)
    connection = sqlite3.connect(root / "metadata.sqlite3")
    assert connection.execute("SELECT value FROM meta WHERE key = 'digests'").fetchone() == ("done",)


# ------------------------------------------------------------ verified and copy-on-write pulls


def no_clone_no_link(monkeypatch):
    """The cache module sees a filesystem that can neither clone nor hard link (the rest of cairn is unaffected)."""
    class NoLinks:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def link(*args, **kwargs):
            raise OSError("no hard links here")

    monkeypatch.setattr(cache_module, "clone", lambda source, target: False)
    monkeypatch.setattr(cache_module, "os", NoLinks())


def can_clone(tmp_path) -> bool:
    source = tmp_path / "probe-source"
    source.write_text("probe")
    target = tmp_path / "probe-target"
    try:
        return clone(source, target)
    finally:
        target.unlink(missing_ok=True)


def local_pull(env, text="hello"):
    backend = LocalRepo(env / ".store", "study")
    version = store.connect(env / ".store").artifact("data").publish(folder(env, "d", {"a.txt": text}))
    return backend, version


def test_pulled_files_are_clones_with_their_own_inode(env, tmp_path):
    if not can_clone(tmp_path):
        pytest.skip("this filesystem cannot clone files")
    backend, version = local_pull(env)
    pulled = version.pull() / "a.txt"
    blob = backend.blob_path(sha("hello"))
    assert pulled.read_text() == "hello"
    assert os.stat(pulled).st_ino != os.stat(blob).st_ino and os.stat(pulled).st_dev == os.stat(blob).st_dev
    assert not os.access(pulled, os.W_OK)
    os.chmod(pulled, 0o644)  # editing a pulled file (after a chmod) cannot reach the blob
    pulled.write_text("edited")
    assert blob.read_text() == "hello"


def test_hard_links_are_the_read_only_fallback(env, monkeypatch):
    monkeypatch.setattr(cache_module, "clone", lambda source, target: False)
    backend, version = local_pull(env)
    pulled = version.pull() / "a.txt"
    assert os.stat(pulled).st_ino == os.stat(backend.blob_path(sha("hello"))).st_ino  # shared: that is why it is read-only
    assert not stat.S_IMODE(os.stat(pulled).st_mode) & 0o222
    assert not os.access(pulled, os.W_OK)


def test_copy_when_neither_clone_nor_link_works(env, monkeypatch):
    no_clone_no_link(monkeypatch)
    backend, version = local_pull(env)
    pulled = version.pull() / "a.txt"
    assert pulled.read_text() == "hello" and os.stat(pulled).st_ino != os.stat(backend.blob_path(sha("hello"))).st_ino
    assert not os.access(pulled, os.W_OK)


def test_a_damaged_blob_fails_the_pull_and_leaves_no_partial_folder(env):
    backend, version = local_pull(env)
    blob = backend.blob_path(sha("hello"))
    os.chmod(blob, 0o644)
    blob.write_text("HELLO")
    with pytest.raises(StoreError, match=r"a\.txt does not match its manifest"):
        version.pull()
    assert not (cache_root() / "versions").exists() or not [p for p in (cache_root() / "versions").iterdir()]


def test_a_wrong_manifest_digest_fails_the_pull(env):
    backend, version = local_pull(env)
    state = backend.get_version("study", "data", 1)
    from cairn.store.cache import materialize_version

    with pytest.raises(StoreError, match="manifest digest"):
        materialize_version(backend, "study", "data", {**state, "digest": "sha256:" + "1" * 64, "uid": "0190" + state["uid"][4:]}, cache_root())
    assert not [p for p in (cache_root() / "versions").iterdir()]


# ----------------------------------------------------------------------- cairn verify


def test_tampered_cache_blob_fails_the_pull_and_verify_repairs_it(server, env, capsys):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"a.txt": "shared", "b.txt": "other"}), store=r)
    path = store.pull("data@v1", store=r)
    run(capsys, "verify", "--cache")  # clean (exit 0)
    uid = r.get_version("study", "data", 1)["uid"]
    blob = cache_module.blob_cache(cache_root(), sha("shared"))
    assert blob.is_file()
    os.chmod(blob, 0o644)
    blob.write_text("tampered")
    out, err = run(capsys, "verify", "--cache", code=1)
    assert str(blob) in out and "problems" in err
    # the pull of an unmaterialized version names the file
    cache_module.remove(cache_module.version_folder(cache_root(), uid))
    with pytest.raises(StoreError, match=r"a\.txt does not match its manifest"):
        store.pull("data@v1", store=r)
    assert not cache_module.version_folder(cache_root(), uid).exists()
    out, err = run(capsys, "verify", "--cache", "--repair")  # repaired problems exit 0
    assert str(blob) in out and "(removed)" in out and not blob.exists()
    assert (store.pull("data@v1", store=r) / "a.txt").read_text() == "shared"  # downloaded again
    run(capsys, "verify", "--cache")


def test_verify_finds_a_tampered_materialized_file_and_repair_drops_the_version(server, env, capsys, monkeypatch):
    no_clone_no_link(monkeypatch)
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"a.txt": "one"}), store=r)
    path = store.pull("data@v1", store=r)
    os.chmod(path / "a.txt", 0o644)
    (path / "a.txt").write_text("edited")
    (path / "extra.txt").write_text("not in the manifest")
    out, _ = run(capsys, "verify", "--cache", code=1)
    assert "a.txt" in out and "extra.txt" in out
    run(capsys, "verify", "--cache", "--repair")
    assert not path.exists()
    assert (store.pull("data@v1", store=r) / "a.txt").read_text() == "one"


def test_verify_a_version_by_reference(server, env, capsys):
    r = use_remote(env, server)
    store.publish("data", folder(env, "d", {"a.txt": "one"}), store=r)
    path = store.pull("data@v1", store=r)
    out, _ = run(capsys, "verify", "data@v1")
    assert "no problems" in out
    os.chmod(path / "a.txt", 0o644)
    (path / "a.txt").write_text("edited")
    out, _ = run(capsys, "verify", "data@v1", code=1)
    assert "a.txt" in out


def test_verify_pending_versions(down, env, capsys):
    version = store.connect().artifact("data").publish(folder(env, "d", {"a.txt": "one"}))
    path = store.pull(f"data@{version.uid}")
    assert (path / "a.txt").read_text() == "one"
    out, _ = run(capsys, "verify", "--cache")
    assert "no problems" in out
    os.chmod(path / "a.txt", 0o644)
    (path / "a.txt").write_text("edited")
    out, _ = run(capsys, "verify", "--cache", "--repair", code=1)  # pending versions are never repaired
    assert "a.txt" in out and path.exists()


def make_store(tmp_path):
    root = tmp_path / "root"
    local = LocalRepo(root, "study")
    local.create_project("study")
    for text in ("one", "two", "three"):
        local._blobs.put_blob(sha(text), io.BytesIO(text.encode()), len(text))
    local.publish("study", "a", entries_of({"1.txt": "one", "2.txt": "two"}))
    local.publish("study", "b", entries_of({"3.txt": "three"}))
    return root, local


def test_verify_a_store_finds_corrupt_and_missing_blobs(tmp_path, capsys):
    root, local = make_store(tmp_path)
    assert local.verify().ok
    out, _ = run(capsys, "verify", "--store", str(root))
    assert "no problems" in out
    corrupt, missing = local.blob_path(sha("one")), local.blob_path(sha("three"))
    os.chmod(corrupt, 0o644)
    corrupt.write_text("0ne")
    missing.unlink()
    report = local.verify()
    kinds = sorted((problem.kind, problem.path) for problem in report.problems)
    assert any(path == str(corrupt) for _, path in kinds)  # the blob does not match its name
    assert any("study:b@v1" in path and "missing" in problem.message for problem in report.problems for path in [problem.path])
    out, err = run(capsys, "verify", "--store", str(root), code=1)
    assert str(corrupt) in out and "study:b@v1" in out and "problems" in err


def test_verify_a_store_checks_manifest_digests(tmp_path):
    root, local = make_store(tmp_path)
    connection = sqlite3.connect(root / "metadata.sqlite3")
    text = connection.execute("SELECT manifest FROM versions WHERE name = 'a'").fetchone()[0]
    state = json.loads(text)
    state["digest"] = "sha256:" + "0" * 64
    connection.execute("UPDATE versions SET manifest = ? WHERE name = 'a'", (json.dumps(state),))
    connection.commit()
    connection.close()
    assert [problem.kind for problem in LocalRepo(root, "study").verify().problems] == ["digest"]


def test_object_api_verify(env):
    backend, version = local_pull(env)
    connected = store.connect(env / ".store")
    assert connected.verify(store=True).ok and connected.verify(all=True).ok and connected.verify().ok
    assert connected.verify("data@v1").ok
    blob = backend.blob_path(sha("hello"))
    os.chmod(blob, 0o644)
    blob.write_text("hellO")
    assert not connected.verify(store=True).ok
    assert not connected.verify("data@v1").ok
