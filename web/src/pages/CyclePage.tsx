import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { POLL_INTERVAL_MS } from "../lib/constants";
import { usePolling } from "../lib/usePolling";
import { fmtCountdown, fmtNumber, fmtPct } from "../lib/format";
import type { CycleForecast, Winrate } from "../lib/types";
import ErrorBanner from "../components/ErrorBanner";
import SideBadge from "../components/SideBadge";
import TierBadge from "../components/TierBadge";

export default function CyclePage() {
  const fetcher = useCallback((signal: AbortSignal) => api.cycle(signal), []);
  const { data, error, refresh } = usePolling(fetcher, POLL_INTERVAL_MS.cycle);

  // Local countdown ticks every second so it feels alive, but gets re-synced
  // every 15s when the server payload refreshes.
  const [countdown, setCountdown] = useState<number>(0);
  useEffect(() => {
    if (data?.seconds_to_next_cycle == null) return;
    setCountdown(data.seconds_to_next_cycle);
  }, [data?.seconds_to_next_cycle]);
  useEffect(() => {
    const t = setInterval(
      () => setCountdown((c) => Math.max(0, c - 1)),
      1000,
    );
    return () => clearInterval(t);
  }, []);

  if (error && !data) return <ErrorBanner error={error} onRetry={refresh} />;

  const cycle = data?.current_cycle;

  return (
    <div className="space-y-4">
      <CycleHeader
        seconds={countdown}
        strong={cycle?.strong_count ?? 0}
        weak={cycle?.weak_market ?? false}
        startUtc={cycle?.cycle_start_utc}
      />

      <WinrateBlock
        wr5h={data?.winrate_5h}
        wr24h={data?.winrate_24h}
        historyCycles={data?.history_cycles ?? 0}
        threshold={data?.win_threshold_pct}
      />

      <section>
        <h2 className="text-sm font-bold text-muted uppercase tracking-wider mb-2">
          Топ прогнозы на 5 часов
        </h2>
        {!cycle || cycle.selected.length === 0 ? (
          <div className="card p-6 text-center text-muted">
            Ждём первый цикл… данные появятся после первого скана.
          </div>
        ) : (
          <div className="grid gap-3">
            {cycle.selected.map((f) => (
              <CycleRow key={f.pair} f={f} />
            ))}
          </div>
        )}
      </section>

      {cycle && (
        <p className="text-xs text-muted">
          Цикл стартовал {cycle.cycle_start_utc}. Следующая ротация в{" "}
          {cycle.next_cycle_utc}.
        </p>
      )}
    </div>
  );
}

function CycleHeader({
  seconds,
  strong,
  weak,
  startUtc,
}: {
  seconds: number;
  strong: number;
  weak: boolean;
  startUtc: string | undefined;
}) {
  return (
    <div className="card p-4 sm:p-5">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div className="text-xs text-muted uppercase tracking-wider">
            Следующий цикл через
          </div>
          <div className="font-mono text-3xl sm:text-4xl font-bold text-accent tabular-nums">
            {fmtCountdown(seconds)}
          </div>
          {startUtc && (
            <div className="text-[11px] text-muted mt-1">
              Текущий стартовал {startUtc}
            </div>
          )}
        </div>
        <div className="text-right">
          <div className="text-xs text-muted uppercase tracking-wider">
            STRONG пиков
          </div>
          <div
            className={`text-3xl sm:text-4xl font-bold tabular-nums ${
              strong >= 3 ? "text-buy" : "text-[#ffa726]"
            }`}
          >
            {strong}
          </div>
        </div>
      </div>
      {weak && (
        <div className="mt-3 text-xs px-3 py-2 rounded-lg bg-[#ffa726]/10 border border-[#ffa726]/30 text-[#ffcc80]">
          <strong>Слабый рынок.</strong> Недостаточно пар прошли жёсткий
          фильтр — слот добран запасными идеями (NORMAL). Торговать только
          при согласии с собственной стратегией.
        </div>
      )}
    </div>
  );
}

function WinrateBlock({
  wr5h,
  wr24h,
  historyCycles,
  threshold,
}: {
  wr5h: Winrate | undefined;
  wr24h: Winrate | undefined;
  historyCycles: number;
  threshold: number | undefined;
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      <WinrateCard title="Winrate 5ч" wr={wr5h} />
      <WinrateCard title="Winrate 24ч" wr={wr24h} />
      <div className="col-span-2 text-[11px] text-muted">
        История: {historyCycles} циклов · окно 10 циклов · порог WIN ≥{" "}
        {fmtNumber(threshold, 2)}%
      </div>
    </div>
  );
}

function WinrateCard({
  title,
  wr,
}: {
  title: string;
  wr: Winrate | undefined;
}) {
  const pct = wr?.winrate_pct ?? 0;
  const tone =
    pct >= 70 ? "text-buy" : pct >= 50 ? "text-accent" : "text-sell";
  return (
    <div className="card p-3">
      <div className="text-xs text-muted uppercase tracking-wider">
        {title}
      </div>
      <div
        className={`mt-1 font-bold text-2xl sm:text-3xl tabular-nums ${tone}`}
      >
        {pct.toFixed(1)}%
      </div>
      <div className="text-[11px] text-muted mt-1">
        {wr?.wins ?? 0} побед / {wr?.losses ?? 0} проигрышей ·{" "}
        {wr?.decisions ?? 0} решений
      </div>
    </div>
  );
}

function CycleRow({ f }: { f: CycleForecast }) {
  const evaluated = f.evaluated_5h;
  const win = f.result_5h === "win";
  const loss = f.result_5h === "loss";
  const movePct = f.move_pct_5h;

  return (
    <Link
      to={`/pair/${f.pair}`}
      className="card card-hover p-3 sm:p-4 block animate-fadeIn"
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="flex items-center gap-2">
          <span className="font-bold font-mono tracking-wide">{f.pair}</span>
          <TierBadge tier={f.tier} />
          {f.high_wr && (
            <span className="chip-premium" title="Историческая winrate ≥ 70%">
              WR 70+
            </span>
          )}
        </div>
        <SideBadge side={f.side} confidence={f.confidence} />
      </div>
      <div className="text-[11px] text-muted mb-2">{f.name_ru}</div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
        <Cell label="Вход" value={f.entry_price.toFixed(5)} mono />
        <Cell label="ADX H1" value={fmtNumber(f.adx_h1, 0)} />
        <Cell label="ADX H4" value={fmtNumber(f.adx_h4, 0)} />
        <Cell
          label="Persist"
          value={`${f.trend_persistence_bars}/5`}
          tone={f.trend_persistence_5h >= 80 ? "text-buy" : "text-text"}
        />
      </div>

      {evaluated && (
        <div
          className={[
            "mt-3 text-xs px-2.5 py-1.5 rounded-lg border",
            win
              ? "bg-buy/10 border-buy/30 text-buy"
              : loss
                ? "bg-sell/10 border-sell/30 text-sell"
                : "bg-muted/10 border-muted/30 text-muted",
          ].join(" ")}
        >
          <strong>{win ? "✓ WIN" : "✗ LOSS"}</strong> · движение{" "}
          {fmtPct(movePct, 3)} за 5 часов
          {f.exit_price_5h != null && (
            <span className="ml-2 opacity-70">
              → {f.exit_price_5h.toFixed(5)}
            </span>
          )}
        </div>
      )}
      {!evaluated && (
        <div className="mt-2 text-[11px] text-muted">
          Ожидание результата по истечении 5ч…
        </div>
      )}
    </Link>
  );
}

function Cell({
  label,
  value,
  mono = false,
  tone = "text-text",
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: string;
}) {
  return (
    <div>
      <div className="text-[10px] text-muted uppercase">{label}</div>
      <div
        className={`${tone} font-semibold ${mono ? "font-mono" : ""} tabular-nums`}
      >
        {value}
      </div>
    </div>
  );
}
