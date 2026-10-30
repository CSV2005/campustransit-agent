"""Secret redaction.

Every string that leaves the process -- an API response, a log line, an error detail
stored in SQLite -- passes through here first. Two layers:

1. **Known values.** Whatever the process actually read into ``GOOGLE_MAPS_SERVER_API_KEY``,
   ``OPENROUTESERVICE_API_KEY``, ``TOMTOM_API_KEY`` is replaced with ``[redacted]``. This
   catches a key echoed back by a provider in an error message, which is exactly how keys
   leak into screenshots.
2. **Shape.** Values that look like the providers' key formats are redacted even when they
   are not ours (a key pasted into a query string in a bug report, say).

The function is cheap and idempotent, so call sites do not need to reason about ordering.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Names whose *values* must never appear in output. Browser keys are deliberately absent:
# ``GOOGLE_MAPS_BROWSER_KEY`` is referrer-restricted and shipped to the client by design.
SECRET_ENV = (
    "GOOGLE_MAPS_SERVER_API_KEY",
    "GOOGLE_MAPS_SERVER_KEY",
    "OPENROUTESERVICE_API_KEY",
    "TOMTOM_API_KEY",
    "GOOGLE_MAPS_API_KEY",          # legacy alias for the server key in this repo
    "GTFS_RT_AUTH_HEADER",          # carries a bearer token / API key for the feed
    "IUDX_CLIENT_SECRET",
    "IUDX_CLIENT_ID",
)

# Provider key shapes. Deliberately conservative: a false positive costs one redacted
# token, a false negative leaks a credential.
_PATTERNS = (
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),          # Google API key
    re.compile(r"\b5b3ce[0-9a-f]{20,}\b"),               # OpenRouteService
    re.compile(r"\b[A-Za-z0-9]{32}\b(?=.*tomtom)", re.I),  # TomTom, only near its name
)
_KEY_PARAM = re.compile(r"(?i)(key|api_key|apikey|token|subscription-key)=([^&\s\"']+)")
_PLACEHOLDER = "[redacted]"


def secret_values() -> list[str]:
    """Non-empty secret values currently in the environment, longest first."""
    vals = {(os.getenv(name) or "").strip() for name in SECRET_ENV}
    return sorted((v for v in vals if len(v) >= 8), key=len, reverse=True)


def redact(text: Any) -> Any:
    """Return ``text`` with secrets removed. Non-strings pass through untouched."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for value in secret_values():
        if value in out:
            out = out.replace(value, _PLACEHOLDER)
    for pat in _PATTERNS:
        out = pat.sub(_PLACEHOLDER, out)
    out = _KEY_PARAM.sub(lambda m: f"{m.group(1)}={_PLACEHOLDER}", out)
    return out


def redact_obj(obj: Any) -> Any:
    """Deep redaction for JSON-serialisable structures."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def looks_like_a_key(value: str) -> bool:
    """True when ``value`` matches a known provider key shape (used by validation)."""
    if not isinstance(value, str) or len(value) < 16:
        return False
    return any(p.search(value) for p in _PATTERNS[:2])
