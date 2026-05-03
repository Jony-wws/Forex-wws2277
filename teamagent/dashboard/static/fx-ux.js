/* FX INVESTMENT — Live UX layer
 *
 * Adds (all client-side, no external deps, no mp3 files):
 *   1. Welcome splash with random Russian greeting + soft C-E-G chime
 *      (Web Audio API). Shown once per session, on first paint.
 *   2. Sound preferences: 🔊 / 🔇 toggle in top-right. Persisted in
 *      localStorage. Default OFF; the splash asks once.
 *   3. Sound palette (all generated via OscillatorNode, 0 ext. assets):
 *        - welcome chime  (C-E-G major arpeggio, 1.5 s, soft)
 *        - click tick     (sine 440 Hz, 30 ms)
 *        - tab ting       (sine 880 Hz, 80 ms)
 *        - modal open     (rising sine 440 → 660, 120 ms)
 *        - modal close    (falling sine 660 → 440, 120 ms)
 *        - GO ding        (C-E-G-B major7, 600 ms) when a final-signal
 *                         transitions WAIT → GO.
 *   4. Russification helpers — map English microstructure values to
 *      human Russian (FVG / Wyckoff / order_block kinds, etc.).
 *   5. Tooltip helper — clicking any [data-explain] element opens a
 *      lightweight bubble with a plain-Russian explanation.
 *   6. Number animator — `flashValue(el, newValue)` smoothly tweens
 *      a numeric element AND flashes green/red on direction.
 *
 * Loaded from intent.html + index.html via <script src="fx-ux.js">.
 */
(function () {
  "use strict";

  // ─── 0. localStorage helpers ───────────────────────────────────────
  const LS = window.localStorage;
  const SOUND_KEY = "fx_sound_on";
  const SPLASH_KEY = "fx_splash_seen";
  function soundEnabled() {
    return LS.getItem(SOUND_KEY) === "1";
  }
  function setSoundEnabled(on) {
    LS.setItem(SOUND_KEY, on ? "1" : "0");
    updateSoundButton();
  }

  // ─── 1. Audio engine — pure Web Audio API, no external assets ──────
  let _ctx = null;
  function audioCtx() {
    if (_ctx) return _ctx;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    _ctx = new Ctor();
    return _ctx;
  }
  function unlock() {
    const c = audioCtx();
    if (!c) return;
    if (c.state === "suspended") c.resume().catch(() => {});
  }
  function envelope(gain, attackS, holdS, releaseS, peakGain, when) {
    gain.gain.cancelScheduledValues(when);
    gain.gain.setValueAtTime(0.00001, when);
    gain.gain.exponentialRampToValueAtTime(peakGain, when + attackS);
    gain.gain.setValueAtTime(peakGain, when + attackS + holdS);
    gain.gain.exponentialRampToValueAtTime(0.00001, when + attackS + holdS + releaseS);
  }
  function tone(freq, when, dur, peak, type = "sine") {
    if (!soundEnabled()) return;
    const c = audioCtx();
    if (!c) return;
    const t0 = when || c.currentTime;
    const o = c.createOscillator();
    o.type = type;
    o.frequency.setValueAtTime(freq, t0);
    const g = c.createGain();
    o.connect(g).connect(c.destination);
    envelope(g, 0.005, Math.max(0.001, dur * 0.5), Math.max(0.05, dur * 0.45), peak, t0);
    o.start(t0);
    o.stop(t0 + dur + 0.05);
  }
  function chord(freqs, when, dur, peakEach, type = "sine") {
    freqs.forEach((f) => tone(f, when, dur, peakEach, type));
  }
  function arpeggio(freqs, startWhen, perDur, peak, type = "sine") {
    if (!soundEnabled()) return;
    const c = audioCtx();
    if (!c) return;
    let t = startWhen || c.currentTime;
    freqs.forEach((f, i) => {
      tone(f, t, perDur * (i === freqs.length - 1 ? 2.5 : 1.0), peak, type);
      t += perDur * 0.7;
    });
  }
  function glide(fromHz, toHz, dur, peak, type = "sine") {
    if (!soundEnabled()) return;
    const c = audioCtx();
    if (!c) return;
    const t0 = c.currentTime;
    const o = c.createOscillator();
    o.type = type;
    o.frequency.setValueAtTime(fromHz, t0);
    o.frequency.exponentialRampToValueAtTime(toHz, t0 + dur);
    const g = c.createGain();
    o.connect(g).connect(c.destination);
    envelope(g, 0.005, dur * 0.4, dur * 0.5, peak, t0);
    o.start(t0);
    o.stop(t0 + dur + 0.05);
  }

  const SOUND = {
    welcome: () => {
      // C5 E5 G5 + sustained C-E-G chord — soft welcome chime (Mac-startup vibe)
      const c = audioCtx();
      if (!c) return;
      const t = c.currentTime + 0.05;
      arpeggio([523.25, 659.25, 783.99], t, 0.12, 0.10, "sine");
      // soft pad chord 0.5s after arpeggio
      chord([261.63, 329.63, 392.00], t + 0.55, 1.4, 0.05, "sine");
    },
    click: () => tone(620, undefined, 0.06, 0.06, "sine"),
    tabSwitch: () => tone(880, undefined, 0.10, 0.07, "sine"),
    modalOpen: () => glide(440, 660, 0.13, 0.07, "sine"),
    modalClose: () => glide(660, 440, 0.13, 0.06, "sine"),
    goDing: () => {
      // C5-E5-G5-B5 (Cmaj7) — celebratory but soft
      const c = audioCtx();
      if (!c) return;
      const t = c.currentTime;
      arpeggio([523.25, 659.25, 783.99, 987.77], t, 0.10, 0.09, "sine");
      chord([523.25, 659.25, 783.99, 987.77], t + 0.45, 1.0, 0.04, "sine");
    },
    error: () => glide(330, 220, 0.20, 0.08, "triangle"),
  };

  // ─── 2. Welcome splash ─────────────────────────────────────────────
  const WELCOME_TEXTS = [
    "Добро пожаловать, инвестор",
    "FX INVESTMENT — твой персональный аналитик",
    "Готовлю прогноз для тебя…",
    "Анализирую 28 валютных пар",
    "Твоя система готова к работе",
    "Привет! Считаю рынок прямо сейчас",
    "Запускаю торговую панель",
  ];

  function pickGreeting() {
    return WELCOME_TEXTS[Math.floor(Math.random() * WELCOME_TEXTS.length)];
  }

  function showSplash() {
    if (document.getElementById("fx-splash")) return;
    const seen = LS.getItem(SPLASH_KEY);
    const splash = document.createElement("div");
    splash.id = "fx-splash";
    splash.className = "fx-splash" + (seen ? " fx-splash-quick" : "");
    splash.innerHTML = `
      <div class="fx-splash-inner">
        <div class="fx-splash-logo" aria-hidden="true">FX</div>
        <div class="fx-splash-title">FX INVESTMENT</div>
        <div class="fx-splash-greet">${pickGreeting()}</div>
        ${seen ? "" : `
        <div class="fx-splash-sound-prompt">
          <button class="fx-splash-btn fx-splash-btn-yes" type="button">🔊 Включить приятные звуки</button>
          <button class="fx-splash-btn fx-splash-btn-no" type="button">Без звуков</button>
        </div>`}
      </div>
    `;
    document.body.appendChild(splash);

    if (!seen) {
      const yes = splash.querySelector(".fx-splash-btn-yes");
      const no = splash.querySelector(".fx-splash-btn-no");
      yes?.addEventListener("click", () => {
        unlock();
        setSoundEnabled(true);
        LS.setItem(SPLASH_KEY, "1");
        SOUND.welcome();
        setTimeout(closeSplash, 1800);
      });
      no?.addEventListener("click", () => {
        setSoundEnabled(false);
        LS.setItem(SPLASH_KEY, "1");
        setTimeout(closeSplash, 350);
      });
      // auto-dismiss after 8s if user didn't click
      setTimeout(() => {
        if (!LS.getItem(SPLASH_KEY)) {
          LS.setItem(SPLASH_KEY, "1");
          closeSplash();
        }
      }, 8000);
    } else {
      // Already seen — show splash briefly and dismiss
      if (soundEnabled()) {
        unlock();
        SOUND.welcome();
      }
      setTimeout(closeSplash, 1400);
    }
  }
  function closeSplash() {
    const splash = document.getElementById("fx-splash");
    if (!splash) return;
    splash.classList.add("fx-splash-fade");
    setTimeout(() => splash.remove(), 600);
  }

  // ─── 3. Sound toggle button ────────────────────────────────────────
  function ensureSoundButton() {
    if (document.getElementById("fx-sound-toggle")) return;
    const btn = document.createElement("button");
    btn.id = "fx-sound-toggle";
    btn.className = "fx-sound-toggle";
    btn.type = "button";
    btn.title = "Звуки сайта";
    btn.addEventListener("click", () => {
      const willOn = !soundEnabled();
      setSoundEnabled(willOn);
      if (willOn) {
        unlock();
        SOUND.click();
      }
    });
    document.body.appendChild(btn);
    updateSoundButton();
  }
  function updateSoundButton() {
    const btn = document.getElementById("fx-sound-toggle");
    if (!btn) return;
    const on = soundEnabled();
    btn.textContent = on ? "🔊" : "🔇";
    btn.classList.toggle("fx-sound-on", on);
    btn.classList.toggle("fx-sound-off", !on);
    btn.title = on ? "Звуки включены — нажми чтобы выключить"
                   : "Звуки выключены — нажми чтобы включить";
  }

  // ─── 4. Russification of microstructure values ─────────────────────
  // Used by intent.js and index.html when rendering the "Market Intent"
  // deep-dive modal. Returns a human Russian explanation for each
  // canonical English token from market_microstructure.py.
  const I18N = {
    // wyckoff stage
    "UNKNOWN": "не определена",
    "ACCUMULATION": "Накопление (большие игроки покупают на дне)",
    "DISTRIBUTION": "Распределение (большие игроки продают на пике)",
    "MARKUP": "Восходящий тренд (бычий импульс)",
    "MARKDOWN": "Нисходящий тренд (медвежий импульс)",
    "RANGE": "Диапазон (рынок без чёткого направления)",
    // hurst regime
    "TRENDING": "Трендовый рынок (направление сохранится)",
    "MEAN_REVERTING": "Возврат к средней (откаты)",
    "RANDOM": "Случайное блуждание (без преимущества)",
    // delta bias
    "BUY_DOMINANT": "Покупатели сильнее",
    "SELL_DOMINANT": "Продавцы сильнее",
    "NEUTRAL": "Нейтрально",
    // OB / FVG kinds
    "bullish_ob": "бычий ордер-блок (зона покупателей)",
    "bearish_ob": "медвежий ордер-блок (зона продавцов)",
    "bullish_fvg": "бычий разрыв (FVG вверх)",
    "bearish_fvg": "медвежий разрыв (FVG вниз)",
    // sweep events
    "buy_side_liquidity_taken": "снята покупательская ликвидность",
    "sell_side_liquidity_taken": "снята продавцовая ликвидность",
    "expect_up": "ждём движение вверх",
    "expect_down": "ждём движение вниз",
    // trade sides
    "BUY": "покупка",
    "SELL": "продажа",
    // verdict states
    "GO": "ОТКРЫВАЙ",
    "GO_CAUTION": "С ОСТОРОЖНОСТЬЮ",
    "WAIT": "ЖДИ",
  };
  function ru(token) {
    if (token == null) return "—";
    const s = String(token);
    return I18N[s] || s.replace(/_/g, " ");
  }

  // Translate "buy_side_liquidity_taken → expect_up" and similar phrases.
  function ruPhrase(s) {
    if (!s) return "—";
    return String(s)
      .split(/\s*→\s*/)
      .map((p) => ru(p.trim()))
      .join(" → ");
  }

  // Translate inner_facts/outer_view summary lines from market_microstructure.
  // Server returns English-ish strings like "Cumulative Delta +12% → BUY_DOMINANT".
  function ruSummaryLine(line) {
    if (!line) return "";
    return String(line)
      .replace(/Cumulative Delta/g, "Кумулятивная дельта")
      .replace(/Last OB:/g, "Последний ордер-блок:")
      .replace(/Sweep:/g, "Снятие ликвидности:")
      .replace(/Wyckoff:/g, "Wyckoff-стадия:")
      .replace(/FVG/g, "FVG (разрыв стоимости)")
      .replace(/Hurst H=/g, "Хёрст H = ")
      .replace(/Footprint POC/g, "Footprint POC (точка управления)")
      .replace(/Whale:/g, "Кит:")
      .replace(/bull_pct/g, "доля покупок")
      .replace(/(\b)(BUY_DOMINANT|SELL_DOMINANT|NEUTRAL|TRENDING|MEAN_REVERTING|RANDOM|UNKNOWN|ACCUMULATION|DISTRIBUTION|MARKUP|MARKDOWN|RANGE|bullish_ob|bearish_ob|bullish_fvg|bearish_fvg|buy_side_liquidity_taken|sell_side_liquidity_taken|expect_up|expect_down)(\b)/g,
        (m, p, tok) => p + ru(tok));
  }

  // Plain-Russian explanations shown in click-tooltips for technical terms
  const TERM_EXPLAIN = {
    "RSI": "Индекс силы (RSI 1H). Выше 70 — перекуплено, ниже 30 — перепродано. " +
           "Используется для разворотных сетапов.",
    "ATR": "Средний истинный диапазон (ATR%). Показывает текущую волатильность " +
           "— сколько пара двигается за единицу времени. Меньше 0.05% — тихо, " +
           "больше 0.15% — активно.",
    "OFI": "Order-Flow Imbalance — дисбаланс ордеров. Положительный → больше " +
           "покупателей, отрицательный → больше продавцов. Совпадение OFI с " +
           "вашим направлением подтверждает сделку.",
    "BBP": "Bollinger %B — где цена внутри полос Боллинджера. Около 100% — у " +
           "верхней границы, около 0% — у нижней. Используется для пробойных " +
           "и разворотных стратегий.",
    "CEI": "Crowd-Energy Index. Сила толпы / суммарный momentum. Выше 60 — " +
           "толпа давит на покупку, ниже 40 — давит на продажу.",
    "Stakan": "Стакан-стратегия (book-pressure). Анализирует объём заявок и " +
              "скорость их съедания.",
    "Daily": "Дейли-стратегия — внутридневной свинг с горизонтом 1-5 часов.",
    "FVG": "Fair-Value Gap (разрыв справедливой стоимости) — пропуск между " +
           "свечами без перекрытия. Цена обычно возвращается, чтобы его закрыть.",
    "Order Block": "Ордер-блок — последняя противоположная свеча перед " +
                   "сильным импульсом. Зона где «крупняк» открывал позиции, " +
                   "часто работает как поддержка/сопротивление.",
    "Wyckoff": "Стадии Уайкоффа: Накопление → Markup (рост) → Распределение → " +
               "Markdown (падение). Помогает понять «фазу» рынка.",
    "Hurst": "Экспонента Хёрста (H). H>0.55 → тренд продолжится; H<0.45 → " +
             "цена откатится; ~0.5 → случайно.",
    "Cumulative Delta": "Кумулятивная дельта = сумма (объём покупателей − " +
                        "объём продавцов). Растёт → покупатели сильнее.",
    "Liquidity Sweep": "Снятие ликвидности — резкое движение через скопление " +
                       "стопов с быстрым возвратом. Часто разворотный сигнал.",
    "Radar Score": "Сводный score рыночного радара (-100…+100). Положительный " +
                   "— покупка, отрицательный — продажа. Учитывает " +
                   "20+ индикаторов и микроструктуру.",
    "ADX": "ADX (Average Directional Index). Сила тренда. Выше 25 — тренд " +
           "сильный; ниже 15 — рынок плоский (флэт).",
    "Ichimoku": "Облако Ишимоку. Цена выше облака — бычий рынок; ниже — " +
                "медвежий. Помогает увидеть глобальное направление за секунду.",
    "Macd": "MACD = разность быстрой и медленной EMA. Гистограмма выше нуля " +
            "— импульс вверх; ниже — вниз.",
    "Stochastic": "Стохастик. %K и %D в диапазоне 0-100. Выше 80 — " +
                  "перекупленность, ниже 20 — перепроданность.",
    "Williams": "Williams %R. Зеркальный стохастику: -20…0 — перекупленность, " +
                "-100…-80 — перепроданность.",
    "ensemble": "Ансамбль — несколько ТОП-вариантов стратегии голосуют за " +
                "сторону сделки (BUY/SELL). Открываемся только при кворуме.",
    "WR": "Win-Rate — процент прибыльных сделок из последних N. Цель ≥ 70%.",
    "Probability": "Вероятность успеха сделки от 50% до 92%. Считается из " +
                   "набора 20+ индикаторов и реальной 30-дневной истории.",
    "Expiry": "Время до автоматического закрытия (1-5 ч). Дольше — больше " +
              "места тренду; короче — меньше шум.",
    "PnL": "Profit-and-Loss — итоговый финансовый результат в $.",
    "Drawdown": "Просадка — максимальное падение equity от пика. Чем меньше " +
                "— тем стабильнее.",
    "Order Flow Imbalance": "См. OFI.",
    "POC": "Point-of-Control — ценовой уровень с максимальным объёмом за " +
           "период. Сильный магнит для цены.",
    "VAL": "Value Area Low — нижняя граница зоны 70% объёма.",
    "VAH": "Value Area High — верхняя граница зоны 70% объёма.",
  };

  function explain(term) {
    if (!term) return null;
    // прямое совпадение
    if (TERM_EXPLAIN[term]) return TERM_EXPLAIN[term];
    // case-insensitive поиск
    const norm = term.trim();
    for (const k of Object.keys(TERM_EXPLAIN)) {
      if (k.toLowerCase() === norm.toLowerCase()) return TERM_EXPLAIN[k];
    }
    return null;
  }

  // ─── 5. Lightweight tooltip ────────────────────────────────────────
  let _tipEl = null;
  function ensureTip() {
    if (_tipEl) return _tipEl;
    _tipEl = document.createElement("div");
    _tipEl.className = "fx-tip-bubble";
    _tipEl.style.display = "none";
    _tipEl.addEventListener("click", () => hideTip());
    document.body.appendChild(_tipEl);
    document.addEventListener("click", (e) => {
      if (!_tipEl) return;
      if (e.target.closest(".fx-tip-bubble")) return;
      if (e.target.closest("[data-explain]")) return;
      hideTip();
    });
    return _tipEl;
  }
  function showTip(el, text) {
    if (!text) return;
    const tip = ensureTip();
    tip.textContent = text;
    tip.style.display = "block";
    const r = el.getBoundingClientRect();
    const tipW = Math.min(320, window.innerWidth - 24);
    tip.style.maxWidth = tipW + "px";
    let x = Math.max(8, Math.min(window.innerWidth - tipW - 8, r.left + r.width / 2 - tipW / 2));
    let y = Math.max(8, r.bottom + 6);
    // if tooltip would go offscreen below, place above
    if (y + 120 > window.innerHeight) y = Math.max(8, r.top - 130);
    tip.style.left = x + "px";
    tip.style.top = (y + window.scrollY) + "px";
  }
  function hideTip() {
    if (_tipEl) _tipEl.style.display = "none";
  }
  // wire delegation: any element with data-explain="<term>" pops the tooltip
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-explain]");
    if (!t) return;
    e.stopPropagation();
    const term = t.getAttribute("data-explain");
    const text = explain(term) || ru(term);
    if (text) {
      SOUND.click();
      showTip(t, text);
    }
  });

  // ─── 6. Generic click sound on .fx-clickable ───────────────────────
  document.addEventListener("click", (e) => {
    const c = e.target.closest("button, .fx-tab, .fx-chip, .fx-clickable, .fs-card-row");
    if (!c) return;
    SOUND.click();
  }, true);

  // ─── 7. Modal-open / modal-close detection (intent dialog) ─────────
  const _origShow = HTMLDialogElement.prototype.showModal;
  if (_origShow) {
    HTMLDialogElement.prototype.showModal = function () {
      const r = _origShow.apply(this, arguments);
      try { SOUND.modalOpen(); } catch (_) {}
      return r;
    };
    const _origClose = HTMLDialogElement.prototype.close;
    HTMLDialogElement.prototype.close = function () {
      try { SOUND.modalClose(); } catch (_) {}
      return _origClose.apply(this, arguments);
    };
  }

  // ─── 8. Number animator + change-flash ─────────────────────────────
  function flashValue(el, newText, opts) {
    if (!el) return;
    const old = el.textContent.trim();
    if (old === String(newText).trim()) return;
    el.textContent = newText;
    el.classList.remove("fx-flash-up", "fx-flash-dn");
    void el.offsetWidth;
    const dir = opts && opts.dir;
    el.classList.add(dir === "down" ? "fx-flash-dn" : "fx-flash-up");
    setTimeout(() => {
      el.classList.remove("fx-flash-up", "fx-flash-dn");
    }, 900);
  }

  // ─── 9. Welcome on DOMContentLoaded ────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    ensureSoundButton();
    showSplash();
  });

  // expose API for intent.js / app.js to consume
  window.FX_UX = {
    sound: SOUND,
    soundEnabled,
    setSoundEnabled,
    unlock,
    ru,
    ruPhrase,
    ruSummaryLine,
    explain,
    flashValue,
    showTip,
    hideTip,
  };
})();
