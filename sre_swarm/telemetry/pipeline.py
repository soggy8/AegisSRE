"""
Telemetry Pipeline
==================
Mock eBPF + OpenTelemetry ingestion pipeline.

Simulates network flow capture from microservices and returns OTLP-shaped
span dicts. In a real deployment this would read from an eBPF ring buffer
and emit to an OTLP collector. Here it generates realistic mock data so the
rest of the system (MCP server, root-cause analysis) has something to work
with without requiring any infrastructure.

Public interface:
    get_spans(trace_ids, service) -> list[dict]
    CPU_OVERHEAD_PCT               -> float  (~2.4 % eBPF simulation overhead)

Span shape (matches CONTRIBUTING.md §Area B spec exactly):
    {
        "trace_id":    str,
        "span_id":     str,
        "service":     str,
        "status_code": int,
        "latency_ms":  int,
        "error":       bool,
        "timestamp":   str   # ISO-8601 UTC
    }
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Simulated CPU overhead from the eBPF capture layer (matches stub in mcp_tools.py)
CPU_OVERHEAD_PCT: float = 2.4

# Where fixture files live relative to this module
_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Known fixture scenarios keyed by a substring that may appear in a trace_id
_SCENARIO_MAP: dict[str, str] = {
    "happy":   "happy_path.json",
    "timeout": "payment_timeout.json",
    "cascade": "cascade_failure.json",
}

# Services that may appear in dynamically generated spans
_KNOWN_SERVICES = [
    "api-gateway",
    "order-service",
    "payment-service",
    "inventory-service",
]

# Error scenarios injected during dynamic generation
_ERROR_SCENARIOS = [
    {"status_code": 500, "latency_ms_range": (4000, 6000), "label": "internal_error"},
    {"status_code": 504, "latency_ms_range": (4500, 7000), "label": "timeout"},
    {"status_code": 503, "latency_ms_range": (2500, 5000), "label": "service_unavailable"},
    {"status_code": 200, "latency_ms_range": (10,   200),  "label": "connection_reset"},
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_spans(trace_ids: list[str], service: str) -> list[dict[str, Any]]:
    """
    Return OTLP-shaped spans for the given trace IDs and target service.

    Strategy:
      1. If any trace_id contains a known scenario keyword (happy / timeout /
         cascade), load the matching fixture file and filter to spans that
         match the requested trace IDs or service.
      2. Otherwise, generate synthetic spans dynamically — one per trace_id,
         with realistic error injection weighted toward the requested service.

    Args:
        trace_ids: List of trace IDs to fetch spans for.
        service:   The primary service under investigation.

    Returns:
        List of span dicts. May be empty if no matching data is found.
    """
    if not trace_ids:
        logger.warning("get_spans called with empty trace_ids list")
        return []

    # --- Try fixture match first -------------------------------------------
    fixture_spans = _try_load_fixture(trace_ids, service)
    if fixture_spans is not None:
        logger.info(
            "Returning %d fixture spans for service=%s traces=%s",
            len(fixture_spans), service, trace_ids,
        )
        return fixture_spans

    # --- Fall back to dynamic generation -----------------------------------
    spans = _generate_spans(trace_ids, service)
    logger.info(
        "Generated %d dynamic spans for service=%s traces=%s",
        len(spans), service, trace_ids,
    )
    return spans


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

def _try_load_fixture(
    trace_ids: list[str], service: str
) -> list[dict[str, Any]] | None:
    """
    Load a fixture file if any trace_id contains a scenario keyword.
    Returns None if no fixture matches (caller should fall back to dynamic gen).
    """
    combined = " ".join(trace_ids).lower()
    for keyword, filename in _SCENARIO_MAP.items():
        if keyword in combined:
            fixture_path = _FIXTURES_DIR / filename
            if not fixture_path.exists():
                logger.warning("Fixture file not found: %s", fixture_path)
                return None
            with fixture_path.open() as f:
                all_spans: list[dict] = json.load(f)
            # Return spans for the requested trace IDs, or all if none match
            matching = [s for s in all_spans if s["trace_id"] in trace_ids]
            return matching if matching else all_spans

    return None


# ---------------------------------------------------------------------------
# Dynamic span generation
# ---------------------------------------------------------------------------

def _generate_spans(
    trace_ids: list[str], service: str
) -> list[dict[str, Any]]:
    """
    Synthesise realistic spans for the given trace IDs.

    For each trace_id we produce one span per known service.  The target
    service is always injected with an error scenario to simulate a real
    incident; other services have a 20% chance of a secondary error
    (simulating cascade effects).
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    spans: list[dict[str, Any]] = []

    for trace_id in trace_ids:
        for svc in _KNOWN_SERVICES:
            if svc == service:
                # Primary failure — always inject an error scenario
                scenario = random.choice(_ERROR_SCENARIOS[:3])  # pick a real error
                status_code = scenario["status_code"]
                lo, hi = scenario["latency_ms_range"]
                latency_ms = random.randint(lo, hi)
                error = True
            elif random.random() < 0.20:
                # Secondary cascade effect on other services
                scenario = random.choice(_ERROR_SCENARIOS)
                status_code = scenario["status_code"]
                lo, hi = scenario["latency_ms_range"]
                latency_ms = random.randint(lo, hi)
                error = status_code >= 400
            else:
                # Healthy span
                status_code = 200
                latency_ms = random.randint(10, 250)
                error = False

            spans.append({
                "trace_id":    trace_id,
                "span_id":     _short_id(),
                "service":     svc,
                "status_code": status_code,
                "latency_ms":  latency_ms,
                "error":       error,
                "timestamp":   now,
            })

    return spans


def _short_id() -> str:
    """Return a short hex string suitable for a span_id."""
    return uuid.uuid4().hex[:12]
