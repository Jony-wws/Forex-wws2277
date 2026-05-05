/* FX INVESTMENT — Live Market Intent UI
 * Uses existing TeamAgent endpoints:
 *   /api/forecasts        — 28 пар: side, prob, indicators (cached, fast)
 *   /api/market-radar     — 20+ scanner overall_score per pair
 *   /api/cot              — Commitment of Traders по 8 валютам
 *   /api/open-trades      — открытые сделки daily-стратегии
 *   /api/stakan/open-trades — открытые сделки stakan-стратегии
 *   /api/stakan/signals   — текущие stakan-сигналы (если есть)
 *   /api/microstructure/{pair} — DEEP dive on demand (modal)
 *   /api/forecast/{pair}  — full indicator stack on demand
 *
 * Updates every 10 sec. No fakes, no random — all values from real backend state.
 */

(() => {
  const PAIRS_MAJORS = new Set([
    "EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","NZDUSD","USDCAD",
    "EURGBP","EURJPY","GBPJPY",
  ]);
  const CCY_LIST = ["USD","EUR","GBP","JPY","AUD","NZD","CAD","CHF"];

  const REFRESH_MS = 10_000; // user explicitly asked: 10 sec
  const CHART_BARS_BACKFILL = 90;

  const grid = document.getElementById("fx-grid");
  const statusDot = document.getElementById("fx-status-dot");
  const statusText = document.getElementById("fx-status-text");
  const clockEl = document.getElementById("fx-clock");
  const summaryMarket = document.getElementById("fx-market-state");
  const summaryTopBuy = document.getElementById("fx-top-buy");
  const summaryTopSell = document.getElementById("fx-top-sell");
  const summaryNextTick = document.getElementById("fx-next-tick");
  const strengthEl = document.getElementById("fx-strength");
  const filterChips = Array.from(document.querySelectorAll(".fx-chip"));
  const searchInput = document.getElementById("fx-search-input");
  const buildEl = document.getElementById("fx-build");
  const deepDlg = document.getElementById("fx-deep");
  const deepBody = document.getElementById("fx-deep-body");
  deepDlg.querySelector(".fx-deep-close").addEventListener("click", () => deepDlg.close());

  let state = {
    forecasts: {},
    radar: {},
    cot: {},
    openTrades: [],
    stakanOpen: [],
    stakanSignals: [],
    dailySignals: [],
    activeFilter: "all",
    search: "",
    cards: new Map(),    // pair -> {root, chart, series, lastBars}
    lastFetchedAt: 0,
    nextTickAt: 0,
  };

  // ─── helpers ───────────────────────────────────────────
  function fmtPrice(p) {
    if (p == null || !isFinite(p)) return "—";
    return p >= 50 ? p.toFixed(3) : p.toFixed(5);
  }
  function fmtPct(p, digits = 1) {
    if (p == null || !isFinite(p)) return "—";
    return p.toFixed(digits) + "%";
  }
  function clamp(x, a, b) { return Math.max(a, Math.min(b, x)); }
  function pairBaseQuote(pair) {
    return [pair.slice(0,3), pair.slice(3,6)];
  }
  function toFixedSafe(x, d=1) {
    if (x == null || !isFinite(x)) return "—";
    return Number(x).toFixed(d);
  }
  function fmtCount(n) { return (n == null ? "—" : String(n)); }

  // pressure score 0..100 (0 = full sell, 50 = neutral, 100 = full buy)
  function pressureScore(f, radarScore) {
    const ind = f.indicators?.["1H"] || f.indicators?.["15m"] || {};
    // Forecast probability: 0..100, side BUY/SELL
    const probSigned = (f.side === "SELL" ? -1 : 1) * (f.probability ?? 0);
    // OFI: -1..+1
    const ofi = ind.ofi10 ?? 0;
    // BBP: -1..+1 ish
    const bbp = clamp(ind.bbp ?? 0, -.01, .01) * 100;
    // CEI: 0..100, recenter
    const cei = ((ind.cei10 ?? 50) - 50) / 50;
    // Mom5 in pip-distance, use sign weight
    const mom = clamp((ind.mom5 ?? 0), -2, 2) / 2;
    // Radar: -100..+100
    const rad = (radarScore ?? 0) / 100;
    // weighted
    const buy = (probSigned * 0.6) + (rad * 0.4 + ofi * 0.4 + bbp * 0.4 + cei * 0.4 + mom * 0.4) * 25;
    return clamp(50 + buy * 0.5, 2, 98);
  }

  function priceTargets(f) {
    const ind = f.indicators?.["1H"] || f.indicators?.["4H"] || {};
    const px = f.current_price;
    const atr = ind.atr14;
    if (!isFinite(px) || !isFinite(atr)) return null;
    const dir = f.side === "SELL" ? -1 : 1;
    const conf = (f.probability ?? 0) / 100;
    const tgt = px + dir * atr * (1 + conf);
    const reverse = px - dir * atr * 1.2;
    return { px, atr, target: tgt, reverse, hours: f.recommended_hours };
  }

  // ─── data fetch ───────────────────────────────────────
  // Build absolute URL from current origin with NO userinfo. Chrome's fetch()
  // refuses to construct a Request when the URL contains credentials (e.g. when
  // the page was opened via https://user:pass@host/...) so we re-anchor every
  // request to a clean origin.
  function abs(url) {
    if (/^https?:\/\//i.test(url)) return url;
    const proto = location.protocol;
    const host = location.host; // host:port without userinfo
    return `${proto}//${host}${url.startsWith("/") ? "" : "/"}${url}`;
  }
  async function fetchJson(url) {
    const r = await fetch(abs(url), { cache: "no-store", credentials: "include" });
    if (!r.ok) {
      const err = `${r.status} ${url}`;
      window.__lastFetchErr = err;
      throw new Error(err);
    }
    return r.json();
  }

  async function refresh() {
    try {
      const [fc, radar, cot, ot, sot, ssig, dsig] = await Promise.all([
        fetchJson("/api/forecasts").catch(() => ({forecasts: {}})),
        fetchJson("/api/market-radar").catch(() => ({pairs: {}})),
        fetchJson("/api/cot").catch(() => ({currencies: {}})),
        fetchJson("/api/open-trades").catch(() => ({trades: []})),
        fetchJson("/api/stakan/open-trades").catch(() => ({trades: []})),
        fetchJson("/api/stakan/signals").catch(() => ({signals: []})),
        fetchJson("/api/daily/signals").catch(() => ({signals: []})),
      ]);
      state.forecasts = fc.forecasts || {};
      state.radar = radar.pairs || {};
      state.cot = cot.currencies || {};
      state.openTrades = ot.trades || [];
      state.stakanOpen = sot.trades || [];
      state.stakanSignals = ssig.signals || [];
      state.dailySignals = dsig.signals || [];
      state.lastFetchedAt = Date.now();
      state.nextTickAt = state.lastFetchedAt + REFRESH_MS;
      window.__fxstate = state;
      setStatus(true);
      renderAll();
    } catch (e) {
      console.error(e);
      setStatus(false, e.message);
    }
  }

  function setStatus(ok, msg) {
    statusDot.classList.toggle("bad", !ok);
    statusText.textContent = ok ? "live" : (msg || "офлайн");
  }

  // ─── rendering ────────────────────────────────────────
  function pairsSorted() {
    const all = Object.values(state.forecasts);
    // Sort by absolute "intent" — most lopsided first
    return all.sort((a, b) => {
      const sa = Math.abs((a.probability_pct ?? a.probability ?? 0) - 50);
      const sb = Math.abs((b.probability_pct ?? b.probability ?? 0) - 50);
      return sb - sa;
    });
  }

  function passesFilter(f) {
    const pair = f.pair;
    if (state.search) {
      const q = state.search.toUpperCase();
      if (!pair.includes(q)) return false;
    }
    switch (state.activeFilter) {
      case "majors": return PAIRS_MAJORS.has(pair);
      case "buy": return f.side === "BUY" && (f.probability_pct ?? 0) >= 55;
      case "sell": return f.side === "SELL" && (f.probability_pct ?? 0) >= 55;
      case "reversal": {
        const ind = f.indicators?.["1H"] || {};
        const rsi = ind.rsi14 ?? 50;
        const bb = ind.bb_pct ?? .5;
        return rsi >= 70 || rsi <= 30 || bb >= .9 || bb <= .1;
      }
      case "active": {
        return state.openTrades.some(t => t.pair === pair) ||
               state.stakanOpen.some(t => t.pair === pair);
      }
      default: return true;
    }
  }

  function topMover(side) {
    const fs = Object.values(state.forecasts).filter(f => f.side === side);
    if (!fs.length) return "—";
    fs.sort((a, b) => (b.probability_pct ?? 0) - (a.probability_pct ?? 0));
    const w = fs[0];
    return `${w.pair} ${toFixedSafe(w.probability_pct, 0)}%`;
  }

  function strengthScores() {
    // For each currency, average net buy-bias from all pairs containing it.
    const scores = {};
    for (const c of CCY_LIST) scores[c] = { sum: 0, n: 0 };
    for (const f of Object.values(state.forecasts)) {
      const [base, quote] = pairBaseQuote(f.pair);
      const baseScore = (f.side === "BUY" ? 1 : -1) * ((f.probability_pct ?? 50) - 50);
      if (scores[base]) { scores[base].sum += baseScore; scores[base].n += 1; }
      if (scores[quote]) { scores[quote].sum -= baseScore; scores[quote].n += 1; }
    }
    const out = {};
    for (const c of CCY_LIST) {
      out[c] = scores[c].n > 0 ? scores[c].sum / scores[c].n : 0;
    }
    return out;
  }

  function renderStrength() {
    const sc = strengthScores();
    const max = Math.max(1, ...Object.values(sc).map(v => Math.abs(v)));
    strengthEl.innerHTML = "";
    const sorted = CCY_LIST.slice().sort((a, b) => sc[b] - sc[a]);
    for (const c of sorted) {
      const v = sc[c];
      const cell = document.createElement("div");
      cell.className = "fx-strength-cell";
      const pct = clamp(50 + (v / max) * 50, 5, 95);
      cell.innerHTML = `
        <div class="bar" style="background: linear-gradient(90deg, ${v >= 0 ? "rgba(32,227,165,.55)" : "rgba(255,85,119,.55)"}, transparent ${pct}%); width: 100%; opacity: ${.15 + Math.abs(v)/max * .35};"></div>
        <div class="ccy">${c}</div>
        <div class="score">${v >= 0 ? "+" : ""}${v.toFixed(0)}</div>
      `;
      strengthEl.appendChild(cell);
    }
  }

  function renderSummary() {
    const total = Object.keys(state.forecasts).length;
    summaryMarket.textContent = total ? `${total} пар · live` : "нет данных";
    summaryTopBuy.textContent = topMover("BUY");
    summaryTopSell.textContent = topMover("SELL");
    updateNextTick();
  }

  function updateNextTick() {
    const left = Math.max(0, state.nextTickAt - Date.now());
    const sec = Math.ceil(left / 1000);
    summaryNextTick.textContent = `${sec}s`;
  }

  function renderClock() {
    const d = new Date();
    const utc = d.toISOString().slice(11, 19);
    const utc5 = new Date(d.getTime() + 5*3600*1000).toISOString().slice(11, 19);
    clockEl.textContent = `${utc} UTC · ${utc5} +5`;
  }

  function ensureCard(pair) {
    let c = state.cards.get(pair);
    if (c) return c;
    const root = document.createElement("article");
    root.className = "fx-card";
    root.dataset.pair = pair;
    root.innerHTML = `
      <div class="fx-card-head">
        <div>
          <div class="fx-card-pair">${pair}</div>
          <div class="fx-card-price" data-price>—</div>
        </div>
        <div class="fx-card-side" data-side>—</div>
      </div>
      <div class="fx-press">
        <div class="fx-press-fill" data-press></div>
        <div class="fx-press-mid"></div>
        <div class="fx-press-labels"><span class="l">SELL</span><span class="r">BUY</span></div>
      </div>
      <div class="fx-chart" data-chart></div>
      <div class="fx-metrics" data-metrics></div>
      <div class="fx-forecasts" data-forecasts></div>
      <div class="fx-targets" data-targets></div>
      <div class="fx-tags" data-tags></div>
    `;
    root.addEventListener("click", () => openDeep(pair));
    grid.appendChild(root);

    // Chart (lightweight-charts) — guarded if library failed to load
    let chart = null, series = null;
    try {
      if (typeof LightweightCharts === "undefined") {
        throw new Error("LightweightCharts library not loaded");
      }
      chart = LightweightCharts.createChart(root.querySelector("[data-chart]"), {
        layout: { background: { color: "transparent" }, textColor: "rgba(149,163,196,.7)", fontSize: 9 },
        grid: { vertLines: { color: "rgba(124,92,255,.06)" }, horzLines: { color: "rgba(124,92,255,.06)" } },
        rightPriceScale: { borderColor: "rgba(124,92,255,.15)", scaleMargins: { top: .15, bottom: .1 } },
        timeScale: { borderColor: "rgba(124,92,255,.15)", timeVisible: false, secondsVisible: false },
        crosshair: { mode: 0 },
        autoSize: true,
        handleScroll: false, handleScale: false,
      });
      series = chart.addAreaSeries({
        topColor: "rgba(0,225,255,.45)", bottomColor: "rgba(0,225,255,.04)", lineColor: "rgba(0,225,255,.95)",
        lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
      });
    } catch (e) {
      console.warn("chart init failed for", pair, e);
    }
    c = { root, chart, series, lastBars: [], chartLoadedAt: 0 };
    state.cards.set(pair, c);
    return c;
  }

  async function loadChart(pair) {
    const c = state.cards.get(pair);
    if (!c || !c.series) return;
    if (Date.now() - c.chartLoadedAt < 60_000) return; // refresh chart at most every 60s
    try {
      const data = await fetchJson(`/api/intent-bars/${pair}?interval=15m&n=${CHART_BARS_BACKFILL}`);
      if (!data.bars || !data.bars.length) return;
      const series = data.bars.map(b => ({ time: b.time, value: b.close }));
      // Add live current price as last point if newer than last bar
      const f = state.forecasts[pair];
      if (f && isFinite(f.current_price)) {
        const nowT = Math.floor(Date.now() / 1000);
        const last = series[series.length - 1];
        if (last && nowT - last.time > 60) {
          series.push({ time: nowT, value: f.current_price });
        }
      }
      c.lastBars = series;
      c.series.setData(series);
      c.chartLoadedAt = Date.now();
    } catch (e) { console.warn("chart load", pair, e); }
  }

  function renderCard(f) {
    const c = ensureCard(f.pair);
    const root = c.root;

    const side = f.side || "NEUTRAL";
    root.classList.toggle("sell", side === "SELL");
    root.classList.toggle("neutral", side !== "SELL" && side !== "BUY");

    root.querySelector("[data-price]").textContent = `px ${fmtPrice(f.current_price)}`;
    root.querySelector("[data-side]").textContent = `${side} ${toFixedSafe(f.probability_pct, 0)}%`;

    const radarScore = state.radar[f.pair]?.overall_score ?? 0;

    // pressure 0..100
    const press = pressureScore(f, radarScore);
    const fill = root.querySelector("[data-press]");
    if (press >= 50) {
      fill.classList.add("buy"); fill.classList.remove("sell");
      fill.style.left = "50%";
      fill.style.width = ((press - 50) * 2) + "%";
    } else {
      fill.classList.add("sell"); fill.classList.remove("buy");
      fill.style.width = ((50 - press) * 2) + "%";
      fill.style.left = press + "%";
    }

    // metrics
    const ind = f.indicators?.["1H"] || f.indicators?.["15m"] || {};
    const ind4h = f.indicators?.["4H"] || ind;
    const rsi = ind.rsi14 ?? null;
    const atrPct = (ind.atr14 && f.current_price) ? (ind.atr14 / f.current_price) * 100 : null;
    const ofi = ind.ofi10 ?? null;
    const cei = ind.cei10 ?? null;
    const bbp = ind.bb_pct ?? null;

    const cot1 = state.cot[pairBaseQuote(f.pair)[0]];
    const cot2 = state.cot[pairBaseQuote(f.pair)[1]];

    function tone(v, low, high, invert=false) {
      if (v == null) return "weak";
      if (invert) [low, high] = [high, low];
      if ((!invert && v >= high) || (invert && v <= high)) return "pos";
      if ((!invert && v <= low)  || (invert && v >= low))  return "neg";
      return "neutral";
    }

    const metrics = root.querySelector("[data-metrics]");
    metrics.innerHTML = `
      <div class="fx-metric">
        <div class="l">RSI 1H</div>
        <div class="v ${rsi == null ? "weak" : (rsi >= 70 ? "neg" : rsi <= 30 ? "pos" : "neutral")}">${toFixedSafe(rsi, 1)}</div>
      </div>
      <div class="fx-metric">
        <div class="l">ATR%</div>
        <div class="v ${atrPct == null ? "weak" : "neutral"}">${atrPct == null ? "—" : atrPct.toFixed(3) + "%"}</div>
      </div>
      <div class="fx-metric">
        <div class="l">Order-flow imbalance</div>
        <div class="v ${tone(ofi, -.05, .05)}">${toFixedSafe(ofi, 2)}</div>
      </div>
      <div class="fx-metric">
        <div class="l">Crowd-energy idx</div>
        <div class="v ${tone(cei, 30, 70)}">${toFixedSafe(cei, 0)}</div>
      </div>
      <div class="fx-metric">
        <div class="l">BB %B</div>
        <div class="v ${tone(bbp, .15, .85)}">${bbp == null ? "—" : (bbp * 100).toFixed(0) + "%"}</div>
      </div>
      <div class="fx-metric">
        <div class="l">Radar score</div>
        <div class="v ${radarScore > 10 ? "pos" : radarScore < -10 ? "neg" : "neutral"}">${radarScore >= 0 ? "+" : ""}${toFixedSafe(radarScore, 0)}</div>
      </div>
    `;

    // forecasts: stakan + daily
    const stakanSig = (state.stakanSignals.find(s => s.pair === f.pair)) || null;
    const dailySig  = (state.dailySignals.find(s => s.pair === f.pair)) || null;
    const stakanOpen = state.stakanOpen.find(t => t.pair === f.pair);
    const dailyOpen  = state.openTrades.find(t => t.pair === f.pair);

    function forecastBlock(name, sig, openT, fallback) {
      let sideB = sig?.side || openT?.side || fallback || "NONE";
      let prob  = sig?.confidence ?? sig?.score ?? null;
      let horz  = sig?.expiry_hours ? `${sig.expiry_hours}h` : (openT ? "open" : "—");
      return `
        <div class="fx-forecast">
          <div class="name">${name}</div>
          <div class="row">
            <span class="side ${sideB}">${sideB}</span>
            <span class="prob">${prob == null ? "" : (Number(prob).toFixed(prob > 1 ? 0 : 2))}</span>
          </div>
          <div class="horizon">${horz}</div>
        </div>
      `;
    }
    root.querySelector("[data-forecasts]").innerHTML =
      forecastBlock("Stakan", stakanSig, stakanOpen, side) +
      forecastBlock("Daily", dailySig, dailyOpen, side);

    // targets
    const t = priceTargets(f);
    const tg = root.querySelector("[data-targets]");
    if (t) {
      const dir = f.side === "SELL" ? "↓" : "↑";
      tg.innerHTML = `
        <div><span class="arrow">${dir}</span> цель ≈ <b>${fmtPrice(t.target)}</b> · ATR-проекция (~${(Math.abs(t.target - t.px)/t.atr).toFixed(1)}× ATR)</div>
        <div>разворот при <b>${fmtPrice(t.reverse)}</b> · окно <b>${t.hours}h</b> до пересчёта прогноза</div>
      `;
    } else {
      tg.innerHTML = `<div class="muted">недостаточно данных для проекции</div>`;
    }

    // tags
    const tags = root.querySelector("[data-tags]");
    const tagBits = [];
    if (rsi != null && rsi >= 70) tagBits.push(`<span class="fx-tag warn">RSI ${rsi.toFixed(0)} overbought</span>`);
    if (rsi != null && rsi <= 30) tagBits.push(`<span class="fx-tag warn">RSI ${rsi.toFixed(0)} oversold</span>`);
    if (bbp != null && bbp >= .9) tagBits.push(`<span class="fx-tag warn">BB upper</span>`);
    if (bbp != null && bbp <= .1) tagBits.push(`<span class="fx-tag warn">BB lower</span>`);
    if (cot1) tagBits.push(`<span class="fx-tag ${cot1.net_pct_oi >= 0 ? "buy" : "sell"}">${pairBaseQuote(f.pair)[0]} COT ${cot1.net_pct_oi >= 0 ? "+" : ""}${(cot1.net_pct_oi||0).toFixed(0)}%</span>`);
    if (cot2) tagBits.push(`<span class="fx-tag ${cot2.net_pct_oi >= 0 ? "buy" : "sell"}">${pairBaseQuote(f.pair)[1]} COT ${cot2.net_pct_oi >= 0 ? "+" : ""}${(cot2.net_pct_oi||0).toFixed(0)}%</span>`);
    if (stakanOpen) tagBits.push(`<span class="fx-tag accent">Stakan open</span>`);
    if (dailyOpen) tagBits.push(`<span class="fx-tag accent">Daily open</span>`);
    tags.innerHTML = tagBits.join("");

    // chart (light)
    loadChart(f.pair);

    // flash effect on update
    root.classList.remove("flash");
    void root.offsetWidth;
    root.classList.add("flash");
  }

  function renderAll() {
    const want = pairsSorted().filter(passesFilter);
    const wantPairs = new Set(want.map(f => f.pair));
    // Remove cards no longer wanted
    for (const [pair, c] of state.cards.entries()) {
      if (!wantPairs.has(pair)) {
        if (c.chart) c.chart.remove();
        c.root.remove();
        state.cards.delete(pair);
      }
    }
    // Empty state
    grid.querySelector(".fx-empty")?.remove();
    if (!want.length) {
      const e = document.createElement("div");
      e.className = "fx-empty";
      e.textContent = "ничего не подходит под фильтр";
      grid.appendChild(e);
      return;
    }
    // Re-order DOM to match sort
    const frag = document.createDocumentFragment();
    for (const f of want) {
      renderCard(f);
      frag.appendChild(state.cards.get(f.pair).root);
    }
    grid.appendChild(frag); // moves existing nodes
    renderSummary();
    renderStrength();
  }

  // ─── deep dive (modal) ───────────────────────────────
  async function openDeep(pair) {
    deepBody.innerHTML = `<h3>${pair}</h3><div class="muted">загружаю микроструктуру…</div>`;
    if (typeof deepDlg.showModal === "function") deepDlg.showModal();
    try {
      const [ms, fc] = await Promise.all([
        fetchJson(`/api/microstructure/${pair}`),
        fetchJson(`/api/forecast/${pair}`).catch(() => null),
      ]);
      const ux = window.FX_UX || { ru: x => x, ruPhrase: x => x, ruSummaryLine: x => x };
      const inner = (ms.summary?.inner_facts || []).map(s => `<li>${escapeHtml(ux.ruSummaryLine(s))}</li>`).join("");
      const outer = (ms.summary?.outer_view || []).map(s => `<li>${escapeHtml(ux.ruSummaryLine(s))}</li>`).join("");
      const cd = ms.cumulative_delta || {};
      const wy = ms.wyckoff || {};
      const hu = ms.hurst || {};
      const obs = (ms.order_blocks || []).slice(0, 3);
      const fvgs = (ms.fair_value_gaps || []).slice(0, 3);
      const sweeps = (ms.liquidity_sweeps || []).slice(0, 3);
      deepBody.innerHTML = `
        <h3>${pair} · Рыночный контекст (Market Intent)</h3>
        <div class="deep-section">
          <h4>Что происходит внутри рынка (ордер-флоу)</h4>
          <ul>${inner || "<li class='muted'>нет inner-фактов</li>"}</ul>
        </div>
        <div class="deep-section">
          <h4>Что показывает снаружи (режим и стадия)</h4>
          <ul>${outer || "<li class='muted'>нет outer-view</li>"}</ul>
        </div>
        <div class="deep-section">
          <h4 data-explain="Cumulative Delta">Кумулятивная дельта</h4>
          <div>Перевес: <b>${ux.ru(cd.bias) || "—"}</b> · сила: <b>${toFixedSafe(cd.norm_pct, 0)}%</b> · расхождение с ценой: <b>${cd.divergence ? "да" : "нет"}</b></div>
        </div>
        <div class="deep-section">
          <h4 data-explain="Wyckoff">Стадия Wyckoff</h4>
          <div>стадия: <b>${ux.ru(wy.stage) || "—"}</b> · уверенность: <b>${toFixedSafe(wy.confidence, 0)}%</b> · позиция в диапазоне: <b>${toFixedSafe(wy.position_in_range_pct, 0)}%</b></div>
        </div>
        <div class="deep-section">
          <h4 data-explain="Hurst">Экспонента Хёрста (режим рынка)</h4>
          <div>H = <b>${toFixedSafe(hu.H, 3)}</b> · режим: <b>${ux.ru(hu.regime) || "—"}</b></div>
        </div>
        <div class="deep-section">
          <h4 data-explain="Order Block">Ордер-блоки (последние ${obs.length})</h4>
          <ul>${obs.map(o => `<li>${ux.ru(o.kind)} @ ${fmtPrice(o.low)}–${fmtPrice(o.high)}</li>`).join("") || "<li class='muted'>нет</li>"}</ul>
        </div>
        <div class="deep-section">
          <h4 data-explain="FVG">Разрывы справедливой стоимости (FVG)</h4>
          <ul>${fvgs.map(g => `<li>${ux.ru(g.kind)} @ ${fmtPrice(g.lo || g.low)}–${fmtPrice(g.hi || g.high)}</li>`).join("") || "<li class='muted'>нет</li>"}</ul>
        </div>
        <div class="deep-section">
          <h4 data-explain="Liquidity Sweep">Снятие ликвидности</h4>
          <ul>${sweeps.map(s => `<li>${ux.ru(s.kind)} → ${ux.ru(s.implication || s.expectation || "")}</li>`).join("") || "<li class='muted'>нет</li>"}</ul>
        </div>
        ${fc ? `
        <div class="deep-section">
          <h4>Forecast snapshot</h4>
          <div>side: <b>${fc.side}</b> · prob: <b>${toFixedSafe(fc.probability_pct, 1)}%</b> · окно ${fc.recommended_hours}h</div>
        </div>` : ""}
      `;
    } catch (e) {
      deepBody.innerHTML = `<h3>${pair}</h3><div class="muted">не удалось загрузить: ${escapeHtml(String(e))}</div>`;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  }

  // ─── filters ──────────────────────────────────────────
  filterChips.forEach(chip => chip.addEventListener("click", () => {
    filterChips.forEach(c => c.classList.toggle("active", c === chip));
    state.activeFilter = chip.dataset.filter;
    renderAll();
  }));
  searchInput.addEventListener("input", () => {
    state.search = searchInput.value.trim();
    renderAll();
  });

  // ─── boot ─────────────────────────────────────────────
  buildEl.textContent = "build " + new Date().toISOString().slice(0,10);
  refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(() => {
    renderClock();
    updateNextTick();
  }, 1000);

  // ─── ФИНАЛЬНЫЙ ПРОГНОЗ ДЛЯ МЕНЯ — единый сигнал для реальной сделки ──
  async function refreshFinalSignal() {
    const body = document.getElementById("final-signal-body");
    const badge = document.getElementById("fs-verdict-badge");
    if (!body) return;
    try {
      const r = await fetch("/api/final-signal", { cache: "no-cache" }).then(x => x.ok ? x.json() : Promise.reject(x.status));
      if (!r || r.error) {
        body.innerHTML = `<div class="muted">Не удалось получить сигнал: ${r ? r.error : "?"}</div>`;
        return;
      }
      const verdictClass =
        r.verdict === "GO" ? "fs-go" :
        r.verdict === "GO_CAUTION" ? "fs-cau" :
        "fs-wait";
      const checksHtml = (r.checks || []).map(c => {
        const dot =
          c.status === "green" ? "🟢" :
          c.status === "red"   ? "🔴" :
                                 "🟡";
        return `<li class="fs-check fs-${c.status}">
          <span class="fs-dot">${dot}</span>
          <span class="fs-name">${c.name_ru}</span>
          <span class="fs-detail muted small">${c.detail_ru || ""}</span>
        </li>`;
      }).join("");
      const sumPair = r.pair
        ? `<div class="fs-pair">${r.pair} <span class="fs-side fs-side-${(r.side || "").toLowerCase()}">${r.side_ru || r.side || ""}</span></div>`
        : "";
      const sumProb = `<div class="fs-prob">${(r.probability_pct || 0).toFixed(0)}%</div>`;
      const sumExp = r.expiry_hours ? `<div class="fs-expiry">экспайри ${r.expiry_hours}ч</div>` : "";
      const altHtml = (r.alternates || []).slice(0, 3).map(a =>
        `<span class="fs-alt-pill">${a.pair} ${a.side} ${(a.probability_pct || 0).toFixed(0)}%</span>`
      ).join(" ");

      body.innerHTML = `
        <div class="fs-row ${verdictClass}">
          <div class="fs-pick">
            ${sumPair}${sumProb}${sumExp}
          </div>
          <div class="fs-verdict">${r.verdict_ru || "—"}</div>
        </div>
        <div class="fs-reasoning small muted">${r.reasoning_ru || ""}</div>
        <div class="fs-checks-title small muted">8 проверок:</div>
        <ul class="fs-checks">${checksHtml}</ul>
        ${altHtml ? `<div class="fs-alts small"><b>Запасные кандидаты:</b> ${altHtml}</div>` : ""}
        <div class="fs-meta small muted">
          Сессия сейчас: <b>${r.session_now_ru || "?"}</b> ·
          источник: <code>/api/final-signal</code> ·
          обновлено только что
        </div>`;

      if (badge) {
        const c = r.summary_counts || {};
        badge.textContent = `${r.verdict || "?"} · 🟢${c.green||0} 🟡${c.yellow||0} 🔴${c.red||0}`;
        badge.className = "badge-stable fs-badge-" + (r.verdict === "GO" ? "go" : r.verdict === "GO_CAUTION" ? "cau" : "wait");
      }
    } catch (e) {
      console.error("final-signal:", e);
      body.innerHTML = `<div class="muted">Ошибка: ${e}</div>`;
    }
  }
  refreshFinalSignal();
  setInterval(refreshFinalSignal, 30 * 1000);

  // ─── МУЛЬТИ-СИГНАЛЫ: 28 финальных прогнозов (индивидуальный подход) ──
  let _lastVerdicts = {};   // pair -> verdict — для WAIT->GO dingа
  let _lastProbs = {};      // pair -> probability — для flash-эффекта
  function _applyMood(summary) {
    const body = document.body;
    body.classList.remove("fx-mood-go", "fx-mood-cau", "fx-mood-wait");
    if (!summary) return;
    if ((summary.go || 0) > 0)              body.classList.add("fx-mood-go");
    else if ((summary.go_caution || 0) > 0) body.classList.add("fx-mood-cau");
    else                                    body.classList.add("fx-mood-wait");
  }
  function ensureStaleBanner() {
    let el = document.getElementById("fs-stale-banner");
    if (el) return el;
    const host = document.getElementById("final-signals-section") || document.body;
    el = document.createElement("div");
    el.id = "fs-stale-banner";
    el.style.cssText =
      "display:none;padding:10px 14px;margin:8px 0;border-radius:10px;" +
      "background:rgba(255,206,94,.12);border:1px solid rgba(255,206,94,.45);" +
      "color:#ffce5e;font-size:13px;line-height:1.5;";
    const grid = document.getElementById("fs-grid");
    if (grid && grid.parentNode) grid.parentNode.insertBefore(el, grid);
    else host.appendChild(el);
    return el;
  }
  async function refreshFinalSignals() {
    const grid = document.getElementById("fs-grid");
    const pill = document.getElementById("fs-summary-pill");
    if (!grid) return;
    try {
      const resp = await fetch("/api/final-signals", { cache: "no-cache" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const source = resp.headers.get("X-FX-Source") || "live";
      const r = await resp.json();
      if (!r || r.error) {
        grid.innerHTML = `<div class="muted">Не удалось получить финальные прогнозы: ${r ? r.error : "?"}</div>`;
        return;
      }
      const sigs = r.signals || [];
      const sum = r.summary || {};

      // Stale-snapshot banner: if the JSON snapshot is older than 10 min OR
      // says "market closed" while the client clock says "market open", the
      // user is looking at frozen data — make that obvious so they don't
      // trade on it.
      const banner = ensureStaleBanner();
      const ageSec = r.as_of_utc
        ? Math.max(0, (Date.now() - new Date(r.as_of_utc).getTime()) / 1000)
        : Infinity;
      const cs = (typeof window.FX_clientMarketStatus === "function")
        ? window.FX_clientMarketStatus() : null;
      const liveOpen = cs ? cs.is_open : null;
      const snapMarketOk = r.global_context && r.global_context.market_ok === true;
      const conflict = liveOpen === true && snapMarketOk === false;
      const tooOld = ageSec > 10 * 60;
      if (conflict || tooOld || source === "baked") {
        banner.style.display = "block";
        const ageMin = Math.round(ageSec / 60);
        const reasons = [];
        if (conflict) reasons.push("по часам устройства рынок ОТКРЫТ, но снапшот говорит «закрыт»");
        if (tooOld) reasons.push(`снапшот старше ${ageMin} мин`);
        if (source === "baked" && reasons.length === 0) reasons.push("живой backend недоступен — показывается кэш с момента последнего деплоя");
        banner.innerHTML =
          `⚠ <b>Внимание: данные снапшота устарели.</b> ` +
          reasons.join("; ") + ". " +
          "Обновляю при следующем тике или после рестарта live backend (Fly.io). " +
          "Рекомендация: открывай сделки только когда статус сверху «ОТКРЫТ» совпадает с экспертом и снапшот свежий.";
      } else {
        banner.style.display = "none";
      }

      if (pill) {
        pill.innerHTML =
          `сессия «<b>${r.session_now_ru || "?"}</b>» · ` +
          `<span class="fs-pill-go">🟢 ${sum.go || 0} GO</span> · ` +
          `<span class="fs-pill-cau">🟡 ${sum.go_caution || 0}</span> · ` +
          `<span class="fs-pill-wait">🔴 ${sum.wait || 0}</span> · ` +
          `стратегии готовы: <b>${sum.qualified_cells_for_session || 0}/${sum.total || 28}</b>`;
      }

      // detect WAIT → GO transitions for the celebratory ding
      let wokeUp = 0;
      for (const s of sigs) {
        const prev = _lastVerdicts[s.pair];
        if (prev && prev !== "GO" && s.verdict === "GO") wokeUp++;
        _lastVerdicts[s.pair] = s.verdict;
      }
      if (wokeUp > 0 && window.FX_UX && window.FX_UX.sound) {
        window.FX_UX.sound.goDing();
      }

      // Apply background mood class based on aggregate verdict
      _applyMood(sum);

      // Render cards
      grid.innerHTML = sigs.map(s => renderFinalCard(s)).join("");

      // Flash probability values that changed since last refresh
      const ux = window.FX_UX;
      grid.querySelectorAll(".fs-card").forEach(card => {
        const pair = card.getAttribute("data-pair");
        const probEl = card.querySelector(".fs-card-prob");
        if (!pair || !probEl) return;
        const prev = _lastProbs[pair];
        const curStr = probEl.textContent.trim();
        const cur = parseFloat(curStr) || 0;
        if (prev != null && prev !== cur) {
          probEl.classList.remove("fx-flash-up", "fx-flash-dn");
          void probEl.offsetWidth;
          probEl.classList.add(cur > prev ? "fx-flash-up" : "fx-flash-dn");
          setTimeout(() => probEl.classList.remove("fx-flash-up", "fx-flash-dn"), 900);
        }
        _lastProbs[pair] = cur;
        // wire expand-on-click for this card
        card.addEventListener("click", e => {
          if (e.target.closest("[data-explain]")) return;
          card.classList.toggle("is-expanded");
        });
      });
    } catch (e) {
      console.error("final-signals:", e);
      grid.innerHTML = `<div class="muted">Ошибка: ${e}</div>`;
    }
  }
  function renderFinalCard(s) {
    const v = s.verdict || "WAIT";
    const cls = v === "GO" ? "fs-card-go" : v === "GO_CAUTION" ? "fs-card-cau" : "fs-card-wait";
    const probCls = v === "GO" ? "go" : v === "GO_CAUTION" ? "cau" : "wait";
    const sideCls = (s.side || "").toLowerCase() === "buy" ? "fs-card-side-buy" : "fs-card-side-sell";
    const sideRu = (s.side || "").toLowerCase() === "buy" ? "BUY (покупка)" : "SELL (продажа)";
    const c = s.summary_counts || {};
    const blocker = (v === "WAIT" && s.short_blocker)
      ? `<div class="fs-card-blocker">⛔ блокирует: ${escapeHtml(s.short_blocker)}</div>` : "";
    const checksHtml = (s.checks || []).map(ch => {
      const dot = ch.status === "green" ? "🟢" : ch.status === "red" ? "🔴" : "🟡";
      return `<li class="fs-${ch.status}">
        <span>${dot}</span>
        <span class="fs-cli-name">${escapeHtml(ch.name_ru)}</span>
        <span class="fs-cli-detail">${escapeHtml(ch.detail_ru || "")}</span>
      </li>`;
    }).join("");
    return `<div class="fs-card ${cls}" data-pair="${s.pair}">
      <div class="fs-card-row1">
        <span class="fs-card-pair">${s.pair}</span>
        <span class="fs-card-side ${sideCls}">${sideRu}</span>
      </div>
      <div class="fs-card-row2">
        <span class="fs-card-prob ${probCls}">${(s.probability_pct||0).toFixed(0)}%</span>
        <span class="fs-card-expiry">экспайри ${s.expiry_hours || "?"}ч</span>
      </div>
      <div class="fs-card-verdict">${escapeHtml(s.verdict_ru || "")}</div>
      ${blocker}
      <div class="fs-card-checks">Проверки: 🟢${c.green||0} 🟡${c.yellow||0} 🔴${c.red||0}</div>
      <div class="fs-card-tap">→ нажми чтобы развернуть все 8 проверок</div>
      <div class="fs-card-expanded">
        <ul>${checksHtml}</ul>
      </div>
    </div>`;
  }
  refreshFinalSignals();
  setInterval(refreshFinalSignals, 15 * 1000);  // every 15s, was 30s

  // ─── Live market-status badge — обновляется каждую секунду ─────────────
  // Так пользователь видит «ОТКРЫТ / ЗАКРЫТ / откроется через X» в реальном
  // времени без перезагрузки страницы. На статическом миррере /api/market-status
  // полностью синтезируется static-shim.js на стороне клиента (DST-aware
  // NY-логика), так что время отсчёта всегда совпадает с реальным —
  // даже если сам бандл был задеплоен сутки назад.
  function fmtCountdown(secs) {
    if (!isFinite(secs) || secs <= 0) return "—";
    const d = Math.floor(secs / 86400);
    const h = Math.floor((secs % 86400) / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    if (d > 0) return `${d}д ${h}ч ${m}м`;
    if (h > 0) return `${h}ч ${m}м ${String(s).padStart(2, "0")}с`;
    if (m > 0) return `${m}м ${String(s).padStart(2, "0")}с`;
    return `${s}с`;
  }
  function ensureMarketBadgeEl() {
    let badge = document.getElementById("fx-market-badge");
    if (badge) return badge;
    // Prefer the always-visible top toolbar (fx-toolbar) so the badge is
    // never hidden by collapsing sections. Fall back to fs-multi-header
    // and finally body.
    const host =
      document.querySelector(".fx-toolbar") ||
      document.querySelector(".fs-multi-header") ||
      document.body;
    badge = document.createElement("div");
    badge.id = "fx-market-badge";
    badge.className = "fx-market-badge";
    badge.style.cssText =
      "display:inline-flex;align-items:center;gap:8px;padding:8px 14px;" +
      "border-radius:999px;background:rgba(74,250,163,.12);" +
      "border:1px solid rgba(74,250,163,.45);font-size:14px;line-height:1;" +
      "white-space:nowrap;color:#e9f7ee;font-weight:600;" +
      "box-shadow:0 0 0 2px rgba(74,250,163,.06) inset;";
    // For fx-toolbar (flex row) we want the badge on the left of the clock,
    // so prepend rather than append.
    if (host.classList && host.classList.contains("fx-toolbar")) {
      host.insertBefore(badge, host.firstChild);
    } else {
      host.appendChild(badge);
    }
    return badge;
  }
  async function refreshLiveMarketBadge() {
    const pill = document.getElementById("fs-summary-pill");
    const badge = ensureMarketBadgeEl();
    let ms;
    let source = "unknown";
    try {
      const r = await fetch("/api/market-status", { cache: "no-store" });
      source = r.headers && r.headers.get("X-FX-Source") || "live";
      ms = await r.json();
      // If the response somehow snuck through stale (older than 60s) and we
      // have a client-side computer available, override with fresh values.
      const ageSec =
        ms && ms.as_of_utc
          ? Math.abs((Date.now() - new Date(ms.as_of_utc).getTime()) / 1000)
          : Infinity;
      const stale = ageSec > 60;
      if (stale && typeof window.FX_clientMarketStatus === "function") {
        ms = window.FX_clientMarketStatus();
        source = "client_side_shim";
      }
    } catch (e) {
      // Network failure — synthesize purely from the user's clock.
      if (typeof window.FX_clientMarketStatus === "function") {
        ms = window.FX_clientMarketStatus();
        source = "client_side_shim";
      } else {
        return;
      }
    }
    const isOpen = !!ms.is_open;
    const cur = pill ? pill.dataset.lastIs || "" : "";
    const now = isOpen ? "open" : "closed";
    if (cur === "closed" && now === "open" && window.FX_UX && window.FX_UX.sound) {
      try { window.FX_UX.sound.goDing(); } catch (e) {}
      refreshFinalSignals();
    }
    if (pill) pill.dataset.lastIs = now;

    // Render the live badge.
    const dotColor = isOpen ? "#4afaa3" : "#ff8090";
    // Dynamically switch the pill background to match status.
    if (isOpen) {
      badge.style.background = "rgba(74,250,163,.14)";
      badge.style.borderColor = "rgba(74,250,163,.55)";
      badge.style.color = "#e9f7ee";
    } else {
      badge.style.background = "rgba(255,128,144,.14)";
      badge.style.borderColor = "rgba(255,128,144,.55)";
      badge.style.color = "#ffe9ec";
    }
    const eventLabel = ms.next_event_text_ru ||
      (isOpen ? "закроется через" : "откроется через");
    const eventSecs = isOpen
      ? (ms.seconds_until_close || 0)
      : (ms.seconds_until_open || 0);
    const sourceTag =
      source === "live"
        ? '<span style="opacity:.6;font-size:11px">live</span>'
        : source === "client_side_shim"
        ? '<span style="opacity:.6;font-size:11px">часы устр-ва</span>'
        : '<span style="opacity:.6;font-size:11px">кэш</span>';
    badge.innerHTML =
      `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;` +
      `background:${dotColor};box-shadow:0 0 10px ${dotColor}"></span>` +
      `<b>${isOpen ? "🟢 РЫНОК ОТКРЫТ" : "🔴 РЫНОК ЗАКРЫТ"}</b>` +
      `<span style="opacity:.7">·</span>` +
      `<span>${eventLabel} <b>${fmtCountdown(eventSecs)}</b></span>` +
      `<span style="opacity:.7">·</span>` +
      sourceTag;
  }
  refreshLiveMarketBadge();
  setInterval(refreshLiveMarketBadge, 1000);

  // ─── AI-АНАЛИТИК: развёрнутый комментарий через Pollinations.ai (free) ──
  async function refreshAINarrative() {
    const el = document.getElementById("ai-narrative-text");
    const src = document.getElementById("ai-narrative-source");
    if (!el) return;
    try {
      const r = await fetch("/api/ai-narrative", {cache: "no-store"});
      const j = await r.json();
      if (src) {
        if (j.source === "pollinations") src.textContent = "🤖 Pollinations.ai · LLM";
        else                              src.textContent = "📋 детерминированный fallback";
      }
      if (j.narrative_ru) {
        el.textContent = j.narrative_ru;
        el.classList.remove("muted");
      } else {
        el.innerHTML = `<span class="muted">Источник недоступен: ${j.error || "—"}</span>`;
      }
    } catch (e) {
      el.innerHTML = `<span class="muted">AI-аналитик: ${e}</span>`;
    }
  }
  refreshAINarrative();
  setInterval(refreshAINarrative, 5 * 60 * 1000);  // matches server cache

  // ─── ЖИВОЙ AI-АНАЛИТИК — мысли по 28 парам в реальном времени ───
  function statusClass(status) {
    return ({
      STORM_PROOF: "la-status-storm",
      QUALIFIED:   "la-status-qualif",
      PROBABLE:    "la-status-prob",
      FROZEN:      "la-status-frozen",
      INSUFFICIENT:"la-status-insuff",
    })[status] || "la-status-insuff";
  }

  function renderLiveAnalystCard(item) {
    const fc = item.forecast || {};
    const cell = item.playbook_cell;
    const lr = item.live_regime || {};
    const sess = item.session || "off";
    const probTxt = (fc.probability_pct != null) ? `${fc.probability_pct}%` : "—";
    const sideTxt = fc.side || "—";
    const cellLine = cell
      ? `<span class="${statusClass(cell.status)}">${cell.status}</span> WR ${cell.wr_pct ?? "—"}% (n=${cell.n_trades ?? 0})`
      : `<span class="la-status-insuff">playbook нет</span>`;
    return `
      <div class="la-card">
        <div class="la-head">
          <span class="la-pair">${item.pair}</span>
          <span class="la-verdict">${item.verdict_emoji || ""}</span>
        </div>
        <div class="la-meta">
          <span>📍 ${sess}</span>
          <span>📊 ${lr.label_ru || lr.regime || "—"}</span>
          <span>H=${lr.hurst != null ? lr.hurst.toFixed(2) : "—"}</span>
        </div>
        <div class="la-meta">
          <span>${sideTxt} ${probTxt}</span>
          <span>${cellLine}</span>
        </div>
        <div class="la-narrative">${(item.narrative_ru || "").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"})[c])}</div>
      </div>
    `;
  }

  async function refreshLiveAnalyst() {
    const grid = document.getElementById("live-analyst-grid");
    const summary = document.getElementById("live-analyst-summary");
    if (!grid) return;
    try {
      const r = await fetch("/api/analyst", {cache: "no-store"});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      const items = j.items || [];
      // Render with regime + session sort: storm-proof first, qualified, probable, others
      const order = {STORM_PROOF: 0, QUALIFIED: 1, PROBABLE: 2, FROZEN: 3, INSUFFICIENT: 4};
      items.sort((a, b) => {
        const sa = (a.playbook_cell || {}).status || "INSUFFICIENT";
        const sb = (b.playbook_cell || {}).status || "INSUFFICIENT";
        return (order[sa] ?? 5) - (order[sb] ?? 5);
      });
      grid.innerHTML = items.map(renderLiveAnalystCard).join("");
      const counts = items.reduce((acc, it) => {
        const s = (it.playbook_cell || {}).status || "INSUFFICIENT";
        acc[s] = (acc[s] || 0) + 1;
        return acc;
      }, {});
      const total = items.length;
      const open = items.filter(it => ["STORM_PROOF","QUALIFIED"].includes((it.playbook_cell || {}).status)).length;
      if (summary) summary.textContent = `${open}/${total} зелёных, ${counts.PROBABLE || 0} probable, ${counts.FROZEN || 0} frozen`;
    } catch (e) {
      grid.innerHTML = `<div class="muted">Не могу получить /api/analyst: ${e}</div>`;
    }
  }
  refreshLiveAnalyst();
  setInterval(refreshLiveAnalyst, 30 * 1000);

  // ─── ДНЕВНОЙ ТАРГЕТ 5 СДЕЛОК НА ПАРУ ───
  async function refreshDailyTarget() {
    const grid = document.getElementById("daily-target-grid");
    const summary = document.getElementById("daily-target-summary");
    if (!grid) return;
    try {
      const r = await fetch("/api/daily-target", {cache: "no-store"});
      const j = await r.json();
      const items = j.items || [];
      items.sort((a, b) => b.count - a.count);
      grid.innerHTML = items.map(it => `
        <div class="dt-card">
          <div class="dt-pair"><span>${it.pair}</span><span class="dt-count">${it.count}/${it.target}</span></div>
          <div class="dt-bar"><div class="dt-fill ${it.on_target ? 'dt-on-target' : ''}" style="width:${it.pct.toFixed(0)}%"></div></div>
          <div class="muted" style="font-size:11px">${it.on_target ? "✓ цель достигнута" : `жду еще ${it.missing}`}</div>
        </div>
      `).join("");
      if (summary) summary.textContent = `${j.on_target_count}/${j.total_pairs} пар на 5+ за ${j.date_utc}`;
    } catch (e) {
      grid.innerHTML = `<div class="muted">Не могу получить /api/daily-target: ${e}</div>`;
    }
  }
  refreshDailyTarget();
  setInterval(refreshDailyTarget, 30 * 1000);

  // ═══ Главная страница: единая секция «Сделки» (open + closed + WR-by-pair) ═══
  // Раньше пользователь должен был ходить на отдельный /trades. По требованию пользователя
  // (одно место для всего на главной) — копия логики trades.js, упрощённая для main-screen.
  const _mtFmt = {
    num(x, d=0){ if(x==null||!isFinite(x)) return "—"; return Number(x).toFixed(d); },
    price(x){ if(x==null||!isFinite(x)) return "—"; const n=Number(x); return n>=100?n.toFixed(3):n.toFixed(5); },
    pips(x){ if(x==null||!isFinite(x)) return "—"; const s=x>=0?"+":""; return `${s}${Number(x).toFixed(1)}`; },
    pnl(x){ if(x==null||!isFinite(x)) return "—"; const s=x>=0?"+$":"−$"; return `${s}${Math.abs(Number(x)).toFixed(2)}`; },
    pct(x,d=1){ if(x==null||!isFinite(x)) return "—"; return `${Number(x).toFixed(d)}%`; },
    utc(iso){ if(!iso) return "—"; try{const d=new Date(iso); return d.toLocaleString(undefined,{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});}catch(_){return iso;} },
    countdown(secs){ if(secs==null||!isFinite(secs)||secs<=0) return "истекла"; const h=Math.floor(secs/3600), m=Math.floor((secs%3600)/60), s=Math.floor(secs%60); if(h>0) return `${h}ч ${String(m).padStart(2,"0")}м`; if(m>0) return `${m}м ${String(s).padStart(2,"0")}с`; return `${s}с`; },
  };
  async function _mtFetch(url, fallback=null){
    try { const r = await fetch(url, {cache:"no-store"}); if(!r.ok) throw new Error(`${url}→${r.status}`); return await r.json(); }
    catch(e){ console.warn("mt-fetch failed", url, e); return fallback; }
  }
  function _mtLivePips(t){
    const f = state.forecasts[t.pair];
    const cur = f && (f.current_price ?? f.latest_price);
    if (!isFinite(cur) || !isFinite(t.open_price)) return {cur:null, pips:null, pnl:null, inProfit:null};
    const pipMul = t.pair.endsWith("JPY") ? 100 : 10000;
    const diff = (cur - t.open_price) * pipMul;
    const pips = t.side === "SELL" ? -diff : diff;
    const inProfit = pips > 0;
    const stake = Number(t.stake_usd ?? 1);
    const payout = Number(t.payout_pct ?? 0.85);
    const projPnl = inProfit ? stake * payout : -stake;
    return {cur, pips, pnl: projPnl, inProfit};
  }
  function _mtRenderOpen(open){
    const tbody = document.getElementById("mt-open-tbody");
    if (!tbody) return;
    if (!open || open.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="mt-empty">Сейчас открытых сделок нет. Как только probability ≥ 70% — paper-trader откроет сам.</td></tr>`;
      return;
    }
    const now = Date.now();
    const rows = open.slice(0, 12).map(t => {
      const expiryMs = new Date(t.expiry_time).getTime();
      const live = _mtLivePips(t);
      const liveCls = live.inProfit===true?"mt-win":live.inProfit===false?"mt-loss":"mt-muted";
      return `<tr>
        <td><b>${t.pair}</b></td>
        <td class="${t.side==="BUY"?"mt-buy":"mt-sell"}">${t.side}</td>
        <td>${_mtFmt.price(t.open_price)}</td>
        <td>${_mtFmt.utc(t.open_time)}</td>
        <td class="mt-muted">${_mtFmt.countdown((expiryMs-now)/1000)}</td>
        <td>${_mtFmt.price(live.cur)}</td>
        <td class="${liveCls}">${_mtFmt.pips(live.pips)}</td>
        <td class="${liveCls}">${_mtFmt.pnl(live.pnl)}</td>
      </tr>`;
    }).join("");
    tbody.innerHTML = rows;
  }
  function _mtRenderClosed(closed){
    const tbody = document.getElementById("mt-closed-tbody");
    if (!tbody) return;
    if (!closed || closed.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="mt-empty">История пока пустая.</td></tr>`;
      return;
    }
    const sorted = [...closed].sort((a,b) => new Date(b.close_time||b.expiry_time) - new Date(a.close_time||a.expiry_time)).slice(0, 10);
    tbody.innerHTML = sorted.map(t => {
      const variant = t.strategy_variant_at_open || "—";
      const wrAtOpen = t.strategy_wr_pct_at_open;
      const stratLabel = wrAtOpen ? `${variant} <span class="mt-muted">(${_mtFmt.pct(wrAtOpen)})</span>` : variant;
      const result = t.result || t.status;
      const resultCls = result === "WIN" ? "mt-win" : "mt-loss";
      return `<tr>
        <td><b>${t.pair}</b></td>
        <td class="${t.side==="BUY"?"mt-buy":"mt-sell"}">${t.side}</td>
        <td>${_mtFmt.utc(t.open_time)}</td>
        <td>${_mtFmt.utc(t.close_time||t.expiry_time)}</td>
        <td>${_mtFmt.pct(t.probability_pct_at_open)}</td>
        <td>${stratLabel}</td>
        <td class="${resultCls}"><b>${result||"—"}</b></td>
        <td class="${resultCls}">${_mtFmt.pnl(t.pnl_usd)}</td>
      </tr>`;
    }).join("");
  }
  function _mtRenderByPair(closed){
    const grid = document.getElementById("mt-bypair-grid");
    const meta = document.getElementById("mt-bypair-meta");
    if (!grid) return;
    const byPair = {};
    (closed||[]).forEach(t => {
      const p = t.pair;
      if (!byPair[p]) byPair[p] = {wins:0, losses:0, pnl:0};
      const r = (t.result||t.status);
      if (r === "WIN") byPair[p].wins++;
      else if (r === "LOSS") byPair[p].losses++;
      byPair[p].pnl += Number(t.pnl_usd||0);
    });
    const entries = Object.entries(byPair).map(([pair, v]) => {
      const total = v.wins + v.losses;
      const wr = total > 0 ? (v.wins / total) * 100 : 0;
      return {pair, total, wr, pnl: v.pnl, ...v};
    }).sort((a,b) => b.wr - a.wr);
    if (entries.length === 0) {
      grid.innerHTML = `<div class="mt-empty">Пока нет закрытых сделок.</div>`;
      if (meta) meta.textContent = "0 пар";
      return;
    }
    grid.innerHTML = entries.map(e => {
      const cls = e.wr >= 70 ? "mt-pair-win" : e.wr < 50 ? "mt-pair-loss" : "";
      return `<div class="mt-pair-cell ${cls}">
        <span class="mt-pair-name">${e.pair}</span>
        <span class="mt-pair-wr">${e.wr.toFixed(0)}%</span>
        <span class="mt-muted" style="font-size:10px">${e.wins}W ${e.losses}L</span>
      </div>`;
    }).join("");
    if (meta) meta.textContent = `${entries.length} пар в истории`;
  }
  function _mtRenderStats(stats, openCount){
    const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    setVal("mt-total", stats?.total ?? 0);
    setVal("mt-open", openCount ?? 0);
    setVal("mt-wins", stats?.wins ?? 0);
    setVal("mt-losses", stats?.losses ?? 0);
    const wr = Number(stats?.win_rate_pct ?? 0);
    const wrEl = document.getElementById("mt-wr");
    if (wrEl) {
      wrEl.textContent = `${wr.toFixed(1)}%`;
      wrEl.className = `mt-val ${wr >= 70 ? "mt-green" : wr < 50 ? "mt-red" : "mt-amber"}`;
    }
    const pnl = Number(stats?.total_pnl_usd ?? 0);
    const pnlEl = document.getElementById("mt-pnl");
    if (pnlEl) {
      pnlEl.textContent = `${pnl>=0?"+$":"−$"}${Math.abs(pnl).toFixed(2)}`;
      pnlEl.className = `mt-val ${pnl >= 0 ? "mt-green" : "mt-red"}`;
    }
    const pill = document.getElementById("mt-summary-pill");
    if (pill) pill.textContent = `${stats?.total ?? 0} сделок · WR ${wr.toFixed(1)}% · ${openCount ?? 0} открыто`;
  }
  async function refreshMainTrades(){
    const [stats, openR, closedR] = await Promise.all([
      _mtFetch("/api/stats", {}),
      _mtFetch("/api/open-trades", {trades:[]}),
      _mtFetch("/api/closed-trades?limit=200", []),
    ]);
    const open = (openR && openR.trades) || [];
    const closed = Array.isArray(closedR) ? closedR : (closedR?.trades || []);
    _mtRenderStats(stats || {}, open.length);
    _mtRenderOpen(open);
    _mtRenderClosed(closed);
    _mtRenderByPair(closed);
    const oc = document.getElementById("mt-open-count");
    if (oc) oc.textContent = String(open.length);
    const cc = document.getElementById("mt-closed-count");
    if (cc) cc.textContent = String(Math.min(closed.length, 10));
  }
  refreshMainTrades();
  setInterval(refreshMainTrades, 30 * 1000);

  // ═══════════════════════════════════════════════════════════════════════
  // СТАКАН · Order Book section (2026-05-04)
  // ═══════════════════════════════════════════════════════════════════════
  // Новый главный раздел: 28 валют в селекторе, для каждой — стакан, крупные
  // игроки, баланс buyers/sellers, прогноз на 24 часа, основной прогноз
  // 1–5 часов с целью + no-return уровнем. Live-обновление каждые 10 сек.
  // Источник — /api/stakan-view/{pair} и /api/stakan-view (всё компактно).
  const SK_DEFAULT_PAIR = "EURUSD";
  const SK_REFRESH_MS = 10_000;
  const SK_PAIRS_28 = [
    "EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD",
    "EURGBP","EURJPY","EURCHF","EURAUD","EURCAD","EURNZD",
    "GBPJPY","GBPCHF","GBPAUD","GBPCAD","GBPNZD",
    "AUDJPY","CADJPY","CHFJPY","NZDJPY",
    "AUDCAD","AUDCHF","AUDNZD","CADCHF","NZDCAD","NZDCHF",
  ];

  const skState = {
    selectedPair: SK_DEFAULT_PAIR,
    summary: null,    // /api/stakan-view -> all 28
    detail: null,     // /api/stakan-view/{pair}
    timer: null,
  };

  function _skFmtPrice(p) {
    if (p == null || !isFinite(p)) return "—";
    const n = Number(p);
    return n >= 50 ? n.toFixed(3) : n.toFixed(5);
  }
  function _skFmtPct(p, d=1) {
    if (p == null || !isFinite(p)) return "—";
    return Number(p).toFixed(d) + "%";
  }
  function _skFmtPips(p) {
    if (p == null || !isFinite(p)) return "—";
    return Number(p).toFixed(0) + " pips";
  }
  async function _skFetch(url) {
    try {
      const r = await fetch(abs(url), { cache: "no-store", credentials: "include" });
      if (!r.ok) throw new Error(`${url} → ${r.status}`);
      return await r.json();
    } catch (e) {
      console.warn("sk-fetch failed", url, e);
      return null;
    }
  }

  function _skRenderPicker() {
    const grid = document.getElementById("sk-pair-picker");
    if (!grid) return;
    const items = (skState.summary && skState.summary.items) || [];
    const byPair = new Map(items.map(it => [it.pair, it]));
    const html = SK_PAIRS_28.map(p => {
      const it = byPair.get(p) || {};
      const side = it.side || "";
      const prob = it.probability_pct;
      const wr = it.session_wr_pct;
      const isQual = !!it.session_qualifies_70pct;
      const cls = [
        "sk-chip",
        p === skState.selectedPair ? "active" : "",
        side === "BUY" ? "side-buy" : side === "SELL" ? "side-sell" : "",
        isQual ? "qual" : "",
      ].filter(Boolean).join(" ");
      const probTxt = prob != null ? _skFmtPct(prob, 0) : "—";
      const wrTxt = wr != null ? `WR ${_skFmtPct(wr, 0)}` : "WR —";
      return `<button class="${cls}" data-pair="${p}" role="tab" aria-selected="${p === skState.selectedPair}">
        <span class="sk-chip-pair">${p}</span>
        <span class="sk-chip-side">${side || "·"}</span>
        <span class="sk-chip-prob">${probTxt}</span>
        <span class="sk-chip-wr">${wrTxt}</span>
      </button>`;
    }).join("");
    grid.innerHTML = html;
    grid.querySelectorAll(".sk-chip").forEach(btn => {
      btn.addEventListener("click", () => {
        skState.selectedPair = btn.dataset.pair;
        try { localStorage.setItem("sk_pair", skState.selectedPair); } catch (_) {}
        _skRefreshDetail();
        // re-render picker active state immediately
        grid.querySelectorAll(".sk-chip").forEach(b => {
          b.classList.toggle("active", b.dataset.pair === skState.selectedPair);
          b.setAttribute("aria-selected", b.dataset.pair === skState.selectedPair);
        });
      });
    });
    const summary = document.getElementById("sk-summary-pill");
    if (summary && skState.summary) {
      const sess = skState.summary.current_session || "—";
      const qualCount = items.filter(x => x.session_qualifies_70pct).length;
      summary.textContent = `сессия ${sess} · ${qualCount}/${items.length} ≥70% WR`;
    }
  }

  function _skRenderHeader(d) {
    const f = d.forecast || {};
    const strat = d.per_session_strategy || {};
    document.getElementById("sk-pair-name").textContent = d.pair;
    document.getElementById("sk-pair-price").textContent = _skFmtPrice(d.current_price);
    const wr = strat.win_rate_pct;
    const wrTxt = wr != null ? _skFmtPct(wr) : "—";
    document.getElementById("sk-pair-sub").textContent =
      `сессия ${d.current_session || "—"} · стратегия ${strat.best_variant || "—"} · WR ${wrTxt}`;

    const sideEl = document.getElementById("sk-badge-side");
    const probEl = document.getElementById("sk-badge-prob");
    const biasEl = document.getElementById("sk-badge-bias");
    const sessEl = document.getElementById("sk-badge-session");

    sideEl.textContent = f.side ? `сигнал ${f.side}` : "сигнал —";
    sideEl.className = "sk-badge " + (f.side === "BUY" ? "buy" : f.side === "SELL" ? "sell" : "");
    probEl.textContent = `prob ${_skFmtPct(f.probability_pct, 0)}`;
    probEl.className = "sk-badge " + ((f.probability_pct ?? 0) >= 70 ? "ok" : "warn");
    const bias = (d.bias_24h && d.bias_24h.direction) || "—";
    biasEl.textContent = `24h ${bias}`;
    biasEl.className = "sk-badge " + (bias === "UP" ? "buy" : bias === "DOWN" ? "sell" : "");
    sessEl.textContent = strat.qualifies_70pct ? "QUALIFIED 70%+" : "не qualified";
    sessEl.className = "sk-badge " + (strat.qualifies_70pct ? "ok" : "warn");
  }

  function _skRender24h(d) {
    const b = d.bias_24h || {};
    const arrow = document.getElementById("sk-24h-arrow");
    const dir = document.getElementById("sk-24h-direction");
    const conf = document.getElementById("sk-24h-conf");
    const bar = document.getElementById("sk-24h-bar");
    const reasons = document.getElementById("sk-24h-reasons");
    const direction = b.direction || "FLAT";
    const c = Number(b.confidence_pct ?? 50);
    if (direction === "UP") {
      arrow.textContent = "▲";
      arrow.className = "sk-hero-arrow up";
      dir.textContent = "РОСТ";
    } else if (direction === "DOWN") {
      arrow.textContent = "▼";
      arrow.className = "sk-hero-arrow down";
      dir.textContent = "ПАДЕНИЕ";
    } else {
      arrow.textContent = "—";
      arrow.className = "sk-hero-arrow flat";
      dir.textContent = "ФЛЭТ";
    }
    conf.textContent = `уверенность ${c.toFixed(0)}%`;
    bar.style.width = `${Math.max(0, Math.min(100, c))}%`;
    bar.className = "sk-hero-bar-fill " + (direction === "UP" ? "up" : direction === "DOWN" ? "down" : "");
    const why = b.reasoning || [];
    reasons.innerHTML = why.map(w => `<li>${w}</li>`).join("");
  }

  function _skRender5h(d) {
    const m = d.main_forecast_5h || {};
    const sideEl = document.getElementById("sk-5h-side");
    sideEl.textContent = m.side || "—";
    sideEl.className = "sk-hero-side " + (m.side === "BUY" ? "buy" : m.side === "SELL" ? "sell" : "");
    document.getElementById("sk-5h-entry").textContent = _skFmtPrice(m.entry_price);
    document.getElementById("sk-5h-target").textContent =
      m.target_price != null ? `${_skFmtPrice(m.target_price)} (${_skFmtPips(m.target_pips)})` : "—";
    document.getElementById("sk-5h-noreturn").textContent =
      m.no_return_price != null ? `${_skFmtPrice(m.no_return_price)} (${_skFmtPips(m.no_return_pips)})` : "—";
    document.getElementById("sk-5h-hours").textContent = m.hours != null ? `${m.hours} ч` : "—";
    document.getElementById("sk-5h-prob").textContent = _skFmtPct(m.probability_pct, 0);
    document.getElementById("sk-5h-explain").textContent = m.explain_ru || "—";
  }

  function _skRenderBuyersSellers(d) {
    const bs = d.buyers_vs_sellers || {};
    const buy = Number(bs.buyers_pct ?? 50);
    const sell = Number(bs.sellers_pct ?? 50);
    const buyEl = document.getElementById("sk-bs-buy");
    const sellEl = document.getElementById("sk-bs-sell");
    buyEl.style.flex = `${buy} 0 0`;
    sellEl.style.flex = `${sell} 0 0`;
    buyEl.textContent = `${buy.toFixed(0)}% покупают`;
    sellEl.textContent = `${sell.toFixed(0)}% продают`;
    const fav = document.getElementById("sk-bs-fav");
    if (bs.favorite === "buyers") {
      fav.textContent = "🟢 фаворит — покупатели";
      fav.className = "sk-bs-fav fav-buy";
    } else if (bs.favorite === "sellers") {
      fav.textContent = "🔴 фаворит — продавцы";
      fav.className = "sk-bs-fav fav-sell";
    } else {
      fav.textContent = "🟡 баланс — паритет";
      fav.className = "sk-bs-fav fav-neutral";
    }
  }

  function _skRenderOrderBook(d) {
    const body = document.getElementById("sk-ob-body");
    const vp = d.volume_profile || {};
    const buckets = (vp.buckets || []).slice();
    if (!buckets.length) {
      body.innerHTML = `<div class="sk-empty">нет данных volume profile</div>`;
      return;
    }
    // sort price descending so highest at top
    buckets.sort((a, b) => Number(b.price) - Number(a.price));
    const cur = Number(vp.current_price ?? d.current_price ?? 0);
    const poc = Number(vp.poc ?? -1);
    const vah = Number(vp.vah ?? -1);
    const val = Number(vp.val ?? -1);
    const maxW = buckets.reduce((m, b) => Math.max(m, Number(b.weight_pct) || 0), 0) || 1;

    let curInjected = false;
    const rows = buckets.map((b, i) => {
      const price = Number(b.price);
      const w = Number(b.weight_pct || 0);
      const widthPct = (w / maxW) * 100;
      const isAbove = price > cur;
      const tag = price === poc ? `<span class="sk-ob-pin sk-ob-poc">POC</span>` :
                  price === vah ? `<span class="sk-ob-pin sk-ob-vah">VAH</span>` :
                  price === val ? `<span class="sk-ob-pin sk-ob-val">VAL</span>` : "";
      const next = buckets[i + 1];
      let curRow = "";
      if (!curInjected && next && cur <= price && cur >= Number(next.price)) {
        curRow = `<div class="sk-ob-cur-line"><span>${_skFmtPrice(cur)}</span><span class="sk-ob-pin sk-ob-cur">текущая</span></div>`;
        curInjected = true;
      }
      return `<div class="sk-ob-row ${isAbove ? "above" : "below"}">
        <div class="sk-ob-price">${_skFmtPrice(price)} ${tag}</div>
        <div class="sk-ob-bar"><div class="sk-ob-bar-fill" style="width:${widthPct.toFixed(1)}%"></div></div>
        <div class="sk-ob-pct">${w.toFixed(2)}%</div>
      </div>${curRow}`;
    }).join("");
    body.innerHTML = rows || `<div class="sk-empty">нет уровней</div>`;
  }

  function _skRenderBigPlayers(d) {
    const list = document.getElementById("sk-bp-list");
    const bps = (d.volume_profile && d.volume_profile.big_players) || [];
    if (!bps.length) {
      list.innerHTML = `<div class="sk-empty">сейчас нет ярких уровней (≥80-percentile объёма)</div>`;
      return;
    }
    const cur = Number(d.current_price || 0);
    const sorted = bps.slice().sort((a, b) => Number(b.weight_pct) - Number(a.weight_pct));
    list.innerHTML = sorted.slice(0, 12).map(bp => {
      const above = Number(bp.price) > cur;
      const cls = above ? "sk-bp sk-bp-resist" : "sk-bp sk-bp-support";
      const label = above ? "сопротивление" : "поддержка";
      return `<div class="${cls}">
        <div class="sk-bp-row1"><span class="sk-bp-label">${label}</span><b>${_skFmtPrice(bp.price)}</b></div>
        <div class="sk-bp-row2">
          <span>вес ${Number(bp.weight_pct || 0).toFixed(2)}%</span>
          <span>${above ? "↑" : "↓"} ${cur ? Math.abs(Number(bp.price) - cur).toFixed(5) : "—"}</span>
        </div>
      </div>`;
    }).join("");
  }

  function _skRenderStrategy(d) {
    const grid = document.getElementById("sk-strategy-grid");
    const strat = d.per_session_strategy || {};
    const all = strat.all_sessions || [];
    if (!all.length) {
      grid.innerHTML = `<div class="sk-empty">strategy_config ещё не подгружен</div>`;
      return;
    }
    grid.innerHTML = all.map(s => {
      const wr = Number(s.win_rate_pct || 0);
      const cls = s.qualifies_70pct ? "ok" : (wr >= 60 ? "warn" : "bad");
      const isCurrent = s.session === d.current_session;
      return `<div class="sk-strat ${cls} ${isCurrent ? 'current' : ''}">
        <div class="sk-strat-name">${s.session}${isCurrent ? " · сейчас" : ""}</div>
        <div class="sk-strat-wr">${wr ? wr.toFixed(1) + "%" : "—"} <span class="sk-strat-trades">${s.trades || 0} сделок</span></div>
        <div class="sk-strat-variant" title="${s.best_label || ""}">${s.best_variant || "—"}</div>
      </div>`;
    }).join("");
  }

  async function _skRefreshDetail() {
    const pair = skState.selectedPair;
    const d = await _skFetch(`/api/stakan-view/${pair}`);
    if (!d || d.error) return;
    skState.detail = d;
    _skRenderHeader(d);
    _skRender24h(d);
    _skRender5h(d);
    _skRenderBuyersSellers(d);
    _skRenderOrderBook(d);
    _skRenderBigPlayers(d);
    _skRenderStrategy(d);
  }

  async function _skRefreshSummary() {
    const s = await _skFetch("/api/stakan-view");
    if (!s) return;
    skState.summary = s;
    _skRenderPicker();
  }

  async function refreshStakanView() {
    await Promise.all([_skRefreshSummary(), _skRefreshDetail()]);
  }

  // Initial load + 10-sec live refresh
  try {
    const saved = localStorage.getItem("sk_pair");
    if (saved && SK_PAIRS_28.includes(saved)) skState.selectedPair = saved;
  } catch (_) {}
  refreshStakanView();
  skState.timer = setInterval(refreshStakanView, SK_REFRESH_MS);

  // ─── 2026-05-04: 5-секундный live-price refresh для текущей пары ─────────
  // Пользователь явно просил «текущая цена обновляется каждые 5 секунд».
  // Тяжёлый /api/stakan-view/{pair} остаётся 10s; сюда тащим только цену.
  const SK_PRICE_REFRESH_MS = 5_000;
  let _skLastLivePrice = null;
  async function _skRefreshLivePrice() {
    const pair = skState.selectedPair;
    if (!pair) return;
    const r = await _skFetch(`/api/live-price/${pair}`);
    if (!r || !r.price) return;
    const priceEl = document.getElementById("sk-pair-price");
    const pulseEl = document.getElementById("sk-pair-pulse");
    const pulseTxt = document.getElementById("sk-pair-pulse-txt");
    const deltaEl = document.getElementById("sk-pair-pulse-delta");
    if (priceEl) priceEl.textContent = _skFmtPrice(r.price);
    if (pulseEl) {
      pulseEl.classList.remove("up","down");
      if (_skLastLivePrice != null && r.price !== _skLastLivePrice) {
        pulseEl.classList.add(r.price > _skLastLivePrice ? "up" : "down");
      }
    }
    _skLastLivePrice = r.price;
    if (pulseTxt) {
      const ts = r.ts ? new Date(r.ts).toLocaleTimeString("ru-RU") : "";
      pulseTxt.textContent = `live ${ts}`;
    }
    if (deltaEl) {
      const d1 = r.change_1m_pips;
      const d5 = r.change_5m_pips;
      const sign1 = (d1 > 0 ? "+" : "");
      const sign5 = (d5 > 0 ? "+" : "");
      deltaEl.innerHTML =
        `<span class="${d1 > 0 ? 'pos' : d1 < 0 ? 'neg' : ''}">1м ${sign1}${d1 ?? "—"}</span> · ` +
        `<span class="${d5 > 0 ? 'pos' : d5 < 0 ? 'neg' : ''}">5м ${sign5}${d5 ?? "—"}</span>`;
    }
  }
  _skRefreshLivePrice();
  setInterval(_skRefreshLivePrice, SK_PRICE_REFRESH_MS);

  // ─── 2026-05-04: news-watch — предупреждение о high-impact событиях ──────
  async function _skRefreshNewsWatch() {
    const pair = skState.selectedPair;
    if (!pair) return;
    const warnEl = document.getElementById("sk-news-warning");
    const warnTxt = document.getElementById("sk-news-warning-text");
    if (!warnEl) return;
    const r = await _skFetch(`/api/news-watch/${pair}?hours_ahead=5`);
    if (!r || r.error) {
      warnEl.hidden = true;
      return;
    }
    if ((r.events || []).length === 0) {
      warnEl.hidden = true;
      return;
    }
    const lines = (r.events || []).slice(0, 3).map(ev => {
      const t = new Date(ev.time).toLocaleString("ru-RU", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" });
      return `${t} — ${ev.title}`;
    }).join(" · ");
    warnTxt.textContent = `Внимание · ${r.events.length} high-impact событий ≤5ч: ${lines}. Если откроешь сделку — может развернуть.`;
    warnEl.hidden = false;
  }
  _skRefreshNewsWatch();
  setInterval(_skRefreshNewsWatch, 60_000); // news refreshed every minute (RSS-cached 15 min anyway)

  // ─── 2026-05-04: СОСТОЯНИЕ СИСТЕМЫ — health/heartbeat каждые 5 сек ───────
  async function refreshSystemHealth() {
    const grid = document.getElementById("sh-grid");
    if (!grid) return;
    const r = await _skFetch("/api/health");
    if (!r || !r.components) {
      grid.innerHTML = `<div class="sh-empty">не удалось получить /api/health</div>`;
      return;
    }
    const comps = r.components || {};
    const entries = Object.entries(comps);
    const alive = entries.filter(([_, v]) => v && v.alive).length;
    const dead = entries.length - alive;
    grid.innerHTML = entries.map(([name, v]) => {
      const ok = !!(v && v.alive);
      const ageSec = v && v.age_sec != null ? v.age_sec : null;
      const ageTxt = ageSec == null ? "—" :
        ageSec < 60 ? `${ageSec}s` :
        ageSec < 3600 ? `${Math.round(ageSec/60)}m` :
        `${(ageSec/3600).toFixed(1)}h`;
      const cls = ok ? "sh-card sh-ok" : "sh-card sh-bad";
      const dot = ok ? "🟢" : "🔴";
      return `<div class="${cls}">
        <div class="sh-dot">${dot}</div>
        <div class="sh-name">${name}</div>
        <div class="sh-age">${ok ? "жив" : "мёртв"} · ${ageTxt}</div>
        ${v && v.pid ? `<div class="sh-pid muted">pid ${v.pid}</div>` : ""}
      </div>`;
    }).join("");
    const setTxt = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    setTxt("sh-total", entries.length);
    setTxt("sh-alive", alive);
    setTxt("sh-dead", dead);
    setTxt("sh-pt-open", (r.paper_trader_summary || {}).open_count ?? "—");
    setTxt("sh-stk-open", (r.stakan_summary || {}).open_count ?? "—");
    setTxt("sh-closed", ((r.paper_trader_summary || {}).closed_count ?? 0) + ((r.stakan_summary || {}).closed_count ?? 0));
    const pill = document.getElementById("sh-summary-pill");
    if (pill) pill.textContent = `${alive}/${entries.length} живы · ${dead} мертвы`;
    const warn = document.getElementById("sh-warnings");
    if (warn) {
      const deadList = entries.filter(([_, v]) => !v || !v.alive);
      if (deadList.length > 0) {
        warn.innerHTML = `<div class="sh-warn">⚠️ Мёртвые: ${deadList.map(([n]) => n).join(", ")}</div>`;
      } else {
        warn.innerHTML = "";
      }
    }
  }
  refreshSystemHealth();
  setInterval(refreshSystemHealth, 5_000);

  // ─── 2026-05-04: АВТО-СДЕЛКИ ОТ СТАКАНА — paper_trader_stakan ────────────
  async function refreshStakanAutoTrades() {
    const openTbody = document.getElementById("mt-stk-open-tbody");
    const closedTbody = document.getElementById("mt-stk-closed-tbody");
    if (!openTbody && !closedTbody) return;
    const [stats, openR, closedR] = await Promise.all([
      _skFetch("/api/stakan/stats"),
      _skFetch("/api/stakan/open-trades"),
      _skFetch("/api/stakan/closed-trades?limit=20"),
    ]);
    const setTxt = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    if (stats) {
      setTxt("mt-stk-open", stats.open ?? 0);
      setTxt("mt-stk-closed", stats.closed ?? 0);
      setTxt("mt-stk-wins", stats.wins ?? 0);
      setTxt("mt-stk-losses", stats.losses ?? 0);
      setTxt("mt-stk-wr", stats.win_rate_pct != null ? `${Number(stats.win_rate_pct).toFixed(1)}%` : "—");
      setTxt("mt-stk-pnl", stats.total_pnl != null ? `${Number(stats.total_pnl).toFixed(2)}$` : "—");
      const pill = document.getElementById("mt-stk-pill");
      if (pill) pill.textContent = `WR ${stats.win_rate_pct != null ? Number(stats.win_rate_pct).toFixed(0)+"%" : "—"} · ${stats.closed ?? 0} закр.`;
    }
    if (openR && openTbody) {
      const trades = openR.trades || [];
      if (trades.length === 0) {
        openTbody.innerHTML = `<tr><td colspan="7" class="mt-empty">сейчас нет открытых авто-сделок от стакана</td></tr>`;
      } else {
        openTbody.innerHTML = trades.map(t => {
          const side = t.side || "";
          const sideCls = side === "BUY" ? "mt-buy" : "mt-sell";
          const opened = t.opened_at ? new Date(t.opened_at).toLocaleString("ru-RU", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" }) : "—";
          const expires = t.expires_at ? new Date(t.expires_at).toLocaleString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "—";
          const cur = t.current_price != null ? _skFmtPrice(t.current_price) : "—";
          const pnl = t.unrealized_pnl != null ? `${Number(t.unrealized_pnl).toFixed(2)}$` : "—";
          return `<tr>
            <td>${t.pair}</td>
            <td class="${sideCls}">${side}</td>
            <td>${opened}</td>
            <td>${_skFmtPrice(t.entry_price)}</td>
            <td>${expires}</td>
            <td>${cur}</td>
            <td>${pnl}</td>
          </tr>`;
        }).join("");
      }
    }
    if (closedR && closedTbody) {
      const trades = (closedR.trades || closedR || []).slice(0, 20);
      if (trades.length === 0) {
        closedTbody.innerHTML = `<tr><td colspan="7" class="mt-empty">пока нет закрытых авто-сделок от стакана</td></tr>`;
      } else {
        closedTbody.innerHTML = trades.map(t => {
          const side = t.side || "";
          const sideCls = side === "BUY" ? "mt-buy" : "mt-sell";
          const opened = t.opened_at ? new Date(t.opened_at).toLocaleString("ru-RU", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" }) : "—";
          const closed = t.closed_at ? new Date(t.closed_at).toLocaleString("ru-RU", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" }) : "—";
          const result = t.result || "?";
          const resCls = result === "WIN" ? "mt-green" : result === "LOSS" ? "mt-red" : "muted";
          const pnl = t.pnl != null ? `${Number(t.pnl).toFixed(2)}$` : "—";
          return `<tr>
            <td>${t.pair}</td>
            <td class="${sideCls}">${side}</td>
            <td>${opened}</td>
            <td>${closed}</td>
            <td>${t.strategy_id || "—"}</td>
            <td class="${resCls}"><b>${result}</b></td>
            <td>${pnl}</td>
          </tr>`;
        }).join("");
      }
    }
  }
  refreshStakanAutoTrades();
  setInterval(refreshStakanAutoTrades, 10_000);

  // When user picks a different pair, immediately refresh price + news + stakan
  // (the existing setInterval still runs in parallel).
  const _origPicker = _skRefreshDetail;
  // (no override needed — _skRefreshDetail reads skState.selectedPair which the
  //  picker click handler updates; price loop reads same; just ensure first
  //  click triggers fresh price.)
  document.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest && e.target.closest(".sk-chip");
    if (btn && btn.dataset.pair) {
      _skRefreshLivePrice();
      _skRefreshNewsWatch();
    }
  });
  // ═══════════════════════════════════════════════════════════════════════
  // PHASE 15 · Quality Dashboard — Time + Confluence + Win Rates (2026-05-05)
  // ═══════════════════════════════════════════════════════════════════════

  async function refreshPhase15() {
    try {
      const [timeData, wrData, forecastData, stratData] = await Promise.all([
        _skFetch("/api/time-status"),
        _skFetch("/api/pair-winrates"),
        _skFetch("/api/forecasts"),
        _skFetch("/api/strategy-winrates"),
      ]);

      // Time countdown
      if (timeData) {
        const h = Math.floor(timeData.hours_to_midnight_utc);
        const m = Math.round((timeData.hours_to_midnight_utc - h) * 60);
        const midnightEl = document.getElementById("p15-midnight-countdown");
        if (midnightEl) midnightEl.textContent = `${h}ч ${String(m).padStart(2, "0")}м`;

        const sessEl = document.getElementById("p15-session");
        if (sessEl) sessEl.textContent = timeData.current_session || "Off";
        if (sessEl && timeData.current_session === "Off") sessEl.style.color = "#ff6666";
        else if (sessEl) sessEl.style.color = "#00ff88";

        const sessRemEl = document.getElementById("p15-session-remaining");
        if (sessRemEl) {
          const sr = timeData.session_remaining_hours;
          sessRemEl.textContent = sr > 0 ? `осталось ${sr.toFixed(1)}ч · max expiry ${timeData.safe_max_expiry_hours}ч` : "сессия закрыта";
        }
      }

      // Win rate: show STRATEGY backtest WR for ALL 28 pairs + paper trading WR where available
      {
        const stratPairs = (stratData && stratData.pairs) || {};
        const stratSummary = (stratData && stratData.summary) || {};
        const wr = (wrData && wrData.pair_winrates) || {};

        // Total WR from strategy backtest (the real metric)
        const wrEl = document.getElementById("p15-total-wr");
        if (wrEl) {
          const avgWR = stratSummary.avg_best_wr_pct || 0;
          wrEl.textContent = avgWR > 0 ? `${avgWR.toFixed(1)}%` : "—";
          wrEl.style.color = avgWR >= 70 ? "#00ff88" : avgWR >= 55 ? "#ffcc00" : "#ff4444";
        }
        const detailEl = document.getElementById("p15-total-wr-detail");
        if (detailEl) {
          const a70 = stratSummary.pairs_above_70pct || 0;
          const a80 = stratSummary.pairs_above_80pct || 0;
          detailEl.textContent = `${a80} пар ≥80% · ${a70} пар ≥70% · бэктест 365д`;
        }

        // Per-pair grid: show ALL 28 pairs from strategy backtest
        const pairGrid = document.getElementById("p15-pair-wr-grid");
        if (pairGrid) {
          // Combine strategy WR with paper trading WR
          const allPairs = Object.entries(stratPairs).sort((a, b) => (b[1].best_wr_pct || 0) - (a[1].best_wr_pct || 0));
          if (allPairs.length === 0) {
            pairGrid.innerHTML = '<div style="color:#5a7a9a;font-size:12px;padding:8px">загрузка…</div>';
          } else {
            pairGrid.innerHTML = allPairs.map(([pair, s]) => {
              const bestWR = s.best_wr_pct || 0;
              const color = bestWR >= 80 ? "#00ff88" : bestWR >= 70 ? "#00e1ff" : bestWR >= 60 ? "#ffcc00" : "#ff6666";
              const bg = bestWR >= 80 ? "#00ff8815" : bestWR >= 70 ? "#00e1ff15" : bestWR >= 60 ? "#ffcc0015" : "#ff666615";
              const paperWR = wr[pair];
              const paperStr = paperWR ? `Paper: ${paperWR.wins}W/${paperWR.losses}L` : "";
              return `<div style="background:${bg};border:1px solid ${color}33;border-radius:8px;padding:8px 10px;font-size:12px">
                <div style="font-weight:700;color:#fff">${pair}</div>
                <div style="color:${color};font-size:16px;font-weight:800">${bestWR.toFixed(1)}%</div>
                <div style="color:#5a7a9a;font-size:10px">${s.best_session || ""}${paperStr ? ` · ${paperStr}` : ""}</div>
              </div>`;
            }).join("");
          }
        }
      }

      // ALL signals with quality tiers + strategy WR from API
      if (forecastData && forecastData.forecasts) {
        const fc = forecastData.forecasts;
        const stratPairs = (stratData && stratData.pairs) || {};
        const signalsGrid = document.getElementById("p15-strong-signals");
        const qualityPill = document.getElementById("p15-quality-pill");
        const qualityEl = document.getElementById("p15-signal-quality");

        // Enrich forecasts with strategy WR from /api/strategy-winrates
        const allSignals = Object.values(fc).filter(f =>
          f && !f.skipped
        ).map(f => {
          const sw = stratPairs[f.pair] || {};
          const bestWR = sw.best_wr_pct || f.strategy_backtest_wr_pct || 0;
          // Recalculate quality tier using strategy WR
          const effectiveProb = Math.max(f.probability_pct || 0, bestWR);
          const tier = effectiveProb >= 75 ? "STRONG" : effectiveProb >= 70 ? "MODERATE" : "WEAK";
          // Recalculate EV with best WR
          const brokerPayout = (f.broker_payout_pct || 85) / 100;
          const wrFrac = Math.max((f.probability_pct || 0), bestWR) / 100;
          const ev = wrFrac * brokerPayout - (1 - wrFrac);
          return { ...f, _stratWR: bestWR, _bestSession: sw.best_session || "", _tier: tier, _ev: ev * 100 };
        }).sort((a, b) => (b._stratWR || 0) - (a._stratWR || 0));

        const strongCount = allSignals.filter(f => f._tier === "STRONG").length;
        const totalForecasts = allSignals.length;
        const above70 = allSignals.filter(f => (f._stratWR || 0) >= 70).length;
        const above80 = allSignals.filter(f => (f._stratWR || 0) >= 80).length;

        if (qualityEl) qualityEl.textContent = `${totalForecasts} пар`;
        if (qualityPill) qualityPill.textContent = `v15 · ${above80} пар ≥80% WR · ${above70} пар ≥70% WR · ${strongCount} STRONG`;

        if (signalsGrid) {
          if (allSignals.length === 0) {
            signalsGrid.innerHTML = '<div style="color:#ffcc00;font-size:13px;padding:12px;background:#ffcc0010;border-radius:8px;border:1px solid #ffcc0033">Рынок закрыт или сканирование в процессе…</div>';
          } else {
            signalsGrid.innerHTML = allSignals.map(f => {
              const sideColor = f.side === "BUY" ? "#00ff88" : "#ff4466";
              const tier = f._tier || "WEAK";
              const tierColor = tier === "STRONG" ? "#00ff88" : tier === "MODERATE" ? "#ffcc00" : "#ff6666";
              const tierBg = tier === "STRONG" ? "#00ff8812" : tier === "MODERATE" ? "#ffcc0012" : "#ff666612";
              const tierLabel = tier === "STRONG" ? "СИЛЬНЫЙ" : tier === "MODERATE" ? "СРЕДНИЙ" : "СЛАБЫЙ";
              const stratWR = f._stratWR || 0;
              const wrColor = stratWR >= 80 ? "#00ff88" : stratWR >= 70 ? "#00e1ff" : stratWR >= 60 ? "#ffcc00" : "#ff6666";
              const evPct = f._ev || 0;
              const evColor = evPct >= 5 ? "#00ff88" : evPct > 0 ? "#ffcc00" : "#ff4444";
              const bestSess = f._bestSession ? ` (${f._bestSession})` : "";
              return `<div style="background:${tierBg};border:1px solid ${tierColor}33;border-radius:10px;padding:12px;position:relative;overflow:hidden">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                  <span style="font-weight:800;color:#fff;font-size:14px">${f.pair}</span>
                  <div style="display:flex;gap:4px">
                    <span style="background:${sideColor}22;color:${sideColor};padding:2px 8px;border-radius:4px;font-weight:700;font-size:11px">${f.side}</span>
                    <span style="background:${tierColor}22;color:${tierColor};padding:2px 6px;border-radius:4px;font-weight:700;font-size:10px">${tierLabel}</span>
                  </div>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:11px">
                  <div><span style="color:#7aa2c0">Стратегия WR:</span> <b style="color:${wrColor};font-size:14px">${stratWR.toFixed(1)}%</b>${bestSess ? `<span style="color:#5a7a9a;font-size:9px">${bestSess}</span>` : ""}</div>
                  <div><span style="color:#7aa2c0">Вероятность:</span> <b style="color:#fff">${(f.probability_pct || 0).toFixed(1)}%</b></div>
                  <div><span style="color:#7aa2c0">EV за сделку:</span> <b style="color:${evColor}">${evPct >= 0 ? "+" : ""}${evPct.toFixed(1)}%</b></div>
                  <div><span style="color:#7aa2c0">Score:</span> <b style="color:#fff">${f.score}/${f.max_score}</b></div>
                </div>
                <div style="position:absolute;top:0;right:0;bottom:0;width:4px;background:${tierColor}"></div>
              </div>`;
            }).join("");
          }
        }
      }
    } catch (e) {
      console.warn("Phase 15 refresh failed:", e);
    }
  }

  // Initial load + periodic refresh
  refreshPhase15();
  setInterval(refreshPhase15, 15_000);

})();
