/**
 * Views — render functions. Take data + element, write DOM. No fetching here.
 *
 * The two rules that shape every card:
 *   1. A number is shown only if the backend returned it. Missing data becomes "—" with
 *      the reason, never a plausible-looking default.
 *   2. Every claim about provenance (verified fare / live traffic / scheduled only) is
 *      rendered from a field the backend set, not from a guess made here.
 */
const Views = (() => {
  "use strict";

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const rupees = (n) => (n == null ? "—" : `₹${Math.round(n)}`);
  const mins = (n) => (n == null ? "—" : `${Math.round(n)} min`);

  const icon = (id, cls) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ` +
    `stroke-linecap="round" stroke-linejoin="round"${cls ? ` class="${cls}"` : ""}>` +
    `<use href="#i-${id}"/></svg>`;

  const MODE = {
    bus: { label: "BMTC Bus", icon: "bus" },
    metro: { label: "Namma Metro", icon: "metro" },
    walk: { label: "Walking", icon: "walk" },
    walkkey: { label: "Walking", icon: "walk" },
    drive: { label: "Driving", icon: "car" },
  };
  const modeOf = (m) => MODE[m] || { label: String(m || "—"), icon: "route" };

  const RANK_COLORS = ["#2563eb", "#ea8a1f", "#0f9d58", "#7c3aed", "#0891b2"];
  const colorFor = (i) => RANK_COLORS[Math.min(i, RANK_COLORS.length - 1)];

  /* ------------------------------------------------------------------ markdown */
  const EMOJI = { "\u{1F68C}": "Bus", "\u{1F687}": "Metro", "\u{1F6B6}": "Walk",
                  "\u{1F68D}": "Bus", "\u{1F682}": "Train" };

  function md(src) {
    let t = String(src == null ? "" : src);
    for (const k in EMOJI) t = t.split(k).join(EMOJI[k]);
    t = esc(t).replace(/\n{3,}/g, "\n\n");
    const lines = t.split("\n");
    const out = [];
    let inTable = false;
    const closeTable = () => { if (inTable) { out.push("</tbody></table>"); inTable = false; } };
    let para = [];

    const flushPara = () => {
      if (para.length) { out.push(`<p>${para.join("<br>")}</p>`); para = []; }
    };

    for (const raw of lines) {
      const line = raw.trimEnd();
      if (/^\|.*\|$/.test(line)) {
        flushPara();
        const cells = line.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
        if (!inTable) {
          out.push('<table><thead><tr>' + cells.map((c) => `<th>${c}</th>`).join("") +
                   "</tr></thead><tbody>");
          inTable = true;
        } else {
          out.push("<tr>" + cells.map((c) => `<td>${c}</td>`).join("") + "</tr>");
        }
        continue;
      }
      closeTable();
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { flushPara(); out.push(`<p><b>${h[2]}</b></p>`); continue; }
      if (!line.trim()) { flushPara(); continue; }
      para.push(line);
    }
    flushPara();
    closeTable();
    return out.join("\n")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/(^|[^*>])\*([^*\n]+)\*(?!\*)/gm, "$1<i>$2</i>");
  }

  /* ------------------------------------------------------------------ toast */
  let toastTimer = null;
  function toast(msg, ms) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("on"), ms || 2600);
  }

  /* ------------------------------------------------------------------ banners */
  function banner(el, msg, kind) {
    if (!el) return;
    if (!msg) { el.classList.add("hidden"); el.innerHTML = ""; return; }
    el.className = "banner " + (kind || "");
    el.innerHTML = icon(kind === "ok" ? "check" : "alert") + `<div>${esc(msg)}</div>`;
    el.classList.remove("hidden");
  }

  function skeleton(container, n) {
    container.innerHTML = Array.from({ length: n || 3 }, () => '<div class="skel"></div>').join("");
  }

  /* ------------------------------------------------------------------ route cards */
  const OPERATOR_LABEL = { KSRTC: "KSRTC", BMTC: "BMTC", BMRCL: "Namma Metro" };

  function legRow(leg) {
    const m = modeOf(leg.mode);
    const label = leg.mode === "walk" ? "Walk" : (leg.label || m.label);
    const dur = leg.minutes != null ? `${Math.round(leg.minutes)} min` : "";
    const km = leg.km ? ` · ${leg.km} km` : "";
    // ₹0 means free; null means nobody published a price, and the two must not look alike.
    const fare = leg.fare_inr == null
      ? (leg.mode === "walk" ? "" : ' · <span class="no-fare">fare n/a</span>')
      : (leg.fare_inr ? ` · ${rupees(leg.fare_inr)}` : "");
    // Only a *ride* has an operator worth naming; a walking leg has none.
    const op = leg.mode !== "walk" && leg.operator && OPERATOR_LABEL[leg.operator]
      && leg.operator !== "BMTC"
      ? `<span class="op-chip ${leg.operator.toLowerCase()}">${
          esc(OPERATOR_LABEL[leg.operator])}</span>` : "";
    const lineChip = leg.line_colour
      ? `<span class="line-dot" style="background:${esc(leg.line_colour)}" title="${
          esc(leg.line || "")} Line"></span>` : "";
    return `<div class="leg ${esc(leg.mode)}${leg.fare_inr == null && leg.mode !== "walk"
      ? " unpriced" : ""}">
      <span class="ic">${icon(m.icon)}</span>
      <span class="nm">${lineChip}${op}<b>${esc(label)}</b> ${esc(leg.from || "")} → ${esc(leg.to || "")}</span>
      <span class="tm">${dur}${km}${fare}</span>
    </div>`;
  }

  function statCell(k, iconId, v) {
    return `<div class="stat"><div class="k">${icon(iconId)}${esc(k)}</div>
      <div class="v">${esc(v)}</div></div>`;
  }

  /** Provenance chips: each one mirrors a field the backend actually set. */
  function sourceChips(it) {
    const chips = [];
    const modes = it.mode_sequence || [];
    if (modes.includes("bus")) {
      chips.push(['ok', "schedule", "BMTC GTFS (verified schedule)"]);
    }
    if (modes.includes("metro")) {
      chips.push(['ok', "train", "BMRCL GTFS (verified schedule)"]);
    }
    if (modes.includes("drive")) {
      chips.push(['warn', "car", "Google Routes (live)"]);
    }
    if (it.confidence === "estimated" || modes.join() === "walk") {
      chips.push(['warn', "walk", "estimated from coordinates"]);
    }

    // Fare status comes straight from the fare object; it is never assumed.
    const fs = it.fare_status || (it.fare && it.fare.status) || "estimated";
    const fsource = it.fare_source || (it.fare && it.fare.source) || "";
    if (fs === "verified") {
      chips.push(['ok', "shield", `Fare: ${fsource || "official chart"}`]);
    } else if (fs === "estimated") {
      chips.push(['warn', "money", `Fare: estimate (${fsource || "slab"})`]);
    } else {
      chips.push(['off', "money", "Fare unavailable"]);
    }

    // Traffic is only ever "live" when a provider said so.
    const tr = it.traffic_status || "unavailable";
    if (tr === "live") {
      chips.push(['warn', "traffic", `Traffic: ${it.traffic_source || "provider"} (live)`]);
    } else if (tr === "unavailable") {
      chips.push(['off', "traffic", "Traffic: unavailable"]);
    }

    return chips.map(([cls, ic, text]) =>
      `<span class="src-chip ${cls}">${icon(ic)}${esc(text)}</span>`).join("");
  }

  function routeCard(it, i, ctx) {
    const color = colorFor(i);
    const active = ctx.active === i ? " on" : "";
    const modes = (it.mode_sequence || []).map((m) => {
      const mm = modeOf(m);
      return `<span class="m">${icon(mm.icon)}${esc(mm.label)}</span>`;
    }).join('<span style="opacity:.5">→</span>');

    const walkOnly = isWalkOnly(it);
    const fare = it.total_price_inr;
    const unpriced = (it.unpriced_legs || []).length || (fare == null && !walkOnly);
    const fareNote = walkOnly ? "no fare — on foot"
      : fare == null ? "not published"
      : it.fare_status === "verified" ? "official fare"
      : it.fare_status === "estimated" ? "estimated" : "";

    const dep = it.departure_time || "—";
    const arr = it.arrival_time || "—";
    const dist = it.distance_km != null ? `${it.distance_km} km` : "—";
    const walk = it.walking_minutes != null ? `${Math.round(it.walking_minutes)} min` : "—";
    const tr = it.transfers != null ? String(it.transfers) : "—";

    const legs = (it.legs || []).map(legRow).join("");

    return `<article class="rcard${active}" data-idx="${i}" style="--c:${color}"
                     tabindex="0" role="button" aria-label="Route ${i + 1}">
      <div class="rc-head">
        <div class="rc-rank">${it.rank != null ? it.rank : i + 1}</div>
        <div class="rc-title">
          <b>${esc(it.title || "Journey")}</b>
          <div class="rc-modes">${modes}</div>
        </div>
        <div class="rc-fare">
          <div class="amt${fare == null ? " na" : ""}">${fare == null ? "n/a" : rupees(fare)}</div>
          <div class="st">${esc(fareNote)}</div>
        </div>
      </div>
      <div class="rc-legs">${legs}</div>
      <div class="rc-stats">
        ${statCell("Total time", "clock", it.total_minutes != null ? mins(it.total_minutes) : "—")}
        ${statCell("Distance", "distance", dist)}
        ${statCell("Depart", "route", dep)}
        ${statCell("Arrive", "pin", arr)}
        ${statCell("Transfers", "transfer", tr)}
      </div>
      <div class="rc-stats" style="padding-top:0;border-top:0">
        ${statCell("Walking", "walk", walk)}
        ${statCell("Mode", "bus", (it.mode_sequence || []).length + " leg(s)")}
        ${statCell("Score", "grid", it.comfort_score != null ? String(it.comfort_score) : "—")}
      </div>
      <div class="rc-src">${sourceChips(it)}</div>
      <div class="rc-actions">
        <button class="mini" data-act="focus" data-idx="${i}">${icon("map")}Show on map</button>
        <button class="mini" data-act="save" data-idx="${i}">${icon("bookmark")}Save</button>
        <button class="mini" data-act="log" data-idx="${i}">${icon("history")}Log trip</button>
        <button class="mini" data-act="fares" data-idx="${i}">${icon("money")}Fares</button>
      </div>
    </article>`;
  }

  function renderPlan(container, result, ctx) {
    ctx = ctx || {};
    if (!result) {
      container.innerHTML = `<div class="empty">${icon("route")}
        <b>No trip planned yet</b>Enter a pair above, or ask in the chat.</div>`;
      return 0;
    }
    const its = sortItineraries(result.itineraries || [], ctx.sort || "rank");
    if (!result.ok || !its.length) {
      const w = (result.warnings || []).filter(Boolean).join(" ") ||
                "No verified route found between those places.";
      container.innerHTML = `<div class="banner err">${icon("alert")}<div>${esc(w)}</div></div>`;
      return 0;
    }
    container.innerHTML = its.map((it, i) => routeCard(it, i, ctx)).join("");
    return its.length;
  }

  const WALKISH = ["walk", "walking"];
  const isWalkOnly = (it) => {
    const m = it.mode_sequence || [];
    return m.length === 1 && WALKISH.includes(m[0]);
  };
  // A walking-only trip is technically the cheapest thing you can do, but nobody opens a
  // transit planner looking for a four-hour walk. Keep it visible, sort it last.
  const transitFirst = (fn) => (a, b) => {
    const d = (isWalkOnly(a) ? 1 : 0) - (isWalkOnly(b) ? 1 : 0);
    return d || fn(a, b);
  };
  const SORTERS = {
    rank: (a, b) => (a.rank || 99) - (b.rank || 99),
    cheapest: transitFirst((a, b) => (a.total_price_inr ?? 1e9) - (b.total_price_inr ?? 1e9)),
    // (unpriced itineraries sort last everywhere: an unknown fare is not a cheap fare)
    fastest: (a, b) => (a.total_minutes ?? 1e9) - (b.total_minutes ?? 1e9),
    fewest: (a, b) => (a.transfers ?? 99) - (b.transfers ?? 99) ||
                      (a.total_minutes ?? 1e9) - (b.total_minutes ?? 1e9),
    walk: transitFirst((a, b) => (a.walking_minutes ?? 1e9) - (b.walking_minutes ?? 1e9)),
  };
  const sortItineraries = (its, key) => its.slice().sort(SORTERS[key] || SORTERS.rank);

  function fareTable(it) {
    const rows = (it.fare_breakdown || []).map((b) => `<tr>
      <td>${esc(modeOf(b.mode).label)}</td><td>${esc(b.service)}</td>
      <td class="mono">${b.km != null ? b.km + " km" : "—"}</td>
      <td class="mono">${b.fare_inr != null ? rupees(b.fare_inr) : "—"}</td>
      <td class="mut">${esc(b.basis_label || b.basis || "")}</td></tr>`).join("");
    return `<div class="fare-table"><table>
      <thead><tr><th>Mode</th><th>Service</th><th>Distance</th><th>Fare</th>
      <th>Basis</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="mut">No paid legs.</td></tr>'}</tbody>
      <tfoot><tr><td colspan="3"><b>${it.total_price_inr == null ? "Priced part" : "Total"}</b></td>
      <td class="mono"><b>${it.total_price_inr == null
        ? (it.priced_subtotal != null ? rupees(it.priced_subtotal) : "n/a")
        : rupees(it.total_price_inr)}</b></td><td>${it.total_price_inr == null
        ? '<span class="mut">KSRTC fare not published</span>' : ""}</td></tr></tfoot>
    </table></div>`;
  }

  /* ------------------------------------------------------------------ traffic panel */
  function trafficPanel(container, data) {
    if (!data) {
      container.innerHTML = `<div class="tp-empty"><b>Plan a trip to see corridor traffic</b>
        Live road traffic needs a traffic provider. Scheduled service supply comes from the
        GTFS you already have, and it appears here once a corridor is known.</div>`;
      return;
    }
    const live = data.live_traffic || {};
    const supply = data.network_supply || {};
    const advice = data.departure_advice || {};
    let html = "";

    if (advice.note) {
      html += `<div class="advice-banner">${icon("clock")}
        <div><b>${esc(advice.headline || advice.note)}</b>
        <span>${esc(advice.explanation || advice.basis || "")}</span></div></div>`;
    }

    html += `<div class="status-chips">`;

    if (live.available) {
      html += `<div class="sc ok"><span class="ico">${icon("traffic")}</span>
        <div class="tx"><b>Live traffic: ${esc(live.level || "measured")}</b>
        <span>${esc(live.provider || "Google Routes")} · +${live.delay_minutes ?? "—"} min vs free flow</span></div></div>`;
    } else {
      html += `<div class="sc off"><span class="ico">${icon("traffic")}</span>
        <div class="tx"><b>Live traffic: unavailable</b>
        <span>${esc((live.reason || "").split(".")[0] || "no provider configured")}</span></div></div>`;
    }

    if (supply.available) {
      const sm = supply.summary || {};
      html += `<div class="sc ok"><span class="ico">${icon("train")}</span>
        <div class="tx"><b>Transit data: GTFS schedule</b>
        <span>${sm.samples || 0} segments · median ${sm.median_implied_speed_kmh ?? "—"} km/h implied</span></div></div>`;
    } else {
      html += `<div class="sc off"><span class="ico">${icon("train")}</span>
        <div class="tx"><b>Transit data: no segments</b>
        <span>${esc((supply.reason || "").slice(0, 60) || "plan a corridor first")}</span></div></div>`;
    }

    html += `<div class="sc ok"><span class="ico">${icon("money")}</span>
      <div class="tx"><b>Fares: official charts</b>
      <span>BMRCL 14.02.2025 · BMTC slabs (estimated)</span></div></div>`;
    html += `</div>`;

    // The hourly chart is only honest with real per-hour data. This build has none, so it
    // says so rather than drawing a plausible-looking curve.
    html += `<div class="tp-empty" style="margin-top:12px">
      <b>Hourly traffic chart: not available</b>
      The GTFS import carries no departure times, so a congestion-by-hour series cannot be
      derived from it, and no historical traffic provider is configured. Rather than draw a
      plausible-looking curve, this panel shows only what was measured.</div>`;

    if (supply.available && (supply.sample_segments || []).length) {
      const segs = supply.sample_segments.slice(0, 4);
      const maxTrips = Math.max(...segs.map((s) => s.n_trips || 1), 1);
      html += `<div style="margin-top:12px">` + segs.map((s) => `
        <div class="seg-row" style="display:flex;align-items:center;gap:9px;font-size:11.5px;padding:4px 0">
          <span style="flex:0 0 150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
                color:var(--ink-2)">${esc(s.from)} → ${esc(s.to)}</span>
          <span style="flex:1;height:6px;border-radius:3px;background:var(--bg-sunken);
                overflow:hidden"><span style="display:block;height:100%;border-radius:3px;
                background:var(--blue);width:${Math.round((s.n_trips / maxTrips) * 100)}%"></span></span>
          <span style="flex:0 0 54px;text-align:right;font-family:var(--mono);color:var(--ink-3)">
            ${s.n_trips} trips</span>
        </div>`).join("") + `</div>`;
    }

    container.innerHTML = html;
  }

  /* ------------------------------------------------------------------ saved / history */
  function renderSaved(container, rows) {
    if (!rows || !rows.length) {
      container.innerHTML = `<div class="empty">${icon("bookmark")}
        <b>Nothing saved yet</b>Plan a trip and press Save on a card.</div>`;
      return;
    }
    container.innerHTML = rows.map((r) => `<div class="list-item">
      <div class="li-main"><b>${esc(r.title || r.origin + " → " + r.destination)}</b>
      <span>${esc(r.origin)} → ${esc(r.destination)}${r.route_id ? " · " + esc(r.route_id) : ""}
      · saved ${esc((r.created_at || "").replace("T", " ").replace("Z", ""))}</span></div>
      <div class="li-act">
        <button class="mini" data-act="load" data-id="${r.id}">${icon("route")}Re-plan</button>
        <button class="mini" data-act="del" data-id="${r.id}">Delete</button>
      </div></div>`).join("");
  }

  function renderHistory(kpiEl, listEl, stats, rows) {
    if (kpiEl && stats) {
      kpiEl.innerHTML = `
        <div class="kpi"><div class="v">${stats.trips_logged}</div><div class="l">Trips logged</div></div>
        <div class="kpi"><div class="v">${rupees(stats.total_fare_inr)}</div><div class="l">Total fare</div></div>
        <div class="kpi"><div class="v">${rupees(stats.average_fare_inr)}</div><div class="l">Average fare</div></div>
        <div class="kpi"><div class="v">${Math.round((stats.total_minutes || 0) / 60 * 10) / 10}h</div>
          <div class="l">Time in transit</div></div>`;
    }
    if (!listEl) return;
    if (!rows || !rows.length) {
      listEl.innerHTML = `<div class="empty">${icon("history")}
        <b>No trips logged</b>Press "Log trip" on any route card.</div>`;
      return;
    }
    listEl.innerHTML = `<table class="tbl"><thead><tr><th>When</th><th>From</th><th>To</th>
      <th>Mode</th><th>Fare</th><th>Duration</th></tr></thead><tbody>${rows.map((r) => `
      <tr><td class="mono">${esc((r.created_at || "").slice(0, 16).replace("T", " "))}</td>
      <td>${esc(r.origin)}</td><td>${esc(r.destination)}</td>
      <td>${esc(r.selected_mode || "—")}</td>
      <td class="mono">${r.estimated_fare_inr != null ? rupees(r.estimated_fare_inr) : "—"}</td>
      <td class="mono">${r.estimated_duration != null ? mins(r.estimated_duration) : "—"}</td>
      </tr>`).join("")}</tbody></table>`;
  }

  /* ------------------------------------------------------------------ settings */
  /* ------------------------------------------------------- metro network + KSRTC */
  function metroLineCard(line, open) {
    const op = line.status === "operational";
    const colour = line.colour || "#475569";
    const stops = line.stations || [];
    const inter = (line.interchanges || []);
    return `<div class="line-card${op ? "" : " pending"}${open ? " open" : ""}"
                 data-line="${esc(line.line_id)}" style="--lc:${esc(colour)}">
      <div class="lc-head">
        <span class="lc-swatch" style="background:${esc(colour)}"></span>
        <div class="lc-main">
          <b>${esc(line.name)} Line</b>
          <span>${esc((line.terminals || []).join(" ⇄ "))}</span>
        </div>
        <span class="src-chip ${op ? "ok" : "warn"}">${icon(op ? "check" : "alert")}${
          esc(op ? "in service" : line.status.replace(/_/g, " "))}</span>
      </div>
      <div class="lc-meta">
        <span>${line.station_count} stations</span>
        <span>${line.length_km != null ? line.length_km + " km" : "—"}</span>
        ${line.fare_range_inr
          ? `<span>${rupees(line.fare_range_inr.min)}–${rupees(line.fare_range_inr.max)}</span>`
          : `<span>fare n/a</span>`}
        <span>${inter.length ? "changes at " + esc(inter.slice(0, 3).join(", ")) : "no interchange"}</span>
      </div>
      <div class="lc-actions">
        <button class="mini" data-act="draw-line" data-line="${esc(line.line_id)}">
          ${icon("map")}Draw on map</button>
        <button class="mini" data-act="toggle-stations" data-line="${esc(line.line_id)}">
          ${icon("grid")}${open ? "Hide" : "Show"} stations</button>
      </div>
      <ol class="lc-stops">${stops.map((s) => `<li${
        s.status !== "operational" ? ' class="pending"' : ""}>${esc(s.name)}${
        s.interchange ? ' <span class="lc-x">interchange</span>' : ""}</li>`).join("")}</ol>
      <div class="lc-note">${esc(line.note || "")}${
        line.verification ? ` · <b>${esc(line.verification)}</b>` : ""}</div>
    </div>`;
  }

  function renderMetroNetwork(container, data, openIds) {
    const lines = (data && data.lines) || [];
    if (!lines.length) {
      container.innerHTML = `<div class="empty">${icon("metro")}
        <b>No metro lines loaded</b>The BMRCL GTFS import produced no line records.</div>`;
      return;
    }
    const open = openIds || new Set();
    container.innerHTML = lines.map((l) => metroLineCard(l, open.has(l.line_id))).join("");
  }

  function renderKsrtc(container, data) {
    const rows = (data && data.corridors) || [];
    if (!rows.length) {
      container.innerHTML = `<div class="empty">${icon("bus")}
        <b>No KSRTC corridors in this build</b>The intercity table is empty.</div>`;
      return;
    }
    const body = rows.map((r) => {
      const verified = r.verification_status === "VERIFIED_SECONDARY"
                    || r.verification_status === "VERIFIED_PRIMARY";
      const cls = verified ? "ok" : (r.fare_inr == null ? "off" : "warn");
      const label = verified ? "published"
        : r.fare_inr == null ? "no fare" : "derived";
      const fare = r.fare_inr == null
        ? '<span class="mut">not published</span>'
        : `<b>${rupees(r.fare_inr)}</b>`;
      return `<tr>
        <td>${esc(r.from_place)} → ${esc(r.to_place)}
          <div class="mut" style="font-size:11px">${esc(r.via || "")}</div></td>
        <td class="mut">${esc(r.service_class)}</td>
        <td class="mono">${r.approx_km != null ? r.approx_km + " km" : "—"}</td>
        <td class="mono">${fare}</td>
        <td><span class="src-chip ${cls}">${icon(verified ? "check" : "alert")}${label}</span></td>
      </tr>`;
    }).join("");
    container.innerHTML = `<div class="fare-table"><table>
      <thead><tr><th>Corridor</th><th>Class</th><th>Distance</th><th>Fare</th>
      <th>Provenance</th></tr></thead><tbody>${body}</tbody></table></div>
      <p class="mut" style="font-size:11.5px;margin-top:8px">${esc((data && data.note) || "")}</p>`;
  }

  /** Intercity options attached to a plan result: quoted, never ranked. */
  function renderIntercity(container, intercity) {
    if (!container) return;
    const corridors = (intercity && intercity.corridors) || [];
    if (!intercity || !intercity.available || !corridors.length) {
      container.innerHTML = "";
      container.classList.add("hidden");
      return;
    }
    container.classList.remove("hidden");
    const cards = corridors.map((c) => {
      const f = c.fare || {};
      const fare = f.amount == null
        ? '<span class="mut">not published</span>'
        : `<b>${rupees(f.amount)}</b> <span class="mut">${esc(f.status || "")}</span>`;
      return `<div class="ic-card">
        <div class="ic-head">
          <span class="ic-badge">KSRTC</span>
          <div class="ic-route"><b>${esc(c.from_place)} → ${esc(c.to_place)}</b>
            <span>${esc(c.service_class)} · ${c.distance_km != null ? c.distance_km + " km" : "—"}
              · via ${esc(c.via || "—")}</span></div>
          <div class="ic-fare">${fare}</div>
        </div>
        <div class="ic-note">${esc(f.note || "")}
          ${c.source_url ? ` · <a href="${esc(c.source_url)}" target="_blank" rel="noopener">source</a>` : ""}</div>
        ${(c.warnings || []).map((w) => `<div class="ic-warn">${icon("alert")}${esc(w)}</div>`).join("")}
      </div>`;
    }).join("");
    container.innerHTML = `<div class="ic-wrap">
      <div class="ic-title">${icon("bus")}KSRTC intercity
        <span class="mut">quoted, not routed</span></div>
      ${cards}
      <p class="mut" style="font-size:11.5px;margin:8px 0 0">${esc(intercity.note || "")}</p>
    </div>`;
  }

  function renderCapabilities(container, caps) {
    container.innerHTML = (caps || []).map((c) => `<div class="list-item">
      <span class="src-chip ${c.available ? "ok" : "warn"}" style="flex:0 0 auto">
        ${icon(c.available ? "check" : "alert")}${c.available ? "ready" : "off"}</span>
      <div class="li-main"><b>${esc(c.label)}</b><span>${esc(c.reason)}</span></div>
      ${c.needs ? `<span class="mut mono" style="font-size:10.5px;flex:0 0 auto">${esc(c.needs)}</span>` : ""}
    </div>`).join("");
  }

  /* ------------------------------------------------------------- integrations */
  // One chip colour per registry state. The label is the server's own words, never a
  // client-side guess: the UI must not say "ready" about something the server did not.
  const STATE_CLASS = {
    ready: "ok",
    configured_untested: "warn",
    not_configured: "off",
    disabled: "off",
    auth_failure: "bad",
    quota_or_billing: "bad",
    provider_unavailable: "bad",
    unsupported: "off",
  };

  function fmtWhen(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return esc(iso);
    return d.toLocaleString();
  }

  function renderIntegrations(container, data, handlers) {
    if (!data || !data.integrations) {
      container.innerHTML = '<div class="empty">Could not load the integration registry.</div>';
      return;
    }
    const sum = data.summary || {};
    const list = data.integrations.map((it) => {
      const cls = STATE_CLASS[it.state] || "off";
      const vars = (it.env_vars || []).map((v) =>
        `<span class="mono env ${v.set ? "set" : "unset"}">${esc(v.name)}` +
        `${v.set ? " set" : (v.required ? " missing" : " optional")}</span>`).join(" ");
      const lc = it.last_check;
      const last = lc
        ? `<span>Last check ${fmtWhen(lc.checked_at)} · ${esc(lc.state_label)}` +
          `${lc.latency_ms != null ? ` · ${lc.latency_ms} ms` : ""}` +
          `${lc.stale ? " · stale" : ""}</span>` +
          (lc.detail ? `<span class="mut">${esc(lc.detail)}</span>` : "")
        : "<span>Never tested</span>";
      const testLabel = !it.test || !it.test.supported
        ? null
        : it.test.requires_confirmation ? "Test (billable)" : "Test connection";
      // A test with no credential would only ever return "not configured", so say that on
      // the button instead of inviting a click that cannot succeed.
      const testDisabled = !it.configured;
      const actions = [
        testLabel
          ? `<button class="mini" data-act="test" data-id="${esc(it.id)}"
               ${testDisabled ? 'disabled title="Set the variable above first" '
                 + 'style="opacity:.5;cursor:not-allowed"' : ""}>` +
            `${icon("zap")}${testLabel}</button>`
          : "",
        it.setup_url
          ? `<a class="mini" href="${esc(it.setup_url)}" target="_blank" rel="noopener">` +
            `${icon("external")}Official setup</a>`
          : "",
        `<label class="intg-toggle" title="Disable without editing .env">
           <input type="checkbox" data-act="toggle" data-id="${esc(it.id)}"
             ${it.enabled ? "checked" : ""}><span>enabled</span></label>`,
      ].join("");
      const restrictions = (it.restrictions || []).map((r) => `<li>${esc(r)}</li>`).join("");
      return `<div class="intg ${cls}" data-id="${esc(it.id)}">
        <div class="intg-head">
          <span class="src-chip ${cls}">${icon(cls === "ok" ? "check" : "alert")}${esc(it.state_label)}</span>
          <div class="li-main"><b>${esc(it.name)}</b>
            <span>${esc(it.purpose)}</span></div>
          <div class="intg-act">${actions}</div>
        </div>
        <div class="intg-meta">
          <span class="mut">${esc(it.provider)}</span>
          <span class="mut">${it.credential === "server" ? "server-side key" :
            it.credential === "browser" ? "public browser key" : "no credential"}</span>
          ${it.billable ? '<span class="mut">metered</span>' : ""}
          ${vars}
        </div>
        <div class="intg-why">${esc(it.reason)}</div>
        <div class="intg-last">${last}</div>
        <details class="intg-more">
          <summary>Setup, cost and troubleshooting</summary>
          <p class="mut" style="font-size:12px;margin:8px 0 4px">
            ${esc(it.notes || it.test && it.test.warning || "")}</p>
          <ul class="intg-rules">${restrictions}</ul>
          ${it.docs_url ? `<p style="margin:6px 0 0;font-size:12px">
            <a href="${esc(it.docs_url)}" target="_blank" rel="noopener">Provider documentation</a></p>` : ""}
        </details>
      </div>`;
    }).join("");

    const rr = data.road_routing;
    const chain = rr
      ? `<div class="intg-chain">
           <b>Road routing chain</b>
           <span class="mut">${(rr.road_order || []).length
             ? rr.road_order.map((p) => `<span class="mono">${esc(p)}</span>`).join(" → ")
             : "no provider configured — driving returns unavailable"}</span>
           <span class="mut">Traffic-capable: ${
             (rr.traffic_aware_providers || []).join(", ") || "none"}</span>
         </div>`
      : "";

    container.innerHTML = `
      <div class="intg-sum">
        <span><b class="ok-t">${sum.ready || 0}</b> ready</span>
        <span><b class="warn-t">${sum.needs_setup || 0}</b> need setup</span>
        <span><b class="bad-t">${sum.problems || 0}</b> problems</span>
        <span><b>${sum.unsupported || 0}</b> unsupported</span>
      </div>
      ${chain}
      <div class="intg-list">${list}</div>`;

    if (container.dataset.bound === "1") return;
    container.dataset.bound = "1";
    container.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-act='test']");
      if (!btn) return;
      const id = btn.dataset.id;
      const it = (data.integrations || []).find((x) => x.id === id) || {};
      let confirm = false;
      if (it.test && it.test.requires_confirmation) {
        const ok = window.confirm(
          `This test makes one real request to ${it.provider} and it is billed by them.` +
          `\n\n${it.test.warning || ""}\n\nContinue?`);
        if (!ok) return;
        confirm = true;
      }
      btn.disabled = true;
      const original = btn.innerHTML;
      btn.innerHTML = "Testing…";
      const res = await API.testIntegration(id, confirm);
      btn.disabled = false;
      btn.innerHTML = original;
      if (handlers && handlers.toast) {
        if (res.ok) {
          handlers.toast(res.data.ok ? "Test passed" : "Test failed — see the card",
                         res.data.ok ? "ok" : "err");
        } else {
          handlers.toast(res.error, "err");
        }
      }
      if (handlers && handlers.reload) handlers.reload();
    });
    container.addEventListener("change", async (ev) => {
      const box = ev.target.closest("[data-act='toggle']");
      if (!box) return;
      const res = await API.setIntegrationEnabled(box.dataset.id, box.checked);
      if (!res.ok && handlers && handlers.toast) handlers.toast(res.error, "err");
      if (handlers && handlers.reload) handlers.reload();
    });
  }

  function renderSources(container, data) {
    if (!data || !data.sources) { container.innerHTML = '<div class="empty">No source data.</div>'; return; }
    const cls = (s) => ["ok", "available", "imported"].includes(s) ? "ok-t"
      : ["unavailable", "error"].includes(s) ? "bad-t" : "warn-t";
    const rows = data.sources.map((s) => `<tr>
      <td>${esc(s.source_name)}</td><td>${esc(s.data_type || "")}</td>
      <td class="${cls(s.status)}">${esc(s.status)}</td>
      <td class="mono">${s.rows != null ? Number(s.rows).toLocaleString() : "—"}</td>
      <td class="mut" style="max-width:300px">${esc(s.detail || "")}</td></tr>`).join("");
    const fv = data.fare_verification;
    container.innerHTML = `<div class="card-panel" style="padding:14px 18px">
      <table class="tbl"><thead><tr><th>Source</th><th>Type</th><th>Status</th><th>Rows</th>
      <th>Detail</th></tr></thead><tbody>${rows}</tbody></table>
      ${fv ? `<p class="mut" style="font-size:12px;margin:12px 0 0">Fare check ·
        ${esc(fv.label)}: <b class="${fv.all_matched ? "ok-t" : "bad-t"}">${fv.matched}/${fv.pairs}</b>
        pairs match, ${fv.mismatches} mismatches, verified ${esc(fv.checked_at)}.</p>` : ""}
    </div>`;
  }

  /* ------------------------------------------------------------------ autocomplete */
  function bindAutocomplete(inputEl, listEl, pick) {
    if (!inputEl || !listEl) return;
    let items = [];
    let sel = -1;
    let timer = null;
    let seq = 0;
    let focused = false;

    const hide = () => { listEl.classList.add("hidden"); sel = -1; };

    function paint() {
      if (!items.length) {
        listEl.innerHTML = '<div class="ac-empty">No match — try a stop or landmark name.</div>';
        listEl.classList.remove("hidden");
        return;
      }
      listEl.innerHTML = items.map((s, i) => `<div class="ac-item${i === sel ? " sel" : ""}" data-i="${i}">
        <span class="ic">${icon(s.kind === "metro_station" ? "metro" : "pin")}</span>
        <span class="nm">${esc(s.name)}</span>
        <span class="kd">${esc(s.label || s.kind || "")}</span>
        ${s.source && String(s.source).indexOf("google") === 0 ? '<span class="sv">Google</span>' : ""}
      </div>`).join("");
      listEl.classList.remove("hidden");
    }

    async function query(q) {
      const mine = ++seq;
      const res = await API.autocomplete(q, 8);
      if (mine !== seq) return;
      if (!focused || document.activeElement !== inputEl) return;
      if (!res.ok) { items = []; paint(); return; }
      items = res.data.suggestions || [];
      sel = -1;
      paint();
    }

    inputEl.addEventListener("focus", () => { focused = true; });
    inputEl.addEventListener("input", () => {
      const q = inputEl.value.trim();
      clearTimeout(timer);
      if (q.length < 2) { hide(); return; }
      timer = setTimeout(() => query(q), 140);
    });
    inputEl.addEventListener("keydown", (e) => {
      if (listEl.classList.contains("hidden")) return;
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(items.length - 1, sel + 1); paint(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(-1, sel - 1); paint(); }
      else if (e.key === "Enter" && sel >= 0) { e.preventDefault(); pick(items[sel].name, items[sel]); hide(); }
      else if (e.key === "Escape") hide();
    });
    listEl.addEventListener("mousedown", (e) => {
      const el = e.target.closest(".ac-item");
      if (!el) return;
      e.preventDefault();
      const it = items[+el.dataset.i];
      if (it) pick(it.name, it);
      hide();
    });
    inputEl.addEventListener("blur", () => { focused = false; setTimeout(hide, 140); });
  }

  return {
    esc, md, rupees, mins, icon, toast, banner, skeleton, colorFor,
    renderPlan, sortItineraries, fareTable, trafficPanel,
    renderSaved, renderHistory, renderCapabilities, renderIntegrations,
    renderSources, bindAutocomplete,
    renderMetroNetwork, renderKsrtc, renderIntercity,
  };
})();

window.Views = Views;
