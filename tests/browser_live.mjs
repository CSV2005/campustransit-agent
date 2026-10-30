/**
 * Browser check for the live bus layer.
 *
 * Two runs are needed because the two answers are the point:
 *
 *   node tests/browser_live.mjs http://localhost:7862   # a feed is configured
 *   node tests/browser_live.mjs http://localhost:7860   # no authorised feed exists
 *
 * Against a feed, the layer must draw exactly the vehicles the server returned, colour the
 * stale one differently, and label the chip "Live". Without one, the layer must draw
 * nothing and say "Unavailable" with the reason -- the failure this test exists to prevent
 * is a map that looks alive because a URL was typed into .env.
 *
 * The feed used here is a local fixture served over HTTP (`tests/fixtures/gtfs_rt`), never
 * a real BMTC feed: a pass proves the plumbing, not that Bengaluru buses are tracked.
 */
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";

const BASE = process.argv[2] || "http://localhost:7860";
const OUT = "tests/screens";
mkdirSync(OUT, { recursive: true });

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

await page.goto(BASE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForFunction(() => window.API && window.CampusMap, null, { timeout: 20000 });

// What the server says, fetched independently of the UI.
const api = await page.evaluate(async () => {
  const r = await fetch("/api/transit/vehicle-positions");
  return { status: r.status, body: await r.json() };
});
const expected = api.body.available ? api.body.count : 0;

// Switch the layer on.
await page.check("#lyr-buses");
await page.waitForSelector("#live-strip:not([hidden])", { timeout: 15000 });
await page.waitForFunction(
  () => document.getElementById("live-chip").dataset.state !== "loading",
  null, { timeout: 20000 });

const view = await page.evaluate(() => ({
  state: document.getElementById("live-chip").dataset.state,
  chip: document.getElementById("live-chip").textContent.trim(),
  text: document.getElementById("live-text").textContent.trim(),
  markers: document.querySelectorAll(".cmap-bus").length,
  stale: document.querySelectorAll(".cmap-bus.stale").length,
}));

check("the strip becomes visible when the layer is switched on", true);
check("marker count matches the feed the server returned",
  view.markers === expected, `${view.markers} markers, server count ${expected}`);

if (api.body.available) {
  check("chip says Live while timestamps are fresh", view.state === "live", view.chip);
  check("stale vehicles are drawn differently", view.stale === api.body.stale_count,
    `${view.stale} stale markers, feed had ${api.body.stale_count}`);
  check("the strip names the source and the refresh interval",
    /updates every \d+s/.test(view.text), view.text);
  check("attribution is not the word 'live' on its own",
    !/^live$/i.test(view.text));
} else {
  check("chip says Unavailable with no feed", view.state === "unavailable", view.chip);
  check("no markers are invented", view.markers === 0, `${view.markers} markers`);
  check("the reason is shown, not a blank strip", view.text.length > 40, view.text.slice(0, 90));
  check("the reason mentions the unconfigured variable or the missing feed",
    /GTFS_RT_VEHICLE_URL|authorised/i.test(view.text));
}

// Capture the ON state before switching off: the strip is the part a screenshot proves.
await page.screenshot({ path: `${OUT}/live-buses-${api.body.available ? "feed" : "nofeed"}.png` });

// Switching it off must remove the markers and hide the strip.
await page.uncheck("#lyr-buses");
await page.waitForTimeout(300);
const off = await page.evaluate(() => ({
  markers: document.querySelectorAll(".cmap-bus").length,
  hidden: document.getElementById("live-strip").hidden,
}));
check("switching the layer off clears the markers", off.markers === 0);
check("switching the layer off hides the strip", off.hidden === true);

check("no console errors", errors.length === 0, errors.slice(0, 2).join(" | "));


await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
