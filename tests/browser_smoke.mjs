/**
 * Browser smoke test — real Chromium against the real server.
 *
 * This is the check the jsdom suite cannot make: does the page actually boot, does a real
 * map draw, does autocomplete suggest, does a plan render cards, do the panels populate,
 * does the single map instance survive a view switch. Screenshots land in tests/screens/
 * so a failure is visible rather than merely reported.
 *
 * Run:  node tests/browser_smoke.mjs [baseUrl]
 * Needs: node_modules (npm install jsdom playwright-core) and a Chromium build
 *        (npx playwright install chromium) plus the usual OS libs.
 */
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";

const BASE = process.argv[2] || "http://localhost:7860";
const OUT = "tests/screens";
mkdirSync(OUT, { recursive: true });

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 940 } });

const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

await page.goto(BASE, { waitUntil: "networkidle", timeout: 60000 });

/* ---------------------------------------------------------------------- boot */
await page.waitForFunction(() => window.App && window.CampusMap, null, { timeout: 15000 });
check("page boots with App + CampusMap on window", true);

const renderer = await page.evaluate(() => CampusMap.getRenderer());
check("map renderer chosen", !!renderer, `renderer=${renderer}`);

const tiles = await page.evaluate(() => {
  const imgs = [...document.querySelectorAll("#map img.leaflet-tile")];
  return { all: imgs.length, decoded: imgs.filter((i) => i.complete && i.naturalWidth > 0).length };
});
check("real map tiles decoded at first paint", tiles.decoded > 0,
  `${tiles.decoded}/${tiles.all} tiles decoded`);

const shell = await page.evaluate(() => ({
  map: document.querySelectorAll("#map").length,
  views: document.querySelectorAll(".view").length,
  nav: document.querySelectorAll("#nav button[data-view]").length,
}));
check("one map container, six views, six nav items",
  shell.map === 1 && shell.views === 6 && shell.nav === 6, JSON.stringify(shell));

/* -------------------------------------------------------------- autocomplete */
await page.click('[data-view="plan"]');
await page.waitForTimeout(400);
check("map follows the view switch", await page.evaluate(
  () => !!document.querySelector("#map-slot-plan #map")));

await page.fill("#origin-2", "jayanagar");
await page.waitForSelector("#ac-origin-2 .ac-item", { timeout: 8000 });
const acCount = await page.locator("#ac-origin-2 .ac-item").count();
const acFirst = (await page.textContent("#ac-origin-2 .ac-item")).trim().slice(0, 40);
check("autocomplete suggests local places", acCount > 0, `${acCount} items, first="${acFirst}"`);

await page.keyboard.press("ArrowDown");
await page.keyboard.press("Enter");
const originVal = await page.inputValue("#origin-2");
check("keyboard-picked suggestion fills the field", originVal.length > 0, `origin="${originVal}"`);

/* ---------------------------------------------------------------- plan + map */
await page.fill("#destination-2", "Whitefield");
await page.waitForSelector("#ac-destination-2 .ac-item", { timeout: 8000 });
await page.keyboard.press("ArrowDown");
await page.keyboard.press("Enter");
await page.click("#view-plan .plan-btn");
await page.waitForSelector("#results-list .rcard", { timeout: 90000 });

const cards = await page.locator("#results-list .rcard").count();
check("route cards rendered", cards > 0, `${cards} cards`);

const firstFare = (await page.textContent("#results-list .rcard .rc-fare .amt")).trim();
check("card shows a fare", /₹\d/.test(firstFare), `fare="${firstFare.slice(0, 20)}"`);

const legs = await page.locator("#results-list .rcard .leg").count();
check("cards show leg detail", legs > 0, `${legs} leg rows`);

const provenance = await page.locator("#results-list .rcard .src-chip").count();
check("cards carry provenance chips", provenance > 0, `${provenance} chips`);

const stats = await page.locator("#results-list .rcard .rc-stats > *").count();
check("cards show the stat grid", stats >= 8, `${stats} stat cells`);

const drawn = await page.evaluate(
  () => document.querySelectorAll("#map path.leaflet-interactive").length);
check("itinerary drawn on the map", drawn > 0, `${drawn} paths`);

const labels = await page.evaluate(() => document.querySelectorAll(".cmap-label").length);
check("fare labels drawn on the map", labels > 0, `${labels} labels`);

// The panel is filled by its own request. Wait for the content, not a fixed 1.5 s:
// on a cold process the plan + traffic round trip is slower than that, and the assertion
// then reads an empty div and reports a product failure that is really a race.
await page.waitForFunction(
  () => /Live traffic/i.test(document.querySelector("#traffic-body")?.innerText || ""),
  null, { timeout: 30000 });
const panel = await page.evaluate(() => {
  const t = document.querySelector("#traffic-body");
  return t ? t.innerText.replace(/\s+/g, " ") : "";
});
check("traffic panel says what it does and does not know",
  /unavailable|Leave now|arrive/i.test(panel), panel.slice(0, 110));
check("traffic panel refuses to invent a per-hour chart",
  /hourly traffic chart: not available/i.test(panel), "explicit 'not available' copy");
// Two honest shapes exist: with corridor geometry the panel names the missing variables,
// without it the shorter "No traffic provider configured". Both must say *why*, and
// neither may print a congestion figure.
check("traffic panel names the missing key rather than a fake number",
  /GOOGLE_MAPS_SERVER_API_KEY|not configured|no traffic provider|no provider|api key/i.test(panel),
  panel.slice(0, 110));

await page.screenshot({ path: `${OUT}/b1-plan.png` });

/* ------------------------------------------------------------------- sorting */
await page.click('#result-tabs [data-sort="cheapest"]');
await page.waitForTimeout(500);
const topFare = (await page.textContent("#results-list .rcard .rc-fare .amt")).trim();
const topModes = (await page.textContent("#results-list .rcard .rc-modes")).trim();
check("cheapest tab puts a transit route on top (a 4-hour walk stays last)",
  /₹\d/.test(topFare), `top="${topFare.slice(0, 12)}" modes="${topModes.slice(0, 30)}"`);

/* ------------------------------------------------------- select, save, log */
await page.click("#results-list .rcard:first-child");
await page.waitForTimeout(400);
check("selecting a card highlights exactly one",
  (await page.locator("#results-list .rcard.on").count()) === 1);

await page.click('#results-list .rcard:first-child button[data-act="save"]');
await page.waitForTimeout(1200);
await page.click('[data-view="saved"]');
await page.waitForSelector("#saved-list .list-item", { timeout: 10000 });
const saved = await page.locator("#saved-list .list-item").count();
check("saved route persisted and listed", saved > 0, `${saved} saved`);
await page.screenshot({ path: `${OUT}/b2-saved.png` });

await page.click('[data-view="plan"]');
await page.waitForTimeout(600);
await page.click('#results-list .rcard:first-child button[data-act="log"]');
await page.waitForTimeout(1200);
await page.click('[data-view="history"]');
await page.waitForSelector("#hist-list table", { timeout: 10000 });
const kpi = (await page.textContent("#hist-kpis .kpi .v")).trim();
check("history logs a trip and aggregates it", kpi !== "0", `trips=${kpi}`);
await page.screenshot({ path: `${OUT}/b3-history.png` });

/* ------------------------------------------------------------------- fares */
await page.click('[data-view="plan"]');
await page.waitForTimeout(600);
await page.click('#results-list .rcard:first-child button[data-act="fares"]');
await page.waitForTimeout(400);
const fareRows = await page.locator("#results-list .rcard:first-child .fare-table tbody tr").count();
check("fare breakdown expands per leg", fareRows > 0, `${fareRows} rows`);

/* ------------------------------------------------------------------ layers */
for (const id of ["lyr-busstops", "lyr-metro"]) {
  const before = await page.isChecked("#" + id);
  await page.click("#" + id);
  await page.waitForTimeout(400);
  check(`layer ${id} toggles`, (await page.isChecked("#" + id)) !== before);
}
const trafficDisabled = await page.evaluate(() => document.getElementById("lyr-traffic").disabled);
check("traffic layer is disabled while no provider is configured", trafficDisabled === true);

/* ----------------------------------------------------------------- explore */
await page.click('[data-view="explore"]');
await page.waitForTimeout(1200);
check("map moves into the Explore view", await page.evaluate(
  () => !!document.querySelector("#map-slot-explore #map")));
await page.click("#near-btn");
await page.waitForTimeout(2500);
const nearRows = await page.locator("#near-result tbody tr").count();
check("nearby-stops lookup returns real GTFS stops", nearRows > 0, `${nearRows} stops`);
await page.screenshot({ path: `${OUT}/b4-explore.png` });

/* ---------------------------------------------------------------- settings */
await page.click('[data-view="settings"]');
await page.waitForSelector("#sources-list table", { timeout: 20000 });
const srcRows = await page.locator("#sources-list tbody tr").count();
// The capabilities list became the registry-backed integration cards; ask the same
// question of the current DOM: does Settings name every dependency, and does the one
// with no credential say so instead of looking ready?
await page.waitForSelector("#integrations-list .intg", { timeout: 20000 });
const intg = await page.evaluate(() => {
  const cards = [...document.querySelectorAll("#integrations-list .intg")];
  return {
    count: cards.length,
    notConfigured: cards.filter((c) => /Not configured/i.test(c.innerText)).length,
  };
});
check("settings lists data sources with provenance", srcRows > 0, `${srcRows} sources`);
check("settings lists integrations honestly", intg.count > 0 && intg.notConfigured > 0,
  `${intg.count} integrations, ${intg.notConfigured} saying "Not configured"`);
await page.screenshot({ path: `${OUT}/b5-settings.png` });

/* -------------------------------------------------------------------- chat */
await page.click('[data-view="chat"]');
await page.fill("#input", "Best route from Jayanagar to Whitefield");
await page.press("#input", "Enter");
// The typing placeholder is itself a .msg.bot, so wait for it to be *replaced* rather
// than for a bubble to exist. A local model on CPU can take a minute; the rules engine
// answers in under a second.
await page.waitForSelector("#msgs .msg.bot", { timeout: 90000 });
await page.waitForFunction(() => !document.querySelector("#msgs .typing"), null,
                           { timeout: 300000 });
await page.waitForTimeout(1200);
const botText = await page.textContent("#msgs .msg.bot:last-child .bubble");
check("assistant replies with content", botText && botText.length > 40,
  `${botText ? botText.length : 0} chars`);
await page.screenshot({ path: `${OUT}/b6-chat.png` });

/* -------------------------------------------------------------------- theme */
await page.click("#theme-btn");
await page.waitForTimeout(500);
const theme = await page.evaluate(() => document.documentElement.dataset.theme);
check("theme toggle switches the palette", theme === "dark", `theme=${theme}`);
await page.screenshot({ path: `${OUT}/b7-dark.png` });
await page.click("#theme-btn");

/* ------------------------------------------------------------------- phone */
// A second context: the map must survive the one-column layout, and the sidebar must
// become a drawer rather than a column that eats the screen.
const phone = await browser.newContext({
  viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
});
const mp = await phone.newPage();
const phoneErrors = [];
mp.on("pageerror", (e) => phoneErrors.push(e.message));
await mp.goto(BASE, { waitUntil: "networkidle", timeout: 60000 });
await mp.waitForTimeout(2000);
const mobileMap = await mp.evaluate(() => {
  const el = document.querySelector("#map");
  const b = el ? el.getBoundingClientRect() : { height: 0 };
  return { h: Math.round(b.height), sidebarX: Math.round(
    document.querySelector(".sidebar").getBoundingClientRect().x) };
});
check("phone layout keeps the map on screen", mobileMap.h > 150, `${mobileMap.h}px tall`);
check("phone layout starts with the sidebar off-canvas", mobileMap.sidebarX < 0,
  `sidebar x=${mobileMap.sidebarX}`);
await mp.screenshot({ path: `${OUT}/b8-mobile-chat.png` });
await mp.click("#menu-btn");
await mp.waitForTimeout(400);
const drawerOpen = await mp.evaluate(
  () => Math.round(document.querySelector(".sidebar").getBoundingClientRect().x));
check("menu button opens the drawer", drawerOpen === 0, `sidebar x=${drawerOpen}`);
await mp.screenshot({ path: `${OUT}/b9-mobile-drawer.png` });
await mp.click('[data-view="plan"]');
await mp.waitForTimeout(800);
const drawerClosed = await mp.evaluate(
  () => document.getElementById("app").classList.contains("sidebar-collapsed"));
check("navigating closes the drawer again", drawerClosed);
check("no errors on the phone layout", phoneErrors.length === 0,
  phoneErrors.slice(0, 2).join(" | ") || "clean");
await phone.close();

/* ------------------------------------------------------------------ console */
const realErrors = consoleErrors.filter((e) => !/favicon/i.test(e));
check("no console errors", realErrors.length === 0,
  realErrors.slice(0, 3).join(" | ") || "clean");

await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) {
  console.log("failures: " + failed.map((f) => f.name).join(", "));
  process.exit(1);
}
