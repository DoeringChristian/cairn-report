import { Link } from "react-router-dom";
import { paths, useApi, type Project } from "../api";
import { ErrorNote, Hero, Loading, Table } from "../components";
import { when } from "../util";

export function ProjectsPage() {
  const { data, error, loading } = useApi<Project[]>(paths.projects);
  document.title = "Projects - Cairn";
  return (
    <main>
      <Hero eyebrow="Repo" title="Projects" subtitle="Most recently updated first." />
      {loading && <Loading />}
      {error && <ErrorNote error={error} />}
      {data && data.length === 0 && <p className="hint">No projects yet. Run <code>cairn publish</code> in a project (with <code>[repo] remote</code> set to this remote, for a remote).</p>}
      {data && data.length > 0 && (
        <Table head={["Project", "Description", "Artifacts", "Updated"]}>
          {data.map(p => (
            <tr key={p.name}>
              <td><Link to={`/p/${encodeURIComponent(p.name)}/`}><strong>{p.title || p.name}</strong></Link><br /><span className="muted mono">{p.name}</span></td>
              <td className="muted">{p.description}</td>
              <td>{p.artifacts}</td>
              <td className="muted nowrap">{when(p.updated)}</td>
            </tr>
          ))}
        </Table>
      )}
    </main>
  );
}
