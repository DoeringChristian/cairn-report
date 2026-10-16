import type { ReactNode } from "react";

export type PillTone = "default" | "tentative" | "pending" | "deleted" | "alias";

const PENDING_TIP = "this repo has the version; its remote does not have it yet, so it has no number (a uid names it)";

/** A small rounded label. `tone` picks the look: default (accent), tentative (warn), pending, deleted, alias (outlined). */
export function Pill({ tone = "default", title, children }: { tone?: PillTone; title?: string; children: ReactNode }) {
  return <span className={"pill" + (tone === "default" ? "" : " " + tone)} title={title}>{children}</span>;
}

/** A version's or run's status, its tone from the status. */
export function StatusPill({ status }: { status: string }) {
  const tone: PillTone = status === "deleted" ? "deleted" : status === "pending" ? "pending"
    : status === "failed" || status === "interrupted" || status === "running" ? "tentative" : "default";
  return <Pill tone={tone} title={status === "pending" ? PENDING_TIP : undefined}>{status}</Pill>;
}

/** Aliases (`stable`, `best`, ...) as outlined pills, sorted. */
export function AliasPills({ aliases = [] }: { aliases?: string[] }) {
  return <>{[...aliases].sort().map(alias => <Pill tone="alias" key={alias}>{alias}</Pill>)}</>;
}
