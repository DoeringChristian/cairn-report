import json
import shutil
import socket

import pytest

from cairn_report.build import build_all
from cairn_report.cli import main
from cairn_report.config import load_config

needs_graphviz = pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz `dot` not installed")


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    main(["--project", str(tmp_path), "init"])
    return tmp_path


def test_init_creates_content_only(project):
    reports = project / "reports"
    assert (reports / "report.md").is_file()
    assert (reports / "local-fitting" / "report.md").is_file()
    assert not list(reports.rglob("*.py")), "reports/ must hold content only"


@needs_graphviz
def test_build_samples(project):
    builds = build_all(load_config(project))
    assert {build.slug for build in builds} == {"local-fitting", "overview"}
    out = project / "generated" / "reports"
    data = json.loads((out / "local-fitting" / "report-data.json").read_text())
    assert data["summary"] and "{{" not in data["summary"]
    assert data["overview"]["href"] == "/#direction-local-fitting"
    manifest = json.loads((out / "overview" / "manifest.json").read_text())
    assert manifest["tooling"]["package"] == "cairn-report"
    assert "direction-local-fitting" in (out / "overview" / "narrative.html").read_text()


@needs_graphviz
def test_custom_css_and_template_override(project):
    (project / "reports" / "style.css").write_text(":root { --accent: #224488; }\n")
    (project / "reports" / "template").mkdir()
    (project / "reports" / "template" / "report-extra.css").write_text("/* replaced */\n")
    (project / "cairn-report.toml").write_text('[style]\ncss = ["reports/style.css"]\ntemplate_dir = "reports/template"\n')
    build_all(load_config(project))
    out = project / "generated" / "reports" / "local-fitting"
    assert '<link rel="stylesheet" href="custom-0.css">' in (out / "index.html").read_text()
    assert (out / "custom-0.css").read_text().startswith(":root")
    assert (out / "report-extra.css").read_text() == "/* replaced */\n"


def test_config_rejects_unknown_keys(tmp_path):
    (tmp_path / "cairn-report.toml").write_text('reprots_dir = "x"\n')
    with pytest.raises(ValueError, match="unknown keys"):
        load_config(tmp_path)


def test_new_direction_and_reserved_slug(project):
    main(["--project", str(project), "new", "second-idea"])
    assert (project / "reports" / "second-idea" / "report.md").is_file()
    with pytest.raises(SystemExit):
        main(["--project", str(project), "new", "media"])


def test_port_fallback(project, monkeypatch):
    from cairn_report import serve as server

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    busy = blocker.getsockname()[1]
    chosen = {}

    def fake_forever(self):
        chosen["port"] = self.server_address[1]

    monkeypatch.setattr(server.ThreadingHTTPServer, "serve_forever", fake_forever)
    server.serve(load_config(project), port=busy, build=False)
    blocker.close()
    assert chosen["port"] != busy
