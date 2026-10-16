import type { ReactNode } from "react";

/** A page section: a serif heading with optional actions on its right (a select, a status), then the content. */
export function Section({ title, actions, label, className, children }: { title: ReactNode; actions?: ReactNode; label?: string; className?: string; children?: ReactNode }) {
  return (
    <section className={"app-section" + (className ? " " + className : "")} aria-label={label}>
      <SectionHead title={title} actions={actions} />
      {children}
    </section>
  );
}

export function SectionHead({ title, actions }: { title: ReactNode; actions?: ReactNode }) {
  return <div className="section-head"><h2>{title}</h2>{actions}</div>;
}

/** A small heading inside a section (the Run panel's Metrics, System, Output). */
export function SubHeading({ children }: { children: ReactNode }) {
  return <h3 className="sub-heading">{children}</h3>;
}

/** A row of controls above a list: search, sort. */
export function Toolbar({ children }: { children: ReactNode }) {
  return <div className="toolbar">{children}</div>;
}

/** A labelled search field in the report theme. */
export function SearchInput({ label, placeholder, value, onChange }: { label: string; placeholder?: string; value: string; onChange: (value: string) => void }) {
  return <input type="search" placeholder={placeholder ?? label} aria-label={label} value={value} onChange={event => onChange(event.target.value)} />;
}
