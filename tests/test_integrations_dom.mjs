/**
 * Settings -> Integrations, rendered against a real DOM (jsdom).
 *
 * The registry is the one thing on that page that must never overstate itself, so these
 * checks are about honesty as much as markup: a provider with only an env var must not read
 * "ready", a billable test must announce that it is billable, and a missing variable must be
 * named on the card rather than hidden in a log.
 *
 *   npm install jsdom && node tests/test_integrations_dom.mjs
 */
import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const SRC = path.join(ROOT, "web/static/js/views.js");

const dom = new JSDOM(`<!doctype html><body>
  <div id="integrations-list"></div><div id="toast"></div>
</body>`, { pretendToBeVisual: true, runScripts: "outside-only" });

const w = dom.window;
w.eval(fs.readFileSync(SRC, "utf8"));
const Views = w.Views;
if (!Views) throw new Error("Views not exposed on window");

const fixture = {
  note: "A provider is ready only when its last check passed.",
  summary: { total: 4, ready: 1, needs_setup: 2, problems: 1, unsupported: 0 },
  road_routing: { road_order: ["google_routes"], traffic_aware_providers: ["google_routes"] },
  integrations: [
    {
      id: "google_places", name: "Google Places API", provider: "Google Maps Platform",
      purpose: "Destination autocomplete", category: "geocoding",
      state: "configured_untested", state_label: "Configured, untested", healthy: false,
      reason: "credentials present, never tested - run the connection test",
      enabled: true, configured: true, credential: "server", optional: true, billable: true,
      env_vars: [{ name: "GOOGLE_MAPS_SERVER_API_KEY", set: true, required: true }],
      setup_url: "https://console.cloud.google.com/google/maps-apis/api-list",
      docs_url: "https://developers.google.com/maps/documentation/places/web-service",
      restrictions: ["Autocomplete requests are billed per 1,000."],
      notes: "Merged with the local index.",
      test: { kind: "billable", supported: true, requires_confirmation: true,
              warning: "This test makes one real request." },
      last_check: null,
    },
    {
      id: "tomtom", name: "TomTom Routing", provider: "TomTom",
      purpose: "Optional road routing", category: "routing",
      state: "not_configured", state_label: "Not configured", healthy: false,
      reason: "set TOMTOM_API_KEY", enabled: true, configured: false,
      credential: "server", optional: true, billable: true,
      env_vars: [{ name: "TOMTOM_API_KEY", set: false, required: true }],
      setup_url: "https://developer.tomtom.com/user/register", docs_url: "",
      restrictions: [], notes: "", test: { kind: "billable", supported: true,
        requires_confirmation: true, warning: "This test makes one real request." },
      last_check: null,
    },
    {
      id: "ollama", name: "Ollama (local LLM)", provider: "Ollama",
      purpose: "Natural-language answers", category: "llm",
      state: "ready", state_label: "Ready", healthy: true,
      reason: "qwen2.5:0.5b reachable", enabled: true, configured: true,
      credential: "none", optional: true, billable: false,
      env_vars: [], setup_url: "https://ollama.com/download", docs_url: "",
      restrictions: [], notes: "", test: { kind: "free", supported: true,
        requires_confirmation: false, warning: "Uses a free endpoint." },
      last_check: { state: "ready", state_label: "Ready", ok: true, detail: "model reachable",
                    latency_ms: 41, checked_at: new Date().toISOString(), stale: false },
    },
    {
      id: "gtfs_realtime", name: "GTFS-Realtime", provider: "BMTC",
      purpose: "Live vehicle positions", category: "transit",
      state: "unsupported", state_label: "Unsupported here", healthy: false,
      reason: "no authorised feed exists", enabled: false, configured: false,
      credential: "server", optional: true, billable: false, env_vars: [],
      setup_url: "https://bmtc.karnataka.gov.in/english", docs_url: "",
      restrictions: ["Will not scrape a feed."], notes: "",
      test: { kind: "none", supported: false, requires_confirmation: false, warning: "" },
      last_check: null,
    },
  ],
};

const container = w.document.getElementById("integrations-list");
Views.renderIntegrations(container, fixture, { toast: () => {}, reload: () => {} });

const checks = [];
const q = (sel) => w.document.querySelector(sel);
const qa = (sel) => Array.from(w.document.querySelectorAll(sel));
const t = (name, fn) => {
  try { fn(); checks.push([true, name]); }
  catch (e) { checks.push([false, name + " -> " + e.message]); }
};

t("renders one card per integration", () => {
  if (qa(".intg").length !== 4) throw new Error(`got ${qa(".intg").length}`);
});

t("summary counts come from the server", () => {
  const txt = q(".intg-sum").textContent;
  if (!/1\s*ready/.test(txt)) throw new Error(txt);
  if (!/2\s*need setup/.test(txt)) throw new Error(txt);
});

t("an untested provider never reads 'ready'", () => {
  const card = q('.intg[data-id="google_places"]');
  const chip = card.querySelector(".src-chip").textContent.trim();
  if (chip !== "Configured, untested") throw new Error(chip);
  if (card.querySelector(".src-chip").classList.contains("ok")) throw new Error("green chip");
});

t("a billable test is labelled billable", () => {
  const btn = q('.intg[data-id="google_places"] [data-act="test"]');
  if (!/billable/i.test(btn.textContent)) throw new Error(btn.textContent);
});

t("a free local test is not labelled billable", () => {
  const btn = q('.intg[data-id="ollama"] [data-act="test"]');
  if (/billable/i.test(btn.textContent)) throw new Error(btn.textContent);
});

t("a missing variable is named on the card", () => {
  const card = q('.intg[data-id="tomtom"]');
  if (!card.textContent.includes("TOMTOM_API_KEY")) throw new Error("variable not shown");
  if (!card.querySelector(".env.unset")) throw new Error("no 'missing' marker");
});

t("every card links to the official provider page", () => {
  for (const card of qa(".intg")) {
    const a = card.querySelector('a[href^="https://"]');
    if (!a) throw new Error(card.dataset.id + " has no setup link");
    if (a.getAttribute("rel") !== "noopener") throw new Error("missing rel=noopener");
  }
});

t("each card has an enable switch", () => {
  if (qa('.intg [data-act="toggle"]').length !== 4) throw new Error("toggle missing");
});

t("unsupported offers no test button", () => {
  const card = q('.intg[data-id="gtfs_realtime"]');
  if (card.querySelector('[data-act="test"]')) throw new Error("test button on unsupported");
});

t("a test is disabled when its variable is missing", () => {
  const btn = q('.intg[data-id="tomtom"] [data-act="test"]');
  if (!btn.disabled) throw new Error("button is clickable without a credential");
  if (!/Set the variable/i.test(btn.getAttribute("title") || "")) throw new Error("no title");
});

t("last check time and latency are shown", () => {
  const txt = q('.intg[data-id="ollama"] .intg-last').textContent;
  if (!/41 ms/.test(txt)) throw new Error(txt);
});

t("restrictions render in the details block", () => {
  const txt = q('.intg[data-id="google_places"] .intg-more').textContent;
  if (!/billed per 1,000/.test(txt)) throw new Error(txt);
});

t("road routing chain is displayed", () => {
  if (!q(".intg-chain").textContent.includes("google_routes")) throw new Error("no chain");
});

t("no key-shaped string reaches the DOM", () => {
  const html = container.innerHTML;
  if (/AIza[0-9A-Za-z_-]{20,}/.test(html)) throw new Error("key-shaped value in markup");
});

let failed = 0;
for (const [ok, name] of checks) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) failed++;
}
console.log(`\n${checks.length - failed}/${checks.length} passed`);
process.exit(failed ? 1 : 0);
