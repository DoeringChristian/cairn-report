import type { ReactNode } from "react";

/** A data table in the report theme: uppercase muted header, row lines, sideways scroll inside its card on a phone.
 *  Rows are plain `<tr>`; mark the current one with `className="selected"`. */
export function DataTable({ head, children }: { head: ReactNode[]; children: ReactNode }) {
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

/** Key/value rows (a config, where a run ran). `code` shows both columns as code, else bold keys and mono values. */
export function KeyValueTable({ rows, head = ["", ""], code = false }: { rows: [string, ReactNode][]; head?: [ReactNode, ReactNode]; code?: boolean }) {
  return (
    <DataTable head={head}>
      {rows.map(([key, value]) => code
        ? <tr key={key}><td><code>{key}</code></td><td><code>{value}</code></td></tr>
        : <tr key={key}><td className="nowrap"><strong>{key}</strong></td><td className="mono wrap-any">{value}</td></tr>)}
    </DataTable>
  );
}
