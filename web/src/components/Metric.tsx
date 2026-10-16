import type { ReactNode } from "react";

/** A row of metric cards: three columns on a wide window, one on a phone. */
export function MetricGrid({ children }: { children: ReactNode }) {
  return <div className="metric-grid app-metrics">{children}</div>;
}

/** A metric card: a label, a big serif value (`compact` for long values such as a date), a note under it. */
export function MetricCard({ label, value, note, compact }: { label: ReactNode; value: ReactNode; note?: ReactNode; compact?: boolean }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong className={compact ? "small-value" : undefined}>{value}</strong>
      <small>{note ?? " "}</small>
    </div>
  );
}
