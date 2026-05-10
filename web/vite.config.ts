import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Build artefacts are emitted into ../static/dashboard so FastAPI can
// serve them from /v2 via StaticFiles, next to the existing / and /tg
// dashboards.  base is set to /v2/ so all asset URLs in the built
// index.html resolve correctly when mounted on that sub-path.
export default defineConfig({
  plugins: [react()],
  base: "/v2/",
  build: {
    outDir: path.resolve(__dirname, "../static/dashboard"),
    emptyOutDir: true,
    sourcemap: false,
    target: "es2020",
  },
  server: {
    port: 5173,
    // During local dev the Vite server proxies /api/* to the local
    // FastAPI dev server (uvicorn app.main:app --port 8080) so the
    // dashboard shows live Yahoo Finance data end-to-end.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
});
