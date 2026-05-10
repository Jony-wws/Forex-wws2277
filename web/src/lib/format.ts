import type { PairEntry, Side } from "./types";

export function fmtPrice(pair: string, price: number | null | undefined): string {
  if (price == null || Number.isNaN(price)) return "—";
  const digits = pair.includes("JPY") ? 3 : 5;
  return price.toFixed(digits);
}

export function fmtPips(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}`;
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}%`;
}

export function fmtNumber(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

export function sideClass(side: Side | null | undefined): string {
  if (side === "BUY") return "text-buy";
  if (side === "SELL") return "text-sell";
  return "text-muted";
}

export function sideLabel(side: Side | null | undefined): string {
  if (side === "BUY") return "ПОКУПКА";
  if (side === "SELL") return "ПРОДАЖА";
  return "НЕТ СИГНАЛА";
}

/** Friendly "just now" / "5s ago" / "2m ago" relative-time formatter. */
export function timeAgo(ts: number | null): string {
  if (!ts) return "—";
  const diff = Math.max(0, Date.now() - ts);
  const s = Math.round(diff / 1000);
  if (s < 5) return "только что";
  if (s < 60) return `${s}с назад`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}м назад`;
  const h = Math.round(m / 60);
  return `${h}ч назад`;
}

/** Format a {HH:MM:SS} countdown from a number of seconds. */
export function fmtCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

/** Lift a "confidence" 50..95 number into a Tailwind text colour. */
export function confidenceTone(c: number): string {
  if (c >= 88) return "text-accent2";
  if (c >= 80) return "text-accent";
  if (c >= 65) return "text-text";
  return "text-muted";
}

/** Return a human-friendly label for a strength bucket. */
export function compactStrength(e: PairEntry): string {
  return e.strength || (e.signal ? "Сигнал" : "Нейтральный");
}
