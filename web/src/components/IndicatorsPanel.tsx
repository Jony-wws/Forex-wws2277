import type { PairEntry } from "../lib/types";
import { fmtNumber } from "../lib/format";

/** Grid of all indicator readings + the voting breakdown below. */
export default function IndicatorsPanel({ entry }: { entry: PairEntry }) {
  const rows: Array<{ label: string; value: string; tone?: string }> = [
    { label: "RSI (H1)", value: fmtNumber(entry.indicators.RSI, 1), tone: rsiTone(entry.indicators.RSI) },
    {
      label: "MACD Hist",
      value: fmtNumber(entry.indicators.MACD, 4),
      tone:
        (entry.indicators.MACD ?? 0) > 0
          ? "text-buy"
          : (entry.indicators.MACD ?? 0) < 0
            ? "text-sell"
            : "text-text",
    },
    {
      label: "Stoch K",
      value: fmtNumber(entry.indicators.Stochastic_K, 1),
      tone: stochTone(entry.indicators.Stochastic_K),
    },
    { label: "ADX H1", value: fmtNumber(entry.adx_h1, 1), tone: adxTone(entry.adx_h1) },
    { label: "ADX H4", value: fmtNumber(entry.adx_h4, 1), tone: adxTone(entry.adx_h4) },
    { label: "Williams %R", value: fmtNumber(entry.indicators.Williams_R, 1) },
    {
      label: "Bollinger %B",
      value: fmtNumber(entry.indicators["Bollinger_%B"], 2),
    },
    {
      label: "Momentum",
      value: fmtNumber(entry.indicators.Momentum, 3),
      tone:
        (entry.indicators.Momentum ?? 0) > 0
          ? "text-buy"
          : (entry.indicators.Momentum ?? 0) < 0
            ? "text-sell"
            : "text-text",
    },
    { label: "EMA20", value: fmtNumber(entry.indicators.EMA20, 5) },
    { label: "EMA50", value: fmtNumber(entry.indicators.EMA50, 5) },
    {
      label: "Persist 5ч",
      value: `${entry.trend_persistence_bars}/5 (${fmtNumber(entry.trend_persistence_5h, 0)}%)`,
      tone: entry.trend_persistence_5h >= 80 ? "text-buy" : "text-text",
    },
    {
      label: "Score",
      value: `${entry.score} / ${entry.max_score}`,
      tone:
        entry.score > 0 ? "text-buy" : entry.score < 0 ? "text-sell" : "text-text",
    },
  ];

  return (
    <div className="card p-3 sm:p-4">
      <div className="text-xs text-muted uppercase tracking-wider mb-3">
        Индикаторы
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-3 gap-y-2 text-sm">
        {rows.map((r) => (
          <div key={r.label} className="flex items-baseline justify-between gap-2">
            <span className="text-[11px] text-muted">{r.label}</span>
            <span
              className={`font-semibold tabular-nums ${r.tone ?? "text-text"}`}
            >
              {r.value}
            </span>
          </div>
        ))}
      </div>

      {entry.details.length > 0 && (
        <>
          <div className="mt-4 mb-2 text-xs text-muted uppercase tracking-wider">
            Голосование ({entry.details.length} блоков)
          </div>
          <ul className="space-y-1 text-xs">
            {entry.details.map((d, i) => (
              <li
                key={i}
                className="flex items-start justify-between gap-2 px-2 py-1 rounded border border-border/50 bg-cardAlt/50"
              >
                <div>
                  <span className="font-semibold text-text">{d.name}</span>
                  <span className="text-muted ml-1.5 text-[11px]">
                    — {d.reason}
                  </span>
                </div>
                <span
                  className={`font-bold tabular-nums shrink-0 ${
                    d.value > 0 ? "text-buy" : d.value < 0 ? "text-sell" : "text-muted"
                  }`}
                >
                  {d.value > 0 ? `+${d.value}` : d.value}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function rsiTone(v: number | undefined): string {
  if (v == null) return "text-text";
  if (v >= 70) return "text-sell";
  if (v <= 30) return "text-buy";
  return "text-text";
}
function stochTone(v: number | undefined): string {
  if (v == null) return "text-text";
  if (v >= 80) return "text-sell";
  if (v <= 20) return "text-buy";
  return "text-text";
}
function adxTone(v: number | undefined): string {
  if (v == null) return "text-text";
  if (v >= 30) return "text-accent2";
  if (v >= 20) return "text-accent";
  return "text-muted";
}
