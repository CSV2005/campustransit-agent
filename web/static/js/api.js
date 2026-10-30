/**
 * API — a thin, typed-ish wrapper over the v1 endpoints.
 *
 * Every call resolves to {ok, data, error, status}. Nothing throws at the call site, so a
 * network failure surfaces as a message in the UI instead of a blank panel. The server's
 * own error detail is surfaced verbatim: if Pydantic rejected a field, the user sees which.
 *
 * TIMEOUTS ARE PER ENDPOINT. A single 45 s cap is what made chat look "down": a local
 * model on a CPU takes 30-90 s, so the browser aborted while the server was still working,
 * and each retry queued behind the previous generation. The chat timeout now starts above
 * the server's own budget (/api/chat reports `timeout_hint_ms`, default budget + 60 s), so
 * the server always gets the chance to answer, including its rules-engine fallback.
 */
const API = (() => {
  "use strict";

  // Milliseconds. Chat is long because local generation is slow; everything else answers
  // from SQLite or a cached call in well under a second, so a 20 s cap means "something is
  // actually wrong" rather than "the model is thinking".
  const TIMEOUTS = {
    chat: 210000,        // replaced by the server's own hint once /api/chat/health answers
    plan: 30000,
    geocode: 15000,
    autocomplete: 10000,
    traffic: 25000,
    // Live positions: the server caches the feed for >= 15 s and answers from that cache,
    // so a request slower than this is a stalled upstream, not a slow query.
    live: 12000,
    default: 20000,
  };
  let chatTimeoutMs = TIMEOUTS.chat;

  function timeoutFor(path) {
    if (path.indexOf("/api/chat") === 0) return chatTimeoutMs;
    if (path.indexOf("/api/v1/routes/plan") === 0 || path.indexOf("/api/routes/plan") === 0) return TIMEOUTS.plan;
    if (path.indexOf("/api/locations") === 0) return TIMEOUTS.geocode;
    if (path.indexOf("/places/autocomplete") >= 0) return TIMEOUTS.autocomplete;
    if (path.indexOf("/traffic") >= 0) return TIMEOUTS.traffic;
    if (path.indexOf("/api/transit/vehicle-positions") === 0
        || path.indexOf("/api/transit/realtime-status") === 0) return TIMEOUTS.live;
    return TIMEOUTS.default;
  }

  function setChatTimeout(ms) {
    const n = Number(ms);
    if (isFinite(n) && n > 10000) chatTimeoutMs = n;
  }

  async function call(path, opts = {}) {
    const ctrl = new AbortController();
    const started = Date.now();
    const limit = opts.timeout || timeoutFor(path);
    const timer = setTimeout(() => ctrl.abort(), limit);
    try {
      const res = await fetch(path, {
        method: opts.method || "GET",
        headers: opts.body ? { "Content-Type": "application/json" } : undefined,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
        signal: ctrl.signal,
      });
      clearTimeout(timer);
      const text = await res.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
      if (!res.ok) {
        return { ok: false, status: res.status, data,
                 error: describe(res.status, data) };
      }
      return { ok: true, status: res.status, data, error: "",
               elapsed_ms: Date.now() - started };
    } catch (err) {
      clearTimeout(timer);
      const aborted = err && err.name === "AbortError";
      const waited = Math.round((Date.now() - started) / 1000);
      // Say how long we waited and what the limit was: "timed out" alone is unactionable.
      return { ok: false, status: 0, data: null, elapsed_ms: Date.now() - started,
               error: aborted
                 ? `Timed out after ${waited}s (limit ${Math.round(limit / 1000)}s). A local ` +
                   `model can take a while on CPU — the planner still answers instantly if ` +
                   `you ask a route or fare question.`
                 : "Could not reach the server." };
    }
  }

  /** Turn a FastAPI error body into one readable line. */
  function describe(status, data) {
    if (!data) return `HTTP ${status}`;
    if (Array.isArray(data.detail)) {          // Pydantic validation errors
      return data.detail.map((d) => {
        const field = (d.loc || []).filter((x) => x !== "body").join(".");
        return field ? `${field}: ${d.msg}` : d.msg;
      }).join("; ");
    }
    if (typeof data.detail === "string") return data.detail;
    if (data.detail && data.detail.error) return data.detail.error;
    if (data.error) return data.error;
    return `HTTP ${status}`;
  }

  const qs = (o) => Object.entries(o)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");

  return {
    describe,
    timeouts: TIMEOUTS,
    setChatTimeout,
    // Current chat cap in ms. Exposed so the UI (and a test) can show what it is waiting
    // for instead of guessing at it.
    chatTimeout: () => chatTimeoutMs,
    health: () => call("/api/health"),
    capabilities: () => call("/api/v1/capabilities"),
    integrations: () => call("/api/v1/integrations"),
    integration: (id) => call(`/api/v1/integrations/${encodeURIComponent(id)}`),
    // 409 (not an error the UI hides): the body says the probe costs money.
    testIntegration: (id, confirm) => call(`/api/v1/integrations/${encodeURIComponent(id)}/test`,
      { method: "POST", body: { confirm_billable: !!confirm }, timeout: 30000 }),
    setIntegrationEnabled: (id, enabled) => call(`/api/v1/integrations/${encodeURIComponent(id)}/enabled`,
      { method: "PUT", body: { enabled: !!enabled } }),
    configHealth: () => call("/api/v1/config/health"),
    dataSources: (probe) => call("/api/v1/data-sources?include_calls=false" + (probe ? "&probe=true" : "")),
    coverage: () => call("/api/v1/coverage"),
    objectives: () => call("/api/v1/routes/objectives"),

    autocomplete: (q, limit) => call(`/api/v1/places/autocomplete?${qs({ q, limit })}`),
    resolve: (q) => call(`/api/v1/places/resolve?${qs({ q })}`),

    plan: (body) => call("/api/v1/routes/plan", { method: "POST", body }),
    // Contract shape from the brief §11.
    planContract: (body) => call("/api/routes/plan", { method: "POST", body }),
    // Richer internal shape, used for per-leg geometry.
    planRich: (body) => call("/api/v1/routes/plan", { method: "POST", body }),
    geocode: (q) => call(`/api/locations/geocode?${qs({ q })}`),
    reverseGeocode: (lat, lng) => call(`/api/locations/reverse-geocode?${qs({ lat, lng })}`),
    nearbyStops: (lat, lng, radius) => call(`/api/transit/stops/nearby?${qs({ lat, lng, radius_km: radius })}`),
    // Live vehicle positions. 200 + available:false means "no authorised feed",
    // 502 means "configured but unreadable"; both are data, not exceptions.
    vehiclePositions: (o) => call(`/api/transit/vehicle-positions?${qs(o || {})}`),
    realtimeStatus: () => call("/api/transit/realtime-status"),
    stations: () => call("/api/transit/stations"),
    lines: () => call("/api/transit/lines"),
    ksrtc: () => call("/api/transit/ksrtc-routes"),
    getSaved: (id) => call(`/api/v1/saved-routes/${id}`),
    publicConfig: () => call("/api/config/public"),
    traffic: (origin, destination) => call(`/api/v1/traffic?${qs({ origin, destination })}`),
    compare: (origin, destination) => call(`/api/v1/routes/compare?${qs({ origin, destination })}`),

    saved: () => call("/api/v1/saved-routes"),
    saveRoute: (body) => call("/api/v1/saved-routes", { method: "POST", body }),
    deleteSaved: (id) => call(`/api/v1/saved-routes/${id}`, { method: "DELETE" }),

    history: () => call("/api/v1/history"),
    logHistory: (body) => call("/api/v1/history", { method: "POST", body }),
    clearHistory: () => call("/api/v1/history", { method: "DELETE" }),

    prefs: () => call("/api/v1/preferences"),
    savePrefs: (body) => call("/api/v1/preferences", { method: "PUT", body }),

    mapConfig: () => call("/api/map/config"),
    basemap: () => call("/api/map/basemap"),
    tileStats: () => call("/api/map/tiles/stats"),

    chat: (message, history) =>
      call("/api/chat", { method: "POST", body: { message, history: history || [] } }),
    chatHealth: async () => {
      const res = await call("/api/chat/health", { timeout: 8000 });
      // The server owns the budget; the client should not invent its own.
      if (res.ok && res.data && res.data.timeout_hint_ms) setChatTimeout(res.data.timeout_hint_ms);
      return res;
    },
    examples: () => call("/api/examples"),
  };
})();

window.API = API;
