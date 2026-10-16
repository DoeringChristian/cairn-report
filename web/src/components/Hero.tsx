import { Fragment, type ReactNode } from "react";
import { Link } from "react-router-dom";

/** The page header: eyebrow (often `Crumbs`), the big title, a subtitle, a status chip on the right, extra lines below. */
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

export type Crumb = { label: ReactNode; to?: string };

/** Breadcrumbs for the hero's eyebrow: `Projects / study / artifact`, the ones with `to` linked. */
export function Crumbs({ items }: { items: Crumb[] }) {
  return <>{items.map((item, index) => (
    <Fragment key={index}>{index > 0 && " / "}{item.to ? <Link to={item.to}>{item.label}</Link> : item.label}</Fragment>
  ))}</>;
}

/** A line of small facts under the hero title (latest version, aliases). */
export function HeroLine({ children }: { children: ReactNode }) {
  return <p className="alias-line">{children}</p>;
}
