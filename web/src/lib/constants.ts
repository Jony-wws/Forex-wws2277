// Single source of truth on the frontend — mirrors app/config.py.
// Keeping the list here so the UI can render the full 28-pair grid even
// before the first /api/signals response arrives (skeleton placeholders).
export const PAIRS: readonly string[] = [
  "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
  "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
  "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
  "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY",
  "AUDCAD", "AUDCHF", "AUDNZD",
  "CADCHF", "NZDCAD", "NZDCHF",
] as const;

export const PAIR_NAMES_RU: Record<string, string> = {
  EURUSD: "Евро / Доллар США",
  GBPUSD: "Фунт / Доллар США",
  USDJPY: "Доллар США / Йена",
  USDCHF: "Доллар США / Франк",
  AUDUSD: "Австрал. доллар / Доллар США",
  USDCAD: "Доллар США / Канад. доллар",
  NZDUSD: "Новозел. доллар / Доллар США",
  EURGBP: "Евро / Фунт",
  EURJPY: "Евро / Йена",
  EURCHF: "Евро / Франк",
  EURAUD: "Евро / Австрал. доллар",
  EURCAD: "Евро / Канад. доллар",
  EURNZD: "Евро / Новозел. доллар",
  GBPJPY: "Фунт / Йена",
  GBPCHF: "Фунт / Франк",
  GBPAUD: "Фунт / Австрал. доллар",
  GBPCAD: "Фунт / Канад. доллар",
  GBPNZD: "Фунт / Новозел. доллар",
  AUDJPY: "Австрал. доллар / Йена",
  CADJPY: "Канад. доллар / Йена",
  CHFJPY: "Франк / Йена",
  NZDJPY: "Новозел. доллар / Йена",
  AUDCAD: "Австрал. доллар / Канад. доллар",
  AUDCHF: "Австрал. доллар / Франк",
  AUDNZD: "Австрал. доллар / Новозел. доллар",
  CADCHF: "Канад. доллар / Франк",
  NZDCAD: "Новозел. доллар / Канад. доллар",
  NZDCHF: "Новозел. доллар / Франк",
};

export const MIN_CONFIDENCE = 80;

// How often the UI refetches each endpoint.  Kept intentionally loose on
// mobile to avoid wasted polling when the tab is backgrounded — the
// backend scanner runs every 10s server-side anyway.
export const POLL_INTERVAL_MS = {
  signals: 5_000,
  cycle: 15_000,
  orderbook: 15_000,
  bars: 30_000,
  health: 30_000,
} as const;
