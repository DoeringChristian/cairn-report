import type { InputHTMLAttributes, ReactNode } from "react";

/** A labelled text input in the report theme (the login form's token). */
export function Field({ label, ...input }: { label: ReactNode } & InputHTMLAttributes<HTMLInputElement>) {
  return <label className="field">{label}<input {...input} /></label>;
}
