import { Link, useParams, useSearchParams } from "react-router-dom";
import { paths, useApi, type Project, type Summary } from "../api";
import { ErrorNote, Hero, Loading, Select, Table } from "../components";
import { artifactHref, isReport, isRunName, reportHref, when } from "../util";

const matches = (a: Summary, query: string) =>
  !query || `${a.name} ${JSON.stringify(a.metadata)} ${Object.keys(a.aliases).join(" ")}`.toLowerCase().includes(query.toLowerCase());

export function ProjectPage() {
  const project = useParams().project!;
  const { data, error, loading } = useApi<{ project: Project; artifacts: Summary[] }>(paths.project(project));
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab");
  const query = params.get("q") ?? "", tab = asked === "artifacts" || asked === "runs" ? asked : "reports", sort = params.get("sort") ?? "updated";
  const set = (key: string, value: string) => setParams(prev => { const next = new URLSearchParams(prev); value ? next.set(key, value) : next.delete(key); return next; }, { replace: true });
  document.title = `${data?.project.title ?? project} - Cairn`;

  const order = (a: Summary, b: Summary) => (sort === "name" ? a.name.localeCompare(b.name) : b.updated.localeCompare(a.updated));
  const all = data?.artifacts ?? [];
  const reports = all.filter(a => isReport(a.name) && matches(a, query)).sort(order);
  const others = all.filter(a => !isReport(a.name) && !isRunName(a.name) && matches(a, query)).sort(order);
  const runs = all.filter(a => isRunName(a.name) && matches(a, query)).sort(order);
  const reportCount = all.filter(a => isReport(a.name)).length;
  const runCount = all.filter(a => isRunName(a.name)).length;

  return (
    <main>
      {error && <ErrorNote error={error} />}
      {loading && <Loading />}
      {data && (
        <>
          <Hero eyebrow={<><Link to="/">Projects</Link> / {project}</>} title={data.project.title || project} subtitle={data.project.description} status={`${all.length} artifact${all.length === 1 ? "" : "s"}`} />
          <div className="toolbar">
            <input type="search" placeholder="Search names, titles, status, aliases" aria-label="Search" value={query} onChange={e => set("q", e.target.value)} />
            <Select label="Sort" value={sort} onChange={v => set("sort", v === "updated" ? "" : v)}
              choices={[{ value: "updated", label: "Sort: updated" }, { value: "name", label: "Sort: name" }]} />
          </div>
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "reports"} onClick={() => set("tab", "")}>Reports <span>{reports.length}</span></button>
            <button role="tab" aria-selected={tab === "artifacts"} onClick={() => set("tab", "artifacts")}>Artifacts <span>{others.length}</span></button>
            <button role="tab" aria-selected={tab === "runs"} onClick={() => set("tab", "runs")}>Runs <span>{runs.length}</span></button>
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
            ? <p className="hint">{all.length > reportCount + runCount ? "No artifact matches." : "No other artifacts yet."}</p>
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
          {tab === "runs" && (runs.length === 0
            ? <p className="hint">{runCount ? "No run matches." : "No runs yet. Every process that uses or publishes artifacts is one (cairn.init)."}</p>
            : (
              <Table head={["Run", "Latest completed", "Config", "Versions", "Updated"]}>
                {runs.map(a => {
                  const config = a.metadata.config && typeof a.metadata.config === "object" ? Object.keys(a.metadata.config).slice(0, 4).join(", ") : "";
                  return (
                    <tr key={a.name}>
                      <td><Link to={artifactHref(project, a.name, a.versions ? `v${a.versions}` : null)}><strong>{a.name.slice("runs/".length)}</strong></Link></td>
                      <td>{a.latest ? <Link to={artifactHref(project, a.name, a.latest)}>v{a.latest}</Link> : <span className="muted">none yet</span>}</td>
                      <td className="muted">{config}</td>
                      <td>{a.versions}</td>
                      <td className="muted nowrap">{when(a.updated)}</td>
                    </tr>
                  );
                })}
              </Table>
            ))}
        </>
      )}
    </main>
  );
}
