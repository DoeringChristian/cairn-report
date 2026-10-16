import { Link, useParams, useSearchParams } from "react-router-dom";
import { paths, useApi, type Project, type Summary } from "../api";
import { AliasPills, Crumbs, DataTable, ErrorNote, Hero, Hint, Loading, Pill, SearchInput, Select, Tabs, Toolbar } from "../components";
import { artifactHref, isReport, isRunName, reportHref, when } from "../util";

type Tab = "reports" | "artifacts" | "runs";

const matches = (a: Summary, query: string) =>
  !query || `${a.name} ${JSON.stringify(a.metadata)} ${Object.keys(a.aliases).join(" ")}`.toLowerCase().includes(query.toLowerCase());

export function ProjectPage() {
  const project = useParams().project!;
  const { data, error, loading } = useApi<{ project: Project; artifacts: Summary[] }>(paths.project(project));
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab");
  const query = params.get("q") ?? "", tab: Tab = asked === "artifacts" || asked === "runs" ? asked : "reports", sort = params.get("sort") ?? "updated";
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
          <Hero eyebrow={<Crumbs items={[{ label: "Projects", to: "/" }, { label: project }]} />} title={data.project.title || project} subtitle={data.project.description} status={`${all.length} artifact${all.length === 1 ? "" : "s"}`} />
          <Toolbar>
            <SearchInput label="Search" placeholder="Search names, titles, status, aliases" value={query} onChange={v => set("q", v)} />
            <Select label="Sort" value={sort} onChange={v => set("sort", v === "updated" ? "" : v)}
              choices={[{ value: "updated", label: "Sort: updated" }, { value: "name", label: "Sort: name" }]} />
          </Toolbar>
          <Tabs<Tab> label="Kinds" value={tab} onChange={v => set("tab", v === "reports" ? "" : v)}
            tabs={[{ value: "reports", label: "Reports", count: reports.length }, { value: "artifacts", label: "Artifacts", count: others.length }, { value: "runs", label: "Runs", count: runs.length }]} />
          {tab === "reports" && (reports.length === 0
            ? <Hint>{reportCount ? "No report matches." : "No reports yet."}</Hint>
            : (
              <DataTable head={["Report", "Status", "Latest", "Updated"]}>
                {reports.map(a => {
                  const title = a.metadata.title || a.name.split("/")[1];
                  return (
                    <tr key={a.name}>
                      <td>
                        {a.latest ? <a href={reportHref(project, a.name)}><strong>{title}</strong></a> : <strong>{title}</strong>}
                        {a.metadata.summary && <><br /><span className="muted">{a.metadata.summary}</span></>}
                      </td>
                      <td>{a.metadata.status && <Pill>{a.metadata.status}</Pill>}</td>
                      <td className="nowrap">{a.latest ? <Link to={artifactHref(project, a.name, a.latest)}>v{a.latest}</Link> : a.pending ? <Pill tone="pending">pending</Pill> : "none"}<br /><span className="muted">{a.versions} version{a.versions === 1 ? "" : "s"}</span></td>
                      <td className="muted nowrap">{when(a.updated)}{a.latest && <><br /><a href={reportHref(project, a.name)}>Open report</a></>}</td>
                    </tr>
                  );
                })}
              </DataTable>
            ))}
          {tab === "artifacts" && (others.length === 0
            ? <Hint>{all.length > reportCount + runCount ? "No artifact matches." : "No other artifacts yet."}</Hint>
            : (
              <DataTable head={["Artifact", "Latest", "Aliases", "Versions", "Updated"]}>
                {others.map(a => (
                  <tr key={a.name}>
                    <td><Link to={artifactHref(project, a.name)}><strong>{a.name}</strong></Link></td>
                    <td>{a.latest ? `v${a.latest}` : a.pending ? <Pill tone="pending">pending</Pill> : "none"}</td>
                    <td><AliasPills aliases={Object.keys(a.aliases).filter(x => x !== "latest")} /></td>
                    <td>{a.versions}</td>
                    <td className="muted nowrap">{when(a.updated)}</td>
                  </tr>
                ))}
              </DataTable>
            ))}
          {tab === "runs" && (runs.length === 0
            ? <Hint>{runCount ? "No run matches." : "No runs yet. Every process that uses or publishes artifacts is one (cairn.init)."}</Hint>
            : (
              <DataTable head={["Run", "Latest completed", "Config", "Versions", "Updated"]}>
                {runs.map(a => {
                  const config = a.metadata.config && typeof a.metadata.config === "object" ? Object.keys(a.metadata.config).slice(0, 4).join(", ") : "";
                  return (
                    <tr key={a.name}>
                      <td><Link to={artifactHref(project, a.name)}><strong>{a.name.slice("runs/".length)}</strong></Link></td>
                      <td>{a.latest ? <Link to={artifactHref(project, a.name, a.latest)}>v{a.latest}</Link> : <span className="muted">none yet</span>}</td>
                      <td className="muted">{config}</td>
                      <td>{a.versions}</td>
                      <td className="muted nowrap">{when(a.updated)}</td>
                    </tr>
                  );
                })}
              </DataTable>
            ))}
        </>
      )}
    </main>
  );
}
