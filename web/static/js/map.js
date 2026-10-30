/**
 * CampusMap — one persistent map object, two rendering backends.
 *
 * The requirement this file exists to satisfy: the map is created ONCE when the page
 * loads and is never recreated. Every later update — new origin, new destination, new
 * itineraries, highlight, reset — mutates that same instance. Overlays are cleared by
 * removing individual objects, never by tearing down the container.
 *
 * Backends:
 *   "google"  real Google Maps JS API (set GOOGLE_MAPS_BROWSER_KEY on the server)
 *   "svg"     a self-contained SVG transit map drawn from the GTFS geometry.
 *             No external script, no network, no API key — so the map is on screen
 *             from the first millisecond and never shows a blank tile error.
 *
 * Both backends expose the same surface, so app.js never knows which is live.
 */
const CampusMap = (() => {
  "use strict";

  const CENTER = { lat: 12.9716, lng: 77.5946 };
  const ZOOM = 11;
  const RANK_COLORS = ["#16a34a", "#d97706", "#dc2626", "#7c3aed", "#0891b2"];
  const LINE_COLORS = { PURPLE: "#7c3aed", GREEN: "#059669", YELLOW: "#ca8a04", PINK: "#db2777" };
  const MODE_COLOR = { bus: "#1d4ed8", metro: "#7c3aed", walk: "#94a3b8" };

  let backend = null;          // { kind, ... }
  let cfg = { renderer: "svg", default_center: CENTER, default_zoom: ZOOM };
  let basemap = null;
  let state = { origin: null, destination: null, itineraries: [], active: null };

  // ---------------------------------------------------------------- svg backend
  function svgBackend(el) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "cmap-svg");
    svg.style.width = "100%";
    svg.style.height = "100%";
    svg.style.display = "block";
    svg.style.cursor = "grab";
    el.appendChild(svg);

    const layers = {};
    ["base", "metro", "routes", "markers"].forEach((name) => {
      const g = document.createElementNS(NS, "g");
      g.setAttribute("class", "cmap-layer-" + name);
      svg.appendChild(g);
      layers[name] = g;
    });

    const view = { lat: CENTER.lat, lng: CENTER.lng, scale: 1400 };
    let size = { w: el.clientWidth || 600, h: el.clientHeight || 460 };

    const cosLat = () => Math.cos((view.lat * Math.PI) / 180);
    function project(lat, lng) {
      return [
        size.w / 2 + (lng - view.lng) * view.scale * cosLat(),
        size.h / 2 - (lat - view.lat) * view.scale,
      ];
    }

    function applyView() {
      svg.setAttribute("viewBox", `0 0 ${size.w} ${size.h}`);
    }

    function el2(tag, attrs) {
      const n = document.createElementNS(NS, tag);
      for (const k in attrs) n.setAttribute(k, attrs[k]);
      return n;
    }

    function fit(points, pad = 1.4) {
      if (!points.length) return;
      const lats = points.map((p) => p[0]), lngs = points.map((p) => p[1]);
      const minLat = Math.min(...lats), maxLat = Math.max(...lats);
      const minLng = Math.min(...lngs), maxLng = Math.max(...lngs);
      view.lat = (minLat + maxLat) / 2;
      view.lng = (minLng + maxLng) / 2;
      const dLat = Math.max(maxLat - minLat, 0.01);
      const dLng = Math.max(maxLng - minLng, 0.01) * Math.cos((view.lat * Math.PI) / 180);
      view.scale = Math.min(size.h / (dLat * pad), size.w / (dLng * pad));
      draw();
    }

    function drawBasemap() {
      layers.base.replaceChildren();
      layers.metro.replaceChildren();
      if (!basemap) return;
      (basemap.bus_stops || []).forEach((s) => {
        const [x, y] = project(s.lat, s.lng);
        layers.base.appendChild(el2("circle", {
          cx: x, cy: y, r: 1.1, fill: "#cbd5c8", "fill-opacity": 0.9,
        }));
      });
      (basemap.metro_lines || []).forEach((line) => {
        const color = LINE_COLORS[line.id] || "#475569";
        const pts = line.stations.map((s) => project(s.lat, s.lng).join(",")).join(" ");
        layers.metro.appendChild(el2("polyline", {
          points: pts, fill: "none", stroke: color,
          "stroke-width": 3, "stroke-opacity": line.status === "operational" ? 0.30 : 0.14,
          "stroke-linecap": "round", "stroke-linejoin": "round",
          "stroke-dasharray": line.status === "operational" ? "" : "5 5",
        }));
        line.stations.forEach((s) => {
          const [x, y] = project(s.lat, s.lng);
          layers.metro.appendChild(el2("circle", {
            cx: x, cy: y, r: 2.2, fill: "#fff", stroke: color,
            "stroke-width": 1.4, "stroke-opacity": line.status === "operational" ? 0.55 : 0.25,
          }));
        });
      });
    }

    function drawRoutes() {
      layers.routes.replaceChildren();
      state.itineraries.forEach((itin, i) => {
        const color = RANK_COLORS[Math.min(i, RANK_COLORS.length - 1)];
        const active = state.active === null || state.active === i;
        (itin.legs || []).forEach((leg) => {
          const g = leg.geometry || [];
          if (g.length < 2 || leg.mode === "walk") return;
          const pts = g.map((p) => project(p[0], p[1]).join(",")).join(" ");
          layers.routes.appendChild(el2("polyline", {
            points: pts, fill: "none",
            stroke: active ? color : "#94a3b8",
            "stroke-width": state.active === i ? 5 : 3.2,
            "stroke-opacity": active ? 0.92 : 0.22,
            "stroke-linecap": "round", "stroke-linejoin": "round",
            "data-itinerary": String(i),
          }));
        });
        // walk legs as dotted connectors
        (itin.legs || []).forEach((leg) => {
          if (leg.mode !== "walk" || (leg.geometry || []).length < 2) return;
          const pts = leg.geometry.map((p) => project(p[0], p[1]).join(",")).join(" ");
          layers.routes.appendChild(el2("polyline", {
            points: pts, fill: "none", stroke: active ? "#64748b" : "#cbd5e1",
            "stroke-width": 2, "stroke-opacity": active ? 0.55 : 0.15,
            "stroke-dasharray": "3 4",
          }));
        });
      });
    }

    function drawMarkers() {
      layers.markers.replaceChildren();
      if (state.origin) addPin(state.origin, "A", "#0b6e4f");
      if (state.destination) addPin(state.destination, "B", "#b91c1c");
    }

    function addPin(p, label, color) {
      const [x, y] = project(p.lat, p.lng);
      layers.markers.appendChild(el2("circle", {
        cx: x, cy: y, r: 9, fill: color, stroke: "#fff", "stroke-width": 2.5,
      }));
      const t = el2("text", {
        x, y: y + 4, "text-anchor": "middle", "font-size": "11",
        "font-weight": "700", fill: "#fff", "font-family": "system-ui,sans-serif",
      });
      t.textContent = label;
      layers.markers.appendChild(t);
    }

    function draw() {
      applyView();
      drawBasemap();
      drawRoutes();
      drawMarkers();
    }

    // ---- interaction: drag to pan, wheel to zoom
    let drag = null;
    svg.addEventListener("pointerdown", (e) => {
      drag = { x: e.clientX, y: e.clientY, lat: view.lat, lng: view.lng };
      svg.setPointerCapture(e.pointerId);
      svg.style.cursor = "grabbing";
    });
    svg.addEventListener("pointermove", (e) => {
      if (!drag) return;
      view.lat = drag.lat + (e.clientY - drag.y) / view.scale;
      view.lng = drag.lng - (e.clientX - drag.x) / (view.scale * cosLat());
      draw();
    });
    const endDrag = () => { drag = null; svg.style.cursor = "grab"; };
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", endDrag);
    svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      view.scale = Math.min(400000, Math.max(300, view.scale * f));
      draw();
    }, { passive: false });

    const ro = new ResizeObserver(() => {
      size = { w: el.clientWidth || 600, h: el.clientHeight || 460 };
      draw();
    });
    ro.observe(el);

    return {
      kind: "svg",
      draw, fit,
      fitPoints(points) { fit(points); },
      reset() { view.lat = CENTER.lat; view.lng = CENTER.lng; view.scale = 1400; draw(); },
      redraw: draw,
    };
  }

  // ---------------------------------------------------------------- osm backend
  // Leaflet over OpenStreetMap raster tiles served by /api/tiles/... on our own origin.
  // The tiles are proxied by the backend because the preview iframe has no network of
  // its own; the browser therefore never talks to tile.openstreetmap.org.
  function osmBackend(el) {
    if (typeof L === "undefined") throw new Error("Leaflet did not load");
    const map = L.map(el, {
      center: [CENTER.lat, CENTER.lng],
      zoom: ZOOM,
      zoomControl: true,
      attributionControl: true,
      // SVG, not canvas: with a handful of polylines the cost is irrelevant, and SVG
      // paths stay inspectable and styleable (and testable) in the DOM.
      preferCanvas: false,
      worldCopyJump: true,
    });
    const tiles = L.tileLayer(cfg.tile_url || "/api/tiles/{z}/{x}/{y}.png", {
      maxZoom: cfg.max_zoom || 19,
      minZoom: 3,
      attribution: cfg.attribution || "\u00a9 OpenStreetMap contributors",
      crossOrigin: false,
      errorTileUrl: "",
    });
    tiles.addTo(map);

    // One layer group per concern. Clearing a group removes its children only — the map
    // instance, the tile layer and the pan/zoom state all survive, which is the
    // "persistent map" requirement.
    const groups = {
      metro: L.layerGroup().addTo(map),
      linefocus: L.layerGroup().addTo(map),
      busstops: L.layerGroup(),
      routes: L.layerGroup().addTo(map),
      labels: L.layerGroup().addTo(map),
      markers: L.layerGroup().addTo(map),
      // Live vehicles are deliberately NOT in ``clear()``: clearing itineraries must not
      // silently stop tracking buses that have nothing to do with the current route.
      // Attached at creation: a layer group that never joins the map keeps its children
      // to itself, which is exactly how the first version drew nothing at all.
      live: L.layerGroup().addTo(map),
    };
    let basemapCache = null;

    // The container changes size for reasons Leaflet cannot see: the traffic panel fills
    // in under the map after its request returns, the view switches re-parent it, and a
    // phone rotates. Without this, Leaflet keeps fitting against a stale size and the
    // routes end up projected off the visible area.
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => {
        try { map.invalidateSize({ animate: false }); } catch { /* detached */ }
      });
      ro.observe(el);
    }

    return {
      kind: "osm",
      map,
      tiles,
      resize() {
        try { map.invalidateSize({ animate: false }); return true; } catch { return false; }
      },
      fitPoints(points) {
        if (!points.length) return;
        const b = L.latLngBounds(points.map((p) => [p[0], p[1]]));
        if (!b.isValid()) return;
        map.fitBounds(b, { padding: [40, 40], maxZoom: 15 });
      },
      clear() {
        groups.routes.clearLayers();
        groups.markers.clearLayers();
      },
      setMarkers(origin, destination) {
        groups.markers.clearLayers();
        const pin = (p, label, color) => L.marker([p.lat, p.lng], {
          icon: L.divIcon({
            className: "cmap-pin",
            html: '<span style="background:' + color + '">' + label + "</span>",
            iconSize: [24, 24], iconAnchor: [12, 12],
          }),
          keyboard: false,
        }).addTo(groups.markers);
        if (origin) pin(origin, "A", "#0b6e4f");
        if (destination) pin(destination, "B", "#b91c1c");
      },
      /** Replace the live-vehicle markers. Returns how many were drawn. */
      liveVehicles(vehicles) {
        groups.live.clearLayers();
        if (!vehicles || !vehicles.length) return 0;
        let drawn = 0;
        vehicles.forEach((v) => {
          if (typeof v.lat !== "number" || typeof v.lon !== "number") return;
          const stale = !v.is_live;
          const colour = stale ? "#94a3b8" : "#0f766e";
          // The arrow is drawn only when the feed supplied a bearing. A default heading
          // would be a claim the provider never made.
          const rot = typeof v.bearing === "number" ? v.bearing : null;
          const glyph = rot === null ? ""
            : '<b style="transform:rotate(' + rot + 'deg)">&#9650;</b>';
          const marker = L.marker([v.lat, v.lon], {
            icon: L.divIcon({
              className: "cmap-bus" + (stale ? " stale" : ""),
              html: '<span style="background:' + colour + '">' + glyph + "</span>",
              iconSize: [18, 18], iconAnchor: [9, 9],
            }),
            keyboard: false,
            zIndexOffset: stale ? 0 : 600,
          });
          marker.addTo(groups.live);
          marker.bindTooltip(vehicleTooltip(v), { direction: "top", offset: [0, -9] });
          drawn += 1;
        });
        return drawn;
      },
      clearLiveVehicles() {
        groups.live.clearLayers();
      },
      drawLabels(itineraries, activeIndex) {
        groups.labels.clearLayers();
        itineraries.forEach((itin, i) => {
          const g = itin.geometry || [];
          if (g.length < 2) return;
          // Every itinerary shares the same corridor near the middle of its geometry, so
          // stacking all the labels at the exact midpoint hides all but one. Walk a little
          // along the path per route and fan the labels vertically.
          const n = itineraries.length;
          // Spread labels along the route as well: routes that share a corridor diverge
          // as you move away from the midpoint, so this buys separation for free.
          const f = n === 1 ? 0.5 : 0.32 + (0.36 * i) / (n - 1);
          const mid = g[Math.max(0, Math.min(g.length - 1, Math.round((g.length - 1) * f)))];
          const dy = Math.round((i - (n - 1) / 2) * 26);
          const color = RANK_COLORS[Math.min(i, RANK_COLORS.length - 1)];
          const fare = itin.total_price_inr;
          const mins = itin.total_minutes;
          const txt = (fare == null ? "—" : "\u20b9" + Math.round(fare)) +
                      "<br><span style='font-weight:500;opacity:.85;font-size:10.5px'>~ " +
                      (mins == null ? "—" : Math.round(mins) + " min") + "</span>";
          const dim = activeIndex !== null && activeIndex !== i;
          L.marker([mid[0], mid[1]], {
            icon: L.divIcon({
              className: "",
              html: '<div class="cmap-label" style="color:' + color +
                    ';opacity:' + (dim ? 0.3 : 1) +
                    ';transform:translate(-50%,calc(-50% + ' + dy + 'px))' +
                    ';z-index:' + (dim ? 200 : 400 + n - i) + '">' + txt + "</div>",
              iconSize: [72, 34],
            }),
            interactive: false,
            keyboard: false,
          }).addTo(groups.labels);
        });
      },
      drawBusStops(stops) {
        groups.busstops.clearLayers();
        (stops || []).forEach((s) => {
          L.circleMarker([s.lat, s.lng], {
            radius: 2, color: "#94a3b8", weight: 1, fillColor: "#cbd5e1",
            fillOpacity: 0.9, interactive: false,
          }).addTo(groups.busstops);
        });
      },
      setLayer(name, on) {
        const g = groups[name];
        if (!g) return false;
        if (on) g.addTo(map); else map.removeLayer(g);
        return true;
      },
      drawLegs(itineraries, activeIndex) {
        groups.routes.clearLayers();
        itineraries.forEach((itin, i) => {
          const color = RANK_COLORS[Math.min(i, RANK_COLORS.length - 1)];
          const dim = activeIndex !== null && activeIndex !== i;
          const legs = itin.legs || [];
          const anyLegGeometry = legs.some((l) => (l.geometry || []).length >= 2);
          // When the per-leg geometry is unavailable (the contract response only carries
          // step summaries) fall back to the itinerary's own line. It is the real route,
          // just not split by leg, which is why the walk styling is skipped here.
          if (!anyLegGeometry && (itin.geometry || []).length >= 2) {
            L.polyline(itin.geometry, {
              color, weight: activeIndex === i ? 6 : 4, opacity: dim ? 0.22 : 0.9,
              lineCap: "round", dashArray: itin.mode_sequence && itin.mode_sequence.length === 1
                && itin.mode_sequence[0] === "walk" ? "3 5" : null,
            }).addTo(groups.routes);
            return;
          }
          legs.forEach((leg) => {
            const g = leg.geometry || [];
            if (g.length < 2) return;
            const walk = leg.mode === "walk";
            L.polyline(g, {
              color: walk ? "#64748b" : color,
              weight: walk ? 2 : (activeIndex === i ? 6 : 4),
              opacity: dim ? 0.22 : (walk ? 0.6 : 0.9),
              dashArray: walk ? "3 5" : null,
              lineCap: "round",
            }).addTo(groups.routes);
          });
        });
      },
      focusLine(lineId) {
        groups.linefocus.clearLayers();
        const bm = basemapCache || {};
        const line = (bm.metro_lines || []).find((l) => l.id === lineId);
        if (!line || !line.stations || !line.stations.length) return null;
        const color = LINE_COLORS[line.id] || "#475569";
        const pending = line.status !== "operational";
        L.polyline(line.stations.map((s) => [s.lat, s.lng]), {
          color, weight: 6, opacity: pending ? 0.5 : 0.95,
          dashArray: pending ? "8 7" : null, lineCap: "round",
        }).addTo(groups.linefocus);
        line.stations.forEach((s) => L.circleMarker([s.lat, s.lng], {
          radius: 4.5, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1,
        }).addTo(groups.linefocus)
          .bindTooltip(`${s.name} · ${line.name} Line` +
                       (s.status !== "operational" ? " (not in service)" : ""),
                       { direction: "top" }));
        map.fitBounds(L.latLngBounds(line.stations.map((s) => [s.lat, s.lng])),
                      { padding: [40, 40], maxZoom: 14 });
        return { id: line.id, name: line.name, stations: line.stations.length,
                 status: line.status, colour: color };
      },
      clearFocus() { groups.linefocus.clearLayers(); },
      drawMetro(lines) {
        groups.metro.clearLayers();
        (lines || []).forEach((line) => {
          const color = LINE_COLORS[line.id] || "#475569";
          const pending = line.status !== "operational";
          L.polyline(line.stations.map((s) => [s.lat, s.lng]), {
            color, weight: 3, opacity: pending ? 0.35 : 0.65,
            dashArray: pending ? "6 6" : null,
          }).addTo(groups.metro);
          if (map.getZoom() >= 12) {
            line.stations.forEach((s) => L.circleMarker([s.lat, s.lng], {
              radius: 3, color, weight: 1.5, fillColor: "#fff", fillOpacity: 1,
              opacity: pending ? 0.5 : 0.9,
            }).addTo(groups.metro).bindTooltip(s.name + (pending ? " (not open)" : ""),
              { direction: "top" }));
          }
        });
      },
      toggleTraffic(show) {
        // Leaflet has no traffic layer without a tile provider that supplies one.
        // Say so instead of pretending.
        return false;
      },
      reset() {
        map.setView([CENTER.lat, CENTER.lng], ZOOM);
      },
      redraw() { /* Leaflet repaints itself */ },
      rememberBasemap(bm) { basemapCache = bm; },
      getBasemap() { return basemapCache; },
    };
  }

  // ------------------------------------------------------------- google backend
  function googleBackend(el) {
    const map = new google.maps.Map(el, {
      center: CENTER, zoom: ZOOM, mapTypeControl: true, fullscreenControl: true,
      streetViewControl: false, zoomControl: true,
      styles: [{ featureType: "transit", stylers: [{ visibility: "on" }] }],
    });
    const overlays = { routes: [], markers: [], metro: [], focus: [], live: [] };
    let traffic = null;

    return {
      kind: "google",
      map,
      fitPoints(points) {
        if (!points.length) return;
        const b = new google.maps.LatLngBounds();
        points.forEach((p) => b.extend({ lat: p[0], lng: p[1] }));
        map.fitBounds(b, 60);
      },
      clear() {
        overlays.routes.forEach((o) => o.setMap(null));
        overlays.markers.forEach((o) => o.setMap(null));
        overlays.routes = []; overlays.markers = [];
      },
      setMarkers(origin, destination) {
        overlays.markers.forEach((o) => o.setMap(null));
        overlays.markers = [];
        if (origin) overlays.markers.push(new google.maps.Marker({
          position: origin, map, label: "A", title: "Origin" }));
        if (destination) overlays.markers.push(new google.maps.Marker({
          position: destination, map, label: "B", title: "Destination" }));
      },
      liveVehicles(vehicles) {
        overlays.live.forEach((o) => o.setMap(null));
        overlays.live = [];
        if (!vehicles || !vehicles.length) return 0;
        vehicles.forEach((v) => {
          if (typeof v.lat !== "number" || typeof v.lon !== "number") return;
          overlays.live.push(new google.maps.Marker({
            position: { lat: v.lat, lng: v.lon }, map,
            title: vehicleTooltip(v),
            icon: {
              path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW,
              scale: 3,
              rotation: typeof v.bearing === "number" ? v.bearing : 0,
              fillColor: v.is_live ? "#0f766e" : "#94a3b8",
              fillOpacity: 1, strokeColor: "#fff", strokeWeight: 1,
            },
            zIndex: v.is_live ? 600 : 100,
          }));
        });
        return vehicles.length;
      },
      clearLiveVehicles() {
        overlays.live.forEach((o) => o.setMap(null));
        overlays.live = [];
      },
      drawLegs(itineraries, activeIndex) {
        overlays.routes.forEach((o) => o.setMap(null));
        overlays.routes = [];
        itineraries.forEach((itin, i) => {
          const color = RANK_COLORS[Math.min(i, RANK_COLORS.length - 1)];
          const dim = activeIndex !== null && activeIndex !== i;
          (itin.legs || []).forEach((leg) => {
            if (leg.mode === "walk" || (leg.geometry || []).length < 2) return;
            overlays.routes.push(new google.maps.Polyline({
              path: leg.geometry.map((p) => ({ lat: p[0], lng: p[1] })),
              strokeColor: color, strokeOpacity: dim ? 0.25 : 0.85,
              strokeWeight: activeIndex === i ? 6 : 4, map, zIndex: activeIndex === i ? 10 : 1,
            }));
          });
        });
      },
      focusLine(lineId) {
        overlays.focus.forEach((o) => o.setMap(null));
        overlays.focus = [];
        const bm = basemapCache || {};
        const line = (bm.metro_lines || []).find((l) => l.id === lineId);
        if (!line || !line.stations.length) return null;
        const color = LINE_COLORS[line.id] || "#475569";
        const pending = line.status !== "operational";
        overlays.focus.push(new google.maps.Polyline({
          path: line.stations.map((s) => ({ lat: s.lat, lng: s.lng })),
          strokeColor: color, strokeOpacity: pending ? 0.55 : 0.95,
          strokeWeight: 6, map, zIndex: 20,
        }));
        const b = new google.maps.LatLngBounds();
        line.stations.forEach((s) => {
          b.extend({ lat: s.lat, lng: s.lng });
          overlays.focus.push(new google.maps.Marker({
            position: { lat: s.lat, lng: s.lng }, map, title: s.name,
            icon: { path: google.maps.SymbolPath.CIRCLE, scale: 4,
                    fillColor: color, fillOpacity: 1, strokeColor: "#fff", strokeWeight: 2 },
          }));
        });
        map.fitBounds(b, 40);
        return { id: line.id, name: line.name, stations: line.stations.length,
                 status: line.status, colour: color };
      },
      clearFocus() {
        overlays.focus.forEach((o) => o.setMap(null));
        overlays.focus = [];
      },
      drawMetro(lines) {
        overlays.metro.forEach((o) => o.setMap(null));
        overlays.metro = [];
        (lines || []).forEach((line) => {
          overlays.metro.push(new google.maps.Polyline({
            path: line.stations.map((s) => ({ lat: s.lat, lng: s.lng })),
            strokeColor: LINE_COLORS[line.id] || "#475569",
            strokeOpacity: line.status === "operational" ? 0.35 : 0.15,
            strokeWeight: 3, map, zIndex: 0,
          }));
        });
      },
      toggleTraffic(show) {
        if (!traffic) traffic = new google.maps.TrafficLayer();
        traffic.setMap(show ? map : null);
      },
      reset() { map.setCenter(CENTER); map.setZoom(ZOOM); },
    };
  }

  // ---------------------------------------------------------------- public API
  function loadGoogleScript(key) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&libraries=geometry&callback=__cmapReady`;
      s.async = true;
      s.onerror = () => reject(new Error("Google Maps script failed to load"));
      window.__cmapReady = resolve;
      document.head.appendChild(s);
    });
  }

  /** Ask the browser to actually decode one tile. Resolves true only if it renders. */
  function probeTile(url) {
    return new Promise((resolve) => {
      const img = new Image();
      const done = (ok) => resolve(ok);
      const timer = setTimeout(() => done(false), 6000);
      img.onload = () => { clearTimeout(timer); done(img.naturalWidth > 0); };
      img.onerror = () => { clearTimeout(timer); done(false); };
      img.src = url + (url.includes("?") ? "&" : "?") + "probe=" + Date.now();
    });
  }

  async function init() {
    const el = document.getElementById("map");
    if (!el) return;
    try {
      cfg = await (await fetch("/api/map/config")).json();
    } catch { /* keep svg default */ }

    // Walk the server's preference chain, but verify each step instead of trusting it.
    // A renderer is only adopted if it can actually draw something right now.
    const chain = [cfg.renderer, ...(cfg.fallback_chain || ["svg"])].filter(Boolean);
    let chosen = null;

    for (const kind of chain) {
      if (kind === "google") {
        if (!cfg.browser_key) continue;
        try {
          await loadGoogleScript(cfg.browser_key);
          chosen = googleBackend(el);
          break;
        } catch (err) {
          console.warn("[CampusMap] Google Maps failed:", err.message);
          note("Google Maps could not load — trying the OpenStreetMap layers.", "warn");
        }
      } else if (kind === "osm") {
        if (typeof L === "undefined") {
          note("Leaflet is not on the page — drew the GTFS map instead.", "warn");
          continue;
        }
        const ok = await probeTile(cfg.tile_probe || "/api/tiles/11/1447/940.png");
        if (!ok) {
          note("Map tiles could not be fetched — drew the GTFS map instead.", "warn");
          continue;
        }
        try {
          chosen = osmBackend(el);
          break;
        } catch (err) {
          console.warn("[CampusMap] OSM backend failed:", err.message);
        }
      } else if (kind === "svg") {
        chosen = svgBackend(el);
        break;
      }
    }
    backend = chosen || svgBackend(el);

    try {
      basemap = await (await fetch("/api/map/basemap")).json();
    } catch { basemap = { metro_lines: [], bus_stops: [] }; }
    if (backend.drawMetro) backend.drawMetro(basemap.metro_lines);
    if (backend.kind === "osm") {
      try { backend.map.invalidateSize(); } catch { /* container not laid out yet */ }
    }
    backend.redraw ? backend.redraw() : backend.fitPoints((basemap.bus_stops || []).map(
      (s) => [s.lat, s.lng]));
    if (backend.rememberBasemap) backend.rememberBasemap(basemap);
    if (backend.kind === "svg") {
      backend.fitPoints((basemap.bus_stops || []).map((s) => [s.lat, s.lng]));
    }
    setAttribution(cfg.attribution);
    updateBadge();
  }

  /** One line describing a vehicle, built only from fields the feed actually sent. */
  function vehicleTooltip(v) {
    const parts = [v.label || v.vehicle_id || "vehicle"];
    if (v.route_id) parts.push("route " + v.route_id);
    if (typeof v.age_s === "number") parts.push(Math.round(v.age_s) + "s ago");
    else parts.push("no timestamp");
    if (!v.is_live) parts.push("DELAYED");
    return parts.join(" \u00b7 ");
  }

  let lastNote = "";

  function note(text, kind) {
    lastNote = text;
    const el = document.getElementById("map-note");
    if (!el) return;
    el.textContent = text || "";
    el.className = "map-note" + (kind ? " " + kind : "");
  }

  // The map instance is created once per page load (brief: persistent map).
  // Switching views re-parents the same DOM node into that view's slot instead of
  // building a second map, then asks the backend to re-measure its container.
  function moveTo(slot) {
    const node = document.getElementById("map");
    if (!node) return false;
    const host = typeof slot === "string" ? document.getElementById(slot) : slot;
    if (!host) return false;
    if (node.parentElement !== host) host.appendChild(node);
    // The layer panel, legend and attribution travel with the map — they would be
    // stranded in a hidden view otherwise.
    const overlays = document.getElementById("map-overlays");
    if (overlays && overlays.parentElement !== host) host.appendChild(overlays);
    const after = () => {
      try {
        if (backend && backend.map && backend.map.invalidateSize) {
          backend.map.invalidateSize({ animate: false });
        } else if (backend && backend.redraw) {
          backend.redraw();
        }
      } catch { /* container still hidden */ }
    };
    after();
    requestAnimationFrame(after);
    setTimeout(after, 120);
    return true;
  }

  function setAttribution(text) {
    const el = document.getElementById("map-attribution");
    if (el) el.textContent = text || "";
  }

  function showFallbackMessage(msg) {
    const f = document.getElementById("map-fallback");
    if (!f) return;
    f.textContent = "⚠️ " + msg;
    f.classList.remove("hidden");
  }

  function updateBadge() {
    const el = document.getElementById("map-badge");
    const label = backend ? ({ google: "Google Maps", osm: "OpenStreetMap",
                               svg: "GTFS map" })[backend.kind] || backend.kind : "—";
    if (el) el.textContent = label;
  }

  function allPoints(itineraries) {
    const pts = [];
    itineraries.forEach((it) => (it.legs || []).forEach((l) => (l.geometry || []).forEach(
      (p) => pts.push(p))));
    if (state.origin) pts.push([state.origin.lat, state.origin.lng]);
    if (state.destination) pts.push([state.destination.lat, state.destination.lng]);
    return pts;
  }

  function drawItineraries(data) {
    if (!backend) return false;
    state.itineraries = data.itineraries || [];
    state.active = null;
    state.origin = data.origin ? { lat: data.origin.lat, lng: data.origin.lng } : state.origin;
    state.destination = data.destination
      ? { lat: data.destination.lat, lng: data.destination.lng } : state.destination;

    const points = allPoints(state.itineraries);
    if (backend.kind === "google" || backend.kind === "osm") {
      // The map may have been re-parented into a differently sized container since the
      // last draw. Re-measure before fitting, or fitBounds computes against the old size
      // and the routes end up projected outside the visible area.
      if (backend.map && backend.map.invalidateSize) {
        try { backend.map.invalidateSize({ animate: false }); } catch { /* hidden */ }
      }
      backend.setMarkers(state.origin, state.destination);
      backend.drawLegs(state.itineraries, null);
      if (backend.drawLabels) backend.drawLabels(state.itineraries, null);
      backend.fitPoints(points);
    } else {
      backend.draw();
      backend.fitPoints(points);
    }
    return points.length > 0;
  }

  function highlightRoute(index) {
    if (!backend) return;
    state.active = index;
    if (backend.kind === "google" || backend.kind === "osm") {
      backend.drawLegs(state.itineraries, index);
      if (backend.drawLabels) backend.drawLabels(state.itineraries, index);
      const pts = [];
      (state.itineraries[index]?.legs || []).forEach((l) =>
        (l.geometry || []).forEach((p) => pts.push(p)));
      backend.fitPoints(pts);
    } else {
      backend.draw();
    }
  }

  function clearRouteOverlays() {
    state.itineraries = [];
    state.active = null;
    if (!backend) return;
    if (backend.kind === "google" || backend.kind === "osm") backend.clear();
    else backend.draw();
    if (backend.drawLabels) backend.drawLabels([], null);
  }

  /** Draw one metro line prominently and frame it. Returns null where unsupported. */
  function focusLine(lineId) {
    if (!backend || !backend.focusLine) return null;
    return backend.focusLine(lineId);
  }

  function clearFocus() {
    if (backend && backend.clearFocus) backend.clearFocus();
  }

  /** Layer toggles from the map's own panel. Returns false where a layer cannot exist. */
  function setLayer(name, on) {
    if (!backend) return false;
    if (name === "traffic") {
      // There is no traffic layer without a traffic provider — say so, do not fake it.
      return toggleTraffic(on);
    }
    if (backend.setLayer) return backend.setLayer(name, on);
    if (name === "routes") {
      state.itineraries = on ? state.itineraries : [];
      return true;
    }
    return false;
  }

  function loadBusStops() {
    if (!backend || !backend.drawBusStops) return;
    const bm = basemap || (backend.getBasemap && backend.getBasemap());
    if (bm && bm.bus_stops) backend.drawBusStops(bm.bus_stops);
  }

  function resetToOverview() {
    state.itineraries = [];
    state.active = null;
    if (!backend) return;
    if (backend.kind === "google" || backend.kind === "osm") backend.clear();
    backend.reset();
    note("");
  }

  function toggleTraffic(show) {
    if (!backend) return false;
    if (backend.kind === "google") {
      backend.toggleTraffic(show);
      note(show ? "Google road-congestion layer on. This is NOT live bus or metro tracking."
                : "", "");
      return true;
    }
    // No Google key: there is no honest traffic overlay to draw. Say so, once.
    note(show ? "Road-traffic data needs a Google Maps key. This build shows scheduled "
              + "times only and will not fake a congestion layer." : "", show ? "warn" : "");
    return false;
  }

  /**
   * Draw live vehicle positions from /api/transit/vehicle-positions.
   *
   * Returns the number drawn, or ``false`` when this basemap has no live layer (the SVG
   * fallback). Never invents a marker: an empty list clears the layer and returns 0.
   */
  function setLiveVehicles(vehicles) {
    if (!backend) return false;
    if (typeof backend.liveVehicles === "function") {
      return backend.liveVehicles(Array.isArray(vehicles) ? vehicles : []);
    }
    return false;
  }

  function clearLiveVehicles() {
    if (backend && typeof backend.clearLiveVehicles === "function") backend.clearLiveVehicles();
  }

  function stats() {
    return {
      renderer: backend ? backend.kind : null,
      itineraries: state.itineraries.length,
      markers: (state.origin ? 1 : 0) + (state.destination ? 1 : 0),
      active: state.active,
      note: lastNote,
    };
  }

  return {
    init, drawItineraries, highlightRoute, clearRouteOverlays, resetToOverview,
    toggleTraffic, stats, showFallbackMessage, setLayer, loadBusStops, moveTo,
    focusLine, clearFocus, setLiveVehicles, clearLiveVehicles,
    resize: () => (backend && backend.resize ? backend.resize() : false),
    getState: () => state,
    getRenderer: () => (backend ? backend.kind : null),
  };
})();

// expose on window as well as the script-level binding, so later scripts, the console
// and test harnesses can all reach the same single instance
window.CampusMap = CampusMap;

// test hook for the Playwright spec
window.__TEST_markerCount = () => CampusMap.stats().markers;
window.__TEST_mapStats = () => CampusMap.stats();
