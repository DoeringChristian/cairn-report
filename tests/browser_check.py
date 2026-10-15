"""A headless-browser walk through the UI (not collected by pytest; run it by hand):

    taskpolicy -b uv run --with playwright python tests/browser_check.py [--keep]

It starts a store server and the UI in threads over a temp folder with data (a dataset, a run folder
with inputs, a published report), logs in, visits projects -> project -> artifact -> a lineage node ->
the report, and writes screenshots (light, dark, phone width) to the temp folder it prints. It fails
on any page error, console error or failed request.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import cairn as store  # noqa: E402
from cairn.cli import main  # noqa: E402
from cairn.server import make_server  # noqa: E402
from cairn.store.tokens import init_root  # noqa: E402
from cairn.ui import RemoteBackend, make_ui_server  # noqa: E402
from conftest import make_data, write  # noqa: E402


def build_data(base: Path, url: str) -> None:
    project = base / "study"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    (project / "cairn.toml").write_text(f'[store]\nurl = "{url}"\nproject = "study"\ninterval = 1\n')
    os.chdir(project)
    os.environ["CAIRN_CACHE_DIR"] = str(base / "cache")
    os.environ.pop("CAIRN_INPUTS", None)
    toy = make_data(base)
    (toy / "notes.md").write_text("# Toy data\n\nA few numbers.\n")
    store.publish("datasets/toy", toy, aliases=["stable"], label="first")
    (toy / "notes.md").write_text("# Toy data\n\nA few more numbers.\n")
    store.publish("datasets/toy", toy, label="second")
    # a run folder with inputs: a script pulls the dataset and writes results
    out = project / "outputs"
    out.mkdir()
    script = ("import pathlib, sys, json; import cairn as store; src = store.pull('datasets/toy@v2'); "
              "pathlib.Path(sys.argv[1], 'result.json').write_text(json.dumps({'rmse': 0.25, 'steps': 40}, indent=2)); "
              "pathlib.Path(sys.argv[1], 'log.txt').write_text('fit done\\n' * 20)")
    subprocess.run([sys.executable, "-c", script, str(out)], check=True, env=os.environ | {"CAIRN_INPUTS": str(out / ".cairn-inputs.jsonl")})
    main(["publish", str(out), "toy-fit"])
    write(project / "reports" / "summary" / "report.md", (
        "---\ntitle: Toy fit summary\nstatus: active\nsummary: The fit of the toy data.\ninputs:\n  fit: toy-fit@latest\n  toy: datasets/toy@v2\n---\n\n"
        "```python exec hide\nresult = (inputs.fit / 'result.json').read_text()\n```\n\n"
        "## Result\n\nThe fit finished: {{ len(result) }} characters.\n\n![Histogram](input:toy/figures/hist.png)\n"))
    main(["publish", "reports/summary"])
    main(["publish", "reports/summary"])  # an unchanged report adds no version; edit it for a second one
    report = project / "reports" / "summary" / "report.md"
    report.write_text(report.read_text() + "\nA later note.\n")
    main(["publish", "reports/summary"])
    long = base / "long-set"  # many files: a page taller than the window, to check the rail while scrolling
    shutil.copytree(toy, long)
    for i in range(10):
        (long / f"part-{i:02d}.txt").write_text(f"part {i}\n" * 5)
    store.publish("datasets/long-set", long)
    store.publish("models/denoiser", toy, inputs=["study:toy-fit@v1"], aliases=["best"])
    # a run: a script that uses the dataset, logs a curve, and publishes a model (the run is its one input)
    (project / "train.py").write_text(
        "import time, cairn\n"
        "run = cairn.init(name='train', config={'lr': 0.1, 'epochs': 3, 'model': {'depth': 4}}, system=0.2)\n"
        "cairn.use('datasets/toy@v2')\n"
        "for step in range(40):\n"
        "    run.add({'loss': 1 / (step + 1), 'acc': step / 40, 'step': step})\n"
        "print('training done')\n"
        "time.sleep(0.6)\n"
        "with cairn.draft('models/student') as model:\n"
        "    model.add_text('weights', 'w')\n")
    subprocess.run([sys.executable, "train.py"], check=True, cwd=project, env=os.environ)


def check_no_overflow(page, label: str) -> None:
    """The files table must not be wider than its card (a long preview line once pushed the file names out of view)."""
    m = page.evaluate("() => { const w = document.querySelector('.table-wrap'); return w ? [w.scrollWidth, w.clientWidth] : [0, 0]; }")
    assert m[0] <= m[1] + 1, f"{label}: table scrolls sideways {m}"


def check_preview(page) -> None:
    """Open every preview of the page: images keep their aspect and fit their container."""
    buttons = page.get_by_role("button", name="Preview", exact=True)
    while buttons.count():  # each opened row turns its button into "Hide"
        buttons.first.click()
    page.wait_for_load_state("networkidle")
    imgs = page.locator(".preview-row img")
    assert imgs.count() >= 1, "an image preview is open"
    for fig in page.locator(".app-figures img").all():
        assert fig.evaluate("e => getComputedStyle(e).objectFit") == "contain", "figure thumbnails show the whole image"
    for i in range(imgs.count()):
        m = imgs.nth(i).evaluate("""e => { const c = e.closest('td').getBoundingClientRect(), r = e.getBoundingClientRect();
            return {nw: e.naturalWidth, nh: e.naturalHeight, w: r.width, h: r.height, cw: c.width, fit: getComputedStyle(e).objectFit}; }""")
        assert m["nw"] > 0, "the preview image loaded"
        assert m["w"] <= m["cw"] + 0.5, f"preview wider than its cell: {m}"
        assert abs(m["w"] / m["h"] - m["nw"] / m["nh"]) < 0.02 or m["fit"] == "contain", f"preview aspect distorted: {m}"
        assert m["fit"] == "contain", f"preview must use object-fit contain: {m}"


def run(keep: bool) -> int:
    from playwright.sync_api import sync_playwright

    base = Path(tempfile.mkdtemp(prefix="cairn-browser-"))
    shots = base / "shots"
    shots.mkdir()
    root = base / "store-root"
    token = init_root(root).read_text().strip()
    store_httpd = make_server(root, port=0)
    store_httpd.log = None
    threading.Thread(target=store_httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{store_httpd.server_address[1]}"
    os.environ["CAIRN_STORE_TOKEN"] = token
    build_data(base, url)
    ui_httpd = make_ui_server(RemoteBackend(url, False), port=0, secure_cookie=False)
    ui_httpd.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=ui_httpd.serve_forever, daemon=True).start()
    ui = f"http://127.0.0.1:{ui_httpd.server_address[1]}"
    problems: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        context = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
        page = context.new_page()
        page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
        page.on("console", lambda m: m.type == "error" and "favicon.ico" not in (m.location.get("url") or "") and problems.append(f"console error: {m.text} ({m.location.get('url')})"))
        page.on("requestfailed", lambda r: problems.append(f"request failed: {r.url}"))
        context.on("response", lambda r: r.status >= 400 and r.status != 401 and problems.append(f"HTTP {r.status}: {r.url}"))

        def view(name: str) -> None:  # a viewport screenshot: what the window shows, so sticky elements are true
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(shots / f"{name}.png"))

        def check_rail(label: str) -> None:
            vh = page.evaluate("window.innerHeight")
            box = page.locator(".rail").bounding_box()
            assert box and abs(box["y"]) < 1 and abs(box["x"]) < 1 and abs(box["height"] - vh) < 1.5, f"{label}: rail at {box}, viewport height {vh}"

        def scroll_check(label: str) -> None:
            height = page.evaluate("document.documentElement.scrollHeight")
            assert height > page.evaluate("window.innerHeight") * 1.5, f"{label}: page is not long ({height})"
            for where, y in (("top", 0), ("middle", height // 2), ("bottom", height)):
                page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
                page.mouse.wheel(0, 1)  # a real scroll event, so the compositor repaints before the screenshot
                page.wait_for_timeout(2500)
                check_rail(f"{label} {where}")
                view(f"{label}-{where}")
            page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")

        def shot(name: str) -> None:
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(shots / f"{name}.png"), full_page=True)

        # login
        page.goto(ui + "/p/study/")
        page.wait_for_url("**/login?next=**")
        page.get_by_label("Store token").wait_for()
        shot("01-login")
        page.get_by_label("Store token").fill("w" * 40)
        page.get_by_role("button", name="Open store").click()
        page.get_by_role("alert").wait_for()
        problems[:] = [x for x in problems if "401" not in x and "Failed to load resource" not in x]  # the wrong token's 401
        page.get_by_label("Store token").fill(token)
        page.get_by_role("button", name="Open store").click()
        page.wait_for_url("**/p/study/")
        page.get_by_role("heading", name="study").wait_for()

        # projects -> project
        page.get_by_role("link", name="Cairn").click()
        page.get_by_role("heading", name="Projects").wait_for()
        shot("02-projects")
        check_rail("projects")
        page.get_by_role("link", name="study").first.click()
        page.get_by_role("tab", name="Reports").wait_for()
        page.locator("main").get_by_text("Toy fit summary").first.wait_for()
        shot("03-project-reports")
        check_rail("project")
        page.get_by_role("tab", name="Artifacts").click()
        page.get_by_role("link", name="toy-fit").first.wait_for()
        shot("04-project-artifacts")
        page.get_by_label("Search").fill("denoiser")
        assert page.locator("tbody tr").count() == 1, "search should leave one artifact"
        shot("05-project-search")
        page.get_by_label("Search").fill("")

        # artifact: versions, files, previews, lineage
        page.get_by_role("link", name="toy-fit").first.click()
        page.get_by_role("heading", name="toy-fit").wait_for()
        page.locator(".graph-node").first.wait_for()
        assert page.locator(".graph-node").count() >= 3, "lineage shows the artifact, its input and its consumers"
        page.get_by_role("button", name="Preview").first.click()
        page.locator(".preview-row").first.wait_for()
        shot("06-artifact")
        page.goto(ui + "/p/study/a/datasets/long-set")
        page.get_by_role("heading", name="datasets/long-set").wait_for()
        page.get_by_role("button", name="Preview").first.wait_for()
        check_preview(page)
        check_no_overflow(page, "files table")
        page.locator(".preview-row img").first.scroll_into_view_if_needed()
        page.mouse.wheel(0, 1)
        page.wait_for_timeout(1500)
        view("21-image-preview")
        scroll_check("20-long-artifact")
        page.goto(ui + "/p/study/a/toy-fit")
        page.locator(".graph-node").first.wait_for()
        page.locator(".image-copy").first.click()
        # a lineage node: the dataset on the left
        page.locator(".graph-node [role=link]", has_text="datasets/toy").first.click()
        page.wait_for_url("**/a/datasets/toy@v2")
        page.get_by_role("heading", name="datasets/toy").wait_for()
        page.locator(".graph-node").first.wait_for()
        page.locator(".figure-card").first.wait_for()
        shot("07-dataset")
        before = page.locator(".graph-node").count()
        page.locator(".graph-node", has_text="toy-fit").locator(".graph-expand").click()  # one more level to the right
        page.wait_for_function(f"document.querySelectorAll('.graph-node').length > {before}")
        shot("08-expanded")
        page.goto(ui + "/p/study/a/models/denoiser")
        page.locator(".graph-node").nth(1).wait_for()
        page.locator(".graph-expand").first.click()  # one more level to the left: the dataset
        page.locator(".graph-node").nth(2).wait_for()
        shot("09-chain")

        # a report: open it, come back through the brand link
        page.goto(ui + "/p/study/a/reports/summary")
        page.get_by_role("link", name="Open report v").wait_for()
        shot("10-report-artifact")
        page.get_by_role("link", name="Open report v").click()
        page.wait_for_url("**/p/study/r/summary@v*/")
        page.locator("#title").wait_for()
        page.wait_for_function("document.querySelector('#title').textContent.length > 0")
        shot("11-report")
        # reports keep only copy-location: no comment controls; selecting text offers the copy button
        assert page.locator("#comment-popover, #selection-comment, .image-comment, #copy-comments, #clear-comments").count() == 0
        page.evaluate("""() => { const p = document.querySelector('#report-prose p'); const range = document.createRange();
            range.selectNodeContents(p); const s = getSelection(); s.removeAllRanges(); s.addRange(range); }""")
        page.locator("#report-prose p").first.dispatch_event("mouseup")
        page.locator("#selection-copy").wait_for(state="visible")
        page.locator("#selection-copy").click()
        page.wait_for_function("document.querySelector('#selection-copy').dataset.tooltip !== 'Copy location'")
        page.locator(".figure-card").first.click()
        page.locator("#lightbox-copy").wait_for(state="visible")
        page.keyboard.press("Escape")
        back = page.locator("#back-link")  # came from the report artifact's page: back goes there
        back.wait_for()
        assert back.inner_text() == "← Back" and back.get_attribute("href") == "/p/study/a/reports/summary", back.get_attribute("href")
        labels = page.locator(".rail section:not([hidden]) .rail-label").all_inner_texts()  # the rail lists the app's own sections
        assert [x.split(" · ")[0].title() for x in labels] == ["Projects", "Reports", "Artifacts", "Runs"], labels
        assert page.locator(".rail a[aria-current=page]").all_inner_texts() == ["study", "Toy fit summary"]
        page.keyboard.press("Tab")
        back.focus()
        page.keyboard.press("Enter")
        page.wait_for_url("**/p/study/a/reports/summary")
        page.goto(ui + "/p/study/r/summary/")  # opened directly: back goes to the project page
        page.locator("#back-link").wait_for()
        assert page.locator("#back-link").inner_text() == "← study" and page.locator("#back-link").get_attribute("href") == "/p/study/"
        page.goto(ui + "/p/study/a/reports/summary")
        page.get_by_role("link", name="Open report v").click()
        page.wait_for_url("**/p/study/r/summary@v*/")
        page.locator("#title").wait_for()
        page.locator(".data-version").first.click()
        page.wait_for_url("**/p/study/a/**")
        page.go_back()
        page.locator(".brand").click()
        page.wait_for_url("**/p/study/")
        page.get_by_role("heading", name="study").wait_for()

        # a run: its panel, its node in an output's lineage, the data-only view, the Runs tab
        page.goto(ui + "/p/study/a/runs/train")
        page.locator(".run-chart").first.wait_for()
        assert page.locator(".run-chart").count() >= 4, "charts of loss, acc, and the system metrics"
        assert "training done" in page.locator(".run-log pre").first.text_content()
        shot("15-run")
        page.goto(ui + "/p/study/a/models/student")
        page.locator(".graph-node.is-run").first.wait_for()
        shot("16-output-with-run")
        page.get_by_role("button", name="Data only").click()
        page.wait_for_function("document.querySelectorAll('.graph-node.is-run').length === 0")
        assert page.locator(".graph-node", has_text="datasets/toy").count() == 1, "the data behind the run"
        shot("17-data-only")
        page.goto(ui + "/p/study/?tab=runs")
        page.get_by_role("link", name="train").first.wait_for()
        shot("18-runs-tab")

        # desktop: the styled selects next to the search box
        page.goto(ui + "/p/study/")
        page.get_by_label("Sort").wait_for()
        assert page.get_by_label("Sort").evaluate("e => getComputedStyle(e).appearance") == "none", "selects are styled, not native"
        assert page.get_by_label("Sort").evaluate("e => e.offsetHeight") == page.get_by_label("Search").evaluate("e => e.offsetHeight"), "select and search are the same height"
        page.get_by_label("Sort").focus()
        page.wait_for_timeout(200)
        shot("22-desktop-selects")
        page.goto(ui + "/p/study/a/toy-fit")
        page.get_by_label("Version", exact=True).wait_for()
        shot("23-desktop-version-select")

        # phone (390x844) and tablet (820x1180): every page, no sideways page scroll, tap targets, collapsed rail
        def responsive(tag: str, width: int, height: int) -> None:
            page.set_viewport_size({"width": width, "height": height})
            pages = [("projects", "/", "h1"), ("project", "/p/study/", ".tabs"), ("project-artifacts", "/p/study/?tab=artifacts", ".table-card"),
                     ("artifact", "/p/study/a/toy-fit", ".graph-node"), ("dataset", "/p/study/a/datasets/toy@v2", ".graph-node"),
                     ("run", "/p/study/a/runs/train", ".run-chart"), ("report", "/p/study/r/summary/", "#report-prose h2")]
            for name, path, ready in pages:
                page.goto(ui + path)
                page.locator(ready).first.wait_for()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(300)
                sw = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
                assert sw[0] <= sw[1], f"{tag} {name}: horizontal page scroll {sw}"
                wide = page.evaluate("""() => [...document.querySelectorAll('main *')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right > innerWidth + 1
                    && !e.closest('.table-wrap, .graph-scroll, .tabs, pre, .raw-html, .math, svg, table, .plotly-card'); }).map(e => e.tagName + '.' + e.className).slice(0, 5)""")
                assert not wide, f"{tag} {name}: elements stick out of the window: {wide}"
                small = page.evaluate("""() => [...document.querySelectorAll('.tabs button, .action-button, select, input, .rail-toggle, .back-link, .image-copy, .graph-toggle button, .report-list a')]
                    .filter(e => e.offsetParent !== null || getComputedStyle(e).position === 'fixed')
                    .map(e => [e.tagName + '.' + e.className, e.getBoundingClientRect()]).filter(([, r]) => r.height > 0 && r.height < 39.5).map(([n, r]) => n + ' ' + Math.round(r.height)).slice(0, 5)""")
                if width < 600:
                    assert not small, f"{tag} {name}: tap targets under 40px: {small}"
                    assert page.locator(".rail .report-list:not([hidden])").first.is_hidden() or not page.locator(".rail .report-list").count(), f"{tag} {name}: rail lists start collapsed"
                    rail_h = page.locator(".rail").bounding_box()["height"]
                    assert rail_h < 70, f"{tag} {name}: collapsed rail is {rail_h}px tall"
                page.screenshot(path=str(shots / f"{tag}-{name}.png"), full_page=True)
                if name == "report":
                    back = page.locator("#back-link")
                    back.wait_for()
                    page.screenshot(path=str(shots / f"{tag}-{name}-viewport.png"))
                if name in ("project", "report") and width < 760:
                    page.locator(".rail-toggle").click()
                    assert page.locator(".rail .report-list:not([hidden])").first.is_visible(), "Browse opens the rail lists"
                    page.screenshot(path=str(shots / f"{tag}-{name}-browse.png"))
                    page.locator(".rail-toggle").click()
                    assert page.locator(".rail .report-list:not([hidden])").first.is_hidden(), "Browse closes them again"

        responsive("phone", 390, 844)
        responsive("tablet", 820, 1180)
        page.set_viewport_size({"width": 1280, "height": 900})
        dark = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark")
        page = dark.new_page()
        page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
        page.on("response", lambda r: r.status >= 400 and r.status != 401 and problems.append(f"HTTP {r.status}: {r.url}"))
        page.goto(ui + "/login")
        page.get_by_label("Store token").fill(token)
        page.get_by_role("button", name="Open store").click()
        page.wait_for_url(ui + "/")
        page.goto(ui + "/p/study/a/toy-fit")
        page.locator(".graph-node").first.wait_for()
        shot("14-dark-artifact")
        browser.close()

    out = Path("/tmp/cairn-ui-mobile-shots")
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(shots, out)
    print("screenshots:", shots, "and", out)
    for line in problems:
        print("PROBLEM", line)
    ui_httpd.shutdown()
    store_httpd.shutdown()
    return 1 if problems else 0


if __name__ == "__main__":
    code = run("--keep" in sys.argv)
    print("browser check:", "FAILED" if code else "ok")
    sys.exit(code)
