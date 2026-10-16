import type { ReactNode } from "react";

/** While data loads: a calm muted line, announced to screen readers. */
export function Loading({ what = "Loading" }: { what?: string }) {
  return <p className="hint state-note" role="status">{what}…</p>;
}

/** An API error as a warning box; the title from the HTTP status. */
export function ErrorNote({ error }: { error: Error & { status?: number } }) {
  return (
    <div className="admonition warning" role="alert">
      <p className="admonition-title">{error.status === 404 ? "Not found" : error.status === 403 ? "Not allowed" : "Error"}</p>
      <p>{error.message}</p>
    </div>
  );
}

/** A muted hint: an empty list ("No reports yet."), a short explanation, a failed preview. */
export function Hint({ children }: { children: ReactNode }) {
  return <p className="hint">{children}</p>;
}

export type NoteTone = "info" | "warning" | "pending" | "deleted";
const NOTE_CLASS: Record<NoteTone, string> = { info: "", warning: " warning", pending: " pending-note", deleted: " tombstone-note" };

/** An admonition box with a title: info (accent), warning, pending (a version not on the remote yet), deleted. */
export function Note({ tone = "info", title, children }: { tone?: NoteTone; title: ReactNode; children: ReactNode }) {
  return (
    <div className={"admonition" + NOTE_CLASS[tone]} role={tone === "warning" ? "alert" : "note"}>
      <p className="admonition-title">{title}</p>
      {children}
    </div>
  );
}
