import { useCallback } from "react";

export const when = (value?: string | null) => (value ? value.slice(0, 16).replace("T", " ") + " UTC" : "");

export function bytes(size: number): string {
  let unit = 0;
  while (size >= 1024 && unit < 3) { size /= 1024; unit++; }
  return `${unit ? size.toFixed(1) : size.toFixed(0)} ${["B", "KB", "MB", "GB"][unit]}`;
}

export const isReport = (name: string) => /^reports\/[^/]+$/.test(name);
export const isRunName = (name: string) => name.startsWith("runs/");
export const enc = (path: string) => path.split("/").map(encodeURIComponent).join("/");
/** How a version is named: `v<n>`, or its uid while it is pending (the source of truth has not numbered it yet). */
export const selOf = (v: { version: number | null; uid: string }) => (v.version != null ? `v${v.version}` : v.uid);
/** The short name shown in tables: `v<n>`, or the start of a pending version's uid. */
export const shortOf = (v: { version: number | null; uid: string; short_id?: string }) => (v.version != null ? `v${v.version}` : v.short_id ?? `${v.uid.slice(0, 13)}…`);
/** A reference as the user types it (the CLI and Python API name artifacts without a project). */
export const userRef = (name: string, sel: string) => `${name}@${sel}`;
export const exact = (project: string, name: string, sel: string) => `${project}:${name}@${sel}`;
export const artifactHref = (project: string, name: string, sel?: string | number | null) =>
  `/p/${encodeURIComponent(project)}/a/${enc(name)}${sel == null ? "" : typeof sel === "number" ? `@v${sel}` : `@${sel}`}`;
export const reportHref = (project: string, name: string, version?: number | null) =>
  `/p/${encodeURIComponent(project)}/r/${encodeURIComponent(name.split("/")[1])}${version ? `@v${version}` : ""}/`;
export const fileHref = (project: string, name: string, sel: string, path: string, download = false) =>
  `/files/${encodeURIComponent(project)}/${enc(name)}@${sel}//${enc(path)}${download ? "?download=1" : ""}`;

export const zipHref = (project: string, name: string, sel: string) =>
  `/files/${encodeURIComponent(project)}/${enc(name)}@${sel}.zip`;

export function parseRef(text: string) {
  const match = /^([^:@/]+):([^@]+)@(.+)$/.exec(text);
  return match ? { project: match[1], name: match[2], sel: match[3] } : null;
}

export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fall back */ }
  const area = document.createElement("textarea");
  area.value = text; area.setAttribute("readonly", ""); area.style.position = "fixed"; area.style.opacity = "0";
  document.body.append(area); area.select();
  const ok = document.execCommand("copy"); area.remove(); return ok;
}

export const useCopy = () => useCallback(copyText, []);

const TEXT = /\.(txt|md|json|jsonl|csv|tsv|log|py|ya?ml|toml|cfg|ini|xml|html?|sh|rst|tex|out)$/i;
export function previewKind(path: string): "image" | "video" | "text" | null {
  if (/\.(png|jpe?g|gif|webp|svg|avif|bmp)$/i.test(path)) return "image";
  if (/\.(mp4|webm|mov|m4v)$/i.test(path)) return "video";
  return TEXT.test(path) ? "text" : null;
}
