import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { paths, useApi, type Entry, type Manifest, type Summary, type VersionRow } from "../api";
import { CopyButton, ErrorNote, Hero, Loading, Pill, Table } from "../components";
import { artifactHref, bytes, exact, fileHref, isReport, parseRef, previewKind, reportHref, when } from "../util";
import { Graph } from "./Graph";

interface ArtifactData { project: string; name: string; summary: Summary; versions: VersionRow[]; aliases: Record<string, number> }
interface VersionData { project: string; name: string; version: Manifest; consumers: string[]; deleted?: string[] }

const TEXT_LIMIT = 64 * 1024, MEDIA_LIMIT = 50 * 1024 * 1024, SHOWN = 200;

function Preview({ project, name, version, entry }: { project: string; name: string; version: number; entry: Entry }) {
  const kind = previewKind(entry.path), url = fileHref(project, name, version, entry.path);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  const size = entry.size ?? 0;
  useEffect(() => {
    if (kind !== "text") return;
    fetch(url, { headers: { Range: `bytes=0-${TEXT_LIMIT - 1}` } }).then(r => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`)))).then(body => {
      if (size <= TEXT_LIMIT && entry.path.endsWith(".json")) { try { body = JSON.stringify(JSON.parse(body), null, 2); } catch { /* keep raw */ } }
      setText(body);
    }, e => setError(e.message));
  }, [url, kind, size, entry.path]);
  if (kind === "image") return size > MEDIA_LIMIT ? <p className="hint">Too large to preview.</p> : <img className="preview-media" src={url} alt={entry.path} loading="lazy" />;
  if (kind === "video") return size > MEDIA_LIMIT ? <p className="hint">Too large to preview.</p> : <video className="preview-media" src={url} controls preload="metadata" muted />;
  if (error) return <p className="hint">Preview failed: {error}</p>;
  if (text === null) return <Loading />;
  return <><pre className="preview-text">{text}</pre>{size > TEXT_LIMIT && <p className="hint">Showing the first {bytes(TEXT_LIMIT)} of {bytes(size)}.</p>}</>;
}

function Files({ project, name, version, files }: { project: string; name: string; version: number; files: Entry[] }) {
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [all, setAll] = useState(false);
  const shown = files.filter(f => f.path.toLowerCase().includes(filter.toLowerCase()));
  const rows = all ? shown : shown.slice(0, SHOWN);
  const toggle = (path: string) => setOpen(prev => { const next = new Set(prev); next.has(path) ? next.delete(path) : next.add(path); return next; });
  const images = files.filter(f => previewKind(f.path) === "image" && (f.size ?? 0) <= 5 * 1024 * 1024).slice(0, 6);
  return (
    <>
      {images.length > 0 && (
        <div className="figure-grid app-figures">
          {images.map(f => (
            <a className="figure-card" key={f.path} href={fileHref(project, name, version, f.path)} target="_blank" rel="noreferrer">
              <img src={fileHref(project, name, version, f.path)} alt={f.path} loading="lazy" />
              <div className="figure-copy"><p className="mono">{f.path}</p></div>
            </a>
          ))}
        </div>
      )}
      {files.length > 12 && <div className="toolbar"><input type="search" placeholder="Filter files" aria-label="Filter files" value={filter} onChange={e => setFilter(e.target.value)} /></div>}
      <Table head={["File", "Size", ""]}>
        {rows.flatMap(f => {
          const kind = previewKind(f.path), isOpen = open.has(f.path);
          const row = (
            <tr key={f.path}>
              <td className="mono wrap-any">{f.sha256 ? <a href={fileHref(project, name, version, f.path)}>{f.path}</a> : <>{f.path} <span className="muted">&rarr; {f.link}</span></>}</td>
              <td className="muted nowrap">{f.sha256 ? bytes(f.size ?? 0) : "link"}</td>
              <td><div className="row-actions">
                {f.sha256 && kind && <button className="comment-trigger small" aria-expanded={isOpen} onClick={() => toggle(f.path)}>{isOpen ? "Hide" : "Preview"}</button>}
                {f.sha256 && <a className="comment-trigger small" href={fileHref(project, name, version, f.path, true)}>Download</a>}
                {f.sha256 && <CopyButton text={`${exact(project, name, version)}//${f.path}`} label="Copy file reference" done="Reference copied" />}
              </div></td>
            </tr>
          );
          return isOpen ? [row, <tr key={f.path + "#"} className="preview-row"><td colSpan={3}><Preview project={project} name={name} version={version} entry={f} /></td></tr>] : [row];
        })}
      </Table>
      {shown.length === 0 && <p className="hint">No file matches.</p>}
      {!all && shown.length > SHOWN && <button className="comment-trigger" onClick={() => setAll(true)}>Show all {shown.length} files</button>}
    </>
  );
}

function RefList({ title, refs, project, empty, deleted = [] }: { title: string; refs: string[]; project: string; empty: string; deleted?: string[] }) {
  return (
    <section className="data-panel app-panel" aria-label={title}>
      <h2>{title}</h2>
      {refs.length === 0 ? <p className="hint">{empty}</p> : (
        <ul>
          {refs.map(ref => {
            const p = parseRef(ref);
            return (
              <li className="data-row" key={ref}>
                <span className="data-name">{p?.name ?? ref}</span>
                {p ? <Link className="data-version" to={artifactHref(p.project, p.name, p.sel)}>{p.sel}</Link> : <span className="data-version">{ref}</span>}
                <span className="data-files">{p && p.project !== project ? p.project : ""}{deleted.includes(ref) && <> <Pill status="deleted" /></>}</span>
                <CopyButton text={ref} label="Copy exact version" done="Version copied" />
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export function ArtifactPage({ project, name, selector }: { project: string; name: string; selector: string | null }) {
  const art = useApi<ArtifactData>(paths.artifact(project, name));
  const head = art.data?.aliases.latest ?? art.data?.versions.at(-1)?.version;
  const sel = selector ?? (head !== undefined ? `v${head}` : null);
  const ver = useApi<VersionData>(art.data && sel ? paths.version(project, name, sel) : null);
  document.title = `${name}${sel ? "@" + sel : ""} - Cairn`;
  const error = art.error ?? ver.error;

  const byVersion: Record<number, string[]> = {};
  Object.entries(art.data?.aliases ?? {}).forEach(([alias, n]) => (byVersion[n] ??= []).push(alias));
  const manifest = ver.data?.version;
  const number = manifest?.version;
  const report = isReport(name);
  const aliasPills = (list: string[] = []) => list.sort().map(a => <span className="pill alias" key={a}>{a}</span>);
  const gone = manifest?.status === "deleted";

  return (
    <main>
      {error && <ErrorNote error={error} />}
      {!error && (art.loading || ver.loading) && <Loading />}
      {art.data && manifest && number !== undefined && (
        <>
          <Hero
            eyebrow={<><Link to="/">Projects</Link> / <Link to={`/p/${encodeURIComponent(project)}/`}>{project}</Link> / {report ? "report" : "artifact"}</>}
            title={name}
            subtitle={art.data.summary.metadata.title && report ? art.data.summary.metadata.title : undefined}
            status={manifest.status === "completed" ? `v${number}` : `v${number} ${manifest.status}`}
          >
            <p className="alias-line">
              {art.data.summary.latest ? <>latest <b>v{art.data.summary.latest}</b></> : "no completed version"} {aliasPills(Object.keys(art.data.aliases).filter(a => a !== "latest"))}
            </p>
            {report && manifest.status === "completed" && <p><a className="comment-trigger open-report" href={reportHref(project, name, number)}>Open report v{number}</a></p>}
          </Hero>

          {gone && (
            <div className="admonition tombstone-note" role="note">
              <p className="admonition-title">Deleted</p>
              <p>v{number} was deleted {when(manifest.deleted_at ?? "")}{manifest.deleted_by ? ` by ${manifest.deleted_by}` : ""}{manifest.reason ? `: ${manifest.reason}` : "."} Its files are gone; its number and uid are never used again, and its lineage is kept.</p>
            </div>
          )}

          <section className="data-panel app-panel" aria-label="Reference">
            <h2>Reference</h2>
            <ul>
              <li className="data-row"><span className="data-name">Version</span><span className="data-version">{exact(project, name, number)}</span><span className="data-files" /><CopyButton text={exact(project, name, number)} label="Copy exact version" done="Version copied" /></li>
              {!gone && <li className="data-row"><span className="data-name">Pull</span><span className="data-version">cairn pull {exact(project, name, number)}</span><span className="data-files" /><CopyButton text={`cairn pull ${exact(project, name, number)}`} label="Copy pull command" done="Command copied" /></li>}
              {manifest.digest && <li className="data-row"><span className="data-name">Digest</span><span className="data-version mono">{manifest.digest}</span><span className="data-files" /><CopyButton text={manifest.digest} label="Copy digest" done="Digest copied" /></li>}
            </ul>
          </section>

          <div className="metric-grid app-metrics">
            <div className="metric"><span>Files</span><strong>{gone ? 0 : manifest.files.length}</strong><small>{gone ? "deleted" : bytes(manifest.files.reduce((n, f) => n + (f.size ?? 0), 0))}</small></div>
            <div className="metric"><span>Created</span><strong className="small-value">{when(manifest.created)}</strong><small>{manifest.source?.host ? `on ${manifest.source.host}` : " "}</small></div>
            <div className="metric"><span>Versions</span><strong>{art.data.versions.length}</strong><small>{manifest.label ? `label ${manifest.label}` : " "}</small></div>
          </div>

          <section className="app-section">
            <div className="section-head"><h2>Versions</h2>
              <select aria-label="Version" value={number} onChange={e => { location.assign(artifactHref(project, name, Number(e.target.value))); }}>
                {[...art.data.versions].reverse().map(v => <option key={v.version} value={v.version}>v{v.version}{v.label ? ` (${v.label})` : ""} {v.status !== "completed" ? v.status : ""}</option>)}
              </select>
            </div>
            <Table head={["Version", "Label", "Status", "Created", "Aliases", "Source host"]}>
              {[...art.data.versions].reverse().map(v => (
                <tr key={v.version} className={[v.version === number ? "selected" : "", v.status === "deleted" ? "tombstone" : ""].join(" ").trim()}>
                  <td className="nowrap"><Link to={artifactHref(project, name, v.version)}><strong>v{v.version}</strong></Link>{report && v.status === "completed" && <> &middot; <a href={reportHref(project, name, v.version)}>open</a></>}</td>
                  <td>{v.label}</td>
                  <td><Pill status={v.status} />{v.status === "deleted" && <span className="muted tombstone-reason" title={`deleted ${v.deleted_at ?? ""}${v.deleted_by ? " by " + v.deleted_by : ""}`}> {v.reason || when(v.deleted_at ?? "")}</span>}</td>
                  <td className="muted nowrap">{when(v.created)}</td>
                  <td>{aliasPills(byVersion[v.version])}</td>
                  <td className="muted">{v.source?.host}</td>
                </tr>
              ))}
            </Table>
          </section>

          <section className="app-section">
            <div className="section-head"><h2>Lineage of v{number}</h2></div>
            {manifest.status === "completed" || gone ? <Graph root={exact(project, name, number)} project={project} /> : <p className="hint">Lineage is available once the version has completed.</p>}
          </section>

          <section className="app-section">
            <div className="section-head"><h2>Files of v{number}</h2></div>
            {gone ? <p className="hint">This version was deleted: its files are gone.</p> : manifest.files.length ? <Files project={project} name={name} version={number} files={manifest.files} /> : <p className="hint">This version has no files.</p>}
          </section>

          <RefList title="Inputs" refs={manifest.inputs} project={project} empty="No inputs recorded." deleted={ver.data!.deleted} />
          <RefList title="Consumers" refs={ver.data!.consumers} project={project} empty="No consumers yet." deleted={ver.data!.deleted} />

          <section className="app-section">
            <div className="section-head"><h2>Metadata</h2></div>
            <div className="report-prose table-card"><pre>{JSON.stringify(manifest.metadata, null, 2)}</pre></div>
          </section>
        </>
      )}
    </main>
  );
}
