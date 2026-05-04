// stakan-only.js — минимальный фронт для страницы СТАКАН-only.
// Тянет /api/stakan-view/{pair} (10s) + /api/live-price/{pair} (5s).
// Никаких других секций — только селектор пар, большой вердикт,
// стакан и крупные игроки.
(() => {
  "use strict";

  const PAIRS_28 = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY",
    "AUDCAD", "AUDCHF", "AUDNZD", "CADCHF", "NZDCAD", "NZDCHF",
  ];

  const DEFAULT_PAIR = "EURUSD";
  const DETAIL_REFRESH_MS = 10_000;
  const PRICE_REFRESH_MS = 5_000;
  const SUMMARY_REFRESH_MS = 30_000;

  const state = {
    selectedPair: DEFAULT_PAIR,
    detail: null,
    summary: null,
    detailTimer: null,
    priceTimer: null,
    summaryTimer: null,
    lastPrice: null,
    pairVerdict: new Map(),  // pair -> { verdict, color }
  };

  // ── helpers ──────────────────────────────────────────────────────
  function fmtPrice(v) {
    if (v == null || !isFinite(v)) return "—";
    const n = Number(v);
    return n >= 50 ? n.toFixed(3) : n.toFixed(5);
  }
  function fmtPct(v, d = 1) {
    if (v == null || !isFinite(v)) return "—";
    return Number(v).toFixed(d) + "%";
  }
  function fmtPips(v) {
    if (v == null || !isFinite(v)) return "—";
    return Number(v).toFixed(0) + " p";
  }
  function fmtHours(h) {
    if (h == null || !isFinite(h)) return "—";
    return Number(h).toFixed(1) + " ч";
  }

  async function jget(url) {
    try {
      const r = await fetch(url, { cache: "no-store", credentials: "include" });
      if (!r.ok) throw new Error(`${url} -> ${r.status}`);
      return await r.json();
    } catch (e) {
      console.warn("fetch failed:", url, e);
      return null;
    }
  }

  function setStatus(text, kind = "ok") {
    const dot = document.getElementById("so-status-dot");
    const txt = document.getElementById("so-status-text");
    if (txt) txt.textContent = text;
    if (dot) {
      dot.classList.remove("warn", "error");
      if (kind === "warn") dot.classList.add("warn");
      else if (kind === "error") dot.classList.add("error");
    }
  }

  function tickClock() {
    const el = document.getElementById("so-clock");
    if (!el) return;
    const d = new Date();
    el.textContent = d.toUTCString().split(" ")[4] + " UTC";
  }
  setInterval(tickClock, 1000);
  tickClock();

  // ── Picker ───────────────────────────────────────────────────────
  function renderPicker() {
    const grid = document.getElementById("so-picker");
    if (!grid) return;
    const items = (state.summary && state.summary.items) || [];
    const byPair = new Map(items.map((it) => [it.pair, it]));
    const html = PAIRS_28.map((p) => {
      const it = byPair.get(p) || {};
      const v = state.pairVerdict.get(p);
      const verdictTxt = v ? v.short : "";
      const colorCls = v ? v.color : "gray";
      const sideCls = it.side === "BUY" ? "buy" : it.side === "SELL" ? "sell" : "";
      const active = p === state.selectedPair ? "active" : "";
      return `<button class="so-chip ${active}" data-pair="${p}" role="tab" aria-selected="${active === "active"}">
        <span class="so-chip-pair">${p}</span>
        <span class="so-chip-side ${sideCls}">${it.side || "·"}</span>
        <span class="so-chip-verdict ${colorCls}">${verdictTxt || "ЖДЁМ"}</span>
      </button>`;
    }).join("");
    grid.innerHTML = html;
    grid.querySelectorAll(".so-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.pair === state.selectedPair) return;
        state.selectedPair = btn.dataset.pair;
        try { localStorage.setItem("so_pair", state.selectedPair); } catch (_) {}
        // re-render active state immediately
        grid.querySelectorAll(".so-chip").forEach((b) => {
          const isAct = b.dataset.pair === state.selectedPair;
          b.classList.toggle("active", isAct);
          b.setAttribute("aria-selected", String(isAct));
        });
        // immediate refresh
        refreshDetail();
        refreshLivePrice();
      });
    });
  }

  // ── Verdict block ────────────────────────────────────────────────
  function shortVerdict(verdict) {
    if (!verdict) return "ЖДЁМ";
    const m = {
      "КУПИТЬ": "КУПИТЬ",
      "ПРОДАТЬ": "ПРОДАТЬ",
      "СКОРЕЕ КУПИТЬ": "СК.КУПИТЬ",
      "СКОРЕЕ ПРОДАТЬ": "СК.ПРОДАТЬ",
      "ОЖИДАНИЕ": "ЖДЁМ",
    };
    return m[verdict] || verdict;
  }

  function renderVerdict(d) {
    const v = (d && d.verdict) || {};
    const sec = document.getElementById("so-verdict-section");
    const text = document.getElementById("so-verdict-text");
    const reason = document.getElementById("so-verdict-reason");
    const strength = document.getElementById("so-verdict-strength");

    sec.dataset.color = v.verdict_color || "gray";
    text.textContent = v.verdict || "—";

    const strengthMap = {
      strong: "сильный сигнал",
      medium: "умеренный сигнал",
      wait:   "ждём",
    };
    strength.textContent = strengthMap[v.verdict_strength] || "—";
    strength.className = "so-verdict-strength " + (v.verdict_strength || "");

    reason.textContent = v.reason_ru || "—";

    document.getElementById("so-stat-agree").textContent =
      v.institutional_sources_agree != null
        ? `${v.institutional_sources_agree} из ${v.institutional_sources_total} (${fmtPct(v.agreement_pct, 0)})`
        : "—";
    document.getElementById("so-stat-balance").textContent = fmtPct(v.favorite_balance_pct, 0);
    const favTxt =
      v.favorite_side === "buyers" ? "покупатели"
      : v.favorite_side === "sellers" ? "продавцы"
      : "нейтрал";
    document.getElementById("so-stat-favorite").textContent = favTxt;
    document.getElementById("so-stat-hours").textContent = fmtHours(v.hours_to_midnight_utc5);
    document.getElementById("so-stat-target").textContent =
      v.target_by_midnight != null
        ? `${fmtPrice(v.target_by_midnight)} (${fmtPips(v.target_pips_to_midnight)})`
        : "—";
    document.getElementById("so-stat-noreturn").textContent =
      v.no_return_level != null
        ? `${fmtPrice(v.no_return_level)} (${fmtPips(v.no_return_pips)})`
        : "—";

    // Buyers vs sellers bar
    const bp = Number((v && v.buyers_pct) || 50);
    const sp = Number((v && v.sellers_pct) || 50);
    const buy = document.getElementById("so-bs-buy");
    const sell = document.getElementById("so-bs-sell");
    buy.style.width  = bp + "%";
    sell.style.width = sp + "%";
    buy.textContent  = bp.toFixed(0) + "% покуп.";
    sell.textContent = sp.toFixed(0) + "% прод.";

    // Sources list
    const list = document.getElementById("so-sources-list");
    const srcs = (v && v.sources) || [];
    list.innerHTML = srcs.map((s) => {
      const sideCls = s.side === "UP" ? "up" : s.side === "DOWN" ? "down" : "none";
      const sideTxt = s.side === "UP" ? "↑" : s.side === "DOWN" ? "↓" : "·";
      const tagCls = s.kind === "institutional" ? "inst" : "retail";
      const tagTxt = s.kind === "institutional" ? `INST ×${s.weight}` : `Розница ×${s.weight}`;
      return `<li>
        <span class="so-src-tag ${tagCls}">${tagTxt}</span>
        <span class="so-src-side ${sideCls}">${sideTxt}</span>
        <span>${escapeHtml(s.label || "—")}</span>
      </li>`;
    }).join("") || "<li>—</li>";
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  // ── Stakan (Volume Profile) ──────────────────────────────────────
  function renderOrderBook(d) {
    const body = document.getElementById("so-ob-body");
    const vp = (d && d.volume_profile) || {};
    const buckets = (vp.buckets || []).slice();
    if (!buckets.length) {
      body.innerHTML = `<div class="so-empty">нет данных volume profile</div>`;
      return;
    }
    const cur = Number(vp.current_price || (d && d.current_price) || 0);
    const poc = Number(vp.poc || 0);
    const vah = Number(vp.vah || 0);
    const val = Number(vp.val || 0);
    const maxW = Math.max(...buckets.map((b) => Number(b.weight_pct || 0)), 1);
    // отрисовываем сверху вниз: сначала верхние (выше) цены — потом ниже
    buckets.sort((a, b) => Number(b.price || 0) - Number(a.price || 0));
    const rows = [];
    let curInserted = false;
    const eq = (a, b) => Math.abs(a - b) < 1e-9;
    for (const b of buckets) {
      const price = Number(b.price || 0);
      // вставить «текущую цену» перед первой бакетой ниже cur
      if (!curInserted && price < cur) {
        rows.push(
          `<div class="so-ob-row cur">
            <span class="so-ob-price">${fmtPrice(cur)}</span>
            <span class="so-ob-bar" style="width:100%"></span>
            <span class="so-ob-vol">текущая</span>
          </div>`
        );
        curInserted = true;
      }
      const w = Number(b.weight_pct || 0);
      const pct = (w / maxW) * 100;
      const cls = [
        "so-ob-row",
        price > cur ? "above" : "below",
        eq(price, poc) ? "poc" : "",
        eq(price, vah) ? "vah" : "",
        eq(price, val) ? "val" : "",
      ].filter(Boolean).join(" ");
      rows.push(
        `<div class="${cls}">
          <span class="so-ob-price">${fmtPrice(price)}</span>
          <span class="so-ob-bar" style="width:${pct.toFixed(1)}%"></span>
          <span class="so-ob-vol">${w.toFixed(1)}%</span>
        </div>`
      );
    }
    if (!curInserted && cur > 0) {
      rows.push(
        `<div class="so-ob-row cur">
          <span class="so-ob-price">${fmtPrice(cur)}</span>
          <span class="so-ob-bar" style="width:100%"></span>
          <span class="so-ob-vol">текущая</span>
        </div>`
      );
    }
    body.innerHTML = rows.join("");
    // автоскролл к текущей цене
    const curRow = body.querySelector(".so-ob-row.cur");
    if (curRow && curRow.scrollIntoView) {
      try { curRow.scrollIntoView({ block: "center", behavior: "instant" }); } catch (_) {}
    }
  }

  // ── Big players ──────────────────────────────────────────────────
  function renderBigPlayers(d) {
    const list = document.getElementById("so-bp-list");
    const bp = ((d && d.volume_profile) || {}).big_players || [];
    if (!bp.length) {
      list.innerHTML = `<div class="so-empty">нет институциональных уровней</div>`;
      return;
    }
    const cur = Number(((d && d.volume_profile) || {}).current_price || (d && d.current_price) || 0);
    bp.sort((a, b) => Number(b.weight_pct || 0) - Number(a.weight_pct || 0));
    list.innerHTML = bp.slice(0, 12).map((b) => {
      const price = Number(b.price || 0);
      const kind = b.kind || (price < cur ? "support" : "resistance");
      const w = Number(b.weight_pct || 0);
      const dist = cur > 0 ? Math.abs(price - cur) : 0;
      const pip = b.pair && b.pair.endsWith("JPY") ? 0.01 : 0.0001;
      const pips = (dist / pip).toFixed(0);
      return `<div class="so-bp-row ${kind}">
        <div class="so-bp-price">${fmtPrice(price)} <span class="so-bp-kind">${kind === "support" ? "поддержка" : "сопротивление"}</span></div>
        <div class="so-bp-weight">${w.toFixed(1)}%</div>
        <div class="so-bp-weight">${pips} p</div>
      </div>`;
    }).join("");
  }

  // ── Pair bar ─────────────────────────────────────────────────────
  function renderPairBar(d) {
    document.getElementById("so-pair-name").textContent = state.selectedPair;
    document.getElementById("so-pair-price").textContent = fmtPrice(d && d.current_price);
    document.getElementById("so-pair-session").textContent =
      `сессия ${(d && d.current_session) || "—"}`;
    document.getElementById("so-pair-asof").textContent =
      d && d.forecast_as_of ? `forecast ${new Date(d.forecast_as_of).toUTCString().split(" ")[4]} UTC` : "forecast —";
  }

  // ── Refresh detail ───────────────────────────────────────────────
  async function refreshDetail() {
    setStatus("обновляю стакан…", "warn");
    const d = await jget(`/api/stakan-view/${state.selectedPair}`);
    if (!d) {
      setStatus("ошибка загрузки", "error");
      return;
    }
    state.detail = d;
    renderPairBar(d);
    renderVerdict(d);
    renderOrderBook(d);
    renderBigPlayers(d);
    // обновляем chip-вердикт текущей пары
    if (d.verdict) {
      state.pairVerdict.set(state.selectedPair, {
        short: shortVerdict(d.verdict.verdict),
        color: d.verdict.verdict_color || "gray",
      });
      // обновим только этот chip — остальные обновятся при следующем summary
      const chip = document.querySelector(`.so-chip[data-pair="${state.selectedPair}"] .so-chip-verdict`);
      if (chip) {
        chip.textContent = shortVerdict(d.verdict.verdict);
        chip.className = "so-chip-verdict " + (d.verdict.verdict_color || "gray");
      }
    }
    setStatus("на связи · обновляется каждые 10с", "ok");
  }

  // ── Refresh live price (5 sec) ───────────────────────────────────
  async function refreshLivePrice() {
    const r = await jget(`/api/live-price/${state.selectedPair}`);
    if (!r || !isFinite(r.price)) return;
    const cur = Number(r.price);
    const priceEl = document.getElementById("so-pair-price");
    const deltaEl = document.getElementById("so-pair-delta");
    if (priceEl) priceEl.textContent = fmtPrice(cur);
    const dpips = r.change_5m_pips;
    if (deltaEl && dpips != null) {
      const sign = dpips > 0 ? "+" : "";
      deltaEl.textContent = `${sign}${dpips} p / 5m`;
      deltaEl.style.color = dpips > 0 ? "#4afaa3" : dpips < 0 ? "#ff8090" : "#9aa9bf";
    }
    if (state.lastPrice != null && cur !== state.lastPrice) {
      // small flash on price change
      priceEl.style.transition = "color 0.15s";
      priceEl.style.color = cur > state.lastPrice ? "#4afaa3" : "#ff8090";
      setTimeout(() => { priceEl.style.color = "#ffffff"; }, 350);
    }
    state.lastPrice = cur;
  }

  // ── Refresh summary (every 30s — для chip-grid) ──────────────────
  async function refreshSummary() {
    const s = await jget(`/api/stakan-view`);
    if (!s) return;
    state.summary = s;
    renderPicker();
  }

  // ── Boot ─────────────────────────────────────────────────────────
  try {
    const saved = localStorage.getItem("so_pair");
    if (saved && PAIRS_28.includes(saved)) state.selectedPair = saved;
  } catch (_) {}

  document.getElementById("so-pair-name").textContent = state.selectedPair;
  document.getElementById("so-build").textContent = "build " + new Date().toISOString().slice(0, 10);

  refreshSummary().then(refreshDetail);
  refreshLivePrice();

  state.summaryTimer = setInterval(refreshSummary, SUMMARY_REFRESH_MS);
  state.detailTimer = setInterval(refreshDetail, DETAIL_REFRESH_MS);
  state.priceTimer = setInterval(refreshLivePrice, PRICE_REFRESH_MS);
})();
