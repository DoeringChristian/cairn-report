import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Link } from "react-router-dom";

type Look = { small?: boolean; primary?: boolean; className?: string };
const classes = ({ small, primary, className }: Look) => ["action-button", small && "small", primary && "primary", className].filter(Boolean).join(" ");

export type ButtonProps = Look & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className">;

/** The reports' bordered action button (`.action-button`); `small` for table rows, `primary` for the one main action. */
export function Button({ small, primary, className, type = "button", ...rest }: ButtonProps) {
  return <button type={type} className={classes({ small, primary, className })} {...rest} />;
}

export type LinkButtonProps = Look & { children: ReactNode; title?: string; download?: boolean | string } & ({ href: string; to?: never } | { to: string; href?: never });

/** An action button that is a link: `href` for a page outside the app (files, reports), `to` for an app route. */
export function LinkButton({ small, primary, className, href, to, download, children, title }: LinkButtonProps) {
  const cls = classes({ small, primary, className });
  if (to !== undefined) return <Link className={cls} to={to} title={title}>{children}</Link>;
  return <a className={cls} href={href} title={title} download={download === true ? "" : download || undefined}>{children}</a>;
}

/** Buttons that pick one of a few views (`aria-pressed`), as the lineage graph's "Show runs / Data only". */
export function ToggleGroup<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: { value: T; label: string }[]; onChange: (value: T) => void }) {
  return (
    <span className="graph-toggle" role="group" aria-label={label}>
      {options.map(option => (
        <Button key={option.value} small aria-pressed={option.value === value} onClick={() => onChange(option.value)}>{option.label}</Button>
      ))}
    </span>
  );
}

/** The round, translucent icon button of the lightbox (`button.icon`), positioned by its extra class. */
export function IconButton({ label, className, children, ...rest }: { label: string; className?: string; children: ReactNode } & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className" | "children">) {
  return <button type="button" className={"icon" + (className ? " " + className : "")} aria-label={label} title={label} {...rest}>{children}</button>;
}

/** Right-aligned buttons of a table row. */
export function RowActions({ children }: { children: ReactNode }) {
  return <div className="row-actions">{children}</div>;
}
