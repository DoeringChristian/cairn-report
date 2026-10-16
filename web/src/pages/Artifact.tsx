import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { paths, useApi, type Entry, type Manifest, type Summary, type VersionRow } from "../api";
import {
  AliasPills, Button, CodeChips, CopyButton, Crumbs, DataTable, ErrorNote, FigureCard, FigureGrid, Hero, HeroLine, Hint, JsonBlock,
  KeyValueTable, LinkButton, Loading, MetricCard, MetricGrid, Note, Panel, RefRow, RefRows, RowActions, SearchInput, Section, Select,
  StatusPill, TextBlock, Thumbnail, ThumbnailSpacer, Toolbar, useLightbox, type GalleryItem,
} from "../components";
import { artifactHref, bytes, exact, fileHref, userRef, isReport, parseRef, previewKind, reportHref, selOf, shortOf, when, zipHref } from "../util";
import { Graph } from "./Graph";
import { RunPanel } from "./Run";

interface ArtifactData { project: string; name: string; summary: Summary; versions: VersionRow[]; aliases: Record<string, number> }
interface VersionData { project: string; name: string; version: Manifest; consumers: string[]; deleted?: string[] }

const TEXT_LIMIT = 64 * 1024, MEDIA_LIMIT = 50 * 1024 * 1024, THUMB_LIMIT = 5 * 1024 * 1024, SHOWN = 200, FIGURES = 6;

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
        <Section title="Config">
          {list.length > 0 && <Hint>Overrides: <CodeChips items={list} /></Hint>}
          <KeyValueTable head={["Key", "Value"]} code rows={flatten(config)} />
        </Section>
      )}
      <Section title="Metadata"><JsonBlock value={others} /></Section>
    </>
  );
}

/** A file's inline preview: an image (a click opens the lightbox), a video, or the start of a text file. */
function Preview({ url, entry, onOpen }: { url: string; entry: Entry; onOpen?: () => void }) {
  const kind = previewKind(entry.path);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  const size = entry.size ?? 0;
  useEffect(() => {
    if (kind !== "text") return;
    let live = true;
    fetch(url, { headers: { Range: `bytes=0-${TEXT_LIMIT - 1}` } }).then(r => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`)))).then(body => {
      if (size <= TEXT_LIMIT && entry.path.endsWith(".json")) { try { body = JSON.stringify(JSON.parse(body), null, 2); } catch { /* keep raw */ } }
      if (live) setText(body);
    }, e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [url, kind, size, entry.path]);
  if (kind === "image") {
    if (size > MEDIA_LIMIT) return <Hint>Too large to preview. {onOpen && <Button small onClick={onOpen}>Open in the viewer</Button>}</Hint>;
    return (
      <button type="button" className="preview-open" onClick={onOpen} aria-label={`Open ${entry.path} in the viewer`} title="Open in the viewer">
        <img className="preview-media" src={url} alt={entry.path} loading="lazy" />
      </button>
    );
  }
  if (kind === "video") return size > MEDIA_LIMIT ? <Hint>Too large to preview.</Hint> : <video className="preview-media" src={url} controls preload="metadata" muted />;
  if (error) return <Hint>Preview failed: {error}</Hint>;
  if (text === null) return <Loading />;
  return <><TextBlock>{text}</TextBlock>{size > TEXT_LIMIT && <Hint>Showing the first {bytes(TEXT_LIMIT)} of {bytes(size)}.</Hint>}</>;
}

/** A version's files: figure cards of its first images, then the list (filter, thumbnails, preview, download, copy).
 *  All its images, in list order, are one lightbox gallery. */
function Files({ project, name, version, files }: { project: string; name: string; version: string; files: Entry[] }) {
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [all, setAll] = useState(false);
  const href = (path: string, download = false) => fileHref(project, name, version, path, download);
  const isImage = (f: Entry) => !!f.sha256 && previewKind(f.path) === "image";
  const images = useMemo(() => files.filter(isImage), [files]);  // eslint-disable-line react-hooks/exhaustive-deps
  const gallery = useMemo<GalleryItem[]>(() => images.map(f => ({
    src: href(f.path), title: f.path, caption: bytes(f.size ?? 0), download: href(f.path, true),
    copy: { text: `${userRef(name, version)}//${f.path}`, label: "Copy file reference", done: "Reference copied" },
  })), [images, project, name, version]);  // eslint-disable-line react-hooks/exhaustive-deps
  const position = useMemo(() => new Map(images.map((f, i) => [f.path, i])), [images]);
  const { open: show, lightbox } = useLightbox(gallery);
  const shown = files.filter(f => f.path.toLowerCase().includes(filter.toLowerCase()));
  const rows = all ? shown : shown.slice(0, SHOWN);
  const toggle = (path: string) => setOpen(prev => { const next = new Set(prev); next.has(path) ? next.delete(path) : next.add(path); return next; });
  const figures = images.filter(f => (f.size ?? 0) <= THUMB_LIMIT).slice(0, FIGURES);
  return (
    <>
      {figures.length > 0 && (
        <FigureGrid>
          {figures.map(f => <FigureCard key={f.path} src={href(f.path)} href={href(f.path)} title={f.path} onOpen={() => show(position.get(f.path)!)} />)}
        </FigureGrid>
      )}
      {files.length > 12 && <Toolbar><SearchInput label="Filter files" value={filter} onChange={setFilter} /></Toolbar>}
      <DataTable head={["File", "Size", ""]}>
        {rows.flatMap(f => {
          const kind = previewKind(f.path), isOpen = open.has(f.path), at = position.get(f.path);
          const row = (
            <tr key={f.path}>
              <td className="mono wrap-any">
                <span className={images.length ? "file-name" : undefined}>
                  {images.length > 0 && (at !== undefined
                    ? <Thumbnail src={(f.size ?? 0) <= THUMB_LIMIT ? href(f.path) : undefined} href={href(f.path)} label={`Open ${f.path} in the viewer`} onOpen={() => show(at)} />
                    : <ThumbnailSpacer />)}
                  {f.sha256 ? <a href={href(f.path)}>{f.path}</a> : <span>{f.path} <span className="muted">&rarr; {f.link}</span></span>}
                </span>
              </td>
              <td className="muted nowrap">{f.sha256 ? bytes(f.size ?? 0) : "link"}</td>
              <td><RowActions>
                {f.sha256 && kind && <Button small aria-expanded={isOpen} onClick={() => toggle(f.path)}>{isOpen ? "Hide" : "Preview"}</Button>}
                {f.sha256 && <LinkButton small href={href(f.path, true)}>Download</LinkButton>}
                {f.sha256 && <CopyButton text={`${userRef(name, version)}//${f.path}`} label="Copy file reference" done="Reference copied" />}
              </RowActions></td>
            </tr>
          );
          return isOpen ? [row, <tr key={f.path + "#"} className="preview-row"><td colSpan={3}><Preview url={href(f.path)} entry={f} onOpen={at !== undefined ? () => show(at) : undefined} /></td></tr>] : [row];
        })}
      </DataTable>
      {shown.length === 0 && <Hint>No file matches.</Hint>}
      {!all && shown.length > SHOWN && <Button onClick={() => setAll(true)}>Show all {shown.length} files</Button>}
      {lightbox}
    </>
  );
}

function RefList({ title, refs, project, empty, deleted = [] }: { title: string; refs: string[]; project: string; empty: string; deleted?: string[] }) {
  return (
    <Panel title={title}>
      {refs.length === 0 ? <Hint>{empty}</Hint> : (
        <RefRows>
          {refs.map(ref => {
            const p = parseRef(ref);
            return (
              <RefRow key={ref} name={p?.name ?? ref}
                value={p ? <Link className="data-version" to={artifactHref(p.project, p.name, p.sel)}>{p.sel}</Link> : ref}
                extra={<>{p && p.project !== project ? p.project : ""}{deleted.includes(ref) && <> <StatusPill status="deleted" /></>}</>}
                copy={{ text: p ? userRef(p.name, p.sel) : ref, label: "Copy exact version", done: "Version copied" }} />
            );
          })}
        </RefRows>
      )}
    </Panel>
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
  const gone = manifest?.status === "deleted";
  const isRun = (manifest?.metadata as Record<string, unknown> | undefined)?.kind === "run";
  const size = manifest ? bytes(manifest.files.reduce((n, f) => n + (f.size ?? 0), 0)) : "";

  return (
    <main>
      {error && <ErrorNote error={error} />}
      {!error && (art.loading || ver.loading) && <Loading />}
      {art.data && manifest && number !== undefined && (
        <>
          <Hero
            eyebrow={<Crumbs items={[{ label: "Projects", to: "/" }, { label: project, to: `/p/${encodeURIComponent(project)}/${isRun ? "?tab=runs" : ""}` }, { label: report ? "report" : isRun ? "run" : "artifact" }]} />}
            title={name}
            subtitle={art.data.summary.metadata.title && report ? art.data.summary.metadata.title : undefined}
            status={manifest.status === "completed" ? number : pending ? `${shortOf(manifest)} pending` : `${number} ${manifest.status}`}
          >
            <HeroLine>
              {art.data.summary.latest ? <>latest <b>v{art.data.summary.latest}</b></> : "no completed version"} <AliasPills aliases={Object.keys(art.data.aliases).filter(a => a !== "latest")} />
            </HeroLine>
            {report && manifest.status === "completed" && manifest.version != null && <p><LinkButton primary className="open-report" href={reportHref(project, name, manifest.version)}>Open report {number}</LinkButton></p>}
          </Hero>

          {pending && (
            <Note tone="pending" title="Pending">
              <p>This repo has this version, but its remote does not have it yet, so it has no number: its uid <code>{manifest.uid}</code> names it.
                It is sent automatically (the next command, a running job, or <code>cairn sync</code>; <code>cairn status</code> lists it){manifest.sync === "rejected" ? <>. The remote refused it: {manifest.error}</> : "."}</p>
            </Note>
          )}

          {gone && (
            <Note tone="deleted" title="Deleted">
              <p>{number} was deleted {when(manifest.deleted_at ?? "")}{manifest.deleted_by ? ` by ${manifest.deleted_by}` : ""}{manifest.reason ? `: ${manifest.reason}` : "."} Its files are gone; its number and uid are never used again, and its lineage is kept.</p>
            </Note>
          )}

          <Panel title="Reference">
            <RefRows>
              <RefRow name="Version" value={userRef(name, number)} copy={{ text: userRef(name, number), label: "Copy exact version", done: "Version copied" }} />
              {manifest.short_id && <RefRow name="Id" value={manifest.short_id} mono copy={{ text: manifest.short_id, label: "Copy short id", done: "Id copied" }} />}
              {!gone && <RefRow name="Pull" value={`cairn pull ${userRef(name, number)}`} copy={{ text: `cairn pull ${userRef(name, number)}`, label: "Copy pull command", done: "Command copied" }} />}
              {!gone && manifest.files.length > 0 && (
                <RefRow name="Files" value={<LinkButton small href={zipHref(project, name, number)} download>Download zip</LinkButton>}
                  extra={manifest.status === "running" ? "what is synced so far; the run is still running" : size} />
              )}
              {manifest.digest && <RefRow name="Digest" value={manifest.digest} mono copy={{ text: manifest.digest, label: "Copy digest", done: "Digest copied" }} />}
            </RefRows>
          </Panel>

          <MetricGrid>
            <MetricCard label="Files" value={gone ? 0 : manifest.files.length} note={gone ? "deleted" : size} />
            <MetricCard label="Created" compact value={when(manifest.created).replace(/-/g, "\u2011")} note={manifest.source?.host ? `on ${manifest.source.host}` : undefined} />
            <MetricCard label="Versions" value={art.data.versions.length} note={manifest.label ? `label ${manifest.label}` : undefined} />
          </MetricGrid>

          {isRun && !gone && !pending && <RunPanel project={project} name={name} sel={number} status={manifest.status} />}

          <Section title="Versions" actions={
            <Select label="Version" value={number} onChange={v => location.assign(artifactHref(project, name, v))}
              choices={[...art.data.versions].reverse().map(v => ({ value: selOf(v), label: `${shortOf(v)}${v.label ? ` (${v.label})` : ""}`, note: v.status !== "completed" ? v.status : undefined }))} />
          }>
            <DataTable head={["Version", "Id", "Label", "Status", "Created", "Aliases", "Source host"]}>
              {[...art.data.versions].reverse().map(v => (
                <tr key={v.uid} className={[selOf(v) === number ? "selected" : "", v.status === "deleted" ? "tombstone" : "", v.pending ? "pending" : ""].join(" ").trim() || undefined}>
                  <td className="nowrap"><Link to={artifactHref(project, name, selOf(v))} title={v.pending ? v.uid : undefined}><strong>{shortOf(v)}</strong></Link>{report && v.status === "completed" && v.version != null && <> &middot; <a href={reportHref(project, name, v.version)}>open</a></>}</td>
                  <td><code className="muted">{v.short_id}</code></td>
                  <td>{v.label}</td>
                  <td><StatusPill status={v.status} />{v.status === "deleted" && <span className="muted tombstone-reason" title={`deleted ${v.deleted_at ?? ""}${v.deleted_by ? " by " + v.deleted_by : ""}`}> {v.reason || when(v.deleted_at ?? "")}</span>}</td>
                  <td className="muted nowrap">{when(v.created)}</td>
                  <td>{v.version != null && <AliasPills aliases={byVersion[v.version]} />}</td>
                  <td className="muted">{v.source?.host}</td>
                </tr>
              ))}
            </DataTable>
          </Section>

          <Section title={`Lineage of ${shortOf(manifest)}`}>
            {!pending && ["completed", "deleted", "running", "failed"].includes(manifest.status) ? <Graph root={exact(project, name, number)} project={project} /> : <Hint>Lineage is shown once the remote has the version (its inputs are listed below).</Hint>}
          </Section>

          <Section title={`Files of ${shortOf(manifest)}`}>
            {gone ? <Hint>This version was deleted: its files are gone.</Hint> : manifest.files.length ? <Files project={project} name={name} version={number} files={manifest.files} /> : <Hint>This version has no files.</Hint>}
          </Section>

          <RefList title="Inputs" refs={manifest.inputs} project={project} empty="No inputs recorded." deleted={ver.data!.deleted} />
          <RefList title="Consumers" refs={ver.data!.consumers} project={project} empty="No consumers yet." deleted={ver.data!.deleted} />

          <MetadataSections metadata={manifest.metadata as Record<string, unknown>} />
        </>
      )}
    </main>
  );
}
