import { useCallback, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import { POLL_INTERVAL_MS } from "../lib/constants";
import { usePolling } from "../lib/usePolling";
import type { BarInterval } from "../lib/types";
import { fmtPct, fmtPips, sideLabel } from "../lib/format";
import ErrorBanner from "../components/ErrorBanner";
import SideBadge from "../components/SideBadge";
import CandleChart from "../components/CandleChart";
import OrderBookPanel from "../components/OrderBookPanel";
import IndicatorsPanel from "../components/IndicatorsPanel";
import { SkeletonCard } from "../components/Skeleton";

const INTERVALS: ReadonlyArray<{ key: BarInterval; label: string }> = [
  { key: "15m", label: "15М" },
  { key: "1h", label: "1Ч" },
  { key: "4h", label: "4Ч" },
  { key: "1d", label: "1Д" },
];

export default function PairDetailPage() {
  const { pair: rawPair } = useParams();
  const pair = (rawPair ?? "").toUpperCase();
  const [interval, setIntervalKey] = useState<BarInterval>("1h");

  const signalsFetcher = useCallback(
    (signal: AbortSignal) => api.signals(signal),
    [],
  );
  const barsFetcher = useCallback(
    (signal: AbortSignal) => api.bars(pair, interval, signal),
    [pair, interval],
  );
  const obFetcher = useCallback(
    (signal: AbortSignal) => api.orderbook(pair, signal),
    [pair],
  );

  const { data: signals, error: sErr, refresh: sRefresh } = usePolling(
    signalsFetcher,
    POLL_INTERVAL_MS.signals,
  );
  const { data: bars, error: bErr, loading: bLoading } = usePolling(
    barsFetcher,
    POLL_INTERVAL_MS.bars,
    [pair, interval],
  );
  const { data: ob, error: obErr } = usePolling(
    obFetcher,
    POLL_INTERVAL_MS.orderbook,
    [pair],
  );

  const entry = useMemo(
    () => signals?.pairs?.[pair],
    [signals, pair],
  );

  if (sErr && !signals) {
    return <ErrorBanner error={sErr} onRetry={sRefresh} />;
  }

  const priceDigits = pair.includes("JPY") ? 3 : 5;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-2">
        <Link
          to="/"
          className="btn text-xs py-1.5"
          aria-label="Назад ко всем парам"
        >
          ← Назад
        </Link>
        <div className="text-[11px] text-muted truncate">
          {entry?.name_ru ?? "Загрузка…"}
        </div>
      </div>

      {/* === SUMMARY === */}
      <section className="card p-4">
        <div className="flex items-start justify-between gap-3 mb-2 flex-wrap">
          <div>
            <div className="font-bold text-2xl font-mono tracking-wide">
              {pair}
            </div>
            {entry ? (
              <div className="mt-1 flex items-baseline gap-3">
                <span className="font-mono text-3xl font-bold tabular-nums">
                  {entry.price_display}
                </span>
                <span
                  className={`text-sm font-semibold tabular-nums ${
                    entry.change_24h_pct > 0
                      ? "text-buy"
                      : entry.change_24h_pct < 0
                        ? "text-sell"
                        : "text-text"
                  }`}
                >
                  {fmtPips(entry.change_24h_pips)} пп (
                  {fmtPct(entry.change_24h_pct)})
                </span>
              </div>
            ) : (
              <div className="mt-1 h-9 w-32 skeleton" />
            )}
          </div>
          <div className="text-right">
            {entry ? (
              <>
                <SideBadge
                  side={entry.signal}
                  confidence={entry.confidence}
                />
                <div className="text-xs text-muted mt-1">
                  {sideLabel(entry.signal)} · {entry.strength}
                </div>
              </>
            ) : (
              <div className="h-6 w-24 skeleton" />
            )}
          </div>
        </div>

        {entry && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-3 text-xs">
            <Forecast title="Прогноз 5ч" f={entry.forecast_5h} />
            <Forecast title="Прогноз 24ч" f={entry.forecast_24h} />
            <Badge
              label="4 ТФ согласованы"
              on={entry.multi_tf_aligned}
              tone="accent"
            />
            <Badge
              label="STRONG тренд"
              on={entry.is_strong_trend}
              tone="accent2"
            />
          </div>
        )}
      </section>

      {/* === CHART === */}
      <section className="card p-3 sm:p-4">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <div className="text-xs text-muted uppercase tracking-wider">
            Свечной график
          </div>
          <div className="flex gap-1">
            {INTERVALS.map((i) => (
              <button
                key={i.key}
                type="button"
                onClick={() => setIntervalKey(i.key)}
                className={[
                  "px-2.5 py-1 rounded-md text-xs font-semibold border",
                  interval === i.key
                    ? "bg-accent/20 border-accent/50 text-accent"
                    : "bg-card border-border text-muted hover:text-text",
                ].join(" ")}
              >
                {i.label}
              </button>
            ))}
          </div>
        </div>
        {bErr && !bars ? (
          <div className="text-sm text-sell">
            Не удалось загрузить свечи: {bErr.message}
          </div>
        ) : bLoading && !bars ? (
          <div className="skeleton w-full h-[320px]" />
        ) : bars && bars.bars.length > 0 ? (
          <CandleChart
            bars={bars.bars}
            priceFormatDigits={priceDigits}
            height={320}
          />
        ) : (
          <div className="text-sm text-muted p-4 text-center">
            Нет данных для этого таймфрейма.
          </div>
        )}
      </section>

      {/* === ORDERBOOK + INDICATORS === */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {obErr && !ob ? (
          <div className="card p-4 text-sm text-sell">
            Стакан недоступен: {obErr.message}
          </div>
        ) : ob ? (
          <OrderBookPanel ob={ob} priceDigits={priceDigits} />
        ) : (
          <SkeletonCard />
        )}
        {entry ? (
          <IndicatorsPanel entry={entry} />
        ) : (
          <SkeletonCard />
        )}
      </div>
    </div>
  );
}

function Forecast({
  title,
  f,
}: {
  title: string;
  f: { direction: string; strength: string; confidence?: number } | null;
}) {
  return (
    <div className="card p-2.5 bg-cardAlt/50">
      <div className="text-[10px] text-muted uppercase">{title}</div>
      {f ? (
        <>
          <div
            className={`font-semibold ${
              f.direction === "Рост" ? "text-buy" : "text-sell"
            }`}
          >
            {f.direction}
          </div>
          <div className="text-[11px] text-muted">
            {f.strength}
            {f.confidence != null && ` · ${f.confidence}%`}
          </div>
        </>
      ) : (
        <div className="text-muted text-[11px]">Нет прогноза</div>
      )}
    </div>
  );
}

function Badge({
  label,
  on,
  tone,
}: {
  label: string;
  on: boolean;
  tone: "accent" | "accent2";
}) {
  // NOTE: Tailwind's JIT scanner only picks up *static* class strings, so
  // interpolated class names like `border-${tone}/40` would silently be
  // purged from the bundle.  Enumerate every variant explicitly.
  let wrapper: string;
  let dot: string;
  let text: string;
  if (!on) {
    wrapper = "bg-cardAlt/50";
    dot = "bg-muted/40";
    text = "text-muted";
  } else if (tone === "accent") {
    wrapper = "border-accent/40 bg-accent/5";
    dot = "bg-accent";
    text = "text-accent font-semibold";
  } else {
    wrapper = "border-accent2/40 bg-accent2/5";
    dot = "bg-accent2";
    text = "text-accent2 font-semibold";
  }
  return (
    <div className={`card p-2.5 text-[11px] flex items-center gap-2 ${wrapper}`}>
      <span className={`inline-block w-2 h-2 rounded-full ${dot}`} />
      <span className={text}>{label}</span>
    </div>
  );
}
