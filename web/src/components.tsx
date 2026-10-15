import { useEffect, useId, useRef, useState, type ReactNode } from "react";
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
  return <span className={"pill" + (status === "deleted" ? " deleted" : bad ? " tentative" : "")}>{status}</span>;
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

export type Choice = { value: string; label: string; note?: string };

/** A select in the report theme: a button and a listbox of our own instead of the system menu.
 *  Keyboard: arrows/Home/End move, Enter/Space choose, Escape closes, typing jumps to a label. */
export function Select({ label, value, choices, onChange }: { label: string; value: string; choices: Choice[]; onChange: (value: string) => void }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const id = useId();
  const current = choices.find(c => c.value === value) ?? choices[0];
  const show = () => { setActive(Math.max(0, choices.findIndex(c => c.value === value))); setOpen(true); };
  const choose = (index: number) => { setOpen(false); root.current?.querySelector("button")?.focus(); if (choices[index] && choices[index].value !== value) onChange(choices[index].value); };
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  useEffect(() => { if (open) list.current?.children[active]?.scrollIntoView({ block: "nearest" }); }, [open, active]);
  const onKey = (event: React.KeyboardEvent) => {
    const last = choices.length - 1;
    const move: Record<string, number> = { ArrowDown: Math.min(last, active + 1), ArrowUp: Math.max(0, active - 1), Home: 0, End: last, PageDown: Math.min(last, active + 8), PageUp: Math.max(0, active - 8) };
    if (event.key in move) { event.preventDefault(); if (!open) show(); else setActive(move[event.key]); return; }
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); if (open) choose(active); else show(); return; }
    if (event.key === "Escape" && open) { event.preventDefault(); setOpen(false); return; }
    if (event.key === "Tab") { setOpen(false); return; }
    if (event.key.length === 1 && /\S/.test(event.key)) {
      const key = event.key.toLowerCase(), from = open ? active + 1 : 0;
      const hit = [...choices.slice(from), ...choices.slice(0, from)].findIndex(c => c.label.toLowerCase().includes(key));
      if (hit >= 0) { if (!open) show(); setActive((from + hit) % choices.length); }
    }
  };
  return (
    <div className="select" ref={root}>
      <button type="button" className="select-button" aria-label={label} aria-haspopup="listbox" aria-expanded={open} aria-controls={id}
        aria-activedescendant={open ? `${id}-${active}` : undefined} onClick={() => (open ? setOpen(false) : show())} onKeyDown={onKey}>
        <span className="select-value">{current?.label}</span>
      </button>
      {open && (
        <ul className="select-list" role="listbox" id={id} aria-label={label} ref={list}>
          {choices.map((c, i) => (
            <li key={c.value} id={`${id}-${i}`} role="option" aria-selected={c.value === value} className={i === active ? "active" : undefined}
              onPointerEnter={() => setActive(i)} onPointerDown={e => e.preventDefault()} onClick={() => choose(i)}>
              <span>{c.label}</span>{c.note && <small>{c.note}</small>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
