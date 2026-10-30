/**
 * App — controller. Owns navigation, the current plan, theme, and every user action.
 *
 * Rules: every control does something; the map object is created once and only mutated;
 * whatever the server says is unavailable is shown as unavailable.
 */
const App = (() => {
  "use strict";

  const state = {
    // Conversation turns sent back with each message. Bounded: a local 3B model has a
    // small context and older turns would crowd out the tool results.
    chatHistory: [],
    view: "chat",
    plan: null,          // contract-shaped response from /api/routes/plan
    sort: "rank",
    activeIdx: 0,
    mode: "all",
    query: null,         // { origin, destination }
    arrivalBy: "",
    caps: [],
    prefs: {},
    demo: false,
    theme: "light",
    lines: [],           // metro line objects from /api/transit/lines
    openLines: null,     // Set of line ids whose station list is expanded
    intercity: null,     // KSRTC corridor quotes attached to the last plan
  };

  const $ = (id) => document.getElementById(id);
  const el = (sel) => document.querySelector(sel);

  const VIEW_TITLE = {
    chat: "CampusTransit",
    plan: "Trip Planner",
    explore: "Explore Map",
    saved: "Saved Routes",
    history: "Travel History",
    settings: "Settings",
  };

  /* ---------------------------------------------------------------- theme */
  function applyTheme(theme) {
    state.theme = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", state.theme);
    const use = $("theme-icon") ? $("theme-icon").querySelector("use") : null;
    if (use) use.setAttribute("href", state.theme === "dark" ? "#i-sun" : "#i-moon");
    try { localStorage.setItem("ct-theme", state.theme); } catch { /* private mode */ }
    // Leaflet keeps its own DOM; nudge it to re-read CSS variables.
    window.dispatchEvent(new Event("resize"));
  }

  /* ---------------------------------------------------------------- navigation */
  function show(view) {
    if (!VIEW_TITLE[view]) return;
    state.view = view;
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.id === "view-" + view));
    document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
    const h2 = el(".topbar .titles h2");
    if (h2) h2.textContent = VIEW_TITLE[view];
    // Phones get the sidebar as a drawer; every navigation closes it again.
    const shell = document.getElementById("app");
    if (window.innerWidth <= 980) shell.classList.add("sidebar-collapsed");
    else shell.classList.remove("sidebar-collapsed");

    if (view === "saved") loadSaved();
    if (view === "history") loadHistory();
    if (view === "settings") loadSettings();
    if (view === "explore") loadExplore();
    // One map instance for the whole session: re-parent it into this view's slot.
    if (window.CampusMap && window.CampusMap.moveTo) {
      if (view === "chat" || view === "plan" || view === "explore") {
        window.CampusMap.moveTo("map-slot-" + view);
      }
    }
    // The traffic panel is one element too — it follows the map into the view that has
    // room for it, and goes home to the chat column otherwise.
    const tp = document.getElementById("traffic-panel");
    const tpSlot = document.getElementById("tp-slot-" + view);
    if (tp && tpSlot && tp.parentElement !== tpSlot) tpSlot.appendChild(tp);
    if (view === "plan" && state.plan) refreshMap();
    if (view === "chat" || view === "plan" || view === "explore") {
      setTimeout(() => window.dispatchEvent(new Event("resize")), 60);
    }
  }

  /* ---------------------------------------------------------------- plan */
  function syncInputs() {
    const pairs = [["origin", "origin-2"], ["destination", "destination-2"]];
    for (const [a, b] of pairs) {
      const va = $(a) ? $(a).value : "";
      const vb = $(b) ? $(b).value : "";
      const v = va || vb;
      if ($(a) && !va) $(a).value = v;
      if ($(b) && !vb) $(b).value = v;
    }
  }

  function readPair() {
    const pick = (a, b) => (($(a) && $(a).value.trim()) || ($(b) && $(b).value.trim()) || "");
    return { origin: pick("origin", "origin-2"), destination: pick("destination", "destination-2") };
  }

  function modesForRequest() {
    if (state.mode === "all") return ["bus", "metro", "walking"];
    if (state.mode === "driving") return ["driving"];
    if (state.mode === "walking") return ["walking"];
    return [state.mode];
  }

  async function planTrip(originArg, destinationArg) {
    const pair = (originArg && destinationArg)
      ? { origin: String(originArg).trim(), destination: String(destinationArg).trim() }
      : readPair();

    if (!pair.origin || !pair.destination) {
      Views.toast("Enter both an origin and a destination");
      return;
    }
    if (pair.origin.toLowerCase() === pair.destination.toLowerCase()) {
      Views.toast("Origin and destination are the same place");
      return;
    }

    state.query = pair;
    if ($("origin")) $("origin").value = pair.origin;
    if ($("origin-2")) $("origin-2").value = pair.origin;
    if ($("destination")) $("destination").value = pair.destination;
    if ($("destination-2")) $("destination-2").value = pair.destination;

    const btn = $("btn-plan");
    if (btn) btn.disabled = true;
    // Drop the previous pair's KSRTC quote before the new one arrives: a stale corridor
    // fare under a fresh result list is exactly the kind of mix-up this panel must not make.
    if ($("intercity-slot")) {
      $("intercity-slot").classList.add("hidden");
      $("intercity-slot").innerHTML = "";
    }
    Views.skeleton($("results-list"), 3);

    const body = {
      origin: pair.origin,
      destination: pair.destination,
      preference: state.sort === "rank" ? "balanced"
        : state.sort === "fewest" ? "fewest_transfers"
        : state.sort === "walk" ? "least_walking" : state.sort,
      arrival_by: state.arrivalBy || null,
      transport_modes: modesForRequest(),
    };

    const t0 = performance.now();
    const res = await API.planContract(body);
    const ms = Math.round(performance.now() - t0);
    if (btn) btn.disabled = false;

    if (!res.ok) {
      $("results-list").innerHTML =
        `<div class="banner err">${Views.icon("alert")}<div>${Views.esc(res.error)}</div></div>`;
      Views.toast("Planning failed: " + res.error);
      return;
    }

    const data = res.data;
    state.plan = data;
    state.activeIdx = 0;

    // Show the name the server actually resolved (with its preferred spelling) rather
    // than the raw text, so the two forms agree with the map and the header.
    const resolvedO = (data.origin && data.origin.name) || pair.origin;
    const resolvedD = (data.destination && data.destination.name) || pair.destination;
    if (resolvedO !== pair.origin || resolvedD !== pair.destination) {
      ["origin", "origin-2"].forEach((k) => { if ($(k)) $(k).value = resolvedO; });
      ["destination", "destination-2"].forEach((k) => { if ($(k)) $(k).value = resolvedD; });
    }

    // Normalise the contract shape into what the renderer + map want.
    const itineraries = (data.routes || []).map((r, i) => ({
      itinerary_id: r.id,
      rank: i + 1,
      title: r.title,
      mode_sequence: r.transport_modes || [],
      total_price_inr: r.fare ? r.fare.amount : null,
      fare_status: r.fare ? r.fare.status : "unavailable",
      fare_source: r.fare ? r.fare.source : null,
      fare_reason: r.fare ? r.fare.reason : null,
      unpriced_legs: (r.fare && r.fare.unpriced_legs) || [],
      priced_subtotal: r.fare ? r.fare.priced_subtotal : null,
      total_minutes: r.duration_minutes,
      distance_km: r.distance_km,
      departure_time: r.departure_time,
      arrival_time: r.arrival_time,
      transfers: r.transfers,
      walking_minutes: r.walking_minutes,
      comfort_score: r.comfort_score,
      explanation: r.explanation,
      traffic_status: r.traffic ? r.traffic.status : "unavailable",
      traffic_source: r.traffic ? r.traffic.source : null,
      geometry: r.geometry || [],
      legs: (r.steps || []).map((s) => ({
        mode: s.mode, label: s.service, from: s.from, to: s.to,
        minutes: s.duration_minutes, km: s.distance_km, fare_inr: s.fare,
        operator: s.operator || null, line: s.line || null,
        line_colour: s.line_colour || null,
        geometry: [],
      })),
      fare_breakdown: (r.fare && r.fare.breakdown) || [],
      confidence: r.confidence,
    }));

    // The contract only carries step summaries, so pull real per-leg geometry from the
    // rich endpoint for the same pair. Falling back to straight legs is visible, not silent.
    const rich = await API.planRich({
      origin: pair.origin, destination: pair.destination,
      arrival_by: state.arrivalBy || null,
      preference: body.preference,
      max_itineraries: 5,
    });
    if (rich.ok && rich.data.itineraries) {
      // Match on itinerary_id: the id encodes objective + legs, so a route that the
      // contract returned under a different mode filter simply has no twin here and
      // keeps its own line rather than borrowing someone else's geometry.
      const byId = new Map(rich.data.itineraries.map((it) => [it.itinerary_id, it]));
      let grafted = 0;
      itineraries.forEach((it) => {
        const src = byId.get(it.itinerary_id);
        if (src && (src.legs || []).some((l) => (l.geometry || []).length >= 2)) {
          it.legs = src.legs;
          it.geometry = src.geometry;
          grafted += 1;
        }
      });
      state.geometryGrafted = grafted;
    }

    state.itineraries = itineraries;
    state.intercity = data.intercity || null;
    const shown = Views.renderPlan($("results-list"), {
      ok: !!(data.routes || []).length,
      itineraries,
      warnings: data.warnings || [],
    }, { sort: state.sort, active: 0 });

    Views.renderIntercity($("intercity-slot"), state.intercity);
    if ($("tab-count")) $("tab-count").textContent = `(${shown})`;
    if ($("plan-sub")) {
      $("plan-sub").textContent = shown
        ? `${data.origin && data.origin.name ? data.origin.name : pair.origin} → ` +
          `${data.destination && data.destination.name ? data.destination.name : pair.destination}`
        : "no routes";
    }

    if (data.warnings && data.warnings.length) Views.toast(data.warnings[0], 4200);

    refreshMap();
    renderTrafficPanel(data);
    if (state.view === "chat") show("plan");
    syncUrl(pair);
    const total = document.querySelectorAll("#results-list .rcard").length;
    if (!$("results-list").querySelector(".rcard")) {
      $("results-list").insertAdjacentHTML("afterbegin",
        `<div class="banner">${Views.icon("alert")}<div>
         ${Views.esc((data.warnings || ["No route found."]).join(" "))}</div></div>`);
    }
    if ($("tab-count")) $("tab-count").textContent = `(${total})`;
    return ms;
  }

  async function refreshMap() {
    if (!window.CampusMap || !state.plan) return;
    const its = Views.sortItineraries(state.itineraries || [], state.sort);
    const o = state.plan.origin, d = state.plan.destination;
    if (!o || !d || o.latitude == null || d.latitude == null) return;
    CampusMap.drawItineraries({
      origin: { lat: o.latitude, lng: o.longitude },
      destination: { lat: d.latitude, lng: d.longitude },
      itineraries: its,
    });
  }

  function renderTrafficPanel(data) {
    const advice = data.departure_advice || {};
    const payload = {
      live_traffic: { available: false, reason: "No traffic provider configured." },
      network_supply: { available: false, reason: "No corridor yet." },
      departure_advice: advice.note ? {
        note: advice.note,
        headline: advice.mode === "latest_departure"
          ? `To reach by ${state.arrivalBy || advice.latest_departure}, leave by ${advice.latest_departure}`
          : `Leave now to arrive around ${advice.arrive_about || "—"}`,
        explanation: advice.basis || "",
      } : null,
    };
    Views.trafficPanel($("traffic-body"), payload);

    // Ask the backend for real traffic + supply on this corridor.
    if (state.query) {
      API.traffic(state.query.origin, state.query.destination).then((r) => {
        if (!r.ok) return;
        Views.trafficPanel($("traffic-body"), {
          live_traffic: r.data.live_traffic || payload.live_traffic,
          network_supply: r.data.network_supply || payload.network_supply,
          departure_advice: payload.departure_advice,
        });
        // The panel just grew, so the map above it shrank. Re-measure, then re-fit so the
        // whole itinerary stays visible in the space that is actually left.
        requestAnimationFrame(() => {
          if (!window.CampusMap) return;
          CampusMap.resize();
          if (state.view === "plan" && state.plan) refreshMap();
        });
      });
    }
  }

  function syncUrl(pair) {
    try {
      const u = new URL(window.location.href);
      u.searchParams.set("from", pair.origin);
      u.searchParams.set("to", pair.destination);
      window.history.replaceState({}, "", u);
    } catch { /* sandboxed iframe */ }
  }

  /* ---------------------------------------------------------------- card actions */
  function currentSorted() {
    return Views.sortItineraries(state.itineraries || [], state.sort);
  }

  function selectItinerary(idx) {
    state.activeIdx = idx;
    document.querySelectorAll("#results-list .rcard").forEach((c) => {
      c.classList.toggle("on", +c.dataset.idx === idx);
    });
    if (window.CampusMap) CampusMap.highlightRoute(idx);
  }

  function onResultsClick(e) {
    const btn = e.target.closest("[data-act]");
    const card = e.target.closest(".rcard");
    if (btn) {
      e.stopPropagation();
      const it = currentSorted()[+btn.dataset.idx];
      if (!it) return;
      const act = btn.dataset.act;
      if (act === "focus") { selectItinerary(+btn.dataset.idx); scrollMapIntoView(); }
      if (act === "save") saveItinerary(it);
      if (act === "log") logItinerary(it);
      if (act === "fares") toggleFares(it, +btn.dataset.idx);
      return;
    }
    if (card) selectItinerary(+card.dataset.idx);
  }

  function scrollMapIntoView() {
    const m = $("map");
    if (m) m.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function toggleFares(it, idx) {
    const card = document.querySelector(`#results-list .rcard[data-idx="${idx}"]`);
    if (!card) return;
    const existing = card.querySelector(".fare-table");
    if (existing) { existing.remove(); return; }
    const div = document.createElement("div");
    div.innerHTML = Views.fareTable(it);
    card.appendChild(div.firstElementChild);
  }

  async function saveItinerary(it) {
    const q = state.query || {};
    const res = await API.saveRoute({
      origin: q.origin || "", destination: q.destination || "",
      route_id: it.itinerary_id || "", title: `${q.origin} → ${q.destination}`,
      itinerary: it,
    });
    if (res.ok) { Views.toast("Saved to Saved Routes"); refreshCounts(); }
    else Views.toast("Could not save: " + res.error);
  }

  async function logItinerary(it) {
    const q = state.query || {};
    const res = await API.logHistory({
      origin: q.origin || "", destination: q.destination || "",
      departure_time: it.departure_time || state.arrivalBy || "",
      selected_mode: (it.mode_sequence || []).join("+"),
      estimated_fare_inr: it.total_price_inr,
      estimated_duration: it.total_minutes != null ? Math.round(it.total_minutes) : null,
      route_id: it.itinerary_id || "",
    });
    if (res.ok) { Views.toast("Trip logged"); refreshCounts(); }
    else Views.toast("Could not log: " + res.error);
  }

  /* ---------------------------------------------------------------- chat */
  function pushMsg(kind, html, isHtml) {
    const w = $("welcome");
    if (w) w.remove();
    const node = document.createElement("div");
    node.className = "msg " + kind;
    const av = kind === "user"
      ? Views.icon("user")
      : Views.icon("bus");
    node.innerHTML = `<div class="av">${av}</div>
      <div class="body"><div class="who">${kind === "user" ? "You" : "CampusTransit"}</div>
      <div class="bubble"></div></div>`;
    const b = node.querySelector(".bubble");
    if (isHtml) b.innerHTML = html; else b.textContent = html;
    $("msgs").appendChild(node);
    $("msgs").scrollTop = $("msgs").scrollHeight;
    return node;
  }

  async function send() {
    const input = $("input");
    const text = input.value.trim();
    if (!text) return;
    pushMsg("user", text);
    input.value = "";
    input.style.height = "auto";
    $("send").disabled = true;

    const pending = pushMsg("bot",
      '<span class="typing"><i></i><i></i><i></i></span>', true);

    // A local model on a CPU can take a minute. Saying so -- with the elapsed seconds --
    // is the difference between "thinking" and "broken".
    const t0 = Date.now();
    const bubble = pending.querySelector(".bubble");
    const ticker = setInterval(() => {
      const s = Math.round((Date.now() - t0) / 1000);
      const typ = bubble.querySelector(".typing");
      if (typ && s >= 5) {
        bubble.innerHTML = typ.outerHTML +
          `<span class="slow-hint">${s}s · local model, still working…</span>`;
      }
    }, 1000);

    const res = await API.chat(text, state.chatHistory.slice(-6));
    clearInterval(ticker);
    pending.remove();
    $("send").disabled = false;

    if (!res.ok) {
      pushMsg("bot", `<div class="banner err">${Views.icon("alert")}<div>Could not reach
        the server: ${Views.esc(res.error)}</div></div>`, true);
      return;
    }
    const d = res.data || {};
    const answer = d.answer || d.reply || "I could not answer that.";
    const node = pushMsg("bot", Views.md(answer), true);

    // Who answered, and if the model was passed over, why. This is the difference between
    // "the AI said so" and "a deterministic planner said so" — the user gets to know.
    const prov = provenance(d);
    if (prov) {
      const line = document.createElement("div");
      line.className = "msg-prov" + (d.fallback_reason ? " warn" : "");
      line.innerHTML = prov;
      node.querySelector(".body").appendChild(line);
    }
    state.chatHistory.push({ role: "user", content: text },
                           { role: "assistant", content: answer });

    const od = extractOD(text);
    if (od) {
      if ($("origin")) $("origin").value = od.origin;
      if ($("destination")) $("destination").value = od.destination;
      planTrip(od.origin, od.destination);
    }
    const at = extractArrival(text);
    if (at) state.arrivalBy = at;
    if (d.tools_used) {
      const n = d.tools_used.length;
      const pill = $("loc-btn");
      if (pill) pill.title = `answered by ${d.engine || "rules"} · ${n} tool${n === 1 ? "" : "s"}`;
    }
  }

  function provenance(d) {
    const tools = (d.tools_used || []).length;
    const bits = [];
    if (d.engine === "llm") {
      bits.push(`Answered by <b>${Views.esc(d.model || "local model")}</b> via Ollama`);
    } else if (d.engine === "rules") {
      // Two very different situations share one engine: "no model configured, none
      // needed" and "a model was configured and could not be used". Do not blur them.
      bits.push(d.fallback_reason
        ? "Answered by the <b>deterministic planner</b> — the local model was not used"
        : "Answered by the <b>deterministic planner</b> (no model needed)");
    } else {
      return "";
    }
    if (tools) bits.push(`${tools} tool${tools === 1 ? "" : "s"}`);
    if (d.latency_ms) bits.push(`${(d.latency_ms / 1000).toFixed(1)} s`);
    const audit = d.price_audit || {};
    if (audit.ok === true && audit.checked) {
      bits.push(`all ${audit.checked} fare${audit.checked === 1 ? "" : "s"} traced to a tool`);
    }
    let html = `<span class="dot"></span>${bits.join(" · ")}`;
    if (d.fallback_reason) {
      html += `<div class="why">${Views.icon("alert")} ${Views.esc(d.fallback_reason)}</div>`;
    }
    return html;
  }

  function extractOD(text) {
    const t = text.replace(/\s+/g, " ").trim();
    let m = t.match(/\bfrom\s+([^,.?]+?)\s+to\s+([^,.?]+)/i);
    if (m) return { origin: tidyPlace(m[1]), destination: tidyPlace(m[2]) };
    m = t.match(/\b(?:route|way|go|get|travel)\b[^]*?\bto\s+([^,.?]+)/i);
    if (!m) m = t.match(/\bto\s+([A-Z][\w\s.]+)/);
    if (m) {
      const dest = tidyPlace(m[1]);
      const home = state.prefs.default_origin || "";
      if (home) return { origin: home, destination: dest };
      return null;
    }
    return null;
  }

  function tidyPlace(s) {
    return String(s || "")
      .replace(/\b(tomorrow|today|tonight|at|by|around|before)\b.*$/i, "")
      .replace(/\b\d{1,2}(:\d{2})?\s*(am|pm)?\b/gi, "")
      .replace(/\s+/g, " ").trim().replace(/[,.;]$/, "");
  }

  function extractArrival(text) {
    const m = String(text).match(/\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b/i);
    if (!m) return "";
    let h = parseInt(m[1], 10);
    const min = m[2] ? parseInt(m[2], 10) : 0;
    const pm = /pm/i.test(m[3]);
    if (pm && h < 12) h += 12;
    if (!pm && h === 12) h = 0;
    return `${String(h).padStart(2, "0")}:${String(min).padStart(2, "0")}`;
  }

  /* ---------------------------------------------------------------- saved / history */
  async function loadSaved() {
    const res = await API.saved();
    if (!res.ok) return;
    const rows = res.data.saved_routes || res.data || [];
    Views.renderSaved($("saved-list"), rows);
    if ($("n-saved")) $("n-saved").textContent = rows.length;
  }

  async function onSavedClick(e) {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    if (btn.dataset.act === "del") {
      const r = await API.deleteSaved(id);
      if (r.ok) { Views.toast("Deleted"); loadSaved(); } else Views.toast(r.error);
    }
    if (btn.dataset.act === "load") {
      const r = await API.getSaved(id);
      if (!r.ok) { Views.toast(r.error); return; }
      const s = r.data;
      if ($("origin")) $("origin").value = s.origin;
      if ($("destination")) $("destination").value = s.destination;
      show("chat");
      planTrip(s.origin, s.destination);
    }
  }

  async function loadHistory() {
    const res = await API.history();
    if (!res.ok) return;
    Views.renderHistory($("hist-kpis"), $("hist-list"), res.data.stats, res.data.entries);
    if ($("n-history")) $("n-history").textContent = res.data.stats.trips_logged;
  }

  /* ---------------------------------------------------------------- explore */
  async function loadExplore() {
    const [lines, ksrtc] = await Promise.all([API.lines(), API.ksrtc()]);

    if (lines.ok) {
      const d = lines.data;
      const op = (d.lines || []).filter((l) => l.routable);
      const pending = (d.lines || []).filter((l) => !l.routable);
      const stations = (d.lines || []).reduce((n, l) => n + (l.station_count || 0), 0);
      $("explore-kpis").innerHTML = `
        <div class="kpi"><div class="v">${stations}</div>
          <div class="l">Metro stations on the ${d.count} lines</div></div>
        <div class="kpi"><div class="v">${op.length}</div>
          <div class="l">In service: ${op.map((l) => l.name).join(", ") || "—"}</div></div>
        <div class="kpi"><div class="v">${pending.length}</div>
          <div class="l">Under construction: ${pending.map((l) => l.name).join(", ") || "—"}</div></div>`;
      state.lines = d.lines || [];
      Views.renderMetroNetwork($("explore-lines"), d, state.openLines);
    } else {
      $("explore-lines").innerHTML =
        `<div class="banner err">${Views.icon("alert")}<div>${Views.esc(lines.error)}</div></div>`;
    }

    if (ksrtc.ok) Views.renderKsrtc($("explore-ksrtc"), ksrtc.data);
    else $("explore-ksrtc").innerHTML =
      `<div class="banner err">${Views.icon("alert")}<div>${Views.esc(ksrtc.error)}</div></div>`;
  }

  function onExploreClick(e) {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const lineId = btn.dataset.line;
    const line = (state.lines || []).find((l) => l.line_id === lineId);

    if (btn.dataset.act === "toggle-stations") {
      if (!state.openLines) state.openLines = new Set();
      if (state.openLines.has(lineId)) state.openLines.delete(lineId);
      else state.openLines.add(lineId);
      const card = btn.closest(".line-card");
      if (card) {
        card.classList.toggle("open", state.openLines.has(lineId));
        btn.innerHTML = `${Views.icon("grid")}${
          state.openLines.has(lineId) ? "Hide" : "Show"} stations`;
      }
      return;
    }

    if (btn.dataset.act === "draw-line") {
      if (!line) return;
      const res = window.CampusMap ? CampusMap.focusLine(lineId) : null;
      if (!res) {
        Views.toast("The map cannot draw a line with this renderer");
        return;
      }
      const pending = line.status !== "operational";
      Views.toast(`${line.name} Line · ${line.station_count} stations${pending
        ? " · under construction, not in service" : ""}`);
      scrollMapIntoView();
    }
  }

  async function nearby() {
    const lat = parseFloat($("near-lat").value);
    const lng = parseFloat($("near-lng").value);
    if (!isFinite(lat) || !isFinite(lng)) { Views.toast("Enter a latitude and longitude"); return; }
    const r = await API.nearbyStops(lat, lng, 0.6);
    if (!r.ok) { $("near-result").innerHTML = `<div class="banner err">${Views.esc(r.error)}</div>`; return; }
    const stops = r.data.stops || [];
    $("near-result").innerHTML = stops.length
      ? `<div class="card-panel"><table class="tbl"><thead><tr><th>Stop</th><th>Distance</th>
         <th>Stop ID</th></tr></thead><tbody>${stops.map((s) => `<tr>
         <td>${Views.esc(s.name)}</td><td class="mono">${s.distance_km} km</td>
         <td class="mono mut">${Views.esc(s.stop_id)}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty">No GTFS stop within 600 m of that point.</div>`;
  }

  /* ---------------------------------------------------------------- settings */
  async function loadSettings() {
    const [integrations, sources, prefs] = await Promise.all([
      API.integrations(), API.dataSources(false), API.prefs(),
    ]);
    if (integrations.ok) {
      state.integrations = integrations.data;
      Views.renderIntegrations($("integrations-list"), integrations.data, {
        toast: (msg) => Views.toast(msg, 4000),
        reload: loadSettings,
      });
    } else {
      Views.renderIntegrations($("integrations-list"), null);
    }
    if (sources.ok) Views.renderSources($("sources-list"), sources.data);
    if (prefs.ok) applyPrefs(prefs.data);
  }

  function applyPrefs(p) {
    state.prefs = p;
    const sel = $("pref-default");
    if (sel && !sel.options.length) {
      ["balanced", "fastest", "cheapest", "fewest_transfers", "least_walking"]
        .forEach((k) => sel.add(new Option(k.replace(/_/g, " "), k)));
    }
    if (sel) sel.value = p.default_preference || "balanced";
    if ($("pref-origin")) $("pref-origin").value = p.default_origin || "";
    if ($("pref-map")) $("pref-map").value = p.map_provider || "auto";
    if ($("pref-theme")) $("pref-theme").value = state.theme;
    if ($("pref-traffic")) {
      const cap = (state.caps || []).find((c) => c.key === "google_traffic");
      const can = !cap || cap.available;
      $("pref-traffic").classList.toggle("on", p.show_traffic_overlay === "true" && can);
      $("pref-traffic").disabled = !can;
      $("pref-traffic").style.opacity = can ? "1" : ".45";
      $("pref-traffic").title = can ? "Toggle the road-traffic overlay"
        : "No traffic provider configured — set GOOGLE_MAPS_SERVER_API_KEY";
      const lyr = $("lyr-traffic");
      if (lyr) { lyr.disabled = !can; $("lyr-traffic-lbl").classList.toggle("off", !can); }
    }
    if (p.default_origin && $("origin") && !$("origin").value) $("origin").value = p.default_origin;
  }

  async function savePref(patch) {
    const res = await API.savePrefs(patch);
    if (res.ok) { applyPrefs(res.data); Views.toast("Saved"); }
    else Views.toast("Could not save: " + res.error);
  }

  /* ---------------------------------------------------------------- boot */
  // -------------------------------------------------------------- live buses
  // The only polling loop in the app, and it is deliberately conservative:
  //
  //   * the server caches the feed, so this interval is the *minimum* the provider sees;
  //   * a hidden tab stops fetching instead of quietly draining someone's quota;
  //   * "no feed configured" ends the loop rather than retrying a 200 that will never change;
  //   * an upstream failure backs off to 60 s and says so in the strip.
  //
  // Nothing here draws a bus that the server did not return. With no authorised feed the
  // layer stays empty and the strip reads "Unavailable".
  const live = { on: false, timer: null, inFlight: false, fails: 0 };

  const LIVE_LABEL = {
    off: "Live buses", loading: "Checking…", live: "Live", delayed: "Delayed data",
    unavailable: "Unavailable",
  };

  function liveChip(state, text) {
    const strip = $("live-strip");
    const chip = $("live-chip");
    const txt = $("live-text");
    if (strip) strip.hidden = false;
    if (chip) {
      chip.dataset.state = state;
      chip.textContent = LIVE_LABEL[state] || state;
    }
    if (txt) txt.textContent = text || "";
  }

  function liveSchedule(ms) {
    if (live.timer) clearTimeout(live.timer);
    live.timer = live.on ? setTimeout(liveTick, ms) : null;
  }

  function liveStop() {
    live.on = false;
    if (live.timer) { clearTimeout(live.timer); live.timer = null; }
    if (window.CampusMap) CampusMap.clearLiveVehicles();
  }

  async function liveTick() {
    if (!live.on || live.inFlight) return;
    if (document.hidden) { liveSchedule(5000); return; }   // come back when visible
    live.inFlight = true;
    let res;
    try {
      res = await API.vehiclePositions({ limit: 800 });
    } finally {
      live.inFlight = false;
    }
    if (!live.on) return;

    const d = (res && res.data) || {};

    if (res && res.ok && d.available) {
      live.fails = 0;
      const drawn = window.CampusMap ? CampusMap.setLiveVehicles(d.vehicles || []) : 0;
      const refresh = Math.max(15, Number(d.refresh_after_s) || 20);
      if (!d.count) {
        liveChip("unavailable",
          `The feed is readable (${d.source || "source"}) but returned no vehicles. ` +
          "Nothing is being hidden or estimated.");
      } else if (d.live_count > 0) {
        liveChip("live",
          `${d.live_count} of ${d.count} buses reporting within ` +
          `${Math.round(d.stale_after_s || 120)}s` +
          (d.stale_count ? `, ${d.stale_count} delayed` : "") +
          ` · ${d.source} · updates every ${refresh}s`);
      } else {
        liveChip("delayed",
          `${d.count} buses, none reporting within ` +
          `${Math.round(d.stale_after_s || 120)}s — these are old positions, not current ones`);
      }
      live._drawn = drawn;
      liveSchedule(refresh * 1000);
      return;
    }

    if (res && res.ok && !d.available) {
      // No authorised source. Say so once and stop: retrying cannot create a feed.
      const why = d.reason || "No live feed is configured.";
      liveChip("unavailable", why.length > 240 ? why.slice(0, 237) + "…" : why);
      liveSchedule(120000);
      return;
    }

    // Configured but broken (502) or unreachable. This is an upstream problem and the
    // strip is allowed to say so; the scheduled routes keep working regardless.
    live.fails += 1;
    const why = (res && (res.error || (d && d.reason))) || "Live feed unavailable.";
    liveChip("unavailable", why.length > 240 ? why.slice(0, 237) + "…" : why);
    liveSchedule(60000);
  }

  function setupLiveBuses() {
    const box = $("lyr-buses");
    if (!box) return;
    box.addEventListener("change", (e) => {
      if (!e.target.checked) {
        liveStop();
        const strip = $("live-strip");
        if (strip) strip.hidden = true;
        return;
      }
      const renderer = window.CampusMap ? CampusMap.getRenderer() : null;
      if (typeof CampusMap === "undefined" || renderer === "svg" || !renderer) {
        e.target.checked = false;
        Views.toast("Live positions need the OpenStreetMap or Google basemap");
        return;
      }
      live.on = true;
      liveChip("loading", "Reading the vehicle feed…");
      liveTick();
    });

    // Returning to the tab refreshes immediately instead of waiting out the interval.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && live.on) {
        if (live.timer) { clearTimeout(live.timer); live.timer = null; }
        liveTick();
      }
    });
  }

  async function boot() {
    // theme before anything paints
    let saved = null;
    try { saved = localStorage.getItem("ct-theme"); } catch { /* ignore */ }
    applyTheme(saved || "light");
    if (window.innerWidth <= 980) document.getElementById("app").classList.add("sidebar-collapsed");

    $("nav").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-view]");
      if (b) show(b.dataset.view);
    });
    $("theme-btn").addEventListener("click", () => applyTheme(state.theme === "dark" ? "light" : "dark"));
    $("menu-btn").addEventListener("click", () =>
      document.getElementById("app").classList.toggle("sidebar-collapsed"));
    $("scrim").addEventListener("click", () =>
      document.getElementById("app").classList.add("sidebar-collapsed"));
    $("loc-btn").addEventListener("click", () => { show("explore"); });
    $("profile-btn").addEventListener("click", () => { show("settings"); });

    // autocomplete on both form pairs
    ["", "-2"].forEach((sfx) => {
      Views.bindAutocomplete($("origin" + sfx), $("ac-origin" + sfx), (n) => {
        $("origin" + sfx).value = n;
        const other = $("origin" + (sfx ? "" : "-2"));
        if (other) other.value = n;
      });
      Views.bindAutocomplete($("destination" + sfx), $("ac-destination" + sfx), (n) => {
        $("destination" + sfx).value = n;
        const other = $("destination" + (sfx ? "" : "-2"));
        if (other) other.value = n;
      });
    });

    const submit = (e) => { e.preventDefault(); planTrip(); };
    $("route-form").addEventListener("submit", submit);
    if ($("route-form-2")) $("route-form-2").addEventListener("submit", submit);

    const swap = () => {
      const a = $("origin").value, b = $("destination").value;
      $("origin").value = b; $("destination").value = a;
      if ($("origin-2")) $("origin-2").value = b;
      if ($("destination-2")) $("destination-2").value = a;
    };
    $("swap-btn").addEventListener("click", swap);
    if ($("swap-btn-2")) $("swap-btn-2").addEventListener("click", swap);

    // mode filters (two sets, kept in sync)
    document.querySelectorAll(".mode-row").forEach((row) => {
      row.addEventListener("click", (e) => {
        const b = e.target.closest(".mode-pill");
        if (!b) return;
        state.mode = b.dataset.mode;
        document.querySelectorAll(".mode-pill").forEach((p) =>
          p.classList.toggle("on", p.dataset.mode === state.mode));
        if (state.query) planTrip(state.query.origin, state.query.destination);
      });
    });

    // result tabs
    $("result-tabs").addEventListener("click", (e) => {
      const b = e.target.closest("[data-sort]");
      if (!b) return;
      state.sort = b.dataset.sort;
      document.querySelectorAll("#result-tabs .tab").forEach((t) => t.classList.toggle("on", t === b));
      if (state.itineraries && state.itineraries.length) {
        Views.renderPlan($("results-list"), { ok: true, itineraries: state.itineraries },
          { sort: state.sort, active: 0 });
        refreshMap();
        selectItinerary(0);
      }
    });

    $("results-list").addEventListener("click", onResultsClick);
    $("results-list").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        const card = e.target.closest(".rcard");
        if (card) { e.preventDefault(); selectItinerary(+card.dataset.idx); }
      }
    });

    $("saved-list").addEventListener("click", onSavedClick);

    // map layer toggles
    const layer = (id, name) => {
      const box = $(id);
      if (!box) return;
      box.addEventListener("change", () => {
        if (name === "busstops" && box.checked) CampusMap.loadBusStops();
        const ok = window.CampusMap ? CampusMap.setLayer(name, box.checked) : false;
        if (!ok && box.checked) {
          box.checked = false;
          Views.toast(name === "traffic"
            ? "No traffic provider configured — nothing to draw"
            : "That layer is not available with the current basemap");
        }
      });
    };
    layer("lyr-routes", "routes");
    layer("lyr-metro", "metro");
    layer("lyr-busstops", "busstops");
    layer("lyr-traffic", "traffic");
    setupLiveBuses();

    if ($("near-btn")) $("near-btn").addEventListener("click", nearby);
    if ($("explore-lines")) $("explore-lines").addEventListener("click", onExploreClick);

    // chat
    $("send").addEventListener("click", send);
    $("input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });
    $("input").addEventListener("input", (e) => {
      e.target.style.height = "auto";
      e.target.style.height = Math.min(150, e.target.scrollHeight) + "px";
    });

    // settings
    if ($("pref-default")) {
      $("pref-default").addEventListener("change", (e) => savePref({ default_preference: e.target.value }));
      $("pref-origin").addEventListener("change", (e) => savePref({ default_origin: e.target.value.trim() }));
      $("pref-map").addEventListener("change", (e) => {
        savePref({ map_provider: e.target.value })
          .then(() => Views.toast("Reload the page to apply the new basemap"));
      });
      $("pref-theme").addEventListener("change", (e) => {
        applyTheme(e.target.value);
        savePref({ theme: e.target.value });
      });
      $("pref-traffic").addEventListener("click", () =>
        savePref({ show_traffic_overlay: !$("pref-traffic").classList.contains("on") }));
    }

    // deep link
    try {
      const u = new URL(window.location.href);
      const from = u.searchParams.get("from"), to = u.searchParams.get("to");
      if (from) { $("origin").value = from; if ($("origin-2")) $("origin-2").value = from; }
      if (to) { $("destination").value = to; if ($("destination-2")) $("destination-2").value = to; }
      if (from && to) setTimeout(() => planTrip(from, to), 500);
    } catch { /* ignore */ }

    // background
    Views.trafficPanel($("traffic-body"), null);
    const [health, caps, examples] = await Promise.all([
      API.health(), API.capabilities(), API.examples(),
    ]);
    if (health.ok) {
      const c = health.data.counts || {};
      const pill = $("loc-btn");
      if (pill) pill.title = `${(c.stops || 0).toLocaleString()} BMTC stops · ` +
        `${(c.metro_stops || 0)} metro stations · ${(c.places || 0).toLocaleString()} places`;
    }
    if (caps.ok) state.caps = caps.data.providers || [];
    if (examples.ok && $("chips")) {
      $("chips").innerHTML = "";
      (examples.data.examples || []).slice(0, 5).forEach((q) => {
        const b = document.createElement("button");
        b.className = "chip";
        b.type = "button";
        b.textContent = q;
        b.addEventListener("click", () => { show("chat"); $("input").value = q; send(); });
        $("chips").appendChild(b);
      });
    }

    await Promise.all([
      API.prefs().then((r) => { if (r.ok) applyPrefs(r.data); }),
      loadSaved(),
      loadHistory(),
    ]);
  }

  async function refreshCounts() {
    const [s, h] = await Promise.all([API.saved(), API.history()]);
    if (s.ok) {
      const rows = s.data.saved_routes || s.data || [];
      if ($("n-saved")) $("n-saved").textContent = rows.length;
      if (state.view === "saved") Views.renderSaved($("saved-list"), rows);
    }
    if (h.ok && $("n-history")) $("n-history").textContent = h.data.stats.trips_logged;
  }

  return { boot, show, planTrip, state, extractOD, extractArrival, applyTheme, refreshCounts };
})();

window.App = App;

document.addEventListener("DOMContentLoaded", () => {
  if (window.CampusMap) CampusMap.init();
  App.boot();
});
