import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// The built app goes into the Python package, which ships it (no Node needed to install).
const out = fileURLToPath(new URL("../src/cairn/ui/static", import.meta.url));
const target = process.env.CAIRN_UI_URL ?? "http://127.0.0.1:8801"; // a running `cairn ui --insecure-cookies`

export default defineConfig({
  plugins: [react()],
  build: { outDir: out, emptyOutDir: true, sourcemap: false },
  server: {
    fs: { allow: [".."] }, // report.css and report-extra.css live in src/cairn/template/
    proxy: {
      "/ui-api": target,
      "/files": target,
      "/logout": target,
      // reports keep their own server-rendered pages: /p/<project>/r/...
      "^/p/[^/]+/r/": target,
    },
  },
});
