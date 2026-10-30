"""Integration registry endpoints.

    GET  /api/v1/integrations                    every integration + its state
    GET  /api/v1/integrations/{id}               one integration
    POST /api/v1/integrations/{id}/test          run the safe test, or 409 if it costs money
    PUT  /api/v1/integrations/{id}/enabled       enable/disable without editing .env
    GET  /api/v1/config/health                   startup validation, names and shapes only

The test endpoint is deliberately awkward to call with money attached: a billable probe
returns **409 Conflict** with ``requires_confirmation: true`` and the price warning until
the caller sends ``{"confirm_billable": true}``. That makes an accidental probe a bug in a
client, not a charge on someone's card.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.services import config_check, integrations, road_routing
from src.services.integration_tests import ProbeRefused, run_test
from src.services.redact import redact_obj

router = APIRouter(prefix="/api/v1", tags=["integrations"])


class TestRequest(BaseModel):
    confirm_billable: bool = False


class EnabledRequest(BaseModel):
    enabled: bool


def _snapshot() -> dict:
    reg = integrations.registry()
    reg["road_routing"] = road_routing.describe()
    health = config_check.validate()
    reg["config"] = {k: health[k] for k in
                     ("ok", "app_env", "summary", "warnings", "errors")}
    # Belt and braces: nothing in here should contain a key, and if a provider error ever
    # echoed one this is where it would be caught.
    return redact_obj(reg)


@router.get("/integrations")
def list_integrations() -> dict:
    return _snapshot()


@router.get("/integrations/{integration_id}")
def get_integration(integration_id: str) -> dict:
    st = integrations.status(integration_id)
    if st is None:
        raise HTTPException(404, f"unknown integration '{integration_id}'")
    return redact_obj(st)


@router.post("/integrations/{integration_id}/test")
def test_integration(integration_id: str, body: TestRequest | None = None):
    confirm = bool((body.confirm_billable if body else False))
    if integration_id not in integrations.CATALOG:
        raise HTTPException(404, f"unknown integration '{integration_id}'")
    try:
        result = run_test(integration_id, confirm_billable=confirm)
    except ProbeRefused as exc:
        return JSONResponse(
            status_code=409,
            content=redact_obj({
                "error": "this test would make a billable request",
                "integration_id": exc.integration_id,
                "requires_confirmation": True,
                "warning": exc.warning,
                "how": ("repeat the request with {\"confirm_billable\": true} once you "
                        "accept that one provider request will be billed"),
            }))
    return JSONResponse(redact_obj(result))


@router.put("/integrations/{integration_id}/enabled")
def set_enabled(integration_id: str, body: EnabledRequest):
    if integration_id not in integrations.CATALOG:
        raise HTTPException(404, f"unknown integration '{integration_id}'")
    return redact_obj(integrations.set_enabled(integration_id, body.enabled))


@router.get("/config/health")
def config_health() -> dict:
    out = config_check.validate()
    return redact_obj(out)
