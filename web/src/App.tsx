import { useEffect, useState, type FormEvent } from "react";
import { Link, NavLink, Route, Routes, useLocation, useParams } from "react-router-dom";
import { api, paths, useApi, type Me, type Project, type Summary } from "./api";
import { Hero, Loading } from "./components";
import { ArtifactPage } from "./pages/Artifact";
import { ProjectPage } from "./pages/Project";
import { ProjectsPage } from "./pages/Projects";
import { artifactHref, isReport, isRunName, enc } from "./util";

function Rail() {
  const { pathname } = useLocation();
  const match = /^\/p\/([^/]+)/.exec(pathname);
  const project = match ? decodeURIComponent(match[1]) : null;
  const projects = useApi<Project[]>(pathname === "/login" ? null : paths.projects);
  const detail = useApi<{ project: Project; artifacts: Summary[] }>(project ? paths.project(project) : null);
  const me = useApi<Me>(pathname === "/login" ? null : "me");
  const artifacts = (detail.data?.artifacts ?? []).slice().sort((a, b) => b.updated.localeCompare(a.updated));
  const reports = artifacts.filter(a => isReport(a.name));
  const others = artifacts.filter(a => !isReport(a.name) && !isRunName(a.name));
  const runs = artifacts.filter(a => isRunName(a.name));
  return (
    <aside className="rail">
      <Link className="brand" to="/"><span>C</span> Cairn</Link>
      <div className="rail-scroll">
        {projects.data && (
          <section className="report-list" aria-label="Projects">
            <p className="rail-label">Projects</p>
            <nav>
              {projects.data.map(p => (
                <NavLink key={p.name} to={`/p/${encodeURIComponent(p.name)}/`} end={false} className={p.name === project ? "active-project" : ""} aria-current={p.name === project ? "page" : undefined}>
                  {p.title || p.name}
                </NavLink>
              ))}
            </nav>
          </section>
        )}
        {project && reports.length > 0 && (
          <section className="report-list rail-project" aria-label="Reports">
            <p className="rail-label">Reports</p>
            <nav>
              {reports.slice(0, 30).map(a => (
                <a key={a.name} href={`/p/${encodeURIComponent(project)}/r/${enc(a.name.split("/")[1])}/`}>{a.metadata.title || a.name.split("/")[1]}</a>
              ))}
            </nav>
          </section>
        )}
        {project && others.length > 0 && (
          <section className="report-list rail-project" aria-label="Artifacts">
            <p className="rail-label">Artifacts</p>
            <nav>
              {others.slice(0, 60).map(a => <Link key={a.name} to={artifactHref(project, a.name)}>{a.name}</Link>)}
            </nav>
          </section>
        )}
        {project && runs.length > 0 && (
          <section className="report-list rail-project" aria-label="Runs">
            <p className="rail-label">Runs</p>
            <nav>
              {runs.slice(0, 30).map(a => <Link key={a.name} to={artifactHref(project, a.name, a.versions ? `v${a.versions}` : null)}>{a.name.slice("runs/".length)}</Link>)}
            </nav>
          </section>
        )}
      </div>
      <div className="rail-note">
        {me.data?.scope === "project" && <>Token for project <b>{me.data.project}</b><br /></>}
        {me.data?.auth && <a href="/logout">Log out</a>}
      </div>
    </aside>
  );
}

function Login() {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      await api("login", { method: "POST", body: JSON.stringify({ token }) });
      const next = new URLSearchParams(location.search).get("next") ?? "/";
      location.assign(next.startsWith("/") && !next.startsWith("//") && !next.includes("\\") ? next : "/");
    } catch (e) {
      setError((e as Error).message); setBusy(false);
    }
  };
  return (
    <main>
      <Hero eyebrow="Artifact store" title="Log in" subtitle="Enter a store token: the owner token or one scoped to a project." />
      <form className="login-form" onSubmit={submit}>
        <label>Store token
          <input type="password" autoComplete="current-password" autoFocus required value={token} onChange={e => setToken(e.target.value)} />
        </label>
        {error && <p className="login-error" role="alert">{error}</p>}
        <div className="form-actions"><button className="primary" type="submit" disabled={busy}>Open store</button></div>
      </form>
    </main>
  );
}

function ArtifactRoute() {
  const params = useParams();
  const rest = params["*"] ?? "";
  const at = rest.indexOf("@");
  const name = (at < 0 ? rest : rest.slice(0, at)).replace(/\/+$/, "");
  const sel = at < 0 ? null : rest.slice(at + 1).replace(/\/+$/, "");
  return <ArtifactPage project={params.project!} name={name} selector={sel} />;
}

function NotFound() {
  return <main><Hero eyebrow="404" title="No such page" subtitle={<Link to="/">Back to the projects</Link>} /></main>;
}

export function App() {
  const { pathname } = useLocation();
  useEffect(() => { window.scrollTo(0, 0); }, [pathname]);
  return (
    <>
      <Rail />
      <Routes>
        <Route path="/" element={<ProjectsPage />} />
        <Route path="/login" element={<Login />} />
        <Route path="/p/:project" element={<ProjectPage />} />
        <Route path="/p/:project/a/*" element={<ArtifactRoute />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </>
  );
}

export { Loading };
