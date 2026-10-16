import type { ReactNode } from "react";

/** A bordered card on the paper (the lineage graph's frame); `bar` is a row of hint and controls at its top. */
export function Card({ bar, className, children }: { bar?: ReactNode; className?: string; children: ReactNode }) {
  return (
    <div className={"card" + (className ? " " + className : "")}>
      {bar && <div className="card-bar">{bar}</div>}
      {children}
    </div>
  );
}

/** A titled list panel (the reports' Data panel): Reference, Inputs, Consumers. Holds `RefRows`, or a hint. */
export function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="data-panel app-panel" aria-label={title}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}
