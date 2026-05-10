import type {
  BarInterval,
  BarsResponse,
  CycleResponse,
  HealthResponse,
  OrderBook,
  SignalsResponse,
} from "./types";

// API base — same-origin when served from FastAPI (/v2/), and the Vite
// dev server proxies /api to 127.0.0.1:8080 locally.  An explicit
// VITE_API_BASE override lets contributors point the dev UI at the
// production Fly.io API if they want to skip running the backend.
const API_BASE: string = (
  (import.meta.env.VITE_API_BASE as string | undefined) ?? ""
).replace(/\/$/, "");

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    signal,
    headers: { accept: "application/json" },
    // Always bypass the HTTP cache — the data changes every ~10s on the
    // server and stale reads would make the dashboard lie silently.
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`${path} failed: HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

export const api = {
  signals: (signal?: AbortSignal) =>
    getJSON<SignalsResponse>("/api/signals", signal),

  cycle: (signal?: AbortSignal) =>
    getJSON<CycleResponse>("/api/cycle", signal),

  orderbook: (pair: string, signal?: AbortSignal) =>
    getJSON<OrderBook>(`/api/orderbook/${pair.toUpperCase()}`, signal),

  orderbooks: (signal?: AbortSignal) =>
    getJSON<Record<string, OrderBook>>("/api/orderbooks", signal),

  bars: (pair: string, interval: BarInterval = "1h", signal?: AbortSignal) =>
    getJSON<BarsResponse>(
      `/api/bars/${pair.toUpperCase()}?interval=${interval}`,
      signal,
    ),

  health: (signal?: AbortSignal) =>
    getJSON<HealthResponse>("/api/health", signal),
};

export { API_BASE };
