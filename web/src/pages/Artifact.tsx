import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { paths, useApi, type Entry, type Manifest, type Summary, type VersionRow } from "../api";
import { CopyButton, ErrorNote, Hero, Loading, Pill, Select, Table } from "../components";
import { artifactHref, bytes, exact, fileHref, isReport, parseRef, previewKind, reportHref, selOf, shortOf, when, zipHref } from "../util";
import { Graph } from "./Graph";
import { RunPanel } from "./Run";

interface ArtifactData { project: string; name: string; summary: Summary; versions: VersionRow[]; aliases: Record<string, number> }
interface VersionData { project: string; name: string; version: Manifest; consumers: string[]; deleted?: string[] }

const TEXT_LIMIT = 64 * 1024, MEDIA_LIMIT = 50 * 1024 * 1024, SHOWN = 200;

/** A nested config as `a.b.c` -> value rows (lists of plain values stay one row). */
function flatten(value: unknown, prefix = ""): [string, string][] {
  if (value !== null && typeof value === "object" && !(Array.isArray(value) && value.every(item => item === null || typeof item !== "object"))) {
    const entries = Array.isArray(value) ? value.map((item, index) => [String(index), item] as const) : Object.entries(value as Record<string, unknown>);
    if (!entries.length) return [[prefix || "(empty)", Array.isArray(value) ? "[]" : "{}"]];
    return entries.flatMap(([key, item]) => flatten(item, prefix ? `${prefix}.${key}` : key));
  }
  return [[prefix || "(value)", typeof value === "string" ? value : JSON.stringify(value)]];
}

/** The run config (`metadata.config`, e.g. a Hydra job's) as a table, its overrides as chips, then the rest of the metadata. */
function MetadataSections({ metadata }: { metadata: Record<string, unknown> }) {
  const { config, overrides, ...rest } = metadata ?? {};
  const hasConfig = config !== undefined && config !== null && typeof config === "object";
  const others = hasConfig ? rest : { ...rest, ...(config !== undefined ? { config } : {}) };
  const list = Array.isArray(overrides) ? overrides.map(String) : [];
  if (!Array.isArray(overrides) && overrides !== undefined) (others as Record<string, unknown>).overrides = overrides;
  return (
    <>
      {hasConfig && (
        <section className="app-section">
          <div className="section-head"><h2>Config</h2></div>
          {list.length > 0 && <p className="hint">Overrides: {list.map(item => <code key={item} style={{ marginRight: 8 }}>{item}</code>)}</p>}
          <Table head={["Key", "Value"]}>
            {flatten(config).map(([key, value]) => (
              <tr key={key}><td><code>{key}</code></td><td><code>{value}</code></td></tr>
            ))}
          </Table>
        </section>
      )}
      <section className="app-section">
        <div className="section-head"><h2>Metadata</h2></div>
        <div className="report-prose table-card"><pre>{JSON.stringify(others, null, 2)}</pre></div>
      </section>
    </>
  );
}

function Preview({ project, name, version, entry }: { project: string; name: string; version: string; entry: Entry }) {
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

function Files({ project, name, version, files }: { project: string; name: string; version: string; files: Entry[] }) {
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
                {f.sha256 && kind && <button className="action-button small" aria-expanded={isOpen} onClick={() => toggle(f.path)}>{isOpen ? "Hide" : "Preview"}</button>}
                {f.sha256 && <a className="action-button small" href={fileHref(project, name, version, f.path, true)}>Download</a>}
                {f.sha256 && <CopyButton text={`${exact(project, name, version)}//${f.path}`} label="Copy file reference" done="Reference copied" />}
              </div></td>
            </tr>
          );
          return isOpen ? [row, <tr key={f.path + "#"} className="preview-row"><td colSpan={3}><Preview project={project} name={name} version={version} entry={f} /></td></tr>] : [row];
        })}
      </Table>
      {shown.length === 0 && <p className="hint">No file matches.</p>}
      {!all && shown.length > SHOWN && <button className="action-button" onClick={() => setAll(true)}>Show all {shown.length} files</button>}
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
  const last = art.data?.versions.at(-1);
  const head = art.data?.aliases.latest !== undefined ? `v${art.data.aliases.latest}` : last ? selOf(last) : undefined;
  const sel = selector ?? head ?? null;
  const ver = useApi<VersionData>(art.data && sel ? paths.version(project, name, sel) : null);
  document.title = `${name}${sel ? "@" + sel : ""} - Cairn`;
  const error = art.error ?? ver.error;

  const byVersion: Record<number, string[]> = {};
  Object.entries(art.data?.aliases ?? {}).forEach(([alias, n]) => (byVersion[n] ??= []).push(alias));
  const manifest = ver.data?.version;
  const number = manifest ? selOf(manifest) : undefined;  // v<n>, or the uid of a pending version
  const pending = !!manifest?.pending;
  const report = isReport(name);
  const aliasPills = (list: string[] = []) => list.sort().map(a => <span className="pill alias" key={a}>{a}</span>);
  const gone = manifest?.status === "deleted";
  const isRun = (manifest?.metadata as Record<string, unknown> | undefined)?.kind === "run";

  return (
    <main>
      {error && <ErrorNote error={error} />}
      {!error && (art.loading || ver.loading) && <Loading />}
      {art.data && manifest && number !== undefined && (
        <>
          <Hero
            eyebrow={<><Link to="/">Projects</Link> / <Link to={`/p/${encodeURIComponent(project)}/${isRun ? "?tab=runs" : ""}`}>{project}</Link> / {report ? "report" : isRun ? "run" : "artifact"}</>}
            title={name}
            subtitle={art.data.summary.metadata.title && report ? art.data.summary.metadata.title : undefined}
            status={manifest.status === "completed" ? number : pending ? `${shortOf(manifest)} pending` : `${number} ${manifest.status}`}
          >
            <p className="alias-line">
              {art.data.summary.latest ? <>latest <b>v{art.data.summary.latest}</b></> : "no completed version"} {aliasPills(Object.keys(art.data.aliases).filter(a => a !== "latest"))}
            </p>
            {report && manifest.status === "completed" && manifest.version != null && <p><a className="action-button open-report" href={reportHref(project, name, manifest.version)}>Open report {number}</a></p>}
          </Hero>

          {pending && (
            <div className="admonition pending-note" role="note">
              <p className="admonition-title">Pending</p>
              <p>This repo has this version, but its remote does not have it yet, so it has no number: its uid <code>{manifest.uid}</code> names it.
                It is sent automatically (the next command, a running job, or <code>cairn sync</code>; <code>cairn status</code> lists it){manifest.sync === "rejected" ? <>. The remote refused it: {manifest.error}</> : "."}</p>
            </div>
          )}

          {gone && (
            <div className="admonition tombstone-note" role="note">
              <p className="admonition-title">Deleted</p>
              <p>{number} was deleted {when(manifest.deleted_at ?? "")}{manifest.deleted_by ? ` by ${manifest.deleted_by}` : ""}{manifest.reason ? `: ${manifest.reason}` : "."} Its files are gone; its number and uid are never used again, and its lineage is kept.</p>
            </div>
          )}

          <section className="data-panel app-panel" aria-label="Reference">
            <h2>Reference</h2>
            <ul>
              <li className="data-row"><span className="data-name">Version</span><span className="data-version">{exact(project, name, number)}</span><span className="data-files" /><CopyButton text={exact(project, name, number)} label="Copy exact version" done="Version copied" /></li>
              {!gone && <li className="data-row"><span className="data-name">Pull</span><span className="data-version">cairn pull {exact(project, name, number)}</span><span className="data-files" /><CopyButton text={`cairn pull ${exact(project, name, number)}`} label="Copy pull command" done="Command copied" /></li>}
              {!gone && manifest.files.length > 0 && (
                <li className="data-row">
                  <span className="data-name">Files</span>
                  <span className="data-version"><a className="action-button small" href={zipHref(project, name, number)} download>Download zip</a></span>
                  <span className="data-files">{manifest.status === "running" ? "what is synced so far; the run is still running" : bytes(manifest.files.reduce((n, f) => n + (f.size ?? 0), 0))}</span>
                  <span />
                </li>
              )}
              {manifest.digest && <li className="data-row"><span className="data-name">Digest</span><span className="data-version mono">{manifest.digest}</span><span className="data-files" /><CopyButton text={manifest.digest} label="Copy digest" done="Digest copied" /></li>}
            </ul>
          </section>

          <div className="metric-grid app-metrics">
            <div className="metric"><span>Files</span><strong>{gone ? 0 : manifest.files.length}</strong><small>{gone ? "deleted" : bytes(manifest.files.reduce((n, f) => n + (f.size ?? 0), 0))}</small></div>
            <div className="metric"><span>Created</span><strong className="small-value">{when(manifest.created).replace(/-/g, "\u2011")}</strong><small>{manifest.source?.host ? `on ${manifest.source.host}` : " "}</small></div>
            <div className="metric"><span>Versions</span><strong>{art.data.versions.length}</strong><small>{manifest.label ? `label ${manifest.label}` : " "}</small></div>
          </div>

          {isRun && !gone && !pending && <RunPanel project={project} name={name} sel={number} status={manifest.status} />}

          <section className="app-section">
            <div className="section-head"><h2>Versions</h2>
              <Select label="Version" value={number} onChange={v => location.assign(artifactHref(project, name, v))}
                choices={[...art.data.versions].reverse().map(v => ({ value: selOf(v), label: `${shortOf(v)}${v.label ? ` (${v.label})` : ""}`, note: v.status !== "completed" ? v.status : undefined }))} />
            </div>
            <Table head={["Version", "Label", "Status", "Created", "Aliases", "Source host"]}>
              {[...art.data.versions].reverse().map(v => (
                <tr key={v.uid} className={[selOf(v) === number ? "selected" : "", v.status === "deleted" ? "tombstone" : "", v.pending ? "pending" : ""].join(" ").trim()}>
                  <td className="nowrap"><Link to={artifactHref(project, name, selOf(v))} title={v.pending ? v.uid : undefined}><strong>{shortOf(v)}</strong></Link>{report && v.status === "completed" && v.version != null && <> &middot; <a href={reportHref(project, name, v.version)}>open</a></>}</td>
                  <td>{v.label}</td>
                  <td><Pill status={v.status} />{v.status === "deleted" && <span className="muted tombstone-reason" title={`deleted ${v.deleted_at ?? ""}${v.deleted_by ? " by " + v.deleted_by : ""}`}> {v.reason || when(v.deleted_at ?? "")}</span>}</td>
                  <td className="muted nowrap">{when(v.created)}</td>
                  <td>{v.version != null ? aliasPills(byVersion[v.version]) : null}</td>
                  <td className="muted">{v.source?.host}</td>
                </tr>
              ))}
            </Table>
          </section>

          <section className="app-section">
            <div className="section-head"><h2>Lineage of {shortOf(manifest)}</h2></div>
            {!pending && ["completed", "deleted", "running", "failed"].includes(manifest.status) ? <Graph root={exact(project, name, number)} project={project} /> : <p className="hint">Lineage is shown once the remote has the version (its inputs are listed below).</p>}
          </section>

          <section className="app-section">
            <div className="section-head"><h2>Files of {shortOf(manifest)}</h2></div>
            {gone ? <p className="hint">This version was deleted: its files are gone.</p> : manifest.files.length ? <Files project={project} name={name} version={number} files={manifest.files} /> : <p className="hint">This version has no files.</p>}
          </section>

          <RefList title="Inputs" refs={manifest.inputs} project={project} empty="No inputs recorded." deleted={ver.data!.deleted} />
          <RefList title="Consumers" refs={ver.data!.consumers} project={project} empty="No consumers yet." deleted={ver.data!.deleted} />

          <MetadataSections metadata={manifest.metadata as Record<string, unknown>} />
        </>
      )}
    </main>
  );
}
