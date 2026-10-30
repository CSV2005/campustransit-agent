"""Deterministic planner + answer composer.

This is the controller that runs when no LLM is available (and the reference the
LLM agent is evaluated against). It classifies the question, calls the same tools
the LLM would call, and builds the answer *from tool output only*. Every rupee
figure in the final text is copied out of a tool observation, and an audit step
verifies that at the end.
"""
from __future__ import annotations

import functools
import json
import math
import re
import time
from typing import Any

from src.tools.common import STOP_TOKENS
from src.tools.pass_tool import BY_ID
from src.tools.registry import call_tool

# Ways people name a specific pass in a question, longest/most specific first.
PASS_ALIASES: list[tuple[str, str]] = [
    ("vayu vajra", "bmtc_vayu_vajra_gold"),
    ("airport pass", "bmtc_vayu_vajra_gold"),
    ("kia pass", "bmtc_vayu_vajra_gold"),
    ("student ac", "bmtc_student_pass_ac_vajra"),
    ("ac student", "bmtc_student_pass_ac_vajra"),
    ("vajra student", "bmtc_student_pass_ac_vajra"),
    ("student vajra", "bmtc_student_pass_ac_vajra"),
    ("vajra", "bmtc_monthly_vajra_ac"),
    ("ordinary monthly", "bmtc_monthly_ordinary"),
    ("ordinary pass", "bmtc_monthly_ordinary"),
    ("normal monthly", "bmtc_monthly_ordinary"),
    ("monthly pass", "bmtc_monthly_ordinary"),
    ("shakti", "shakti_scheme"),
    ("free student", "karnataka_free_student_pass"),
    ("free pass", "karnataka_free_student_pass"),
    ("student pass", "karnataka_free_student_pass"),
    ("ksrtc monthly", "ksrtc_sarige_monthly"),
    ("season ticket", "ksrtc_sarige_monthly"),
    ("sarige", "ksrtc_sarige_monthly"),
]

# Questions that want a recommendation, not a lookup of one named pass.
RECOMMEND_INTENT = re.compile(
    r"\b(which\s+(?:\w+\s+){0,2}pass|what\s+(?:\w+\s+){0,2}pass|"
    r"should i (?:buy|get|take)|worth it|best pass|do i need|recommend|"
    r"cheaper|save money)\b", re.I)

# Student pass fees vary by course, and the corpus carries the full category table;
# these questions must go to retrieval rather than to a single pass card.
CATEGORY_INTENT = re.compile(
    r"\b(iti|primary|high school|puc|pu college|diploma|professional|degree|"
    r"evening|phd|post.?graduate|category|categories)\b", re.I)

REPLACEMENT_INTENT = re.compile(r"\b(replacement|lost|duplicate|damaged|stolen)\b", re.I)

# "How do I apply" is a procedure question answered by the corpus, not a price lookup.
POLICY_INTENT = re.compile(
    r"\b(policy|rule|rules|allowed|not allowed|permitted|official|circular|order)\b", re.I)
APPLY_INTENT = re.compile(
    r"\b(apply|applying|application|how do i get one|process|steps|register|renew)\b", re.I)


def detect_named_pass(text: str) -> str | None:
    low = text.lower()
    for alias, pid in PASS_ALIASES:
        if alias in low:
            return pid
    return None


def _pass_card(pid: str) -> str:
    """Describe one pass straight from the curated card, with no recommendation logic."""
    p = BY_ID[pid]
    price = p.get("price_inr")
    if price is None:
        price_txt = "**price not verified** — no published figure could be sourced"
    elif price == 0:
        price_txt = "**free** (₹0)"
    else:
        price_txt = f"**{_money(price)}**"
    lines = [f"### {p['name']}", "",
             f"- **Price:** {price_txt}",
             f"- **Operator:** {p.get('operator', 'n/a')}"]
    if p.get("validity_note"):
        lines.append(f"- **Validity:** {p['validity_note']}")
    if p.get("service_classes"):
        lines.append(f"- **Covers:** {', '.join(p['service_classes'])}")
    if p.get("eligibility"):
        lines.append(f"- **Who can buy it:** {p['eligibility']}")
    if p.get("price_note"):
        lines.append(f"- **Price note:** {p['price_note']}")
    if p.get("notes"):
        lines.append(f"- **Notes:** {p['notes']}")
    if p.get("where_to_apply"):
        lines += ["", f"**Where to apply:** {p['where_to_apply']}"]
    if p.get("application_url"):
        lines.append(f"**Portal:** {p['application_url']}")
    lines += ["", f"*Source: {p.get('source_url','n/a')} — status "
                  f"`{p.get('verification_status','')}`, last verified "
                  f"{p.get('last_verified','n/a')}.*"]
    return "\n".join(lines)


DEFAULT_PROFILE = {
    "travel_days": 24,
    "needs_ac": False,
    "is_woman": False,
    "is_student": True,
    "airport": False,
}

QUESTION_PREFIX = re.compile(
    r"^(?:(?:hi|hey|hello)[,!\s]+)?(?:please\s+)?(?:can you\s+|could you\s+|tell me\s+|"
    r"i want to know\s+|i need\s+|what(?:'s| is)\s+the\s+(?:best\s+)?|which is the\s+(?:best\s+)?|"
    r"which\s+|what\s+|how do i\s+|how to\s+|help me\s+)*", re.I)


def _clean_place(text: str) -> str:
    t = text.strip(" .,?!:;-")
    t = QUESTION_PREFIX.sub("", t)
    t = re.sub(r"^(?:the\s+)?(?:best\s+|cheapest\s+|fastest\s+|quickest\s+)?", "", t, flags=re.I)
    t = re.sub(r"\b(?:by|via|using|with)\s+(?:the\s+)?(?:bus|buses|metro|namma metro|train|"
               r"auto|bmtc|ksrtc)\b", " ", t, flags=re.I)
    t = re.sub(r"\b(?:bus|buses|route|routes|pass|passes|commute|travel|go|get|take|"
               r"reach|journey|trip|daily|monthly|metro)\b", " ", t, flags=re.I)
    return " ".join(t.split()).strip(" .,?!:;-")


def _resolve_ok(place: str) -> bool:
    from src.tools.common import resolve_place
    if not place or len(place) < 3:
        return False
    r = resolve_place(place)
    return bool(r.get("ok")) and r.get("confidence", 0) >= 55


INTENT_TOKENS = {
    "which", "what", "how", "do", "does", "i", "me", "you", "tell", "show", "give",
    "best", "cheapest", "fastest", "should", "can", "could", "please", "want", "need",
    "go", "goes", "going", "reach", "get", "take", "travel", "commute", "route",
    "routes", "bus", "buses", "pass", "passes", "metro", "line", "and", "or", "the",
    "is", "it", "there", "here", "where", "when", "why", "change", "board", "alight",
    "fare", "fares", "price", "prices", "cost", "costs", "ticket", "tickets",
    "ksrtc", "bmtc", "timing", "timings", "schedule", "much", "many",
}

SPAN = r"[a-z0-9 ,.\-&']"


def _span_is_place(raw: str) -> str | None:
    """Clean a candidate span; return it only if it looks like a place the index knows."""
    cleaned = _clean_place(raw)
    if not cleaned:
        return None
    tokens = [t for t in re.split(r"[^a-z0-9]+", cleaned) if t]
    if not tokens or len(tokens) > 7:
        return None
    if any(t in INTENT_TOKENS for t in tokens):
        return None
    return cleaned if _resolve_ok(cleaned) else None


def extract_od(text: str) -> tuple[str | None, str | None]:
    """Pull an origin/destination pair out of free text.

    Only three shapes are accepted, and each span is validated against the place
    index with intent words stripped first, so 'which bus goes from X to Y' can never
    turn into origin='which bus goes' or destination='goes'.
    """
    t = " " + re.sub(r"\s+", " ", text.lower().replace("→", " to ").replace("->", " to ")) + " "

    # A destination span must reach the end of the sentence, so an instruction clause after
    # it becomes part of the span: "Dayananda Sagar University, Bengaluru, using bus and
    # metro where practical" was one 12-token "place" and the whole OD was lost. Trim such
    # clauses from the tail only -- a "by bus" before the origin must not eat the sentence.
    head, sep, tail = t.rpartition(" to ")
    if sep:
        tail = re.split(
            r"[,;]?\s+(?:using|via|by (?:bus|metro|train|car|auto|walk(?:ing)?)|"
            r"where (?:practical|possible)|if (?:possible|practical)|prefer\w*|please)\b",
            tail)[0]
        t = head + sep + tail + " "

    # 1. "<DEST> from <ORIGIN>", e.g. "how do I reach DSU from Banashankari"
    for m in re.finditer(rf"({SPAN}+?)\s+from\s+({SPAN}+?)(?:[.?!]|$)", t):
        d = _span_is_place(m.group(1))
        o = _span_is_place(m.group(2))
        if o and d and o != d:
            return o, d

    # 2. "from <ORIGIN> to <DEST>"
    for m in re.finditer(rf"from\s+({SPAN}+?)\s+to\s+({SPAN}+?)(?:[.?!]|$)", t):
        o = _span_is_place(m.group(1))
        d = _span_is_place(m.group(2))
        if o and d and o != d:
            return o, d

    # 3. "between <A> and <B>"
    for m in re.finditer(rf"between\s+({SPAN}+?)\s+and\s+({SPAN}+?)(?:[.?!]|$)", t):
        o = _span_is_place(m.group(1))
        d = _span_is_place(m.group(2))
        if o and d and o != d:
            return o, d

    # 4. bare "<A> to <B>" (no from), e.g. "Majestic to Whitefield by metro"
    for m in re.finditer(rf"({SPAN}+?)\s+to\s+({SPAN}+?)(?:[.?!]|$)", t):
        o = _span_is_place(m.group(1))
        d = _span_is_place(m.group(2))
        if o and d and o != d:
            return o, d
    return None, None


def parse_daily_spend(text: str) -> float | None:
    m = re.search(r"(?:rs\.?|₹|inr)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:a|per|/)\s*day", text, re.I)
    if not m:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:rupees\s*)?(?:a|per|/)\s*day", text, re.I)
    return float(m.group(1)) if m else None


def parse_monthly_spend(text: str) -> float | None:
    m = re.search(r"(?:rs\.?|₹|inr)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:a|per|/)?\s*month", text, re.I)
    if not m:
        m = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:rupees\s*)?(?:a|per|/)\s*month", text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def profile_from_message(text: str, base: dict) -> dict:
    p = dict(DEFAULT_PROFILE, **(base or {}))
    low = text.lower()
    if re.search(r"\b(i am|i'm|im)\s+(a\s+)?(girl|woman|female)\b", low) or "as a woman" in low:
        p["is_woman"] = True
    if re.search(r"\b(not a student|faculty|staff|teacher|working professional|i work)\b", low):
        p["is_student"] = False
    wants_ac = re.search(
        r"\b(?:need|want|prefer|use|take|require)\s+(?:the\s+)?(?:ac|a/c|vajra|volvo|air[- ]?conditioned)\b", low)
    refuses_ac = re.search(
        r"\b(?:don'?t|do not|no|without|not)\s+(?:really\s+)?(?:need|want|require|use|taking)\s+(?:the\s+)?"
        r"(?:ac|a/c|vajra|volvo|air[- ]?conditioned)\b", low)
    if wants_ac and not refuses_ac:
        p["needs_ac"] = True
    if "airport" in low or "kia" in low:
        p["airport"] = True
    return p


class Trace:
    def __init__(self) -> None:
        self.steps: list[dict] = []

    def run(self, tool: str, **args) -> dict:
        t0 = time.time()
        result = call_tool(tool, args)
        self.steps.append({
            "step": len(self.steps) + 1,
            "tool": tool,
            "args": args,
            "status": "ok" if result.get("ok") else "error",
            "ms": int((time.time() - t0) * 1000),
            "observation": result.get("summary", "")[:600],
            "_data": result.get("data"),
        })
        return result


def _money(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"₹{x:,.0f}" if float(x).is_integer() else f"₹{x:,.2f}"


# Abbreviations that must not be treated as a sentence end ("Rs. 30/-", "No. 5").
ABBREV = ("Rs", "No", "Dr", "St", "Mr", "Mrs", "Ms", "e.g", "i.e", "etc", "vs",
          "approx", "Smt", "Shri", "Km", "rs", "no")


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text)
    marked = text
    for ab in ABBREV:
        marked = marked.replace(f"{ab}.", f"{ab}<DOT>")
    out = []
    for s in re.split(r"(?<=[.!?])\s+|\n", marked):
        s = s.replace("<DOT>", ".")
        s = re.sub(r"^[\s\-*•#\d.]+", "", s).strip()
        s = re.sub(r"\*\*", "", s).strip()
        if len(s) > 25:
            out.append(s)
    return out


RELEVANCE_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "should", "would", "will", "what", "which", "who", "how", "much", "many", "any",
    "and", "or", "for", "from", "with", "about", "tell", "give", "need", "want", "use",
    "using", "get", "got", "there", "this", "that", "these", "those", "it", "its",
    "i", "me", "my", "we", "you", "your", "if", "of", "in", "on", "at", "to", "by",
}


@functools.lru_cache(maxsize=1)
def _doc_freq() -> tuple[dict, int]:
    """Document frequency per stemmed token over the policy corpus.

    Used to stop filler words ("need", "pass") from qualifying a passage that a
    distinctive word ("replacement", "ITI") would not.
    """
    from collections import Counter
    from src.config import CORPUS_PATH
    counts: Counter = Counter()
    n = 0
    if CORPUS_PATH.exists():
        for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("metadata", {}).get("doc_type") not in ("policy", "pass"):
                continue
            n += 1
            toks = ({_stem(w) for w in re.findall(r"[a-z]{3,}", d["text"].lower())}
                    - RELEVANCE_STOP)
            counts.update(toks)
    return dict(counts), max(n, 1)


def _idf(token: str, df: dict, n: int) -> float:
    return math.log((n + 1) / (df.get(token, 0) + 1)) + 1.0


def _stem(w: str) -> str:
    for suf in ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _dedupe_repeats(text: str) -> str:
    """Drop a sentence that immediately repeats one already in the passage."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and p.rstrip(".") == out[-1].rstrip("."):
            continue
        out.append(p)
    return " ".join(out)


def _clean_chunk(text: str) -> str:
    """Flatten a retrieved chunk into prose: drop markdown headings and table rows."""
    keep = []
    for line in (text or "").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        if t.startswith("|"):
            # Render markdown table rows as readable text instead of dropping them:
            # for fee questions the table *is* the answer.
            cells = [c.strip() for c in t.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells if c):
                continue                     # separator row
            cells = [c for c in cells if c]
            if cells:
                keep.append(" — ".join(cells))
            continue
        keep.append(re.sub(r"^[•\-*]\s*", "", t))
    out = re.sub(r"\s+", " ", " ".join(keep)).strip()
    out = _dedupe_repeats(out)
    out = re.sub(r"^(?:PASS|ROUTE|CAMPUS):\s*", "", out)
    out = re.sub(r"\(operator:.*?\)\)?\.?\s*", "", out)
    out = re.sub(r"\s-\s", "; ", out)
    return out


def extract_relevant(results: list[dict], query: str, max_sentences: int = 6) -> list[str]:
    """Return coherent answer passages, one per retrieved chunk.

    Working at chunk level (rather than sentence level) keeps FAQ answers intact: the
    FAQ chunk already pairs one question with its full answer, so quoting it whole is
    both more accurate and less likely to splice unrelated sentences together.
    """
    q_stems = ({_stem(w) for w in re.findall(r"[a-z]{3,}", query.lower())}
               - RELEVANCE_STOP)
    if not q_stems:
        return []
    df, n_docs = _doc_freq()
    q_idf = {t: _idf(t, df, n_docs) for t in q_stems}
    q_total = sum(q_idf.values()) or 1.0
    ranked: list[tuple[float, str]] = []
    for r in results[:4]:
        body = _clean_chunk(r["text"])
        if len(body) < 20:
            continue
        # include the chunk heading in the relevance check - it carries the question
        # being answered ("22. What do I do if I lose my pass card?"), which is often
        # the only place the query's words appear.
        head = (r["metadata"].get("heading") or r["metadata"].get("pass_id")
                or r["metadata"].get("route_name") or "")
        # Acceptance is judged on the passage body only; the heading is too easy to
        # match ("What does the Vayu Vajra Gold pass cost?" would otherwise qualify
        # any question containing the word "cost").
        body_toks = ({_stem(w) for w in re.findall(r"[a-z]{3,}", body.lower())}
                     - RELEVANCE_STOP)
        head_tokens = ({_stem(w) for w in re.findall(r"[a-z]{3,}", head.lower())}
                       - RELEVANCE_STOP)
        # Acceptance: the passage body must share a distinctive term, OR the vector
        # match must be strong on its own -- good answers often never repeat the
        # question's words ("An institution-signed application form..." answers a
        # question about "documents" without using the word).
        body_matched = q_stems & body_toks
        head_matched = q_stems & head_tokens
        vec = r.get("score", 0)
        # Accept when the body shares a term, the vector hit is strong, or the chunk's
        # own question ("What do I do if I lose my pass card?") clearly matches the
        # user's. Two distinct heading terms are required so that a single common word
        # ("cost") cannot qualify an unrelated FAQ.
        if not body_matched and vec < 0.65 and len(head_matched) < 2:
            continue
        # Ranking: include the heading, which states the question being answered.
        idf_frac = sum(q_idf[t] for t in (body_matched | head_matched)) / q_total
        sents = _sentences(body) or [body]
        picked: list[str] = []
        for s in sents:
            if picked and len(" ".join(picked)) + len(s) > 520:
                break
            picked.append(s)
            if len(picked) >= 2 and len(" ".join(picked)) > 160:
                break
        passage = " ".join(picked).strip()
        if not passage:
            continue
        head_hit = 0.3 if head_matched else 0.0
        # Vector score dominates; IDF coverage of distinctive terms breaks ties.
        score = 4.0 * vec + idf_frac + head_hit
        ranked.append((score, passage))
    ranked.sort(key=lambda x: -x[0])
    out, seen_tokens = [], []
    for _, passage in ranked:
        toks = set(re.findall(r"[a-z0-9]+", passage.lower()))
        if any(len(toks & prev) / max(1, len(toks | prev)) > 0.55 for prev in seen_tokens):
            continue
        seen_tokens.append(toks)
        out.append(passage)
        if len(out) >= max_sentences:
            break
    return out


def _sources_block(trace: Trace) -> str:
    links = []
    for t in trace.steps:
        for url in re.findall(r"https?://[^\s)\"]+", json.dumps(t)):
            if url not in links:
                links.append(url)
    if not links:
        return ""
    top = links[:4]
    return "\n\n---\n**Sources:** " + " · ".join(f"<{u}>" for u in top)


def _audit_prices(answer: str, trace: Trace) -> dict:
    """Every ₹ figure in the answer must exist somewhere in a tool result."""
    obs = json.dumps([{k: v for k, v in t.items() if k != "_data"} for t in trace.steps])
    obs += json.dumps([t.get("_data") for t in trace.steps], default=str)
    obs_numbers = {float(x.replace(",", "")) for x in re.findall(r"[0-9][0-9,]*\.?[0-9]*", obs)}
    claimed = {float(x.replace(",", "")) for x in re.findall(r"₹\s*([0-9][0-9,]*\.?[0-9]*)", answer)}
    unverified = sorted(n for n in claimed if n not in obs_numbers)
    return {"ok": not unverified, "unverified_amounts": unverified, "n_checked": len(claimed)}


# --------------------------------------------------------------------------- composers
def _route_section(route: dict) -> str:
    data = route.get("data", {})
    lines = ["### Recommended route"]
    if data.get("direct"):
        seen_numbers, unique = set(), []
        for d in data["direct"]:
            if d["route_short_name"] in seen_numbers:
                continue
            seen_numbers.add(d["route_short_name"])
            unique.append(d)
        for i, d in enumerate(unique[:3]):
            tag = "AC (Vajra)" if d.get("is_ac") else "ordinary (non-AC)"
            prefix = "**Best option:** " if i == 0 else "- "
            walk = ""
            if d.get("walk_to_board_km", 0) > 0.2:
                walk += f" · walk {d['walk_to_board_km']} km to board"
            if d.get("walk_from_alight_km", 0) > 0.2:
                walk += f" · walk {d['walk_from_alight_km']} km after alighting"
            lines.append(
                f"{prefix}Route **{d['route_short_name']}** — {d['route_long_name']}\n"
                f"  Board **{d['board_stop']}** → alight **{d['alight_stop']}**"
                f" · ~{d['stops_passed']} stops · {d['trips_per_day']} trips/day · {tag}{walk}")
    elif data.get("transfers"):
        for t in data["transfers"][:2]:
            lines.append(f"- Take **{t['leg1_route']}** → change at **{t['transfer_stop']}** "
                         f"→ take **{t['leg2_route']}** (~{t['approx_stops']} stops total)")
    else:
        lines.append(route.get("summary", "No BMTC route found."))
    return "\n".join(lines)


ALGO_INTENT = re.compile(
    r"\b(dijkstra|a\*|astar|a-star|rrt\*?|algorithm|algorithms|pathfind\w*|"
    r"graph search|which algorithm)\b", re.I)
OPT_INTENT = re.compile(
    r"\b(fastest|quickest|optimal|optimum|best route|fewest transfers|least transfers|"
    r"cheapest (?:route|way|journey|option)|shortest path|best way)\b", re.I)
COMPARE_INTENT = re.compile(r"\b(compare|versus|vs\.?|all three|each algorithm|benchmark)\b", re.I)


def _plan_section(res: dict) -> str:
    data = res.get("data") or {}
    if not res.get("ok"):
        return "### Optimal route\n" + res.get("summary", "No route found.")
    legs = [l for l in data.get("legs", []) if l["mode"] in ("bus", "metro", "walk")]
    lines = ["### Optimal route", ""]
    _tf = data.get("total_fare_inr")
    lines.append(f"{(f'**Rs {_tf:.0f}**' if _tf is not None else '**Fare not published**')} · "
                 f"~{data.get('minutes_in_vehicle', 0):.0f} min in vehicle · "
                 f"{sum(1 for l in legs if l['mode'] != 'walk')} paid leg(s) · "
                 f"objective: {data.get('objective')} — {data.get('objective_label', '')}")
    lines += ["", "| Leg | Mode | From → To | Minutes | Fare |", "|---|---|---|---|---|"]
    for l in legs:
        icon = {"bus": "🚌", "metro": "🚇", "walk": "🚶"}.get(l["mode"], "•")
        fare = ("not published" if l["fare_inr"] is None
                else "—" if not l["fare_inr"] else f"₹{l['fare_inr']:.0f}")
        stops = f" ({l['stops']} stops)" if l.get("stops") and l["mode"] == "metro" else ""
        lines.append(f"| {l['label']} | {icon} | {l['from']} → {l['to']}{stops} | "
                     f"{l['minutes']:.0f} | {fare} |")
    lines += ["", f"*Found by **{data.get('algorithm', '?')}** — it expanded "
                  f"{data.get('nodes_expanded', 0):,} graph nodes in "
                  f"{data.get('elapsed_ms', 0):.0f} ms.*"]
    if data.get("cost_matches_replay") is False:
        lines.append("**Warning:** the path's cost did not survive an independent replay; "
                     "treat the ranking as unreliable.")
    lines += ["", f"Cost model: {data.get('cost_model', '')}"]
    return "\n".join(lines)


def _compare_section(res: dict) -> str:
    data = res.get("data") or {}
    if not res.get("ok"):
        return res.get("summary", "No comparison available.")
    lines = ["### Dijkstra vs A* vs RRT", "", res.get("summary", ""), "",
             "| Algorithm | Optimal? | Cost | Nodes expanded | Time |", "|---|---|---|---|---|"]
    for row in data.get("table", []):
        cost = "—" if row.get("cost") is None else f"{row['cost']:.2f}"
        lines.append(f"| {row['algorithm']} | {'yes' if row['optimal'] else 'no'} | {cost} | "
                     f"{row['nodes_expanded']:,} | {row['elapsed_ms']:.0f} ms |")
    lines += ["", "Costs are in generalised minutes (ride time + boarding + transfers). "
                  "A* and Dijkstra agreeing on cost is the optimality check; RRT makes no "
                  "such claim and is reported with its gap."]
    return "\n".join(lines)


def _cost_section(fare: dict, monthly: dict, profile: dict) -> str:
    fdata = fare.get("data", {})
    mdata = monthly.get("data", {})
    basis = {
        "gtfs_route_fare_stage": "BMTC GTFS fare stage for this route",
        "gtfs_zone_pair": "BMTC GTFS fare stage (zone pair)",
        "published_slab_estimate": f"published BMTC slab (Jan 2025) for ~{fdata.get('distance_km', '?')} km",
    }.get(fdata.get("basis"), fdata.get("basis", "unknown"))
    lines = [
        "### Cost breakdown",
        "| Item | Amount |",
        "|---|---|",
        f"| Single ride | {_money(fdata.get('fare_inr'))} |",
        f"| Return day ({mdata.get('trips_per_day', 2)} rides) | {_money(mdata.get('per_day_inr'))} |",
        f"| {mdata.get('travel_days_per_month')} days/month ({mdata.get('rides_per_month')} rides) "
        f"| **{_money(mdata.get('payg_monthly_inr'))}** |",
        "",
        f"Fare basis: {basis}.",
    ]
    if fdata.get("basis") == "published_slab_estimate":
        lines.append("This is an estimate, not a quoted ticket price; the GTFS feed has no "
                     "fare stage for this stop pair. AC services cost more.")
    return "\n".join(lines)


def _pass_section(pass_res: dict) -> str:
    data = pass_res.get("data", {})
    top = data.get("top_recommendation")
    lines = ["### Pass recommendation"]
    if not top:
        return "\n".join(lines + ["No pass matched this profile."])
    price = "no cost" if top.get("price_inr") == 0 else _money(top.get("price_inr"))
    lines.append(f"**{top['name']}** — {price}. {top.get('why', '')}")
    if top.get("monthly_saving_inr") is not None and (data.get("monthly_payg_inr") or 0) > 0:
        if top["monthly_saving_inr"] > 0:
            lines.append(f"At {_money(data.get('monthly_payg_inr'))}/month pay-as-you-go, this saves "
                         f"**{_money(top['monthly_saving_inr'])} per month**.")
        else:
            lines.append(f"It costs {_money(-top['monthly_saving_inr'])} more per month than paying "
                         f"per ride at {_money(data.get('monthly_payg_inr'))}.")
    others = [o for o in data.get("ranked_options", []) if o["pass_id"] != top["pass_id"]]
    if others:
        lines.append("")
        lines.append("Also considered: " + "; ".join(
            f"{o['name']} ({'price unverified' if o['price_inr'] is None else _money(o['price_inr'])}, "
            f"{o['verification_status']})" for o in others[:4]) + ".")
    if data.get("caveats"):
        lines.append("")
        lines.extend(f"- {c}" for c in data["caveats"][:3])
    return "\n".join(lines)


def _apply_section(chunks: list[dict], pass_id: str | None) -> str:
    p = BY_ID.get(pass_id or "", {})
    lines = ["### How to apply"]
    if p:
        lines.append(f"Apply via: {p['where_to_apply']}")
        lines.append("")
        lines.append("**Documents:** " + "; ".join(p["documents_required"][:6]) + ".")
        lines.append("")
        lines.append(f"Official portal: {p['application_url']}")
    if chunks:
        bullets = extract_relevant(chunks, "how to apply documents", max_sentences=3)
        if bullets:
            lines.append("")
            lines.append("**From the policy corpus:**")
            lines.extend(f"- {b}" for b in bullets)
    return "\n".join(lines)


def _verify_section(pass_res: dict, fare: dict | None, route: dict | None) -> str:
    lines = ["### Verify before you pay"]
    top = (pass_res.get("data") or {}).get("top_recommendation") if pass_res else None
    if top:
        lines.append(f"- Pass price source: {top['source_url']} — status "
                     f"**{top['verification_status']}**, last verified {top['last_verified']}.")
    if fare and fare.get("data", {}).get("basis") == "published_slab_estimate":
        lines.append("- The bus fare above is a distance-slab estimate. Confirm the actual ticket "
                     "or pass rate at the counter before you budget around it.")
    if route and route.get("ok"):
        lines.append("- Route and stop data: BMTC GTFS feed valid 2026-09-07 to 2027-09-07. "
                     "Schedules change without notice.")
    lines.append("- Pass rules changed in June 2026 (free student passes). Confirm on Seva Sindhu "
                 "before paying anything at a counter.")
    return "\n".join(lines)


def _policy_answer(query: str, res: dict, max_bullets: int = 7) -> str:
    results = (res.get("data") or {}).get("results", [])
    if not results:
        return ("I could not find that in the policy corpus. Try asking about student pass "
                "documents, Shakti, Seva Sindhu steps, or KSRTC fares.")
    bullets = extract_relevant(results, query, max_sentences=max_bullets)
    if not bullets:
        return ("**I don't have a verified answer for that.** \n\n"
                "Nothing in the indexed policy corpus (pass cards, six policy documents and "
                f"40 FAQs) addresses this closely enough to quote, so I won't guess.\n\n"
                "The corpus covers: student pass eligibility and documents, the 2026-27 free "
                "pass, BMTC/KSRTC pass prices, Shakti, Seva Sindhu application and "
                "troubleshooting, metro coverage and fares, and the Kanakapura Road corridor.\n\n"
                "For anything else, use the official channels: BMTC "
                "(mybmtc.karnataka.gov.in), KSRTC (ksrtc.karnataka.gov.in) or Seva Sindhu "
                "(sevaone.karnataka.gov.in).")
    lines = ["### What the policy documents say", ""]
    lines.extend(f"- {b}" for b in bullets)
    headings = [r["metadata"].get("heading") or r["metadata"].get("source_file")
                for r in results[:3]]
    lines.append("")
    lines.append("Matched documents: " + "; ".join(h for h in headings if h) + ".")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ planner
SMALL_TALK_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|namaste|good (morning|afternoon|evening))\b"
    r"|\b(what can you do|who are you|what are you|how do you work|"
    r"what do you know|help me understand|what is this app|how can you help)\b", re.I)


def _is_small_talk(message: str, has_od: bool) -> bool:
    """Greetings and 'what can you do' — conversational, not data questions."""
    if has_od:
        return False
    if len(message.split()) > 14:
        return False
    return bool(SMALL_TALK_RE.search(message))


def _capabilities_text() -> str:
    lines = [
        "### What I can do",
        "",
        "I plan journeys across **BMTC buses, Namma Metro (Purple, Green and Yellow lines)**",
        "and walking, price them from the published fare tables, and explain the trade-offs.",
        "",
        "- **Plan a trip** — try *Majestic to Whitefield* or *cheapest way from Koramangala "
        "to RVCE*. I rank the itineraries; you can re-sort by cheapest, fastest, fewest "
        "changes or least walking.",
        "- **Fares** — Namma Metro from BMRCL's revised fare chart (14.02.2025); BMTC from the "
        "published distance slab and, where the feed has it, the route's own fare stages.",
        "- **Metro questions** — which line serves a place, where to change, the fare between "
        "two stations.",
        "- **Passes** — whether a monthly pass beats paying per trip, and which one fits you.",
        "- **Documents** — what you need for a BMTC student pass, quoted from the policy corpus.",
        "- **KSRTC corridors** — published intercity fares (Kanakapura, Harohalli, Mysuru), "
        "quoted only: there is no KSRTC timetable or stop sequence in this build.",
        "- **Departure advice** — when to leave to arrive by a deadline.",
        "",
        "Two things I will not do: invent a fare, timetable or traffic reading, and call a "
        "price verified when it is an estimate. If a number is missing, I say so.",
        "",
        "Open **Plan a Trip** to enter an origin and destination, or just ask me here.",
    ]
    return "\n".join(lines)


def answer(message: str, profile: dict | None = None) -> dict[str, Any]:
    t0 = time.time()
    trace = Trace()
    prof = profile_from_message(message, profile or {})
    low = message.lower()

    o, d = extract_od(message)
    has_od = bool(o and d)
    wants_route = bool(re.search(r"\b(bus|route|commute|travel|reach|get to|go to|how do i get)\b", low))
    wants_fare = bool(re.search(r"\b(fare|cost|price|how much|cheap|expense|budget)\b", low))
    wants_pass = bool(re.search(r"\b(pass|monthly|season|concession|worth it|save)\b", low))
    wants_docs = bool(re.search(r"\b(document|documents|apply|application|eligib|renew|"
                                r"rejected|seva|bonafide|certificate|receipt|affidavit)\b", low))
    wants_dataset = bool(re.search(
        r"\b(dataset|data files?|source data|raw data|curated data|processed data)\b", low))
    wants_metro = ("metro" in low or "namma metro" in low
                   or bool(re.search(r"\b(green|purple|yellow|pink)\s+line\b", low)))
    wants_ksrtc = "ksrtc" in low or "intercity" in low or "kanakapura" in low

    daily = parse_daily_spend(message)
    days = int(prof.get("travel_days") or 24)

    # ------------------------------------------------------- greeting / capabilities
    # Answered without any tool: this is a description of the assistant, not a claim about
    # Bengaluru. It exists so the rules engine (the fallback when no model is running) can
    # hold a conversation instead of replying "I don't have a verified answer" to "hello".
    if _is_small_talk(message, has_od):
        return _finish(_capabilities_text(), trace, t0)

    # ------------------------------------------------------- source dataset lookup
    if wants_dataset:
        result = trace.run("search_dataset", query=message, k=5)
        matches = (result.get("data") or {}).get("results") or []
        lines = ["### Dataset search results", ""]
        if matches:
            for item in matches[:5]:
                source = item.get("source") or "local index"
                text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
                lines.append(f"- **{source}:** {text[:500]}")
        else:
            lines.append(result.get("summary") or
                         "No matching data was found in the local indexes.")
        return _finish("\n".join(lines), trace, t0)

    # ------------------------------------------- contact / phone-number question
    if re.search(r"\b(phone|telephone|helpline|contact number|mobile number|"
                 r"direct line|email address|fax)\b", low):
        res = trace.run("search_policy", query="contact helpline phone number office", k=3)
        txt = json.dumps((res.get("data") or {}).get("results", []), default=str).lower()
        numbers = re.findall(r"\b0\d{2,4}[\s-]?\d{6,8}\b|\b\+91[\s-]?\d{10}\b", txt)
        lines = ["### Contact details", ""]
        if numbers:
            lines.append("The policy corpus lists these numbers:")
            lines += [f"- {n}" for n in dict.fromkeys(numbers)][:3]
        else:
            lines += [
                "**I don't have a verified phone number for that.** No phone number, "
                "extension or direct line for BMTC or KSRTC officials exists in the indexed "
                "corpus, so I will not invent one.",
                "",
                "What I can point you to:",
                "- BMTC official site and contact page: <https://mybmtc.karnataka.gov.in/>",
                "- KSRTC official site: <https://ksrtc.karnataka.gov.in/>",
                "- Seva Sindhu (pass applications): <https://sevaone.karnataka.gov.in/>",
                "",
                "Counter addresses in the corpus are real; individual phone numbers are not "
                "part of the verified dataset.",
            ]
            if re.search(r"\b(surcharge|fare|fee|price|cost|pass|document|eligib|"
                         r"apply|refund|scheme)\b", low):
                ctx = trace.run("search_policy", query=message, doc_type="any", k=3)
                extra = extract_relevant((ctx.get("data") or {}).get("results", []),
                                         message, max_sentences=2)
                if extra:
                    lines += ["", "**On the rest of your question, from the policy corpus:**"]
                    lines += [f"- {b}" for b in extra]
        return _finish("\n".join(lines), trace, t0, sources=[
            "https://mybmtc.karnataka.gov.in/", "https://ksrtc.karnataka.gov.in/",
            "https://sevaone.karnataka.gov.in/"])

    # --------------------------------------------------- a specific, named pass
    named = detect_named_pass(message)
    if (named and not RECOMMEND_INTENT.search(message) and not has_od
            and not CATEGORY_INTENT.search(message)
            and not REPLACEMENT_INTENT.search(message)
            and not APPLY_INTENT.search(message)):
        coverage_q = re.search(r"\b(work on|works on|valid on|cover|covered|covers|"
                               r"allowed|can i use|eligible|free on|travel free|"
                               r"travel on|use (?:it )?on|good on)\b", low)
        if coverage_q:
            # "Does X cover Y?" is an eligibility question: the corpus answers it
            # directly, so lead with that and keep the card's coverage line as context.
            res = trace.run("search_policy", query=message, doc_type="any", k=4)
            card_res = trace.run("get_pass", pass_id=named)
            passages = extract_relevant((res.get("data") or {}).get("results", []),
                                        message, max_sentences=3)
            card = card_res.get("data") or BY_ID[named]
            body = [f"### {card['name']}", ""]
            if passages:
                body += [f"- {b}" for b in passages]
            body += ["", f"**Coverage on the pass card:** {', '.join(card.get('service_classes', []))}."]
            return _finish("\n".join(body), trace, t0)
        trace.run("get_pass", pass_id=named)
        return _finish(_pass_card(named), trace, t0)

    # ------------------------------------ course-category or replacement questions
    if not has_od and (CATEGORY_INTENT.search(message) or REPLACEMENT_INTENT.search(message)) \
            and not RECOMMEND_INTENT.search(message) \
            and not re.search(r"\bfrom\b.*\bto\b", low):
        res = trace.run("search_policy", query=message, doc_type="any", k=4)
        return _finish(_policy_answer(message, res), trace, t0)

    # --------------------------------------------- optimal routing / graph search
    algo_q = ALGO_INTENT.search(message)
    if (algo_q or (has_od and OPT_INTENT.search(message))) and not wants_pass \
            and not wants_docs and not named:
        if not has_od and algo_q:
            return _finish(
                "### How the routing works\n\n"
                "Three search algorithms are wired into this agent, and they do different "
                "jobs:\n\n"
                "- **Dijkstra** — the reference. It expands the cheapest-first frontier over "
                "the whole multimodal graph (every bus stop and metro station, every "
                "scheduled ride, plus short walks between nearby stops) and is provably "
                "optimal for the cost it is given.\n"
                "- **A\\*** — the same search with a geometric lower bound: straight-line "
                "distance to the destination divided by the fastest speed seen anywhere in "
                "the graph. Because that can never overestimate, A* returns the same "
                "optimal cost as Dijkstra while expanding fewer nodes. The benchmark checks "
                "that the two costs agree on every test corridor.\n"
                "- **RRT** — a sampling planner. It grows a tree of random extensions toward "
                "the destination and returns *a* feasible route in milliseconds, with no "
                "optimality claim. It is used to seed an upper bound for A*, and in "
                "continuous space (RRT\\*) for walking connectors.\n\n"
                "Ask for a route between two places and name an algorithm to see it run, or "
                "ask me to compare all three on a specific journey.",
                trace, t0)
        objective = ("cheapest" if re.search(r"\bcheap", low) else
                     "fewest_transfers" if re.search(r"\b(fewest|least) transfers\b", low)
                     else "fastest")
        if COMPARE_INTENT.search(message):
            res = trace.run("compare_routing_algorithms", origin=o, destination=d,
                            objective=objective)
            return _finish(_compare_section(res), trace, t0)
        algo = "dijkstra" if re.search(r"\bdijkstra\b", low) else "astar"
        res = trace.run("plan_optimal_route", origin=o, destination=d,
                        objective=objective, algorithm=algo)
        cheapest_blocked = False
        if not res.get("ok") and objective == "cheapest":
            # A cheapest ranking needs a price for every leg. When one operator publishes
            # none (KSRTC intercity legs have no fare table in this build) the search
            # cannot order options by price. Show the fastest verified route and say why,
            # instead of the unhelpful "No route found".
            res = trace.run("plan_optimal_route", origin=o, destination=d,
                            objective="fastest", algorithm=algo)
            cheapest_blocked = True
        body = _plan_section(res)
        if res.get("ok") and wants_metro and not any(
                l["mode"] == "metro" for l in (res.get("data") or {}).get("legs", [])):
            # The traveller asked for metro "where practical", so answer that directly
            # instead of silently returning an all-bus plan.
            ml = trace.run("plan_metro_leg", origin=o, destination=d)
            if not ml.get("ok"):
                body += ("\n\n**Metro is not practical for this trip:** "
                         + (ml.get("summary") or "no usable metro leg") +
                         " — that is why the plan above is bus-only.")
            else:
                md = ml.get("data") or {}
                fare_bit = ""
                fr = md.get("fare_inr") or {}
                if fr.get("min") is not None:
                    lo, hi = fr["min"], fr["max"]
                    fare_bit = (f", {_money(lo)}" if lo == hi else f", {_money(lo)}-{_money(hi)}")
                body += (f"\n\n**Metro alternative (not the route above):** board at "
                         f"{md.get('board_station', {}).get('name', '?')}, alight at "
                         f"{md.get('alight_station', {}).get('name', '?')}, "
                         f"{md.get('stops', '?')} stops{fare_bit}. The ranked route above did "
                         f"not use it; prices for both come from the published tables.")
        if cheapest_blocked and res.get("ok"):
            body += ("\n\n> **Why this is not ranked as the cheapest.** The cheapest "
                     "objective needs a published fare on every leg, and this journey has "
                     "at least one leg with none (KSRTC intercity fares are not in any feed "
                     "this project trusts). Ranking by price would mean inventing a number, "
                     "so this is the fastest verified route instead; every BMTC and metro "
                     "leg shown is priced from the published tables.")
        if res.get("ok"):
            body += ("\n\n**How this was found:** " + ("Dijkstra" if algo == "dijkstra"
                    else "A*") + " over the corridor graph — " +
                    f"{res['data'].get('graph', {}).get('stops', 0):,} stops, "
                    f"{res['data'].get('graph', {}).get('ride_edges', 0):,} ride edges. "
                    "Fares come from the published tables, not from the search.")
        return _finish(body, trace, t0)

    # ------------------------------------------------------- walking connector
    if has_od and re.search(r"\b(on foot|walk|walking|by foot|walkable)\b", low) \
            and not wants_pass and not wants_docs:
        res = trace.run("plan_walk_connector", origin=o, destination=d)
        d_ = res.get("data") or {}
        lines = ["### On foot", "", res.get("summary", "")]
        if d_.get("path"):
            lines += ["", f"- RRT\\* tree: {d_['tree_size']} nodes, "
                          f"{d_['length_km']:.2f} km path, "
                          f"detour ratio {d_['detour_ratio']:.2f}x the straight line",
                      f"- Data status: **{d_['data_status']}** — {d_.get('note', '')}"]
        lines += ["", "If you would rather ride, ask for the route and I will send you the "
                      "bus/metro itinerary with published fares."]
        return _finish("\n".join(lines), trace, t0)

    # ---------------------------------------------------------------- KSRTC only
    ksrtc_route_request = re.search(r"\b(route|which bus|how do i get|how to reach|bus from)\b", low)
    if wants_ksrtc and not ksrtc_route_request:
        k = trace.run("get_ksrtc_options", place="Harohalli" if "harohalli" in low else "")
        season = re.search(r"\b(monthly|season ticket|mst|month pass|commute pass)\b", low)
        lines = ["### KSRTC options on the Kanakapura Road corridor", "",
                 k.get("summary", "")]
        for row in (k.get("data") or {}).get("options", [])[:4]:
            cls = row["service_class"]
            if row.get("fare_inr") is not None:
                lines.append(f"- **{cls}: {row['from_place']} → {row['to_place']}** — "
                             f"{_money(row['fare_inr'])} ({row['fare_basis'].replace('_', ' ')}, "
                             f"{row['verification_status']}). {row.get('notes','')[:180]}")
            else:
                lines.append(f"- **{cls}: {row['from_place']} → {row['to_place']}** — "
                             f"fare not published ({row['verification_status']}). Ask at the depot.")
        if season:
            lines += ["", "### Monthly season ticket",
                      "KSRTC issues monthly season tickets (MST) at depot counters, and the "
                      "monthly rate is not published in any feed or portal page we could "
                      "verify. The per-trip ordinary fare above is the only figure you can "
                      "budget from; the monthly rate must be confirmed at the depot "
                      "(UNVERIFIED_PLACEHOLDER for the monthly figure itself)."]
        lines += ["", "### Verify before you pay",
                  "- Only the fares marked *published* are real quoted fares. Anything marked "
                  "*derived* is an interpolation and is labelled as such."]
        return _finish("\n".join(lines), trace, t0)

    # ------------------------------------------- metro line / station-fare questions
    metro_rules_q = re.search(
        r"\b(penalty|penalt|luggage|baggage|token|overstay|mismatch|ticketless|"
        r"refund|discount|smart ?card|ncmc|rule|rules|helpline|fine|fare rules|"
        r"child|children|recharge|top.?up)\b", low)

    line_m = re.search(r"\b(green|purple|yellow|pink|blue)\s+line\b", low)
    line_detail_intent = re.search(
        r"\b(about|stations?|list|details|route of|how many stations|fare|fares|"
        r"price|prices|open|opening|status)\b", low)
    if line_m and line_detail_intent and not has_od and not wants_pass:
        colour = line_m.group(1)
        det = trace.run("metro_line_details", line=colour)
        d = det.get("data") or {}
        if not det.get("ok"):
            return _finish(f"**{det.get('summary','')}**", trace, t0)
        status = d.get("status", "operational")
        lines = [f"### {colour.title()} Line", "", det.get("summary", ""), ""]
        if status != "operational":
            lines += [f"> **This line is not carrying passengers yet.** {d.get('note') or ''}",
                      ""]
        fares = [f for f in d.get("fares_from_terminus", []) if f["fare_inr"]]
        if fares:
            origin = d["stations"][0]
            lines += [f"**Fare from {origin}**", "",
                      "| Station | Fare | Basis |", "|---|---|---|"]
            shown = [f for f in d["fares_from_terminus"] if f["fare_inr"]]
            if len(shown) > 14:                      # keep long lines readable
                step = max(1, len(shown) // 12)
                shown = shown[:6] + shown[6::step][:6] + [shown[-1]]
                shown = {(f["station"]): f for f in shown}.values()
            for f in shown:
                basis = ("published" if f["fare_source"] == "published_matrix"
                         else "derived")
                lines.append(f"| {f['station']} | {_money(f['fare_inr'])} | {basis} |")
            lines.append("")
        lines.append("**All stations:** " + " → ".join(d.get("stations", [])))
        if status != "operational":
            lines += ["", "*Fares on this line are derived from the published BMRCL "
                          "hop-count fare table and are labelled as such; confirm at a "
                          "counter once the line opens.*"]
        return _finish("\n".join(lines), trace, t0)

    station_fare = None
    if wants_metro and re.search(r"\b(fare|price|cost|how much)\b", low) and not wants_pass:
        fare_text = re.sub(r"[()]", " ", low)
        mf = re.search(rf"from\s+({SPAN}+?)\s+to\s+({SPAN}+?)(?:[.?!]|$)", fare_text) or \
             re.search(rf"({SPAN}+?)\s+to\s+({SPAN}+?)(?:[.?!]|$)", fare_text)
        if mf:
            res = trace.run("metro_fare", from_station=mf.group(1).strip(),
                            to_station=mf.group(2).strip())
            if res.get("ok"):
                station_fare = res
                fd = res["data"]
                lines = ["### Metro fare", "", res["summary"], "",
                         "| | |", "|---|---|",
                         f"| From | {fd['from']['name']} |",
                         f"| To | {fd['to']['name']} |",
                         f"| Stops | {fd['stops']} |",
                         f"| Fare | {_money(fd['fare_inr'])} |",
                         f"| Basis | {fd['fare_source'].replace('_', ' ')} |"]
                sc = fd.get("smartcard_fares") or {}
                if sc.get("smartcard_offpeak") is not None:
                    lines += ["", "**With a smart card / NCMC:**",
                              f"- Peak (Mon-Sat): {_money(sc['smartcard_peak'])}",
                              f"- Non-peak (Mon-Sat): {_money(sc['smartcard_offpeak'])}",
                              f"- Sundays and national holidays: {_money(sc['smartcard_offpeak'])}"]
                if fd.get("service_pending"):
                    lines += ["", "> **One of these stations is not in service yet.** The "
                                  "fare is derived from the published BMRCL fare table so you "
                                  "can plan, but no train runs there today."]
                return _finish("\n".join(lines), trace, t0)

    # --------------------------------------- metro line question without an origin
    if wants_metro and not has_od and not wants_pass and not metro_rules_q:
        named = None
        for cand in re.findall(r"\b(whitefield|kadugodi|majestic|jayanagar|silk institute|"
                               r"electronic city|banashankari|indiranagar|hebbal|"
                               r"mg road|kr puram|bommasandra|challaghatta)\b", low):
            named = cand
            break
        from_line = ""
        mfl = re.search(r"\b(?:from|on|off)\s+(?:the\s+)?(green|purple|yellow)\s+line", low)
        if mfl:
            from_line = mfl.group(1)
        info = trace.run("metro_info", place=named or "Majestic", from_line=from_line)
        lines = ["### Namma Metro", "", info.get("summary", "")]
        d = info.get("data") or {}
        for ln in d.get("lines", []):
            lines.append(f"- **{ln['line']} Line** — {ln['termini']}")
        if d.get("transfer"):
            lines.append(f"- **Change at {d['transfer']['station']}** to switch from the "
                         f"{from_line.title()} Line to the {d['lines'][0]['line']} Line.")
        elif d.get("interchanges"):
            lines.append("- Interchanges: " + ", ".join(
                i["station"] for i in d["interchanges"]))
        # Only reach for the policy corpus if the question actually asks about a rule or a
        # document. Appending it unconditionally is how a metro question ended up with a
        # "what the policy documents say" section repeating the same stations.
        if wants_docs or POLICY_INTENT.search(message):
            policy = trace.run("search_policy", query=message, doc_type="any", k=2)
            lines += ["", _policy_answer(message, policy, max_bullets=2)]
        return _finish("\n".join(lines), trace, t0)

    # ------------------------------------------------------------- metro question
    if wants_metro and has_od and not wants_pass:
        metro = trace.run("plan_metro_leg", origin=o, destination=d)
        if not metro.get("ok"):
            sf = trace.run("metro_fare", from_station=o, to_station=d)
            if sf.get("ok"):
                fd = sf["data"]
                lines = ["### Metro fare", "", sf["summary"], "",
                         "| | |", "|---|---|",
                         f"| From | {fd['from']['name']} |",
                         f"| To | {fd['to']['name']} |",
                         f"| Stops | {fd['stops']} |",
                         f"| Fare | {_money(fd['fare_inr'])} |",
                         f"| Basis | {fd['fare_source'].replace('_', ' ')} |",
                         f"| Line(s) | {', '.join(fd['lines_on_route'])} |"]
                sc = fd.get("smartcard_fares") or {}
                if sc.get("smartcard_offpeak") is not None:
                    lines += ["", "**With a smart card / NCMC:**",
                              f"- Peak (Mon-Sat): {_money(sc['smartcard_peak'])}",
                              f"- Non-peak (Mon-Sat): {_money(sc['smartcard_offpeak'])}",
                              f"- Sundays and national holidays: {_money(sc['smartcard_offpeak'])}"]
                if fd.get("service_pending"):
                    lines += ["", "> **This station is not in service yet.** The fare is "
                                  "derived from the published BMRCL fare table so you can "
                                  "plan ahead, but no train runs there today."]
                return _finish("\n".join(lines), trace, t0)
        fare = trace.run("estimate_fare", origin=o, destination=d, mode="metro") if metro.get("ok") else None
        lines = ["### Metro plan", "", metro.get("summary", "")]
        m = metro.get("data") or {}
        if m:
            lines += [f"- Board **{m['board_station']['name']}** "
                      f"({m['walk_to_station_km']} km away)",
                      f"- Alight **{m['alight_station']['name']}** "
                      f"({m['walk_from_station_km']} km away)"]
            if m.get("interchange"):
                lines.append(f"- Change at **{m['interchange']}**")
            lines.append(f"- {m['stops']} stops on the "
                         + " → ".join(x["line"] + " Line" for x in m["lines"]))
            if m.get("fare_inr"):
                lo, hi = m["fare_inr"]["min"], m["fare_inr"]["max"]
                lines.append(f"- Fare {_money(lo) if lo == hi else f'{_money(lo)}–{_money(hi)}'} "
                             f"(distance-based token fare)")
        lines += ["", "Metro fares are not covered by the state free student pass; the bus legs "
                      "on either side are. Ask me for the full door-to-door plan if you want the "
                      "bus legs priced too."]
        return _finish("\n".join(lines), trace, t0)

    # ------------------------------------------------ policy / documents question
    if wants_docs and not has_od and not re.search(r"worth|should i|which pass|best pass", low):
        res = trace.run("search_policy", query=message, doc_type="any", k=4)
        body = _policy_answer(message, res)
        return _finish(body, trace, t0)

    # ------------------------------------------------------- full trip plan
    if has_od and (wants_route or wants_fare or wants_pass or wants_docs):
        route = trace.run("find_routes", origin=o, destination=d)
        top = (route.get("data") or {}).get("direct", [])
        top_t = (route.get("data") or {}).get("transfers", [])
        board = top[0]["board_stop"] if top else None
        alight = top[0]["alight_stop"] if top else None
        fare = trace.run("estimate_fare", origin=board or o, destination=alight or d,
                         route_id=top[0]["route_id"] if top else None, mode="bus")
        one_way = (fare.get("data") or {}).get("fare_inr")
        monthly = trace.run("estimate_monthly_cost", one_way_fare_inr=one_way or 0,
                            legs_per_trip=1, trips_per_day=2, travel_days_per_month=days)
        crosses = bool(o and re.search(r"harohalli|kanakapura|dsu|bannikuppe", (o + " " + (d or "")).lower()))
        pass_res = trace.run("recommend_pass",
                             monthly_payg_cost_inr=(monthly.get("data") or {}).get("payg_monthly_inr", 0),
                             needs_ac=prof["needs_ac"],
                             is_woman_karnataka_resident=prof["is_woman"],
                             is_student=prof["is_student"],
                             crosses_bmtc_limits=crosses,
                             airport_travel=prof["airport"])
        policy = trace.run("search_policy",
                           query=f"documents required and how to apply for a student bus pass "
                                 f"{'free 2026' if not prof['needs_ac'] else 'AC Vajra'}",
                           doc_type="any", k=3)
        metro = None
        if wants_metro:
            metro = trace.run("plan_metro_leg", origin=o, destination=d)

        top_pass = (pass_res.get("data") or {}).get("top_recommendation") or {}
        o_name = ((route.get("data") or {}).get("origin") or {}).get("resolved") or o
        d_name = ((route.get("data") or {}).get("destination") or {}).get("resolved") or d
        lines = [f"**{o_name} → {d_name}**", ""]
        if metro and metro.get("ok"):
            lines.append(f"_Metro option:_ {metro['summary']}")
            lines.append("")
        lines.append(_route_section(route))
        lines.append("")
        lines.append(_cost_section(fare, monthly, prof))
        lines.append("")
        lines.append(_pass_section(pass_res))
        lines.append("")
        lines.append(_apply_section((policy.get("data") or {}).get("results", []),
                                    top_pass.get("pass_id")))
        lines.append("")
        lines.append(_verify_section(pass_res, fare, route))
        return _finish("\n".join(lines), trace, t0)

    # --------------------------------------- "does the pass cover X" -> policy corpus
    if wants_pass and not has_od and not re.search(
            r"\b(which pass|what pass|should i|do i need|worth it|best pass)\b", low) and re.search(
            r"\b(cover|covered|covers|include|includes|valid on|work on|works on|automatic)\b", low):
        res = trace.run("search_policy", query=message, doc_type="any", k=4)
        return _finish(_policy_answer(message, res), trace, t0)

    # ------------------------------------------------------------- pass question
    if wants_pass or wants_docs:
        lump = parse_monthly_spend(message)
        if daily:
            monthly = trace.run("estimate_monthly_cost", one_way_fare_inr=daily,
                                legs_per_trip=1, trips_per_day=1, travel_days_per_month=days)
        elif lump:
            monthly = {"data": {"payg_monthly_inr": lump, "per_day_inr": None,
                                "rides_per_month": None, "trips_per_day": 2,
                                "travel_days_per_month": days}}
        else:
            monthly = {"data": {"payg_monthly_inr": 0.0, "per_day_inr": 0,
                                "rides_per_month": 0, "trips_per_day": 2,
                                "travel_days_per_month": days}}
        pass_res = trace.run("recommend_pass",
                             monthly_payg_cost_inr=(monthly.get("data") or {}).get("payg_monthly_inr", 0),
                             needs_ac=prof["needs_ac"],
                             is_woman_karnataka_resident=prof["is_woman"],
                             is_student=prof["is_student"],
                             crosses_bmtc_limits=False, airport_travel=prof["airport"])
        policy = trace.run("search_policy",
                           query=message, doc_type="any", k=3)
        top_pass = (pass_res.get("data") or {}).get("top_recommendation") or {}
        lines = ["### Pass advice", ""]
        if daily:
            lines.append(f"You spend {_money(daily)} a day, which is "
                         f"**{_money((monthly['data']).get('payg_monthly_inr'))} a month** "
                         f"over {days} travel days.")
            lines.append("")
        elif lump:
            lines.append(f"You said you spend about **{_money(lump)} a month** on buses.")
            lines.append("")
        lines.append(_pass_section(pass_res))
        lines.append("")
        lines.append(_apply_section((policy.get("data") or {}).get("results", []),
                                    top_pass.get("pass_id")))
        lines.append("")
        lines.append(_verify_section(pass_res, None, None))
        return _finish("\n".join(lines), trace, t0)

    # ------------------------------------------------------------- metro question
    if wants_metro:
        if has_od:
            metro = trace.run("plan_metro_leg", origin=o, destination=d)
            fare = trace.run("estimate_fare", origin=o, destination=d, mode="metro")
            lines = ["### Metro plan", "", metro.get("summary", "")]
            m = metro.get("data") or {}
            if m:
                lines.append(f"- Board **{m['board_station']['name']}** "
                             f"({m['walk_to_station_km']} km away)")
                lines.append(f"- Alight **{m['alight_station']['name']}** "
                             f"({m['walk_from_station_km']} km away)")
                if m.get("interchange"):
                    lines.append(f"- Change at **{m['interchange']}**")
                lines.append(f"- {m['stops']} stops on the "
                             + " → ".join(x["line"] + " Line" for x in m["lines"]))
                if m.get("fare_inr"):
                    lo, hi = m["fare_inr"]["min"], m["fare_inr"]["max"]
                    fare_txt = (_money(lo) if lo == hi
                                else f"{_money(lo)}–{_money(hi)}")
                    lines.append(f"- Fare {fare_txt} (distance-based token fare)")
            lines += ["", "Metro fares are not covered by the state free student pass; "
                          "the bus legs on either side are."]
            return _finish("\n".join(lines), trace, t0)
        res = trace.run("search_policy", query=message, doc_type="any", k=4)
        return _finish(_policy_answer(message, res), trace, t0)

    # ---------------------------------------------------------------- policy Q&A
    if wants_docs or len(message.split()) > 3:
        res = trace.run("search_policy", query=message, doc_type="any", k=4)
        return _finish(_policy_answer(message, res), trace, t0)

    return _finish(
        "I can help with three things:\n\n"
        "- **Routes** — \"How do I get from Jayanagar to DSU Harohalli?\"\n"
        "- **Passes** — \"Which student pass should I get?\" or \"Is a monthly pass worth it "
        "if I spend Rs 60 a day?\"\n"
        "- **Policy** — \"What documents do I need?\" or \"Does Shakti cover AC buses?\"\n\n"
        "Tell me your origin and destination (or your daily spend) and I will price the trip "
        "and pick the pass.", trace, t0)


# Tools whose output counts as "looking it up" for each kind of question. Kept in sync
# with src.agent._TOOL_FAMILIES by the test suite rather than by an import, so the rules
# engine keeps no dependency on the LLM layer.
_METRO_TOOLS = {"metro_info", "metro_fare", "metro_line_details", "plan_metro_leg"}
_PASS_TOOLS = {"recommend_pass", "get_pass", "estimate_monthly_cost"}
_FARE_TOOLS = _METRO_TOOLS | _PASS_TOOLS | {"estimate_fare"}
_POLICY_TOOLS = {"search_policy", "get_pass"}
_ROUTE_TOOLS = {"find_routes", "plan_optimal_route", "plan_metro_leg",
                "compare_routing_algorithms", "estimate_fare"}


def _ground(text: str, steps: list[dict]) -> str:
    """Remove sections the tools do not actually support.

    A template bug shipped once: "which metro line serves Majestic?" produced a confident
    metro answer followed by a *policy* section quoting metro lines, because the policy
    chunk happened to mention them. Every sentence looked sourced; the section was not.
    Rather than trust the templates, the finished answer is re-checked against the tools
    that ran and any unsupported trailing section is dropped, with a note saying so.
    """
    used = {s.get("tool", "") for s in steps}
    headings = [(m.start(), m.group(1).strip()) for m in
                re.finditer(r"^#{2,3} (.+)$", text, re.M)]
    if not headings:
        return text
    rule = {
        "metr": _METRO_TOOLS, "line": _METRO_TOOLS,
        "polic": _POLICY_TOOLS, "document": _POLICY_TOOLS, "corpus": _POLICY_TOOLS,
        "pass": _PASS_TOOLS, "fare": _FARE_TOOLS, "cost": _FARE_TOOLS,
        "route": _ROUTE_TOOLS, "itinerar": _ROUTE_TOOLS, "walk": _ROUTE_TOOLS,
    }
    kept: list[str] = []
    dropped: list[str] = []
    for i, (pos, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        section = text[pos:end]
        low = title.lower()
        needed = next((tools for key, tools in rule.items() if key in low), None)
        if needed is None or (used & needed):
            kept.append(section)
        else:
            dropped.append(title)
    if not dropped:
        return text
    out = text[:headings[0][0]] + "".join(kept)
    out = out.rstrip() + ("\n\n*Dropped " + ", ".join(f"**{d}**" for d in dropped)
                          + ": the tools this answer ran do not cover that section.*")
    return out


def _finish(text: str, trace: Trace, t0: float,
            sources: list[str] | None = None) -> dict[str, Any]:
    if sources is None:
        text += _sources_block(trace)
    elif sources:
        text += "\n\n---\n**Sources:** " + " · ".join(f"<{u}>" for u in sources)
    audit = _audit_prices(text, trace)
    steps = [{k: v for k, v in s.items() if k != "_data"} for s in trace.steps]
    text = _ground(text, steps)
    return {
        "answer": text.strip(),
        "trace": steps,
        "engine": "rules",
        "latency_ms": int((time.time() - t0) * 1000),
        "tools_used": [s["tool"] for s in trace.steps],
        "price_audit": audit,
    }
