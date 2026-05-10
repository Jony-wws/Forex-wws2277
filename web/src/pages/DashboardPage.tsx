import { useCallback, useMemo, useState } from "react";
import { api } from "../lib/api";
import { PAIRS, POLL_INTERVAL_MS } from "../lib/constants";
import { usePolling } from "../lib/usePolling";
import { timeAgo } from "../lib/format";
import ErrorBanner from "../components/ErrorBanner";
import PairCard from "../components/PairCard";
import { SkeletonCard } from "../components/Skeleton";

type Filter = "all" | "signals" | "buy" | "sell" | "strong";
type SortKey = "default" | "confidence" | "change" | "adx";

export default function DashboardPage() {
  const fetcher = useCallback(
    (signal: AbortSignal) => api.signals(signal),
    [],
  );
  const { data, error, loading, lastUpdated, refresh } = usePolling(
    fetcher,
    POLL_INTERVAL_MS.signals,
  );

  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<SortKey>("default");

  const entries = useMemo(() => {
    const pairs = data?.pairs ?? {};
    // Keep the 28-pair grid stable on first paint: start from the full
    // PAIRS list and fill in whatever has arrived so far.  This avoids
    // layout jumps as the scanner fills the object one pair at a time.
    const rows = PAIRS.map((p) => pairs[p]).filter(Boolean);

    const filtered = rows.filter((e) => {
      if (filter === "signals") return e.signal !== null;
      if (filter === "buy") return e.signal === "BUY";
      if (filter === "sell") return e.signal === "SELL";
      if (filter === "strong") return e.is_strong_trend;
      return true;
    });

    if (sort === "confidence") {
      filtered.sort((a, b) => b.confidence - a.confidence);
    } else if (sort === "change") {
      filtered.sort(
        (a, b) => Math.abs(b.change_24h_pct) - Math.abs(a.change_24h_pct),
      );
    } else if (sort === "adx") {
      filtered.sort((a, b) => b.adx_h1 - a.adx_h1);
    }
    return filtered;
  }, [data, filter, sort]);

  const stats = useMemo(() => {
    const pairs = Object.values(data?.pairs ?? {});
    return {
      total: pairs.length,
      buy: pairs.filter((p) => p.signal === "BUY").length,
      sell: pairs.filter((p) => p.signal === "SELL").length,
      strong: pairs.filter((p) => p.is_strong_trend).length,
    };
  }, [data]);

  return (
    <div className="space-y-4">
      <StatsBar
        total={stats.total}
        buy={stats.buy}
        sell={stats.sell}
        strong={stats.strong}
        updated={data?.updated_at ?? null}
        lastUpdated={lastUpdated}
        onRefresh={refresh}
      />

      <Filters
        filter={filter}
        setFilter={setFilter}
        sort={sort}
        setSort={setSort}
        counts={stats}
      />

      {error && !data ? (
        <ErrorBanner error={error} onRetry={refresh} />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {loading && entries.length === 0 ? (
            Array.from({ length: 9 }).map((_, i) => <SkeletonCard key={i} />)
          ) : entries.length === 0 ? (
            <div className="col-span-full card p-6 text-center text-muted">
              Нет пар по текущему фильтру.
            </div>
          ) : (
            entries.map((e) => <PairCard key={e.pair} entry={e} />)
          )}
        </div>
      )}
    </div>
  );
}

function StatsBar({
  total,
  buy,
  sell,
  strong,
  updated,
  lastUpdated,
  onRefresh,
}: {
  total: number;
  buy: number;
  sell: number;
  strong: number;
  updated: string | null;
  lastUpdated: number | null;
  onRefresh: () => void;
}) {
  return (
    <div className="card p-3 flex items-center flex-wrap gap-3 text-sm">
      <Stat label="Всего" value={`${total}/28`} />
      <Stat label="BUY" value={String(buy)} tone="text-buy" />
      <Stat label="SELL" value={String(sell)} tone="text-sell" />
      <Stat label="STRONG" value={String(strong)} tone="text-accent2" />
      <div className="ml-auto flex items-center gap-2 text-xs text-muted">
        <span>
          {updated ? `Сервер: ${updated}` : "Ожидание данных…"} · клиент{" "}
          {timeAgo(lastUpdated)}
        </span>
        <button
          type="button"
          className="btn text-xs py-1 px-2"
          onClick={onRefresh}
          aria-label="Обновить"
          title="Обновить сейчас"
        >
          ↻
        </button>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone = "text-text",
}: {
  label: string;
  value: string;
  tone?: string;
}) {
  return (
    <span className="flex items-baseline gap-1.5">
      <span className="text-xs text-muted">{label}</span>
      <span className={`font-semibold tabular-nums ${tone}`}>{value}</span>
    </span>
  );
}

function Filters({
  filter,
  setFilter,
  sort,
  setSort,
  counts,
}: {
  filter: Filter;
  setFilter: (f: Filter) => void;
  sort: SortKey;
  setSort: (s: SortKey) => void;
  counts: { total: number; buy: number; sell: number; strong: number };
}) {
  const chips: Array<{ key: Filter; label: string; count: number }> = [
    { key: "all", label: "Все", count: counts.total },
    { key: "signals", label: "Сигналы", count: counts.buy + counts.sell },
    { key: "buy", label: "BUY", count: counts.buy },
    { key: "sell", label: "SELL", count: counts.sell },
    { key: "strong", label: "STRONG", count: counts.strong },
  ];
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex flex-wrap gap-1.5">
        {chips.map((c) => (
          <button
            key={c.key}
            type="button"
            onClick={() => setFilter(c.key)}
            className={[
              "px-2.5 py-1 rounded-lg text-xs font-semibold border transition-colors",
              filter === c.key
                ? "bg-accent/20 border-accent/50 text-accent"
                : "bg-card border-border text-muted hover:text-text",
            ].join(" ")}
          >
            {c.label}
            <span className="ml-1 opacity-60 tabular-nums">{c.count}</span>
          </button>
        ))}
      </div>
      <div className="ml-auto">
        <label className="text-xs text-muted mr-1.5">Сортировка</label>
        <select
          value={sort}
          onChange={(e) => setSort(e.target.value as SortKey)}
          className="bg-card border border-border rounded-lg px-2 py-1 text-xs text-text"
        >
          <option value="default">По умолчанию</option>
          <option value="confidence">По уверенности</option>
          <option value="change">По движению 24ч</option>
          <option value="adx">По ADX</option>
        </select>
      </div>
    </div>
  );
}
