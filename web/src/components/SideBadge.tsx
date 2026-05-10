import type { Side } from "../lib/types";

export default function SideBadge({
  side,
  confidence,
}: {
  side: Side | null;
  confidence?: number;
}) {
  if (side === "BUY") {
    return (
      <span className="chip-buy">
        <ArrowUp /> ПОКУПКА
        {confidence != null && (
          <span className="opacity-70 ml-0.5">{confidence}%</span>
        )}
      </span>
    );
  }
  if (side === "SELL") {
    return (
      <span className="chip-sell">
        <ArrowDown /> ПРОДАЖА
        {confidence != null && (
          <span className="opacity-70 ml-0.5">{confidence}%</span>
        )}
      </span>
    );
  }
  return <span className="chip-neutral">—</span>;
}

function ArrowUp() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden>
      <path
        d="M5 1.5L9 7H1L5 1.5Z"
        fill="currentColor"
      />
    </svg>
  );
}
function ArrowDown() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden>
      <path d="M5 8.5L1 3h8L5 8.5Z" fill="currentColor" />
    </svg>
  );
}
