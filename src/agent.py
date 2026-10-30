"""CampusTransit agent facade.

Two interchangeable controllers sit behind one `chat()` method:

* `llm`   - Ollama + tool calling (llama3.1:8b by default). The model chooses tools;
            every number still comes from the deterministic tool layer.
* `rules` - the deterministic planner in rules_agent.py. Used automatically when
            Ollama is not reachable, so the demo never depends on a model being up.

Both return the same shape: answer, trace, engine, latency_ms, tools_used, price_audit.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from src.config import CHAT_BUDGET_S, LLM_ENABLED, LLM_MODEL, OLLAMA_TIMEOUT_S
from src.rules_agent import answer as rules_answer
from src.services import ollama_client
from src.services.ollama_client import OllamaError
from src.tools.registry import TOOLS, call_tool

MAX_TOOL_ROUNDS = 6
# Below this much remaining budget a further model round cannot finish, so stop cleanly.
MIN_ROUND_S = 12.0


class DeadlineExceeded(Exception):
    """The chat turn ran out of its wall-clock budget before the model finished."""


class UngroundedAnswer(RuntimeError):
    """The model produced an answer the tool layer cannot corroborate.

    Raised instead of returning it: a confident wrong fare is the one failure this project
    exists to avoid, so the caller falls back to the deterministic engine and says why.
    """

SYSTEM_PROMPT = """You are CampusTransit, a Bengaluru student commute and transit-pass advisor.

HARD RULES
1. NEVER invent fares, pass prices, route numbers, timings or stops. Every number you
   report must come from a tool result. If a tool says a price is unverified or
   estimated, repeat that caveat.
2. NEVER do arithmetic yourself. Call estimate_monthly_cost and recommend_pass.
3. Resolve vague place names with find_stop before calling find_routes.
4. For eligibility, documents or "how do I apply" questions, call search_policy.
5. Cite the source for each fact (the tool result includes source URLs).

WORKFLOW
Step 1: If the origin or destination is missing, ask for it. Ask once, briefly.
Step 2: find_stop -> find_routes to build the itinerary.
Step 3: estimate_fare for the ride, then estimate_monthly_cost.
Step 4: recommend_pass for the financial decision.
Step 5: search_policy for documents and the application process.
Step 6: Answer in this structure:
   ## Recommended Route
   ## Cost Breakdown
   ## Pass Recommendation
   ## How to Apply
   ## Verify Before You Pay

Be concise. Use INR. Maximum 10 tool calls per question. Never claim a pass is free
unless recommend_pass returned a price of 0."""


def ollama_available(timeout: float = 1.0) -> tuple[bool, str]:
    """Is a usable model actually there? Returns (usable, human-readable reason).

    "Usable" means the server answered *and* the configured model is pulled — a running
    server with the wrong model is a failure that should be reported, not discovered
    halfway through a 40-second generation.
    """
    st = ollama_client.status(timeout=timeout)
    if st["available"] and st["model_pulled"]:
        return True, st["reason"]
    return False, st["reason"] or "ollama unavailable"


class CampusTransitAgent:
    def __init__(self, force_engine: str | None = None) -> None:
        """``force_engine="llm"`` skips the env gate and tries the model anyway.

        Used by the live-model test and by ``python -m src.agent``, so neither has to
        depend on how the shell happens to be configured. A forced engine still falls back
        to rules on failure — forcing a *try*, not a guaranteed answer.
        """
        self.last_withheld: dict[str, Any] | None = None
        self.llm_ok, self.llm_reason = (False, "disabled")
        if force_engine == "llm":
            self.llm_ok, self.llm_reason = True, f"forced (model={LLM_MODEL})"
        elif force_engine == "rules":
            self.llm_ok, self.llm_reason = False, "forced to rules"
        elif LLM_ENABLED in ("auto", "on", "true", "1"):
            self.llm_ok, self.llm_reason = ollama_available()
        self.engine = "llm" if self.llm_ok else "rules"

    # ------------------------------------------------------------------ public API
    def chat(self, message: str, profile: dict | None = None,
             history: list[dict] | None = None,
             budget_s: float | None = None) -> dict[str, Any]:
        """Answer a question. The model tries first; the rules engine is always behind it.

        A model that is down, slow, or confused must never turn into a failed request:
        the traveller still gets a verified, tool-derived answer, with the reason the
        model was not used attached as ``fallback_reason``.
        """
        if self.engine == "llm":
            try:
                return self._chat_llm(message, profile, history, budget_s=budget_s)
            except DeadlineExceeded as exc:
                # Not a failure to hide: the traveller gets a verified answer now and the
                # reason the model was cut off, so the next attempt can be a shorter turn.
                return self._fallback(message, profile, f"deadline: {exc}")
            except OllamaError as exc:
                return self._fallback(message, profile, f"{exc.kind}: {exc}")
            except UngroundedAnswer as exc:
                return self._fallback(message, profile, f"withheld: {exc}")
            except Exception as exc:                  # a bad model output, not a bad request
                return self._fallback(message, profile, f"{type(exc).__name__}: {exc}")
        t0 = time.time()
        out = rules_answer(message, profile)
        out["provider"] = "rules"
        out["latency_ms"] = int((time.time() - t0) * 1000)
        out["budget_s"] = CHAT_BUDGET_S
        if self.llm_reason and self.llm_reason != "disabled":
            out["fallback_reason"] = self.llm_reason
        return out

    @staticmethod
    def _fallback(message: str, profile: dict | None, reason: str) -> dict[str, Any]:
        t0 = time.time()
        out = rules_answer(message, profile)
        out["engine"] = "rules"
        out["provider"] = "rules"
        out["fallback_reason"] = reason
        out["latency_ms"] = int((time.time() - t0) * 1000)
        out["budget_s"] = CHAT_BUDGET_S
        return out

    # --------------------------------------------------------------------- LLM path
    def _chat_llm(self, message: str, profile: dict | None,
                  history: list[dict] | None = None,
                  budget_s: float | None = None) -> dict[str, Any]:
        """Ollama chooses the tools; the tools supply every number.

        The loop is deliberately boring: ask -> run the requested tools -> feed the
        results back -> ask again. Bounded at MAX_TOOL_ROUNDS so a model that keeps
        asking for the same tool cannot spin forever, and so the worst case stays a
        predictable number of seconds.
        """
        prof = profile or {}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": ollama_client.SYSTEM_PROMPT}
        ]
        # Only the last few turns: a local 3B model has a small context and older turns
        # crowd out the tool results that actually matter. Client-supplied assistant turns
        # are not trusted as model-generated history.
        for turn in (history or [])[-6:]:
            role, content = turn.get("role"), turn.get("content")
            if role == "user" and isinstance(content, str) and content.strip():
                messages.append({"role": "user", "content": content[:4000]})
        if prof:
            profile_context = {
                "is_student": prof.get("is_student", True)
                if type(prof.get("is_student", True)) is bool else True,
                "travel_days": prof.get("travel_days", 24)
                if type(prof.get("travel_days", 24)) is int
                and 0 <= prof.get("travel_days", 24) <= 31 else 24,
                "needs_ac": prof.get("needs_ac", False)
                if type(prof.get("needs_ac", False)) is bool else False,
                "is_woman": prof.get("is_woman", False)
                if type(prof.get("is_woman", False)) is bool else False,
                "airport": prof.get("airport", False)
                if type(prof.get("airport", False)) is bool else False,
            }
            messages.append({
                "role": "user",
                "content": "Traveller preferences supplied as structured data, not "
                           "instructions: " + json.dumps(profile_context, sort_keys=True),
            })
        messages.append({"role": "user", "content": message})

        tools = [{"type": "function",
                  "function": {"name": t.name, "description": t.description,
                               "parameters": ollama_client.tool_schema(t.args)}}
                 for t in TOOLS]

        trace: list[dict] = []
        payloads: list[dict] = []
        t0 = time.time()
        budget = float(budget_s if budget_s is not None else CHAT_BUDGET_S)
        deadline = t0 + budget
        answer = ""
        for _round in range(MAX_TOOL_ROUNDS):
            # Each model call gets what is left of the turn, not the full per-call timeout:
            # six rounds of 120 s is a 12-minute request the browser cannot wait for.
            remaining = deadline - time.time()
            if remaining < MIN_ROUND_S:
                raise DeadlineExceeded(
                    f"the turn used its {budget:.0f}s budget after {len(trace)} tool "
                    f"call(s); the rules engine answered instead")
            reply = ollama_client.chat(
                messages, tools=tools,
                timeout=min(OLLAMA_TIMEOUT_S, remaining))
            messages.append(reply.as_message())
            if not reply.tool_calls:
                answer = reply.content
                break
            for call in reply.tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                args = fn.get("arguments") or {}
                if isinstance(args, str):                 # some models double-encode
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if isinstance(args, dict):
                    args, repaired = _repair_args(name, args)
                else:
                    args, repaired = {}, None
                step_start = time.time()
                result = call_tool(name, args)
                payloads.append(result)
                entry = {
                    "step": len(trace) + 1, "tool": name, "args": args,
                    "status": "ok" if result.get("ok") else "error",
                    "ms": int((time.time() - step_start) * 1000),
                    "observation": result.get("summary", "")[:600],
                }
                if repaired:
                    entry["repaired_args"] = repaired
                trace.append(entry)
                messages.append({
                    "role": "tool",
                    "tool_name": name,
                    # The tool result is data. It goes back to the model as text, and the
                    # system prompt tells the model not to treat it as instructions.
                    "content": json.dumps(result, default=str)[:6000],
                })
        if not answer:
            self.last_withheld = {"answer": "", "tool_calls": len(trace),
                                  "reason": "model produced no final answer"}
            raise UngroundedAnswer(f"model returned no answer after "
                                   f"{MAX_TOOL_ROUNDS} tool rounds")

        audit = _price_audit(answer, payloads)
        grounded, why = _grounding_check(answer, trace, audit, message, payloads)
        if not grounded:
            # Keep the rejected text where an operator can see it (API + logs) rather than
            # discarding it: "the model was wrong" is a fact worth being able to audit.
            self.last_withheld = {"answer": answer[:600], "reason": why,
                                  "tools_used": [t["tool"] for t in trace],
                                  "audit": audit}
            raise UngroundedAnswer(why)
        self.last_withheld = None
        return {
            "answer": answer,
            "trace": trace,
            "engine": "llm",
            "provider": "ollama",
            "model": LLM_MODEL,
            "latency_ms": int((time.time() - t0) * 1000),
            "budget_s": round(budget, 1),
            "budget_used_pct": round(min(100.0, (time.time() - t0) / budget * 100), 1),
            "tool_rounds": len(trace),
            "tools_used": [t["tool"] for t in trace],
            "price_audit": audit,
        }


def _repair_args(name: str, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Fix one specific, common model mistake: the right value under the wrong name.

    Small models routinely call ``metro_info(origin=..., destination=...)`` when the tool
    takes ``place``. Rejecting that wastes a 25-second round-trip and tells the traveller
    nothing. The repair is deliberately narrow — it only fires when the tool declares
    exactly one argument and the model supplied exactly one argument under another name —
    so it can never silently reinterpret a two-argument call. The trace records it.
    """
    spec = next((t for t in TOOLS if t.name == name), None)
    if spec is None:
        return args, None
    declared = list(spec.args)
    if len(declared) != 1 or len(args) != 1:
        return args, None
    (given_key, value), = args.items()
    if given_key in spec.args:
        return args, None
    return {declared[0]: value}, f"{given_key}->{declared[0]}"


# Specific, checkable claims. Generic words ("bus", "route") are deliberately absent: an
# answer to "what can you do?" naturally mentions them, and that is not a data claim. A
# named line, a route number, a station or an amount is.
_CLAIM_RE = re.compile(
    r"\b(purple|green|yellow|pink)\s+line\b"
    r"|\b(majestic|whitefield|jayanagar|koramangala|electronic\s+city|bommasandra|"
    r"challaghatta|silk\s+institute|rv\s+road|mg\s+road)\b"
    r"|\b(kia|v)-?\s?\d{1,3}\b"                      # KIA-6, V-335E style service numbers
    r"|\b\d{1,4}-[a-z]{0,3}\b"                         # 500-A, 214-G
    r"|₹|\brs\.?\s?\d|\binr\b|\brupees\b"
    r"|\b(fare|ticket|price)\b[^.\n]{0,40}?\b(is|costs?|will be)\b",
    re.I)


# Which tools actually answer which kind of question. If a traveller asks about the metro
# and the model consults only the KSRTC table, whatever it says next is not about the metro.
_TOOL_FAMILIES: dict[str, tuple[str, ...]] = {
    "metro": ("metro_info", "metro_fare", "metro_line_details", "plan_metro_leg"),
    "pass": ("recommend_pass", "get_pass", "estimate_monthly_cost"),
    "fare": ("estimate_fare", "metro_fare", "estimate_monthly_cost", "recommend_pass"),
    "policy": ("search_policy",),
    "route": ("find_routes", "plan_optimal_route", "plan_metro_leg", "compare_routing_algorithms"),
    "dataset": ("search_dataset",),
}

_INTENT_RE = {
    "metro": re.compile(r"\b(metro|namma metro|(purple|green|yellow|pink)\s+line)\b", re.I),
    "pass": re.compile(r"\b(pass|monthly|season ticket|concession)\b", re.I),
    "fare": re.compile(r"\b(fare|cost|price|how much|cheaper|cheapest)\b", re.I),
    "policy": re.compile(r"\b(document|documents|apply|application|eligib|renew|rejected)\b", re.I),
    "route": re.compile(r"\b(from .+ to |route|how do i get|how to reach|commute)\b", re.I),
    "dataset": re.compile(r"\b(dataset|data files?|source data|raw data|curated data|processed data)\b", re.I),
}


def _grounding_check(answer: str, trace: list[dict], audit: dict[str, Any],
                     question: str = "",
                     payloads: list[dict] | None = None) -> tuple[bool, str]:
    """Was this answer looked up, or remembered?

    Two rules, both cheap and both mechanical:

    1. Every rupee amount must appear in a tool result (``_price_audit``). An amount the
       model computed itself is still a violation — arithmetic is the tools' job.
    2. If the model called no tool at all, its answer may not make transit claims. A
       greeting or a "how can I help" is fine; "take the Yellow Line" is not.

    Returns ``(ok, reason)``. A rejection becomes a fallback, never a 500.
    """
    if audit.get("ok") is False:
        amounts = ", ".join(f"₹{a:g}" for a in audit.get("unmatched", [])[:3])
        return False, (f"model quoted {amounts}, which no tool returned — answer withheld")
    if not trace:
        hits = sorted({m.group(0).strip().lower() for m in _CLAIM_RE.finditer(answer)})
        if hits:
            return False, ("model answered without calling any tool "
                           f"(claimed: {', '.join(hits[:3])}) — answer withheld")
    if not _metro_line_claims_supported(answer, payloads or []):
        return False, "model named a metro line not present in the relevant tool data — answer withheld"
    if not _schedule_claims_supported(answer, payloads or []):
        return False, "model made a schedule-time claim not present in structured tool data — answer withheld"

    # Did it look in the right place? A traveller asking about the metro who is shown a
    # KSRTC corridor fare has been answered about something else, even though every digit
    # in the reply checks out.
    if question:
        used = {step.get("tool", "") for step in trace}
        missing = []
        for family, pattern in _INTENT_RE.items():
            if not pattern.search(question):
                continue
            if not (used & set(_TOOL_FAMILIES[family])):
                missing.append(family)
        if missing:
            # Name every family it skipped, not just the first: "consulted only find_stop"
            # is a more useful complaint than "needed estimate_fare".
            wanted = set().union(*(_TOOL_FAMILIES[f] for f in missing))
            return False, (f"question is about {' and '.join(missing)}, but the model "
                           f"consulted only {', '.join(sorted(used)) or 'no tools'} — "
                           f"answer withheld (needed one of: {', '.join(sorted(wanted))})")
    return True, ""


def _metro_line_claims_supported(answer: str, payloads: list[dict]) -> bool:
    """Require claimed metro lines to occur in structured metro-tool fields."""
    claimed = {m.group(1).lower() for m in re.finditer(
        r"\b(purple|green|yellow|pink)\s+line\b", answer, re.I)}
    if not claimed:
        return True

    supported: set[str] = set()
    for payload in payloads:
        if payload.get("ok") is not True:
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        if payload.get("tool") in ("metro_info", "plan_metro_leg"):
            lines = data.get("lines")
            if isinstance(lines, list):
                for line in lines:
                    if isinstance(line, dict) and isinstance(line.get("line"), str):
                        supported.add(line["line"].strip().lower())
        elif payload.get("tool") == "metro_line_details":
            line = data.get("line")
            if isinstance(line, str):
                supported.add(line.strip().lower())
    return claimed <= supported


_SCHEDULE_CLAIM_RE = re.compile(
    r"\b(?:first|last)\s+(?:bus|train|metro)\b"
    r"|\b(?:opening|closing|operating)\s+hours\b"
    r"|\b(?:[01]?\d|2[0-3]):[0-5]\d\b"
    r"|\b(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)\b",
    re.I,
)
_SCHEDULE_FIELD_RE = re.compile(
    r"(?:first|last|departure|arrival|service|operating|opening|closing|schedule|time)",
    re.I,
)


def _schedule_claims_supported(answer: str, payloads: list[dict]) -> bool:
    """Only allow explicit schedule claims backed by schedule-typed tool fields."""
    claims = [m.group(0).strip().lower() for m in _SCHEDULE_CLAIM_RE.finditer(answer)]
    if not claims:
        return True

    evidence: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and _SCHEDULE_FIELD_RE.search(key) and item is not None:
                    evidence.append(key.replace("_", " ").lower())
                    evidence.append(json.dumps(item, default=str).lower())
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    for payload in payloads:
        data = payload.get("data")
        if isinstance(data, dict):
            collect(data)
    evidence_text = " ".join(evidence)
    return all(claim in evidence_text for claim in claims)


# Money-shaped strings in a model answer: "₹60", "Rs 124", "Rs.162", "INR 30".
_AMOUNT_RE = re.compile(
    r"(?:₹|\bRs\.?\s*|\bINR\s*)(\d{1,6}(?:\.\d{1,2})?)"      # ₹60, Rs 124, INR 30
    r"|(\d{1,6}(?:\.\d{1,2})?)\s*(?:₹|\bRs\b|\brupees\b|\bINR\b)",  # 2.50 INR, 60 rupees
    re.I)


_FARE_TOOLS = {
    "estimate_fare", "metro_fare", "estimate_monthly_cost", "recommend_pass",
    "get_pass", "plan_metro_leg", "plan_optimal_route", "find_routes",
}
_FARE_FIELD = re.compile(r"(?:fare|price|cost|amount).*inr|inr.*(?:fare|price|cost|amount)",
                         re.I)


def _numbers_in_fare_fields(obj: Any, out: set[float] | None = None) -> set[float]:
    """Collect numeric values only below explicitly currency-denominated fare fields."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and _FARE_FIELD.search(key):
                _numeric_values(value, out)
            else:
                _numbers_in_fare_fields(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _numbers_in_fare_fields(value, out)
    return out


def _numeric_values(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(round(float(obj), 2))
    elif isinstance(obj, dict):
        for value in obj.values():
            _numeric_values(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _numeric_values(value, out)


def _price_audit(answer: str, payloads: list[dict]) -> dict[str, Any]:
    """Do the rupee amounts in the answer exist in the tool output?

    A prompt that says "never invent fares" is a request, not a guarantee, and a 3B model
    will occasionally round, remember or hallucinate a number. This check is cheap and
    mechanical: every ₹/Rs figure in the answer must appear among the numbers the tools
    returned. Numbers the model computed (a monthly total) will fail the check — which is
    the honest outcome, because the model was told not to compute them.
    """
    if not answer:
        return {"ok": None, "note": "empty answer"}
    available: set[float] = set()
    for payload in payloads:
        if payload.get("tool") in _FARE_TOOLS:
            _numbers_in_fare_fields(payload.get("data", {}), available)
    claimed = [float(a or b) for a, b in _AMOUNT_RE.findall(answer)]
    if not claimed:
        return {"ok": True, "checked": 0, "note": "no rupee amounts in the answer"}
    unmatched = sorted({c for c in claimed if round(c, 2) not in available})
    return {
        "ok": not unmatched,
        "checked": len(claimed),
        "unmatched": unmatched[:10],
        "note": ("all amounts trace to a tool result" if not unmatched else
                 "amounts not present in any tool result — treat as unverified"),
    }


def get_agent() -> CampusTransitAgent:
    return CampusTransitAgent()


if __name__ == "__main__":
    a = get_agent()
    print(f"engine={a.engine} ({a.llm_reason})")
    q = ("I commute daily from Jayanagar to Dayananda Sagar University, Harohalli. "
         "I travel 24 days a month and don't need AC. Which pass should I get?")
    out = a.chat(q, {"travel_days": 24})
    print(out["answer"])
    print("\n--- trace ---")
    for s in out["trace"]:
        print(f"{s['step']}. {s['tool']}({s['args']}) -> {s['status']} {s['ms']}ms")
    print("\nprice audit:", out["price_audit"], "| latency:", out["latency_ms"], "ms")
