import { useEffect, useState } from "react";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export interface Project { name: string; title: string; description: string; created: string; artifacts?: number; updated?: string }
export interface Summary { name: string; latest: number | null; versions: number; aliases: Record<string, number>; updated: string; metadata: Record<string, any>; pending?: number }
export interface VersionRow {
  version: number | null; id: string; short_id: string; pending?: boolean; sync?: string; error?: string | null; status: string; label: string | null; created: string; completed?: string;
  source?: { host?: string } | null; metadata: Record<string, any>; input_count: number; file_count: number; size: number;
  deleted_at?: string; deleted_by?: string | null; reason?: string | null;  // a tombstone (status "deleted")
}
export interface Entry { path: string; sha256?: string; size?: number; link?: string }
export interface Manifest extends Omit<VersionRow, "input_count" | "file_count" | "size"> { inputs: string[]; files: Entry[]; digest?: string }
export interface Me { auth: boolean; mode?: "local" | "server"; home?: string | null }
/** What the app knows of a version it names by id (an input, a consumer, a lineage node): name and number are null
 *  for an id the repo does not know (and the number while it is pending). */
export interface NodeInfo { name: string | null; version: number | null; short_id: string; status?: string | null; kind?: string }
export interface LineageNode extends NodeInfo { inputs: string[] | null; consumers: string[] | null; deleted?: boolean }
export interface Lineage { ref: string; depth: number; nodes: Record<string, LineageNode>; truncated: boolean }
export interface ArtifactData { project: string; name: string; summary: Summary; versions: VersionRow[]; aliases: Record<string, number> }
/** One version: `ref` is its exact reference (`name@v<n>`, or its whole id while pending); inputs and consumers are ids,
 *  described in `nodes` (as the version itself). */
export interface VersionData { project: string; name: string; ref: string; version: Manifest; consumers: string[]; deleted: string[]; nodes: Record<string, NodeInfo> }
export type Row = Record<string, number | string>;
export interface Rows { rows: Row[]; total: number; truncated: boolean }
export interface Tail { text: string; truncated: boolean; size: number }
export interface RunData {
  project: string; name: string; version: number | null; id: string; short_id: string; status: string;
  metrics: Rows; system: Rows; stdout: Tail | null; stderr: Tail | null; error: Tail | null;
  info: Record<string, any> | null; config: unknown; sweep: Record<string, any> | null;
}

const enc = encodeURIComponent;
/** The API's paths; `ref` is any reference text (an id or a prefix of one, a name, name@v<n>, name@<word>). */
export const paths = {
  projects: "projects",
  project: (p: string) => `projects/${enc(p)}`,
  artifact: (p: string, name: string) => `projects/${enc(p)}/artifacts/${name.split("/").map(enc).join("/")}`,
  version: (p: string, ref: string) => `projects/${enc(p)}/version?ref=${enc(ref)}`,
  run: (p: string, ref: string) => `projects/${enc(p)}/run?ref=${enc(ref)}`,
  lineage: (p: string, ref: string, depth = 1) => `projects/${enc(p)}/lineage?ref=${enc(ref)}&depth=${depth}`,
};

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/ui-api/${path}`, {
    credentials: "same-origin",
    ...init,
    headers: { "X-Cairn": "1", ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
  });
  let body: any = null;
  try { body = await response.json(); } catch { /* not JSON */ }
  if (response.status === 401 && path !== "login") {
    cache.clear();
    if (location.pathname !== "/login") location.assign(`/login?next=${enc(location.pathname + location.search)}`);
  }
  if (!response.ok) throw new ApiError(response.status, body?.error ?? `HTTP ${response.status}`);
  return body as T;
}

const cache = new Map<string, { time: number; data: unknown }>();
const TTL = 15000;

export function useApi<T>(path: string | null) {
  const [state, set] = useState<{ path: string | null; data?: T; error?: ApiError }>({ path: null });
  useEffect(() => {
    if (!path) return;
    let live = true;
    const hit = cache.get(path);
    if (hit && Date.now() - hit.time < TTL) { set({ path, data: hit.data as T }); return; }
    api<T>(path).then(
      data => { cache.set(path, { time: Date.now(), data }); if (live) set({ path, data }); },
      error => { if (live) set({ path, error }); },
    );
    return () => { live = false; };
  }, [path]);
  const current = state.path === path;
  return { data: current ? state.data : undefined, error: current ? state.error : undefined, loading: !!path && !(current && (state.data || state.error)) };
}

/** `useApi` that fetches again every `every` ms while `active` (a running run's panel), bypassing the cache. */
export function usePoll<T>(path: string | null, every: number, active: boolean) {
  const [state, set] = useState<{ path: string | null; data?: T; error?: ApiError; tick: number }>({ path: null, tick: 0 });
  useEffect(() => {
    if (!path) return;
    let live = true;
    const load = () => api<T>(path).then(
      data => { if (live) set(prev => ({ path, data, tick: prev.tick + 1 })); },
      error => { if (live) set(prev => ({ path, error, data: prev.path === path ? prev.data : undefined, tick: prev.tick + 1 })); },
    );
    load();
    const timer = active ? window.setInterval(load, every) : undefined;
    return () => { live = false; if (timer) window.clearInterval(timer); };
  }, [path, every, active]);
  const current = state.path === path;
  return { data: current ? state.data : undefined, error: current ? state.error : undefined, loading: !!path && !(current && (state.data || state.error)), tick: state.tick };
}

/** Drop a path from the cache (a running version's data changes). */
export const forget = (path: string) => cache.delete(path);
