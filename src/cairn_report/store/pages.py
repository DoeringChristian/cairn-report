"""Server-rendered HTML pages of the store server (no scripts, no frameworks)."""

from __future__ import annotations

import html
import json
from urllib.parse import quote

from . import refs
from .local import LocalStore, StoreError
from .tokens import Principal

PAGE_STYLE = """
:root{--paper:#f5f2e9;--ink:#202621;--muted:#697169;--line:#d9d8ce;--card:#fffef9;--accent:#176b5b;--warn:#b56231;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--paper:#141917;--ink:#e6ebe7;--muted:#9aa59e;--line:#2c3430;--card:#1b211e;--accent:#5fb8a3;--warn:#e09a6b;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;padding:40px 16px;background:var(--paper);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}
main{max-width:1040px;margin:auto}h1{font-family:Georgia,serif;font-size:40px;letter-spacing:-.02em;margin:0 0 6px;overflow-wrap:anywhere}
h2{font-family:Georgia,serif;font-size:24px;margin:34px 0 0}
.muted{color:var(--muted)}a{color:var(--accent)}.crumbs{font-size:13px;margin-bottom:18px}
table{width:100%;border-collapse:collapse;margin-top:14px;font-variant-numeric:tabular-nums}
th{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
.wrap{overflow-x:auto}.badge{font-size:12px;font-weight:700;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
.completed{color:var(--accent)}.failed,.interrupted,.running{color:var(--warn)}
tr.selected td{background:var(--card)}pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto;font-size:13px}
code,.mono{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
form{display:grid;gap:12px;max-width:420px;margin-top:24px}input{font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
button{font:inherit;padding:10px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:var(--card);cursor:pointer}
"""

MAX_FILES_SHOWN = 500


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


def stamp_text(value: str | None) -> str:
    return html.escape(value[:19].replace("T", " ")) + " UTC" if value else ""


def e(text) -> str:
    return html.escape(str(text))


def count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def project_href(project: str) -> str:
    return f"/p/{quote(project)}/"


def artifact_href(project: str, name: str, version: int | str | None = None) -> str:
    return f"/p/{quote(project)}/a/{quote(name)}" + (f"@v{version}" if isinstance(version, int) else f"@{version}" if version else "")


def report_href(project: str, name: str, version: int | None = None) -> str:
    slug = name.split("/", 1)[1]
    return f"/p/{quote(project)}/r/{quote(slug)}" + (f"@v{version}" if version else "") + "/"


def file_href(project: str, name: str, version: int, path: str) -> str:
    return f"/files/{quote(project)}/{quote(name)}@v{version}//{quote(path)}"


def is_report(name: str) -> bool:
    return name.startswith("reports/") and name.count("/") == 1


def ref_link(principal: Principal, text: str) -> str:
    """An exact reference as a link to its artifact page (plain text if this token cannot read that project)."""
    try:
        ref = refs.parse(text)
    except ValueError:
        return f"<code>{e(text)}</code>"
    label = f"<code>{e(text)}</code>"
    if ref.project and principal.can(ref.project):
        return f'<a href="{artifact_href(ref.project, ref.name, ref.version or ref.selector)}">{label}</a>'
    return label


def crumbs(*parts: tuple[str, str | None]) -> str:
    items = [f'<a href="{href}">{e(label)}</a>' if href else e(label) for label, href in parts]
    return f'<div class="crumbs">{" / ".join(items)}</div>'


def latest_version(store: LocalStore, project: str, name: str) -> dict | None:
    number = store.aliases(project, name).get("latest")
    return store.get_version(project, name, number) if number else None


def overview(store: LocalStore, project: dict) -> dict:
    """A project record with its report and artifact counts and its last update."""
    name = project["name"]
    names = store.artifacts(name)
    summaries = [store.summary(name, item) for item in names]
    return {**project, "reports": sum(is_report(item) for item in names),
            "artifacts": sum(not is_report(item) for item in names),
            "updated": max((item["updated"] for item in summaries), default=project["created"])}


def index_page(store: LocalStore, principal: Principal) -> bytes:
    rows = []
    projects = [overview(store, item) for item in store.projects() if principal.can(item["name"])]
    for item in sorted(projects, key=lambda item: item["updated"], reverse=True):
        description = f'<br><span class="muted">{e(item["description"])}</span>' if item.get("description") else ""
        rows.append(f'<tr><td><a href="{project_href(item["name"])}"><strong>{e(item["title"])}</strong></a>'
                    f'<span class="muted"> {e(item["name"])}</span>{description}</td>'
                    f'<td>{item["reports"]}</td><td>{item["artifacts"]}</td><td class="muted">{stamp_text(item["updated"])}</td></tr>')
    table = ('<div class="wrap"><table><thead><tr><th>Project</th><th>Reports</th><th>Artifacts</th><th>Last update</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else
             '<p class="muted">No projects yet. Run <code>cairn-report publish</code> in a project with <code>[store] url</code> set.</p>')
    body = f'<h1>Store</h1><p class="muted">Projects, most recently updated first. <a href="/logout">Log out</a></p>{table}'
    return page("Store", body)


def project_page(store: LocalStore, project: str) -> bytes:
    record = store.get_project(project)
    reports, others = [], []
    for name in store.artifacts(project):
        summary = store.summary(project, name)
        (reports if is_report(name) else others).append((name, summary))
    report_rows = []
    for name, summary in sorted(reports, key=lambda item: item[1]["updated"], reverse=True):
        head = latest_version(store, project, name)
        meta = head["metadata"] if head else {}
        title = meta.get("title") or name.split("/", 1)[1]
        link = (f'<a href="{report_href(project, name)}"><strong>{e(title)}</strong></a>' if head
                else f"<strong>{e(title)}</strong>")
        status = f'<span class="badge">{e(meta["status"])}</span>' if meta.get("status") else ""
        report_rows.append(
            f'<tr><td>{link}<br><span class="muted">{e(meta.get("summary", ""))}</span></td><td>{status}</td>'
            f'<td><a href="{artifact_href(project, name)}">{"v" + str(summary["latest"]) if summary["latest"] else "none"}</a></td>'
            f'<td class="muted">{stamp_text(summary["updated"])}</td></tr>')
    other_rows = []
    for name, summary in sorted(others, key=lambda item: item[0]):
        names = ", ".join(e(alias) for alias in sorted(summary["aliases"]) if alias != "latest")
        other_rows.append(
            f'<tr><td><a href="{artifact_href(project, name)}">{e(name)}</a></td>'
            f'<td>{"v" + str(summary["latest"]) if summary["latest"] else "none"}</td><td>{names}</td>'
            f'<td class="muted">{stamp_text(summary["updated"])}</td></tr>')
    reports_table = ('<div class="wrap"><table><thead><tr><th>Report</th><th>Status</th><th>Version</th><th>Updated</th></tr></thead>'
                     f'<tbody>{"".join(report_rows)}</tbody></table></div>' if report_rows else '<p class="muted">No reports yet.</p>')
    other_table = ('<div class="wrap"><table><thead><tr><th>Artifact</th><th>Latest</th><th>Aliases</th><th>Updated</th></tr></thead>'
                   f'<tbody>{"".join(other_rows)}</tbody></table></div>' if other_rows else '<p class="muted">No other artifacts yet.</p>')
    description = f'<p>{e(record["description"])}</p>' if record.get("description") else ""
    body = (f'{crumbs(("Store", "/"), (project, None))}<h1>{e(record["title"])}</h1>{description}'
            f'<h2>Reports</h2>{reports_table}<h2>Artifacts</h2>{other_table}')
    return page(record["title"], body)


def artifact_page(store: LocalStore, principal: Principal, project: str, name: str, selected: dict | None) -> bytes:
    """Versions, the selected version's files, metadata, inputs, and consumers: the back-trace view."""
    versions = store.versions(project, name)
    if not versions:
        raise StoreError(f"no artifact {project}:{name}")
    aliases = store.aliases(project, name)
    by_version: dict[int, list[str]] = {}
    for alias, number in aliases.items():
        by_version.setdefault(number, []).append(alias)
    head = aliases.get("latest")
    chosen = selected or next((version for version in versions if version["version"] == head), versions[-1])
    rows = []
    for version in reversed(versions):
        number = version["version"]
        source = (version.get("source") or {}).get("host", "")
        names = ", ".join(e(alias) for alias in sorted(by_version.get(number, [])))
        opened = (f' · <a href="{report_href(project, name, number)}">open</a>'
                  if is_report(name) and version["status"] == "completed" else "")
        rows.append(
            f'<tr{" class=selected" if number == chosen["version"] else ""}><td><a href="{artifact_href(project, name, number)}">v{number}</a>{opened}</td>'
            f'<td>{e(version.get("label") or "")}</td><td><span class="badge {e(version["status"])}">{e(version["status"])}</span></td>'
            f'<td class="muted">{stamp_text(version["created"])}</td><td>{names}</td><td class="muted">{e(source)}</td></tr>')
    number = chosen["version"]
    files = chosen["files"]
    file_rows = "".join(
        f'<tr><td><a href="{file_href(project, name, number, entry["path"])}">{e(entry["path"])}</a></td>'
        f'<td>{size_text(entry["size"]) if "sha256" in entry else "&rarr; " + e(entry["link"])}</td></tr>'
        for entry in files[:MAX_FILES_SHOWN])
    more = f'<p class="muted">{len(files) - MAX_FILES_SHOWN} more not shown.</p>' if len(files) > MAX_FILES_SHOWN else ""
    total = size_text(sum(entry.get("size", 0) for entry in files))
    inputs = "".join(f"<li>{ref_link(principal, item)}</li>" for item in chosen["inputs"]) or '<li class="muted">none</li>'
    consumers: list[str] = []
    if chosen["status"] == "completed":
        consumers = store.lineage(refs.exact(project, name, number))["consumers"]
        consumers = [item for item in consumers if principal.can(refs.parse(item).project)]
    used_by = "".join(f"<li>{ref_link(principal, item)}</li>" for item in consumers) or '<li class="muted">none</li>'
    metadata = json.dumps(chosen["metadata"], indent=2, sort_keys=True)
    report = (f' · <a href="{report_href(project, name, number)}">open report</a>'
              if is_report(name) and chosen["status"] == "completed" else "")
    body = (f'{crumbs(("Store", "/"), (project, project_href(project)), (name, None))}<h1>{e(name)}</h1>'
            f'<p class="muted">{count(len(versions), "version")}{report}</p>'
            '<div class="wrap"><table><thead><tr><th>Version</th><th>Label</th><th>Status</th><th>Created</th><th>Aliases</th>'
            f'<th>Source host</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<h2>v{number}: files</h2><p class="muted">{count(len(files), "file")}, {total}</p>'
            f'<div class="wrap"><table><thead><tr><th>File</th><th>Size</th></tr></thead><tbody>{file_rows}</tbody></table></div>{more}'
            f'<h2>Metadata</h2><pre>{e(metadata)}</pre>'
            f'<h2>Inputs</h2><ul>{inputs}</ul><h2>Consumers</h2><ul>{used_by}</ul>')
    return page(f"{name}@v{number}", body)


def message_page(title: str, text: str) -> bytes:
    return page(title, f'{crumbs(("Store", "/"))}<h1>{e(title)}</h1><p class="muted">{e(text)}</p>')


def login_page(target: str, failed: bool = False) -> bytes:
    message = '<p class="failed">That token is not valid.</p>' if failed else ""
    body = (f'<h1>Store</h1><p class="muted">Enter a store token.</p>{message}'
            f'<form method="post" action="/login"><input type="hidden" name="next" value="{e(target)}">'
            '<input type="password" name="token" autocomplete="current-password" required autofocus>'
            '<button type="submit">Open store</button></form>')
    return page("Store login", body)
