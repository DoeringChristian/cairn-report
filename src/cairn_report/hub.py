"""Hub: an artifact store for reports and runs, with push and pull, behind one token.

    <root>/
      token                                 the single access token (`cairn-report hub init`)
      blobs/<aa>/<sha256-rest>              file contents, content-addressed, written once
      projects/<name>/report.jsonl          the report: one line per pushed version (its full file list)
      projects/<name>/runs.jsonl            runs: one line per new version of a run file (append-only)

A project's report is versioned as a whole: every push that changes it appends
a complete list of its files (`source/...`: the report folder, `site/...`: the
built pages), so any version can be viewed or pulled again. Runs grow file by
file, as the logger or a push sends them; a run file's earlier versions stay
recorded. Nothing stored is ever deleted or overwritten, and the hub never
executes pushed code: reports are built where they are written.

    /                               every project
    /p/<name>/                      the latest report version; /p/<name>@<n>/ version n
    /runs/<name>/[<path>/]          the project's runs and their files
    /files/<name>/runs/<path>       one run file; /files/<name>/report/<path>[?version=n] a report file
    /api/v1/...                     push, pull, and listing API

Every route needs the token: `Authorization: Bearer <token>` for clients, or the
session cookie set by /login for browsers. Run the hub behind an HTTPS reverse
proxy; it binds 127.0.0.1 by default.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__
from .logger import read_jsonl
from .ranges import byte_range

PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
SHA256 = re.compile(r"[0-9a-f]{64}")
REPORT_TREES = ("source", "site")
COOKIE = "cairn_hub"
SESSION_LABEL = b"cairn-hub-session-v1"
MIN_TOKEN = 32
MAX_JSON = 64 * 1024 * 1024
CHUNK = 1 << 20


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_path(path, roots: tuple[str, ...] | None = None) -> bool:
    """A relative POSIX path without `.`/`..`, empty parts, or control characters (inside `roots` if given)."""
    if not isinstance(path, str) or not 0 < len(path) <= 1024 or "\\" in path:
        return False
    parts = path.split("/")
    if roots is not None and (len(parts) < 2 or parts[0] not in roots):
        return False
    return all(part not in ("", ".", "..") and not any(ord(char) < 32 for char in part) for part in parts)


def link_target(path: str, target, roots: tuple[str, ...] | None = None) -> str | None:
    """Where a relative symlink at `path` points, if it stays inside the same tree."""
    if not isinstance(target, str) or not target or target.startswith("/") or "\\" in target or len(target) > 1024:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if not valid_path(resolved, roots):
        return None
    if roots is not None and resolved.split("/")[0] != path.split("/")[0]:
        return None
    return resolved


def clean_entries(entries, roots: tuple[str, ...] | None) -> list[dict]:
    """Validated {path, sha256, size[, mtime]} or {path, link} entries."""
    if not isinstance(entries, list) or len(entries) > 200_000:
        raise ValueError("entries must be a list")
    clean = []
    for entry in entries:
        if not isinstance(entry, dict) or not valid_path(entry.get("path"), roots):
            raise ValueError(f"invalid entry path: {str(entry)[:200]}")
        if "link" in entry:
            if link_target(entry["path"], entry["link"], roots) is None:
                raise ValueError(f"{entry['path']}: link must stay inside its tree")
            clean.append({"path": entry["path"], "link": entry["link"]})
            continue
        sha, size = entry.get("sha256"), entry.get("size")
        if not isinstance(sha, str) or not SHA256.fullmatch(sha) or not isinstance(size, int) or size < 0:
            raise ValueError(f"{entry['path']}: needs sha256 and size")
        mtime = entry.get("mtime")
        clean.append({"path": entry["path"], "sha256": sha, "size": size,
                      "mtime": mtime if isinstance(mtime, (int, float)) else None})
    return clean


def read_token(path: Path) -> str:
    token = path.read_text().strip()
    if len(token) < MIN_TOKEN:
        raise SystemExit(f"{path}: the hub token must have at least {MIN_TOKEN} characters")
    return token


def init_root(root: Path) -> Path:
    """Create a hub root with a fresh random token, readable only by its owner."""
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    token_file = root / "token"
    if token_file.exists():
        raise SystemExit(f"{token_file} already exists; delete it first to rotate the token")
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)  # explicit: default ACLs may ignore the creation mode
        handle.write(secrets.token_urlsafe(32) + "\n")
    Store(root)
    return token_file


def _append_lines(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class Store:
    """Blobs, one versioned report and one run-file index per project; safe for concurrent requests."""

    def __init__(self, root: Path):
        self.root = root
        self.blobs, self.projects, self.tmp = root / "blobs", root / "projects", root / "tmp"
        for folder in (self.blobs, self.projects, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.runs_cache: dict[str, dict[str, dict]] = {}
        self.report_cache: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ blobs

    def blob_path(self, sha: str) -> Path:
        return self.blobs / sha[:2] / sha[2:]

    def has_blob(self, sha: str) -> bool:
        return self.blob_path(sha).is_file()

    def put_blob(self, sha: str, stream, size: int) -> bool:
        """Store `size` bytes from `stream` if they hash to `sha`; False if the blob already existed."""
        target = self.blob_path(sha)
        temporary = self.tmp / f"{sha}.{secrets.token_hex(6)}"
        digest, remaining = hashlib.sha256(), size
        try:
            with temporary.open("wb") as handle:
                while remaining:
                    chunk = stream.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ValueError("upload ended before Content-Length bytes arrived")
                    digest.update(chunk)
                    handle.write(chunk)
                    remaining -= len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if digest.hexdigest() != sha:
                raise ValueError("content does not match its SHA-256 (did the file change during upload?)")
            if target.is_file():
                return False
            target.parent.mkdir(exist_ok=True)
            os.replace(temporary, target)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def missing(self, entries: list[dict]) -> list[str]:
        return sorted({entry["sha256"] for entry in entries if "sha256" in entry and not self.has_blob(entry["sha256"])})

    # ---------------------------------------------------------------- projects

    def project_names(self) -> list[str]:
        return sorted(folder.name for folder in self.projects.iterdir()
                      if (folder / "report.jsonl").is_file() or (folder / "runs.jsonl").is_file())

    def known(self, name: str) -> bool:
        return bool(PROJECT_NAME.fullmatch(name)) and name in self.project_names()

    # ------------------------------------------------------------------ report

    def versions(self, name: str) -> list[dict]:
        with self.lock:
            if name not in self.report_cache:
                self.report_cache[name] = read_jsonl(self.projects / name / "report.jsonl")
            return list(self.report_cache[name])

    def report(self, name: str, version: int | None = None) -> dict | None:
        versions = self.versions(name)
        if not versions:
            return None
        if version is None:
            return versions[-1]
        return next((entry for entry in versions if entry["version"] == version), None)

    def push_report(self, name: str, entries: list) -> tuple[int | None, list[str], bool]:
        """Record a new report version. Returns (version, missing blobs, created)."""
        clean = clean_entries(entries, REPORT_TREES)
        missing = self.missing(clean)
        if missing:
            return None, missing, False
        files = sorted(clean, key=lambda entry: entry["path"])
        with self.lock:
            versions = self.report_cache.setdefault(name, read_jsonl(self.projects / name / "report.jsonl"))
            if versions and _same_files(versions[-1]["entries"], files):
                return versions[-1]["version"], [], False  # unchanged: no new version
            record = {"version": (versions[-1]["version"] + 1) if versions else 1, "time": now(), "entries": files}
            _append_lines(self.projects / name / "report.jsonl", [record])
            versions.append(record)
            return record["version"], [], True

    # -------------------------------------------------------------------- runs

    def runs(self, name: str) -> dict[str, dict]:
        """The latest entry per run path (a copy, safe to read without the lock)."""
        with self.lock:
            return dict(self._runs(name))

    def _runs(self, name: str) -> dict[str, dict]:
        if name not in self.runs_cache:
            self.runs_cache[name] = {entry["path"]: entry for entry in read_jsonl(self.projects / name / "runs.jsonl")}
        return self.runs_cache[name]

    def push_runs(self, name: str, entries: list) -> tuple[int, list[str]]:
        """Append the run entries that differ from the current index. Returns (appended, missing blobs)."""
        clean = clean_entries(entries, None)
        missing = self.missing(clean)
        if missing:
            return 0, missing
        with self.lock:
            index = self._runs(name)
            changed = [entry for entry in clean if _differs(index.get(entry["path"]), entry)]
            stamp = now()
            for entry in changed:
                entry["time"] = stamp
                index[entry["path"]] = entry
            if changed:
                _append_lines(self.projects / name / "runs.jsonl", changed)
        return len(changed), []

    def resolve(self, tree: dict[str, dict], path: str, roots: tuple[str, ...] | None = None) -> dict | None:
        """The file entry at `path`, following stored symlinks (for example `latest`) on the way."""
        for _ in range(16):
            parts = path.split("/")
            for index in range(1, len(parts) + 1):
                entry = tree.get("/".join(parts[:index]))
                if entry and "link" in entry:
                    base = link_target("/".join(parts[:index]), entry["link"], roots)
                    if base is None:
                        return None
                    path = "/".join([base, *parts[index:]])
                    break
            else:
                entry = tree.get(path)
                return entry if entry and "sha256" in entry else None
        return None

    def read_json(self, entry: dict | None):
        if not entry:
            return None
        try:
            return json.loads(self.blob_path(entry["sha256"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def _same_files(old: list[dict], new: list[dict]) -> bool:
    def strip(entries):
        return [(entry["path"], entry.get("sha256"), entry.get("link")) for entry in entries]

    return strip(old) == strip(new)


def _differs(old: dict | None, new: dict) -> bool:
    if old is None:
        return True
    if "link" in new:
        return old.get("link") != new["link"]
    return old.get("sha256") != new["sha256"]


# ---------------------------------------------------------------------- pages

PAGE_STYLE = """
:root{--paper:#f5f2e9;--ink:#202621;--muted:#697169;--line:#d9d8ce;--card:#fffef9;--accent:#176b5b;--warn:#b56231;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--paper:#141917;--ink:#e6ebe7;--muted:#9aa59e;--line:#2c3430;--card:#1b211e;--accent:#5fb8a3;--warn:#e09a6b;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;padding:40px 16px;background:var(--paper);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}
main{max-width:1040px;margin:auto}h1{font-family:Georgia,serif;font-size:40px;letter-spacing:-.02em;margin:0 0 6px}
.muted{color:var(--muted)}a{color:var(--accent)}.crumbs{font-size:13px;margin-bottom:18px}
table{width:100%;border-collapse:collapse;margin-top:22px;font-variant-numeric:tabular-nums}
th{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
.wrap{overflow-x:auto}.badge{font-size:12px;font-weight:700;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
.completed{color:var(--accent)}.failed,.interrupted,.running{color:var(--warn)}
form{display:grid;gap:12px;max-width:420px;margin-top:24px}input{font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
button{font:inherit;padding:10px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:var(--card);cursor:pointer}
"""


def page(title: str, body: str) -> bytes:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{PAGE_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    ).encode()


def size_text(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


def stamp_text(value: str) -> str:
    return html.escape(value[:19].replace("T", " ")) + " UTC" if value else ""


class HubHandler(BaseHTTPRequestHandler):
    store: Store
    token: str = ""
    session: str = ""
    secure_cookie: bool = True
    max_blob: int = 0
    server_version = f"cairn-hub/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------- auth

    def _bearer(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip().encode(), self.token.encode())

    def _cookie(self) -> bool:
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        return bool(morsel) and hmac.compare_digest(morsel.value.encode(), self.session.encode())

    # ----------------------------------------------------------------- routes

    def do_GET(self):
        url = urlparse(self.path)
        path, query = unquote(url.path), parse_qs(url.query)
        if path == "/login":
            return self._login_page(query.get("next", ["/"])[0])
        if path == "/logout":
            return self._send(303, b"", headers={"Location": "/login", "Set-Cookie": self._cookie_header("", 0)})
        if not (self._bearer() or self._cookie()):
            if path.startswith("/api/"):
                return self._json(401, {"error": "missing or invalid token"})
            return self._send(303, b"", headers={"Location": "/login?next=" + quote(url.path)})
        if path == "/":
            return self._index()
        if path == "/api/v1/projects":
            return self._json(200, [self._summary(name) for name in self.store.project_names()])
        match = re.fullmatch(r"/api/v1/projects/([^/]+)/(report|report/versions|runs)", path)
        if match:
            name, what = match.groups()
            if not self.store.known(name):
                return self._json(404, {"error": f"no project {name!r}"})
            if what == "runs":
                return self._json(200, list(self.store.runs(name).values()))
            if what == "report/versions":
                return self._json(200, [{"version": v["version"], "time": v["time"], "files": len(v["entries"])}
                                        for v in self.store.versions(name)])
            record = self.store.report(name, self._version(query))
            return self._json(200, record) if record else self._json(404, {"error": "no such report version"})
        match = re.fullmatch(r"/files/([^/]+)/(report|runs)/(.+)", path)
        if match and self.store.known(match.group(1)):
            name, kind, inside = match.groups()
            if kind == "runs":
                entry = self.store.resolve(self.store.runs(name), inside)
            else:
                record = self.store.report(name, self._version(query))
                entry = self.store.resolve({e["path"]: e for e in record["entries"]}, inside, REPORT_TREES) if record else None
            return self._blob(entry, inside, download=True) if entry else self.send_error(404)
        match = re.fullmatch(r"/runs/([^/]+)/(.*)", path)
        if match and self.store.known(match.group(1)):
            return self._runs_page(match.group(1), match.group(2).strip("/"))
        match = re.fullmatch(r"/p/([^/@]+)(?:@(\d+))?(/.*)?", path)
        if match and self.store.known(match.group(1)):
            name, version, rest = match.groups()
            if rest is None:
                return self._send(301, b"", headers={"Location": f"{path}/"})
            return self._site(name, int(version) if version else None, rest[1:])
        self.send_error(404)

    def do_PUT(self):
        match = re.fullmatch(r"/api/v1/blobs/([0-9a-f]{64})", urlparse(self.path).path)
        if not match:
            return self.send_error(404)
        if not self._bearer():
            return self._json(401, {"error": "missing or invalid token"})
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._json(411, {"error": "Content-Length required"})
        if size < 0 or size > self.max_blob:
            return self._json(413, {"error": f"blob larger than {self.max_blob} bytes"})
        try:
            created = self.store.put_blob(match.group(1), self.rfile, size)
        except ValueError as error:
            return self._json(400, {"error": str(error)})
        self._json(201 if created else 200, {"sha256": match.group(1), "created": created})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path == "/login":
            return self._login()
        if not self._bearer():
            return self._json(401, {"error": "missing or invalid token"})
        try:
            payload = self._payload(MAX_JSON)
            if path == "/api/v1/blobs/missing":
                hashes = payload.get("sha256", [])
                if not isinstance(hashes, list) or len(hashes) > 200_000 or not all(isinstance(h, str) and SHA256.fullmatch(h) for h in hashes):
                    raise ValueError("sha256 must be a list of hex digests")
                return self._json(200, {"missing": [h for h in hashes if not self.store.has_blob(h)]})
            match = re.fullmatch(r"/api/v1/projects/([^/]+)/(report|runs)", path)
            if not match:
                return self.send_error(404)
            name, kind = match.groups()
            if not PROJECT_NAME.fullmatch(name):
                raise ValueError("invalid project name")
            if kind == "report":
                version, missing, created = self.store.push_report(name, payload.get("entries"))
                if missing:
                    return self._json(409, {"error": "upload these blobs first", "missing": missing})
                return self._json(201 if created else 200, {"version": version, "created": created})
            appended, missing = self.store.push_runs(name, payload.get("entries"))
            if missing:
                return self._json(409, {"error": "upload these blobs first", "missing": missing})
            return self._json(200, {"updated": appended})
        except (ValueError, json.JSONDecodeError) as error:
            return self._json(400, {"error": str(error)})

    @staticmethod
    def _version(query: dict) -> int | None:
        value = query.get("version", [None])[0]
        return int(value) if value and value.isdigit() else None

    # ---------------------------------------------------------------- reports

    def _site(self, name: str, version: int | None, rest: str):
        record = self.store.report(name, version)
        if record is None:
            if version is None:  # runs only, no report yet
                return self._send(303, b"", headers={"Location": f"/runs/{name}/"})
            return self.send_error(404)
        files = {entry["path"]: entry for entry in record["entries"]}
        file = f"site/{rest}" if rest else "site/index.html"
        if rest and not rest.endswith("/") and f"{file}/index.html" in files:
            return self._send(301, b"", headers={"Location": urlparse(self.path).path + "/"})
        if file.endswith("/"):
            file += "index.html"
        entry = files.get(file) if valid_path(file) else None
        if not entry or "sha256" not in entry:
            return self.send_error(404)
        immutable = "/_static/" in f"/{rest}" or re.search(r"(^|&)v=[0-9a-f]+", urlparse(self.path).query) is not None
        compressed = files.get(file + ".gz")
        if compressed and "sha256" in compressed and "gzip" in self.headers.get("Accept-Encoding", ""):
            return self._blob(compressed, file, immutable=immutable, encoding="gzip")
        return self._blob(entry, file, immutable=immutable)

    # ------------------------------------------------------------ hub pages

    def _summary(self, name: str) -> dict:
        runs = self.store.runs(name)
        record = self.store.report(name)
        files = [entry for entry in runs.values() if "sha256" in entry]
        times = [entry.get("time", "") for entry in runs.values()] + ([record["time"]] if record else [])
        site = (self.store.read_json(next((e for e in record["entries"] if e["path"] == "site/site.json"), None))
                if record else None) or {}
        return {
            "name": name,
            "report": {"version": record["version"], "time": record["time"], "title": site.get("title")} if record else None,
            "runs": len({path.rsplit("/", 1)[0] for path in runs if path.endswith("/run.json")}),
            "run_files": len(files),
            "run_bytes": sum(entry["size"] for entry in files),
            "updated": max(times, default=""),
        }

    def _index(self):
        rows = []
        for summary in sorted((self._summary(name) for name in self.store.project_names()),
                              key=lambda item: item["updated"], reverse=True):
            name = html.escape(summary["name"])
            report = summary["report"]
            report_cell = (f'<a href="/p/{name}/">{html.escape(report["title"] or "report")}</a>'
                           f' <span class="muted">v{report["version"]}</span>') if report else '<span class="muted">none</span>'
            rows.append(
                f"<tr><td><strong>{name}</strong></td><td>{report_cell}</td>"
                f'<td><a href="/runs/{name}/">{summary["runs"]} runs</a> '
                f'<span class="muted">{summary["run_files"]} files · {size_text(summary["run_bytes"])}</span></td>'
                f'<td class="muted">{stamp_text(summary["updated"])}</td></tr>'
            )
        table = (
            '<div class="wrap"><table><thead><tr><th>Project</th><th>Report</th><th>Runs</th>'
            f'<th>Last push</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            if rows else '<p class="muted">Nothing pushed yet. Run <code>cairn-report push</code> in a project.</p>'
        )
        body = f'<h1>Hub</h1><p class="muted">Every pushed project, newest first. <a href="/logout">Log out</a></p>{table}'
        self._send(200, page("Hub", body), "text/html; charset=utf-8")

    def _runs_page(self, name: str, sub: str):
        runs = self.store.runs(name)
        escaped = html.escape(name)
        crumbs = f'<div class="crumbs"><a href="/">Hub</a> / <a href="/runs/{escaped}/">{escaped}</a>'
        if not sub:  # every run: <run-name>/<timestamp>/run.json
            rows = []
            for path in sorted(runs):
                match = re.fullmatch(r"([^/]+)/([^/]+)/run\.json", path)
                if not match:
                    continue
                run, stamp = match.groups()
                status = (self.store.read_json(runs[path]) or {}).get("status", "unknown")
                latest = runs.get(f"{run}/latest", {}).get("link", "").rstrip("/") == stamp
                files = [e for key, e in runs.items() if key.startswith(f"{run}/{stamp}/") and "sha256" in e]
                rows.append(
                    f'<tr><td><a href="/runs/{escaped}/{html.escape(run)}/{html.escape(stamp)}/">{html.escape(run)}</a></td>'
                    f"<td>{html.escape(stamp)}{' · <strong>latest</strong>' if latest else ''}</td>"
                    f'<td><span class="badge {html.escape(str(status))}">{html.escape(str(status))}</span></td>'
                    f"<td>{len(files)} files · {size_text(sum(e['size'] for e in files))}</td></tr>"
                )
            report = f' · <a href="/p/{escaped}/">report</a>' if self.store.report(name) else ""
            body = (f'{crumbs}</div><h1>{escaped}</h1><p class="muted">Runs{report}</p>'
                    '<div class="wrap"><table><thead><tr><th>Run</th><th>Started</th><th>Status</th><th>Files</th></tr></thead>'
                    f"<tbody>{''.join(rows) or '<tr><td colspan=4 class=muted>No runs yet.</td></tr>'}</tbody></table></div>")
            return self._send(200, page(f"{name} runs", body), "text/html; charset=utf-8")
        if not valid_path(sub):
            return self.send_error(404)
        prefix = sub + "/"
        files = sorted((path, entry) for path, entry in runs.items() if path.startswith(prefix))
        if not files:
            return self.send_error(404)
        rows = "".join(
            f'<tr><td><a href="/files/{escaped}/runs/{quote(path)}">{html.escape(path[len(prefix):])}</a></td>'
            f"<td>{size_text(entry['size']) if 'sha256' in entry else '→ ' + html.escape(entry['link'])}</td>"
            f'<td class="muted">{stamp_text(entry.get("time", ""))}</td></tr>'
            for path, entry in files
        )
        body = (f"{crumbs} / {html.escape(sub)}</div><h1>{html.escape(sub.rsplit('/', 1)[-1])}</h1>"
                '<div class="wrap"><table><thead><tr><th>File</th><th>Size</th><th>Pushed</th></tr></thead>'
                f"<tbody>{rows}</tbody></table></div>")
        self._send(200, page(sub, body), "text/html; charset=utf-8")

    def _login_page(self, target: str, failed: bool = False):
        target = target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"
        message = '<p class="failed">That token is not valid.</p>' if failed else ""
        body = (f"<h1>Hub</h1><p class=\"muted\">Enter the hub token.</p>{message}"
                f'<form method="post" action="/login"><input type="hidden" name="next" value="{html.escape(target)}">'
                '<input type="password" name="token" autocomplete="current-password" required autofocus>'
                "<button type=\"submit\">Open hub</button></form>")
        self._send(401 if failed else 200, page("Hub login", body), "text/html; charset=utf-8")

    def _login(self):
        try:
            form = parse_qs(self._body(4096).decode())
        except ValueError:
            return self.send_error(400)
        given = form.get("token", [""])[0].strip()
        target = form.get("next", ["/"])[0]
        if not hmac.compare_digest(given.encode(), self.token.encode()):
            time.sleep(1)  # slows down guessing
            return self._login_page(target, failed=True)
        target = target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"
        self._send(303, b"", headers={"Location": target, "Set-Cookie": self._cookie_header(self.session, 30 * 86400)})

    def _cookie_header(self, value: str, max_age: int) -> str:
        secure = "; Secure" if self.secure_cookie else ""
        return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}"

    # ------------------------------------------------------------------- I/O

    def _body(self, limit: int, allow_empty: bool = False) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        if size > limit or (size == 0 and not allow_empty):
            raise ValueError("invalid payload size")
        return self.rfile.read(size) if size else b""

    def _payload(self, limit: int) -> dict:
        value = json.loads(self._body(limit) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON object")
        return value

    def _blob(self, entry: dict, path: str, download: bool = False, immutable: bool = False, encoding: str | None = None):
        etag = f'"{entry["sha256"]}"'
        headers = {"ETag": etag, "X-Content-Type-Options": "nosniff", "Vary": "Accept-Encoding",
                   "Cache-Control": "private, max-age=31536000, immutable" if immutable else "no-cache"}
        if encoding:
            headers["Content-Encoding"] = encoding
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", headers=headers)
        blob = self.store.blob_path(entry["sha256"])
        kind = mimetypes.guess_type(path.rsplit("/", 1)[-1])[0] or "application/octet-stream"
        if download:
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.rsplit('/', 1)[-1])}"
        size = blob.stat().st_size
        wanted = None if encoding else byte_range(self.headers.get("Range"), size)  # videos seek with ranges
        if not encoding:
            headers["Accept-Ranges"] = "bytes"
        if wanted is False:
            return self._send(416, b"", headers=headers | {"Content-Range": f"bytes */{size}"})
        first, last = wanted or (0, size - 1)
        if wanted:
            headers["Content-Range"] = f"bytes {first}-{last}/{size}"
        self.send_response(206 if wanted else 200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(last - first + 1))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        with blob.open("rb") as handle:
            handle.seek(first)
            remaining = last - first + 1
            while remaining > 0:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send(self, status: int, body: bytes, kind: str | None = None, headers: dict | None = None) -> None:
        if status >= 400:
            self.close_connection = True  # an unread request body must not be parsed as the next request
        self.send_response(status)
        if kind:
            self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value).encode(), "application/json", {"Cache-Control": "no-store"})

    def log_message(self, format, *args):  # one line per request, without the token
        sys.stderr.write(f"{self.log_date_time_string()} {self.address_string()} {format % args}\n")


def make_hub(root: Path, host: str = "127.0.0.1", port: int = 8800, token: str | None = None,
             token_file: Path | None = None, max_blob: int = 50 * 1024**3, secure_cookie: bool = True) -> ThreadingHTTPServer:
    """A bound hub server (not yet serving)."""
    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f"{root} does not exist; `cairn-report hub serve {root}` creates it")
    token = token or os.environ.get("CAIRN_HUB_TOKEN") or read_token(token_file or root / "token")
    if len(token) < MIN_TOKEN:
        raise SystemExit(f"the hub token must have at least {MIN_TOKEN} characters")
    handler = type("Handler", (HubHandler,), {
        "store": Store(root),
        "token": token,
        "session": hmac.new(token.encode(), SESSION_LABEL, hashlib.sha256).hexdigest(),
        "secure_cookie": secure_cookie,
        "max_blob": max_blob,
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve_hub(root: Path, host: str = "127.0.0.1", port: int = 8800, token_file: Path | None = None,
              max_blob: int = 50 * 1024**3, secure_cookie: bool = True) -> None:
    """Serve a hub; a new root (no token yet, none given) is initialized first. Prints the token, which
    is the login for browsers and clients, to the terminal only: never into a URL."""
    created = None
    if token_file is None and not os.environ.get("CAIRN_HUB_TOKEN") and not (root / "token").exists():
        created = init_root(root)
    server = make_hub(root, host, port, token_file=token_file, max_blob=max_blob, secure_cookie=secure_cookie)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: the hub serves plain HTTP; expose it only through an HTTPS reverse proxy or a tunnel", flush=True)
    url = f"http://{host}:{server.server_address[1]}/"
    if created:
        print(f"Created hub {root.resolve()} with a new token ({created}, owner-only)", flush=True)
    print(f"Hub: {url} (data in {root.resolve()})", flush=True)
    print(f"Token: {server.RequestHandlerClass.token}", flush=True)
    print("  log in with it in the browser; clients read it from CAIRN_HUB_TOKEN or a token_file", flush=True)
    server.serve_forever()
