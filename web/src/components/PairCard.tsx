import { Link } from "react-router-dom";
import type { PairEntry } from "../lib/types";
import { fmtPct, fmtPips, fmtNumber } from "../lib/format";
import SideBadge from "./SideBadge";

/**
 * Compact mobile-first card for one currency pair.
 *
 * Layout (mobile):
 *   ┌──────────────────────────────────────┐
 *   │ EURUSD                   [CHIP BUY]  │
 *   │ Евро / Доллар США          conf 82   │
 *   │ 1.08421       +12.3 пп  +0.11%       │
 *   │ ADX 24 · Persist 80% · 3 ТФ          │
 *   └──────────────────────────────────────┘
 */
export default function PairCard({ entry }: { entry: PairEntry }) {
  const priceTone =
    entry.change_24h_pct > 0
      ? "text-buy"
      : entry.change_24h_pct < 0
        ? "text-sell"
        : "text-text";
  return (
    <Link
      to={`/pair/${entry.pair}`}
      className="card card-hover p-3 block animate-fadeIn group"
    >
      <div className="flex items-center justify-between gap-2 mb-0.5">
        <div className="font-bold font-mono text-text tracking-wide">
          {entry.pair}
        </div>
        <SideBadge side={entry.signal} confidence={entry.confidence} />
      </div>
      <div className="text-[11px] text-muted truncate mb-2">
        {entry.name_ru}
      </div>

      <div className="flex items-end justify-between gap-2">
        <div className="font-mono text-lg font-semibold tabular-nums">
          {entry.price_display}
        </div>
        <div className="text-right leading-tight">
          <div className={`text-xs font-semibold tabular-nums ${priceTone}`}>
            {fmtPips(entry.change_24h_pips)} пп
          </div>
          <div className={`text-[11px] tabular-nums ${priceTone}`}>
            {fmtPct(entry.change_24h_pct)}
          </div>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-2.5 gap-y-1 text-[10px] text-muted">
        <Meta label="ADX" value={fmtNumber(entry.adx_h1, 0)} />
        <Meta label="Persist" value={`${entry.trend_persistence_bars}/5`} />
        {entry.multi_tf_aligned && (
          <span className="text-accent font-semibold">
            {entry.multi_tf_strict ? "4 ТФ ✓" : `${entry.multi_tf_count ?? 3} ТФ ✓`}
          </span>
        )}
        {entry.is_strong_trend && (
          <span className="text-accent2 font-semibold">STRONG</span>
        )}
      </div>
    </Link>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <span>
      <span className="text-muted/60">{label}</span>{" "}
      <span className="text-text/80 font-semibold tabular-nums">{value}</span>
    </span>
  );
}
