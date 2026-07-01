// Types mirroring the FastAPI response payloads in app/main.py.
// Kept hand-written (no code generator) because the API surface is small
// and stable.  Fields that are only present for evaluated cycle forecasts
// (result_5h, exit_price_5h, …) are declared optional.

export type Side = "BUY" | "SELL";

export type ForecastDirection = {
  direction: string; // "Рост" | "Снижение"
  strength: string;
  confidence?: number;
};

export type IndicatorSnapshot = {
  RSI?: number;
  MACD?: number;
  Stochastic_K?: number;
  ADX?: number;
  ADX_H4?: number;
  Williams_R?: number;
  "Bollinger_%B"?: number;
  Momentum?: number;
  EMA20?: number;
  EMA50?: number;
  Persistence_5h?: number;
  [k: string]: number | undefined;
};

export type VoteDetail = {
  name: string;
  value: number;
  reason: string;
};

export type PairEntry = {
  pair: string;
  name_ru: string;
  price: number;
  price_display: string;
  change_24h_pips: number;
  change_24h_pct: number;
  signal: Side | null;
  side: Side | null;
  confidence: number;
  strength: string;
  score: number;
  max_score: number;
  multi_tf_aligned: boolean;
  multi_tf_strict?: boolean;
  multi_tf_count?: number;
  adx_h1: number;
  adx_h4: number;
  trend_persistence_5h: number;
  trend_persistence_bars: number;
  is_strong_trend: boolean;
  details: VoteDetail[];
  indicators: IndicatorSnapshot;
  forecast_5h: ForecastDirection | null;
  forecast_24h: ForecastDirection | null;
};

export type SignalsResponse = {
  pairs: Record<string, PairEntry>;
  updated_at: string | null;
  scan_count: number;
};

export type CycleTier = "PREMIUM" | "STRONG" | "NORMAL";
export type EvalResult = "win" | "loss";

export type CycleForecast = {
  pair: string;
  name_ru: string;
  side: Side;
  confidence: number;
  score: number;
  max_score: number;
  strength: string;
  session: string;
  tier: CycleTier;
  adx_h1: number;
  adx_h4: number;
  trend_persistence_5h: number;
  trend_persistence_bars: number;
  multi_tf_aligned: boolean;
  multi_tf_strict?: boolean;
  multi_tf_count?: number;
  high_wr: boolean;
  entry_price: number;
  forecast_5h: ForecastDirection | null;
  forecast_24h: ForecastDirection | null;
  evaluated_5h: boolean;
  evaluated_24h: boolean;
  result_5h?: EvalResult;
  result_24h?: EvalResult;
  exit_price_5h?: number;
  exit_price_24h?: number;
  move_pct_5h?: number;
  move_pct_24h?: number;
};

export type CycleSnapshot = {
  cycle_start_utc: string;
  next_cycle_utc: string;
  selected: CycleForecast[];
  weak_market: boolean;
  strong_count: number;
};

export type Winrate = {
  wins: number;
  losses: number;
  decisions: number;
  winrate_pct: number;
  cycles: number;
};

export type StrongGate = {
  confidence: number;
  ratio: number;
  adx_h1: number;
  adx_h4: number;
  persistence_5h: number;
};

export type CycleResponse = {
  current_cycle: CycleSnapshot | null;
  next_cycle_utc: string;
  seconds_to_next_cycle: number;
  winrate_5h: Winrate;
  winrate_24h: Winrate;
  history_cycles: number;
  win_threshold_pct: number;
  min_picks: number;
  max_picks: number;
  strong_gate: StrongGate;
};

export type DepthLevel = {
  price: number;
  side: "bid" | "ask";
  volume_pct: number;
  distance_pips: number;
};

export type VolumeProfileRow = {
  price: number;
  price_low: number;
  price_high: number;
  volume: number;
  bar_count: number;
  volume_pct: number;
};

export type OrderBook = {
  pair: string;
  bid: number;
  ask: number;
  spread_pips: number;
  mid: number;
  supports: number[];
  resistances: number[];
  depth: DepthLevel[];
  volume_profile: VolumeProfileRow[];
};

export type HealthResponse = {
  status: string;
  scan_count: number;
  updated_at: string | null;
  time_utc5: string;
};

export type BarInterval = "15m" | "1h" | "4h" | "1d";

// OHLCV bar served by the new /api/bars/{pair} endpoint.  time is a unix
// epoch *in seconds* (UTC) so it can be fed straight into
// lightweight-charts without any client-side conversion.
export type Bar = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type BarsResponse = {
  pair: string;
  interval: BarInterval;
  bars: Bar[];
};
