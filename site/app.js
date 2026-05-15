/* eslint-disable no-console */
/**
 * FOREX AI — frontend logic.
 *
 * Reads three live JSON files from the repository's `data` branch
 * (filled by the AI brain + refresh_data cron jobs):
 *
 *   data/top1.json        — the canonical Top-1 forecast (every 5h)
 *   data/brain_full.json  — per-pair breakdown across 6 AI layers
 *   data/signals.json     — 28-pair signals + prices (every 5 min)
 *
 * Everything renders in the user's language (Russian) with UTC+5 timestamps.
 * Charts are TradingView Advanced Chart Widgets — no account needed.
 */
(function () {
  "use strict";

  // ─── Config ──────────────────────────────────────────────────────────
  const REPO_OWNER = "Jony-wws";
  const REPO_NAME = "Forex-wws2277";
  const DATA_BRANCH = "data";
  // Use raw.githubusercontent.com — it serves the live branch tip with
  // an Etag, never the multi-hour jsDelivr branch cache.  The user
  // explicitly asked for a 1-minute refresh, which means we MUST read
  // the freshest bytes the moment the GitHub Action pushes them.
  // statically.io is a battle-tested mirror we fall back to if raw is
  // throttled (rare, but not impossible from mobile Chrome).
  const PRIMARY_BASE  = `https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${DATA_BRANCH}/data`;
  const FALLBACK_BASE = `https://cdn.statically.io/gh/${REPO_OWNER}/${REPO_NAME}/${DATA_BRANCH}/data`;
  const REFRESH_MS = 60 * 1000;          // re-fetch top1.json every minute
  const SIGNALS_REFRESH_MS = 60 * 1000;  // re-fetch signals.json every minute
  const TZ_OFFSET = 5 * 3600 * 1000;     // UTC+5 (user is in Russia)
  // 5h cycle boundaries in UTC (same grid as scripts/cycle_5h.py and
  // app/brain.py::_next_cycle_iso).  Used as a client-side safety net:
  // if the server-side `next_cycle_utc` falls into the past (stale CDN
  // or unusually long cron lag), we recompute it locally so the
  // countdown never freezes at 00:00:00.
  const CYCLE_BOUNDARIES_UTC_HOURS = [0, 5, 10, 15, 19];

  // ─── State ───────────────────────────────────────────────────────────
  const state = {
    top1: null,
    brain: null,
    signals: null,
    selectedPair: null,
    countdownTimer: null,
    heroChartWidget: null,
    analysisChartWidget: null,
    lastTop1Fetch: 0,
  };

  // ─── Helpers ─────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function $$(sel) { return Array.from(document.querySelectorAll(sel)); }

  function fmtTimeMSk(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const local = new Date(d.getTime() + TZ_OFFSET);
    return local.toISOString().slice(11, 16); // HH:MM
  }

  function fmtDateMSk(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const local = new Date(d.getTime() + TZ_OFFSET);
    const iso10 = local.toISOString().slice(0, 10).split("-").reverse().join(".");
    return `${iso10} ${local.toISOString().slice(11, 16)}`;
  }

  function tvSymbol(pair) {
    // TradingView's forex feed uses FX:EURUSD style symbols for majors and
    // OANDA/FX_IDC for some crosses.  FX: covers all 28 we track.
    return `FX:${pair}`;
  }

  async function fetchJson(path) {
    // Cache-bust by minute bucket so the browser always picks up the
    // newest cron output without disabling HTTP cache entirely.
    const bucket = Math.floor(Date.now() / 60000);
    const tryOne = async (base) => {
      const url = `${base}/${path}?t=${bucket}`;
      const resp = await fetch(url, { cache: "no-store" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status} for ${path}`);
      return resp.json();
    };
    try {
      return await tryOne(PRIMARY_BASE);
    } catch (e) {
      // raw.githubusercontent rare-fails on flaky mobile networks; try
      // statically.io as a hot spare so the dashboard keeps ticking.
      return tryOne(FALLBACK_BASE);
    }
  }

  function nextCycleIsoFromGrid(now) {
    const base = now instanceof Date ? now : new Date();
    const candidates = CYCLE_BOUNDARIES_UTC_HOURS.map(h => {
      const d = new Date(Date.UTC(
        base.getUTCFullYear(), base.getUTCMonth(), base.getUTCDate(), h, 0, 0, 0
      ));
      if (d.getTime() <= base.getTime()) d.setUTCDate(d.getUTCDate() + 1);
      return d;
    });
    candidates.sort((a, b) => a.getTime() - b.getTime());
    return candidates[0].toISOString();
  }

  // ─── Tab routing ─────────────────────────────────────────────────────
  function setTab(id) {
    $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === `tab-${id}`));
    $$(".tabs .tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === id));
    $$(".bottom-nav .nav-item").forEach(b => b.classList.toggle("active", b.dataset.tab === id));
    // Lazy-init analysis chart when the tab first appears so we don't
    // pay the TradingView widget cost up-front.
    if (id === "analysis" && state.selectedPair) {
      mountAnalysisChart(state.selectedPair);
    }
  }
  $$(".tabs .tab-btn").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $$(".bottom-nav .nav-item").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));

  // ─── Live status pill ────────────────────────────────────────────────
  function setLive(status, text) {
    const dot = $("liveDot");
    dot.classList.remove("stale", "offline");
    if (status === "stale") dot.classList.add("stale");
    else if (status === "offline") dot.classList.add("offline");
    $("liveText").textContent = text;
  }

  // ─── Top-1 hero card ─────────────────────────────────────────────────
  function renderHero(top1Payload) {
    const top = top1Payload && top1Payload.top1;
    if (!top) {
      $("topPair").textContent = "—";
      $("topName").textContent = "AI пока не находит качественного сетапа.";
      $("topSide").className = "side-tag wait";
      $("topSide").textContent = "ОЖИДАНИЕ";
      $("topTier").textContent = "Все 28 пар отфильтрованы по veto-правилам";
      $("confFill").style.width = "0%";
      $("confText").textContent = "—";
      ["mEntry","mAtr","mSl","mTp"].forEach(id => $(id).textContent = "—");
      $("aiLayers").innerHTML = `
        <div class="empty">
          Защита для реальной торговли: если ни одна пара не прошла все фильтры
          (multi-TF, ADX ≥ 20, отсутствие новостей в ближайшие 2ч),
          AI не показывает сигнал и ждёт следующего цикла. <br/><br/>
          <span class="empty-em">Это нормально — не каждый цикл даёт качество.</span>
        </div>`;
      return;
    }

    $("topPair").textContent = top.pair;
    $("topName").textContent = top.name_ru;

    const sideEl = $("topSide");
    if (top.side === "BUY") {
      sideEl.className = "side-tag buy";
      sideEl.textContent = "ПОКУПКА (BUY)";
    } else if (top.side === "SELL") {
      sideEl.className = "side-tag sell";
      sideEl.textContent = "ПРОДАЖА (SELL)";
    } else {
      sideEl.className = "side-tag wait";
      sideEl.textContent = "ОЖИДАНИЕ";
    }

    const tierBits = [];
    if (top.layers && top.layers.technical) {
      tierBits.push(`ADX H1 ${(top.layers.technical.adx_h1 || 0).toFixed(0)}`);
      tierBits.push(`персистентность ${(top.layers.technical.persistence_5h || 0).toFixed(0)}%`);
    }
    $("topTier").textContent = tierBits.join(" · ") || "—";

    const conf = top.confidence || 0;
    $("confFill").style.width = `${conf}%`;
    $("confText").textContent = `${conf}%`;

    const lv = top.levels || {};
    $("mEntry").textContent = lv.entry != null ? lv.entry : "—";
    $("mAtr").textContent = lv.atr_h1 != null ? lv.atr_h1 : "—";
    $("mSl").textContent = lv.stop_loss != null ? lv.stop_loss : "—";
    $("mTp").textContent = lv.take_profit != null ? lv.take_profit : "—";

    renderLayers($("aiLayers"), top.layers);
    mountHeroChart(top.pair);
  }

  // ─── 6-layer breakdown renderer ──────────────────────────────────────
  function renderLayers(container, layers) {
    if (!layers) {
      container.innerHTML = `<div class="empty">Нет данных по слоям.</div>`;
      return;
    }
    const items = [];

    if (layers.technical) {
      const t = layers.technical;
      items.push({
        icon: "T",
        title: "Технический анализ",
        score: t.normalised,
        reason: `Score ${t.score}/${t.max_score} · ADX H1 ${(t.adx_h1||0).toFixed(0)} · persist ${(t.persistence_5h||0).toFixed(0)}% · multi-TF ${t.multi_tf_aligned?'✓':'×'}`
                + (t.extras && t.extras.smc && t.extras.smc.reasons.length
                   ? ` · SMC: ${t.extras.smc.reasons.slice(0,2).join('; ')}`
                   : "")
                + (t.extras && t.extras.wyckoff && t.extras.wyckoff.reason
                   ? ` · Wyckoff: ${t.extras.wyckoff.reason}`
                   : ""),
      });
    }
    if (layers.macro) {
      items.push({
        icon: "M",
        title: "Макро (DXY · yields · commodities)",
        score: layers.macro.normalised,
        reason: layers.macro.reason || "—",
      });
    }
    if (layers.fundamental) {
      items.push({
        icon: "F",
        title: "Фундамент (Carry · ставки ЦБ)",
        score: layers.fundamental.normalised,
        reason: layers.fundamental.reason || "—",
      });
    }
    if (layers.news) {
      const baseM = layers.news.next_event_base_min;
      const quoteM = layers.news.next_event_quote_min;
      const detail = (baseM < 9999 || quoteM < 9999)
        ? `Ближайшее: ${Math.min(baseM, quoteM)} мин`
        : "Новостных событий в ближайшие 2 часа нет";
      items.push({
        icon: "N",
        title: "Новостной фон",
        score: layers.news.score,
        reason: `${layers.news.reason || "—"} · ${detail}`,
      });
    }
    if (layers.sentiment) {
      items.push({
        icon: "S",
        title: "Sentiment (risk-on/off)",
        score: layers.sentiment.normalised,
        reason: layers.sentiment.reason || "—",
      });
    }
    if (layers.political) {
      items.push({
        icon: "P",
        title: "Геополитика",
        score: layers.political.normalised,
        reason: layers.political.reason || "—",
      });
    }

    container.innerHTML = items.map(it => {
      const s = Number(it.score) || 0;
      const cls = s > 0.05 ? "pos" : s < -0.05 ? "neg" : "";
      const scoreText = s > 0 ? `+${s.toFixed(2)}` : s.toFixed(2);
      return `
        <div class="layer">
          <div class="layer-icon">${it.icon}</div>
          <div class="layer-body">
            <div class="layer-title">
              ${it.title}
              <span class="layer-score ${cls}">${scoreText}</span>
            </div>
            <div class="layer-reason">${escapeHtml(it.reason)}</div>
          </div>
        </div>`;
    }).join("");
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ─── TradingView widgets ─────────────────────────────────────────────
  function mountHeroChart(pair) {
    if (!window.TradingView) return;
    const el = $("heroChart");
    el.innerHTML = `<div id="heroChartInner"></div>`;
    state.heroChartWidget = new TradingView.widget({
      container_id: "heroChartInner",
      symbol: tvSymbol(pair),
      interval: "60",
      timezone: "Asia/Karachi", // UTC+5
      theme: "dark",
      style: "1",
      locale: "ru",
      toolbar_bg: "#21111a",
      hide_side_toolbar: true,
      hide_top_toolbar: false,
      allow_symbol_change: false,
      withdateranges: false,
      save_image: false,
      autosize: true,
    });
  }

  function mountAnalysisChart(pair) {
    if (!window.TradingView) return;
    const el = $("analysisChart");
    el.innerHTML = `<div id="analysisChartInner"></div>`;
    state.analysisChartWidget = new TradingView.widget({
      container_id: "analysisChartInner",
      symbol: tvSymbol(pair),
      interval: "60",
      timezone: "Asia/Karachi",
      theme: "dark",
      style: "1",
      locale: "ru",
      toolbar_bg: "#21111a",
      studies: ["RSI@tv-basicstudies", "MACD@tv-basicstudies"],
      hide_side_toolbar: false,
      hide_top_toolbar: false,
      allow_symbol_change: true,
      withdateranges: true,
      save_image: false,
      autosize: true,
    });
  }

  // ─── Pair list ───────────────────────────────────────────────────────
  function renderPairs(signals) {
    const container = $("pairList");
    if (!signals || !signals.pairs) {
      container.innerHTML = `<div class="empty">Нет данных по парам — ждём первого refresh_data.</div>`;
      return;
    }
    const entries = Object.values(signals.pairs);
    entries.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
    container.innerHTML = entries.map(p => {
      const ch = Number(p.change_24h_pct || 0);
      const cls = ch > 0 ? "color-up" : ch < 0 ? "color-down" : "";
      const sign = ch > 0 ? "+" : "";
      return `
        <div class="pair-row" data-pair="${p.pair}">
          <div>
            <div class="sym">${p.pair}</div>
            <div class="name">${escapeHtml(p.name_ru)}</div>
          </div>
          <div>
            <div style="font-size:11px;color:var(--muted)">${escapeHtml(p.strength || "—")}</div>
            <div style="font-size:11px;color:var(--soft)">Уверенность ${p.confidence || 0}%</div>
          </div>
          <div>
            <div class="price">${p.price_display || "—"}</div>
            <div class="change ${cls}">${sign}${ch.toFixed(2)}%</div>
          </div>
        </div>`;
    }).join("");
    $$("#pairList .pair-row").forEach(row =>
      row.addEventListener("click", () => {
        const pair = row.dataset.pair;
        state.selectedPair = pair;
        // sync the analysis-tab selector and jump there
        const sel = $("analysisPair");
        if (sel) sel.value = pair;
        setTab("analysis");
        mountAnalysisChart(pair);
        renderAnalysisLayers(pair);
      })
    );
  }

  // ─── Analysis tab ────────────────────────────────────────────────────
  function populateAnalysisSelector(signals) {
    const sel = $("analysisPair");
    if (!signals || !signals.pairs) {
      sel.innerHTML = `<option>—</option>`;
      return;
    }
    sel.innerHTML = Object.values(signals.pairs).map(p =>
      `<option value="${p.pair}">${p.pair} · ${escapeHtml(p.name_ru)}</option>`
    ).join("");
    if (state.selectedPair) sel.value = state.selectedPair;
    else state.selectedPair = sel.value;
    sel.addEventListener("change", () => {
      state.selectedPair = sel.value;
      mountAnalysisChart(sel.value);
      renderAnalysisLayers(sel.value);
    });
  }

  function renderAnalysisLayers(pair) {
    const target = $("analysisLayers");
    if (!state.brain || !state.brain.all_evals) {
      target.innerHTML = `<div class="empty">Ждём данных от AI-мозга…</div>`;
      return;
    }
    const ev = state.brain.all_evals.find(e => e.pair === pair);
    if (!ev) {
      target.innerHTML = `<div class="empty">Нет данных по этой паре.</div>`;
      return;
    }
    if (ev.veto) {
      target.innerHTML = `<div class="empty">
        Veto: <span class="empty-em">${escapeHtml(ev.veto)}</span><br/>
        Эта пара исключена из Top-1 в текущем цикле.
      </div>` + layersHtml(ev.layers);
    } else {
      target.innerHTML = layersHtml(ev.layers);
    }
  }

  function layersHtml(layers) {
    const fake = document.createElement("div");
    renderLayers(fake, layers);
    return fake.innerHTML;
  }

  // ─── News tab ────────────────────────────────────────────────────────
  function renderNews(brain) {
    if (!brain) {
      $("newsList").innerHTML = `<div class="empty">Ждём первый отчёт мозга…</div>`;
      $("upcomingEvents").innerHTML = `<div class="empty">—</div>`;
      return;
    }
    // We don't currently publish raw headlines (they'd inflate the
    // data branch); show a one-line summary per currency plus the
    // upcoming high-impact event minutes.
    const risk = brain.political_risk || {};
    const minutes = brain.news_minutes || {};

    const newsRows = Object.keys(risk).map(c => `
      <div class="news-item">
        <div class="news-source">${c}</div>
        <div class="news-title">Гео-риск: ${risk[c]}/3</div>
        <div class="news-tags">Источник: Reuters World · BBC World</div>
      </div>`).join("");
    $("newsList").innerHTML = newsRows || `<div class="empty">Спокойно</div>`;

    const ev = Object.entries(minutes)
      .filter(([_, m]) => m < 9999)
      .sort((a, b) => a[1] - b[1])
      .map(([c, m]) => `
        <div class="news-item">
          <div class="news-source">${c}</div>
          <div class="news-title">через ${m} мин — high-impact</div>
          <div class="news-tags">Источник: ForexFactory</div>
        </div>`).join("");
    $("upcomingEvents").innerHTML = ev || `<div class="empty">В ближайшее время — спокойно.</div>`;
  }

  // ─── Currency strength tab ───────────────────────────────────────────
  function renderStrength(top1Payload) {
    if (!top1Payload || !top1Payload.macro_currency_strength) {
      $("strengthList").innerHTML = `<div class="empty">Ждём расчёта…</div>`;
      return;
    }
    const cs = top1Payload.macro_currency_strength;
    const rows = Object.entries(cs)
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
      .map(([c, v]) => {
        const w = Math.min(100, Math.abs(v) / 3 * 100);
        const cls = v < 0 ? "neg" : "";
        const sign = v > 0 ? "+" : "";
        return `
          <div class="cs-bar">
            <div class="cs-curr">${c}</div>
            <div class="cs-track"><div class="cs-fill ${cls}" style="width:${w}%"></div></div>
            <div class="cs-value">${sign}${v.toFixed(2)}</div>
          </div>`;
      }).join("");
    $("strengthList").innerHTML = rows;
  }

  // ─── Journal tab ─────────────────────────────────────────────────────
  function renderJournal(top1Payload) {
    if (!top1Payload || !top1Payload.top5 || top1Payload.top5.length === 0) {
      $("journalList").innerHTML = `<div class="empty">История появится после первого цикла.</div>`;
      return;
    }
    const rows = top1Payload.top5.map((t, idx) => `
      <div class="news-item">
        <div class="news-source">#${idx + 1} · ${t.side || "—"}</div>
        <div class="news-title">${t.pair} — ${escapeHtml(t.name_ru || "")} · ${t.confidence || 0}%</div>
        <div class="news-tags">
          <span class="news-tag">multi-TF ${t.layers && t.layers.technical && t.layers.technical.multi_tf_aligned ? "✓" : "×"}</span>
          <span class="news-tag">ADX ${(t.layers && t.layers.technical && t.layers.technical.adx_h1 || 0).toFixed(0)}</span>
          <span class="news-tag">persist ${(t.layers && t.layers.technical && t.layers.technical.persistence_5h || 0).toFixed(0)}%</span>
        </div>
      </div>`).join("");
    $("journalList").innerHTML = rows;
  }

  // ─── Countdown timer ─────────────────────────────────────────────────
  function startCountdown(nextIso) {
    if (state.countdownTimer) clearInterval(state.countdownTimer);
    // If the data branch has not been refreshed yet (stale CDN, first
    // boot, unusually long cron gap) the server-side ``next_cycle_utc``
    // may be in the past.  In that case fall back to the canonical 5h
    // grid so the timer never gets stuck at 00:00:00 — that bug is
    // exactly what the user reported.
    const now = new Date();
    let target = nextIso ? new Date(nextIso).getTime() : NaN;
    if (!Number.isFinite(target) || target <= now.getTime() + 5_000) {
      const fallback = nextCycleIsoFromGrid(now);
      target = new Date(fallback).getTime();
      nextIso = fallback;
      console.info("[countdown] server next_cycle_utc stale — using grid fallback", fallback);
    }
    $("nextCycleAt").textContent = fmtTimeMSk(nextIso);

    const tick = () => {
      const diff = Math.max(0, target - Date.now());
      const h = Math.floor(diff / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      $("countdown").textContent =
        `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
      if (diff <= 0) {
        clearInterval(state.countdownTimer);
        // The cycle boundary just passed — kick off a refetch and then
        // immediately restart the countdown using the next grid slot,
        // so the timer never lingers at zero waiting for the cron.
        reloadAll();
        startCountdown(nextCycleIsoFromGrid(new Date()));
      }
    };
    tick();
    state.countdownTimer = setInterval(tick, 1000);
  }

  // ─── Main reload pipeline ────────────────────────────────────────────
  async function reloadTop1() {
    try {
      const top1 = await fetchJson("top1.json");
      state.top1 = top1;
      state.lastTop1Fetch = Date.now();
      renderHero(top1);
      renderStrength(top1);
      renderJournal(top1);
      startCountdown(top1.next_cycle_utc);
      setLive("live", `Обновлено: ${fmtTimeMSk(top1.generated_at_utc)} (UTC+5)`);
    } catch (e) {
      console.warn("top1.json fetch failed", e);
      // Brain may not have run yet on a fresh repo — show a friendly fallback.
      setLive("stale", "Ждём первый цикл AI-мозга");
      renderHero(null);
    }
    // Brain breakdown is bigger; allow it to lag the top1 fetch.
    try {
      const brain = await fetchJson("brain_full.json");
      state.brain = brain;
      renderNews(brain);
      if (state.selectedPair) renderAnalysisLayers(state.selectedPair);
    } catch (e) {
      console.warn("brain_full.json fetch failed", e);
    }
  }

  async function reloadSignals() {
    try {
      const signals = await fetchJson("signals.json");
      state.signals = signals;
      renderPairs(signals);
      populateAnalysisSelector(signals);
      if (!state.selectedPair && signals.pairs) {
        state.selectedPair = Object.keys(signals.pairs)[0];
      }
    } catch (e) {
      console.warn("signals.json fetch failed", e);
      $("pairList").innerHTML = `<div class="empty">Не удалось загрузить signals.json. Cron публикует его каждые 5 минут.</div>`;
    }
  }

  async function reloadAll() {
    await Promise.all([reloadTop1(), reloadSignals()]);
  }

  // ─── Theme toggle (light / dark) ─────────────────────────────────────
  //
  // User asked for a button to switch between the dark default and a
  // light theme.  The CSS does all the heavy lifting via the
  // ``body.light-theme`` class — we just toggle that class and persist
  // the choice in ``localStorage`` so it survives reloads.
  function applyTheme(theme) {
    const isLight = theme === "light";
    document.body.classList.toggle("light-theme", isLight);
    const icon = document.getElementById("themeIcon");
    const label = document.getElementById("themeLabel");
    const meta = document.querySelector('meta[name="theme-color"]');
    if (icon)  icon.textContent  = isLight ? "\u2600" : "\u263d";       // ☀ / ☽
    if (label) label.textContent = isLight ? "Светлая" : "Темная";
    if (meta)  meta.setAttribute("content", isLight ? "#faf5f3" : "#1a0a0c");
  }

  function initThemeToggle() {
    const saved = localStorage.getItem("forex-theme");
    applyTheme(saved === "light" ? "light" : "dark");
    const btn = document.getElementById("themeToggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const next = document.body.classList.contains("light-theme")
        ? "dark"
        : "light";
      localStorage.setItem("forex-theme", next);
      applyTheme(next);
    });
  }

  // ─── Visibility-driven refetch ───────────────────────────────────────
  //
  // When the user puts the tab in the background, the 60-second refresh
  // loop keeps running but nothing is rendered.  When they come back,
  // we immediately repull top1 + signals so the screen shows the most
  // recent data without waiting for the next tick.
  function initVisibilityRefetch() {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        reloadAll();
      }
    });
  }

  // ─── Boot ────────────────────────────────────────────────────────────
  function boot() {
    setLive("offline", "Подключение…");
    initThemeToggle();
    initVisibilityRefetch();
    reloadAll();
    setInterval(reloadTop1, REFRESH_MS);
    setInterval(reloadSignals, SIGNALS_REFRESH_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
