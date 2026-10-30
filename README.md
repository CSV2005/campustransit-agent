# CampusTransit Agent

A question-answering agent for Bengaluru campus commuting: **BMTC bus + Namma Metro routing,
student-pass advice, and fare maths** for places like Dayananda Sagar University Harohalli,
DSCE, DSATM, Christ, IIMB and the Kanakapura Road corridor.

> **Price honesty, up front.** Verification differs by mode, and the agent says which is
> which on every answer.
>
> | Domain | Status |
> |---|---|
> | **Namma Metro fares** | **VERIFIED_PRIMARY** — BMRCL's own Revised Fare Chart (14.02.2025) PDF, downloaded from `bmrc.co.in/tickets/` and reproduced against all 6,806 GTFS pairs |
> | **Namma Metro rules** | **VERIFIED_PRIMARY** — BMRCL Fare Rules page (penalties, luggage, smart-card discounts, helpdesk) |
> | BMTC / KSRTC pass prices | VERIFIED_SECONDARY — press coverage of the 9 Jan 2025 revision and the 12 Jun 2026 free-pass order; the official portals were unreachable |
> | BMTC bus fares | distance-slab **estimate** unless the GTFS feed carries a route-scoped fare for that stop pair (108 of 4,434 routes) |
> | KSRTC monthly season ticket | `UNVERIFIED_PLACEHOLDER` — stored as `null`, never printed as a number |
>
> See §10.

---

## 1. What this is

The agent answers three kinds of question and refuses a fourth:

| Works | Example |
|---|---|
| Route planning | "Best route and pass from Jayanagar to DSU Harohalli?" → route 213-VD, board Jayanagara 38th Cross, alight BWSSB Quaters Harohalli, ₹32, 7 trips/day |
| Fare + pass maths | "I spend ₹60 a day, 24 days a month — is a monthly pass worth it?" → ₹64/day, ₹1,536/month, free student pass saves ₹1,536 |
| Rules and eligibility | "What documents do I need?" / "Does Shakti work on AC buses?" → answers quoted from the policy corpus with sources |
| **Refuses** | "Tell me the BMTC general manager's direct line" → *"I don't have a verified phone number for that… I will not invent one."* |

Core design rule, taken from the build guide: **numbers never come from the language model.**
Fares, distances, monthly costs and pass prices are computed in Python from SQLite. The
vector index only ever holds prose — pass cards, policy documents, FAQs and route descriptions.
A post-hoc **price audit** re-reads every `₹` figure in the final answer and fails the response
if any amount cannot be found in a tool observation.

## 2. Architecture

```
                    ┌─────────────────────────────┐
 user question ───▶ │  agent.py  (LLM or planner) │
                    └──────────┬──────────────────┘
                               │ tool calls (JSON args)
        ┌──────────────────────┼──────────────────────────────┐
        ▼                      ▼                              ▼
  structured tools        routing tools                   retrieval tools
  estimate_fare         find_stop / find_routes         search_policy / search_dataset
  estimate_monthly_cost plan_metro_leg / metro_info     (Chroma + MiniLM)
  recommend_pass        plan_optimal_route  ← Dijkstra/A*
                        compare_routing_algorithms ← +RRT
                        plan_walk_connector ← RRT*
        │                      │                              │
        ▼                      ▼                              ▼
   transit.db + FTS5     route_segment / metro_segment   Chroma source + policy corpus
   fare_matrix, passes   (scheduled minutes per link)      curated/raw files and routes
```

* **Hybrid by construction.** Structured numeric data is never vectorised. `search_policy`
  even filters `doc_type` so a fare question cannot be answered from a paragraph.
* **Two agents, one interface.** `CampusTransitAgent` tries a local Ollama model with exactly
  the 15 tools in `registry.py`. If Ollama is unreachable (it is, in this sandbox) it falls
  back to `rules_agent.py`, a deterministic planner/composer that calls the *same* tools in
  the *same* order and composes the answer from observations only.
* **Every tool returns** `{ok, tool, summary, data, sources}`. `summary` is what the LLM sees;
  `data` is what the composer and the audit use.

## 3. Data: D1–D4

| ID | Dataset | Source used | Rows ingested | Status |
|---|---|---|---|---|
| **D1** | BMTC GTFS | `github.com/Vonter/bmtc-gtfs` (mirror of the DULT Transport Data Hub feed, scraped from the Namma BMTC app); feed valid 2026-09-07 → 2027-09-07 | 9,960 stops · 4,434 routes · 57,836 trips · 1,540,686 stop_times · 170,161 fare rules | ✅ |
| **D2** | BMRCL / Namma Metro GTFS **+ Pink Line + official fare chart** | GTFS from `github.com/Vonter/bmrcl-gtfs` (valid 2026-08-17 → 2027-08-17); **official fare chart PDF and fare-rules page from bmrc.co.in (primary source)**; curated Reach 6 file for the Pink Line, which the feed does not carry because it is not open | 504 stations · **4 lines** · 9,702 fare cells · 30 official fare slabs · 14 official rules | ✅ |
| **D3** | OSM Bengaluru + Kanakapura corridor | Overpass API — POIs around the campuses, all bus stops in the southern corridor | 2,779 POIs · 319 bus-stop nodes | ✅ |
| **D4** | data.gov.in Karnataka transport | **Blocked** (HTTP 503 / timeout / DNS failure — see `data/raw/dataset_manifest.json → failed_attempts`). Replaced with a curated KSRTC corridor table | 4 corridor rows (KBS→Kanakapura ₹124 published; KBS→Harohalli ₹86 derived; express `not_published`; KBS→Mysuru ₹162) | ⚠️ substituted, disclosed |

`src/make_manifest.py` writes `data/raw/dataset_manifest.json` with provider, URL, licence,
row counts, feed validity and a `caveat` string for every dataset, plus the list of failed
attempts so the D4 gap is auditable rather than silent.

**D4 substitution detail.** `data.gov.in` never resolved from the sandbox. Rather than drop
D4 (the guide requires all four), the corridor table was built from the KSRTC fare table
republished after the 5 Jan 2025 revision, with each row labelled `published`,
`derived_interpolation` or `not_published`. The two derived rows carry the formula in a
`notes` field so a reviewer can recompute them.

## 4. Repository layout

```
campustransit-agent/
├── build.py                   one command: download GTFS → transit.db → corpus → index
├── run.py                     one command: serve the app (any browser, any OS)
├── app.py                     Streamlit UI (guide's prescribed interface)
├── web/
│   ├── server.py              FastAPI: /api/health, /api/chat, /api/tools, /api/examples
│   └── index.html             single-file streaming UI (live preview)
├── data/
│   ├── raw/gtfs/{bmtc,bmrcl}  D1, D2 as delivered
│   ├── raw/bmrcl_official/    BMRCL fare chart PDF + extracted fare_chart.json
│   ├── raw/osm/               D3 Overpass payloads + .ql queries used
│   ├── raw/dataset_manifest.json
│   ├── curated/
│   │   ├── passes.json        8 passes, guide schema + source_url/last_verified
│   │   ├── metro_pink_line.json  18 Pink Line stations (coords, section, phase, sources)
│   │   ├── places.json        14 campuses with coordinates and nearest stop ids
│   │   ├── ksrtc_corridor.csv D4 substitute
│   │   ├── policy_docs/       6 long-form documents
│   │   └── faq.md             40 question/answer pairs
│   └── processed/
│       ├── transit.db         ~180 MB, 28 tables (incl. route_segment / metro_segment)
│       ├── corpus.jsonl       733 documents
│       ├── chroma/            persistent collection `campustransit`
│       ├── index_meta.json
│       ├── eval_results.json  per-question eval output
│       └── eval_report.md     human-readable eval table
└── src/
    ├── config.py              paths + embedder/LLM settings
    ├── ingest_gtfs.py  ingest_places.py  ingest_ksrtc.py  ingest_metro_pink.py
    ├── build_corpus.py  build_index.py  make_manifest.py  ingest_routing_graph.py
    ├── ingest_bmrcl_official.py   BMRCL fare chart PDF -> metro_fare_slabs
    ├── routing/               graph.py (corridor graph), search.py (Dijkstra+A*),
    │                          rrt.py (RRT / RRT*), fares.py (marginals + repricing)
    ├── tools/                 common, route_tool, fare_tool, pass_tool, rag_tool,
    │                          plan_tool, ksrtc_tool, registry
    ├── rules_agent.py         deterministic planner/composer + price audit
    ├── agent.py               CampusTransitAgent (LLM → rules fallback)
    └── eval/                  eval_set.json (41), retrieval_eval.json (10),
                                run_eval.py, benchmark_routing.py
```

## 5. Setup

Two commands from a fresh clone — one to install, one to build the data:

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python build.py                                       # downloads the GTFS, builds transit.db,
                                                      # the corpus and the vector index (~2 min)
python run.py --open                                  # http://localhost:7860
```

Optional: `cp .env.example .env` for `LLM_MODEL`, `OLLAMA_BASE_URL`, `EMBED_BACKEND`.

`EMBED_BACKEND` accepts `chroma-default` (MiniLM ONNX, downloads ~80 MB on first use) or a
local `sentence-transformers` model name. Python 3.11+ is required; this build was made on
3.13.16.

## 6. Build the data layer

Run from the repository root, in order:

```bash
python -m src.ingest_gtfs      # D1 + D2 → stops, routes, trips, stop_times, route_stops,
                               #          fare_matrix, metro_* tables   (~4 min)
python -m src.ingest_places    # D3 + curated campuses → places (13,071 rows)
python -m src.ingest_ksrtc     # D4 substitute → ksrtc_corridor
python -m src.ingest_metro_pink # Pink Line → metro_stops/routes/fare_matrix + line status
python -m src.ingest_bmrcl_official  # BMRCL fare chart PDF → metro_fare_slabs + rules,
                               #          re-verifies all 6,806 published pairs
python -m src.ingest_routing_graph   # GTFS stop_times → route_segment / metro_segment
                               #          (scheduled minutes + km per stop pair, ~16 s)
python -m src.build_corpus     # curated + readable raw/processed source files → corpus.jsonl
python -m src.export_qwen_sft # source-grounded Qwen chat-format SFT examples
python -m src.build_index      # embed + persist Chroma collection
python -m src.build_data_index # all non-private transit DB records → local FTS5 index
python -m src.make_manifest    # provenance + row counts
```

`search_dataset` exposes a local search tool to the assistant. It retrieves source-file
chunks from Chroma and transit records from the SQLite FTS5 index; exact route, stop, and
fare answers should continue to use their dedicated structured tools. PDF sources need
the `pypdf` dependency. Raster previews are represented by their companion vector/source
documents when available. Model weights and Chroma implementation files are not treated
as knowledge documents, and private saved-route/history/preferences tables are excluded.
This indexes searchable data rather than copying all files into the LLM prompt.
The optional `qwen_sft.jsonl` output is only a source-grounded fine-tuning data export;
it does not train or alter the Ollama model. Actual Qwen fine-tuning needs a supported
training runtime/adapter conversion workflow (typically a CUDA GPU); Ollama serves the
resulting model but is not a training framework.

`route_stops` flattens `stop_times ⋈ trips` into `(route_id, direction_id, stop_id, seq)` and
is indexed on `(route_id, direction_id, seq)` and `(stop_id)`. `route_stops_full` is the view
that joins names back in. `fare_matrix` collapses route-scoped and global fare rules into
`(route_id, from_zone, to_zone, fare_inr)`.

`route_segment` (205,354 rows) and `metro_segment` (164 rows) are the edge lists the
graph routers use: mean scheduled run time and stop-to-stop distance for every
(route, direction, stop pair), aggregated across all 1.54 M BMTC `stop_times` rows.
They are separate from `route_stops` on purpose — `route_stops` collapses trip variants
and carries no clock, which is exactly what a search needs and an enumerator does not.

## 7. Tools

| Tool | Arguments | Returns |
|---|---|---|
| `find_stop` | `query` | matched stop/place with lat-lon, confidence, nearest stops |
| `find_routes` | `origin`, `destination` | direct routes, one-transfer itineraries, walk + frequency ranking |
| `plan_metro_leg` | `origin`, `destination` | board/alight stations, line(s), stops, interchange, token fare |
| `metro_info` | `place`, `from_line` | line serving a place, termini, interchange to change lines |
| `metro_fare` | `from_station`, `to_station` | fare between two named stations, across lines, hop count, fare basis |
| `metro_line_details` | `line` | every station on a line, service status, opening date, fares from the terminus |
| `estimate_fare` | `origin`, `destination`, `mode` | fare with `fare_basis` + `verification_status` |
| `estimate_monthly_cost` | `one_way_fare_inr`, `trips_per_day`, `travel_days_per_month` | day/month totals, rides |
| `get_pass` | `pass_id` | one curated pass card: price, validity, coverage, eligibility, source |
| `recommend_pass` | monthly cost, `needs_ac`, `is_woman…`, `is_student`, `airport_travel` | ranked passes, savings, why |
| `search_policy` | `query`, `doc_type`, `k` | passages + metadata + sources (never numbers) |
| `plan_optimal_route` | `origin`, `destination`, `objective`, `algorithm` | optimal path by Dijkstra or A*, per-leg fares repriced from the published tables, nodes expanded |
| `compare_routing_algorithms` | `origin`, `destination`, `objective` | Dijkstra vs A* vs RRT: cost agreement, expansions, RRT's gap from optimal |
| `plan_walk_connector` | `origin`, `destination` | RRT* walking polyline, length, detour ratio, `OBSTACLE_AWARE` / `OPEN_FIELD` |
| `get_ksrtc_options` | `place` | corridor rows with fare basis and verification status |

Fare resolution order: route-specific GTFS fare stage (0.95 confidence) → global zone-pair
rule (0.80) → distance-slab estimate (0.60). Every result carries
`verification_status ∈ {VERIFIED_SECONDARY, UNVERIFIED_DERIVED, UNVERIFIED_PLACEHOLDER}`
and a `fare_basis` string, and the composer prints that basis next to the number.

## 8. Run the app

```bash
python run.py                 # http://localhost:7860
python run.py --port 8080     # any free port; auto-picks the next one if taken
python run.py --open          # also opens a browser tab
python run.py --host 127.0.0.1  # bind to loopback only
```

`run.py` checks that the data layer exists before it starts and tells you the exact
command to rebuild it if not, so a missing database can never turn into a blank page.

**It runs in any browser, on any OS, with no build step.** The UI is one hand-written
HTML file, one stylesheet and four plain `<script>` files — no React, no bundler, no CDN,
no Google Fonts. Leaflet is vendored and its CSS is inlined into the page. That is
deliberate: it renders identically in Chrome, Safari, Firefox, Edge and a phone browser,
it works offline, and it survives a sandboxed iframe with no network access.

The layout is a light three-column shell — navigation, a work column, and a map that is
already drawn when the page opens — with a dark theme behind one toggle. Below 980 px it
collapses to one column with the sidebar as a drawer and the same map as a full-width band,
so the map is never the thing that gets dropped.

It binds to `0.0.0.0`, so a phone on the same Wi-Fi can open the `Network` address
`run.py` prints at startup. To keep it local, pass `--host 127.0.0.1`.

| Endpoint | What it does |
|---|---|
| `/` | the dashboard shell (New Chat, Plan, Explore, Saved, History, Settings) |
| `/api/health` | engine, row counts, index metadata, tool list |
| `/api/chat` | `POST {message, profile}` → `{answer, trace, tools_used, price_audit, latency_ms}` |
| `/api/tools` · `/api/examples` | tool catalogue, sample questions |
| `/docs` | FastAPI's auto-generated API explorer |

The full endpoint set is listed in §9e.

The equivalent raw command, if you would rather not use the launcher:

```bash
uvicorn web.server:app --host 0.0.0.0 --port 7860
```

There is also a Streamlit interface (`streamlit run app.py --server.port 8501`) that talks
to the same agent, kept because the original build guide prescribed Streamlit.

Both UIs expose the same agent. Each answer is followed by the real tool trace: tool name,
arguments, latency and an observation excerpt, plus the price-audit verdict.

API:

```bash
curl -s localhost:7860/api/health | jq
curl -s -X POST localhost:7860/api/chat \
  -H 'content-type: application/json' \
  -d '{"message":"Best route and pass from Jayanagar to DSU Harohalli?",
       "profile":{"travel_days":24,"is_student":true}}' | jq '.answer, .tools_used, .price_audit'
```

## 9. Evaluation

```bash
python -m src.eval.run_eval      # → data/processed/eval_results.json + eval_report.md
```

41 golden questions (`src/eval/eval_set.json`) covering routing, pass maths, documents,
Shakti, AC passes, KSRTC, metro (including the Pink Line, official fare rules and two
negative checks), free-pass rules and one deliberate hallucination trap; plus 10 retrieval queries (`retrieval_eval.json`)
with graded source targets.

Latest run (`engine=rules`, Ollama unavailable in the sandbox):

| Metric | Result | Target |
|---|---|---|
| Tool-selection accuracy (all expected tools called) | **100 %** (41/41) | ≥ 85 % |
| Content accuracy (required facts present, forbidden facts absent) | **100 %** (41/41) | ≥ 80 % |
| Price-audit pass rate (no ₹ figure absent from tool output) | **100 %** | 100 % |
| Retrieval hit@4 | **100 %** (10/10) | ≥ 80 % |
| Average tool calls per question | **2.0** | ≤ 5 (efficiency) |
| Average end-to-end latency | **0.51 s** | < 25 s |

**Read these numbers with the right scepticism.** The eval set and the agent were written by
the same person, and two gold labels (`q03`, `q23`) were corrected during the build because
the original expectation was wrong, not the answer. The 100 % figures mean *no regressions
against a self-authored checklist*; they are not an independent benchmark. Two answers were
inspected by hand at each change (metro fare, policy retrieval) because a substring check
cannot tell a coherent paragraph from a pile of sentences.

**Second pass (after the Pink Line work) caught four more classes of bug, all fixed:**

* **Answers with no tool call.** A question naming a specific pass ("how much is the
  Vayu Vajra Gold?") was answered from the pass card without touching a tool, so the
  price audit had nothing to verify against and silently failed. Added `get_pass`, which
  every named-pass answer now routes through.
* **The chunker deleted a whole document.** A rewrite dropped any paragraph block that
  began with a heading line — but in these docs the body sits directly under the heading
  with no blank line, so `05_applying_and_troubleshooting.md` produced *zero* chunks and
  the agent could not answer "how do I apply on Seva Sindhu". Caught by a coverage check
  on corpus counts per file.
* **Table rows were being thrown away.** `_clean_chunk` skipped any line starting with
  `|`, which is exactly where the student-pass fee table lives, so "what is the ITI pass
  fee?" could never surface ₹1,300.
* **Lexical relevance punished good answers.** The best answer to "what documents do I
  need" never uses the word *documents*. Replaced the overlap rule with IDF-weighted
  coverage plus three acceptance paths (body term, strong vector hit, two-term heading
  match), and made the vector score the primary ranking signal.

What the harness actually caught during the build, and what was fixed:

* `estimate_fare` returning amounts the answer never used → audit now scans full `data`.
* "₹-0 more per month" printed when there was no pay-as-you-go baseline.
* `find_routes` miscounting transfer hops (absolute `seq` instead of `d.seq - x.seq`).
* Destination-first parsing ("How do I reach DSU from Banashankari") swallowing intent words
  and producing `destination="goes"`; the extractor now rejects any span containing an
  intent token.
* The policy composer splicing unrelated sentences; it now quotes FAQ chunks whole and
  returns an explicit *"I don't have a verified answer"* when no passage clears the
  relevance bar.

### How the primary sources were obtained

`bmrc.co.in` is a Next.js app whose content is rendered client-side, and its `/fare/` path is
a 404. The route in was:

1. `/tickets/` links a PDF: `Revised_Fare_Chart_for_webiste.pdf`, served from
   `english.bmrc.co.in:8282`. Saved to `data/raw/bmrcl_official/`, parsed with `pdfplumber`
   into `fare_chart.json` (30 rows).
2. The fare-rules text lives in a bundled `language/english.json` webpack module inside
   `_next/static/chunks/pages/fare-rules.js`. Extracted and written out as
   `data/curated/policy_docs/07_bmrcl_official_fares.md`.
3. The site's private API host (`106.51.122.122:8282`) is unreachable from the build
   sandbox, which is why the page had to be read from the bundle rather than the API.

`python -m src.ingest_bmrcl_official` re-runs steps 1–2 and re-verifies the whole matrix.

## 9b. Metro lines and the fare model

**Four lines are modelled; three carry passengers.** The Explore view lists all four with
their stations in travel order, and `/api/transit/lines` returns the same thing
machine-readably. `routable: false` on Pink is enforced in the router, not just in the
label: no Pink station can be boarded, and the map draws it dashed.

| Line | Termini | Stations | Status |
|---|---|---|---|
| Green | Madavara – Silk Institute | 32 | operational |
| Purple | Whitefield (Kadugodi) – Challaghatta | 37 | operational |
| Yellow | Rashtreeya Vidyalaya Road – Delta Electronics Bommasandra | 16 | operational |
| **Pink** | **Kalena Agrahara – Nagawara** | **18** | **under construction** |

The Pink Line is in the database because it is the corridor that serves Bannerghatta Road,
JP Nagar, IIMB, Dairy Circle, MG Road, Shivajinagar and Nagawara — but **it is not open**.
The 7.5 km elevated section (Kalena Agrahara – Tavarekere, 6 stations) finished its final
CMRS safety inspection on 8 October 2026 and is awaiting authorisation, with passenger
services expected late October or early November 2026; the 13.76 km underground section
(Dairy Circle – Nagawara, 12 stations) is targeted for early 2027. The agent therefore
**never plans a journey on it**. It will quote the projected fare and station list, and say
plainly that no train runs there yet.

**How fares are computed — and now officially confirmed.** The fare is not distance-based.
BMRCL's own *Revised Fare Chart* (dated **14.02.2025**, downloaded from
`bmrc.co.in/tickets/`) keys the fare on **"No. of Stations travelled excluding originating
station"**. That is exactly the hop-count structure reverse-engineered from the GTFS matrix
earlier in the build, so the two now corroborate each other: the official table reproduces
**6,806 of 6,806** published station pairs exactly (100.00 %).

| Hops | 1–2 | 3–4 | 5–6 | 7–8 | 9–10 | 11–15 | 16–20 | 21–25 | 26+ |
|---|---|---|---|---|---|---|---|---|---|
| Fare | ₹10 | ₹20 | ₹30 | ₹40 | ₹50 | ₹60 | ₹70 | ₹80 | ₹90 |

Pink Line fares are generated from the same official chart over the union network and tagged
`fare_source = derived_official_chart`; published pairs keep `published_matrix`. A 5 %
revision announced for 9 February 2026 was kept in abeyance and never implemented, so the
14.02.2025 chart is the one in force.

**Smart card / NCMC pricing** comes from the same official page and is now priced in every
metro answer:

| When | Discount off token fare |
|---|---|
| Mon–Sat peak | 5 % |
| Mon–Sat non-peak | 10 % |
| Sundays & national holidays (26 Jan, 15 Aug, 2 Oct) | 10 % |

Non-peak = start of revenue hours to 08:00, 12:00–16:00, and 21:00 to close. So Majestic →
Whitefield (₹80 token) is ₹76 peak / ₹72 non-peak on a smart card.

**Official fare rules** are in the corpus as `07_bmrcl_official_fares.md`: ticketless travel
₹200 + max token fare, mismatch ₹10, overstay ₹50/hour capped at ₹100, luggage 60×45×25 cm
with ₹30 per extra bag (₹250 penalty if unpaid), same-station entry/exit ₹10, token
revalidation ₹5, smart card ₹50 with a ₹3,000 cap, and the helpdesk number 1800 425 12345.

Because the Pink Line adds real north-south shortcuts (for example Bannerghatta Road to
Majestic in 13 stops instead of a bus-and-transfer slog), some *existing* inter-line hop
counts will shorten once it opens. Published fares are read straight from the matrix, so they
are unaffected; only the projected Pink journeys use the new topology.

### KSRTC: four corridors, quoted but never routed

There is **no KSRTC GTFS feed in this build** — no stops, no stop sequence, no timetable,
no shape. What exists is four published corridor fares in `ksrtc_corridor`:

| Corridor | Class | km | Fare | Trust |
|---|---|---|---|---|
| KBS → Kanakapura | KSRTC ordinary (non-AC) | 65 | **₹124** | `VERIFIED_SECONDARY` |
| KBS → Kanakapura | Karnataka Sarige / express | 65 | **—** | `UNVERIFIED_PLACEHOLDER` |
| KBS → Harohalli (DSU) | KSRTC ordinary (non-AC) | 45 | ₹86 (derived) | `UNVERIFIED_DERIVED` |
| KBS → Mysuru | KSRTC ordinary (non-AC) | 145 | **₹162** | `VERIFIED_SECONDARY` (benchmark) |

So KSRTC appears in two places and nowhere else: a browsable table on **Explore**, and a
*"KSRTC intercity — quoted, not routed"* block under the itineraries whenever a plan's two
endpoints name a corridor. It never contributes a leg, a line on the map, a stop or a
walking connector. The block states its own limits in the panel: *"quoted, not routed:
these are published corridor fares with the board and alight points named. There is no
KSRTC stop-level GTFS in this build, so no timetable, stop sequence or line is
synthesised."*

Matching is deliberately strict. `"Kanakapura"` matches the town; `"Kanakapura Road"`,
which is a Bengaluru arterial 35 km from it, does **not** — `_mentions()` compares whole
names and rejects `_ROAD_WORDS` suffixes. When the endpoint text and the coordinates
disagree, the panel says so: *"'Kanakapura' resolved to NICE Road Kanakapura Road
(12.858, 77.528), which is not the Kanakapura on this corridor. The fare below is for
Kanakapura; confirm the stop before buying a ticket."*

A destination outside the place index still gets a quote. `Majestic → Mysuru` returns zero
routes, the warning *"Could not resolve 'Mysuru'"*, and the ₹162 corridor — which is the
honest answer, because the fare is real even though the router cannot walk you to it.

### The BMTC feed contains 40 KSRTC services — and they are not priced

The BMTC GTFS carries 40 long-distance services (`KSRTC-HASSAN-1`,
`KSRTC-KOLLEGALA-2`, …) with real stops and trips, so they *do* route. They are not,
however, BMTC city buses, and the BMTC distance slab stops at ₹32 for 50 km. Pricing a
300 km coach with it is not an estimate, it is a different number entirely. So:

* `fares.operator_of()` tags a leg `KSRTC` when its route label carries the prefix;
* those legs get `fare_inr: null` and `basis: "ksrtc_fare_not_published"`;
* an itinerary that contains one reports **`total_price_inr: null`**, a
  `priced_subtotal_inr` for the rest of the trip, and an `unpriced_legs` list naming the
  service — a student must never read "₹40" and board a ₹400 coach;
* the UI prints `n/a` + *"not published"* in amber, and the explanation says
  *"fare not published for KSRTC-KOLLEGALA-2"*;
* ranking keeps them honest: an unknown amount is never *the cheapest*. The five search
  objectives use a documented ranking prior (`KSRTC_PRIOR_INR_PER_KM = 1.4`, bracketed by
  the two published fares at ₹1.12–1.91/km) floored by the BMTC slab it replaces, so the
  optimiser does not treat an intercity coach as a ₹32 ride — but that prior is **never
  printed as a fare**. `Cheapest` sorts unpriced itineraries last.

## 9c. Pathfinding: Dijkstra, A* and RRT

Three search algorithms, three different jobs. None of them is a decoration.

### The graph they search

`src/routing/graph.py` turns the GTFS feeds into a **time-expanded corridor graph**:

* **Nodes** are physical stops — 9,960 BMTC stops and 504 metro stations, clipped to an
  ellipse between origin and destination (typically 1,000–1,500 stops for a city trip).
* **Edges** are scheduled rides taken straight from `route_segment` / `metro_segment`,
  which are built by `src/ingest_routing_graph.py` from all 1.54 M BMTC `stop_times`
  rows: mean run time and distance per (route, direction, stop pair). Bus speeds come out
  at **21.0 km/h**, metro at **38.8 km/h** — both plausible for Bengaluru, neither invented.
* **Walking connectors** join stops within 450 m (300 m for bus-to-bus), priced at
  4.8 km/h with a 1.25 street-detour factor.
* **The search state is `(stop, carrier, ticket, hops, km)`.** The `ticket` field is what
  makes the money right: an open metro ticket survives an interchange (BMRCL charges one
  token for the whole journey) but not an exit, and a bus change is always a second
  ticket. Without that in the state a search would transfer for free.

Two data-cleaning steps are worth knowing about, because both are visible in the numbers:

* **370 of 205,354 bus segments imply a speed above 90 km/h** — consecutive stops 50–64 km
  apart in under a minute, i.e. duplicated stop codes in the feed. They are dropped
  (`_plausible()`). Left in, a single 7,000 km/h edge raises the maximum speed and makes
  the A* heuristic vacuous.
* **Bengaluru runs ~20 bus routes over the same consecutive stop pair.** The raw corridor
  has 53,850 ride edges covering only ~2,600 distinct links. Keeping the two fastest
  carriers per link (`max_parallel=2`) cuts the graph by 80 % without removing a journey —
  the dropped carriers are slower on every link they share.

### The three algorithms

| | Role | Optimal? | Median time | Where it lives |
|---|---|---|---|---|
| **Dijkstra** | the reference — proves what the optimum is | **yes** | 17.2 ms | `src/routing/search.py::dijkstra` |
| **A\*** | the same optimum, fewer expansions | **yes, if h is admissible** | 17.4 ms | `src/routing/search.py::astar` |
| **RRT** | *a* feasible route fast; walking connectors | no, and it does not pretend to be | 4.2 ms | `src/routing/rrt.py` |

Dijkstra and A* share **one expansion loop**. They differ only in the order the priority
queue pops, so they are guaranteed to be searching the same graph with the same successor
rule — which is what makes the agreement check a real test rather than a coincidence.

The heuristic is `straight-line distance to the goal ÷ the fastest speed observed anywhere
in the graph`. No vehicle beats the fastest vehicle and no path is shorter than a straight
line, so it can never overestimate the remaining time. That is the admissibility argument,
and it is the reason A*'s answer is allowed to be called optimal.

RRT is included because it is genuinely useful where the other two are not — but the
benchmark is blunt about its limits:

* It reaches the destination on **79 %** of searches and returns in **4.2 ms median**.
* When it does reach, its answer is **41 % above optimal at the median** and 14 % at best.
  Individual corridors are far worse: the worst run in the sample came in at 26× the
  optimal cost. That is what a sampling planner is: feasible, fast, not optimal. When it
  fails outright it says so (`goal_hit: false`) instead of returning its closest approach
  as if it were an answer.
* Its cost can be handed to A* as an upper bound (the standard anytime pattern). Measured
  across the 27 searches where RRT produced a bound, it **never once** improved on A*'s own
  pruning — a solution 41 % above optimal leaves nothing to cut. The feature is kept,
  measured and reported rather than assumed to help.
* Where RRT is actually load-bearing is **`plan_walk_connector`** — RRT\* in continuous
  2-D space, for the last-metre question where there is no transit graph to search at all.

### Verification: `python -m src.eval.benchmark_routing 12`

Randomly drawn corridors (3–25 km apart), three objectives each, 34 searches:

| Check | Result |
|---|---|
| Dijkstra and A* return the same cost | **100 %** (34/34) |
| The returned cost survives an independent replay of the path | **100 %** (34/34) |
| A* expansion saving vs Dijkstra | **8.2 % median, 35.8 % best** |
| RRT reaches the destination | **79.4 %**, 4.2 ms median |
| RRT gap to optimal | **40.7 % median**, 14.0 % best, 2,625 % worst |

The replay check is the interesting one. `replay_cost()` is a *second, independent*
implementation of the cost model, including the fare marginals and the transfer rule. If
the search's bookkeeping ever drifts from the model, the two numbers diverge and the
benchmark fails. They currently agree to three decimal places on every search.

Fares are **never** taken from the search. The search optimises with marginal costs (per
hop for the metro, per kilometre for the bus); the number quoted to the user always comes
from `price_path()`, which re-reads the BMRCL official chart and the BMTC fare-stage
matrix. Every itinerary carries `cost_matches_replay: true` so the claim is auditable.

### What this does *not* buy you

* `objective="cheapest"` is optimal with respect to **BMTC's distance slabs**, not the
  fare-stage table. On premium KIA airport routes those two disagree, so a "cheapest"
  answer can be re-priced slightly above the search's own estimate. The quoted fare is
  still exact; only the *ranking* is slab-optimal. Documented, not hidden.
* A* is not magic on an urban transit graph. A geometric heuristic bounds travel time
  loosely when a 20 km trip costs 40 minutes across a dense network, so the median saving
  is single-digit percent. The dramatic A* wins in textbooks come from road networks with
  straight, fast edges.
* The Pink Line is excluded from routing (`service_status='operational'` filter) because
  it is not carrying passengers yet. Its fares remain queryable through `metro_fare`,
  labelled `derived_official_chart` + "not in service".

## 9d. The map

The app opens with a full Bengaluru map already on screen, and it stays the same map
object for the life of the page. Asking a question never reloads it.

### One instance, mutated in place

`web/static/js/map.js` creates exactly one map and exposes operations on it:

```
init()               called once, on load
drawItineraries()    replaces route overlays; does not touch the container
highlightRoute(i)    re-styles the existing polylines
clearRouteOverlays() removes route layers only, keeps the basemap
resetToOverview()    returns to the city view, same instance
```

Overlays are removed by deleting the individual SVG elements (or calling `setMap(null)`
on the Google polylines). The map element is never re-created, which is what makes
"clear the old route without reloading the map" a one-line operation rather than a
page transition.

This is why the UI is the FastAPI page and not Streamlit: `st.chat_input()` reruns the
script and `components.html()` rebuilds its iframe every time, so a Streamlit map cannot
keep one live JS object. Streamlit remains available (`streamlit run app.py`) as a
secondary view, but it is not the interface the map lives in.

### Three renderers, tried in order

`/api/map/config` returns a preference *chain*, not a single answer:

| Order | Renderer | When it is used | What you get |
|---|---|---|---|
| 1 | **Google Maps** | `GOOGLE_MAPS_BROWSER_KEY` is set | Google base tiles, drag/zoom, `TrafficLayer` |
| 2 | **OpenStreetMap** (Leaflet) | a tile probe succeeds | Real street-level Bengaluru from raster tiles |
| 3 | **SVG** (built-in) | always | Transit map drawn from GTFS stop geometry; works with no network at all |

The client does not trust the server's choice: before adopting a renderer it verifies it.
For Google that means the script actually loaded; for OSM it loads one real tile into an
`Image()` and checks `naturalWidth > 0`. If the probe fails it walks to the next entry in
the chain and says so on the map footer. So a missing key, a blocked CDN and an offline
sandbox all end with a working map instead of a blank one.

`MAP_PROVIDER=auto|google|osm|svg` pins the starting point.

### Why the tiles are proxied

The preview iframe this app runs in has **no network access**. A `tile.openstreetmap.org`
URL in the browser therefore fails silently and the map would degrade to SVG. The backend
*can* reach the tiles, so it fetches them and serves them from our own origin:

```
GET /api/tiles/{z}/{x}/{y}.png  ->  data/cache/tiles/{z}/{x}/{y}.png
```

* 256×256 PNG, cached on disk for 7 days, evicted past 4,000 tiles.
* A descriptive `User-Agent` and `Referer` are sent, per the
  [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
* If upstream fails the endpoint returns **503 with a JSON body** telling the client to
  use the SVG renderer — never a blank tile.
* `© OpenStreetMap contributors` is rendered on the map and cannot be switched off.

This is correct for a single-user demo on one machine. It is **not** a substitute for a
tile CDN: one proxy per server, no upstream rate handling, no clustering.

### Route geometry is real

`GET /api/route` returns `legs[].geometry`: the sequence of GTFS stop coordinates the
path traverses, per leg. Not a straight line between endpoints, and not a Google
polyline — so the map cannot show a route the fare engine did not actually price.

The lines are stop-to-stop segments, not road shapes: `shapes.txt` is 114 MB of
polyline geometry and is pruned from the repository (see `build.py`). At a mean
bus-stop spacing of 750 m the difference is visible when you zoom right in.

### Verified

| Check | Result |
|---|---|
| `node tests/test_map_dom.mjs` — jsdom, real DOM | **16/16** |
| `pytest tests/test_map_api.py` | **10/10** |
| `node tests/browser_smoke.mjs` — real Chromium | **37/37** |
| Tiles decoded in Chromium | **9 of 9** `<img>` tiles, `naturalWidth > 0` |

The DOM suite runs `map.js` for real and asserts the contract that matters: the map is
created once, drawing a second set of routes **replaces** the first rather than appending,
highlighting dims the others, and reset clears the routes while keeping the metro basemap.

`python scripts/preview_map.py Jayanagar Majestic` writes the same projection to
`data/processed/map_preview.svg` so the geometry can be eyeballed without a browser.

### What is deliberately absent

* **No live traffic without a Google key.** The checkbox exists, and without a key it is
  disabled, greyed and carries the exact reason in its tooltip — rather than showing an
  empty layer or a fake one. The traffic panel under the map does the same thing, at
  greater length.
* **No Google fare data.** Google does not reliably return BMTC/BMRCL fares, and it is
  never asked. Every rupee on the map comes from `price_path()` against the BMRCL chart
  and the BMTC fare-stage table.
* **No live vehicle positions.** Not available in GTFS static, not simulated.

## 9e. The dashboard, the spec API and user state

The page is a six-view shell — **New Chat**, **Plan a Trip**, **Explore Map**, **Saved
Routes**, **Travel History**, **Settings** — rendered from one HTML file, one stylesheet
and four plain scripts. No bundler, no `node_modules` build step, no external font, CDN or
stylesheet: Leaflet 1.9.4 and its CSS are vendored and the CSS is inlined into
`index.html`, so the app renders inside a sandboxed iframe and works from `localhost`
with the network off. The map tiles are the only thing that leaves the process, and they
go through this server's own cache (`/api/tiles/{z}/{x}/{y}.png`).

`tests/screens/b1-plan.png` (light) and `b7-dark.png` (dark) are captured by
`tests/browser_smoke.mjs` on every run, along with the mobile shots. The metro/KSRTC
panels are captured separately: `n14-explore-metro.png` (Explore, Purple expanded),
`n15-yellow-line.png` (Yellow Line framed on the map after *Draw on map*),
`n16-pink-line.png` (Pink, drawn dashed with *under construction* in the toast),
`n17-ksrtc.png` (a planned trip with the quoted-corridor panel beneath the itineraries),
`n18-ksrtc-leg.png` (an itinerary containing a real KSRTC service, fare `n/a`) and
`n19-intercity-only.png` (`Majestic → Mysuru`: no route, but the ₹162 corridor).

### One map, four owners

There is exactly **one** map instance for the life of the page. `#map` is created once
inside the New Chat view; switching to Plan or Explore **re-parents the same DOM node**
into that view's slot and calls `invalidateSize()` after layout. Nothing is destroyed,
no tiles are re-fetched, and the route overlays survive the switch. The layer panel,
legend and attribution travel with it — they would otherwise be stranded in a hidden
view. `tests/browser_smoke.mjs` asserts `document.querySelectorAll("#map").length === 1`
on the live page.

### Metro and KSRTC on screen

**Explore** answers "what network exists" with four line cards — colour, termini, station
count, length, fare range, interchanges and trust level — plus `Draw on map` (highlights
the line, frames it, dims the others; the polyline is drawn *on top of* the base strokes,
never instead of them) and `Show stations` (expands the ordered station list, with
interchanges tagged). Pink Line cards carry an *under construction* pill, and its
`Draw on map` says so in the toast: *"Pink Line · 18 stations · under construction, not in
service"*.

**Plan** puts the KSRTC panel **below** the itineraries, never above them: the routes
answer the question, the corridor fare is a supplement. Starting a new plan clears the
previous quote first, so a stale fare can never sit under a fresh result list.

### What each control actually does

| View | Control | Effect |
|---|---|---|
| New Chat | chat composer | `POST /api/chat`; the rule engine answers with or without an LLM. A "from X to Y" question is hoisted into the form and planned |
| New Chat | suggestion chips | fill the composer with a real question |
| New Chat | origin / destination | 13,071-place local autocomplete, keyboard navigable (↑ ↓ Enter Esc) |
| New Chat | swap | exchanges the pair on both forms |
| New Chat | *Plan Route* | plans, renders cards, draws geometry and fare labels on the map |
| New Chat | mode pills | re-request with `transport_modes` = all / bus / metro / walking / driving |
| New Chat | layer checkboxes | routes, metro lines, GTFS bus stops; **traffic is disabled with a reason** until a provider exists |
| New Chat | *Today / Tomorrow* | re-reads departure advice for that day |
| Plan | result tabs | re-sorts the same routes: rank, cheapest, fastest, fewest transfers, least walking |
| Plan | card click / *Show on map* | highlights that itinerary, dims the others, zooms the shared map |
| Plan | *Save* / *Log trip* / *Fares* | writes SQLite / writes history / expands the per-leg fare table |
| Explore | KPI tiles + line cards | real station and line counts, Pink Line marked `under_construction` |
| Explore | *Draw on map* / *Show stations* | highlights that line and frames it (`focusLine`) / expands its station list; both act on the shared map |
| Explore | KSRTC table | every corridor this build holds a fare for, with class, distance and trust level |
| Explore | *Find stops* | `/api/transit/stops/nearby` over real GTFS stops |
| Saved | *Load* / *Delete* | reloads the stored pair into the form / removes the row |
| History | KPI tiles + table | live aggregates over your own logged trips |
| Settings | preference selects, theme, traffic switch | written to SQLite, applied on the next request |
| Settings | integrations + data sources | every provider and dataset with its real status and licence |

No control is decorative. Where a control cannot do anything useful it is disabled and
says why — the traffic overlay is the standing example.

### Endpoints

The original eight endpoints are untouched. The brief §10 set lives at its documented
paths in `web/api/routes_spec.py`; the richer `/api/v1` surface from the earlier phase is
still mounted alongside it.

```
GET    /api/config/public                 what the browser is allowed to know (never a server key)
GET    /api/data-sources/status           dataset + provider registry with licences
GET    /api/locations/autocomplete        local index, Google merged in when configured
GET    /api/locations/geocode             text -> coordinates (+ nearest stop)
GET    /api/locations/reverse-geocode     coordinates -> nearest indexed place
POST   /api/chat                          chat: model first, rules engine always behind it
GET    /api/chat/health                   is a local model usable, and if not, which fix
GET    /api/v1/integrations               every provider, eight-state status, setup URLs
GET    /api/v1/integrations/{id}          one provider
POST   /api/v1/integrations/{id}/test     safe check; 409 unless billable is confirmed
PUT    /api/v1/integrations/{id}/enabled  enable/disable without editing .env
GET    /api/v1/config/health               startup validation: names and shapes, never values
POST   /api/routes/plan                   itineraries in the §11 contract shape
POST   /api/routes/compare                the same pair under all five objectives
GET    /api/routes/objectives             the search cost weights, for audit
GET    /api/routes/{id}                   a saved itinerary, or a live re-plan
POST   /api/traffic/analyze               live corridor traffic + scheduled GTFS supply
GET    /api/traffic/status                whether a traffic provider exists, and why not
GET    /api/transit/stops/nearby          real GTFS stops within a radius
GET    /api/transit/routes                route catalogue, searchable
GET    /api/transit/stations              stations, with line status
GET    /api/transit/lines                 the four metro lines, stations in travel order
GET    /api/transit/ksrtc-routes          KSRTC corridors: quoted fares only, never routed
GET    /api/transit/vehicle-positions     live bus positions; 200 "unavailable" or 502 if configured-but-broken
GET    /api/transit/trip-updates          GTFS-RT trip updates (decoded, not yet shown in the UI)
GET    /api/transit/realtime-status       is a feed configured, and is it readable right now
GET    /api/saved-routes                  list
POST   /api/saved-routes                  save one itinerary snapshot
DELETE /api/saved-routes/{id}             delete one
GET    /api/travel-history                entries + aggregates
POST   /api/travel-history                log a trip
DELETE /api/travel-history                clear

/api/v1/*                                 capabilities, coverage, preferences, history stats,
                                          objective comparison, A* vs Dijkstra vs RRT
```

Requests are validated by Pydantic models with `extra="forbid"`, so a typo'd field is a
422 rather than a silently ignored one — `{"modes": [...]}` is rejected, the field is
`transport_modes`. Responses that describe provider state are permissive on purpose:
they carry provider-specific extras.

### Two kinds of "no"

* **The request was wrong** → 4xx with a field-level message (`"origin: must not be blank"`).
* **The request was fine but there is no data** → 200 with `ok: false`, an empty `routes`
  list and a `warnings` entry saying why.

A 500 is reserved for a bug. The traffic panel and the integrations list use the same
principle in data form: `{available: false, reason: "...", needs: "..."}`.

### A place it had to guess is announced

The local resolver falls back to fuzzy matching, which is what makes "Hebbal" find
"Hebbala". For a query that shares almost nothing with the match that fallback is a
misroute, so `/api/routes/plan` compares what was typed with what was resolved and, when
the match is weak, returns:

> *“Atlantis Megacity 9000” was not in the place index; it was interpreted as
> “Megacity Layout”, the closest fuzzy match. Check the destination before travelling.*

Hand-maintained aliases (`majestic` → Kempegowda Bus Station, `hebbal` → Hebbala,
`jayanagar` → Jayanagara Bus Station) never trigger it. `tests/test_routes_spec.py` asserts
both directions: four real places stay quiet, two inventions get flagged.

### Five ranking objectives

`src/routing/search.py` defines the cost function for each:

| Objective | minutes | rupees | transfers | extra walk weight |
|---|---|---|---|---|
| fastest | 1.0 | 0 | 0 | 0 |
| cheapest | 0.05 | 1.0 | 0 | 0 |
| fewest_transfers | 0.05 | 0 | 45 | 0 |
| least_walking | 0.15 | 0.30 | 2.0 | 10.0 |
| balanced | 1.0 | 0.35 | 6.0 | 1.5 |

Koramangala → RV College, all modes, no preference: **₹92 / 45.7 min / 4 changes**,
₹98 / 50.2, ₹118 / 62.8, and the bus-only cheapest at ₹60 / 122.6 min. The weights are
exposed at `/api/routes/objectives` so the ranking can be audited rather than taken on
trust.

The *Cheapest* tab sorts a walking-only trip last even though ₹0 beats ₹60. It is kept in
the list — it is the honest answer to "what if I just walk?" — but a transit planner that
puts a four-hour walk at the top of "cheapest" is not answering the question that was
asked.

### Traffic: what is measured and what is missing

| Block | Source | When absent |
|---|---|---|
| **Live road traffic** | Google Routes, `TRAFFIC_AWARE_OPTIMAL`, `TRAFFIC_ON_POLYLINE` | `available: false` plus the exact missing config. No estimate is substituted. |
| **Scheduled network supply** | `route_segment.n_trips` and scheduled minutes from this database | `available: false` + why |
| **Hourly congestion chart** | *nothing* | The panel says **"Hourly traffic chart: not available"** and explains that the GTFS import has no departure times and no historical provider is configured |

The supply block reports trips per segment and the speed implied by scheduled minutes over
segment length, and says in its own note that a low implied speed could be congestion
**or** a dated timetable and that this data cannot tell them apart. This is the standing
example of the rule the whole build follows: a missing series is printed as missing, never
as a plausible-looking curve.

### User state

Four tables in the same `transit.db` (additive; nothing existing was altered):

```
saved_routes       itinerary snapshots you pinned
travel_history     trips you logged, with the fare shown at the time
preferences        key/value, per user_id
data_source_status dataset + provider registry with last success
api_call_log       every outbound provider call: provider, status, latency
```

`transit.db` now has **37 tables** (31 from the GTFS ingest plus 6 for this layer).
`src/db/extra_schema.py` is idempotent — safe to run on every start.

### Verified

Everything below was executed on this machine against this working tree.

| Check | Result |
|---|---|
| `pytest tests/` — 172 tests, providers mocked, LLM off | **172 passed, 1 skipped** |
| &nbsp;&nbsp;`tests/test_routes_spec.py` — the §10 contract + metro/KSRTC | **40 passed** |
| &nbsp;&nbsp;`tests/test_ollama_client.py` — client, schema, grounding gates | **20 passed** (+1 live, skipped) |
| &nbsp;&nbsp;`tests/test_api_v1.py` — v1 contract | **33 passed** |
| &nbsp;&nbsp;`tests/test_map_api.py` — map/tile endpoints | **10 passed** |
| &nbsp;&nbsp;`tests/test_integrations.py` — registry states, redaction, probes, geocoding, chain | **45 passed** |
| &nbsp;&nbsp;`tests/test_gtfs_rt.py` — protobuf/JSON decode, staleness, cache, 502s, redaction | **24 passed** |
| `node tests/test_map_dom.mjs` — jsdom, layer API | **16/16** |
| `node tests/test_integrations_dom.mjs` — jsdom, Settings cards | **14/14** |
| `node tests/browser_smoke.mjs` — real Chromium, real server, real tiles | **37/37** (against a live Ollama too) |
| `node tests/browser_integrations.mjs` — Chromium: registry, 409 guard, slow chat | **15/15**, slowest turn 41.6 s, no client abort |
| `node tests/browser_live.mjs` — Chromium: live layer with a local fixture feed / without any feed | **9/9** and **9/9** (3 markers + Live / 0 markers + Unavailable) |
| `python -m src.eval.run_eval` | **41/41 cases**; tool selection 100 %, content 98 %, price audit 100 %, retrieval hit@4 100 %, 0.48 s mean (one must-mention miss, q37) |

The six new contract tests guard the honesty rules, not the happy path: Pink is
`routable: false` everywhere it appears; no KSRTC row claims a stop list or a shape; a
KSRTC leg comes back `fare_inr: null` with `basis: "ksrtc_fare_not_published"`; an
unpriced itinerary carries a `priced_subtotal_inr`, names its `unpriced_legs`, and is never
labelled cheapest (and sorts last under the `cheapest` preference); the intercity block
quotes ₹124 for the ordinary corridor and nothing at all for the express; and
`Majestic → Mysuru` returns zero routes while still quoting ₹162 — with the "could not
resolve" warning printed exactly once.

`tests/browser_smoke.mjs` is the one that would have caught every UI bug found in this
phase: it boots the page, checks one map container and six views, switches views and
asserts the map followed, types into autocomplete and picks with the keyboard, plans a
trip, checks fares/legs/provenance chips/stat grid, checks polylines *and* fare labels on
the map, checks the traffic panel's honesty copy, sorts, saves, logs, expands fares,
toggles layers, looks up nearby stops, reads Settings, sends a chat message and flips the
theme — failing on any console error. Screenshots land in `tests/screens/`.

Four things this phase changed because a browser check failed:

* the map used to be **hidden** on narrow screens (`@media (max-width:980px){.mapcol{display:none}}`),
  which contradicts "the map is visible the moment the app opens"; it is now a full-width
  band under the conversation, and the sidebar became a drawer with a scrim;
* the layer panel lived inside the New Chat view, so it vanished when the map moved to
  Plan or Explore — the map, its layer panel, legend and attribution now move together;
* the map was fitted **before** the traffic panel below it had rendered, so Leaflet
  computed the fit against a taller container and projected the routes off the visible
  area. The container now has a `ResizeObserver`, and the panel's own response re-fits;
* the KSRTC panel first rendered **above** the itineraries, which reads as "here is your
  answer, it is a bus that does not run in this app". It now sits below the routes and is
  cleared at the start of every plan, so a stale corridor quote cannot appear under a
  fresh result list.

All three were only visible in a real browser at a real viewport size, which is the
argument for keeping `tests/browser_smoke.mjs` in the loop rather than trusting jsdom.

## 9f. The local model (Ollama)

**No paid API key is involved.** The chat path talks to a local Ollama server over HTTP and
falls back to the deterministic planner when there is no model. Everything a student reads
as a *fact* — a fare, a route, a station, a corridor — comes from a tool, never from the
model's memory.

### Setup

```bash
# 1. install Ollama (Windows: the .exe; Linux: the tarball; macOS: the .dmg)
ollama --version

# 2. store models wherever you have space (Windows example — set it, then quit and
#    relaunch Ollama from the tray so it picks the variable up)
#    [Environment]::SetEnvironmentVariable("OLLAMA_MODELS", "D:\OllamaModels", "User")

# 3. pull a tool-calling model
ollama pull qwen2.5:0.5b       # default; use 1.5b/3b on machines with more memory

# Optional: make the direct Ollama CLI use the CampusTransit identity instructions too.
ollama create campus-transit-qwen:0.5b -f Modelfile.campustransit
ollama run campus-transit-qwen:0.5b "Say CampusTransit AI is working"

# 4. make sure the server is up (the desktop app does this for you)
ollama serve

# 5. optionally override settings; the built-in default is qwen2.5:0.5b
cp .env.example .env            # LLM_MODEL, OLLAMA_BASE_URL, LLM_ENABLED, OLLAMA_TIMEOUT_S
python run.py

# 6. check it took effect
curl localhost:7860/api/chat/health
```

`/api/chat/health` answers with the reason, not a boolean:

```json
{"provider": "ollama", "model": "qwen2.5:0.5b", "available": true,
 "model_pulled": true, "models": ["qwen2.5:0.5b"],
 "reason": "ollama/qwen2.5:0.5b is ready", "needs": "", "ok": true}
```

If Ollama is not running: `reason: "Cannot reach Ollama at http://localhost:11434"`,
`needs: "install Ollama and run ollama serve (default port 11434)"`. If it is running with
the wrong model: `reason: "Ollama is up, but 'qwen2.5:0.5b' is not pulled (installed: …)"`,
`needs: "ollama pull qwen2.5:3b"`. The Settings screen and `/api/v1/capabilities` show the
same live state — the LLM row is probed, not inferred from an env var.

### What the model is allowed to do

It chooses tools, in a loop capped at six rounds; it does not compute. The system prompt
(one copy, in `src/services/ollama_client.py`) forbids inventing routes, fares, schedules
or traffic, forbids arithmetic, and tells it to repeat any caveat a tool returns.

Three checks run on every model answer before it is shown:

1. **Every ₹ amount must appear in a tool result.** `_price_audit` extracts `₹60`, `Rs 124`,
   `2.50 INR` and `60 rupees` and looks each one up in the numbers the tools returned.
2. **If it called no tool, it may not make a transit claim.** A greeting is fine; "take the
   Yellow Line" with no lookup behind it is not.
3. **It must have consulted a tool that covers the question.** If the question is about the
   metro and the model only consulted the KSRTC table, the answer is about something else.

A failure raises `UngroundedAnswer`; the caller falls back to the rules engine and the reply
carries `fallback_reason`, which the UI prints under the message. The rejected text is kept
in `agent.last_withheld` for inspection rather than deleted.

### What was actually verified here

| Check | Result |
|---|---|
| Ollama 0.40.11 installed, `qwen2.5:0.5b` + `qwen2.5:1.5b` pulled | works |
| `POST /api/chat` with a pass question → model called `recommend_pass`, answer returned, **all fares traced to a tool** | **verified** (`engine=llm`, 76 s on 2 CPU cores) |
| `POST /api/chat` "Which Namma Metro line serves Majestic?" on 0.5b | model answered **without the metro tools** → withheld, rules engine replied with the correct station list |
| Same question on 1.5b, first attempt | model answered **with no tool call at all** and invented "2.50 INR" → withheld |
| Same pass question on 0.5b, a later run | model did the arithmetic itself (₹1,140 = 60 × 19) → **withheld**; the rules engine answered |
| Same pass question on 1.5b | model called `recommend_pass`, answer passed the audit — this is not deterministic, which is why the gate exists |
| `POST /api/chat` greeting → 0.5b answered conversationally in 3.7 s, no tools | verified |
| Browser: chat turn renders answer + provenance line (`Answered by qwen2.5:0.5b via Ollama · 1 tool · 32.3 s`) | verified, screenshot `tests/screens/n20-chat-llm.png` |
| `tests/test_ollama_client.py` — 20 mocked tests + 1 live test that skips without a model | **20 passed, 1 skipped** (17 + live when a model is present) |

**The honest summary: a 0.5B/1.5B model on CPU is not good enough to be trusted
unguarded, and it is not deterministic either** — the same question can pass the audit on
one run and be withheld on the next. It will call the wrong tool, skip the tools entirely, and compute its own
arithmetic. The guards are what make it safe to ship; they also mean that on this hardware
the deterministic planner answers most questions, and says so in the provenance line. A
3B+ model on a machine with 8 GB of RAM behaves considerably better — that path is
untested here because this container gets the model runner killed while loading 3B.

### Speed

CPU inference on 2 cores: 0.5b ≈ 10–35 s per turn, 1.5b ≈ 65 s, 3b untested (killed for
memory). `OLLAMA_TIMEOUT_S` defaults to 120 s. The UI disables the composer while a turn is
in flight, so a slow answer cannot be double-sent.

### Why not LangChain

The first implementation used `langchain_ollama.ChatOllama`. It put two heavy packages on
the critical path of an app whose point is "clone and run", and the import sat inside the
method — so a missing package looked exactly like a missing model. `src/services/ollama_client.py`
is ~330 lines of `httpx` with typed errors (`OllamaUnavailable`, `OllamaTimeout`,
`OllamaModelMissing`, `OllamaHTTPError`), which removed the dependency *and* made the
failures say what to do. Ollama's OpenAI-compatible `/v1` endpoint exists if you would
rather use the OpenAI SDK; this project does not need it.

## 10. Integrations, keys and the registry

Every optional provider is described once in `src/services/integrations.py` and rendered
from that record — Settings and `GET /api/v1/integrations` cannot disagree, because there is
only one source. Full setup, console URLs, billing notes and troubleshooting:
**[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)**. Windows commands:
**[docs/POWERSHELL.md](docs/POWERSHELL.md)**.

### The eight states

| State | Reached how |
|---|---|
| `not_configured` | a required variable is missing |
| `configured_untested` | credentials present, no successful check in 24 h |
| `ready` | last check passed inside 24 h, **or** a local integration verified on this page load |
| `disabled` | switched off in Settings |
| `auth_failure` | 401/403, `REQUEST_DENIED`, `PERMISSION_DENIED` |
| `quota_or_billing` | 429, `OVER_QUERY_LIMIT`, `RESOURCE_EXHAUSTED` |
| `provider_unavailable` | transport error, timeout, 5xx |
| `unsupported` | cannot be used here (no authorised GTFS-RT feed; Nominatim by policy) |

An environment variable alone never produces `ready`. A success older than 24 hours
downgrades to `configured_untested`, because a revoked key still looks fine in a week-old
result. Local integrations (Ollama, GTFS tables, tile cache, policy index) are re-checked on
every Settings load, which is why their "ready" is a fact rather than an assumption.

### 14 integrations

Google Maps JS · Google Places · Google Geocoding · Google Routes · Google traffic ·
OpenStreetMap tiles · OpenRouteService · TomTom · GTFS static (BMTC + BMRCL) ·
GTFS-Realtime (*implemented, needs a feed*) · BMTC unofficial WebAPI (*unsupported by
decision*) · Ollama · Chroma policy index · Nominatim (*unsupported by policy*).

### Live bus positions: the reader is real, the feed is yours

`/api/transit/vehicle-positions` decodes a GTFS-Realtime feed (protobuf or JSON), caches it
for at least 15 s, shares one in-flight request between callers, and marks a vehicle **Live**
only when the provider's own timestamp is inside `GTFS_RT_STALE_AFTER_S`. The map's
*Live buses* layer polls at the interval the server reports, stops while the tab is hidden,
and clears its markers the moment you switch it off.

There is no bundled feed and no default URL, because **BMTC publishes no open,
unauthenticated vehicle-positions endpoint**: its AVL stream is a secure IUDX resource, and
the move onto ONDC was still "advanced discussions" as of October 2026. With
`GTFS_RT_VEHICLE_URL` unset the layer draws nothing and says *Unavailable* with the reason.
It never places a bus from a timetable.

The reverse-engineered `bmtcmobileapi` endpoints were examined and are **not** wired: they
are per-route, undocumented, and unlicensed for this use. Full reasoning, the IUDX
acquisition path, the staleness model and the test procedure are in
[`docs/GTFS_REALTIME.md`](docs/GTFS_REALTIME.md).

### Tests are safe by construction

`POST /api/v1/integrations/{id}/test` answers **409** with `requires_confirmation: true`
until the caller sends `{"confirm_billable": true}` for anything metered. Loading the
Settings page never calls a provider. An unconfigured integration answers `not_configured`
without a network call and without recording a check.

```bash
# free: reads local state only
curl localhost:7860/api/v1/integrations

# free: Ollama /api/tags
curl -X POST localhost:7860/api/v1/integrations/ollama/test

# refused: 409, nothing billed
curl -X POST localhost:7860/api/v1/integrations/google_places/test

# one billed Autocomplete request
curl -X POST localhost:7860/api/v1/integrations/google_places/test \
     -H 'Content-Type: application/json' -d '{"confirm_billable": true}'
```

### Road routing is a configured chain

```dotenv
ROAD_ROUTING_CHAIN=google_routes,openrouteservice,tomtom   # default
ROAD_ROUTING_PROVIDER=openrouteservice                     # pin one, no fallback
```

Providers that are unconfigured, switched off, or last failed with `auth_failure` /
`quota_or_billing` are skipped, and the response records why:

```json
"attempts": [{"provider": "google_routes", "ok": false, "ms": 412, "error": "HTTP 500"},
             {"provider": "openrouteservice", "ok": true, "ms": 233, "error": ""}],
"provider": "openrouteservice", "traffic_aware": false
```

Each provider only claims what it can support: Google and TomTom can be traffic-aware,
OpenRouteService never is (static network), and **nothing** is labelled a road route unless
a provider returned geometry. With no provider configured, driving returns
`available: false` — no straight-line substitution. Traffic panels read Google first (it
returns per-segment speeds), then TomTom (delay only), then
`"Live traffic data unavailable"`.

### Fares never come from a routing provider

Unchanged and load-bearing: every price comes from the BMRCL chart or the BMTC GTFS fare
stages in SQLite. A KSRTC leg with no published fare stays `null` and renders as
*not published* — including in `plan_optimal_route` legs, where a coercion from `None` to
`0` was found and fixed. A total that would silently omit such a leg is reported as
`null` with `priced_subtotal_inr` and an `unpriced_legs` list.

### Timeouts: what the 45-second abort actually was

`web/static/js/api.js` applied a single `TIMEOUT_MS = 45000` to every call. A local model on
a CPU needs 30–90 s per turn, so the browser aborted mid-generation while the server kept
working, and each retry queued behind the previous generation on a single-threaded Ollama.
Measured on this machine (2 vCPU, `qwen2.5:0.5b`): **36.5 s, 41.6 s and 53.6 s** for the same
pass question across runs.

The fix is three-part, and none of it is "raise the number":

1. **Per-endpoint client timeouts** (`TIMEOUTS` in `api.js`): chat starts at 210 s and is
   then set from the server's own `timeout_hint_ms`; autocomplete 10 s, geocode 15 s, plan
   30 s, everything else 20 s. A timeout message names the wait and the limit.
2. **A server-side deadline** (`CHAT_BUDGET_S`, default 150 s), checked between tool rounds,
   with each model call given only the remaining budget. On expiry the rules engine answers
   and `fallback_reason` says `deadline: …`.
3. **Duration logging**: one line per request with method, path, status and milliseconds
   (`POST /api/chat -> 200 in 53570 ms`), which is what made the 45 s abort explainable.

The elapsed counter in the chat bubble ("42s · local model, still working…") exists because
"thinking" and "broken" look identical after ten seconds.

## 11. Limitations, verification status and things that are deliberately absent

**Prices are placeholders or secondary-source values, not official quotes.**

* **Metro fares are the one domain on a primary source.** BMRCL's Revised Fare Chart
  (14.02.2025) was downloaded as a PDF and parsed into `metro_fare_slabs`; it reproduces the
  entire GTFS matrix. Metro rules came from the same site. Everything below is still
  secondary or estimated.

  The chart is one page of two tables, and totals 30 fare rows: *"1 to 2" → ₹10* through
  *"26 to 30" → ₹90*, plus a flat ₹10 for entering and exiting at the same station. Its own
  wording — *"No. of Stations travelled excluding originating station"* — is what makes the
  hop-count model official rather than reverse-engineered. Full table in
  `data/curated/policy_docs/07_bmrcl_official_fares.md`.
* Pass prices (₹1,200 ordinary, ₹1,400 student AC, ₹2,000 Vajra, ₹4,000 Vayu Vajra, ₹80
  daily, ₹350 weekly) come from press coverage of the 9 Jan 2025 BMTC revision. The BMTC
  portal was unreachable, so they are `VERIFIED_SECONDARY`, not `VERIFIED_PRIMARY`.
* The free student pass (₹0) is `VERIFIED_SECONDARY` against the 12 Jun 2026 government
  order as reported; the order PDF itself was not retrievable.
* **KSRTC monthly season ticket: `UNVERIFIED_PLACEHOLDER`.** No published monthly rate was
  reachable; the agent says so and tells the user to ask at the depot instead of guessing.
* **Bus fares are slab estimates.** Only 108 of 4,434 BMTC routes carry a fare stage in the
  feed. Everything else is interpolated from the published Jan 2025 distance slab
  (₹6 / 2 km … ₹32 / 50 km) and printed as "published BMTC slab … estimate".
* **D4 is a substitute.** data.gov.in was down for the whole build; the KSRTC corridor table
  is secondary-sourced and labelled per row.
* **No KSRTC fare table exists in this build, so no KSRTC total is ever printed.** The 40
  KSRTC services that arrive inside the BMTC GTFS are routed (their stops are real) but
  left unpriced: `fare_inr: null`, `total_price_inr: null`, `priced_subtotal_inr` for the
  rest. A city-bus slab would have produced a confident, wrong number — ₹32 for a 300 km
  coach — which is worse than a blank.
* **KSRTC corridors are quotations, not journeys.** Four rows, two quotable. Nothing about
  them is routed, drawn, scheduled or tracked.

**The LLM path now runs against a real model — see §9f.** It is no longer "written but
untested": Ollama was installed in this environment, `qwen2.5:0.5b` and `qwen2.5:1.5b`
were pulled, and the tool-calling loop was exercised end to end. What that testing found
is the reason §9f has three guard rails in it.

**Live vehicle positions are unavailable without a feed you own.** The reader, the layer,
the staleness rules and the tests are in place; the feed is not. Saying so is the feature.

**Known gaps**

* **No live traffic, live vehicle positions or real-time crowding.** No authorised BMTC or
  BMRCL GTFS-Realtime feed ships here, and no Google key was available while this was
  built. The traffic panel therefore shows *unavailable* for live data and real scheduled
  supply beside it. `congestion_ratio`, `delay_minutes` and the speed-interval list are
  implemented and unit-tested against a stubbed Routes response, but have **never run
  against the live API** — that path is written, not verified.
* **No departure times in the GTFS import.** Per-hour service frequency, and therefore any
  congestion-by-hour chart, cannot be derived from this database. `trip_stops` has
  `(trip, seq, stop)` and no clock.
* **The map draws stop-to-stop, not along roads.** `shapes.txt` is pruned from the repo
  (114 MB). Against a street-level OSM basemap this is visible when you zoom in. No
  supplier of road geometry is available without a key, and inventing smoothed curves
  would misrepresent the data.
* **Autocomplete with a Google key is written but unverified.** The merge logic, the
  `google_places` source tag and the Details→coordinates path are covered by stubs, not by
  a real key.
* **The OSM tile proxy is a single-user convenience, not infrastructure.** One server, no
  clustering, no upstream rate negotiation, 4,000-tile disk cap. Fine for this demo; wrong
  for a deployment with real traffic.
* **Preferences are per-installation.** `user_id` defaults to `local`; there is no auth
  and no multi-user isolation, though the column exists so it can be added.
* **The v1 API has no pagination beyond `limit`/`offset` on history**, no ETags, no
  compression configured, and no rate limiting.



* Route coverage is only what the GTFS mirror exposes (routes with live tracking); a few
  once-a-day services are missing.
* `get_ksrtc_options` can return both a published ordinary row and a "fare not published"
  express row for the same origin-destination; they are distinct service classes, but the
  pair reads as contradictory unless you notice the prefix.
* Policy/FAQ fee tables disagree in places (ITI ₹1,300 vs ₹1,310; evening/PhD ₹1,630 vs
  ₹1,050). The documents record both, and the agent quotes the corpus rather than choosing.
* The corpus does **not** contain phone numbers, individual officer contacts, real-time
  occupancy or live arrival times. Ask and the agent refuses rather than inventing.
* Two "Harohalli" stops exist; the DSU one is `BWSSBQ` (12.6599, 77.4512), not the east
  Bengaluru stop the fuzzy matcher prefers. `places.json` pins the campus alias.
* Everything is Bangalore-specific and 2026-27-specific. Feed validity ends 2027-09-07;
  re-ingest before then.
* **Pink Line specifics are secondary-sourced and unopened.** Station names, order and
  coordinates come from Wikipedia station articles and news reports, not from BMRCL. The
  gate names in particular drift between sources (Kadugundanahalli / Kadugondanahalli;
  "Bilekahalli IIM-B" / "IIMB"). Its fares are derived, not published, and are flagged as
  such everywhere they appear. Re-check the whole line against the official feed after it
  opens.
* Pink Line geometry measures 19.48 km straight-line against the published 21.26 km track
  length — the same ~9 % curve factor the three operational lines show, which is why it is
  kept as an independent check rather than used to compute fares.

**What would make this production-grade:** official access to the BMTC and KSRTC pass
portals (the metro side is already on a primary source), a live Ollama/Groq endpoint to
actually exercise the LLM path, BMTC real-time vehicle positions for arrival estimates, and an
externally authored eval set.

---

Data: BMTC and BMRCL GTFS via the DULT Transport Data Hub mirrors; OpenStreetMap contributors
(ODbL); KSRTC fare table as republished after the 5 Jan 2025 revision. Code: MIT.
