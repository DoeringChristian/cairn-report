import { Link, useParams, useSearchParams } from "react-router-dom";
import { paths, useApi, type Project, type Summary } from "../api";
import { ErrorNote, Hero, Loading, Table } from "../components";
import { artifactHref, isReport, reportHref, when } from "../util";

const matches = (a: Summary, query: string) =>
  !query || `${a.name} ${JSON.stringify(a.metadata)} ${Object.keys(a.aliases).join(" ")}`.toLowerCase().includes(query.toLowerCase());

export function ProjectPage() {
  const project = useParams().project!;
  const { data, error, loading } = useApi<{ project: Project; artifacts: Summary[] }>(paths.project(project));
  const [params, setParams] = useSearchParams();
  const query = params.get("q") ?? "", tab = params.get("tab") === "artifacts" ? "artifacts" : "reports", sort = params.get("sort") ?? "updated";
  const set = (key: string, value: string) => setParams(prev => { const next = new URLSearchParams(prev); value ? next.set(key, value) : next.delete(key); return next; }, { replace: true });
  document.title = `${data?.project.title ?? project} - Cairn`;

  const order = (a: Summary, b: Summary) => (sort === "name" ? a.name.localeCompare(b.name) : b.updated.localeCompare(a.updated));
  const all = data?.artifacts ?? [];
  const reports = all.filter(a => isReport(a.name) && matches(a, query)).sort(order);
  const others = all.filter(a => !isReport(a.name) && matches(a, query)).sort(order);
  const reportCount = all.filter(a => isReport(a.name)).length;

  return (
    <main>
      {error && <ErrorNote error={error} />}
      {loading && <Loading />}
      {data && (
        <>
          <Hero eyebrow={<><Link to="/">Projects</Link> / {project}</>} title={data.project.title || project} subtitle={data.project.description} status={`${all.length} artifact${all.length === 1 ? "" : "s"}`} />
          <div className="toolbar">
            <input type="search" placeholder="Search names, titles, status, aliases" aria-label="Search" value={query} onChange={e => set("q", e.target.value)} />
            <select aria-label="Sort" value={sort} onChange={e => set("sort", e.target.value === "updated" ? "" : e.target.value)}>
              <option value="updated">Sort: updated</option><option value="name">Sort: name</option>
            </select>
          </div>
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "reports"} onClick={() => set("tab", "")}>Reports <span>{reports.length}</span></button>
            <button role="tab" aria-selected={tab === "artifacts"} onClick={() => set("tab", "artifacts")}>Artifacts <span>{others.length}</span></button>
          </div>
          {tab === "reports" && (reports.length === 0
            ? <p className="hint">{reportCount ? "No report matches." : "No reports yet."}</p>
            : (
              <Table head={["Report", "Status", "Latest", "Updated"]}>
                {reports.map(a => {
                  const title = a.metadata.title || a.name.split("/")[1];
                  return (
                    <tr key={a.name}>
                      <td>
                        {a.latest ? <a href={reportHref(project, a.name)}><strong>{title}</strong></a> : <strong>{title}</strong>}
                        {a.metadata.summary && <><br /><span className="muted">{a.metadata.summary}</span></>}
                      </td>
                      <td>{a.metadata.status && <span className="pill">{a.metadata.status}</span>}</td>
                      <td className="nowrap">{a.latest ? <Link to={artifactHref(project, a.name, a.latest)}>v{a.latest}</Link> : "none"}<br /><span className="muted">{a.versions} version{a.versions === 1 ? "" : "s"}</span></td>
                      <td className="muted nowrap">{when(a.updated)}{a.latest && <><br /><a href={reportHref(project, a.name)}>Open report</a></>}</td>
                    </tr>
                  );
                })}
              </Table>
            ))}
          {tab === "artifacts" && (others.length === 0
            ? <p className="hint">{all.length > reportCount ? "No artifact matches." : "No other artifacts yet."}</p>
            : (
              <Table head={["Artifact", "Latest", "Aliases", "Versions", "Updated"]}>
                {others.map(a => (
                  <tr key={a.name}>
                    <td><Link to={artifactHref(project, a.name)}><strong>{a.name}</strong></Link></td>
                    <td>{a.latest ? `v${a.latest}` : "none"}</td>
                    <td>{Object.keys(a.aliases).filter(x => x !== "latest").sort().map(x => <span className="pill alias" key={x}>{x}</span>)}</td>
                    <td>{a.versions}</td>
                    <td className="muted nowrap">{when(a.updated)}</td>
                  </tr>
                ))}
              </Table>
            ))}
        </>
      )}
    </main>
  );
}
