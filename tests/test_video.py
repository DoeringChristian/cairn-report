import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from conftest import make_data
from cairn.reports import serve as server
import cairn as store
from cairn.reports.build import build_all, mp4_size
from cairn.config import load_config
from cairn.runs.logger import RunLogger
from cairn.ranges import byte_range

pytest.importorskip("imageio_ffmpeg")


def write_video(run_dir, name="rollout", size=(48, 63)):
    frames = np.linspace(0, 1, 12)[:, None, None] * np.ones((12, *size))
    log = RunLogger(run_dir)
    path = log.video(name, frames, fps=12)
    log.close()
    return path


def test_logged_video_is_browser_ready_mp4(tmp_path):
    path = write_video(tmp_path)
    assert path.suffix == ".mp4" and path.stat().st_size > 0
    assert mp4_size(path) == (64, 48)  # padded to even sizes for yuv420p
    assert json.loads((tmp_path / "artifacts.jsonl").read_text().splitlines()[-1])["kind"] == "video"


def publish_video(project):
    """A new version of the `descent` input that also holds a video; returns the video file."""
    run = make_data(project.parent / "with-video")
    path = write_video(run)
    store.publish("descent", run)
    return path


def test_video_becomes_a_figure_card(project):
    publish_video(project)
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text() + "\n![Rollout](input:descent/videos/rollout.mp4 \"Twelve frames.\")\n")
    build_all(load_config(project))
    narrative = (project / "generated" / "reports" / "alpha" / "narrative.html").read_text()
    assert 'data-figure-type="video"' in narrative and "<video data-src=" in narrative
    assert 'data-fps="12"' in narrative
    assert 'width="64" height="48"' in narrative  # the card's shape is known before the video loads


@pytest.mark.parametrize("header, expected", [
    ("bytes=0-99", (0, 99)), ("bytes=100-", (100, 999)), ("bytes=-10", (990, 999)),
    ("bytes=500-5000", (500, 999)), ("bytes=1000-", False), ("bytes=0-1,5-9", None), (None, None)])
def test_byte_ranges(header, expected):
    assert byte_range(header, 1000) == expected


def test_served_video_answers_range_requests(project, monkeypatch):
    path = publish_video(project)
    alpha = project / "reports" / "alpha" / "report.md"
    alpha.write_text(alpha.read_text() + "\n![Rollout](input:descent/videos/rollout.mp4)\n")
    build_all(load_config(project))
    monkeypatch.setattr(server, "OUTPUT_ROOT", project / "generated" / "reports")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        copied = next((project / "generated" / "reports").rglob("*.mp4")).relative_to(project / "generated" / "reports")
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/{copied.as_posix()}",
                                         headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(request) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == f"bytes 10-19/{path.stat().st_size}"
            assert response.read() == path.read_bytes()[10:20]
    finally:
        httpd.shutdown()
