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
import re
import shutil
import subprocess
import sys
import tempfile
import struct
import threading
import zlib
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import cairn as store  # noqa: E402
from cairn.cli import main  # noqa: E402
from cairn.server import make_server  # noqa: E402
from cairn.store.tokens import init_root  # noqa: E402
from cairn.ui import RemoteBackend, make_ui_server, open_backend  # noqa: E402
from conftest import make_data, write, write_config  # noqa: E402


def png(path: Path, width: int, height: int) -> None:
    """A gradient PNG of the given size, written without any imaging library (a picture worth zooming into)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(
        b"\0"
        + bytes(
            v
            for x in range(width)
            for v in ((x * 255) // width, (y * 255) // height, 128 + ((x // 40 + y // 40) % 2) * 90)
        )
        for y in range(height)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def build_data(base: Path, url: str) -> None:
    project = base / "study"
    project.mkdir()
    write_config(project, f'[remote]\nurl = "{url}/study"\n[repo]\ninterval = 1\n')
    os.chdir(project)
    os.environ.pop("CAIRN_INPUTS", None)
    toy = make_data(base)
    (toy / "notes.md").write_text("# Toy data\n\nA few numbers.\n")
    store.publish("datasets/toy", toy, aliases=["stable"], label="first")
    (toy / "notes.md").write_text("# Toy data\n\nA few more numbers.\n")
    store.publish("datasets/toy", toy, label="second")
    # a run folder with inputs: a script pulls the dataset and writes results
    out = project / "outputs"
    out.mkdir()
    script = (
        "import pathlib, sys, json; import cairn as store; src = store.pull('datasets/toy@v2'); "
        "pathlib.Path(sys.argv[1], 'result.json').write_text(json.dumps({'rmse': 0.25, 'steps': 40}, indent=2)); "
        "pathlib.Path(sys.argv[1], 'log.txt').write_text('fit done\\n' * 20)"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(out)],
        check=True,
        env=os.environ | {"CAIRN_INPUTS": str(out / ".cairn-inputs.jsonl")},
    )
    main(["publish", str(out), "toy-fit"])
    write(
        project / "reports" / "summary" / "report.md",
        (
            "---\ntitle: Toy fit summary\nstatus: active\nsummary: The fit of the toy data.\ninputs:\n  fit: toy-fit@latest\n  toy: datasets/toy@v2\n---\n\n"
            "```python exec hide\nresult = (inputs.fit / 'result.json').read_text()\n```\n\n"
            "## Result\n\nThe fit finished: {{ len(result) }} characters.\n\n![Histogram](input:toy/figures/hist.png)\n"
        ),
    )
    main(["publish", "reports/summary"])
    main(["publish", "reports/summary"])  # an unchanged report adds no version; edit it for a second one
    report = project / "reports" / "summary" / "report.md"
    report.write_text(report.read_text() + "\nA later note.\n")
    main(["publish", "reports/summary"])
    long = base / "long-set"  # many files: a page taller than the window, to check the rail while scrolling
    shutil.copytree(toy, long)
    for i in range(10):
        (long / f"part-{i:02d}.txt").write_text(f"part {i}\n" * 5)
    png(long / "figures" / "wide.png", 1200, 400)  # three images: a gallery to page through in the lightbox
    (long / "figures" / "tall.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="600" viewBox="0 0 300 600">'
        '<rect width="300" height="600" fill="#dcebe5"/><circle cx="150" cy="300" r="120" fill="#176b5b"/></svg>'
    )
    store.publish("datasets/long-set", long)
    store.publish("models/denoiser", toy, inputs=["toy-fit@v1"], aliases=["best"])
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
        "    model.add_text('weights', 'w')\n"
    )
    subprocess.run([sys.executable, "train.py"], check=True, cwd=project, env=os.environ)


def check_no_overflow(page, label: str) -> None:
    """The files table must not be wider than its card (a long preview line once pushed the file names out of view)."""
    m = page.evaluate(
        "() => { const w = document.querySelector('.table-wrap'); return w ? [w.scrollWidth, w.clientWidth] : [0, 0]; }"
    )
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
        m = imgs.nth(i).evaluate(
            """e => { const c = e.closest('td').getBoundingClientRect(), r = e.getBoundingClientRect();
            return {nw: e.naturalWidth, nh: e.naturalHeight, w: r.width, h: r.height, cw: c.width, fit: getComputedStyle(e).objectFit}; }"""
        )
        assert m["nw"] > 0, "the preview image loaded"
        assert m["w"] <= m["cw"] + 0.5, f"preview wider than its cell: {m}"
        assert (
            abs(m["w"] / m["h"] - m["nw"] / m["nh"]) < 0.02 or m["fit"] == "contain"
        ), f"preview aspect distorted: {m}"
        assert m["fit"] == "contain", f"preview must use object-fit contain: {m}"


LIGHTBOX = "dialog.app-lightbox"


def lightbox_state(page) -> dict:
    return page.evaluate(
        """() => { const d = document.querySelector('dialog.app-lightbox'), img = d && d.querySelector('.stage img');
        return {open: !!(d && d.open), title: d?.querySelector('.lightbox-path')?.textContent ?? null, zoom: d?.querySelector('.zoom span')?.textContent ?? null,
                meta: d?.querySelector('.lightbox-meta')?.textContent ?? null, x: img ? parseFloat(img.style.getPropertyValue('--pan-x')) : null,
                y: img ? parseFloat(img.style.getPropertyValue('--pan-y')) : null, loaded: !!(img && img.complete && img.naturalWidth > 0),
                overflow: document.documentElement.style.overflow, focusInside: !!(d && d.contains(document.activeElement)),
                download: d?.querySelector('.lightbox-download')?.getAttribute('href') ?? null}; }"""
    )


def wait_image(page) -> None:
    page.wait_for_function(
        "() => { const i = document.querySelector('dialog.app-lightbox .stage img'); return i && i.complete && i.naturalWidth > 0 && !i.classList.contains('is-loading'); }"
    )


def check_lightbox(page, shots: Path) -> None:
    """The lightbox from the inline preview and from the file list: one gallery in list order, arrows, zoom (keys,
    buttons, wheel), mouse panning, reset on a new image, focus kept inside and given back, no page scroll behind."""
    images = page.evaluate(
        "() => [...document.querySelectorAll('.table-card tbody tr td:first-child')].filter(td => td.querySelector('.thumb:not(.thumb-spacer)')).map(td => td.querySelector('a:not(.thumb)').textContent)"
    )
    assert images == ["figures/hist.png", "figures/tall.svg", "figures/wide.png"], images
    # (a) from the inline preview of the second image
    opener = page.locator(".preview-row .preview-open").nth(1)
    opener.scroll_into_view_if_needed()
    scroll = page.evaluate("scrollY")
    opener.click()
    page.locator(LIGHTBOX + "[open]").wait_for()
    wait_image(page)
    state = lightbox_state(page)
    assert state["title"] == "figures/tall.svg" and state["meta"].startswith("2 of 3"), state
    assert state["overflow"] == "hidden" and state["focusInside"] and state["zoom"] == "100%", state
    assert state["download"] and state["download"].endswith("?download=1"), state
    page.screenshot(path=str(shots / "30-lightbox-preview.png"))
    page.keyboard.press("ArrowRight")
    wait_image(page)
    assert lightbox_state(page)["title"] == "figures/wide.png"
    page.keyboard.press("ArrowRight")  # wraps around
    assert lightbox_state(page)["title"] == "figures/hist.png"
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    assert lightbox_state(page)["title"] == "figures/tall.svg"
    page.get_by_role("button", name="Next image").click()
    wait_image(page)
    assert lightbox_state(page)["title"] == "figures/wide.png"
    # zoom: keys, buttons, the wheel about the pointer
    page.keyboard.press("+")
    assert lightbox_state(page)["zoom"] == "125%"
    page.get_by_role("button", name="Zoom in").click()
    assert lightbox_state(page)["zoom"] == "150%"
    page.keyboard.press("-")
    assert lightbox_state(page)["zoom"] == "125%"
    box = page.locator(LIGHTBOX + " .stage img").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx + 80, cy + 30)
    page.mouse.wheel(0, -120)
    page.wait_for_timeout(200)
    state = lightbox_state(page)
    assert state["zoom"] == "140%" and abs(state["x"]) > 1, f"the wheel zooms about the pointer: {state}"
    page.wait_for_timeout(200)
    page.screenshot(path=str(shots / "31-lightbox-zoomed.png"))
    # pan with the mouse
    before = lightbox_state(page)
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 60, cy + 40, steps=5)
    page.mouse.up()
    after = lightbox_state(page)
    assert (
        abs(after["x"] - before["x"] - 60) < 2 and abs(after["y"] - before["y"] - 40) < 2
    ), f"panning moves the image: {before} -> {after}"
    page.get_by_role("button", name="Reset").click()
    state = lightbox_state(page)
    assert state["zoom"] == "100%" and state["x"] == 0 and state["y"] == 0, state
    page.keyboard.press("+")
    page.keyboard.press("ArrowLeft")  # a new image starts at 100%, centred
    state = lightbox_state(page)
    assert state["zoom"] == "100%" and state["x"] == 0 and state["title"] == "figures/tall.svg", state
    for _ in range(12):  # focus stays inside while open
        page.keyboard.press("Tab")
        assert lightbox_state(page)["focusInside"], "Tab left the lightbox"
    page.keyboard.press("Shift+Tab")
    assert lightbox_state(page)["focusInside"], "Shift+Tab left the lightbox"
    page.mouse.wheel(0, 400)  # the page behind does not scroll
    assert page.evaluate("scrollY") == scroll, "the page scrolled behind the lightbox"
    page.keyboard.press("Escape")
    page.locator(LIGHTBOX + "[open]").wait_for(state="detached")
    state = lightbox_state(page)
    assert not state["open"] and state["overflow"] == "", state
    assert opener.evaluate("e => e === document.activeElement"), "focus goes back to the preview on close"
    # (b) from a thumbnail in the file list, closed with the visible button
    page.get_by_role("link", name="Open figures/wide.png in the viewer").click()
    page.locator(LIGHTBOX + "[open]").wait_for()
    wait_image(page)
    state = lightbox_state(page)
    assert state["title"] == "figures/wide.png" and state["meta"].startswith("3 of 3"), state
    page.get_by_role("button", name="Close").click()
    page.locator(LIGHTBOX + "[open]").wait_for(state="detached")
    # (c) from a figure card
    page.locator(".app-figures .figure-card").first.click()
    page.locator(LIGHTBOX + "[open]").wait_for()
    assert lightbox_state(page)["title"] == "figures/hist.png"
    page.keyboard.press("Escape")
    page.locator(LIGHTBOX + "[open]").wait_for(state="detached")
    thumbs = page.locator(".thumb img").evaluate_all("l => l.map(i => i.loading)")
    assert thumbs and all(x == "lazy" for x in thumbs), thumbs


def check_touch_lightbox(browser, ui: str, token: str, shots: Path, problems: list[str]) -> None:
    """On a touch phone: a thumbnail opens the lightbox, a pinch zooms, one finger pans, a swipe pages."""
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True, device_scale_factor=2
    )
    page = context.new_page()
    page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
    page.on("console", lambda m: m.type == "error" and problems.append(f"console error: {m.text}"))
    page.goto(ui + "/login")
    page.get_by_label("Token").fill(token)
    page.get_by_role("button", name="Open repo").click()
    page.wait_for_url(ui + "/")
    page.goto(ui + "/p/study/a/datasets/long-set")
    page.get_by_role("link", name="Open figures/wide.png in the viewer").tap()
    page.locator(LIGHTBOX + "[open]").wait_for()
    wait_image(page)
    page.wait_for_timeout(300)
    page.screenshot(path=str(shots / "phone-lightbox.png"))
    sw = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
    assert sw[0] <= sw[1], f"phone lightbox scrolls sideways {sw}"
    for name in ("Close", "Previous image", "Next image", "Zoom in", "Zoom out", "Reset"):
        h = page.get_by_role("button", name=name).bounding_box()
        assert h and h["height"] >= 39.5 and h["x"] >= 0 and h["x"] + h["width"] <= 390, f"phone lightbox {name}: {h}"
    cdp = context.new_cdp_session(page)
    box = page.locator(LIGHTBOX + " .stage img").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def touch(kind: str, points: list[tuple[float, float]]) -> None:
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": kind, "touchPoints": [{"x": x, "y": y, "id": i} for i, (x, y) in enumerate(points)]},
        )

    touch("touchStart", [(cx - 30, cy), (cx + 30, cy)])
    for step in range(1, 6):
        touch("touchMove", [(cx - 30 - step * 12, cy), (cx + 30 + step * 12, cy)])
    touch("touchEnd", [])
    zoom = int(lightbox_state(page)["zoom"].rstrip("%"))
    assert zoom > 150, f"a pinch zooms: {zoom}%"
    before = lightbox_state(page)
    touch("touchStart", [(cx, cy)])
    for step in range(1, 6):
        touch("touchMove", [(cx - step * 10, cy + step * 4)])
    touch("touchEnd", [])
    after = lightbox_state(page)
    assert after["x"] < before["x"] - 30, f"one finger pans a zoomed image: {before} -> {after}"
    page.screenshot(path=str(shots / "phone-lightbox-zoomed.png"))
    page.get_by_role("button", name="Reset").tap()
    touch("touchStart", [(cx + 100, cy)])
    for step in range(1, 6):
        touch("touchMove", [(cx + 100 - step * 30, cy)])
    touch("touchEnd", [])
    assert lightbox_state(page)["title"] == "figures/hist.png", "a swipe to the left shows the next image (wrapping)"
    context.close()


def check_palette(page, shots: Path, tag: str) -> None:
    """The palette page, reached from the rail's footer link (the server serves the app at / and /p/...)."""
    page.locator(".rail-note").get_by_role("link", name="Components").click()
    page.wait_for_url("**/palette")
    page.get_by_role("heading", name="Components", exact=True).wait_for()
    for y in range(
        0, page.evaluate("document.documentElement.scrollHeight"), 600
    ):  # the lazy images load as they come near
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(60)
    page.wait_for_function("[...document.querySelectorAll('main img')].every(i => i.complete && i.naturalWidth > 0)")
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=str(shots / f"{tag}-palette.png"), full_page=True)
    page.get_by_role("button", name="Open the gallery").click()
    page.locator(LIGHTBOX + "[open]").wait_for()
    wait_image(page)
    page.screenshot(path=str(shots / f"{tag}-palette-lightbox.png"))
    page.keyboard.press("Escape")
    page.locator(LIGHTBOX + "[open]").wait_for(state="detached")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(300)
    sw = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert sw[0] <= sw[1], f"{tag} palette at phone width: horizontal page scroll {sw}"
    wide = page.evaluate(
        """() => [...document.querySelectorAll('main *')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right > innerWidth + 1
        && !e.closest('.table-wrap, .tabs, pre, svg, table'); }).map(e => e.tagName + '.' + e.className).slice(0, 5)"""
    )
    assert not wide, f"{tag} palette at phone width: elements stick out: {wide}"
    page.screenshot(path=str(shots / f"{tag}-palette-phone.png"), full_page=True)
    page.set_viewport_size({"width": 1280, "height": 900})


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
    os.environ["CAIRN_TOKEN"] = token
    build_data(base, url)
    ui_httpd = make_ui_server(RemoteBackend(url), port=0)
    ui_httpd.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=ui_httpd.serve_forever, daemon=True).start()
    ui = f"http://127.0.0.1:{ui_httpd.server_address[1]}"
    problems: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        context = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
        page = context.new_page()
        page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
        page.on(
            "console",
            lambda m: m.type == "error"
            and "favicon.ico" not in (m.location.get("url") or "")
            and problems.append(f"console error: {m.text} ({m.location.get('url')})"),
        )
        page.on("requestfailed", lambda r: problems.append(f"request failed: {r.url}"))
        context.on(
            "response", lambda r: r.status >= 400 and r.status != 401 and problems.append(f"HTTP {r.status}: {r.url}")
        )

        def view(name: str) -> None:  # a viewport screenshot: what the window shows, so sticky elements are true
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(shots / f"{name}.png"))

        def check_rail(label: str) -> None:
            vh = page.evaluate("window.innerHeight")
            box = page.locator(".rail").bounding_box()
            assert (
                box and abs(box["y"]) < 1 and abs(box["x"]) < 1 and abs(box["height"] - vh) < 1.5
            ), f"{label}: rail at {box}, viewport height {vh}"

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
        page.get_by_label("Token").wait_for()
        shot("01-login")
        page.get_by_label("Token").fill("w" * 40)
        page.get_by_role("button", name="Open repo").click()
        page.get_by_role("alert").wait_for()
        problems[:] = [
            x for x in problems if "401" not in x and "Failed to load resource" not in x
        ]  # the wrong token's 401
        page.get_by_label("Token").fill(token)
        page.get_by_role("button", name="Open repo").click()
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
        check_lightbox(page, shots)
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

        # a version by its id: the Reference panel shows name@v<n>, the short id and the whole id; /v/<short id>
        # and /v/<id> open the same version (the rail marks its artifact); an alias in the URL names a version too
        def reference_rows() -> dict:
            page.locator("section.data-panel[aria-label=Reference] .data-row").first.wait_for()
            return page.evaluate(
                """() => Object.fromEntries([...document.querySelectorAll('section.data-panel[aria-label=Reference] .data-row')]
                .map(r => [r.querySelector('.data-name').textContent, r.querySelector('.data-version').textContent]))"""
            )

        page.goto(ui + "/p/study/a/toy-fit")
        page.get_by_role("heading", name="toy-fit").wait_for()
        rows = reference_rows()
        short, whole = rows["Short id"], rows["Id"]
        assert rows["Version"] == "toy-fit@v1", rows
        assert re.fullmatch(r"[0-9a-f]{64}", whole) and whole.startswith(short) and len(short) >= 7, rows
        assert "uid" not in page.locator("main").inner_text().lower().split(), "no uid wording"
        data = page.evaluate(
            "async () => (await fetch('/ui-api/projects/study/version?ref=toy-fit', {headers: {'X-Cairn': '1'}})).json()"
        )
        assert data["ref"] == "toy-fit@v1" and data["version"]["id"] == whole and "uid" not in data["version"], data
        assert all(re.fullmatch(r"[0-9a-f]{64}", i) for i in data["version"]["inputs"]), data["version"]["inputs"]
        assert all(data["nodes"][i]["name"] for i in data["version"]["inputs"] + data["consumers"]), data["nodes"]
        inputs = page.locator("section.data-panel[aria-label=Inputs] a.data-version")
        assert inputs.all_inner_texts() == ["v2"], inputs.all_inner_texts()
        assert inputs.first.get_attribute("href") == "/p/study/a/datasets/toy@v2", inputs.first.get_attribute("href")
        page.goto(ui + f"/p/study/v/{short}")
        page.get_by_role("heading", name="toy-fit").wait_for()
        page.locator(".graph-node").first.wait_for()
        assert reference_rows()["Id"] == whole
        assert page.locator(".rail a[aria-current=page]").all_inner_texts() == ["study", "toy-fit"]
        shot("08b-by-id")
        page.goto(ui + f"/p/study/v/{whole}")
        page.get_by_role("heading", name="toy-fit").wait_for()
        assert reference_rows()["Version"] == "toy-fit@v1"
        page.goto(ui + "/p/study/a/models/denoiser@best")
        page.get_by_role("heading", name="models/denoiser").wait_for()
        assert reference_rows()["Version"] == "models/denoiser@v1"
        page.goto(ui + "/p/study/a/datasets/toy@stable")
        page.get_by_role("heading", name="datasets/toy").wait_for()
        assert reference_rows()["Version"] == "datasets/toy@v1"
        page.get_by_role("button", name="Version", exact=True).click()  # the select goes to the chosen version's page
        page.get_by_role("option", name=re.compile(r"^v2")).click()
        page.wait_for_url("**/a/datasets/toy@v2")
        page.wait_for_function("document.title.startsWith('datasets/toy@v2')")

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
        assert (
            page.locator(
                "#comment-popover, #selection-comment, .image-comment, #copy-comments, #clear-comments"
            ).count()
            == 0
        )
        page.evaluate(
            """() => { const p = document.querySelector('#report-prose p'); const range = document.createRange();
            range.selectNodeContents(p); const s = getSelection(); s.removeAllRanges(); s.addRange(range); }"""
        )
        page.locator("#report-prose p").first.dispatch_event("mouseup")
        page.locator("#selection-copy").wait_for(state="visible")
        page.locator("#selection-copy").click()
        page.wait_for_function("document.querySelector('#selection-copy').dataset.tooltip !== 'Copy location'")
        page.locator(".figure-card").first.click()
        page.locator("#lightbox-copy").wait_for(state="visible")
        page.keyboard.press("Escape")
        back = page.locator("#back-link")  # came from the report artifact's page: back goes there
        back.wait_for()
        assert (
            back.inner_text() == "← Back" and back.get_attribute("href") == "/p/study/a/reports/summary"
        ), back.get_attribute("href")
        labels = page.locator(
            ".rail section:not([hidden]) .rail-label"
        ).all_inner_texts()  # the rail lists the app's own sections
        assert [x.split(" · ")[0].title() for x in labels] == ["Projects", "Reports", "Artifacts", "Runs"], labels
        assert page.locator(".rail a[aria-current=page]").all_inner_texts() == ["study", "Toy fit summary"]
        page.keyboard.press("Tab")
        back.focus()
        page.keyboard.press("Enter")
        page.wait_for_url("**/p/study/a/reports/summary")
        page.goto(ui + "/p/study/r/summary/")  # opened directly: back goes to the project page
        page.locator("#back-link").wait_for()
        assert (
            page.locator("#back-link").inner_text() == "← study"
            and page.locator("#back-link").get_attribute("href") == "/p/study/"
        )
        page.goto(ui + "/p/study/a/reports/summary")
        page.get_by_role("link", name="Open report v").click()
        page.wait_for_url("**/p/study/r/summary@v*/")
        page.locator("#title").wait_for()
        pinned = page.evaluate(
            "async () => (await (await fetch('/ui-api/projects/study/version?ref=reports/summary', "
            "{headers: {'X-Cairn': '1'}})).json()).version.inputs"
        )
        first = page.locator(".data-version").first
        first.wait_for()
        href = first.get_attribute("href") or ""
        assert href.startswith("/p/study/v/") and href.split("/")[-1] in pinned, (href, pinned)  # by id: a number
        # the build recorded can name another version after a renumbering (a remote added later)
        first.click()
        page.wait_for_url("**/p/study/v/**")
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

        # desktop: the selects are our own listbox, not the system menu; mouse and keyboard both choose
        page.goto(ui + "/p/study/")
        sort = page.get_by_role("button", name="Sort")
        sort.wait_for()
        assert page.locator("main select").count() == 0, "no native selects"
        assert sort.evaluate("e => e.offsetHeight") == page.get_by_label("Search").evaluate(
            "e => e.offsetHeight"
        ), "select and search are the same height"
        sort.click()
        page.get_by_role("listbox", name="Sort").wait_for()
        shot("22-desktop-selects")
        page.get_by_role("option", name="Sort: name").click()
        page.wait_for_url("**sort=name**")
        assert sort.inner_text() == "Sort: name" and page.get_by_role("listbox").count() == 0
        sort.focus()
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Home")
        page.keyboard.press("Enter")
        page.wait_for_function("!location.search.includes('sort=name')")
        sort.click()
        page.keyboard.press("Escape")
        assert page.get_by_role("listbox").count() == 0, "Escape closes the list"
        sort.click()
        page.mouse.click(5, 5)
        assert page.get_by_role("listbox").count() == 0, "an outside click closes the list"
        page.goto(ui + "/p/study/a/toy-fit")
        version = page.get_by_role("button", name="Version", exact=True)
        version.wait_for()
        version.click()
        page.get_by_role("listbox", name="Version").wait_for()
        shot("23-desktop-version-select")

        # phone (390x844) and tablet (820x1180): every page, no sideways page scroll, tap targets, collapsed rail
        def responsive(tag: str, width: int, height: int) -> None:
            page.set_viewport_size({"width": width, "height": height})
            pages = [
                ("projects", "/", "h1"),
                ("project", "/p/study/", ".tabs"),
                ("project-artifacts", "/p/study/?tab=artifacts", ".table-card"),
                ("artifact", "/p/study/a/toy-fit", ".graph-node"),
                ("dataset", "/p/study/a/datasets/toy@v2", ".graph-node"),
                ("run", "/p/study/a/runs/train", ".run-chart"),
                ("report", "/p/study/r/summary/", "#report-prose h2"),
            ]
            for name, path, ready in pages:
                page.goto(ui + path)
                page.locator(ready).first.wait_for()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(300)
                sw = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
                assert sw[0] <= sw[1], f"{tag} {name}: horizontal page scroll {sw}"
                wide = page.evaluate(
                    """() => [...document.querySelectorAll('main *')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right > innerWidth + 1
                    && !e.closest('.table-wrap, .graph-scroll, .tabs, pre, .raw-html, .math, svg, table, .plotly-card'); }).map(e => e.tagName + '.' + e.className).slice(0, 5)"""
                )
                assert not wide, f"{tag} {name}: elements stick out of the window: {wide}"
                small = page.evaluate(
                    """() => [...document.querySelectorAll('.tabs button, .action-button, .select-button, input, .rail-toggle, .back-link, .image-copy, .graph-toggle button, .report-list a')]
                    .filter(e => e.offsetParent !== null || getComputedStyle(e).position === 'fixed')
                    .map(e => [e.tagName + '.' + e.className, e.getBoundingClientRect()]).filter(([, r]) => r.height > 0 && r.height < 39.5).map(([n, r]) => n + ' ' + Math.round(r.height)).slice(0, 5)"""
                )
                if width < 600:
                    assert not small, f"{tag} {name}: tap targets under 40px: {small}"
                    assert (
                        page.locator(".rail .report-list:not([hidden])").first.is_hidden()
                        or not page.locator(".rail .report-list").count()
                    ), f"{tag} {name}: rail lists start collapsed"
                    rail_h = page.locator(".rail").bounding_box()["height"]
                    assert rail_h < 70, f"{tag} {name}: collapsed rail is {rail_h}px tall"
                page.screenshot(path=str(shots / f"{tag}-{name}.png"), full_page=True)
                if name == "report":
                    back = page.locator("#back-link")
                    back.wait_for()
                    page.screenshot(path=str(shots / f"{tag}-{name}-viewport.png"))
                if name in ("project", "report") and width < 760:
                    page.locator(".rail-toggle").click()
                    assert page.locator(
                        ".rail .report-list:not([hidden])"
                    ).first.is_visible(), "Browse opens the rail lists"
                    page.screenshot(path=str(shots / f"{tag}-{name}-browse.png"))
                    page.locator(".rail-toggle").click()
                    assert page.locator(
                        ".rail .report-list:not([hidden])"
                    ).first.is_hidden(), "Browse closes them again"

        page.goto(ui + "/")
        check_palette(page, shots, "light")

        responsive("phone", 390, 844)
        responsive("tablet", 820, 1180)
        page.set_viewport_size({"width": 1280, "height": 900})
        dark = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark")
        page = dark.new_page()
        page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
        page.on(
            "response", lambda r: r.status >= 400 and r.status != 401 and problems.append(f"HTTP {r.status}: {r.url}")
        )
        page.goto(ui + "/login")
        page.get_by_label("Token").fill(token)
        page.get_by_role("button", name="Open repo").click()
        page.wait_for_url(ui + "/")
        page.goto(ui + "/p/study/a/toy-fit")
        page.locator(".graph-node").first.wait_for()
        shot("14-dark-artifact")
        page.on("console", lambda m: m.type == "error" and problems.append(f"console error (dark): {m.text}"))
        page.goto(ui + "/")
        check_palette(page, shots, "dark")
        check_touch_lightbox(browser, ui, token, shots, problems)

        # a bare name opens its @latest as the repo resolves it: with the remote away, a version pending in the
        # project's repo is the latest, though no alias of the listing names it (it has no number yet)
        store_httpd.shutdown()
        store_httpd.server_close()
        pending = (
            store.connect().artifact("datasets/toy").publish(write(base / "pending" / "notes.md", "# Pending\n").parent)
        )
        assert pending.pending, pending
        local_httpd = make_ui_server(open_backend(no_auth=True), port=0, no_auth=True)
        local_httpd.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=local_httpd.serve_forever, daemon=True).start()
        local = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        local.on("pageerror", lambda error: problems.append(f"page error (local): {error}"))
        local.on("response", lambda r: r.status >= 400 and problems.append(f"HTTP {r.status} (local): {r.url}"))
        local.goto(f"http://127.0.0.1:{local_httpd.server_address[1]}/p/study/a/datasets/toy")
        try:
            local.get_by_role("heading", name="datasets/toy").wait_for()
        except Exception:
            print("LOCAL PAGE", local.url, local.locator("body").inner_text()[:1000])
            raise
        local.locator("section.data-panel[aria-label=Reference] .data-row").first.wait_for()
        shown = local.evaluate(
            """() => Object.fromEntries([...document.querySelectorAll('section.data-panel[aria-label=Reference] .data-row')]
            .map(r => [r.querySelector('.data-name').textContent, r.querySelector('.data-version').textContent]))"""
        )
        if shown.get("Id") != pending.id:
            problems.append(f"the bare name shows {shown} as its latest, not the pending {pending.id}")
        local_httpd.shutdown()
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
