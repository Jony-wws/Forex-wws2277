import type { CycleTier } from "../lib/types";

const MAP: Record<CycleTier, { label: string; cls: string; title: string }> = {
  PREMIUM: {
    label: "PREMIUM",
    cls: "chip-premium",
    title: "ADX H1 ≥ 28 + ≥4/5 H1 баров по тренду + все ТФ согласованы",
  },
  STRONG: {
    label: "STRONG",
    cls: "chip-strong",
    title: "Сильный устойчивый тренд — все 5 жёстких условий пройдены",
  },
  NORMAL: {
    label: "NORMAL",
    cls: "chip-neutral",
    title: "Запасной выбор: добор до минимума пиков в слабом рынке",
  },
};

export default function TierBadge({ tier }: { tier: CycleTier }) {
  const t = MAP[tier] ?? MAP.NORMAL;
  return (
    <span className={t.cls} title={t.title}>
      {t.label}
    </span>
  );
}
