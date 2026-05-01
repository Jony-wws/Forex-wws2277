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
  pips: x => x == null ? "—" : (x >= 0 ? "+" : "") + x.toFixed(1),
  utc: s => {
    if (!s) return "—";
    const d = new Date(s);
    return d.toISOString().slice(0, 19).replace("T", " ");
  },
  utc5: s => {
    // UTC+5 (Иркутск/Челябинск/Уральск). Локальное время пользователя.
    if (!s) return "—";
    const d = new Date(new Date(s).getTime() + 5 * 3600 * 1000);
    return d.toISOString().slice(0, 19).replace("T", " ") + " UTC+5";
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

      const pipsClass = live.pips == null ? "muted" : (live.pips >= 0 ? "win" : "loss");
      tb.appendChild(el("tr", {},
        el("td", {}, t.pair),
        el("td", { class: sideClass }, t.side),
        el("td", {}, fmt.price(t.open_price)),
        el("td", { class: "muted small" }, fmt.utc5(t.open_time)),
        el("td", {}, fmt.countdown(live.time_remaining_sec)),
        el("td", {}, fmt.price(live.current_price)),
        el("td", { class: pipsClass }, fmt.pips(live.pips)),
        el("td", { class: pipsClass }, live.diff_pct == null ? "—" : (live.diff_pct >= 0 ? "+" : "") + live.diff_pct.toFixed(3) + "%"),
        el("td", { class: okClass }, fmt.pnl(projPayout)),
        el("td", { class: okClass }, okLabel),
      ));
    }
    if (r.count === 0) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 10, class: "muted" }, "пока нет открытых сделок")));
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
  // Текущая цена крупно сверху — пользователь жаловался что её не видно
  container.appendChild(el("div", { class: "vp-current-price" },
    el("span", { class: "muted small" }, "Текущая цена: "),
    el("strong", { class: "big green" }, fmt.price(vp.current_price)),
    el("span", { class: "muted small" }, ` (диапазон ${fmt.price(vp.low)}–${fmt.price(vp.high)})`),
  ));
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

  // bars (топ-30 по объёму + всегда включаем bucket текущей цены)
  const bars = el("div", { class: "vp-bars" });
  const sortedByWeight = [...vp.buckets].sort((a, b) => b.weight_pct - a.weight_pct);
  const top30 = sortedByWeight.slice(0, 30);
  // bucket ближайший к текущей цене
  let currBucket = null, bestDist = Infinity;
  for (const b of vp.buckets) {
    const d = Math.abs(b.price - vp.current_price);
    if (d < bestDist) { bestDist = d; currBucket = b; }
  }
  if (currBucket && !top30.find(b => b.price === currBucket.price)) {
    top30.push(currBucket);
  }
  const top = top30.sort((a, b) => b.price - a.price);
  const max = Math.max(...top.map(b => b.weight_pct));
  const bigPrices = new Set((vp.big_players || []).map(b => b.price));
  // Чётко определяем bucket текущей цены: тот, что ближе всего по абсолютной
  // разнице (а не по «±1/50 диапазона»). Это чтобы ровно одна полоса в стакане
  // подсвечивалась как «текущая цена», и пользователь не путался.
  const currPrice = vp.current_price;
  let currKey = null;
  if (currPrice != null && top.length) {
    let bestD = Infinity;
    for (const b of top) {
      const d = Math.abs(b.price - currPrice);
      if (d < bestD) { bestD = d; currKey = b.price; }
    }
  }
  for (const b of top) {
    const isPoc = Math.abs(b.price - vp.poc) < 1e-9;
    const isBig = bigPrices.has(b.price);
    const isCurr = currKey !== null && b.price === currKey;
    const cls = ["vp-bar", isPoc ? "poc" : "", isBig ? "big-player" : "", isCurr ? "current" : ""].filter(Boolean).join(" ");
    // Подпись справа: сначала «🎯 ЦЕНА», потом 🐋 кит / POC / VAH / VAL
    let tag = "";
    if (isCurr) tag = "🎯 цена";
    else if (isBig) tag = "🐋 кит";
    else if (isPoc) tag = "POC";
    else if (Math.abs(b.price - vp.vah) < 1e-9) tag = "VAH";
    else if (Math.abs(b.price - vp.val) < 1e-9) tag = "VAL";
    bars.appendChild(el("div", { class: cls },
      el("span", {}, fmt.price(b.price)),
      el("div", { class: "fill" }, el("div", { class: "v", style: `width:${(b.weight_pct / max * 100).toFixed(1)}%` })),
      el("span", {}, fmt.pct(b.weight_pct)),
      el("span", { class: isCurr ? "" : "muted" }, tag),
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

// ───── Стакан-стратегия (параллельная система) ─────

async function refreshStakanStats() {
  try {
    const s = await api("/api/stakan/stats");
    $("stakan-stat-total").textContent = s.total ?? 0;
    $("stakan-stat-wins").textContent = s.wins ?? 0;
    $("stakan-stat-losses").textContent = s.losses ?? 0;
    $("stakan-stat-wr").textContent = ((s.win_rate_pct ?? 0)).toFixed(1) + "%";
    const pnl = s.total_pnl_usd ?? 0;
    $("stakan-stat-pnl").textContent = (pnl >= 0 ? "+$" : "-$") + Math.abs(pnl).toFixed(2);
    $("stakan-stat-pnl").className = "big " + (pnl >= 0 ? "green" : "red");
  } catch (e) { console.error("stakan stats:", e); }
}

function votesPill(yes, total) {
  const cls = yes >= 7 ? "pass" : (yes >= 5 ? "almost" : "fail");
  return el("span", { class: "votes-pill " + cls }, `${yes}/${total}`);
}

async function refreshStakanOpen() {
  try {
    const r = await api("/api/stakan/open-trades");
    $("stakan-open-count").textContent = r.count ?? 0;
    const tb = document.querySelector("#stakan-open-table tbody");
    tb.innerHTML = "";
    for (const t of r.trades) {
      const live = t.live || {};
      const sideClass = t.side === "BUY" ? "side-buy" : "side-sell";
      const inMoney = live.in_money_now;
      const projPayout = live.projected_payout;
      const pipsClass = live.pips == null ? "muted" : (live.pips >= 0 ? "win" : "loss");
      const lvl = t.level_at_open || {};
      const v = t.votes_at_open || {};
      tb.appendChild(el("tr", {},
        el("td", {}, t.pair),
        el("td", { class: sideClass }, t.side),
        el("td", {}, fmt.price(t.open_price)),
        el("td", {}, fmt.price(lvl.price)),
        el("td", {}, (t.level_distance_pips_at_open ?? 0).toFixed(1) + " pips"),
        el("td", { class: "muted small" }, fmt.utc5(t.open_time)),
        el("td", {}, fmt.countdown(live.time_remaining_sec)),
        el("td", {}, fmt.price(live.current_price)),
        el("td", { class: pipsClass }, fmt.pips(live.pips)),
        el("td", { class: inMoney ? "win" : (inMoney === false ? "loss" : "muted") },
          fmt.pnl(projPayout)),
        el("td", {}, votesPill(v.yes ?? 0, v.total ?? 10)),
      ));
    }
    if (r.count === 0) {
      tb.appendChild(el("tr", {},
        el("td", { colspan: 11, class: "muted" }, "Сейчас открытых стакан-сделок нет — ждём сигнал ≥7/10")));
    }
  } catch (e) { console.error("stakan open:", e); }
}

async function refreshStakanSignals() {
  try {
    const r = await api("/api/stakan/signals");
    const tb = document.querySelector("#stakan-signals-table tbody");
    tb.innerHTML = "";
    const sigs = r.signals || [];
    if (sigs.length === 0) {
      tb.appendChild(el("tr", {},
        el("td", { colspan: 8, class: "muted" }, "ещё не считали — ждём первый tick paper_trader_stakan (60 сек)")));
      return;
    }
    // отсортируем: сначала открытые, потом по убыванию голосов
    sigs.sort((a, b) => {
      const sa = a.skip_reason ? 0 : 2;
      const sb = b.skip_reason ? 0 : 2;
      if (sa !== sb) return sb - sa;
      const va = (a.votes && a.votes.yes) || 0;
      const vb = (b.votes && b.votes.yes) || 0;
      return vb - va;
    });
    for (const s of sigs) {
      const opened = !s.skip_reason;
      const lvl = s.best_level || (s.sample_levels && s.sample_levels[0]) || {};
      const v = s.votes;
      const status = opened ? el("span", { class: "win" }, "OPEN")
        : (s.skip_reason === "already_open" ? el("span", { class: "muted" }, "уже открыт")
          : (s.skip_reason === "no_valid_avoidance_level" ? el("span", { class: "muted" }, "нет валидного уровня")
            : el("span", { class: "muted" }, "ждёт голосов")));
      const direction = opened ? s.direction : (s.direction || lvl.trade_direction || "—");
      const sideClass = direction === "BUY" ? "side-buy" : direction === "SELL" ? "side-sell" : "muted";
      tb.appendChild(el("tr", {},
        el("td", {}, s.pair),
        el("td", {}, status),
        el("td", { class: sideClass }, direction || "—"),
        el("td", {}, lvl.price != null ? fmt.price(lvl.price) : "—"),
        el("td", {}, lvl.level_distance_pips != null ? lvl.level_distance_pips.toFixed(1) : "—"),
        el("td", {}, lvl.avoidance_distance_pips != null ? lvl.avoidance_distance_pips.toFixed(1) : "—"),
        el("td", {}, v ? votesPill(v.yes, v.total) : "—"),
        el("td", { class: "muted small" }, s.skip_reason || ""),
      ));
    }
  } catch (e) { console.error("stakan signals:", e); }
}

async function refreshStakanClosed() {
  try {
    const r = await api("/api/stakan/closed-trades");
    const tb = document.querySelector("#stakan-closed-table tbody");
    tb.innerHTML = "";
    for (const t of r.trades) {
      const sideClass = t.side === "BUY" ? "side-buy" : "side-sell";
      const resultClass = t.result === "WIN" ? "win" : "loss";
      const lvl = t.level_at_open || {};
      tb.appendChild(el("tr", {},
        el("td", {}, t.pair),
        el("td", { class: sideClass }, t.side),
        el("td", {}, fmt.price(t.open_price)),
        el("td", {}, fmt.price(t.close_price)),
        el("td", {}, lvl.price != null ? fmt.price(lvl.price) : "—"),
        el("td", { class: "muted" }, fmt.utc(t.open_time)),
        el("td", { class: "muted" }, fmt.utc(t.close_time)),
        el("td", { class: resultClass }, t.result),
        el("td", { class: resultClass }, fmt.pnl(t.pnl_usd)),
      ));
    }
    if (r.count === 0) {
      tb.appendChild(el("tr", {},
        el("td", { colspan: 9, class: "muted" }, "ещё ни одна стакан-сделка не закрыта")));
    }
  } catch (e) { console.error("stakan closed:", e); }
}

function tick() {
  $("last-refresh").textContent = new Date().toLocaleTimeString();
  refreshStats();
  refreshOpenTrades();
  refreshHealth();
  refreshAgents();
  refreshClosed();
  refreshStakanStats();
  refreshStakanOpen();
  refreshStakanSignals();
  refreshStakanClosed();
  refreshDailyStats();
  refreshDailyOpen();
  refreshDailySignals();
  refreshDailyPaused();
  refreshDailyClosed();
  refreshMarketRadar();
}

// ───── Daily Best Pick (3-я стратегия) ─────
async function refreshDailyStats() {
  try {
    const s = await api("/api/daily/stats");
    $("daily-stat-total").textContent = s.total ?? 0;
    $("daily-stat-wins").textContent = s.wins ?? 0;
    $("daily-stat-losses").textContent = s.losses ?? 0;
    $("daily-stat-wr").textContent = ((s.win_rate_pct ?? 0)).toFixed(1) + "%";
    $("daily-stat-rolling-wr").textContent = ((s.rolling_30_win_rate_pct ?? 0)).toFixed(1) + "%";
    const pnl = s.total_pnl_usd ?? 0;
    $("daily-stat-pnl").textContent = (pnl >= 0 ? "+$" : "-$") + Math.abs(pnl).toFixed(2);
    $("daily-stat-pnl").className = "big " + (pnl >= 0 ? "green" : "red");
  } catch (e) { console.error("daily stats:", e); }
}

async function refreshDailyOpen() {
  try {
    const trades = await api("/api/daily/open-trades");
    const tb = document.querySelector("#daily-open-table tbody");
    tb.innerHTML = "";
    $("daily-open-count").textContent = (trades || []).length;
    if (!trades || !trades.length) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 10, class: "muted" },
        "пока нет открытых сделок (ждём первый daily sweep — каждый день в 19:00 UTC = 00:00 UTC+5)")));
      return;
    }
    for (const t of trades) {
      const sideCls = t.side === "BUY" ? "green" : "red";
      const expiryAt = new Date(t.expiry_time);
      const remaining = Math.max(0, Math.floor((expiryAt - Date.now()) / 60000));
      const conf = (t.confidence_pct ?? 0).toFixed(1);
      const meta = (t.meta_score ?? 0).toFixed(1);
      const row = el("tr", {},
        el("td", { class: "mono" }, t.pair),
        el("td", { class: sideCls }, t.side),
        el("td", { class: "mono" }, (t.open_price ?? 0).toFixed(5)),
        el("td", { class: "mono" }, conf + "%"),
        el("td", { class: "mono" }, meta),
        el("td", { class: "mono" }, "$" + (t.stake_usd ?? 0).toFixed(2)),
        el("td", { class: "mono" }, remaining + " мин"),
        el("td", { class: "mono" }, "—"),
        el("td", { class: "mono" }, "—"),
        el("td", { class: "mono" }, "—"),
      );
      tb.appendChild(row);
    }
  } catch (e) { console.error("daily open:", e); }
}

function _fmtComp(comp) {
  if (!comp) return "—";
  const score = (comp.score ?? 0).toFixed(0);
  const sgn = comp.score > 0 ? "+" : "";
  return sgn + score;
}

async function refreshDailySignals() {
  try {
    const r = await api("/api/daily/signals");
    const tb = document.querySelector("#daily-signals-table tbody");
    tb.innerHTML = "";
    if (!r || !r.signals || !r.signals.length) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 11, class: "muted" },
        "ещё не считали — daily sweep запускается раз в день в " + (r.next_run_hour_utc ?? 19) + ":00 UTC")));
      return;
    }
    for (const s of r.signals) {
      const sideCls = s.side === "BUY" ? "green" : (s.side === "SELL" ? "red" : "muted");
      const conf = (s.confidence_pct ?? 0).toFixed(1) + "%";
      const meta = (s.meta_score ?? 0).toFixed(1);
      const c = s.components || {};
      const status = s.opened ? `OPEN ${s.expiry_hours}h $${s.stake_usd}` : (s.skip_reason || "—");
      const statusCls = s.opened ? "green" : "muted";
      const row = el("tr", {},
        el("td", { class: "mono" }, s.pair),
        el("td", { class: sideCls }, s.side || "—"),
        el("td", { class: "mono" }, conf),
        el("td", { class: "mono" }, meta),
        el("td", { class: "mono" }, _fmtComp(c.forecast_prob)),
        el("td", { class: "mono" }, _fmtComp(c.radar_score)),
        el("td", { class: "mono" }, _fmtComp(c.stakan_votes)),
        el("td", { class: "mono" }, _fmtComp(c.reversal_filter)),
        el("td", { class: "mono" }, _fmtComp(c.macro_tilt)),
        el("td", { class: "mono" }, _fmtComp(c.cot_z)),
        el("td", { class: statusCls }, status),
      );
      tb.appendChild(row);
    }
  } catch (e) { console.error("daily signals:", e); }
}

async function refreshDailyPaused() {
  try {
    const r = await api("/api/daily/paused");
    const node = $("daily-paused-list");
    if (!r || Object.keys(r).length === 0) {
      node.textContent = "Нет пауз — все 28 пар активны.";
      return;
    }
    node.innerHTML = "";
    for (const [pair, info] of Object.entries(r)) {
      const until = new Date(info.until).toLocaleString();
      node.appendChild(el("div", {},
        el("strong", {}, pair), ` paused until ${until} (rolling WR ${info.rolling_wr}%, ${info.trades_in_window} trades)`));
    }
  } catch (e) { console.error("daily paused:", e); }
}

async function refreshDailyClosed() {
  try {
    const trades = await api("/api/daily/closed-trades");
    const tb = document.querySelector("#daily-closed-table tbody");
    tb.innerHTML = "";
    if (!trades || !trades.length) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 9, class: "muted" }, "пусто")));
      return;
    }
    for (const t of trades.slice().reverse().slice(0, 50)) {
      const sideCls = t.side === "BUY" ? "green" : "red";
      const resultCls = t.result === "WIN" ? "green" : "red";
      const conf = (t.confidence_pct ?? 0).toFixed(1) + "%";
      const row = el("tr", {},
        el("td", { class: "mono" }, t.pair),
        el("td", { class: sideCls }, t.side),
        el("td", { class: "mono" }, conf),
        el("td", { class: "mono" }, (t.open_price ?? 0).toFixed(5)),
        el("td", { class: "mono" }, (t.close_price ?? 0).toFixed(5)),
        el("td", { class: "mono" }, t.open_time?.slice(0, 16).replace("T", " ") || ""),
        el("td", { class: "mono" }, t.close_time?.slice(0, 16).replace("T", " ") || ""),
        el("td", { class: resultCls }, t.result || "—"),
        el("td", { class: (t.pnl_usd >= 0 ? "green" : "red") }, "$" + (t.pnl_usd ?? 0).toFixed(2)),
      );
      tb.appendChild(row);
    }
  } catch (e) { console.error("daily closed:", e); }
}

// ───── PRO microstructure («что внутри рынка») ─────
async function refreshMicrostructure() {
  const btn = $("microstructure-refresh-btn");
  const status = $("microstructure-status");
  if (!btn) return;
  btn.disabled = true;
  status.textContent = "считаем 28 пар (~30-40 сек)...";
  try {
    const t0 = Date.now();
    const r = await api("/api/microstructure");
    const took = ((Date.now() - t0) / 1000).toFixed(1);
    const tb = document.querySelector("#microstructure-table tbody");
    tb.innerHTML = "";
    const pairs = r.pairs || {};
    const keys = Object.keys(pairs).sort();
    if (!keys.length) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 7, class: "muted" }, "пусто")));
      status.textContent = "пусто";
      return;
    }
    for (const pair of keys) {
      const p = pairs[pair];
      if (p.error) {
        tb.appendChild(el("tr", {},
          el("td", { class: "mono" }, pair),
          el("td", { colspan: 6, class: "muted" }, "ошибка: " + p.error)));
        continue;
      }
      const stage = p.wyckoff_stage || "UNKNOWN";
      const stagePill = el("span", { class: "wyckoff-pill " + stage }, stage);
      const dCls = p.delta_bias === "BUY" ? "green" : (p.delta_bias === "SELL" ? "red" : "muted");
      const deltaTxt = p.delta_norm_pct == null ? "—" : ((p.delta_norm_pct >= 0 ? "+" : "") + p.delta_norm_pct.toFixed(0) + "%");
      const hurst = p.hurst_H == null ? "—" : `${p.hurst_H} (${p.hurst_regime})`;
      const smc = `${p.n_order_blocks ?? 0} · ${p.n_fvgs ?? 0} · ${p.n_sweeps ?? 0} · ${p.n_whales ?? 0}`;
      const innerLines = (p.inner_facts || []).join(" · ") || "—";
      const outerLines = (p.outer_view || []).join(" · ") || "—";
      const row = el("tr", {},
        el("td", { class: "mono" }, pair),
        el("td", {}, stagePill),
        el("td", { class: dCls + " mono" }, deltaTxt),
        el("td", { class: "mono small" }, hurst),
        el("td", { class: "mono" }, smc),
        el("td", { class: "small" }, innerLines),
        el("td", { class: "small" }, outerLines),
      );
      tb.appendChild(row);
    }
    status.textContent = `обновлено за ${took}s`;
  } catch (e) {
    console.error("microstructure:", e);
    status.textContent = "ошибка: " + e.message;
  } finally {
    btn.disabled = false;
  }
}
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("microstructure-refresh-btn");
  if (btn) btn.addEventListener("click", refreshMicrostructure);
});

// ───── Market Radar (20 scanners × 28 pairs) ─────
async function refreshMarketRadar() {
  try {
    const r = await api("/api/market-radar");
    const tb = document.querySelector("#radar-table tbody");
    tb.innerHTML = "";
    const pairs = r.pairs || {};
    const pairKeys = Object.keys(pairs).sort();
    if (!pairKeys.length) {
      tb.appendChild(el("tr", {}, el("td", { colspan: 5, class: "muted" },
        "ещё не считали — ждём первый цикл market_radar (60 сек)")));
      return;
    }
    for (const pair of pairKeys) {
      const p = pairs[pair];
      const score = p.overall_score ?? 0;
      const dir = p.direction || "NEUTRAL";
      const dirCls = dir === "BUY" ? "green" : (dir === "SELL" ? "red" : "muted");
      const strong = (p.scanners_passing ?? 0) + " / " + (p.scanner_count ?? 0);
      // top 3 scanners by abs score
      const scanners = p.scanners || {};
      const ranked = Object.entries(scanners)
        .filter(([_, s]) => typeof s.score === "number")
        .sort((a, b) => Math.abs(b[1].score) - Math.abs(a[1].score))
        .slice(0, 3);
      const top = ranked.map(([n, s]) => `${n.replace(/^s\d+_/, "")}=${(s.score >= 0 ? "+" : "") + s.score.toFixed(0)}`).join(" · ");
      const row = el("tr", {},
        el("td", { class: "mono" }, pair),
        el("td", { class: dirCls }, dir),
        el("td", { class: "mono" }, (score >= 0 ? "+" : "") + score.toFixed(1)),
        el("td", { class: "mono" }, strong),
        el("td", { class: "small mono" }, top || "—"),
      );
      tb.appendChild(row);
    }
  } catch (e) { console.error("market radar:", e); }
}

function tickForecasts() {
  refreshForecasts();
  refreshStrategyMatrix();
  refreshMarketRegime();
  refreshWRFloor();
  refreshWeeklyLoss();
  refreshFundamentals();
  refreshCOT();
}

// ───── WR floor monitor (rolling 50 trades vs 70%) ─────
async function refreshWRFloor() {
  const node = $("wr-floor-content");
  if (!node) return;
  try {
    const r = await api("/api/wr-floor");
    node.innerHTML = "";
    if (!r || r.note) {
      node.appendChild(el("div", {}, r && r.note ? r.note : "пусто"));
      return;
    }
    const cls = r.below_floor ? "warn" : "ok";
    const wrTxt = r.wr_pct == null ? "—" : `${r.wr_pct}%`;
    const allTxt = r.wr_pct_all_time == null ? "—" : `${r.wr_pct_all_time}%`;
    node.appendChild(el("div", { class: cls === "ok" ? "" : "loss" },
      el("strong", {}, `${cls === "ok" ? "✓" : "⚠️"} rolling WR (${r.window} сделок): `),
      wrTxt,
      el("span", { class: "muted" }, ` · floor ${r.floor_pct}% · all-time ${allTxt}`)));
    node.appendChild(el("div", { class: "muted small", style: "margin-top:4px;" },
      r.alert || ""));
  } catch (e) {
    node.textContent = "ошибка: " + (e.message || e);
  }
}

// ───── Weekly loss review ─────
async function refreshWeeklyLoss() {
  const node = $("weekly-loss-content");
  if (!node) return;
  try {
    const r = await api("/api/weekly-loss-review");
    node.innerHTML = "";
    if (!r || r.note) {
      node.appendChild(el("div", {}, r && r.note ? r.note : "пусто"));
      return;
    }
    const head = el("div", {},
      el("strong", {}, `За 7 дней: ${r.n_total} сделок · ${r.n_wins} WIN / ${r.n_losses} LOSS · WR ${r.wr_pct}%`));
    node.appendChild(head);

    if (r.loss_by_pair_top5 && r.loss_by_pair_top5.length) {
      const txt = r.loss_by_pair_top5.map(([p, n]) => `${p} (${n})`).join(" · ");
      node.appendChild(el("div", {}, el("strong", {}, "Минусы по парам (топ-5): "), txt));
    }
    if (r.loss_by_session && Object.keys(r.loss_by_session).length) {
      const txt = Object.entries(r.loss_by_session).map(([s, n]) => `${s} (${n})`).join(" · ");
      node.appendChild(el("div", {}, el("strong", {}, "Минусы по сессиям: "), txt));
    }
    if (r.loss_by_hour_utc_top5 && r.loss_by_hour_utc_top5.length) {
      const txt = r.loss_by_hour_utc_top5.map(([h, n]) => `${String(h).padStart(2, "0")}:00 (${n})`).join(" · ");
      node.appendChild(el("div", {}, el("strong", {}, "Минусы по часам UTC (топ-5): "), txt));
    }
    if (r.loss_by_side && Object.keys(r.loss_by_side).length) {
      const txt = Object.entries(r.loss_by_side).map(([s, n]) => `${s} (${n})`).join(" · ");
      node.appendChild(el("div", {}, el("strong", {}, "Минусы по направлению: "), txt));
    }
    if (r.worst_pairs_wr_le_40pct && r.worst_pairs_wr_le_40pct.length) {
      const txt = r.worst_pairs_wr_le_40pct.slice(0, 5)
        .map(([p, wr, n]) => `${p} ${wr}% (${n})`).join(" · ");
      node.appendChild(el("div", { class: "loss" }, el("strong", {}, "⚠️ Худшие пары (WR≤40%, ≥3 сделок): "), txt));
    }
    if (r.advice) {
      node.appendChild(el("div", { class: "muted small", style: "margin-top:6px;" }, r.advice));
    }
    if (r.as_of) {
      node.appendChild(el("div", { class: "muted small" },
        `Обновлено: ${fmt.ago(r.as_of)}`));
    }
  } catch (e) {
    node.textContent = "ошибка: " + (e.message || e);
  }
}

// ───── COT speculator positioning ─────
async function refreshCOT() {
  const node = $("cot-content");
  if (!node) return;
  try {
    const r = await api("/api/cot");
    node.innerHTML = "";
    if (!r || r.note) {
      node.appendChild(el("div", {}, r && r.note ? r.note : "пусто"));
      return;
    }
    const ccy = r.currencies || (r.cot_raw || {}).currencies || {};
    // 1) Per-currency table
    const tt = el("table", { class: "trades-table compact" });
    tt.appendChild(el("tr", {},
      el("th", {}, "CCY"),
      el("th", {}, "net % OI"),
      el("th", {}, "z-score"),
      el("th", {}, "mean %"),
      el("th", {}, "stdev"),
      el("th", {}, "extreme?"),
    ));
    for (const code of ["EUR","GBP","JPY","CHF","AUD","CAD","NZD"]) {
      const v = ccy[code] || {};
      const z = v.z_score;
      let cls = "muted";
      if (typeof z === "number") {
        if (z > 1.5) cls = "loss";       // crowded long → contrarian sell
        else if (z < -1.5) cls = "win";  // crowded short → contrarian buy
      }
      tt.appendChild(el("tr", {},
        el("td", {}, el("strong", {}, code)),
        el("td", {}, fmtNum(v.net_pct_oi)),
        el("td", { class: cls }, fmtNum(v.z_score)),
        el("td", {}, fmtNum(v.mean_pct_oi)),
        el("td", {}, fmtNum(v.std_pct_oi)),
        el("td", {}, v.extreme || "—"),
      ));
    }
    node.appendChild(tt);

    // 2) Top contrarian signals
    const top = r.top_contrarian_signals || [];
    if (top.length) {
      node.appendChild(el("div", { class: "muted small", style: "margin-top:10px;" },
        "Top контра-сигналы (specs растянулись → ожидаем разворот):"));
      const ul = el("ul", { class: "compact" });
      for (const t of top) {
        const cls = t.side === "BUY" ? "win" : "loss";
        ul.appendChild(el("li", {},
          el("strong", { class: cls }, `${t.pair} ${t.side}`),
          ` · combined_z=${fmtNum(t.combined_z)}`,
          ` · сила ${fmtNum(t.strength_pct)}%`,
        ));
      }
      node.appendChild(ul);
    }

    // 3) all 28 pair signals (collapsible)
    const sigs = r.all_pair_signals || {};
    if (Object.keys(sigs).length) {
      const det = el("details", { style: "margin-top:8px;" });
      det.appendChild(el("summary", { class: "muted small" },
        `все ${Object.keys(sigs).length} пар — раскрыть`));
      const stt = el("table", { class: "trades-table compact" });
      stt.appendChild(el("tr", {},
        el("th", {}, "pair"),
        el("th", {}, "side"),
        el("th", {}, "combined z"),
        el("th", {}, "base z"),
        el("th", {}, "quote z"),
        el("th", {}, "strength%"),
      ));
      const arr = Object.entries(sigs).sort((a,b) => Math.abs(b[1].combined_z||0) - Math.abs(a[1].combined_z||0));
      for (const [p, v] of arr) {
        const cls = v.side === "BUY" ? "win" : (v.side === "SELL" ? "loss" : "muted");
        stt.appendChild(el("tr", {},
          el("td", {}, p),
          el("td", {}, el("span", { class: cls }, v.side || "?")),
          el("td", {}, fmtNum(v.combined_z)),
          el("td", {}, fmtNum(v.base_z)),
          el("td", {}, fmtNum(v.quote_z)),
          el("td", {}, fmtNum(v.strength_pct)),
        ));
      }
      det.appendChild(stt);
      node.appendChild(det);
    }
    if (r.source) {
      node.appendChild(el("div", { class: "muted small", style: "margin-top:6px;" }, r.source));
    }
  } catch (e) {
    node.textContent = "ошибка: " + (e.message || e);
  }
}

// ───── Fundamental macro (FRED rates / yields / CPI) ─────
async function refreshFundamentals() {
  const node = $("fundamentals-content");
  if (!node) return;
  try {
    const r = await api("/api/fundamentals");
    node.innerHTML = "";
    if (!r || r.note) {
      node.appendChild(el("div", {}, r && r.note ? r.note : "пусто"));
      return;
    }
    // 1) Per-currency rates table
    const ccy = (r.currencies || (r.fundamentals_raw || {}).currencies || {});
    const ccyTable = el("table", { class: "trades-table compact" });
    const head = el("tr", {},
      el("th", {}, "CCY"),
      el("th", {}, "policy rate %"),
      el("th", {}, "10y yield %"),
      el("th", {}, "CPI YoY %"),
    );
    ccyTable.appendChild(head);
    for (const code of ["USD","EUR","GBP","JPY","CHF","AUD","CAD","NZD"]) {
      const v = ccy[code] || {};
      const pr = v.policy_rate ?? (v.policy_rate?.value);
      const yy = v["10y_yield"] ?? (v["10y_yield"]?.value);
      const cp = v.cpi_yoy_pct ?? (v.cpi?.yoy_pct);
      ccyTable.appendChild(el("tr", {},
        el("td", {}, el("strong", {}, code)),
        el("td", {}, fmtNum(pr)),
        el("td", {}, fmtNum(yy)),
        el("td", {}, fmtNum(cp)),
      ));
    }
    node.appendChild(ccyTable);

    // 2) Top biased pairs (highest |tilt_score|)
    const top = r.top_bias_pairs || [];
    if (top.length) {
      node.appendChild(el("div", { class: "muted small", style: "margin-top:10px;" },
        "Top‑10 пар с самым сильным fundamental bias:"));
      const ul = el("ul", { class: "compact" });
      for (const t of top.slice(0, 10)) {
        const cls = t.side === "BUY" ? "win" : (t.side === "SELL" ? "loss" : "muted");
        ul.appendChild(el("li", {},
          el("strong", { class: cls }, `${t.pair} ${t.side}`),
          " · score ",
          fmtNum(t.tilt_score),
          ` · conf ${fmtNum(t.confidence_pct)}%`,
        ));
      }
      node.appendChild(ul);
    }

    // 3) All 28 pair tilts table (collapsed by default)
    const tilts = r.all_pair_tilts || {};
    const npairs = Object.keys(tilts).length;
    if (npairs) {
      const det = el("details", { style: "margin-top:8px;" });
      det.appendChild(el("summary", { class: "muted small" },
        `все ${npairs} пар — раскрыть`));
      const tt = el("table", { class: "trades-table compact" });
      tt.appendChild(el("tr", {},
        el("th", {}, "pair"),
        el("th", {}, "side"),
        el("th", {}, "tilt"),
        el("th", {}, "rate Δ%"),
        el("th", {}, "10y Δ%"),
        el("th", {}, "cpi Δ%"),
        el("th", {}, "conf%"),
      ));
      const arr = Object.entries(tilts).sort((a,b) => Math.abs(b[1].tilt_score||0) - Math.abs(a[1].tilt_score||0));
      for (const [p, v] of arr) {
        const cls = v.side === "BUY" ? "win" : (v.side === "SELL" ? "loss" : "muted");
        tt.appendChild(el("tr", {},
          el("td", {}, p),
          el("td", {}, el("span", { class: cls }, v.side || "?")),
          el("td", {}, fmtNum(v.tilt_score)),
          el("td", {}, fmtNum(v.rate_diff_pct)),
          el("td", {}, fmtNum(v.yield_diff_pct)),
          el("td", {}, fmtNum(v.cpi_diff_pct)),
          el("td", {}, fmtNum(v.confidence_pct)),
        ));
      }
      det.appendChild(tt);
      node.appendChild(det);
    }

    // 4) source attribution
    if (r.source) {
      node.appendChild(el("div", { class: "muted small", style: "margin-top:6px;" }, r.source));
    }
  } catch (e) {
    node.textContent = "ошибка: " + (e.message || e);
  }
}

function fmtNum(v) {
  if (v === undefined || v === null || (typeof v === "number" && !isFinite(v))) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  return Math.abs(n) >= 100 ? n.toFixed(1) : n.toFixed(2);
}

// ───── 365-day market regime ─────
async function refreshMarketRegime() {
  const node = $("market-regime-content");
  if (!node) return;
  try {
    const r = await api("/api/market-regime");
    if (!r || !r.pairs || !r.global_hot_hours_utc_top10 || r.global_hot_hours_utc_top10.length === 0) {
      node.innerHTML = "";
      node.appendChild(el("div", {},
        r && r.note ? r.note : "ещё не вычислено — запусти `python -m teamagent.market_regime_analyzer`"));
      return;
    }
    node.innerHTML = "";
    // глобальные hot hours
    const ghh = r.global_hot_hours_utc_top10.slice(0, 5).map(h =>
      `${String(h.hour_utc).padStart(2, "0")}:00 ${h.session} (${h.mean_abs_ret_bp_avg} bp)`
    ).join(" · ");
    node.appendChild(el("div", {}, el("strong", {}, "🔥 Топ-5 hot hours UTC по 28 парам: "), ghh));

    // топ "движущихся" пар
    const moves = Object.entries(r.pairs)
      .filter(([_, p]) => p.vol_thresholds)
      .map(([pair, p]) => ({ pair, mean: p.vol_thresholds.mean_abs_ret_bp }))
      .sort((a, b) => b.mean - a.mean);
    const topMove = moves.slice(0, 5).map(m => `${m.pair} ${m.mean}bp`).join(" · ");
    const calmMove = moves.slice(-5).map(m => `${m.pair} ${m.mean}bp`).join(" · ");
    node.appendChild(el("div", {}, el("strong", {}, "💨 Самые волатильные: "), topMove));
    node.appendChild(el("div", {}, el("strong", {}, "🧊 Самые тихие: "), calmMove));

    // обновлено когда
    if (r.as_of) {
      node.appendChild(el("div", { class: "muted", style: "margin-top:6px;" },
        `Обновлено: ${fmt.ago(r.as_of)} · lookback ${r.lookback_days || 365} дней · pairs ${r.pairs_analyzed || Object.keys(r.pairs).length}`));
    }
  } catch (e) {
    node.textContent = "ошибка: " + (e.message || e);
  }
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

function tickClock() {
  const now = new Date();
  const utc = now.toISOString().slice(11, 19);
  const localMs = now.getTime() + 5 * 3600 * 1000;
  const utc5 = new Date(localMs).toISOString().slice(11, 19);
  $("clock-utc").textContent = utc;
  $("clock-utc5").textContent = utc5;
  // Текущая FX-сессия (по UTC часу, как у backend)
  const h = now.getUTCHours();
  let sess;
  if (h < 7) sess = "Asia (00–07 UTC)";
  else if (h < 13) sess = "London (07–13 UTC)";
  else if (h < 17) sess = "Overlap (13–17 UTC)";
  else if (h < 22) sess = "NY (17–22 UTC)";
  else sess = "off-hours (22–24 UTC)";
  $("clock-session").textContent = "сессия: " + sess;
}

document.addEventListener("DOMContentLoaded", () => {
  $("manual-refresh").addEventListener("click", () => { tick(); tickForecasts(); refreshVP(); });
  $("vp-refresh").addEventListener("click", refreshVP);
  $("vp-pair").addEventListener("change", refreshVP);

  populateVPDropdown().then(refreshVP);

  tickClock();
  tick();
  tickForecasts();
  setInterval(tickClock, 1000);
  setInterval(tick, REFRESH_LIVE_MS);
  setInterval(tickForecasts, REFRESH_FORECASTS_MS);
});
