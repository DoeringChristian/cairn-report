import { useEffect, useRef, useState } from "react";
import { copyText } from "../util";

export const COPY_ICON = (
  <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8.5" y="8.5" width="11" height="11" rx="2" /><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2" /></svg>
);

/** The reports' copy-location button: the same class, tooltip and flash. `label` names what it copies. */
export function CopyButton({ text, label = "Copy", done = "Copied", className }: { text: string; label?: string; done?: string; className?: string }) {
  const [tip, setTip] = useState<string | null>(null);
  const timer = useRef<number>();
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <button
      type="button" className={["image-copy", className, tip && "show-tip"].filter(Boolean).join(" ")} title={label} aria-label={label}
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
