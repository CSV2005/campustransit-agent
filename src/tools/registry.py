"""Single tool registry shared by the LLM agent and the deterministic planner.

Keeping one registry means the two controllers can never drift apart: the same
function, the same description and the same JSON schema are used whether the tool is
chosen by llama3.1 or by the rule-based planner.
"""
from __future__ import annotations

from typing import Any, Callable

from src.tools.fare_tool import estimate_fare, estimate_monthly_cost
from src.tools.ksrtc_tool import get_ksrtc_options
from src.tools.pass_tool import get_pass, recommend_pass
from src.tools.plan_tool import (compare_routing_algorithms, plan_optimal_route,
                                 plan_walk_connector)
from src.tools.rag_tool import search_dataset, search_policy
from src.tools.route_tool import (find_routes, find_stop, metro_fare, metro_info,
                                   metro_line_details, plan_metro_leg)


class ToolSpec:
    def __init__(self, name: str, fn: Callable, description: str, args: dict[str, str]):
        self.name = name
        self.fn = fn
        self.description = description
        self.args = args

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "args": self.args}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        "find_stop", find_stop,
        "Resolve a free-text place name (campus, locality, landmark, stop) to the nearest "
        "official BMTC stop and metro station. Use this before routing when a place name "
        "might be ambiguous.",
        {"place": "string"},
    ),
    ToolSpec(
        "find_routes", find_routes,
        "Find BMTC bus routes between two places. Returns direct one-seat rides when a real "
        "trip runs them, otherwise verified one-transfer itineraries. Handles vague names "
        "and snaps campuses to their nearest stop. Use this for any 'how do I get from A to B' "
        "question.",
        {"origin": "string", "destination": "string"},
    ),
    ToolSpec(
        "plan_metro_leg", plan_metro_leg,
        "Plan a Namma Metro leg between two places: nearest stations, line(s), interchange, "
        "stop count and the published metro fare. Use when the user mentions the metro or "
        "when a long trip could combine bus and metro.",
        {"origin": "string", "destination": "string"},
    ),
    ToolSpec(
        "metro_info", metro_info,
        "Describe the Namma Metro lines serving the station nearest a place, plus the "
        "interchange stations to change lines. Use for 'which line goes to X' questions.",
        {"place": "string"},
    ),
    ToolSpec(
        "metro_fare", metro_fare,
        "Fare between two named Namma Metro stations (works across lines, including the "
        "not-yet-open Pink Line). Use for 'metro fare from X to Y'.",
        {"from_station": "string", "to_station": "string"},
    ),
    ToolSpec(
        "metro_line_details", metro_line_details,
        "List every station on a metro line (green/purple/yellow/pink), its operating "
        "status, opening date if not open, and fares from the terminus.",
        {"line": "string"},
    ),
    ToolSpec(
        "estimate_fare", estimate_fare,
        "Compute the single-ride fare between two places from BMTC's GTFS fare stages, falling "
        "back to the published BMTC distance slab. Never estimate a fare by hand; call this.",
        {"origin": "string", "destination": "string", "route_id": "string|null", "mode": "bus|metro"},
    ),
    ToolSpec(
        "estimate_monthly_cost", estimate_monthly_cost,
        "Multiply a single fare into a monthly pay-as-you-go cost. Use BEFORE recommending any "
        "pass. Never do this arithmetic yourself.",
        {"one_way_fare_inr": "number", "legs_per_trip": "int", "trips_per_day": "int",
         "travel_days_per_month": "int"},
    ),
    ToolSpec(
        "get_pass", get_pass,
        "Return one curated pass card by pass_id (price, validity, coverage, eligibility, "
        "source). Use when the user names a specific pass.",
        {"pass_id": "string"},
    ),
    ToolSpec(
        "recommend_pass", recommend_pass,
        "Rank BMTC/KSRTC/Shakti passes for this traveller and compute savings and break-even "
        "against the monthly pay-as-you-go cost. Deterministic; do not reason about pass "
        "prices yourself.",
        {"monthly_payg_cost_inr": "number", "needs_ac": "bool",
         "is_woman_karnataka_resident": "bool", "is_student": "bool",
         "crosses_bmtc_limits": "bool", "airport_travel": "bool"},
    ),
    ToolSpec(
        "search_policy", search_policy,
        "Semantic search over curated policy text: eligibility, documents required, how to "
        "apply on Seva Sindhu, renewal, Shakti scheme rules, free student pass 2026-27, "
        "refunds, and FAQs. Use for every 'what do I need' or 'how do I apply' question.",
        {"query": "string", "doc_type": "any|pass|policy|route|campus", "k": "int"},
    ),
    ToolSpec(
        "search_dataset", search_dataset,
        "Search source files from the raw/curated data folders and all indexed transit "
        "database records. Use for questions about what is in the dataset, source files, "
        "GTFS records, places, or data provenance. This searches local indexes; it does "
        "not send the data to an external model service.",
        {"query": "string", "k": "int"},
    ),
    ToolSpec(
        "plan_optimal_route", plan_optimal_route,
        "Graph-search the whole network for the optimal journey between two places. "
        "Use when the user asks for the fastest, cheapest or fewest-changes option, or "
        "names a pathfinding algorithm. algorithm='dijkstra' is the exhaustive optimal "
        "reference, 'astar' is the same optimum with fewer expansions. Returns per-leg "
        "fares priced from the published BMRCL chart / BMTC fare stages.",
        {"origin": "string", "destination": "string",
         "objective": "fastest|cheapest|fewest_transfers", "algorithm": "astar|dijkstra"},
    ),
    ToolSpec(
        "compare_routing_algorithms", compare_routing_algorithms,
        "Run Dijkstra, A* and RRT on the same corridor and report whether the two optimal "
        "algorithms agree, how many graph nodes each expanded, and how far off optimal the "
        "RRT path landed. Use for questions about how routing works or algorithm "
        "comparisons.",
        {"origin": "string", "destination": "string",
         "objective": "fastest|cheapest|fewest_transfers"},
    ),
    ToolSpec(
        "plan_walk_connector", plan_walk_connector,
        "Plan a walking path between two points with RRT* in continuous space. Use for "
        "'how far is it on foot from X to Y' when no transit leg applies. Reports whether "
        "an obstacle layer was available.",
        {"origin": "string", "destination": "string"},
    ),
    ToolSpec(
        "get_ksrtc_options", get_ksrtc_options,
        "Look up KSRTC ordinary services and published fares on the Kanakapura Road / Harohalli "
        "corridor, including which fares are published versus estimated.",
        {"place": "string"},
    ),
]

BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def call_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch a tool call by name. Unknown names return a structured error, never raise."""
    spec = BY_NAME.get(name)
    if not spec:
        return {"ok": False, "tool": name, "summary": f"Unknown tool '{name}'.",
                "data": {"available": list(BY_NAME)}}
    try:
        return spec.fn(**(args or {}))
    except TypeError as exc:
        return {"ok": False, "tool": name, "summary": f"Bad arguments for {name}: {exc}",
                "data": {"expected": spec.args}}
    except Exception as exc:  # keep the agent alive
        return {"ok": False, "tool": name, "summary": f"{name} failed: {type(exc).__name__}: {exc}",
                "data": {}}


def langchain_tools():
    """Wrap the same functions as LangChain tools for the Ollama/LangGraph agent."""
    from langchain_core.tools import tool

    wrapped = []
    for spec in TOOLS:
        t = tool(spec.fn)
        t.name = spec.name
        t.description = spec.description
        wrapped.append(t)
    return wrapped


def tool_catalogue() -> list[dict[str, Any]]:
    return [t.as_dict() for t in TOOLS]
