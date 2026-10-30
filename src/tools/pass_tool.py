"""Deterministic pass recommendation engine over data/curated/passes.json.

The engine encodes the rules that actually decide a Bengaluru student's pass choice:
Shakti (women, non-AC) first, then the 2026-27 free student pass (non-AC), then the
paid AC products. It returns every considered option with its price and verification
status so the answer can cite exactly where each number came from.
"""
from __future__ import annotations

import json
from typing import Any

from src.config import CURATED

PASSES: list[dict] = json.loads((CURATED / "passes.json").read_text(encoding="utf-8"))
BY_ID = {p["pass_id"]: p for p in PASSES}


def get_pass(pass_id: str) -> dict[str, Any]:
    """Return one curated pass card: price, validity, coverage, eligibility, sources.

    Every price the agent quotes for a *named* pass goes through here, so the amount
    in the answer is always traceable to a tool observation.
    """
    p = BY_ID.get(pass_id)
    if not p:
        return {"ok": False, "tool": "get_pass",
                "summary": f"No pass with id '{pass_id}'. Known ids: {', '.join(BY_ID)}."}
    price = p.get("price_inr")
    if price is None:
        price_txt = "price not published (UNVERIFIED_PLACEHOLDER)"
    elif price == 0:
        price_txt = "free (Rs 0)"
    else:
        price_txt = f"Rs {price:,.0f}"
    return {
        "ok": True, "tool": "get_pass",
        "summary": (f"{p['name']} ({p.get('operator','')}): {price_txt}; "
                    f"validity {p.get('validity_note') or p.get('validity_days')}; "
                    f"covers {', '.join(p.get('service_classes', [])) or 'n/a'}. "
                    f"Status {p.get('verification_status')}."),
        "data": p,
        "sources": [p.get("source_url", "")] if p.get("source_url") else [],
    }


def _option(pass_id: str, why: str, monthly_payg: float | None = None) -> dict[str, Any]:
    p = BY_ID[pass_id]
    price = p.get("price_inr")
    out = {
        "pass_id": p["pass_id"],
        "name": p["name"],
        "operator": p["operator"],
        "price_inr": price,
        "price_note": p.get("price_note"),
        "validity": p.get("validity_note") or p.get("validity_days"),
        "why": why,
        "verification_status": p.get("verification_status"),
        "last_verified": p.get("last_verified"),
        "source_url": p.get("source_url"),
    }
    if monthly_payg is not None and price is not None:
        out["monthly_saving_inr"] = round(monthly_payg - price, 2)
        out["worth_it"] = (monthly_payg - price) > 0
    return out


def recommend_pass(monthly_payg_cost_inr: float, needs_ac: bool = False,
                   is_woman_karnataka_resident: bool = False, is_student: bool = True,
                   crosses_bmtc_limits: bool = False, airport_travel: bool = False) -> dict[str, Any]:
    """Rank passes for this traveller. Deterministic - do not compute this yourself."""
    payg = float(monthly_payg_cost_inr)
    options: list[dict] = []

    # 1. Shakti dominates every paid non-AC product for Karnataka women.
    if is_woman_karnataka_resident and not needs_ac:
        options.append(_option(
            "shakti_scheme",
            "Free travel on non-AC BMTC and KSRTC services inside Karnataka. "
            "This makes every paid non-AC pass redundant.", payg))

    # 2. Free student pass (2026-27) for everyone else on non-AC services.
    if is_student and not needs_ac:
        options.append(_option(
            "karnataka_free_student_pass",
            "The state pays the pass fee for non-AC BMTC/KSRTC travel in 2026-27. "
            "This is the pass a DSU Harohalli commuter should apply for.", payg))

    # 3. Paid AC products.
    if needs_ac and is_student:
        options.append(_option(
            "bmtc_student_pass_ac_vajra",
            "The free student pass does not cover AC services, so an AC student pass is "
            "the only way to ride Vajra buses all month.", payg))
    if needs_ac and not is_student:
        options.append(_option("bmtc_monthly_vajra_ac",
                               "Unlimited Vajra AC travel; breaks even above roughly 50 rides.", payg))
    if airport_travel:
        options.append(_option("bmtc_vayu_vajra_gold",
                               "Only pass that covers Vayu Vajra airport services.", payg))

    # 4. Non-student baseline / long corridor cases.
    if not is_student and not needs_ac:
        options.append(_option("bmtc_monthly_ordinary",
                               "Standard non-AC monthly pass for a city commuter.", payg))
    if crosses_bmtc_limits:
        options.append(_option("ksrtc_sarige_monthly",
                               "Needed when the commute leaves the BMTC network. KSRTC prices "
                               "this corridor-by-corridor at the depot."))
        if is_student:
            options.append(_option(
                "ksrtc_student_pass",
                "Covers KSRTC ordinary services up to 60 km one-way on the declared "
                "corridor; free under the 2026-27 scheme.", payg))

    # The KSRTC student pass and the state free pass are the same free entitlement on
    # different networks; showing both as separate "options" just adds noise.
    if any(o["pass_id"] == "karnataka_free_student_pass" for o in options):
        options = [o for o in options if o["pass_id"] != "ksrtc_student_pass"]
    options.sort(key=lambda o: (o.get("price_inr") is None, o.get("price_inr") or 0))
    top = options[0] if options else None

    caveats = []
    if needs_ac:
        caveats.append("AC and Vayu Vajra services are excluded from both Shakti and the "
                       "free student pass.")
    if crosses_bmtc_limits:
        caveats.append("Beyond the BMTC network, a KSRTC pass or the free student pass must "
                       "cover the outer leg; KSRTC monthly pass prices are set at the depot.")
    if any(o["verification_status"] == "PRICE_UNVERIFIED" for o in options):
        caveats.append("At least one option has no verifiable published price; confirm it "
                       "at the depot before budgeting.")
    caveats.append("Pass rules changed in June 2026. Confirm the current rules on Seva Sindhu "
                   "before paying anything.")

    return {
        "ok": True,
        "tool": "recommend_pass",
        "summary": (f"Top recommendation: {top['name']} at "
                    f"{'no cost' if top['price_inr'] == 0 else 'Rs %.0f' % top['price_inr'] if top['price_inr'] is not None else 'an unverified price'}."
                    if top else "No pass matched this profile."),
        "data": {
            "monthly_payg_inr": round(payg, 2),
            "profile": {"needs_ac": needs_ac,
                        "is_woman_karnataka_resident": is_woman_karnataka_resident,
                        "is_student": is_student,
                        "crosses_bmtc_limits": crosses_bmtc_limits,
                        "airport_travel": airport_travel},
            "top_recommendation": top,
            "ranked_options": options,
            "estimated_monthly_saving_inr": top.get("monthly_saving_inr") if top else None,
            "caveats": caveats,
            "all_known_passes": [
                {"pass_id": p["pass_id"], "name": p["name"], "price_inr": p.get("price_inr"),
                 "verification_status": p.get("verification_status")} for p in PASSES],
        },
        "sources": ["data/curated/passes.json"],
    }
