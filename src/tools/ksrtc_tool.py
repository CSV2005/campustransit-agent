"""D4: KSRTC corridor options, with the verification status of every fare made explicit."""
from __future__ import annotations

from typing import Any

from src.tools.common import db


def get_ksrtc_options(place: str = "") -> dict[str, Any]:
    """Look up KSRTC ordinary services for a place on the Kanakapura Road corridor.

    Returns published fares where they exist and flags derived or unpublished prices
    so the answer can say "this is an estimate" instead of inventing a number.
    """
    con = db()
    rows = [dict(r) for r in con.execute(
        "SELECT corridor_id, service_class, from_place, to_place, via, approx_km, fare_inr, "
        "fare_basis, source_url, publisher, published_date, last_verified, "
        "verification_status, notes FROM ksrtc_corridor")]
    con.close()

    q = (place or "").lower()
    if q:
        import re
        stop = {"the", "and", "for", "from", "karnataka", "bus", "fare", "bengaluru"}
        q_tokens = {t for t in re.findall(r"[a-z]{3,}", q) if t not in stop}
        def score(r):
            blob = f"{r['from_place']} {r['to_place']} {r['via']} {r['service_class']}".lower()
            return len(q_tokens & {t for t in re.findall(r"[a-z]{3,}", blob)})
        scored = sorted(((score(r), r) for r in rows), key=lambda x: -x[0])
        hits = [r for sc, r in scored if sc > 0]
        # keep one benchmark row (zero overlap) so the answer still has context
        rows = hits or [scored[0][1]] if scored else rows

    if not rows:
        return {"ok": False, "tool": "get_ksrtc_options",
                "summary": "No KSRTC corridor data for that place.", "data": {"options": []}}

    published = [r for r in rows if r["fare_basis"] == "published"]
    derived = [r for r in rows if r["fare_basis"] != "published"]

    parts = []
    for r in rows:
        cls = r["service_class"]
        if r["fare_inr"] is None:
            parts.append(f"{cls} {r['from_place']} -> {r['to_place']}: fare not published "
                         f"({r['verification_status']}), confirm at the depot.")
        else:
            tag = "published" if r["fare_basis"] == "published" else "ESTIMATE"
            parts.append(f"{cls} {r['from_place']} -> {r['to_place']}: Rs {r['fare_inr']:.0f} "
                         f"({tag}, {r['verification_status']}).")

    return {
        "ok": True,
        "tool": "get_ksrtc_options",
        "summary": " ".join(parts),
        "data": {
            "options": rows,
            "n_published": len(published),
            "n_derived": len(derived),
            "warning": ("Some fares here are interpolated, not published. Say so in the answer."
                        if derived else None),
        },
        "sources": sorted({r["source_url"] for r in rows if r.get("source_url")}),
    }
