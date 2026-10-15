import hashlib
import json

import pytest

from cairn.cli import main
from cairn import LocalStore
from cairn.server import make_server
from cairn.store.tokens import init_root

from test_server import call


def blob(root, data: bytes) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    path = root / "blobs" / sha[:2] / sha[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"sha256": sha, "size": len(data), "mtime": 1.0}


def lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.fixture
def old_hub(tmp_path):
    root = tmp_path / "old"
    (root).mkdir()
    (root / "token").write_text("x" * 40)

    def file(path, text):
        return {"path": path, **blob(root, text.encode())}

    old_version = {"version": 1, "time": "t1", "entries": [file("source/report.md", "# old\n")]}
    entries = [
        file("source/report.md", "---\ntitle: Overview\n---\nroot\n"),
        file("source/assets/root.txt", "r"),
        file("source/method/a/report.md", "no front matter\n"),
        file("source/method/a/assets/fig.txt", "fig"),
        file("source/method/report.md", "---\ntitle: Method\n---\n"),
        file("site/index.html", "<html>root</html>"),
        file("site/method/index.html", "<html>method</html>"),
        file("site/method/a/index.html", "<html>a</html>"),
        file("site/_static/app.js", "js"),
    ]
    lines(root / "projects" / "demo" / "report.jsonl", [old_version, {"version": 2, "time": "t2", "entries": entries}])
    run = lambda name, stamp, status: [file(f"{name}/{stamp}/run.json", json.dumps({"status": status})),  # noqa: E731
                                       file(f"{name}/{stamp}/metrics.jsonl", f"{stamp}\n")]
    rows = (run("descent", "2025-01-01_10-00-00", "completed") + run("descent", "2025-01-02_10-00-00", "completed")
            + run("descent", "2025-01-03_10-00-00", "failed") + run("Bad Name", "2025-01-01_10-00-00", "completed"))
    rows += [file("descent/2025-01-01_10-00-00/note.txt", "first"), file("descent/2025-01-01_10-00-00/note.txt", "second"),
             {"path": "descent/latest", "link": "2025-01-02_10-00-00"}]
    lines(root / "projects" / "demo" / "runs.jsonl", [{**row, "time": "t"} for row in rows])
    return root


def test_migrate(old_hub, tmp_path, capsys):
    new = tmp_path / "new"
    before = sorted(str(p) for p in old_hub.rglob("*"))
    main(["migrate", str(old_hub), str(new)])
    out = capsys.readouterr().out
    assert "2 runs migrated" not in out and "3 runs migrated" in out
    assert "1 older report versions" in out and "Bad Name" in out and "inputs:" in out
    assert sorted(str(p) for p in old_hub.rglob("*")) == before

    store = LocalStore(new)
    assert [p["name"] for p in store.projects()] == ["demo"]
    versions = store.versions("demo", "descent")
    assert [(v["label"], v["status"]) for v in versions] == [
        ("2025-01-01_10-00-00", "completed"), ("2025-01-02_10-00-00", "completed"), ("2025-01-03_10-00-00", "failed")]
    assert versions[0]["metadata"] == {"migrated_from": "demo/descent/2025-01-01_10-00-00"}
    assert store.aliases("demo", "descent") == {"latest": 2}  # the failed run never moved it, the link is ignored
    paths = {e["path"]: e for e in versions[0]["files"]}
    assert sorted(paths) == ["metrics.jsonl", "note.txt", "run.json"]
    assert store.blob_path(paths["note.txt"]["sha256"]).read_text() == "second"
    assert store.artifacts("demo") == ["descent", "reports/method", "reports/method-a", "reports/overview"]

    def files(name):
        return sorted(e["path"] for e in store.resolve(f"demo:{name}")[2]["files"])

    assert files("reports/overview") == ["site/_static/app.js", "site/overview/index.html", "source/assets/root.txt", "source/report.md"]
    assert files("reports/method") == ["site/_static/app.js", "site/method/index.html", "source/report.md"]
    assert files("reports/method-a") == ["site/_static/app.js", "site/method-a/index.html", "source/assets/fig.txt", "source/report.md"]
    method = store.resolve("demo:reports/method-a")[2]
    assert method["metadata"] == {"title": "a", "migrated_from": "demo@2 source/method/a/report.md"}
    assert store.resolve("demo:reports/overview")[2]["metadata"]["title"] == "Overview"
    assert store.resolve("demo:reports/overview")[2]["inputs"] == []

    main(["migrate", str(old_hub), str(new)])  # idempotent
    assert "0 runs migrated (3 already there)" in capsys.readouterr().out
    assert len(store.versions("demo", "descent")) == 3
    assert len(store.versions("demo", "reports/overview")) == 1


def test_migrated_store_is_served(old_hub, tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    new = tmp_path / "new"
    main(["migrate", str(old_hub), str(new)])
    token = init_root(new).read_text().strip()
    httpd = make_server(new, port=0)
    httpd.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    from cairn.ui.server import RemoteBackend, make_ui_server

    ui_httpd = make_ui_server(RemoteBackend(f"http://127.0.0.1:{httpd.server_address[1]}", False), port=0, secure_cookie=False)
    ui_httpd.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=ui_httpd.serve_forever, daemon=True).start()
    served = SimpleNamespace(port=ui_httpd.server_address[1], token="")
    try:
        _, headers, _ = call(served, "POST", "/ui-api/login", body=json.dumps({"token": token}), token=None,
                             headers={"Content-Type": "application/json", "X-Cairn": "1"})
        cookie = {"Cookie": headers["Set-Cookie"].split(";")[0]}
        assert call(served, "GET", "/p/demo/r/overview/", token=None, headers=cookie)[0] == 200
        status, _, page = call(served, "GET", "/ui-api/projects/demo/artifacts/descent", token=None, headers={**cookie, "X-Cairn": "1"})
        assert status == 200 and len(json.loads(page)["versions"]) == 3
        assert call(served, "GET", "/p/demo/", token=None, headers=cookie)[0] == 200
    finally:
        ui_httpd.shutdown()
        ui_httpd.server_close()
        httpd.shutdown()
        httpd.server_close()
