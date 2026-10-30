"""Test-suite defaults.

The suite must be fast, offline and deterministic, so the LLM is switched off for it:
every endpoint then answers from the deterministic planner, which is also the behaviour a
machine without Ollama gets. The one test that needs a real model opts back in with
``CampusTransitAgent(force_engine="llm")`` and skips itself when no model is installed.

This file sets the environment *before* ``src.config`` is imported, because the config
module reads it at import time.
"""
from __future__ import annotations

import os

os.environ.setdefault("LLM_ENABLED", "off")
