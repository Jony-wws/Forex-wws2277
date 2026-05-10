import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Vite `base` (= basename for asset URLs) + the React-Router basename
// that main.tsx reads both come from the same env var so we only have
// one knob to flip for each deploy target:
//
//   • FastAPI /v2 mount  → VITE_BASE_PATH unset → "/v2/"
//   • GitHub Pages build → VITE_BASE_PATH="/Forex-wws2277/"
//
// Setting these at build time means the emitted index.html has the
// correct <script src="…"> paths baked in for whichever host will
// serve it.
const basePath = process.env.VITE_BASE_PATH || "/v2/";

export default defineConfig({
  plugins: [react()],
  base: basePath,
  build: {
    outDir: path.resolve(__dirname, "../static/dashboard"),
    emptyOutDir: true,
    sourcemap: false,
    target: "es2020",
  },
  // Propagate the base path to runtime code via import.meta.env.
  define: {
    "import.meta.env.VITE_BASE_PATH": JSON.stringify(basePath),
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
});
