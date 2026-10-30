"""Pydantic models for the v1 API.

Request models are strict: an unknown ``preference`` is a 422, not a silent fallback.
Response models describe the contract but are deliberately permissive (``extra="allow"``)
because the planner carries provider-specific extras it is not worth mirroring here.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Preference = Literal["fastest", "cheapest", "fewest_transfers", "least_walking", "balanced"]
HhMm = re.compile(r"^\d{2}:\d{2}$")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1, max_length=120)
    destination: str = Field(min_length=1, max_length=120)
    preference: Preference = "balanced"
    arrival_by: str | None = None
    max_itineraries: int = Field(default=4, ge=1, le=8)

    @field_validator("origin", "destination")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("arrival_by")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if not HhMm.match(v):
            raise ValueError("arrival_by must be HH:MM")
        h, m = (int(x) for x in v.split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("arrival_by must be a valid 24-hour time")
        return v


class LegOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    mode: str
    label: str
    from_place: str | None = Field(default=None, alias="from")
    to_place: str | None = Field(default=None, alias="to")
    minutes: float
    km: float
    # None, not 0: a KSRTC leg has no published fare in this build.
    fare_inr: float | None = Field(default=None, ge=0, le=100000)
    fare_basis: str | None = None
    stops: int | None = None
    geometry: list[list[float]] = Field(default_factory=list)


class ItineraryOut(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    itinerary_id: str
    rank: int
    objective: str
    mode_sequence: list[str]
    # None when at least one leg has no published fare (see unpriced_legs).
    total_price_inr: float | None = Field(default=None, ge=0, le=100000)
    total_minutes: float
    minutes_in_vehicle: float
    walking_minutes: float
    transfers: int
    comfort_score: float | None = None
    ranked_best_for: list[str] = Field(default_factory=list)
    explanation: str = ""
    fare_breakdown: list[dict[str, Any]] = Field(default_factory=list)
    legs: list[LegOut] = Field(default_factory=list)
    geometry: list[list[float]] = Field(default_factory=list)


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    origin: dict[str, Any] | None = None
    destination: dict[str, Any] | None = None
    recommended: str | None = None
    preference: str | None = None
    itineraries: list[ItineraryOut] = Field(default_factory=list)
    best_of: dict[str, Any] = Field(default_factory=dict)
    departure_advice: dict[str, Any] = Field(default_factory=dict)
    fare_source: str = ""
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: float | None = None


class SuggestionOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    label: str
    kind: str
    lat: float | None = None
    lon: float | None = None
    stop_id: str | None = None
    source: str = "local"
    confidence: float | None = None
    routable: bool = True


class AutocompleteResponse(BaseModel):
    ok: bool
    query: str
    suggestions: list[SuggestionOut] = Field(default_factory=list)
    providers_used: list[str] = Field(default_factory=list)
    indexed_places: int = 0
    google_note: str = ""
    note: str = ""


class SaveRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1, max_length=120)
    destination: str = Field(min_length=1, max_length=120)
    route_id: str = Field(default="", max_length=120)
    title: str = Field(default="", max_length=160)
    itinerary: dict[str, Any] = Field(default_factory=dict)


class SavedRouteOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    origin: str
    destination: str
    route_id: str = ""
    title: str = ""
    created_at: str
    payload: dict[str, Any] | None = None


class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1, max_length=120)
    destination: str = Field(min_length=1, max_length=120)
    departure_time: str = Field(default="", max_length=40)
    selected_mode: str = Field(default="", max_length=60)
    estimated_fare_inr: float | None = Field(default=None, ge=0, le=100000)
    estimated_duration: int | None = Field(default=None, ge=0, le=10000)
    route_id: str = Field(default="", max_length=120)


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_preference: Preference | None = None
    default_origin: str | None = Field(default=None, max_length=120)
    map_provider: Literal["auto", "google", "osm", "svg"] | None = None
    theme: Literal["dark", "light"] | None = None
    units: Literal["metric", "imperial"] | None = None
    show_traffic_overlay: bool | None = None


class CapabilityOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    label: str
    available: bool
    reason: str = ""
    needs: str = ""
    docs: str = ""


class SourceOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_name: str
    data_type: str | None = None
    url: str | None = None
    last_checked: str | None = None
    last_success: str | None = None
    status: str = "unknown"
    detail: str | None = None
    licence: str | None = None
    rows: int | None = None
