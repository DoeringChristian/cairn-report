import { useEffect, useState } from "react";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export interface Project { name: string; title: string; description: string; created: string; artifacts?: number; updated?: string }
export interface Summary { name: string; latest: number | null; versions: number; aliases: Record<string, number>; updated: string; metadata: Record<string, any> }
export interface VersionRow {
  version: number; status: string; label: string | null; created: string; completed?: string;
  source?: { host?: string } | null; metadata: Record<string, any>; input_count: number; file_count: number; size: number;
}
export interface Entry { path: string; sha256?: string; size?: number; link?: string }
export interface Manifest extends Omit<VersionRow, "input_count" | "file_count" | "size"> { inputs: string[]; files: Entry[]; digest?: string }
export interface Me { scope: "owner" | "project" | "store"; project: string | null; auth: boolean }
export interface LineageNode { inputs: string[] | null; consumers: string[] | null }
export interface Lineage { ref: string; depth: number; nodes: Record<string, LineageNode>; truncated: boolean }

const enc = encodeURIComponent;
export const paths = {
  projects: "projects",
  project: (p: string) => `projects/${enc(p)}`,
  artifact: (p: string, name: string) => `projects/${enc(p)}/artifacts/${enc(name)}`,
  version: (p: string, name: string, sel: string) => `projects/${enc(p)}/artifacts/${enc(name)}/versions/${enc(sel)}`,
  lineage: (ref: string, depth = 1) => `lineage?ref=${enc(ref)}&depth=${depth}`,
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
