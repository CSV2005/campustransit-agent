/**
 * Runs web/static/js/map.js against a real DOM (jsdom) and asserts the map contract:
 * created once, overlays replaced not appended, reset preserves the base layer.
 *
 *   npm install jsdom && node tests/test_map_dom.mjs
 */
import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const SRC = path.join(ROOT, "web/static/js/map.js");

const dom = new JSDOM(`<!doctype html><body>
  <div id="map" style="width:600px;height:400px"></div>
  <div id="map-fallback" class="hidden"></div>
  <div id="map-note"></div><div id="map-attribution"></div><div id="map-badge"></div>
</body>`, { pretendToBeVisual: true, runScripts: "outside-only" });

const w = dom.window;
w.ResizeObserver = class { observe() {} disconnect() {} };
w.fetch = async (url) => {
  if (url === "/api/map/config") return { json: async () => ({
    renderer: "svg", browser_key: "", default_center: { lat: 12.9716, lng: 77.5946 },
    default_zoom: 11, attribution: "GTFS map" }) };
  if (url === "/api/map/basemap") return { json: async () => ({ metro_lines: [
      { id: "PURPLE", name: "Purple", status: "operational", stations: [
        { lat: 12.97, lng: 77.57 }, { lat: 12.98, lng: 77.62 }, { lat: 13.01, lng: 77.70 }] },
      { id: "PINK", name: "Pink", status: "under_construction", stations: [
        { lat: 12.90, lng: 77.57 }, { lat: 13.02, lng: 77.62 }] }],
      bus_stops: [{ lat: 12.95, lng: 77.58 }, { lat: 12.96, lng: 77.60 }] }) };
  throw new Error("unexpected fetch " + url);
};

w.eval(fs.readFileSync(SRC, "utf8"));
const CampusMap = w.CampusMap;
if (!CampusMap) throw new Error("CampusMap not exposed on window");

await CampusMap.init();

const route = (o) => ({ ok: true,
  origin: { name: "A", lat: o[0], lng: o[1] },
  destination: { name: "B", lat: o[2], lng: o[3] },
  itineraries: [
    { rank: 1, total_price_inr: 22, minutes_in_vehicle: 10, transfers: 1,
      verification: "VERIFIED_PRIMARY", mode_sequence: ["bus", "metro"],
      legs: [{ mode: "bus", label: "213-M", from: "A", to: "M",
               geometry: [[o[0], o[1]], [12.95, 77.58], [12.96, 77.60]] },
             { mode: "metro", label: "Green", from: "M", to: "B",
               geometry: [[12.96, 77.60], [12.97, 77.61], [o[2], o[3]]] }] },
    { rank: 2, total_price_inr: 36, minutes_in_vehicle: 12, transfers: 1,
      verification: "VERIFIED_PRIMARY", mode_sequence: ["bus", "metro"],
      legs: [{ mode: "bus", label: "215-C", from: "A", to: "M",
               geometry: [[o[0], o[1]], [12.955, 77.585], [12.96, 77.60]] },
             { mode: "metro", label: "Green", from: "M", to: "B",
               geometry: [[12.96, 77.60], [12.99, 77.66], [o[2], o[3]]] }] }] });

const checks = [];
const q = (sel) => w.document.querySelector(sel);
const qa = (sel) => w.document.querySelectorAll(sel);

checks.push(["map svg created", !!q("#map svg")]);
checks.push(["four render layers", qa("#map svg > g").length === 4]);
checks.push(["svg renderer selected", CampusMap.getRenderer() === "svg"]);
checks.push(["operational + pending metro lines drawn", qa(".cmap-layer-metro polyline").length === 2]);
checks.push(["pending line dashed", !!q("polyline[stroke-dasharray]")]);
checks.push(["bus stop dots drawn", qa(".cmap-layer-base circle").length === 2]);

CampusMap.drawItineraries(route([12.92, 77.58, 12.98, 77.62]));
const routesAfterFirst = qa(".cmap-layer-routes polyline").length;
const statsFirst = CampusMap.stats();
checks.push(["route polylines drawn", routesAfterFirst > 1]);
checks.push(["two markers", qa(".cmap-layer-markers circle").length === 2]);
checks.push(["state holds both itineraries", statsFirst.itineraries === 2 && statsFirst.markers === 2]);

CampusMap.drawItineraries(route([12.90, 77.55, 13.00, 77.65]));
checks.push(["overlays replaced, not appended",
             qa(".cmap-layer-routes polyline").length === routesAfterFirst]);

CampusMap.highlightRoute(1);
checks.push(["highlight marks active", CampusMap.stats().active === 1]);
checks.push(["inactive route dimmed",
             [...qa(".cmap-layer-routes polyline")].some(
               (p) => p.getAttribute("stroke-opacity") === "0.22")]);

CampusMap.resetToOverview();
checks.push(["reset clears route state", CampusMap.stats().itineraries === 0]);
checks.push(["reset clears route dom", qa(".cmap-layer-routes polyline").length === 0]);
checks.push(["reset keeps metro basemap", qa(".cmap-layer-metro polyline").length === 2]);

CampusMap.toggleTraffic(true);
checks.push(["traffic toggle is honest without a key",
             q("#map-note").textContent.includes("will not fake")]);

let fails = 0;
for (const [name, ok] of checks) {
  if (!ok) fails++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
}
console.log(`\n${checks.length - fails}/${checks.length} passed`);
process.exit(fails ? 1 : 0);
