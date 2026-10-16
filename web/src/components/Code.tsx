import { forwardRef, type ReactNode } from "react";

/** A dark code card (the reports' code block): metadata, JSON. */
export function CodeBlock({ children }: { children: ReactNode }) {
  return <div className="report-prose table-card"><pre>{children}</pre></div>;
}

/** Any value as indented JSON in a `CodeBlock`. */
export function JsonBlock({ value }: { value: unknown }) {
  return <CodeBlock>{JSON.stringify(value, null, 2)}</CodeBlock>;
}

/** Raw text that scrolls inside its box (a file preview, a log tail); dark inside a table, plain elsewhere. */
export const TextBlock = forwardRef<HTMLPreElement, { children: ReactNode }>(function TextBlock({ children }, ref) {
  return <pre ref={ref} className="preview-text">{children}</pre>;
});

/** Short code chips in a line (a run's overrides). */
export function CodeChips({ items }: { items: string[] }) {
  return <span className="code-chips">{items.map(item => <code key={item}>{item}</code>)}</span>;
}
