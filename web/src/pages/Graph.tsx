import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, paths, type Lineage, type LineageNode } from "../api";
import { artifactHref, parseRef } from "../util";

const W = 210, H = 46, GAP_X = 84, GAP_Y = 14, PAD = 16, PAD_X = 26;
type Info = Record<string, LineageNode>;

interface Placed { ref: string; col: number; row: number; x: number; y: number }

/** Inputs to the left, the artifact in the middle, consumers to the right; column = longest distance from the center. */
function layout(root: string, info: Info) {
  const depth: Record<string, number> = { [root]: 0 };
  const relax = (side: "inputs" | "consumers", sign: number) => {
    for (let pass = 0; pass < 12; pass++) {
      let changed = false;
      for (const ref of Object.keys(depth)) {
        if (ref !== root && Math.sign(depth[ref]) !== sign) continue;
        for (const next of info[ref]?.[side] ?? []) {
          const want = depth[ref] + sign, current = depth[next];
          if (next === root || Math.abs(want) > 12) continue;
          if (current === undefined || (Math.sign(current) === sign && Math.abs(want) > Math.abs(current))) { depth[next] = want; changed = true; }
        }
      }
      if (!changed) break;
    }
  };
  relax("inputs", -1); relax("consumers", 1);
  const edges: [string, string][] = [];
  Object.entries(info).forEach(([ref, node]) => {
    node.inputs?.forEach(i => edges.push([i, ref]));
    node.consumers?.forEach(c => edges.push([ref, c]));
  });
  for (let pass = 0; pass < 12; pass++) {  // an edge always points one column to the right, also among the nodes of one side
    let changed = false;
    for (const [a, b] of edges) {
      const da = depth[a], db = depth[b];
      if (da === undefined || db === undefined) continue;
      if (da >= 0 && db > 0 && db <= da) { depth[b] = da + 1; changed = true; }
      else if (db <= 0 && da < 0 && da >= db) { depth[a] = db - 1; changed = true; }
    }
    if (!changed) break;
  }
  const columns = new Map<number, string[]>();
  Object.entries(depth).forEach(([ref, col]) => columns.set(col, [...(columns.get(col) ?? []), ref]));
  const keys = [...columns.keys()].sort((a, b) => a - b);
  // order rows by the mean row of already placed neighbours, walking outward from the center
  const rowOf: Record<string, number> = {};
  const order = [...keys.filter(k => k >= 0), ...keys.filter(k => k < 0).reverse()];
  for (const col of order) {
    const refs = columns.get(col)!;
    const score = (ref: string) => {
      const near = edges.filter(([a, b]) => (a === ref && rowOf[b] !== undefined) || (b === ref && rowOf[a] !== undefined)).map(([a, b]) => rowOf[a === ref ? b : a]);
      return near.length ? near.reduce((x, y) => x + y, 0) / near.length : 0;
    };
    refs.sort((a, b) => score(a) - score(b) || a.localeCompare(b));
    refs.forEach((ref, index) => (rowOf[ref] = index));
  }
  const tallest = Math.max(...keys.map(k => columns.get(k)!.length));
  const height = tallest * (H + GAP_Y) - GAP_Y;
  const placed: Record<string, Placed> = {};
  for (const col of keys) {
    const refs = columns.get(col)!;
    const offset = (height - (refs.length * (H + GAP_Y) - GAP_Y)) / 2;
    refs.forEach((ref, row) => (placed[ref] = { ref, col, row, x: PAD_X + (col - keys[0]) * (W + GAP_X), y: PAD + offset + row * (H + GAP_Y) }));
  }
  const unique = new Set(edges.filter(([a, b]) => placed[a] && placed[b]).map(([a, b]) => `${a}\n${b}`));
  return { placed, edges: [...unique].map(key => key.split("\n") as [string, string]), width: PAD_X * 2 + keys.length * W + (keys.length - 1) * GAP_X, height: height + PAD * 2 };
}

function label(ref: string, project: string) {
  const parsed = parseRef(ref);
  if (!parsed) return { top: ref, bottom: "" };
  return { top: parsed.name, bottom: (parsed.project === project ? "" : parsed.project + " ") + "@" + parsed.sel };
}

export function Graph({ root, project }: { root: string; project: string }) {
  const navigate = useNavigate();
  const [info, setInfo] = useState<Info>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [truncated, setTruncated] = useState(false);

  const merge = (data: Lineage) => {
    setTruncated(t => t || data.truncated);
    setInfo(prev => {
      const next: Info = { ...prev };
      for (const [ref, node] of Object.entries(data.nodes)) next[ref] = { inputs: node.inputs ?? next[ref]?.inputs ?? null, consumers: node.consumers ?? next[ref]?.consumers ?? null };
      return next;
    });
  };
  const load = async (ref: string, depth = 1) => {
    setBusy(true); setError("");
    try { merge(await api<Lineage>(paths.lineage(ref, depth))); } catch (e) { setError((e as Error).message); }
    setBusy(false);
  };
  useEffect(() => { setInfo({}); setTruncated(false); load(root, 1); /* eslint-disable-next-line */ }, [root]);

  const model = useMemo(() => (info[root] ? layout(root, info) : null), [info, root]);
  if (error) return <p className="hint">Lineage unavailable: {error}</p>;
  if (!model) return <p className="hint state-note">Loading lineage…</p>;
  const { placed, edges, width, height } = model;
  if (edges.length === 0) return <p className="hint">This version has no inputs and no consumers.</p>;

  const expandable = (p: Placed) => {
    if (p.col === 0) return false;
    const side = p.col < 0 ? "inputs" : "consumers";
    return info[p.ref]?.[side] == null;
  };
  const anyExpandable = Object.values(placed).some(expandable);

  return (
    <div className="graph-card">
      <div className="graph-bar">
        <span className="muted">Inputs on the left, consumers on the right. Click a node to open it, + to expand one more level.</span>
        <button className="comment-trigger" disabled={busy || !anyExpandable} onClick={async () => {
          setBusy(true);
          try { merge(await api<Lineage>(paths.lineage(root, 3))); } catch (e) { setError((e as Error).message); }
          setBusy(false);
        }}>Expand all (3 levels)</button>
      </div>
      <div className="graph-scroll">
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Lineage graph">
          {edges.map(([a, b]) => {
            const from = placed[a], to = placed[b];
            const x1 = from.x + W, y1 = from.y + H / 2, x2 = to.x, y2 = to.y + H / 2, mid = (x1 + x2) / 2;
            return <path key={a + b} className="graph-edge" d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`} />;
          })}
          {Object.values(placed).map(p => {
            const parsed = parseRef(p.ref), text = label(p.ref, project);
            const go = () => parsed && navigate(artifactHref(parsed.project, parsed.name, parsed.sel));
            return (
              <g key={p.ref} className={"graph-node" + (p.col === 0 ? " is-root" : "")} transform={`translate(${p.x},${p.y})`}>
                <g role="link" tabIndex={0} onClick={go} onKeyDown={e => { if (e.key === "Enter") go(); }}>
                  <title>{p.ref}</title>
                  <rect width={W} height={H} rx={10} />
                  <text x={12} y={19} className="graph-name">{text.top.length > 28 ? "…" + text.top.slice(-27) : text.top}</text>
                  <text x={12} y={35} className="graph-sub">{text.bottom}</text>
                </g>
                {expandable(p) && (
                  <g role="button" tabIndex={0} aria-label={`Expand ${p.ref}`} className="graph-expand" transform={`translate(${p.col < 0 ? -10 : W + 10},${H / 2})`}
                    onClick={() => load(p.ref)} onKeyDown={e => { if (e.key === "Enter") load(p.ref); }}>
                    <circle r={10} /><text textAnchor="middle" y={4}>+</text>
                  </g>
                )}
              </g>
            );
          })}
        </svg>
      </div>
      {truncated && <p className="hint">The graph was capped; open a node to continue from there.</p>}
    </div>
  );
}
