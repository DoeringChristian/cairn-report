/** References: the one place in the web code that takes a reference apart (it mirrors store/refs.py). A reference
 *  has exactly one of four forms, told apart by its shape alone:
 *
 *      <id>             3f9a2c1               one version: 4 to 64 hex digits (a prefix of its id), no `@`
 *      <name>           descent               the artifact's newest completed version (`@latest`)
 *      <name>@v<n>      descent@v3            version n of the artifact
 *      <name>@<word>    models/denoiser@best  the version an alias or a label names
 *
 *  and a file inside the version may follow after `//`. Names, aliases and labels are never 4 or more hex digits
 *  alone, so the forms cannot overlap. Everything else here builds references and links from a version's fields
 *  (name, number, id), never by parsing: nothing rewrites a selector (digits stay digits, never `v<n>`). */

export type Ref =
  | { id: string; name?: undefined; selector?: undefined; path: string | null }
  | { id?: undefined; name: string; selector: string | null; path: string | null };

/** What the app knows of a version to name it: its artifact (null when unknown), its number (null while pending), its id. */
export interface Named { name: string | null; version: number | null; id: string }

// The patterns and checks of store/refs.py, one for one; tests/refs_vectors.json checks that both parse alike.
const HEX = /^[0-9a-fA-F]{4,}$/;  // an id's shape: never a name, an alias, or a label alone
const SEGMENT = /^[a-z0-9][a-z0-9_.-]*$/;
const WORD = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const LABEL = /^[A-Za-z0-9][A-Za-z0-9_.-]*(\/[A-Za-z0-9][A-Za-z0-9_.-]*)*$/;
const VERSION = /^v(0|[1-9][0-9]*)$/;
const MALFORMED = /^[vV][-+]?[0-9]/;  // looks like a version: never a word (`v<n>` aside, which is one)
const UID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;  // Cairn 0.14's
const BLANK = " \t\n\r\x0b\x0c";  // a reference never starts or ends with one
const MAX_NAME = 200;

const hexAlone = (text: string) => HEX.test(text);

/** Whether `text` has the shape of an id (4 to 64 hex digits alone). */
export const isId = (text: string) => hexAlone(text) && text.length <= 64;

const validName = (name: string) =>
  !!name && name.length <= MAX_NAME && !hexAlone(name) && !UID.test(name) &&
  name.split("/").every(segment => SEGMENT.test(segment) && !segment.includes(".."));

const validWord = (word: string) =>
  WORD.test(word) && word.length <= 128 && !MALFORMED.test(word) && !UID.test(word) && !hexAlone(word);

const validLabel = (label: string) => {
  if (!LABEL.test(label) || label.length > 128) return false;
  if (!label.includes("/")) return validWord(label);
  return label.split("/").every(word => !MALFORMED.test(word));
};

const badPath = (path: string) =>
  !path || path.includes("\\") ||
  path.split("/").some(part => part === "" || part === "." || part === ".." || [...part].some(c => c.charCodeAt(0) < 32));

/** The reference `text` taken apart exactly as store/refs.py `parse` takes it apart, or null where that refuses it. */
export function parseRef(text: string): Ref | null {
  if (typeof text !== "string" || !text || BLANK.includes(text[0]) || BLANK.includes(text[text.length - 1])) return null;
  const cut = text.indexOf("//");
  const head = cut < 0 ? text : text.slice(0, cut);
  const path = cut < 0 ? null : text.slice(cut + 2);
  if (path !== null && badPath(path)) return null;
  if (head.includes(":")) return null;
  const at = head.indexOf("@");
  if (at < 0) {
    if (hexAlone(head)) return head.length > 64 ? null : { id: head.toLowerCase(), path };
    return validName(head) ? { name: head, selector: null, path } : null;
  }
  const name = head.slice(0, at), selector = head.slice(at + 1);
  if (hexAlone(name) || !validName(name) || !selector) return null;
  if (VERSION.test(selector)) return { name, selector, path };
  if (MALFORMED.test(selector) || UID.test(selector) || hexAlone(selector)) return null;
  return validLabel(selector) ? { name, selector, path } : null;
}

/** Which of the four forms a parsed reference has: an id, a bare name (`latest`), `name@v<n>`, or `name@<word>`. */
export const refForm = (ref: Ref) =>
  ref.id !== undefined ? "id" : ref.selector == null ? "latest" : VERSION.test(ref.selector) ? "version" : "word";

/** A parsed reference as text again (what the API's `?ref=` takes). */
export const refText = (ref: Ref) =>
  (ref.id !== undefined ? ref.id : ref.selector ? `${ref.name}@${ref.selector}` : ref.name) + (ref.path ? `//${ref.path}` : "");

/** A version's exact reference: `name@v<n>`, or its whole id while it has no number (or no known name). */
export const exactRef = (v: Named) => (v.name != null && v.version != null ? `${v.name}@v${v.version}` : v.id);

/** A version as it is shown: `name@v<n>`, or its short id when it has no number or no known name. */
export const shownRef = (v: Named & { short_id?: string | null }) =>
  v.name != null && v.version != null ? `${v.name}@v${v.version}` : (v.short_id ?? v.id.slice(0, 7));

/** The version's short name in a table: `v<n>`, or its short id while it has no number. */
export const shortName = (v: { version: number | null; id: string; short_id?: string | null }) =>
  v.version != null ? `v${v.version}` : (v.short_id ?? v.id.slice(0, 7));

const segments = (path: string) => path.split("/").map(encodeURIComponent).join("/");
const projectPart = (project: string) => `/p/${encodeURIComponent(project)}`;

/** The artifact's page at its latest version, or at an alias or label (`word`). */
export const artifactHref = (project: string, name: string, word?: string | null) =>
  `${projectPart(project)}/a/${segments(name)}${word ? `@${encodeURIComponent(word)}` : ""}`;

/** A numbered version's page (`/a/<name>@v<n>`), else the page of its id (`/v/<id>`). */
export function versionHref(project: string, name: string | null, version: number | null, id: string | null): string {
  if (name != null && version != null) return `${projectPart(project)}/a/${segments(name)}@v${version}`;
  if (id) return `${projectPart(project)}/v/${id}`;
  return name != null ? artifactHref(project, name) : `${projectPart(project)}/`;
}

/** The URL head of a version's files: `<name>@v<n>` (the name encoded per segment), or its whole id. */
const fileHead = (v: Named) => (v.name != null && v.version != null ? `${segments(v.name)}@v${v.version}` : v.id);

export const fileHref = (project: string, v: Named, path: string, download = false) =>
  `/files/${encodeURIComponent(project)}/${fileHead(v)}//${segments(path)}${download ? "?download=1" : ""}`;

export const zipHref = (project: string, v: Named) => `/files/${encodeURIComponent(project)}/${fileHead(v)}.zip`;

/** A report's page (`reports/<x>`), at version n or its latest. */
export const reportHref = (project: string, name: string, version?: number | null) =>
  `${projectPart(project)}/r/${encodeURIComponent(name.split("/")[1])}${version ? `@v${version}` : ""}/`;
