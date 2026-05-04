/**
 * /trades — единое место где собраны все open + closed сделки.
 *
 * Задача: показать пользователю единый раздел "Сделки" с:
 *   1) Сводкой (total / open / wins / losses / WR / PnL)
 *   2) Открытыми сделками с live-PnL
 *   3) WR разбивкой по парам (диагностика — какие пары работают, какие сливают)
 *   4) Историей закрытых сделок с источником стратегии
 *
 * Все API проходят через static-shim, поэтому страница работает и на Fly,
 * и на static-build mirror (где shim перенаправляет /api/* на live или baked).
 */

"use strict";

const $ = (id) => document.getElementById(id);

const fmt = {
  num(x, d = 0) {
    if (x == null || !isFinite(x)) return "—";
    return Number(x).toFixed(d);
  },
  price(x) {
    if (x == null || !isFinite(x)) return "—";
    const n = Number(x);
    return n >= 100 ? n.toFixed(3) : n.toFixed(5);
  },
  pips(x) {
    if (x == null || !isFinite(x)) return "—";
    const sign = x >= 0 ? "+" : "";
    return `${sign}${Number(x).toFixed(1)}`;
  },
  pnl(x) {
    if (x == null || !isFinite(x)) return "—";
    const sign = x >= 0 ? "+$" : "−$";
    return `${sign}${Math.abs(Number(x)).toFixed(2)}`;
  },
  pct(x, d = 1) {
    if (x == null || !isFinite(x)) return "—";
    return `${Number(x).toFixed(d)}%`;
  },
  utc(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      // dd.MM HH:mm in user's locale
      return d.toLocaleString(undefined, {
        month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
      });
    } catch (_) { return iso; }
  },
  countdown(secs) {
    if (secs == null || !isFinite(secs) || secs <= 0) return "истекла";
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = Math.floor(secs % 60);
    if (h > 0) return `${h}ч ${String(m).padStart(2, "0")}м`;
    if (m > 0) return `${m}м ${String(s).padStart(2, "0")}с`;
    return `${s}с`;
  },
};

async function fetchJson(url, fallback = null) {
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${url} → ${r.status}`);
    return await r.json();
  } catch (e) {
    console.warn("fetch failed:", url, e);
    return fallback;
  }
}

function renderStats(stats, openCount) {
  $("ts-total").textContent = stats.total ?? 0;
  $("ts-open").textContent = openCount ?? 0;
  $("ts-wins").textContent = stats.wins ?? 0;
  $("ts-losses").textContent = stats.losses ?? 0;
  const wr = Number(stats.win_rate_pct ?? 0);
  const wrEl = $("ts-wr");
  wrEl.textContent = `${wr.toFixed(1)}%`;
  wrEl.className = "val " + (wr >= 70 ? "green" : wr >= 50 ? "amber" : "red");
  const pnl = Number(stats.total_pnl_usd ?? 0);
  const pnlEl = $("ts-pnl");
  pnlEl.textContent = fmt.pnl(pnl);
  pnlEl.className = "val " + (pnl > 0 ? "green" : pnl < 0 ? "red" : "");
}

function renderOpenTrades(payload) {
  const trades = payload && payload.trades ? payload.trades : [];
  $("tr-open-count").textContent = trades.length;
  const tbody = $("tr-open-tbody");
  tbody.innerHTML = "";
  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="tr-empty">сейчас нет открытых сделок (paper-trader откроет когда вероятность ≥ 70%)</td></tr>`;
    return;
  }
  for (const t of trades) {
    const live = t.live || {};
    const sideClass = t.side === "BUY" ? "side-buy" : "side-sell";
    const inMoney = live.in_money_now;
    const okLabel = inMoney === true ? "✓ да" : inMoney === false ? "✗ нет" : "—";
    const okClass = inMoney === true ? "win" : inMoney === false ? "loss" : "muted";
    const pipsClass = live.pips == null ? "muted" : (live.pips >= 0 ? "win" : "loss");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><b>${t.pair}</b></td>
      <td class="${sideClass}">${t.side}</td>
      <td>${fmt.price(t.open_price)}</td>
      <td class="muted">${fmt.utc(t.open_time)}</td>
      <td>${fmt.countdown(live.time_remaining_sec)}</td>
      <td>${fmt.price(live.current_price)}</td>
      <td class="${pipsClass}">${fmt.pips(live.pips)}</td>
      <td class="${okClass}">${fmt.pnl(live.projected_payout)}</td>
      <td class="${okClass}">${okLabel}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderClosedHistory(payload) {
  const trades = payload && payload.trades ? payload.trades : [];
  $("tr-closed-count").textContent = trades.length;
  const tbody = $("tr-closed-tbody");
  tbody.innerHTML = "";
  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="tr-empty">истории сделок ещё нет</td></tr>`;
    return;
  }
  for (const t of trades) {
    const sideClass = t.side === "BUY" ? "side-buy" : "side-sell";
    const result = t.result || "—";
    const win = result === "WIN";
    const resultClass = win ? "win" : (result === "LOSS" ? "loss" : "muted");
    const pnl = Number(t.pnl_usd ?? 0);
    const pnlClass = pnl > 0 ? "win" : pnl < 0 ? "loss" : "muted";
    const prob = t.probability_pct_at_open != null ? fmt.pct(t.probability_pct_at_open) : "—";
    const variant = t.strategy_variant_at_open || "—";
    const variantShort = variant.length > 22 ? variant.slice(0, 21) + "…" : variant;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><b>${t.pair}</b></td>
      <td class="${sideClass}">${t.side}</td>
      <td>${fmt.price(t.open_price)}</td>
      <td>${fmt.price(t.close_price)}</td>
      <td class="muted">${fmt.utc(t.open_time)}</td>
      <td class="muted">${fmt.utc(t.close_time)}</td>
      <td>${prob}</td>
      <td class="muted" title="${variant}">${variantShort}</td>
      <td class="${resultClass}"><b>${result}</b></td>
      <td class="${pnlClass}"><b>${fmt.pnl(pnl)}</b></td>
    `;
    tbody.appendChild(tr);
  }
}

function renderByPair(closed) {
  const grid = $("tr-by-pair-grid");
  const trades = closed && closed.trades ? closed.trades : [];
  if (!trades.length) {
    grid.innerHTML = `<div class="muted">недостаточно закрытых сделок для статистики</div>`;
    $("tr-by-pair-meta").textContent = "0 пар";
    return;
  }
  const byPair = {};
  for (const t of trades) {
    const p = t.pair;
    if (!byPair[p]) byPair[p] = { wins: 0, losses: 0, total: 0, pnl: 0 };
    byPair[p].total += 1;
    byPair[p].pnl += Number(t.pnl_usd ?? 0);
    if (t.result === "WIN") byPair[p].wins += 1;
    else if (t.result === "LOSS") byPair[p].losses += 1;
  }
  const arr = Object.entries(byPair).map(([pair, s]) => ({
    pair, ...s,
    wr: s.total > 0 ? (s.wins / s.total) * 100 : 0,
  }));
  arr.sort((a, b) => b.wr - a.wr || b.total - a.total);
  $("tr-by-pair-meta").textContent = `${arr.length} ${arr.length === 1 ? "пара" : "пар"} · ${trades.length} сделок`;
  grid.innerHTML = "";
  for (const s of arr) {
    const cellClass = s.wr >= 70 ? "win" : s.wr < 50 ? "loss" : "";
    const div = document.createElement("div");
    div.className = `tr-pair-cell ${cellClass}`;
    div.innerHTML = `
      <span class="name">${s.pair}</span>
      <span class="wr">${s.wr.toFixed(0)}%</span>
      <span class="muted small">${s.wins}W/${s.losses}L</span>
    `;
    grid.appendChild(div);
  }
}

function setStatus(ok, text) {
  const dot = $("tr-status-dot");
  const txt = $("tr-status-text");
  if (dot) dot.style.background = ok ? "#4afaa3" : "#ff8090";
  if (txt) txt.textContent = text;
}

function setClock() {
  const el = $("fx-clock");
  if (!el) return;
  const now = new Date();
  const utc = now.toISOString().slice(11, 19);
  el.textContent = `${utc} UTC`;
}

function renderPlaybook(pb) {
  const grid = $("tr-playbook-grid");
  const meta = $("tr-playbook-meta");
  if (!grid) return;
  if (!pb || !pb.summary || !pb.summary.total_cells) {
    grid.innerHTML = `<div class="muted">${(pb && pb.summary && pb.summary.note) || "playbook ещё строится…"}</div>`;
    if (meta) meta.textContent = "—";
    return;
  }
  const s = pb.summary;
  if (meta) {
    meta.textContent = `${s.total_cells} ячеек · 🛡️${s.storm_proof || 0} · ✓${s.qualified || 0} · ${s.probable || 0}prob · ${s.frozen || 0}frozen`;
  }
  const cells = Array.isArray(pb.cells) ? pb.cells.slice() : [];
  cells.sort((a, b) => {
    const order = { STORM_PROOF: 0, QUALIFIED: 1, PROBABLE: 2, FROZEN: 3, INSUFFICIENT: 4 };
    return (order[a.status] ?? 5) - (order[b.status] ?? 5)
        || (b.wr_pct || 0) - (a.wr_pct || 0);
  });
  grid.innerHTML = "";
  for (const c of cells) {
    const wrCls = c.wr_pct >= 70 ? "win" : c.wr_pct < 50 ? "loss" : "";
    const stormBadge = c.storm_proof ? "🛡️ " : "";
    const div = document.createElement("div");
    div.className = `tr-pair-cell ${wrCls}`;
    div.innerHTML = `
      <span class="name">${stormBadge}${c.pair} · ${c.session}</span>
      <span class="muted small">${c.regime}</span>
      <span class="wr">${(c.wr_pct ?? 0).toFixed(0)}%</span>
      <span class="muted small">n=${c.n_trades || 0} · Wilson≥${(c.wilson_lower_pct ?? 0).toFixed(0)}%</span>
      <span class="muted small">${c.side_bias || ""} · ${c.status}</span>
    `;
    grid.appendChild(div);
  }
}

async function refresh() {
  setStatus(true, "обновляю…");
  const [stats, open, closed, playbook] = await Promise.all([
    fetchJson("/api/stats", {}),
    fetchJson("/api/open-trades", { trades: [], count: 0 }),
    fetchJson("/api/closed-trades?limit=100", { trades: [], count: 0 }),
    fetchJson("/api/playbook", { summary: {}, cells: [] }),
  ]);
  renderStats(stats || {}, open ? open.count : 0);
  renderOpenTrades(open || {});
  renderClosedHistory(closed || {});
  renderByPair(closed || {});
  renderPlaybook(playbook || {});
  setStatus(true, "live");
  // build stamp
  if (stats && stats.as_of) {
    $("tr-build").textContent = "обновлено " + fmt.utc(stats.as_of);
  }
}

setClock();
setInterval(setClock, 1000);
refresh();
setInterval(refresh, 30 * 1000);
