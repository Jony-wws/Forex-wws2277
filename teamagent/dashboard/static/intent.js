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
})();
