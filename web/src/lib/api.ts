import type {
  BarInterval,
  BarsResponse,
  CycleResponse,
  HealthResponse,
  OrderBook,
  SignalsResponse,
} from "./types";

// The SPA runs in two modes:
//
// 1. **Server mode** (default, used when FastAPI serves /v2 or during
//    `npm run dev`). API lives at /api/* on the same origin — Vite's
//    dev server proxies /api to 127.0.0.1:8080.
//
// 2. **Static mode** (GitHub Pages). There is no backend. The
//    deploy_pages workflow sets VITE_STATIC_DATA=1 at build time.
//    The SPA then reads pre-built JSON snapshots from the `data`
//    branch via jsDelivr CDN. Files are regenerated every 15 min by
//    the refresh_data.yml cron.
//
// Everything below is a drop-in replacement for the old `api` object:
// the same method names, same return types. Components don't need to
// know which mode they're in.

const STATIC_MODE =
  (import.meta.env.VITE_STATIC_DATA as string | undefined) === "1";

const DATA_OWNER =
  (import.meta.env.VITE_DATA_OWNER as string | undefined) ?? "Jony-wws";
const DATA_REPO =
  (import.meta.env.VITE_DATA_REPO as string | undefined) ?? "Forex-wws2277";

// jsDelivr acts as a global CDN in front of GitHub raw files. A simple
// cachebuster (`?t=…`) defeats the edge cache every minute so users
// pick up fresh data within ~1 minute of the cron commit.
function staticUrl(path: string): string {
  const minuteBucket = Math.floor(Date.now() / 60_000);
  return `https://cdn.jsdelivr.net/gh/${DATA_OWNER}/${DATA_REPO}@data/data/${path}?t=${minuteBucket}`;
}

// Fallback if jsDelivr hasn't picked up a brand-new data branch yet —
// raw.githubusercontent bypasses the CDN entirely.
function rawFallbackUrl(path: string): string {
  return `https://raw.githubusercontent.com/${DATA_OWNER}/${DATA_REPO}/data/data/${path}`;
}

const API_BASE: string = (
  (import.meta.env.VITE_API_BASE as string | undefined) ?? ""
).replace(/\/$/, "");

async function fetchJSON<T>(urls: string[], signal?: AbortSignal): Promise<T> {
  let lastErr: unknown = new Error("No URLs provided");
  for (const url of urls) {
    try {
      const res = await fetch(url, {
        signal,
        headers: { accept: "application/json" },
        cache: "no-store",
      });
      if (res.ok) {
        return (await res.json()) as T;
      }
      lastErr = new Error(`${url} → HTTP ${res.status}`);
    } catch (err) {
      if ((err as { name?: string }).name === "AbortError") throw err;
      lastErr = err;
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

async function getDynamic<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    signal,
    headers: { accept: "application/json" },
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`${path} failed: HTTP ${res.status}`);
  return (await res.json()) as T;
}

async function getStatic<T>(filename: string, signal?: AbortSignal): Promise<T> {
  return fetchJSON<T>([staticUrl(filename), rawFallbackUrl(filename)], signal);
}

// Cycle endpoint stitching: the server returns a live countdown
// (`seconds_to_next_cycle`). In static mode we only refresh every 15
// minutes, so we recompute the countdown on the client using
// `next_cycle_utc` to keep the timer honest.
function recomputeCountdown(cycle: CycleResponse): CycleResponse {
  if (!STATIC_MODE) return cycle;
  const nextIso = cycle.next_cycle_utc;
  if (!nextIso) return cycle;
  // `next_cycle_utc` is "YYYY-MM-DD HH:MM UTC" — add Z so JS parses it.
  const parseable = nextIso.replace(" UTC", "Z").replace(" ", "T");
  const nextMs = Date.parse(parseable);
  if (Number.isNaN(nextMs)) return cycle;
  return {
    ...cycle,
    seconds_to_next_cycle: Math.max(0, Math.round((nextMs - Date.now()) / 1000)),
  };
}

export const api = STATIC_MODE
  ? {
      async signals(signal?: AbortSignal) {
        return getStatic<SignalsResponse>("signals.json", signal);
      },
      async cycle(signal?: AbortSignal) {
        const c = await getStatic<CycleResponse>("cycle.json", signal);
        return recomputeCountdown(c);
      },
      async orderbook(pair: string, signal?: AbortSignal) {
        const all = await getStatic<Record<string, OrderBook>>(
          "orderbooks.json",
          signal,
        );
        const ob = all[pair.toUpperCase()];
        if (!ob) throw new Error(`Orderbook not found for ${pair}`);
        return ob;
      },
      async orderbooks(signal?: AbortSignal) {
        return getStatic<Record<string, OrderBook>>("orderbooks.json", signal);
      },
      async bars(
        pair: string,
        interval: BarInterval = "1h",
        signal?: AbortSignal,
      ) {
        return getStatic<BarsResponse>(
          `bars/${pair.toUpperCase()}-${interval}.json`,
          signal,
        );
      },
      async health(signal?: AbortSignal) {
        // The cron writes `data/health.json` with a slightly different
        // shape; map it onto HealthResponse so components stay
        // unchanged.
        const raw = await getStatic<{
          status: string;
          updated_at_utc5: string;
          pairs_built: number;
        }>("health.json", signal);
        return {
          status: raw.status,
          scan_count: raw.pairs_built,
          updated_at: raw.updated_at_utc5,
          time_utc5: raw.updated_at_utc5,
        } satisfies HealthResponse;
      },
    }
  : {
      signals: (signal?: AbortSignal) =>
        getDynamic<SignalsResponse>("/api/signals", signal),
      cycle: (signal?: AbortSignal) =>
        getDynamic<CycleResponse>("/api/cycle", signal),
      orderbook: (pair: string, signal?: AbortSignal) =>
        getDynamic<OrderBook>(`/api/orderbook/${pair.toUpperCase()}`, signal),
      orderbooks: (signal?: AbortSignal) =>
        getDynamic<Record<string, OrderBook>>("/api/orderbooks", signal),
      bars: (
        pair: string,
        interval: BarInterval = "1h",
        signal?: AbortSignal,
      ) =>
        getDynamic<BarsResponse>(
          `/api/bars/${pair.toUpperCase()}?interval=${interval}`,
          signal,
        ),
      health: (signal?: AbortSignal) =>
        getDynamic<HealthResponse>("/api/health", signal),
    };

export { API_BASE, STATIC_MODE };
