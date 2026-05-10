import { useCallback } from "react";
import { api } from "../lib/api";
import { POLL_INTERVAL_MS } from "../lib/constants";
import { usePolling } from "../lib/usePolling";
import { timeAgo } from "../lib/format";

/**
 * Small pulse-dot "live" indicator in the header.
 *
 * Green + pulsing = backend is serving fresh data (scan_count grew in the
 *   last minute).
 * Amber = backend up but data is stale (> 60s since last scan).
 * Red = /api/health itself is failing.
 */
export default function HealthBadge() {
  const fetcher = useCallback((signal: AbortSignal) => api.health(signal), []);
  const { data, error, lastUpdated } = usePolling(
    fetcher,
    POLL_INTERVAL_MS.health,
  );

  let tone = "bg-muted";
  let label = "…";
  let title = "Проверка состояния…";

  if (error) {
    tone = "bg-sell";
    label = "OFF";
    title = `Сервер не отвечает: ${error.message}`;
  } else if (data) {
    const fresh = lastUpdated && Date.now() - lastUpdated < 60_000;
    tone = fresh ? "bg-buy animate-pulseDot" : "bg-[#ffa726]";
    label = fresh ? "LIVE" : "STALE";
    title = `Скан #${data.scan_count} · обновлено ${timeAgo(lastUpdated)} · ${data.time_utc5 ?? ""}`;
  }

  return (
    <div
      title={title}
      className="flex items-center gap-2 px-2.5 py-1 rounded-lg border border-border bg-card text-xs"
    >
      <span className={`inline-block w-2 h-2 rounded-full ${tone}`} />
      <span className="font-semibold tracking-wide text-muted">{label}</span>
      <span className="hidden sm:inline text-muted/70">
        {data ? `#${data.scan_count}` : ""}
      </span>
    </div>
  );
}
