"""Hub: one server that keeps many projects' runs and reports, pushed by `cairn-report push`.

    <root>/
      token                         the single access token (created by `cairn-report hub init`)
      blobs/<aa>/<sha256-rest>      file contents, content-addressed, written once, never changed
      projects/<name>/files.jsonl   append-only index: one line per new version of a path
      projects/<name>/comments/<slug>.jsonl   comments written on the hub (append-only)

A pushed project has three trees: `outputs/` (Hydra runs), `reports/` (report
sources and comment logs), and `site/` (the built reports). The hub never
deletes or overwrites a stored version: a changed file is a new index line, and
a file deleted on the client stays on the hub. Reports are built by the client;
the hub only serves their static output and never executes pushed code.

    /                               every project
    /p/<name>/                      the project's overview report; /p/<name>/reports/<slug>/ a direction
    /runs/<name>/[<path>/]          the project's runs and their files
    /files/<name>/<path>            one stored file (latest version)
    /api/v1/...                     push, index, and comment API (bearer token)

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

from . import __version__, comments

PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
TREES = ("outputs", "reports", "site")
OVERVIEW = "overview"
COOKIE = "cairn_hub"
SESSION_LABEL = b"cairn-hub-session-v1"
MIN_TOKEN = 32
MAX_JSON = 64 * 1024 * 1024
CHUNK = 1 << 20
COMMENT_ROUTE = re.compile(r"api/reports/([a-z0-9][a-z0-9-]*)/comments(?:/(export|clear|delete))?")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_path(path) -> bool:
    """A relative POSIX path inside one of the pushed trees, without `.`/`..` or control characters."""
    if not isinstance(path, str) or not 0 < len(path) <= 1024 or "\\" in path:
        return False
    parts = path.split("/")
    return (
        len(parts) > 1
        and parts[0] in TREES
        and all(part not in ("", ".", "..") and not any(ord(char) < 32 for char in part) for part in parts)
    )


def link_target(path: str, target) -> str | None:
    """Where a relative symlink at `path` points, if it stays inside the same tree."""
    if not isinstance(target, str) or not target or target.startswith("/") or "\\" in target or len(target) > 1024:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return resolved if valid_path(resolved) and resolved.split("/")[0] == path.split("/")[0] else None


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
        handle.write(secrets.token_urlsafe(32) + "\n")
    Store(root)
    return token_file


class Store:
    """Blobs plus one append-only file index per project; safe for concurrent requests."""

    def __init__(self, root: Path):
        self.root = root
        self.blobs, self.projects, self.tmp = root / "blobs", root / "projects", root / "tmp"
        for folder in (self.blobs, self.projects, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.trees: dict[str, dict[str, dict]] = {}

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

    # ------------------------------------------------------------- the index

    def project_names(self) -> list[str]:
        return sorted(folder.name for folder in self.projects.iterdir() if (folder / "files.jsonl").is_file())

    def tree(self, name: str) -> dict[str, dict]:
        """The latest entry per path (a shallow copy, safe to read without the lock)."""
        with self.lock:
            return dict(self._tree(name))

    def _tree(self, name: str) -> dict[str, dict]:
        if name not in self.trees:
            index = self.projects / name / "files.jsonl"
            self.trees[name] = {entry["path"]: entry for entry in comments.read_log(index)}
        return self.trees[name]

    def update(self, name: str, entries: list) -> tuple[int, list[str]]:
        """Append the entries that differ from the current index. Returns (appended, missing blobs)."""
        clean = []
        for entry in entries:
            if not isinstance(entry, dict) or not valid_path(entry.get("path")):
                raise ValueError(f"invalid entry path: {str(entry)[:200]}")
            if "link" in entry:
                if link_target(entry["path"], entry["link"]) is None:
                    raise ValueError(f"{entry['path']}: link must stay inside its tree")
                clean.append({"path": entry["path"], "link": entry["link"]})
            else:
                sha, size = entry.get("sha256"), entry.get("size")
                if not isinstance(sha, str) or not SHA256.fullmatch(sha) or not isinstance(size, int) or size < 0:
                    raise ValueError(f"{entry['path']}: needs sha256 and size")
                mtime = entry.get("mtime")
                clean.append({"path": entry["path"], "sha256": sha, "size": size,
                              "mtime": mtime if isinstance(mtime, (int, float)) else None})
        missing = sorted({entry["sha256"] for entry in clean if "sha256" in entry and not self.has_blob(entry["sha256"])})
        if missing:
            return 0, missing
        with self.lock:
            tree = self._tree(name)
            changed = [entry for entry in clean if self._differs(tree.get(entry["path"]), entry)]
            if changed:
                folder = self.projects / name
                folder.mkdir(exist_ok=True)
                stamp = now()
                with (folder / "files.jsonl").open("a", encoding="utf-8") as handle:
                    for entry in changed:
                        entry["time"] = stamp
                        handle.write(json.dumps(entry) + "\n")
                        tree[entry["path"]] = entry
                    handle.flush()
                    os.fsync(handle.fileno())
        return len(changed), []

    @staticmethod
    def _differs(old: dict | None, new: dict) -> bool:
        if old is None:
            return True
        if "link" in new:
            return old.get("link") != new["link"]
        return old.get("sha256") != new["sha256"]

    def resolve(self, name: str, path: str, tree: dict | None = None) -> dict | None:
        """The file entry at `path`, following stored symlinks (for example `latest`) on the way."""
        tree = tree if tree is not None else self.tree(name)
        for _ in range(16):
            parts = path.split("/")
            for index in range(1, len(parts) + 1):
                entry = tree.get("/".join(parts[:index]))
                if entry and "link" in entry:
                    base = link_target("/".join(parts[:index]), entry["link"])
                    if base is None:
                        return None
                    path = "/".join([base, *parts[index:]])
                    break
            else:
                entry = tree.get(path)
                return entry if entry and "sha256" in entry else None
        return None

    def comment_log(self, name: str, slug: str) -> Path:
        return self.projects / name / "comments" / f"{slug}.jsonl"

    def read_json(self, entry: dict | None):
        if not entry:
            return None
        try:
            return json.loads(self.blob_path(entry["sha256"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def read_lines(self, entry: dict | None) -> list[dict]:
        return comments.read_log(self.blob_path(entry["sha256"])) if entry else []


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


def size_text(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


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

    def _same_origin(self) -> bool:
        """Cookie-authenticated writes must be JSON from this origin (with SameSite=Strict, against CSRF)."""
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc == self.headers.get("Host")

    # ----------------------------------------------------------------- routes

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        if path == "/login":
            return self._login_page(parse_qs(url.query).get("next", ["/"])[0])
        if path == "/logout":
            return self._send(303, b"", headers={"Location": "/login", "Set-Cookie": self._cookie_header("", 0)})
        if not (self._bearer() or self._cookie()):
            if path.startswith("/api/") or "/api/" in path:
                return self._json(401, {"error": "missing or invalid token"})
            return self._send(303, b"", headers={"Location": "/login?next=" + quote(url.path)})
        if path == "/":
            return self._index()
        if path == "/api/v1/projects":
            return self._json(200, [self._project_summary(name) for name in self.store.project_names()])
        match = re.fullmatch(r"/api/v1/projects/([^/]+)/tree", path)
        if match and PROJECT_NAME.fullmatch(match.group(1)):
            if match.group(1) not in self.store.project_names():
                return self._json(404, {"error": f"no project {match.group(1)!r}"})
            return self._json(200, list(self.store.tree(match.group(1)).values()))
        match = re.fullmatch(r"/files/([^/]+)/(.+)", path)
        if match and self._known(match.group(1)):
            entry = self.store.resolve(match.group(1), match.group(2))
            return self._blob(entry, match.group(2), download=True) if entry else self.send_error(404)
        match = re.fullmatch(r"/runs/([^/]+)/(.*)", path)
        if match and self._known(match.group(1)):
            return self._runs(match.group(1), match.group(2).strip("/"))
        match = re.fullmatch(r"/p/([^/]+)(/.*)?", path)
        if match and self._known(match.group(1)):
            if match.group(2) is None:
                return self._send(301, b"", headers={"Location": f"/p/{match.group(1)}/"})
            return self._site(match.group(1), match.group(2)[1:])
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
            self.close_connection = True
            return self._json(413, {"error": f"blob larger than {self.max_blob} bytes"})
        try:
            created = self.store.put_blob(match.group(1), self.rfile, size)
        except ValueError as error:
            self.close_connection = True
            return self._json(400, {"error": str(error)})
        self._json(201 if created else 200, {"sha256": match.group(1), "created": created})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path == "/login":
            return self._login()
        match = re.fullmatch(r"/api/v1/projects/([^/]+)/(missing|tree)", path)
        if match:
            if not self._bearer():
                return self._json(401, {"error": "missing or invalid token"})
            name = match.group(1)
            if not PROJECT_NAME.fullmatch(name):
                return self._json(400, {"error": "invalid project name"})
            try:
                payload = self._payload(MAX_JSON)
                if match.group(2) == "missing":
                    hashes = payload.get("sha256", [])
                    if not isinstance(hashes, list) or len(hashes) > 200_000 or not all(isinstance(h, str) and SHA256.fullmatch(h) for h in hashes):
                        raise ValueError("sha256 must be a list of hex digests")
                    return self._json(200, {"missing": [h for h in hashes if not self.store.has_blob(h)]})
                entries = payload.get("entries", [])
                if not isinstance(entries, list) or len(entries) > 200_000:
                    raise ValueError("entries must be a list")
                appended, missing = self.store.update(name, entries)
                if missing:
                    return self._json(409, {"error": "upload these blobs first", "missing": missing})
                return self._json(200, {"updated": appended})
            except (ValueError, json.JSONDecodeError) as error:
                return self._json(400, {"error": str(error)})
        match = re.fullmatch(r"/p/([^/]+)/(.+)", path)
        if match and self._known(match.group(1)) and COMMENT_ROUTE.fullmatch(match.group(2)):
            if not (self._bearer() or (self._cookie() and self._same_origin())):
                return self._json(401, {"error": "missing or invalid token"})
            return self._comment_post(match.group(1), *COMMENT_ROUTE.fullmatch(match.group(2)).groups())
        self.send_error(404)

    # ---------------------------------------------------------------- reports

    def _site(self, name: str, rest: str):
        tree = self.store.tree(name)
        if rest == "api/reports":
            return self._json(200, self._report_list(name, tree))
        match = COMMENT_ROUTE.fullmatch(rest)
        if match:
            slug, action = match.groups()
            if f"site/{slug}/report-data.json" not in tree:
                return self.send_error(404)
            return self._json(200, comments.fold(self._comment_records(name, slug, tree)), download=action == "export")
        match = re.fullmatch(r"reports/([a-z0-9][a-z0-9-]*)(/.*)?", rest)
        if match and match.group(1) != OVERVIEW:
            if match.group(2) is None:
                return self._send(301, b"", headers={"Location": f"/p/{name}/reports/{match.group(1)}/"})
            file = f"site/{match.group(1)}/{match.group(2)[1:] or 'index.html'}"
        else:
            file = f"site/{OVERVIEW}/{rest or 'index.html'}"
        entry = tree.get(file) if valid_path(file) else None
        if not entry or "sha256" not in entry:
            if not rest and not any(path.startswith("site/") for path in tree):
                return self._send(303, b"", headers={"Location": f"/runs/{name}/"})  # runs only, no reports yet
            return self.send_error(404)
        return self._blob(entry, file)

    def _report_list(self, name: str, tree: dict) -> list[dict]:
        slugs = sorted(
            path.split("/")[1] for path in tree
            if re.fullmatch(r"site/[a-z0-9][a-z0-9-]*/report-data\.json", path)
        )
        ordered = ([OVERVIEW] if OVERVIEW in slugs else []) + [slug for slug in slugs if slug != OVERVIEW]
        reports = []
        for slug in ordered:
            data = self.store.read_json(tree[f"site/{slug}/report-data.json"]) or {}
            reports.append({
                "slug": slug,
                "title": data.get("title", slug),
                "subtitle": data.get("subtitle", ""),
                "status": data.get("status", ""),
                "updated": data.get("updated", ""),
                "summary": data.get("summary", ""),
                "links_to": data.get("links_to", []),
                "href": "" if slug == OVERVIEW else f"reports/{slug}/",
            })
        return reports

    def _comment_records(self, name: str, slug: str, tree: dict) -> list[dict]:
        """The project's pushed comment log merged with comments written on the hub."""
        pushed = "reports/knowledge/comments.jsonl" if slug == OVERVIEW else f"reports/{slug}/knowledge/comments.jsonl"
        return comments.merge(self.store.read_lines(tree.get(pushed)), comments.read_log(self.store.comment_log(name, slug)))

    def _comment_post(self, name: str, slug: str, action: str | None):
        tree = self.store.tree(name)
        if f"site/{slug}/report-data.json" not in tree or action == "export":
            return self.send_error(404)
        try:
            payload = self._payload(comments.MAX_PAYLOAD, allow_empty=action == "clear")
            with self.store.lock:  # one writer at a time per hub keeps the fold consistent
                rows, status, body = comments.apply(self._comment_records(name, slug, tree), action, payload)
                comments.append(self.store.comment_log(name, slug), rows)
            self._json(status, body)
        except (ValueError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})

    # ------------------------------------------------------------ hub pages

    def _known(self, name: str) -> bool:
        return bool(PROJECT_NAME.fullmatch(name)) and (self.store.projects / name / "files.jsonl").is_file()

    def _project_summary(self, name: str) -> dict:
        tree = self.store.tree(name)
        files = [entry for entry in tree.values() if "sha256" in entry]
        runs = {path.rsplit("/", 1)[0] for path in tree if path.startswith("outputs/") and path.endswith("/run.json")}
        return {
            "name": name,
            "files": len(files),
            "bytes": sum(entry["size"] for entry in files),
            "runs": len(runs),
            "reports": f"site/{OVERVIEW}/index.html" in tree or any(path.endswith("/index.html") and path.startswith("site/") for path in tree),
            "updated": max((entry.get("time", "") for entry in tree.values()), default=""),
        }

    def _index(self):
        rows = []
        for summary in sorted((self._project_summary(name) for name in self.store.project_names()),
                              key=lambda item: item["updated"], reverse=True):
            name = html.escape(summary["name"])
            report = f'<a href="/p/{name}/">reports</a>' if summary["reports"] else '<span class="muted">no reports yet</span>'
            rows.append(
                f"<tr><td><strong>{name}</strong></td><td>{report}</td>"
                f'<td><a href="/runs/{name}/">{summary["runs"]} runs</a></td>'
                f'<td>{summary["files"]} files · {size_text(summary["bytes"])}</td>'
                f'<td class="muted">{html.escape(summary["updated"][:19].replace("T", " "))} UTC</td></tr>'
            )
        table = (
            '<div class="wrap"><table><thead><tr><th>Project</th><th>Reports</th><th>Runs</th><th>Files</th>'
            f'<th>Last push</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            if rows else '<p class="muted">No project has pushed yet. Run <code>cairn-report push</code> in a project.</p>'
        )
        body = f'<h1>Research hub</h1><p class="muted">Every pushed project, newest first. <a href="/logout">Log out</a></p>{table}'
        self._send(200, page("Research hub", body), "text/html; charset=utf-8")

    def _runs(self, name: str, sub: str):
        tree = self.store.tree(name)
        escaped = html.escape(name)
        crumbs = f'<div class="crumbs"><a href="/">Hub</a> / <a href="/runs/{escaped}/">{escaped}</a>'
        if not sub:  # every run: outputs/<run-name>/<timestamp>/run.json
            runs = []
            for path in sorted(tree, reverse=True):
                match = re.fullmatch(r"outputs/([^/]+)/([^/]+)/run\.json", path)
                if match:
                    status = (self.store.read_json(tree[path]) or {}).get("status", "unknown")
                    latest = tree.get(f"outputs/{match.group(1)}/latest", {}).get("link", "").rstrip("/") == match.group(2)
                    files = [entry for key, entry in tree.items() if key.startswith(f"outputs/{match.group(1)}/{match.group(2)}/") and "sha256" in entry]
                    runs.append((match.group(1), match.group(2), status, latest, len(files), sum(e["size"] for e in files)))
            runs.sort(key=lambda run: (run[0], run[1]), reverse=False)
            rows = "".join(
                f'<tr><td><a href="/runs/{escaped}/outputs/{html.escape(run)}/{html.escape(stamp)}/">{html.escape(run)}</a></td>'
                f"<td>{html.escape(stamp)}{' · <strong>latest</strong>' if latest else ''}</td>"
                f'<td><span class="badge {html.escape(status)}">{html.escape(status)}</span></td>'
                f"<td>{count} files · {size_text(size)}</td></tr>"
                for run, stamp, status, latest, count, size in runs
            )
            report = f' · <a href="/p/{escaped}/">reports</a>' if any(path.startswith("site/") for path in tree) else ""
            body = (f"{crumbs}</div><h1>{escaped}</h1><p class=\"muted\">Runs pushed from the project's <code>outputs/</code>{report}</p>"
                    '<div class="wrap"><table><thead><tr><th>Run</th><th>Started</th><th>Status</th><th>Files</th></tr></thead>'
                    f"<tbody>{rows or '<tr><td colspan=4 class=muted>No runs yet.</td></tr>'}</tbody></table></div>")
            return self._send(200, page(f"{name} runs", body), "text/html; charset=utf-8")
        if not valid_path(sub):
            return self.send_error(404)
        prefix = sub + "/"
        files = sorted((path, entry) for path, entry in tree.items() if path.startswith(prefix))
        if not files:
            return self.send_error(404)
        rows = "".join(
            f'<tr><td><a href="/files/{escaped}/{quote(path)}">{html.escape(path[len(prefix):])}</a></td>'
            f"<td>{size_text(entry['size']) if 'sha256' in entry else '→ ' + html.escape(entry['link'])}</td>"
            f"<td class=\"muted\">{html.escape(entry.get('time', '')[:19].replace('T', ' '))}</td></tr>"
            for path, entry in files
        )
        body = (f"{crumbs} / {html.escape(sub)}</div><h1>{html.escape(sub.rsplit('/', 1)[-1])}</h1>"
                '<div class="wrap"><table><thead><tr><th>File</th><th>Size</th><th>Pushed</th></tr></thead>'
                f"<tbody>{rows}</tbody></table></div>")
        self._send(200, page(sub, body), "text/html; charset=utf-8")

    def _login_page(self, target: str, failed: bool = False):
        target = target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"
        message = '<p class="failed">That token is not valid.</p>' if failed else ""
        body = (f"<h1>Research hub</h1><p class=\"muted\">Enter the hub token.</p>{message}"
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

    def _payload(self, limit: int, allow_empty: bool = False) -> dict:
        value = json.loads(self._body(limit, allow_empty) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON object")
        return value

    def _blob(self, entry: dict, path: str, download: bool = False):
        etag = f'"{entry["sha256"]}"'
        headers = {"ETag": etag, "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", headers=headers)
        blob = self.store.blob_path(entry["sha256"])
        kind = mimetypes.guess_type(path.rsplit("/", 1)[-1])[0] or "application/octet-stream"
        if download:
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.rsplit('/', 1)[-1])}"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(blob.stat().st_size))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        with blob.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, CHUNK)

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

    def _json(self, status: int, value, download: bool = False) -> None:
        headers = {"Cache-Control": "no-store"}
        if download:
            headers["Content-Disposition"] = 'attachment; filename="report-comments.json"'
        self._send(status, json.dumps(value).encode(), "application/json", headers)

    def log_message(self, format, *args):  # one line per request, without the token
        sys.stderr.write(f"{self.log_date_time_string()} {self.address_string()} {format % args}\n")


def make_hub(root: Path, host: str = "127.0.0.1", port: int = 8800, token: str | None = None,
             token_file: Path | None = None, max_blob: int = 50 * 1024**3, secure_cookie: bool = True) -> ThreadingHTTPServer:
    """A bound hub server (not yet serving)."""
    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f"{root} does not exist; create it with `cairn-report hub init {root}`")
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
    server = make_hub(root, host, port, token_file=token_file, max_blob=max_blob, secure_cookie=secure_cookie)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: the hub serves plain HTTP; expose it only through an HTTPS reverse proxy or a tunnel", flush=True)
    print(f"Hub: http://{host}:{server.server_address[1]}/ (data in {root.resolve()})", flush=True)
    server.serve_forever()
