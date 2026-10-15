import { useEffect, useRef } from "react";
import { paths, usePoll, type Row, type RunData, type Tail } from "../api";
import { Pill, Table } from "../components";
import { when } from "../util";

const REFRESH = 5000;
const W = 300, H = 120, PAD_L = 44, PAD_B = 22, PAD_T = 10, PAD_R = 10;
const SKIP = new Set(["step", "time", "elapsed_s"]);

type Point = [number, number];

/** Each numeric key of the rows as a series over `step` (when the rows have one) or over seconds since the first row. */
function series(rows: Row[], bySteps: boolean): Map<string, Point[]> {
  const start = rows.length ? Date.parse(String(rows[0].time ?? "")) : NaN;
  const found = new Map<string, Point[]>();
  rows.forEach((row, index) => {
    const t = Date.parse(String(row.time ?? ""));
    const x = bySteps ? Number(row.step) : Number.isFinite(t) && Number.isFinite(start) ? (t - start) / 1000 : index;
    if (!Number.isFinite(x)) return;
    for (const [key, value] of Object.entries(row)) {
      if (SKIP.has(key) || typeof value !== "number" || !Number.isFinite(value)) continue;
      if (!found.has(key)) found.set(key, []);
      found.get(key)!.push([x, value]);
    }
  });
  return found;
}

const tick = (value: number) => {
  const size = Math.abs(value);
  if (size !== 0 && (size >= 1e5 || size < 1e-3)) return value.toExponential(1);
  return Number(value.toPrecision(3)).toString();
};

function Chart({ title, points, xLabel }: { title: string; points: Point[]; xLabel: string }) {
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  let [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  if (x0 === x1) { x0 -= 1; x1 += 1; }
  if (y0 === y1) { y0 -= Math.abs(y0) * 0.1 || 1; y1 += Math.abs(y1) * 0.1 || 1; }
  const sx = (x: number) => PAD_L + ((x - x0) / (x1 - x0)) * (W - PAD_L - PAD_R);
  const sy = (y: number) => H - PAD_B - ((y - y0) / (y1 - y0)) * (H - PAD_B - PAD_T);
  const line = points.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(" ");
  const last = points[points.length - 1];
  return (
    <figure className="run-chart">
      <figcaption><span className="mono">{title}</span><span className="muted">{tick(last[1])}</span></figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title} over ${xLabel}`}>
        <line className="run-axis" x1={PAD_L} y1={H - PAD_B} x2={W - PAD_R} y2={H - PAD_B} />
        <line className="run-axis" x1={PAD_L} y1={PAD_T} x2={PAD_L} y2={H - PAD_B} />
        <text className="run-tick" x={PAD_L - 4} y={PAD_T + 8} textAnchor="end">{tick(y1)}</text>
        <text className="run-tick" x={PAD_L - 4} y={H - PAD_B} textAnchor="end">{tick(y0)}</text>
        <text className="run-tick" x={PAD_L} y={H - 6}>{tick(x0)}</text>
        <text className="run-tick" x={W - PAD_R} y={H - 6} textAnchor="end">{tick(x1)} {xLabel}</text>
        {points.length === 1
          ? <circle className="run-dot" cx={sx(last[0])} cy={sy(last[1])} r={3} />
          : <path className="run-line" d={line} />}
      </svg>
    </figure>
  );
}

function Charts({ rows, empty, preferSteps }: { rows: Row[]; empty: string; preferSteps: boolean }) {
  const bySteps = preferSteps && rows.some(row => typeof row.step === "number");
  const found = series(bySteps ? rows.filter(row => typeof row.step === "number") : rows, bySteps);
  if (!found.size) return <p className="hint">{empty}</p>;
  return (
    <div className="run-charts">
      {[...found.entries()].map(([key, points]) => <Chart key={key} title={key} points={points} xLabel={bySteps ? "step" : "s"} />)}
    </div>
  );
}

function LogTail({ title, tail, follow }: { title: string; tail: Tail | null; follow: boolean }) {
  const box = useRef<HTMLPreElement>(null);
  useEffect(() => { if (follow && box.current) box.current.scrollTop = box.current.scrollHeight; }, [tail?.text, follow]);
  if (!tail) return null;
  return (
    <div className="run-log">
      <p className="run-log-title"><span className="mono">{title}</span>{tail.truncated && <span className="muted"> the last {Math.round(tail.text.length / 1024)} KB of {Math.round(tail.size / 1024)} KB</span>}</p>
      {tail.text ? <pre ref={box} className="preview-text">{tail.text}</pre> : <p className="hint">Empty.</p>}
    </div>
  );
}

function Info({ info, sweep }: { info: Record<string, any> | null; sweep: Record<string, any> | null }) {
  if (!info && !sweep) return null;
  const slurm = info?.slurm ?? null;
  const rows: [string, string][] = [];
  if (info?.host) rows.push(["Host", `${info.host}${info.cpus ? ` (${info.cpus} CPUs)` : ""}${info.gpus?.length ? `, ${info.gpus.join(", ")}` : ""}`]);
  if (info?.command) rows.push(["Command", (info.argv ?? info.command).join(" ")]);
  if (info?.cwd) rows.push(["Directory", info.cwd]);
  if (slurm?.job_id) rows.push(["Slurm job", `${slurm.job_id}${slurm.array_task_id ? ` (array task ${slurm.array_task_id})` : ""}${slurm.partition ? ` on ${slurm.partition}` : ""}`]);
  if (info?.python?.version) rows.push(["Python", `${info.python.version} (${info.python.executable})`]);
  if (info?.platform) rows.push(["Platform", info.platform]);
  if (info?.started) rows.push(["Started", when(info.started)]);
  if (sweep?.label && sweep?.num !== undefined) rows.push(["Sweep", `trial ${sweep.num} of ${sweep.label}`]);
  if (Array.isArray(sweep?.trials)) rows.push(["Sweep", `${sweep.trials.length} trials: ${sweep.trials.map((t: any) => `${t.trial} ${t.status}`).join(", ")}`]);
  return (
    <Table head={["", ""]}>
      {rows.map(([key, value]) => <tr key={key}><td className="nowrap"><strong>{key}</strong></td><td className="mono wrap-any">{value}</td></tr>)}
    </Table>
  );
}

/** The Run panel of a version of kind "run": its metrics, the machine's, the output, and where it ran. While the
 *  run is running it fetches again every few seconds. */
export function RunPanel({ project, name, version, status }: { project: string; name: string; version: number; status: string }) {
  const running = status === "running";
  const run = usePoll<RunData>(paths.run(project, name, `v${version}`), REFRESH, running);
  const data = run.data;
  const live = data ? data.status === "running" : running;
  return (
    <section className="app-section run-panel" aria-label="Run">
      <div className="section-head">
        <h2>Run</h2>
        <span className="muted">{data && <Pill status={data.status} />} {live ? `refreshing every ${REFRESH / 1000} s` : ""}</span>
      </div>
      {run.error && <p className="hint">Run data unavailable: {run.error.message}</p>}
      {!data && !run.error && <p className="hint state-note">Loading the run…</p>}
      {data && (
        <>
          {data.error && <LogTail title="logs/error.txt" tail={data.error} follow={false} />}
          <h3>Metrics</h3>
          <Charts rows={data.metrics.rows} preferSteps empty={live ? "No metrics synced yet (run.add({...}))." : "This run logged no metrics (run.add({...}))."} />
          {data.metrics.total > data.metrics.rows.length && <p className="hint">{data.metrics.rows.length} of {data.metrics.total} entries shown, evenly thinned.</p>}
          <h3>System</h3>
          <Charts rows={data.system.rows} preferSteps={false} empty="No system metrics (system/metrics.jsonl)." />
          <h3>Output</h3>
          {!data.stdout && !data.stderr && <p className="hint">No captured output (logs/stdout.txt, logs/stderr.txt).</p>}
          <LogTail title="logs/stdout.txt" tail={data.stdout} follow={live} />
          <LogTail title="logs/stderr.txt" tail={data.stderr} follow={live} />
          <h3>Where it ran</h3>
          <Info info={data.info} sweep={data.sweep} />
        </>
      )}
    </section>
  );
}
