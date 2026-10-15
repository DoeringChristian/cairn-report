import html
import json
import re
import shutil
import socket
import threading
import urllib.request

import pytest

import cairn as store
from cairn.build import BuildError, build_all, image_size
from cairn.cli import main
from cairn.config import load_config

from conftest import make_data, write


def built(project):
    return project / "generated" / "reports"


def test_flat_reports_build_with_links_navigation_and_index(project):
    builds = build_all(load_config(project))
    assert [build.key for build in builds] == ["alpha", "beta"]
    out = built(project)
    site = json.loads((out / "site.json").read_text())
    assert [(r["key"], r["title"], r["href"]) for r in site["reports"]] == [("alpha", "Alpha", "alpha/"), ("beta", "Beta", "beta/")]
    assert site["reports"][0]["status"] == "active" and site["reports"][0]["summary"] == "The first report, error 0.5."
    assert list(site["reports"][0]["inputs"]) == ["descent"] and site["reports"][1]["inputs"] == {}
    assert "parent" not in site["reports"][0] and not (out / "alpha" / "beta").exists()
    assert 'href="../alpha/#result"' in (out / "beta" / "narrative.html").read_text()
    assert 'href="../beta/#detail"' in (out / "alpha" / "narrative.html").read_text()
    page = (out / "beta" / "index.html").read_text()
    assert 'data-root="../" data-report="beta"' in page and 'id="data-panel"' in page and "overview-link" not in page
    assert "parent" not in json.loads((out / "beta" / "report-data.json").read_text())


def test_project_index_lists_every_report(project):
    build_all(load_config(project))
    index = (built(project) / "index.html").read_text()
    assert index.startswith("<!doctype html>") and "<script" not in index
    assert re.search(r'href="\./_static/report\.[0-9a-f]+\.css"|href="_static/report\.[0-9a-f]+\.css"', index)
    assert 'href="alpha/">Alpha</a>' in index and 'href="beta/">Beta</a>' in index
    assert "The first report, error 0.5." in index and "active" in index
    assert "1 input" in index and "0 inputs" in index and "updated 20" in index
    build_all(load_config(project), report="beta")  # a single-report build keeps every report listed
    assert "Alpha" in (built(project) / "index.html").read_text()


def test_serve_answers_the_index_at_root(project):
    from cairn import serve as server

    build_all(load_config(project))
    server.OUTPUT_ROOT = built(project)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/").read().decode()
    finally:
        httpd.shutdown()
    assert 'href="alpha/">Alpha</a>' in body


def test_reports_are_single_pages(project):
    nested = project / "reports" / "alpha" / "sub"
    write(nested / "report.md", "Nested.\n")
    with pytest.raises(BuildError, match="reports are single pages: move it to reports/<slug>/report.md") as error:
        build_all(load_config(project))
    assert "reports/alpha/sub/report.md" in str(error.value)
    shutil.rmtree(nested)
    write(project / "reports" / "report.md", "A root report.\n")
    with pytest.raises(BuildError, match="reports/report.md: reports are single pages"):
        build_all(load_config(project))


def test_the_reports_fence_and_root_links_are_refused(project):
    beta = project / "reports" / "beta" / "report.md"
    beta.write_text("```reports\n```\n")
    with pytest.raises(BuildError, match="reports fence was removed"):
        build_all(load_config(project))
    beta.write_text("[root](report:/)\n")
    with pytest.raises(ValueError, match="malformed report link"):
        build_all(load_config(project))


def test_folders_without_a_report_are_ignored_and_bad_names_warn(project, capsys):
    (project / "reports" / "scratch").mkdir()
    write(project / "reports" / "Bad Name" / "report.md", "Skipped.\n")
    build_all(load_config(project))
    assert [r["key"] for r in json.loads((built(project) / "site.json").read_text())["reports"]] == ["alpha", "beta"]
    assert "Bad Name" in capsys.readouterr().err  # skipped loudly, not silently


def test_no_rules_are_enforced(project):
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text().replace("title: Alpha\nstatus: active\n", ""))  # no title, no status
    build_all(load_config(project))
    data = json.loads((built(project) / "alpha" / "report-data.json").read_text())
    assert data["title"] == "alpha"  # falls back to the folder name
    assert data["findings"][0]["state"] == "promising"  # any status word; numbers in titles are fine


def test_broken_report_link_fails(project):
    beta = project / "reports" / "beta" / "report.md"
    beta.write_text(beta.read_text() + "\n[gone](report:nowhere)\n")
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
    (project / "cairn.toml").write_text('[style]\ncss = ["style.css"]\ntemplate_dir = "template"\n[page]\ncopy_preamble = "Handle these."\n')
    build_all(load_config(project))
    out = built(project)
    page = (out / "alpha" / "index.html").read_text()
    custom = re.search(r'href="\.\./_static/(custom-0\.[0-9a-f]+\.css)"', page).group(1)
    assert (out / "_static" / custom).read_text().startswith(":root")
    extra = re.search(r'href="\.\./_static/(report-extra\.[0-9a-f]+\.css)"', page).group(1)
    assert (out / "_static" / extra).read_text() == "/* replaced */\n"
    assert json.loads((out / "site.json").read_text())["copy_preamble"] == "Handle these."


def test_config_rejects_unknown_keys(tmp_path):
    (tmp_path / "cairn.toml").write_text('reprots_dir = "x"\n')
    with pytest.raises(ValueError, match="unknown keys"):
        load_config(tmp_path)


def test_port_fallback(project, monkeypatch):
    from cairn import serve as server

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


def bundle_of(page: str) -> dict:
    return json.loads(re.search(r'<script type="application/json" id="cairn-bundle">(.*?)</script>', page, re.S).group(1))


def test_share_writes_one_self_contained_page(project):
    from cairn.share import share

    target = share(load_config(project))
    page = target.read_text()
    assert target == project / "generated" / "share" / "index.html"
    assert page.startswith("<!-- Generated by cairn share") and "<!doctype" not in page.lower()
    assert "<title>Reports</title>" in page and "<link" not in page and "�" not in page
    assert not re.search(r'<script src=', page)  # report.js and the styles are inline; Plotly comes from the CDN on demand
    assert 'data-plotly="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@' in page
    bundle = bundle_of(page)
    assert bundle["start"] == "alpha" and list(bundle["reports"]) == ["alpha", "beta"]
    alpha = bundle["reports"]["alpha"]["narrative"]
    assert 'data-src="data:image/svg+xml;base64,' in alpha and 'data-src="data:image/png;base64,' in alpha
    assert 'href="../beta/#detail"' in alpha  # report links stay relative; the page resolves them inside the bundle
    assert bundle["reports"]["alpha"]["data"]["inputs"]["descent"]["resolved"] == "study:descent@v1"


def test_share_one_report_and_size_limit(project, monkeypatch):
    from cairn import share as sharing

    target = sharing.share(load_config(project), report="alpha", out=project / "alpha.html")
    bundle = bundle_of(target.read_text())
    assert bundle["start"] == "alpha" and list(bundle["reports"]) == ["alpha"]
    assert [r["key"] for r in bundle["site"]["reports"]] == ["alpha"] and bundle["site"]["title"] == "Alpha"
    monkeypatch.setattr(sharing, "MAX_BYTES", 1000)
    with pytest.raises(SystemExit, match="above the 0 MB limit"):
        sharing.share(load_config(project), build=False)



def write_beta(project, body):
    (project / "reports" / "beta" / "report.md").write_text(
        "---\ntitle: Beta\n---\n\n## Detail\n\nBack to [alpha](report:alpha#result).\n\n" + body)


@pytest.mark.skipif(shutil.which("dot") is None, reason="needs the Graphviz binary")
def test_graph_helper_makes_a_figure_card(project):
    write_beta(project, "```python exec hide\nsource = 'digraph { x -> h -> y }'\n"
                        "graph(source, id='fig-net', title='The network', caption='x to y through h.')\n```\n")
    build_all(load_config(project))
    narrative = (built(project) / "beta" / "narrative.html").read_text()
    assert 'id="fig-net"' in narrative and "The network" in narrative and 'data-figure-type="image"' in narrative


def test_graph_helper_rejects_unknown_engines(project):
    write_beta(project, "```python exec hide\ngraph('digraph {}', engine='rm')\n```\n")
    with pytest.raises(ValueError, match="unknown Graphviz engine"):
        build_all(load_config(project))


def test_caption_helper_sets_a_matplotlib_figure_caption(project):
    write_beta(project, "```python exec hide\nimport matplotlib.pyplot as plt\nplt.figure(num='curve'); plt.plot([0, 1])\n"
                        "caption('What the curve shows.')\n```\n")
    build_all(load_config(project))
    narrative = (built(project) / "beta" / "narrative.html").read_text()
    assert "<p>What the curve shows.</p>" in narrative and "Generated by report.md" not in narrative


def test_single_report_build_lists_every_report(project):
    build_all(load_config(project))
    write(project / "reports" / "gamma" / "report.md", "---\ntitle: Gamma\nstatus: new\ninputs:\n  d: descent@v1\n---\n\nText.\n")
    build_all(load_config(project), report="beta")  # gamma is added after the last full build
    site = json.loads((built(project) / "site.json").read_text())
    assert [(r["key"], r["title"]) for r in site["reports"]] == [("alpha", "Alpha"), ("beta", "Beta"), ("gamma", "Gamma")]
    assert site["reports"][2]["inputs"] == {"d": {"requested": "descent@v1"}}
    assert "not built yet" in (built(project) / "index.html").read_text()


# ------------------------------------------------------------------ inputs


def manifest_of(project, key="alpha"):
    return json.loads((built(project) / key / "manifest.json").read_text())


def test_inputs_are_pinned_and_recorded(project):
    build_all(load_config(project))
    expected = {"descent": {"requested": "descent@latest", "resolved": "study:descent@v1", "files": 4}}
    manifest = manifest_of(project)
    assert manifest["inputs"] == expected and "source_runs" not in manifest and "selection" not in manifest
    assert json.loads((built(project) / "alpha" / "report-data.json").read_text())["inputs"] == expected
    assert "input:descent/metrics.jsonl" in manifest["sources"] and "reports/alpha/assets/strip.svg" in manifest["sources"]
    assert manifest_of(project, "beta")["inputs"] == {}
    # the alias moves on; the next build pins the new version, the earlier build keeps what it recorded
    data = make_data(project.parent / "second")
    (data / "data" / "summary.json").write_text('{"steps": 4}')
    store.publish("descent", data)
    assert store.versions("descent")[-1]["version"] == 2
    build_all(load_config(project))
    assert manifest_of(project)["inputs"]["descent"] | {"files": 0} == expected["descent"] | {"resolved": "study:descent@v2", "files": 0}


def test_inputs_exposes_paths_by_attribute_and_item(project):
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text() + (
        "\n```python exec hide\nsteps = read_json(inputs.descent / 'data' / 'summary.json')['steps']\n"
        "assert inputs['descent'] == inputs.descent and 'descent' in inputs\n"
        "metric('Steps', steps)\nmetric('Folder', inputs.descent.name)\n```\n"))
    build_all(load_config(project))
    narrative = (built(project) / "alpha" / "narrative.html").read_text()
    assert "<strong>3</strong>" in narrative and "<strong>v1</strong>" in narrative


def test_input_images_are_copied_with_their_trace(project):
    build_all(load_config(project))
    narrative = (built(project) / "alpha" / "narrative.html").read_text()
    assert (built(project) / "alpha" / "assets" / "inputs" / "descent" / "figures" / "hist.png").is_file()
    assert re.search(r'data-src="assets/inputs/descent/figures/hist\.png\?v=[0-9a-f]{12}"', narrative)
    trace = json.loads(html.unescape(re.search(r'data-trace="([^"]+)"', narrative).group(1)))
    assert trace["input"] == "descent@v1//figures/hist.png" and trace["artifact"] == "study:descent@v1"
    assert "run" not in trace and "revision" not in trace
    assert "assets/reports/alpha/assets/strip.svg" in narrative  # report-local figures keep their path


def test_input_errors_are_clear(project):
    alpha = project / "reports" / "alpha" / "report.md"
    text = alpha.read_text()
    alpha.write_text(text.replace("descent@latest", "nothing@latest"))
    with pytest.raises(BuildError, match=r"inputs\.descent: no artifact study:nothing"):
        build_all(load_config(project))
    alpha.write_text(text.replace("descent@latest", "descent@v9"))
    with pytest.raises(BuildError, match=r"inputs\.descent: study:descent has no version v9"):
        build_all(load_config(project))
    alpha.write_text(text.replace("  descent:", "  Descent:"))
    with pytest.raises(BuildError, match="input names are lower-case identifiers"):
        build_all(load_config(project))
    alpha.write_text(text.replace("descent@latest", "descent@latest//metrics.jsonl"))
    with pytest.raises(BuildError, match="names a file"):
        build_all(load_config(project))
    alpha.write_text(text.replace("![Histogram](input:descent/", "![Histogram](input:other/"))
    with pytest.raises(BuildError, match="input 'other', which is not declared"):
        build_all(load_config(project))
    alpha.write_text(text + "\n```python exec hide\ninputs.other\n```\n")
    with pytest.raises(AttributeError, match="declare it under inputs"):
        build_all(load_config(project))


def test_the_input_summary_and_values_use_inputs(project):
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text().replace("summary: The first report, error {{ scores['rmse'] }}.",
                                               "summary: Steps {{ read_json(inputs.descent / 'data' / 'summary.json')['steps'] }}."))
    build_all(load_config(project))
    assert json.loads((built(project) / "alpha" / "report-data.json").read_text())["summary"] == "Steps 3."


# ------------------------------------------------------------------ strictness


def add_block(project, code, slug="alpha"):
    """Append a hidden python block to a report; returns the report line of the block's fence."""
    report = project / "reports" / slug / "report.md"
    text = report.read_text()
    line = text.count("\n") + 2
    report.write_text(text + "\n```python exec hide\n" + code + "\n```\n")
    return line


def test_read_helpers_refuse_files_outside_the_declared_data(project, tmp_path):
    outside = write(tmp_path / "elsewhere.json", "{}")
    for target in ("PROJECT_ROOT / 'pyproject.toml'", f"{str(outside)!r}"):
        alpha = project / "reports" / "alpha" / "report.md"
        original = alpha.read_text()
        line = add_block(project, f"read_json({target})")
        with pytest.raises(BuildError, match="declare it under inputs") as error:
            build_all(load_config(project))
        assert f"alpha/report.md:{line + 1}:" in str(error.value)  # the line of the call (the block's first), not of the fence
        alpha.write_text(original)
    for helper in ("read_csv", "read_jsonl", "read_scalars", "track"):
        alpha.write_text(original)
        add_block(project, f"{helper}(PROJECT_ROOT / 'pyproject.toml')")
        with pytest.raises(BuildError, match="declare it under inputs"):
            build_all(load_config(project))


def test_raw_open_of_an_undeclared_project_file_fails(project):
    line = add_block(project, "x = 1\nopen(PROJECT_ROOT / 'pyproject.toml').read()")
    with pytest.raises(BuildError, match="declare it under inputs") as error:
        build_all(load_config(project))
    message = str(error.value)
    assert f"alpha/report.md:{line + 2}:" in message and "pyproject.toml" in message


def test_other_raw_reads_of_the_project_fail(project):
    for code in ("import pathlib; pathlib.Path('pyproject.toml').read_text()", "import os; os.listdir(PROJECT_ROOT)",
                 "import os; list(os.scandir(PROJECT_ROOT / 'reports'))", "import io; io.open(PROJECT_ROOT / 'pyproject.toml')"):
        alpha = project / "reports" / "alpha" / "report.md"
        original = alpha.read_text()
        add_block(project, code)
        with pytest.raises(BuildError, match="declare it under inputs"):
            build_all(load_config(project))
        alpha.write_text(original)


def test_a_swallowed_violation_still_fails_the_build(project):
    add_block(project, "try:\n    open(PROJECT_ROOT / 'pyproject.toml')\nexcept Exception:\n    pass")
    with pytest.raises(BuildError, match="declare it under inputs"):
        build_all(load_config(project))


def test_an_undeclared_store_cache_file_fails(project):
    other = project.parent / "other-data"
    write(other / "x.txt", "secret")
    store.publish("other", other)
    cached = store.pull("other@v1") / "x.txt"
    assert cached.read_text() == "secret"
    add_block(project, f"open({str(cached)!r}).read()")
    with pytest.raises(BuildError, match="declare it under inputs") as error:
        build_all(load_config(project))
    assert str(cached) in str(error.value) or "x.txt" in str(error.value)
    # declared, the same file is fine
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text().replace("  descent: descent@latest\n", "  descent: descent@latest\n  other: other@latest\n")
                     .replace(f"open({str(cached)!r}).read()", "metric('Secret', open(inputs.other / 'x.txt').read())"))
    build_all(load_config(project))
    assert "<strong>secret</strong>" in (built(project) / "alpha" / "narrative.html").read_text()


def test_raw_open_of_declared_and_report_files_and_other_places_is_allowed(project, tmp_path):
    add_block(project, (
        "import json, tempfile, pathlib, os\n"
        "steps = json.load(open(inputs.descent / 'data' / 'summary.json'))['steps']\n"
        "svg = open(REPORT_ROOT / 'assets' / 'strip.svg').read()\n"
        "assert pathlib.Path(REPORT_ROOT / 'assets' / 'strip.svg').read_text() == svg\n"
        "assert os.listdir(inputs.descent) and os.listdir(REPORT_ROOT)\n"
        "with tempfile.TemporaryDirectory() as folder:\n    (pathlib.Path(folder) / 'x').write_text('ok')\n"
        "import plotly.graph_objects, numpy\n"
        "metric('Steps', steps)"))
    build_all(load_config(project))
    assert "<strong>3</strong>" in (built(project) / "alpha" / "narrative.html").read_text()


def test_the_guard_is_off_outside_report_blocks(project):
    build_all(load_config(project))
    open(project / "pyproject.toml").read()  # the audit hook stays installed but only enforces while a block runs
    assert list(store.pull("descent@v1").iterdir())


# ------------------------------------------------------------------ publishing reports


def test_publish_report_makes_a_self_contained_version(project, capsys):
    main(["publish", "reports/alpha"])
    assert "published reports/alpha@v1" in capsys.readouterr().out
    [version] = store.versions("reports/alpha")
    paths = {entry["path"] for entry in version["files"]}
    assert {"source/report.md", "source/assets/strip.svg", "site/alpha/index.html", "site/alpha/narrative.html",
            "site/alpha/report-data.json", "site/alpha/manifest.json", "site/alpha/assets/inputs/descent/figures/hist.png"} <= paths
    static = {path for path in paths if path.startswith("site/_static/")}
    assert any(re.fullmatch(r"site/_static/report\.[0-9a-f]+\.js(\.gz)?", path) for path in static)
    assert any("plotly" in path for path in static) and not any(path.startswith("site/beta") for path in paths)
    assert version["metadata"] == {"title": "Alpha", "status": "active", "summary": "The first report, error 0.5."}
    assert version["inputs"] == ["study:descent@v1"]
    page = store.pull("reports/alpha@v1") / "site" / "alpha" / "index.html"
    assert (page.parent.parent / "_static").is_dir() and page.is_file()


def test_republishing_an_unchanged_report_creates_no_version(project, capsys):
    main(["publish", "reports/alpha"])
    capsys.readouterr()
    main(["publish", "reports/alpha"])
    assert "reports/alpha@v1 unchanged" in capsys.readouterr().out
    assert [v["version"] for v in store.versions("reports/alpha")] == [1]
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text() + "\nMore text.\n")
    main(["publish", "reports/alpha"])
    assert "published reports/alpha@v2" in capsys.readouterr().out
    # new data under the same alias is a change as well
    data = make_data(project.parent / "second")
    (data / "data" / "summary.json").write_text('{"steps": 4}')
    store.publish("descent", data)
    main(["publish", "reports/alpha"])
    assert store.versions("reports/alpha")[-1]["inputs"] == ["study:descent@v2"]


def test_lineage_connects_the_report_and_its_input(project):
    main(["publish", "reports/alpha"])
    assert store.lineage("reports/alpha@v1") == {"inputs": ["study:descent@v1"], "consumers": []}
    assert store.lineage("descent@v1")["consumers"] == ["study:reports/alpha@v1"]


def test_publish_refuses_options_on_a_report_folder(project):
    with pytest.raises(SystemExit, match="publishes as reports/<slug> on its own"):
        main(["publish", "reports/alpha", "other"])
