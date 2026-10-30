"""Render the map geometry to a standalone SVG — same projection as web/static/js/map.js.

This exists so the projection can be checked without a browser: if the maths in map.js is
wrong, this file is wrong in exactly the same way.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.tools.plan_tool import ranked_itineraries  # noqa: E402
from web.server import map_basemap  # noqa: E402

W, H = 900, 620
LINE_COLORS = {"PURPLE": "#7c3aed", "GREEN": "#059669", "YELLOW": "#ca8a04", "PINK": "#db2777"}
RANK_COLORS = ["#16a34a", "#d97706", "#dc2626"]


def render(origin: str, destination: str, out: Path) -> Path:
    base = map_basemap()
    data = ranked_itineraries(origin, destination)

    # fit to the route when there is one, exactly like the browser does
    route_pts = []
    for it in data.get("itineraries", []):
        for leg in it["legs"]:
            route_pts += leg["geometry"]
    if data.get("origin"):
        route_pts.append([data["origin"]["lat"], data["origin"]["lng"]])
    if data.get("destination"):
        route_pts.append([data["destination"]["lat"], data["destination"]["lng"]])
    pts = route_pts or [[s["lat"], s["lng"]] for s in base["bus_stops"]]

    lats = [p[0] for p in pts]; lngs = [p[1] for p in pts]
    lat0, lat1 = min(lats), max(lats)
    lng0, lng1 = min(lngs), max(lngs)
    clat, clng = (lat0 + lat1) / 2, (lng0 + lng1) / 2
    import math
    coslat = math.cos(math.radians(clat))

    def project(lat, lng):
        s = min(H / max(lat1 - lat0, 0.01) * 0.92, W / max((lng1 - lng0) * coslat, 0.01) * 0.92)
        return (W / 2 + (lng - clng) * s * coslat, H / 2 - (lat - clat) * s)

    out_parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                 f'viewBox="0 0 {W} {H}"><rect width="{W}" height="{H}" fill="#eef2ec"/>']
    for s in base["bus_stops"]:
        x, y = project(s["lat"], s["lng"])
        out_parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.1" fill="#cbd5c8"/>')
    for line in base["metro_lines"]:
        color = LINE_COLORS.get(line["id"], "#475569")
        path = " ".join(f"{project(s['lat'], s['lng'])[0]:.1f},{project(s['lat'], s['lng'])[1]:.1f}"
                        for s in line["stations"])
        op = 0.30 if line["status"] == "operational" else 0.14
        dash = "" if line["status"] == "operational" else ' stroke-dasharray="5 5"'
        out_parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" '
                         f'stroke-width="3" stroke-opacity="{op}"{dash}/>')
        for s in line["stations"]:
            x, y = project(s["lat"], s["lng"])
            out_parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="#fff" '
                             f'stroke="{color}" stroke-width="1.4" stroke-opacity="{op}"/>')
    for i, it in enumerate(data.get("itineraries", [])):
        color = RANK_COLORS[min(i, len(RANK_COLORS) - 1)]
        for leg in it["legs"]:
            if leg["mode"] == "walk" or len(leg["geometry"]) < 2:
                continue
            path = " ".join(f"{project(*p)[0]:.1f},{project(*p)[1]:.1f}" for p in leg["geometry"])
            out_parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" '
                             f'stroke-width="5" stroke-opacity="0.9" stroke-linecap="round"/>')
    for key, label, color in (("origin", "A", "#0b6e4f"), ("destination", "B", "#b91c1c")):
        if data.get(key):
            x, y = project(data[key]["lat"], data[key]["lng"])
            out_parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="{color}" '
                             f'stroke="#fff" stroke-width="2.5"/>')
            out_parts.append(f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" '
                             f'font-size="11" font-weight="700" fill="#fff" '
                             f'font-family="sans-serif">{label}</text>')
    out_parts.append(f'<text x="14" y="24" font-size="12" fill="#5b6b63" '
                     f'font-family="sans-serif">{origin} → {destination} · '
                     f'{len(data.get("itineraries", []))} itineraries · GTFS geometry</text>')
    out_parts.append("</svg>")
    out.write_text("\n".join(out_parts))
    return out


if __name__ == "__main__":
    o = sys.argv[1] if len(sys.argv) > 1 else "Jayanagar"
    d = sys.argv[2] if len(sys.argv) > 2 else "Majestic"
    p = render(o, d, ROOT / "data" / "processed" / "map_preview.svg")
    print("wrote", p)
