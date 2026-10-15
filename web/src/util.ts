import { useCallback } from "react";

export const when = (value?: string | null) => (value ? value.slice(0, 16).replace("T", " ") + " UTC" : "");

export function bytes(size: number): string {
  let unit = 0;
  while (size >= 1024 && unit < 3) { size /= 1024; unit++; }
  return `${unit ? size.toFixed(1) : size.toFixed(0)} ${["B", "KB", "MB", "GB"][unit]}`;
}

export const isReport = (name: string) => /^reports\/[^/]+$/.test(name);
export const enc = (path: string) => path.split("/").map(encodeURIComponent).join("/");
export const exact = (project: string, name: string, version: number) => `${project}:${name}@v${version}`;
export const artifactHref = (project: string, name: string, sel?: string | number | null) =>
  `/p/${encodeURIComponent(project)}/a/${enc(name)}${sel == null ? "" : typeof sel === "number" ? `@v${sel}` : `@${sel}`}`;
export const reportHref = (project: string, name: string, version?: number | null) =>
  `/p/${encodeURIComponent(project)}/r/${encodeURIComponent(name.split("/")[1])}${version ? `@v${version}` : ""}/`;
export const fileHref = (project: string, name: string, version: number, path: string, download = false) =>
  `/files/${encodeURIComponent(project)}/${enc(name)}@v${version}//${enc(path)}${download ? "?download=1" : ""}`;

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
