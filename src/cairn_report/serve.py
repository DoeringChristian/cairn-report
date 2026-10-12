"""Serve the overview (reports/report.md) and every research direction, with comments.

    /                               the overview (generated/reports/overview/)
    /reports/<slug>/                one research direction (generated/reports/<slug>/)
    /api/reports                    all reports, overview first (drives each sidebar)
    /api/reports/<slug>/comments    GET folded comments, POST a comment revision
              .../comments/export   GET, as a download
              .../comments/delete   POST {"id"}: append a delete event
              .../comments/clear    POST: append resolve events for all open comments

Comments are append-only: reports/knowledge/comments.jsonl for the overview
(slug "overview"), reports/<slug>/knowledge/comments.jsonl for a direction.
"""

from __future__ import annotations

import errno
import json
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import build as builder
from .config import Config, load_config

# Set by serve() from the project's Config.
REPORTS_ROOT = OUTPUT_ROOT = Path()
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
COMMENT_ROUTE = re.compile(r"/api/reports/([a-z0-9][a-z0-9-]*)/comments(?:/(export|clear|delete))?")
CLOSED = ("resolve", "delete")
OVERVIEW = "overview"
RESERVED = {OVERVIEW, "knowledge", "fragments", "media"}


def discover_reports() -> dict[str, Path]:
    """The overview first, then every research direction (same rule as build.py)."""
    if not REPORTS_ROOT.is_dir():
        return {}
    reports = {OVERVIEW: REPORTS_ROOT} if (REPORTS_ROOT / "report.md").is_file() else {}
    reports.update({
        folder.name: folder
        for folder in sorted(REPORTS_ROOT.iterdir())
        if folder.is_dir() and folder.name not in RESERVED and SLUG.fullmatch(folder.name)
        and (folder / "report.md").is_file()
    })
    return reports


def report_href(slug: str) -> str:
    return "/" if slug == OVERVIEW else f"/reports/{slug}/"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_comments(folder: Path) -> list[dict]:
    """Fold the append-only log to the latest record per comment id, in first-seen order."""
    latest, order = {}, []
    path = folder / "knowledge" / "comments.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            if line:
                row = json.loads(line)
                if row["id"] not in latest:
                    order.append(row["id"])
                latest[row["id"]] = row
    return [latest[key] for key in order]


class Handler(BaseHTTPRequestHandler):
    reports: dict[str, Path] = {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/reports":
            return self._json(200, [self._summary(slug, folder) for slug, folder in self.reports.items()])
        route = self._comment_route(path)
        if route:
            folder, action = route
            return self._json(200, current_comments(folder), download=action == "export")
        match = re.fullmatch(r"/reports/([a-z0-9][a-z0-9-]*)(/.*)?", path)
        if match and match.group(1) in self.reports and match.group(1) != OVERVIEW:
            if match.group(2) is None:
                return self._redirect(f"/reports/{match.group(1)}/")
            return self._generated(match.group(1), match.group(2))
        if OVERVIEW in self.reports and not path.startswith(("/reports/", "/api/")):
            return self._generated(OVERVIEW, path)
        self.send_error(404)

    def _generated(self, slug: str, relative: str) -> None:
        generated = (OUTPUT_ROOT / slug).resolve()
        candidate = (generated / (unquote(relative.lstrip("/")) or "index.html")).resolve()
        if candidate != generated and generated in candidate.parents and candidate.is_file():
            return self._file(candidate)
        self.send_error(404)

    def do_POST(self):
        route = self._comment_route(urlparse(self.path).path)
        if not route:
            return self.send_error(404)
        folder, action = route
        try:
            payload = self._payload(allow_empty=action == "clear")
            if action == "clear":
                rows = [
                    {"id": row["id"], "timestamp": now(), "event": "resolve", "resolution": "Cleared from the report"}
                    for row in current_comments(folder)
                    if row.get("event") not in CLOSED
                ]
                for row in rows:
                    self._append(folder, row)
                return self._json(200, {"cleared": len(rows)})
            if action == "delete":
                key = str(payload.get("id", "")).strip()
                if not any(row["id"] == key and row.get("event") not in CLOSED for row in current_comments(folder)):
                    raise ValueError("unknown comment ID")
                self._append(folder, {"id": key, "timestamp": now(), "event": "delete"})
                return self._json(200, {"deleted": key})
            text = str(payload.get("text", "")).strip()
            target = str(payload.get("target", "")).strip()
            if not text or len(text) > 4_000 or not target or len(target) > 256:
                raise ValueError("invalid comment")
            key = str(payload.get("id", "")).strip() or str(uuid.uuid4())
            if len(key) > 128:
                raise ValueError("invalid comment ID")
            row = {
                "id": key,
                "timestamp": now(),
                "event": "upsert",
                "target": target,
                "line": payload.get("line"),
                "quote": str(payload.get("quote", ""))[:1_000],
                "text": text,
            }
            self._append(folder, row)
            self._json(201, row)
        except (ValueError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})

    def _summary(self, slug: str, folder: Path) -> dict:
        data_file = OUTPUT_ROOT / slug / "report-data.json"
        data = json.loads(data_file.read_text()) if data_file.is_file() else {}
        return {
            "slug": slug,
            "title": data.get("title", slug),
            "subtitle": data.get("subtitle", ""),
            "status": data.get("status", ""),
            "updated": data.get("updated", ""),
            "summary": data.get("summary", ""),
            "links_to": data.get("links_to", []),
            "href": report_href(slug),
        }

    def _comment_route(self, path: str):
        match = COMMENT_ROUTE.fullmatch(path)
        if match and match.group(1) in self.reports:
            return self.reports[match.group(1)], match.group(2)
        return None

    def _payload(self, allow_empty: bool = False) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 16_384 or (size < 2 and not allow_empty):
            raise ValueError("invalid payload size")
        body = self.rfile.read(size) if size else b"{}"
        return json.loads(body or b"{}")

    @staticmethod
    def _append(folder: Path, row: dict) -> None:
        path = folder / "knowledge" / "comments.jsonl"
        path.parent.mkdir(exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def _file(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(301)
        self.send_header("Location", location)
        self.end_headers()

    def _json(self, status: int, value, download: bool = False) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="report-comments.json"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(config: Config | None = None, host: str = "127.0.0.1", port: int = 8000,
          strict_port: bool = False, build: bool = True) -> None:
    """Build (unless build=False) and serve; move to the next free port unless strict_port."""
    global REPORTS_ROOT, OUTPUT_ROOT
    config = config or load_config()
    REPORTS_ROOT, OUTPUT_ROOT = config.reports_dir, config.output_dir
    if build:
        builder.build_all(config)
    Handler.reports = discover_reports()
    server, chosen = None, port
    for chosen in [port] if strict_port else range(port, port + 50):
        try:
            server = ThreadingHTTPServer((host, chosen), Handler)  # bind before announcing the URL
            break
        except OSError as error:
            if strict_port or error.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise SystemExit(f"Cannot serve on {host}:{chosen}: {error.strerror}.")
    if server is None:
        raise SystemExit(f"No free port in {port}-{port + 49} on {host}; pass --port.")
    if chosen != port:
        print(f"Port {port} is in use; serving on {chosen} instead.", flush=True)
    print(f"Reports: http://{host}:{chosen}/ ({', '.join(Handler.reports)})", flush=True)
    server.serve_forever()
