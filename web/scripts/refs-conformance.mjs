// refs.ts against store/refs.py: every vector of tests/refs_vectors.json (written from refs.py) must parse alike.
// `node scripts/refs-conformance.mjs [vectors.json]` (Node 23.6 or newer runs the TypeScript module directly).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parseRef, refForm } from "../src/refs.ts";

const file = process.argv[2] ?? fileURLToPath(new URL("../../tests/refs_vectors.json", import.meta.url));
const vectors = JSON.parse(readFileSync(file, "utf8"));
const answer = text => {
  const ref = parseRef(text);
  if (ref === null) return { error: true };
  return { form: refForm(ref), id: ref.id ?? null, name: ref.name ?? null, selector: ref.selector ?? null, path: ref.path };
};
const wrong = [];
for (const { text, ...expected } of vectors) {
  const got = answer(text);
  if (JSON.stringify(got) !== JSON.stringify(expected)) wrong.push({ text, expected, got });
}
for (const item of wrong) console.log(JSON.stringify(item));
if (wrong.length) {
  console.log(`refs.ts differs from refs.py on ${wrong.length} of ${vectors.length} references`);
  process.exit(1);
}
console.log(`refs.ts and refs.py: all agree on ${vectors.length} references`);
