import type { ReactNode } from "react";
import { CopyButton } from "./CopyButton";

export type RefRowProps = { name: ReactNode; value: ReactNode; extra?: ReactNode; copy?: { text: string; label: string; done?: string }; mono?: boolean };

/** One row of a `Panel` list: a name, a value (an exact reference, a command), a muted extra, a copy button. */
export function RefRow({ name, value, extra, copy, mono }: RefRowProps) {
  return (
    <li className="data-row">
      <span className="data-name">{name}</span>
      <span className={"data-version" + (mono ? " mono" : "")}>{value}</span>
      <span className="data-files">{extra}</span>
      {copy ? <CopyButton text={copy.text} label={copy.label} done={copy.done} /> : <span />}
    </li>
  );
}

/** The list of a `Panel`. */
export function RefRows({ children }: { children: ReactNode }) {
  return <ul>{children}</ul>;
}
