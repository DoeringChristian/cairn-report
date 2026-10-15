import { useRef, useState, type ReactNode } from "react";
import { copyText } from "./util";

const COPY_ICON = (
  <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8.5" y="8.5" width="11" height="11" rx="2" /><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2" /></svg>
);

/** The reports' copy-location button: the same class, tooltip and flash. */
export function CopyButton({ text, label = "Copy", done = "Copied" }: { text: string; label?: string; done?: string }) {
  const [tip, setTip] = useState<string | null>(null);
  const timer = useRef<number>();
  return (
    <button
      type="button" className={"image-copy" + (tip ? " show-tip" : "")} title={label} aria-label={label}
      data-tooltip={tip ?? label}
      onClick={async event => {
        event.preventDefault(); event.stopPropagation();
        setTip((await copyText(text)) ? done : "Copy failed");
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setTip(null), 1600);
      }}
    >{COPY_ICON}</button>
  );
}

export function Pill({ status }: { status: string }) {
  const bad = status === "failed" || status === "interrupted" || status === "running";
  return <span className={"pill" + (bad ? " tentative" : "")}>{status}</span>;
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <p className="hint state-note" role="status">{what}…</p>;
}

export function ErrorNote({ error }: { error: Error & { status?: number } }) {
  return (
    <div className="admonition warning" role="alert">
      <p className="admonition-title">{error.status === 404 ? "Not found" : error.status === 403 ? "Not allowed" : "Error"}</p>
      <p>{error.message}</p>
    </div>
  );
}

export function Hero({ eyebrow, title, subtitle, status, children }: { eyebrow?: ReactNode; title: ReactNode; subtitle?: ReactNode; status?: ReactNode; children?: ReactNode }) {
  return (
    <header className="hero app-hero">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {subtitle && <p className="subtitle">{subtitle}</p>}
        {children}
      </div>
      {status && <div className="status">{status}</div>}
    </header>
  );
}

export function Table({ head, children }: { head: ReactNode[]; children: ReactNode }) {
  return (
    <div className="report-prose table-card">
      <div className="table-wrap">
        <table>
          <thead><tr>{head.map((cell, index) => <th key={index}>{cell}</th>)}</tr></thead>
          <tbody>{children}</tbody>
        </table>
      </div>
    </div>
  );
}
