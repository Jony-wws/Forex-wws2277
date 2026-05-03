/**
 * static-mode fetch shim — REAL-TIME EDITION (2026-05-03).
 *
 * The same intent.js / app.js are used on the FastAPI backend AND on this
 * static CDN mirror. On the backend they hit /api/* endpoints. On the static
 * mirror those endpoints don't exist as live routes — but we still want the
 * data to be **as fresh as possible**, not frozen at build time.
 *
 * Strategy:
 *   1. /api/market-status — ALWAYS recomputed client-side from the user's
 *      clock + DST-aware NY conversion. NEVER served from baked JSON, because
 *      that JSON freezes "is_open / countdown" at build time and lies to the
 *      user the moment the build is more than a few minutes old.
 *   2. Every other /api/X — try the LIVE Fly backend first (short timeout).
 *      Fall back to the baked ./api/X.json only if Fly is unreachable.
 *      The Fly host can be overridden via window.FX_LIVE_BACKEND (set on the
 *      page before this script loads). Default: https://fxinvestment.fly.dev
 *
 * Why this matters:
 *   The user's #1 complaint was "сайт пишет что рынок закрыт хотя рынок открыт"
 *   ("the site says market closed even though it is open"). That bug was
 *   caused entirely by serving a frozen market-status.json. Fix is to NEVER
 *   trust baked time-sensitive JSON for market-status, and to prefer live
 *   data for everything else.
 *
 * Mapping rules for the baked-JSON fallback (matches what
 * scripts/build_static_mirror.sh generates under static_build/api/):
 *   /api/forecasts                                → ./api/forecasts.json
 *   /api/market-radar                             → ./api/market-radar.json
 *   /api/intent-bars/EURUSD?interval=15m&n=90     → ./api/intent-bars/EURUSD.json
 *   /api/forecast/EURUSD                          → ./api/forecast/EURUSD.json
 *   /api/stakan/open-trades                       → ./api/stakan/open-trades.json
 *   /api/stability-forecast?hours_ahead=1         → ./api/stability-forecast/1.json
 *   /api/meta-strategy/log?limit=20               → ./api/meta-strategy/log.json
 */
(function () {
  const _origFetch = window.fetch.bind(window);

  // ─────────────────────────────────────────────────────────────────────────
  // CONFIG
  // ─────────────────────────────────────────────────────────────────────────
  // Live FastAPI backend (Fly.io). Override before this script loads via
  //   <script>window.FX_LIVE_BACKEND="https://your-host";</script>
  const LIVE_BACKEND =
    (typeof window.FX_LIVE_BACKEND === "string" && window.FX_LIVE_BACKEND) ||
    "https://fxinvestment.fly.dev";

  // Per-request timeout for the live-backend probe. Keep short so a dead Fly
  // doesn't tax the user — we'll fall through to the baked JSON anyway.
  const LIVE_TIMEOUT_MS = 4500;

  // Endpoints we never proxy live (purely synthesized client-side).
  const CLIENT_SIDE_ENDPOINTS = new Set(["/api/market-status"]);

  // ─────────────────────────────────────────────────────────────────────────
  // CLIENT-SIDE MARKET-STATUS (DST-aware, NY-anchored)
  // ─────────────────────────────────────────────────────────────────────────
  // Open  = Sunday 17:00 America/New_York  (= 21:00 UTC EDT, 22:00 UTC EST)
  // Close = Friday 17:00 America/New_York  (= 21:00 UTC EDT, 22:00 UTC EST)
  // Saturday = fully closed.
  // Mon..Thu = fully open.
  // Friday   = open until 17:00 NY-local.
  // Sunday   = open from  17:00 NY-local.
  function _nyParts(d) {
    // We need NY-local weekday + hour + minute + second + DST offset.
    const fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour12: false,
      weekday: "short",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    const parts = {};
    for (const p of fmt.formatToParts(d)) parts[p.type] = p.value;
    const wdMap = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
    return {
      weekday: wdMap[parts.weekday] ?? 0,
      year: +parts.year,
      month: +parts.month,
      day: +parts.day,
      hour: +parts.hour % 24,
      minute: +parts.minute,
      second: +parts.second,
    };
  }

  // Build a UTC Date corresponding to a specific NY-local weekday + hour
  // strictly AFTER `from`. Iterates day-by-day so DST transitions are handled
  // by the runtime's tz database (no manual offsets).
  function _nextNyAnchor(from, targetWeekday, targetHourNy) {
    let probe = new Date(from.getTime());
    for (let i = 0; i < 14; i++) {
      const ny = _nyParts(probe);
      if (ny.weekday === targetWeekday && ny.hour < targetHourNy) {
        // Snap to the NY anchor on this same NY-day. Build a UTC moment that
        // lands exactly on targetHourNy:00:00 NY-local using a UTC offset
        // computed via toISOString round-trip.
        return _nyAnchorToUtc(ny, targetHourNy);
      }
      if (
        ny.weekday === targetWeekday &&
        ny.hour >= targetHourNy &&
        i === 0
      ) {
        // Already past today's anchor — advance one day.
        probe = new Date(probe.getTime() + 24 * 3600 * 1000);
        continue;
      }
      // Not the right weekday yet — advance one day.
      probe = new Date(probe.getTime() + 24 * 3600 * 1000);
      // Normalise the probe time to noon NY to avoid skipping a target weekday
      // due to DST gap-day arithmetic.
    }
    return probe; // safety
  }

  // Convert {year, month, day} (NY-local) + targetHourNy → UTC Date instance.
  function _nyAnchorToUtc(nyParts, targetHourNy) {
    // Trick: format a candidate UTC moment back through NY tz and bisect on
    // the diff. Two bisection passes are enough because NY offset is
    // ±5 hours from UTC, plus DST ±1.
    let candUtc = new Date(
      Date.UTC(
        nyParts.year,
        nyParts.month - 1,
        nyParts.day,
        targetHourNy,
        0,
        0,
        0
      )
    );
    for (let pass = 0; pass < 3; pass++) {
      const back = _nyParts(candUtc);
      const diffH =
        targetHourNy - back.hour - (back.minute / 60) - (back.second / 3600);
      if (Math.abs(diffH) < 1 / 60) break;
      candUtc = new Date(candUtc.getTime() + Math.round(diffH * 3600 * 1000));
    }
    return candUtc;
  }

  function _formatUtcPlus5(d) {
    // Match the Python format: "YYYY-MM-DD HH:MM (UTC+5)"
    const s = new Date(d.getTime() + 5 * 3600 * 1000)
      .toISOString()
      .slice(0, 16)
      .replace("T", " ");
    return s + " (UTC+5)";
  }

  function _formatNy(d) {
    const np = _nyParts(d);
    const pad2 = (n) => String(n).padStart(2, "0");
    return `${np.year}-${pad2(np.month)}-${pad2(np.day)} ${pad2(np.hour)}:${pad2(np.minute)} (NY)`;
  }

  function _sessionLabel(ny) {
    // Same buckets as teamagent/config.py SESSIONS_UTC, but expressed against
    // NY-local hours: Tokyo 19-04 NY (00-09 UTC), London 03-12 NY (08-17 UTC),
    // NY 08-17 NY (13-22 UTC), Sydney 17-02 NY (22-07 UTC).
    const h = ny.hour;
    if (h >= 8 && h < 17) return "NY";
    if (h >= 3 && h < 12) return "London";
    if (h >= 19 || h < 4) return "Tokyo";
    return "Sydney";
  }

  function _clientMarketStatus(now) {
    const t = now instanceof Date ? now : new Date();
    const ny = _nyParts(t);
    let isOpen;
    if (ny.weekday === 5) isOpen = false; // Saturday
    else if (ny.weekday === 4) isOpen = ny.hour < 17; // Friday until 17:00 NY
    else if (ny.weekday === 6) isOpen = ny.hour >= 17; // Sunday from 17:00 NY
    else isOpen = true; // Mon-Thu

    let nextEvent, nextEventUtc, secondsUntilOpen = 0, secondsUntilClose = 0;
    if (isOpen) {
      nextEvent = "close";
      nextEventUtc = _nextNyAnchor(t, 4, 17); // Friday 17:00 NY
      secondsUntilClose = Math.max(
        0,
        Math.floor((nextEventUtc.getTime() - t.getTime()) / 1000)
      );
    } else {
      nextEvent = "open";
      nextEventUtc = _nextNyAnchor(t, 6, 17); // Sunday 17:00 NY
      secondsUntilOpen = Math.max(
        0,
        Math.floor((nextEventUtc.getTime() - t.getTime()) / 1000)
      );
    }

    // Max safe expiry — whole hours that still settle ≥15 min before close.
    let maxSafeExpiry = 0;
    if (isOpen) {
      const buf = 15 * 60;
      maxSafeExpiry = Math.max(
        0,
        Math.floor((secondsUntilClose - buf) / 3600)
      );
    }

    return {
      as_of_utc: t.toISOString(),
      as_of_utc_plus_5: _formatUtcPlus5(t),
      is_open: isOpen,
      status_emoji: isOpen ? "🟢" : "🔴",
      status_text: isOpen ? "ОТКРЫТ" : "ЗАКРЫТ",
      session: isOpen ? _sessionLabel(ny) : "Closed",
      seconds_until_close: secondsUntilClose,
      seconds_until_open: secondsUntilOpen,
      next_event: nextEvent,
      next_event_utc: nextEventUtc.toISOString(),
      next_event_utc_plus_5: _formatUtcPlus5(nextEventUtc),
      next_event_ny: _formatNy(nextEventUtc),
      next_event_text_ru: isOpen ? "закроется через" : "откроется через",
      max_safe_expiry_h: maxSafeExpiry,
      _source: "client_side_shim",
    };
  }

  // Expose so app.js / intent.js can call it directly when they want a
  // guaranteed-fresh status without going through fetch().
  window.FX_clientMarketStatus = _clientMarketStatus;

  function _jsonResponse(obj) {
    return new Response(JSON.stringify(obj), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  // ─────────────────────────────────────────────────────────────────────────
  // BAKED-JSON FALLBACK MAPPING
  // ─────────────────────────────────────────────────────────────────────────
  function _toBakedUrl(u) {
    if (u.pathname === "/api/stability-forecast") {
      const h = u.searchParams.get("hours_ahead") || "1";
      return new URL(
        "./api/stability-forecast/" + h + ".json",
        window.location.href
      ).toString();
    }
    if (u.pathname === "/api/meta-strategy/log") {
      return new URL(
        "./api/meta-strategy/log.json",
        window.location.href
      ).toString();
    }
    return new URL(
      "." + u.pathname + ".json",
      window.location.href
    ).toString();
  }

  // ─────────────────────────────────────────────────────────────────────────
  // LIVE-BACKEND FETCH (with timeout)
  // ─────────────────────────────────────────────────────────────────────────
  async function _liveFetch(u) {
    const live = LIVE_BACKEND.replace(/\/+$/, "") + u.pathname + u.search;
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), LIVE_TIMEOUT_MS);
    try {
      const r = await _origFetch(live, {
        method: "GET",
        credentials: "omit",
        cache: "no-store",
        signal: ctrl.signal,
      });
      clearTimeout(tid);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r;
    } finally {
      clearTimeout(tid);
    }
  }

  async function _bakedFetch(u) {
    return _origFetch(_toBakedUrl(u), { credentials: "omit" });
  }

  // ─────────────────────────────────────────────────────────────────────────
  // INTERCEPTOR
  // ─────────────────────────────────────────────────────────────────────────
  window.fetch = async function (input, init) {
    let url = typeof input === "string" ? input : input && input.url;
    let u;
    try {
      u = new URL(url, window.location.href);
    } catch (e) {
      // Not a URL we understand — pass through unchanged.
      return _origFetch(input, init);
    }

    if (!u.pathname.startsWith("/api/")) {
      // Static asset etc — pass through unchanged.
      return _origFetch(input, init);
    }

    // 1) Pure client-side endpoints (market-status).
    if (CLIENT_SIDE_ENDPOINTS.has(u.pathname)) {
      return _jsonResponse(_clientMarketStatus());
    }

    // 2) Try the live Fly backend first.
    try {
      const r = await _liveFetch(u);
      // Tag the response so dashboards can show "live" indicator.
      const text = await r.text();
      return new Response(text, {
        status: 200,
        headers: {
          "Content-Type": r.headers.get("Content-Type") || "application/json",
          "X-FX-Source": "live",
        },
      });
    } catch (_e) {
      // 3) Fall back to baked JSON.
      try {
        const r2 = await _bakedFetch(u);
        if (!r2.ok) throw new Error("baked HTTP " + r2.status);
        const text = await r2.text();
        return new Response(text, {
          status: 200,
          headers: {
            "Content-Type":
              r2.headers.get("Content-Type") || "application/json",
            "X-FX-Source": "baked",
          },
        });
      } catch (_e2) {
        // Last resort: empty 503-ish JSON so callers see {error}.
        return _jsonResponse({
          error: "live-and-baked-both-failed",
          path: u.pathname,
          live_backend: LIVE_BACKEND,
        });
      }
    }
  };
})();
