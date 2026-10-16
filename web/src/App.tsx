import { useEffect, useState, type FormEvent } from "react";
import { Link, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";
import { api, paths, useApi, type Me, type Project, type Summary } from "./api";
import { Field, Hero, Loading } from "./components";
import { ArtifactPage } from "./pages/Artifact";
import { PalettePage } from "./pages/Palette";
import { ProjectPage } from "./pages/Project";
import { ProjectsPage } from "./pages/Projects";
import { artifactHref, isReport, isRunName, reportHref } from "./util";

type RailItem = { key: string; title: string; to: string; plain?: boolean; current: boolean };

/** The report sidebar's own markup and classes: a labelled list per section, the current item marked. */
function RailSection({ label, items }: { label: string; items: RailItem[] }) {
  if (!items.length) return null;
  return (
    <section className="report-list" aria-label={label}>
      <p className="rail-label">{label} · {items.length}</p>
      <nav>
        {items.map(item => {
          const props = { title: item.title, "aria-current": item.current ? ("page" as const) : undefined };
          return item.plain
            ? <Link key={item.key} to={item.to} {...props}><span>{item.title}</span></Link>
            : <a key={item.key} href={item.to} {...props}><span>{item.title}</span></a>; // report pages are served whole, outside the app
        })}
      </nav>
    </section>
  );
}

function Rail() {
  const { pathname } = useLocation();
  const [open, setOpen] = useState(false);
  useEffect(() => { setOpen(false); }, [pathname]);
  const match = /^\/p\/([^/]+)(?:\/a\/(.+?))?\/?$/.exec(pathname);
  const project = match ? decodeURIComponent(match[1]) : null;
  const here = match?.[2] ? decodeURIComponent(match[2]).split("@")[0] : null;
  const projects = useApi<Project[]>(pathname === "/login" ? null : paths.projects);
  const detail = useApi<{ project: Project; artifacts: Summary[] }>(project ? paths.project(project) : null);
  const me = useApi<Me>(pathname === "/login" ? null : "me");
  const artifacts = (detail.data?.artifacts ?? []).slice().sort((a, b) => b.updated.localeCompare(a.updated));
  const title = (a: Summary) => a.metadata.title || a.name.split("/")[1];
  const reports = artifacts.filter(a => isReport(a.name) && a.latest).sort((a, b) => title(a).toLowerCase().localeCompare(title(b).toLowerCase()));
  const others = artifacts.filter(a => !isReport(a.name) && !isRunName(a.name));
  const runs = artifacts.filter(a => isRunName(a.name));
  const projectItems: RailItem[] = (projects.data ?? []).map(p => ({ key: p.name, title: p.title || p.name, to: `/p/${encodeURIComponent(p.name)}/`, plain: true, current: p.name === project }));
  return (
    <aside className={open ? "rail open" : "rail"}>
      <Link className="brand" to="/"><span>C</span> <span className="site-title">Cairn</span></Link>
      <button type="button" className="rail-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>Browse</button>
      {me.data?.mode !== "local" && <RailSection label="Projects" items={projectItems} />}
      {project && <RailSection label="Reports" items={reports.map(a => ({ key: a.name, title: title(a), to: reportHref(project, a.name), current: false }))} />}
      {project && <RailSection label="Artifacts" items={others.map(a => ({ key: a.name, title: a.name, to: artifactHref(project, a.name), plain: true, current: a.name === here }))} />}
      {project && <RailSection label="Runs" items={runs.map(a => ({ key: a.name, title: a.name.slice("runs/".length), to: artifactHref(project, a.name), plain: true, current: a.name === here }))} />}
      <div className="rail-note">
        <span className="rail-links"><Link to="/palette">Components</Link>{me.data?.auth && <a href="/logout">Log out</a>}</span>
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
      <Hero eyebrow="Cairn" title="Log in" subtitle="Enter the token (the file token in the server's repo), or open the one-time link cairn ui printed." />
      <form className="login-form" onSubmit={submit}>
        <Field label="Token" type="password" autoComplete="current-password" autoFocus required value={token} onChange={e => setToken(e.target.value)} />
        {error && <p className="login-error" role="alert">{error}</p>}
        <div className="form-actions"><button className="primary" type="submit" disabled={busy}>Open repo</button></div>
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

/** `/`: a local UI (one project, `cairn ui` on a project's repo) opens that project; a server lists its projects. */
function Home() {
  const me = useApi<Me>("me");
  if (me.loading && !me.data) return <main><Loading /></main>;
  if (me.data?.mode === "local" && me.data.home) return <Navigate replace to={`/p/${encodeURIComponent(me.data.home)}/`} />;
  return <ProjectsPage />;
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
        <Route path="/" element={<Home />} />
        <Route path="/login" element={<Login />} />
        <Route path="/palette" element={<PalettePage />} />
        <Route path="/p/:project" element={<ProjectPage />} />
        <Route path="/p/:project/a/*" element={<ArtifactRoute />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </>
  );
}

