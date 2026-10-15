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
    os.environ["XDG_CACHE_HOME"] = str(base / "cache")
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
    store.publish("models/denoiser", toy, inputs=["study:toy-fit@v1"], aliases=["best"])


def run(keep: bool) -> int:
    from playwright.sync_api import sync_playwright

    base = Path(tempfile.mkdtemp(prefix="cairn-browser-"))
    shots = base / "shots"
    shots.mkdir()
    root = base / "store-root"
    token = init_root(root).read_text().strip()
    store_httpd = make_server(root, port=0)
    store_httpd.RequestHandlerClass.log_message = lambda *a: None
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
        page.get_by_role("link", name="study").first.click()
        page.get_by_role("tab", name="Reports").wait_for()
        page.locator("main").get_by_text("Toy fit summary").first.wait_for()
        shot("03-project-reports")
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
        page.locator(".data-version").first.click()
        page.wait_for_url("**/p/study/a/**")
        page.go_back()
        page.locator(".brand").click()
        page.wait_for_url("**/p/study/")
        page.get_by_role("heading", name="study").wait_for()

        # phone width and dark mode
        page.set_viewport_size({"width": 390, "height": 800})
        page.goto(ui + "/p/study/a/toy-fit")
        page.locator(".graph-node").first.wait_for()
        shot("12-phone-artifact")
        page.goto(ui + "/p/study/")
        page.locator("main").get_by_text("Toy fit summary").first.wait_for()
        shot("13-phone-project")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "no horizontal page scroll on a phone"
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

    print("screenshots:", shots)
    for line in problems:
        print("PROBLEM", line)
    ui_httpd.shutdown()
    store_httpd.shutdown()
    return 1 if problems else 0


if __name__ == "__main__":
    code = run("--keep" in sys.argv)
    print("browser check:", "FAILED" if code else "ok")
    sys.exit(code)
