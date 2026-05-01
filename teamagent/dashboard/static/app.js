// TeamAgent dashboard frontend
// Обновляется:
//   open trades + stats + health: каждые 30 сек
//   forecasts: каждые 60 сек (источник scaner — раз в 5 мин)
//   volume profile: при выборе пары + ручное обновление

const REFRESH_LIVE_MS = 30 * 1000;
const REFRESH_FORECASTS_MS = 60 * 1000;

const fmt = {
  pct: x => x == null ? "—" : (x).toFixed(1) + "%",
  price: x => x == null ? "—" : (x).toFixed(5),
  pnl: x => x == null ? "—" : (x >= 0 ? `+$${x.toFixed(2)}` : `-$${Math.abs(x).toFixed(2)}`),
  utc: s => {
    if (!s) return "—";
    const d = new Date(s);
    return d.toISOString().slice(0, 19).replace("T", " ");
  },
  countdown: sec => {
    if (sec == null) return "—";
    if (sec <= 0) return "истекло";
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    return `${h}ч ${m.toString().padStart(2, "0")}м ${s.toString().padStart(2, "0")}с`;
  },
  ago: s => {
    if (!s) return "—";
    const sec = Math.max(0, Math.floor((Date.now() - new Date(s).getTime()) / 1000));
    if (sec < 60) return `${sec} сек назад`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m} мин назад`;
    const h = Math.floor(m / 60);
    return `${h} ч ${m % 60} мин назад`;
  },
};

function freshnessBadge(asOfIso) {
  if (!asOfIso) return el("span", { class: "muted small" }, "—");
  const sec = Math.max(0, Math.floor((Date.now() - new Date(asOfIso).getTime()) / 1000));
  let cls = "fresh-fresh", label = "СВЕЖИЙ";
  if (sec > 600) { cls = "fresh-stale"; label = "УСТАРЕЛ"; }
  else if (sec > 300) { cls = "fresh-old"; label = "СТАРЕЕТ"; }
  return el("span", { class: "fresh-badge " + cls, title: fmt.ago(asOfIso) }, label);
}

// Build fetch URL using location.origin (which never contains userinfo).
// If the page was opened via an auto-login URL like
// https://user:pass@host/, relative fetch() URLs inherit the credentials and
// modern browsers throw: "Request cannot be constructed from a URL that
// includes credentials". Using location.origin strips the credentials while
// the browser still sends the cached HTTP Basic Authorization header.
async function api(path) {
  const url = location.origin + path;
  const r = await fetch(url, { cache: "no-store", credentials: "same-origin" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return await r.json();
}

function $(id) { return document.getElementById(id); }
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

// ───── stats ─────
async function refreshStats() {
  try {
    const s = await api("/api/stats");
    $("stat-total").textContent = s.total ?? 0;
    $("stat-wins").textContent = s.wins ?? 0;
    $("stat-losses").textContent = s.losses ?? 0;
    $("stat-wr").textContent = ((s.win_rate_pct ?? 0)).toFixed(1) + "%";
    const pnl = s.total_pnl_usd ?? 0;
    $("stat-pnl").textContent = (pnl >= 0 ? "+$" : "-$") + Math.abs(pnl).toFixed(2);
    $("stat-pnl").className = "big " + (pnl >= 0 ? "green" : "red");
  } catch (e) { console.error(e); }
}

// ───── open trades (live PnL, refresh каждые 30 сек) ─────
async function refreshOpenTrades() {
  try {
    const r = await api("/api/open-trades");
    $("open-count").textContent = r.count ?? 0;
    const tb = document.querySelector("#open-table tbody");
    tb.innerHTML = "";
    for (const t of r.trades) {
      const live = t.live || {};
      const sideClass = t.side === "BUY" ? "side-buy" : "side-sell";
      const inMoney = live.in_money_now;
      const okLabel = inMoney === true ? "✓ да" : inMoney === false ? "✗ нет" : "—";
      const okClass = inMoney === true ? "win" : inMoney === false ? "loss" : "muted";
      const projPayout = live.projected_payout;

      tb.appendChild(el("tr", {},
        el("td", {}, t.pair),
        el("td", { class: sideClass }, t.side),
        el("td", {}, fmt.price(t.open_price)),
        el("td", { class: "muted" }, fmt.utc(t.open_time)),
        el("td", {}, fmt.countdown(live.time_remaining_sec)),
        el("td", {}, fmt.price(live.current_price)),
        el("td", {}, live.diff_pct == null ? "—" : (live.diff_pct >= 0 ? "+" : "") + live.diff_pct.toFixed(3) + "%"),
        el("td", { class: okClass }, fmt.pnl(projPayout)),
        el("td", { class: okClass }, okLabel),
      ));
    }
    if (r.count === 0) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 9, class: "muted" }, "пока нет открытых сделок")));
    }
  } catch (e) { console.error(e); }
}

// ───── PROGNOZY-28 (единый источник, refresh каждые 60 сек) ─────
async function refreshForecasts() {
  try {
    const r = await api("/api/forecasts");
    const sc = await api("/api/strategy-config").catch(() => ({ pairs: {} }));
    const tb = document.querySelector("#forecasts-table tbody");
    tb.innerHTML = "";
    $("forecasts-as-of").textContent = r.scanned_at
      ? `${fmt.utc(r.scanned_at)} UTC · ${fmt.ago(r.scanned_at)}`
      : "—";
    // Сводка strategy_search
    const summary = sc.summary || {};
    const qual = summary.qualified_pairs_70pct || [];
    const sumNode = $("strategy-summary");
    if (sumNode) {
      sumNode.innerHTML = "";
      sumNode.appendChild(el("div", { class: "muted small" },
        `Strategy Search: ${qual.length}/${summary.total_pairs || 28} пар достигли ≥70% WR на 30-дневном бэктесте · ${sc.as_of ? fmt.ago(sc.as_of) : "ещё не запускался"}`,
      ));
    }
    let i = 1;
    for (const f of r.rankings) {
      const sideClass = f.side === "BUY" ? "side-buy" : "side-sell";
      const fdata = (r.forecasts && r.forecasts[f.pair]) || {};
      const af = fdata.agents_for_count;
      const ag = fdata.agents_against_count;
      const scPair = (sc.pairs || {})[f.pair];
      const wr = scPair && scPair.win_rate_pct;
      const variant = scPair && scPair.best_variant;
      const qualifies = scPair && scPair.qualifies_70pct;
      const btCell = wr != null
        ? el("td", { class: qualifies ? "win" : "loss", title: variant ? `${variant}: ${scPair.best_label}` : "" },
            `${wr.toFixed(0)}% (${scPair.trades || 0})`)
        : el("td", { class: "muted small" }, "—");
      const variantCell = variant
        ? el("td", { class: "muted small", title: scPair.best_label }, variant.replace(/^v\d+_/, ""))
        : el("td", { class: "muted small" }, "—");
      const isFrozen = scPair != null && !qualifies;
      const rowAttrs = isFrozen
        ? { onclick: () => showForecastDetail(f.pair), class: "frozen-row" }
        : { onclick: () => showForecastDetail(f.pair) };
      const tr = el("tr", rowAttrs,
        el("td", { class: "muted" }, i),
        el("td", {}, f.pair, isFrozen ? el("span", { class: "frozen-badge", title: `лучшая стратегия даёт ${wr || "?"}% WR — пара заморожена` }, " 🔒") : null),
        el("td", { class: sideClass }, f.side),
        el("td", {}, fmt.pct(f.probability_pct)),
        btCell,
        variantCell,
        el("td", {}, `${f.score}/44`),
        el("td", {}, `${f.recommended_hours}ч`),
        el("td", { class: af === 0 ? "muted small" : "side-buy" }, af == null ? "—" : af),
        el("td", { class: ag === 0 ? "muted small" : "side-sell" }, ag == null ? "—" : ag),
        el("td", {}, freshnessBadge(r.scanned_at)),
      );
      tb.appendChild(tr);
      i++;
    }
    if (r.rankings.length === 0) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 11, class: "muted" }, "сканер ещё не запущен или нет прогнозов выше нуля")));
    }
  } catch (e) { console.error(e); }
}

async function showForecastDetail(pair) {
  try {
    const f = await api(`/api/forecast/${pair}`);
    const detail = $("forecast-detail");
    detail.classList.remove("hidden");
    detail.innerHTML = "";
    detail.appendChild(el("h3", {}, `${f.pair}  ${f.side}  ${fmt.pct(f.probability_pct)}  score ${f.score}/${f.max_score}`));
    detail.appendChild(el("div", { class: "muted small" },
      `сессия ${f.session} · экспирация ${f.recommended_hours}ч · текущая цена ${fmt.price(f.current_price)}`
    ));

    const forList = (f.agents_for && f.agents_for.length)
      ? f.agents_for.map(n => el("li", {}, n))
      : [el("li", { class: "muted small" }, "нет правил подтверждающих эту сторону")];
    const againstList = (f.agents_against && f.agents_against.length)
      ? f.agents_against.map(n => el("li", {}, n))
      : [el("li", { class: "muted small" }, "нет правил против этой стороны")];
    const forBox = el("div", { class: "card-inset" },
      el("strong", { class: "side-buy" }, `За (${f.agents_for_count || 0})`),
      el("ul", {}, ...forList)
    );
    const againstBox = el("div", { class: "card-inset" },
      el("strong", { class: "side-sell" }, `Против (${f.agents_against_count || 0})`),
      el("ul", {}, ...againstList)
    );
    const wrap = el("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px;" }, forBox, againstBox);
    detail.appendChild(wrap);

    const breakdown = el("details", { style: "margin-top:8px;" }, el("summary", {}, "score breakdown"));
    const ul = el("ul", { class: "muted small" });
    for (const b of f.score_breakdown) {
      ul.appendChild(el("li", {}, `${b.contrib >= 0 ? "+" : ""}${b.contrib}  ${b.name} — ${b.reason}`));
    }
    breakdown.appendChild(ul);
    detail.appendChild(breakdown);

    // VP сразу же
    if (f.volume_profile && !f.volume_profile.error) {
      detail.appendChild(el("h4", {}, "Стакан + прогноз 00:00 UTC+5"));
      detail.appendChild(renderVP(f.volume_profile));
    }
  } catch (e) { console.error(e); }
}

// ───── Volume Profile renderer ─────
function renderVP(vp) {
  const container = el("div");
  if (vp.error) { container.appendChild(el("div", { class: "muted" }, "ошибка: " + vp.error)); return container; }

  const fc = vp.forecast_to_utc5_midnight || {};
  container.appendChild(el("div", { class: "muted small" },
    `POC ${fmt.price(vp.poc)} · VAH ${fmt.price(vp.vah)} · VAL ${fmt.price(vp.val)} · ` +
    `направление ${vp.direction} · ${fc.explanation || ""}`
  ));

  if (fc.no_return_levels && fc.no_return_levels.length) {
    const ul = el("ul", { class: "muted small" });
    for (const r of fc.no_return_levels) {
      ul.appendChild(el("li", {}, `${r.side === "below" ? "↓ ниже" : "↑ выше"} ${fmt.price(r.price)} · вес ${fmt.pct(r.weight_pct)} (${r.kind === "support" ? "поддержка" : "сопротивление"})`));
    }
    container.appendChild(el("div", {}, el("strong", {}, "Куда не вернётся: "), ul));
  }

  // bars (топ-30 по объёму)
  const bars = el("div", { class: "vp-bars" });
  const sorted = [...vp.buckets].sort((a, b) => b.weight_pct - a.weight_pct).slice(0, 30);
  const top = sorted.sort((a, b) => b.price - a.price);
  const max = Math.max(...top.map(b => b.weight_pct));
  const bigPrices = new Set((vp.big_players || []).map(b => b.price));
  for (const b of top) {
    const isPoc = Math.abs(b.price - vp.poc) < 1e-9;
    const isBig = bigPrices.has(b.price);
    const isCurr = Math.abs(b.price - vp.current_price) <= (vp.high - vp.low) / 50;
    const cls = ["vp-bar", isPoc ? "poc" : "", isBig ? "big-player" : "", isCurr ? "current" : ""].filter(Boolean).join(" ");
    bars.appendChild(el("div", { class: cls },
      el("span", {}, fmt.price(b.price)),
      el("div", { class: "fill" }, el("div", { class: "v", style: `width:${(b.weight_pct / max * 100).toFixed(1)}%` })),
      el("span", {}, fmt.pct(b.weight_pct)),
      el("span", { class: "muted" }, isBig ? "🐋 кит" : isPoc ? "POC" : ""),
    ));
  }
  container.appendChild(bars);
  return container;
}

async function refreshVP() {
  const pair = $("vp-pair").value;
  $("vp-content").textContent = "загружаю…";
  try {
    const vp = await api(`/api/volume-profile/${pair}`);
    $("vp-content").innerHTML = "";
    $("vp-content").appendChild(renderVP(vp));
  } catch (e) {
    $("vp-content").textContent = "ошибка: " + e.message;
  }
}

async function populateVPDropdown() {
  try {
    const r = await api("/api/forecasts");
    const sel = $("vp-pair");
    sel.innerHTML = "";
    const pairs = (r.rankings || []).map(x => x.pair);
    if (pairs.length === 0) {
      const fallback = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"];
      fallback.forEach(p => sel.appendChild(el("option", { value: p }, p)));
    } else {
      pairs.forEach(p => sel.appendChild(el("option", { value: p }, p)));
    }
  } catch (e) { console.error(e); }
}

// ───── closed trades ─────
async function refreshClosed() {
  try {
    const r = await api("/api/closed-trades");
    const tb = document.querySelector("#closed-table tbody");
    tb.innerHTML = "";
    for (const t of r.trades) {
      const sideClass = t.side === "BUY" ? "side-buy" : "side-sell";
      const resultClass = t.result === "WIN" ? "win" : "loss";
      tb.appendChild(el("tr", {},
        el("td", {}, t.pair),
        el("td", { class: sideClass }, t.side),
        el("td", {}, fmt.price(t.open_price)),
        el("td", {}, fmt.price(t.close_price)),
        el("td", { class: "muted" }, fmt.utc(t.open_time)),
        el("td", { class: "muted" }, fmt.utc(t.close_time)),
        el("td", { class: resultClass }, t.result),
        el("td", { class: resultClass }, fmt.pnl(t.pnl_usd)),
      ));
    }
    if (r.count === 0) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 8, class: "muted" }, "ещё ни одна сделка не закрыта")));
    }
  } catch (e) { console.error(e); }
}

// ───── health (header) ─────
async function refreshHealth() {
  try {
    const h = await api("/api/health");
    const items = [];
    for (const [name, c] of Object.entries(h.components || {})) {
      items.push(`${c.alive ? "🟢" : "🔴"} ${name}`);
    }
    $("health").textContent = items.join("  ");
  } catch (e) { $("health").textContent = "health недоступен"; }
}

// ───── agents ─────
async function refreshAgents() {
  try {
    const r = await api("/api/agents");
    const grid = $("agents-grid");
    grid.innerHTML = "";
    const list = r.agents || [];
    if (list.length === 0) {
      grid.textContent = "агенты ещё не запущены";
      return;
    }
    for (const a of list) {
      const cls = "agent " + (a.alive ? "alive" : "dead");
      grid.appendChild(el("div", { class: cls },
        el("span", {}, a.name),
        el("span", { class: "age" }, a.age_sec != null ? `${a.age_sec}s` : "—"),
      ));
    }
  } catch (e) { console.error(e); }
}

function tick() {
  $("last-refresh").textContent = new Date().toLocaleTimeString();
  refreshStats();
  refreshOpenTrades();
  refreshHealth();
  refreshAgents();
  refreshClosed();
}

function tickForecasts() {
  refreshForecasts();
  refreshStrategyMatrix();
}

// ───── per-session strategy matrix (Asia / London / Overlap / NY × 28 пар) ─────
async function refreshStrategyMatrix() {
  try {
    const sc = await api("/api/strategy-config").catch(() => ({ pairs: {} }));
    const tb = document.querySelector("#strategy-matrix-table tbody");
    if (!tb) return;
    tb.innerHTML = "";
    const sessNames = ["Asia", "London", "Overlap", "NY"];
    const pairs = sc.pairs || {};
    const summary = sc.summary || {};
    const sumNode = $("strategy-matrix-summary");
    if (sumNode) {
      sumNode.innerHTML = "";
      const bs = summary.by_session || {};
      const parts = sessNames.map(s => {
        const d = bs[s] || {};
        const q = d.qualified_count != null ? d.qualified_count : "?";
        const tp = d.total_pairs_with_data != null ? d.total_pairs_with_data : "?";
        return `${s}: ${q}/${tp}`;
      });
      sumNode.appendChild(el("div", {},
        `Пар достигают ≥70% WR per-session: ${parts.join(" · ")} · оценка ~${summary.est_trades_per_day_via_session_gate || 0} сделок/день через session-gate · ${sc.as_of ? "обновлено " + fmt.ago(sc.as_of) : "ещё не запускался"}`,
      ));
    }
    const pairKeys = Object.keys(pairs).sort();
    if (pairKeys.length === 0) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 6, class: "muted" },
        "strategy_search ещё не закончил первый прогон (~10 минут после старта)")));
      return;
    }
    for (const pair of pairKeys) {
      const p = pairs[pair];
      const by = p.by_session || {};
      const cells = sessNames.map(s => {
        const d = by[s] || {};
        const wr = d.win_rate_pct;
        const tr = d.trades || 0;
        const variant = d.best_variant;
        if (wr == null) {
          return el("td", { class: "muted small", title: d.note || "no data" }, "—");
        }
        const cls = d.qualifies_70pct ? "win" : "loss";
        const label = d.best_label || "";
        const title = `${variant || "?"}: ${label}\n${tr} сделок · WR ${wr}%`;
        return el("td", { class: cls, title }, `${wr.toFixed(0)}% (${tr})`);
      });
      const qualCount = sessNames.filter(s => (by[s] || {}).qualifies_70pct).length;
      const qualCell = el("td",
        { class: qualCount === 4 ? "win" : qualCount > 0 ? "" : "muted" },
        `${qualCount}/4`);
      const tr = el("tr", {},
        el("td", {}, pair),
        ...cells,
        qualCell,
      );
      tb.appendChild(tr);
    }
  } catch (e) { console.error("refreshStrategyMatrix:", e); }
}

document.addEventListener("DOMContentLoaded", () => {
  $("manual-refresh").addEventListener("click", () => { tick(); tickForecasts(); refreshVP(); });
  $("vp-refresh").addEventListener("click", refreshVP);
  $("vp-pair").addEventListener("change", refreshVP);

  populateVPDropdown().then(refreshVP);

  tick();
  tickForecasts();
  setInterval(tick, REFRESH_LIVE_MS);
  setInterval(tickForecasts, REFRESH_FORECASTS_MS);
});
