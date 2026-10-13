import json
import re
import socket

import pytest

from cairn_report.build import build_all, image_size
from cairn_report.config import load_config


def built(project):
    return project / "generated" / "reports"


def test_tree_builds_with_links_and_navigation(project):
    builds = build_all(load_config(project))
    assert [build.key for build in builds] == ["alpha/beta", "alpha", ""]  # children before parents
    out = built(project)
    site = json.loads((out / "site.json").read_text())
    assert [(r["key"], r["depth"], r["href"]) for r in site["reports"]] == [("", 0, ""), ("alpha", 1, "alpha/"), ("alpha/beta", 2, "alpha/beta/")]
    beta = json.loads((out / "alpha" / "beta" / "report-data.json").read_text())
    assert beta["parent"] == {"key": "alpha", "title": "Alpha", "href": "alpha/#report-alpha-beta"}
    assert 'href="../../alpha/#result"' in (out / "alpha" / "beta" / "narrative.html").read_text()
    assert 'href="alpha/#finding-it-works-with-3-numbers-in-1-5-s"' in (out / "narrative.html").read_text()
    root_page = (out / "narrative.html").read_text()
    assert 'id="report-alpha"' in root_page and "The first child, error 0.5." in root_page  # child card with its summary
    assert 'data-root="../../" data-report="alpha/beta"' in (out / "alpha" / "beta" / "index.html").read_text()


def test_no_rules_are_enforced(project):
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text().replace("title: Alpha\nstatus: active\n", ""))  # no title, no status
    (project / "outputs" / "demo" / "2026-01-01_00-00-00" / "run.json").write_text('{"status": "failed"}')
    build_all(load_config(project))
    data = json.loads((built(project) / "alpha" / "report-data.json").read_text())
    assert data["title"] == "alpha"  # falls back to the folder name
    assert data["findings"][0]["state"] == "promising"  # any status word; numbers in titles are fine
    manifest = json.loads((built(project) / "alpha" / "manifest.json").read_text())
    assert manifest["source_runs"] == {"outputs/demo/2026-01-01_00-00-00": "failed"}  # recorded, not refused


def test_broken_report_link_fails(project):
    root = project / "reports" / "report.md"
    root.write_text(root.read_text() + "\n[gone](report:nowhere)\n")
    with pytest.raises(ValueError, match="no report 'nowhere'"):
        build_all(load_config(project))


def test_shared_static_files_and_versioned_urls(project):
    build_all(load_config(project))
    out = built(project)
    static = sorted(path.name for path in (out / "_static").iterdir())
    assert any(name.startswith("plotly.") and name.endswith(".min.js.gz") for name in static)
    page = (out / "alpha" / "index.html").read_text()
    assert 'src="../_static/report.' in page and 'data-narrative="narrative.html?v=' in page
    narrative = (out / "alpha" / "narrative.html").read_text()
    assert "data:image" not in narrative
    assert 'data-src="assets/' in narrative and "?v=" in narrative and 'width="400" height="100"' in narrative


def test_custom_css_and_template_override(project):
    (project / "style.css").write_text(":root { --accent: #224488; }\n")
    (project / "template").mkdir()
    (project / "template" / "report-extra.css").write_text("/* replaced */\n")
    (project / "cairn-report.toml").write_text('[style]\ncss = ["style.css"]\ntemplate_dir = "template"\n[page]\ncopy_preamble = "Handle these."\n')
    build_all(load_config(project))
    out = built(project)
    page = (out / "alpha" / "index.html").read_text()
    custom = re.search(r'href="\.\./_static/(custom-0\.[0-9a-f]+\.css)"', page).group(1)
    assert (out / "_static" / custom).read_text().startswith(":root")
    extra = re.search(r'href="\.\./_static/(report-extra\.[0-9a-f]+\.css)"', page).group(1)
    assert (out / "_static" / extra).read_text() == "/* replaced */\n"
    assert json.loads((out / "site.json").read_text())["copy_preamble"] == "Handle these."


def test_config_rejects_unknown_keys(tmp_path):
    (tmp_path / "cairn-report.toml").write_text('reprots_dir = "x"\n')
    with pytest.raises(ValueError, match="unknown keys"):
        load_config(tmp_path)


def test_port_fallback(project, monkeypatch):
    from cairn_report import serve as server

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    busy = blocker.getsockname()[1]
    chosen = {}
    monkeypatch.setattr(server.ThreadingHTTPServer, "serve_forever", lambda self: chosen.setdefault("port", self.server_address[1]))
    server.serve(load_config(project), port=busy, build=False)
    blocker.close()
    assert chosen["port"] != busy


def test_image_size_headers(tmp_path):
    import matplotlib.pyplot as plt

    figure = plt.figure(figsize=(4, 2), dpi=50)
    figure.savefig(tmp_path / "a.png")
    figure.savefig(tmp_path / "a.jpg")
    plt.close(figure)
    (tmp_path / "a.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 100"></svg>')
    assert image_size(tmp_path / "a.png") == (200, 100)
    assert image_size(tmp_path / "a.jpg") == (200, 100)
    assert image_size(tmp_path / "a.svg") == (300, 100)
