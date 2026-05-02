/**
 * static-mode fetch shim.
 *
 * The same intent.js / app.js are used on the FastAPI backend AND on this
 * static CDN mirror. On the backend they hit /api/* endpoints. On the static
 * mirror those endpoints don't exist — we bake the responses as static JSON
 * files at build time and rewrite all /api/X[?q] requests to ./api/X.json.
 *
 * Mapping rules (matches what was generated under static_build/api):
 *   /api/forecasts                                  → ./api/forecasts.json
 *   /api/market-radar                               → ./api/market-radar.json
 *   /api/intent-bars/EURUSD?interval=15m&n=90       → ./api/intent-bars/EURUSD.json
 *   /api/forecast/EURUSD                            → ./api/forecast/EURUSD.json
 *   /api/stakan/open-trades                         → ./api/stakan/open-trades.json
 *   /api/stability-forecast?hours_ahead=1           → ./api/stability-forecast/1.json
 *   /api/stability-forecast?hours_ahead=6           → ./api/stability-forecast/6.json
 *   /api/stability-forecast?hours_ahead=24          → ./api/stability-forecast/24.json
 *   /api/meta-strategy/log?limit=20                 → ./api/meta-strategy/log.json
 */
(function () {
  const _origFetch = window.fetch.bind(window);

  function rewrite(url) {
    let u;
    try {
      u = new URL(url, window.location.href);
    } catch (e) {
      return url;
    }
    if (!u.pathname.startsWith("/api/")) return url;

    // Special-cases for query-style endpoints
    if (u.pathname === "/api/stability-forecast") {
      const h = u.searchParams.get("hours_ahead") || "1";
      return new URL("./api/stability-forecast/" + h + ".json", window.location.href).toString();
    }
    if (u.pathname === "/api/meta-strategy/log") {
      return new URL("./api/meta-strategy/log.json", window.location.href).toString();
    }

    // Generic: strip query, append .json
    return new URL("." + u.pathname + ".json", window.location.href).toString();
  }

  window.fetch = function (input, init) {
    let req = input;
    if (typeof input === "string") {
      req = rewrite(input);
    } else if (input && input.url) {
      req = new Request(rewrite(input.url), input);
    }
    // strip credentials — static origin doesn't need them, and Chrome refuses
    // to send credentials cross-origin without explicit CORS allow-credentials.
    const opts = Object.assign({}, init || {}, { credentials: "omit" });
    return _origFetch(req, opts);
  };
})();
