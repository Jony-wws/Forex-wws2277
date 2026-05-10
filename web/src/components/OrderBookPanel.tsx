import type { OrderBook } from "../lib/types";
import { fmtNumber } from "../lib/format";

/**
 * Horizontal-bar style orderbook approximation.
 *
 * The backend exposes pseudo-level-2 depth derived from the volume
 * profile + estimated spread (see app/orderbook.py).  We render each
 * price level as a bar whose length is proportional to volume_pct, and
 * colour asks red / bids green.  Support / resistance ticks are shown
 * as dashed lines on the side for quick visual reference.
 */
export default function OrderBookPanel({
  ob,
  priceDigits = 5,
}: {
  ob: OrderBook;
  priceDigits?: number;
}) {
  const asks = ob.depth
    .filter((d) => d.side === "ask")
    .sort((a, b) => b.price - a.price); // top of book at top of list
  const bids = ob.depth
    .filter((d) => d.side === "bid")
    .sort((a, b) => b.price - a.price);

  const maxPct = Math.max(
    1,
    ...ob.depth.map((d) => d.volume_pct),
  );

  return (
    <div className="card p-3 sm:p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="text-xs text-muted uppercase tracking-wider">
          Стакан (аппрокс.)
        </div>
        <div className="text-xs tabular-nums text-muted">
          спред {fmtNumber(ob.spread_pips, 1)} пп
        </div>
      </div>

      <div className="space-y-0.5 text-xs font-mono">
        {asks.map((d, i) => (
          <DepthBar
            key={`ask-${i}`}
            price={d.price}
            pct={d.volume_pct}
            distance={d.distance_pips}
            max={maxPct}
            side="ask"
            digits={priceDigits}
          />
        ))}
      </div>

      <div className="my-2 py-1.5 px-2 rounded bg-cardAlt flex items-center justify-between text-xs">
        <span className="text-sell">ASK {ob.ask.toFixed(priceDigits)}</span>
        <span className="text-muted text-[11px] tabular-nums">
          MID {ob.mid.toFixed(priceDigits)}
        </span>
        <span className="text-buy">BID {ob.bid.toFixed(priceDigits)}</span>
      </div>

      <div className="space-y-0.5 text-xs font-mono">
        {bids.map((d, i) => (
          <DepthBar
            key={`bid-${i}`}
            price={d.price}
            pct={d.volume_pct}
            distance={d.distance_pips}
            max={maxPct}
            side="bid"
            digits={priceDigits}
          />
        ))}
      </div>

      {(ob.supports.length > 0 || ob.resistances.length > 0) && (
        <div className="mt-3 pt-3 border-t border-border grid grid-cols-2 gap-2 text-xs">
          <div>
            <div className="text-[10px] uppercase text-muted mb-1">
              Сопротивления
            </div>
            {ob.resistances.length ? (
              ob.resistances.map((r) => (
                <div key={r} className="font-mono text-sell tabular-nums">
                  {r.toFixed(priceDigits)}
                </div>
              ))
            ) : (
              <div className="text-muted/60">—</div>
            )}
          </div>
          <div>
            <div className="text-[10px] uppercase text-muted mb-1">
              Поддержки
            </div>
            {ob.supports.length ? (
              ob.supports.map((s) => (
                <div key={s} className="font-mono text-buy tabular-nums">
                  {s.toFixed(priceDigits)}
                </div>
              ))
            ) : (
              <div className="text-muted/60">—</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function DepthBar({
  price,
  pct,
  distance,
  max,
  side,
  digits,
}: {
  price: number;
  pct: number;
  distance: number;
  max: number;
  side: "bid" | "ask";
  digits: number;
}) {
  const width = Math.max(2, (pct / max) * 100);
  const tone = side === "ask" ? "bg-sell/15" : "bg-buy/15";
  const priceTone = side === "ask" ? "text-sell" : "text-buy";
  return (
    <div className="relative flex items-center justify-between px-1 py-0.5">
      <div
        className={`absolute inset-y-0 right-0 ${tone} rounded-sm`}
        style={{ width: `${width}%` }}
        aria-hidden
      />
      <span className={`relative z-[1] tabular-nums ${priceTone}`}>
        {price.toFixed(digits)}
      </span>
      <span className="relative z-[1] text-muted tabular-nums">
        {pct.toFixed(1)}%{" "}
        <span className="text-muted/50 ml-1">
          {distance.toFixed(1)}пп
        </span>
      </span>
    </div>
  );
}
