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

import copy
import json
import logging
import random
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Simulated CPU overhead from the eBPF capture layer (matches stub in mcp_tools.py)
CPU_OVERHEAD_PCT: float = 2.4

# Where fixture files live relative to this module
_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Known fixture scenarios keyed by a substring that may appear in a trace_id
_SCENARIO_MAP: dict[str, str] = {
    "happy":     "happy_path.json",
    "timeout":   "payment_timeout.json",
    "cascade":   "cascade_failure.json",
    "inventory": "inventory_failure.json",
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
      3. If trace_ids contain "order_id:<id>" or "payment_id:<id>" entries,
         those real IDs are embedded into the first error span so the LLM
         can produce a correct compensation plan.

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
        spans = fixture_spans
        logger.info(
            "Returning %d fixture spans for service=%s traces=%s",
            len(spans), service, trace_ids,
        )
    else:
        # --- Fall back to dynamic generation --------------------------------
        spans = _generate_spans(trace_ids, service)
        logger.info(
            "Generated %d dynamic spans for service=%s traces=%s",
            len(spans), service, trace_ids,
        )

    # --- Annotate first error span with real IDs (if provided) -------------
    real_ids = _extract_ids(trace_ids)
    if real_ids:
        for span in spans:
            if span.get("error"):
                span.update(real_ids)
                logger.info("Annotated error span with real IDs: %s", real_ids)
                break

    return spans


def _primary_trace_id(trace_ids: list[str]) -> str:
    """First real trace id (excludes order_id:/payment_id: carriers)."""
    for trace_id in trace_ids:
        if trace_id.startswith("order_id:") or trace_id.startswith("payment_id:"):
            continue
        return trace_id
    return "trace-unknown"


def _fixture_seed(trace_ids: list[str]) -> int:
    material = "|".join(sorted(trace_ids))
    return hash(material) & 0xFFFFFFFF


def _extract_ids(trace_ids: list[str]) -> dict[str, str]:
    """
    Extract real order/payment IDs embedded as structured trace_id entries.

    The trigger script passes entries like "order_id:ord-abc123" alongside
    normal trace IDs so downstream components can surface the real IDs
    without a separate lookup.
    """
    ids: dict[str, str] = {}
    for t in trace_ids:
        if t.startswith("order_id:"):
            ids["order_id"] = t.split(":", 1)[1]
        elif t.startswith("payment_id:"):
            ids["payment_id"] = t.split(":", 1)[1]
    return ids


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
            return _personalize_fixture(
                all_spans,
                primary_trace_id=_primary_trace_id(trace_ids),
                affected_service=service,
                seed=_fixture_seed(trace_ids),
            )

    return None


def _personalize_fixture(
    all_spans: list[dict],
    primary_trace_id: str,
    affected_service: str,
    seed: int,
) -> list[dict]:
    """
    Turn static fixture JSON into a per-incident trace: pick a template trace,
    remap ids, jitter latencies/timestamps, and occasionally vary cascade shape.
    """
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for span in all_spans:
        by_trace[str(span["trace_id"])].append(span)

    rng = random.Random(seed)
    template_key = rng.choice(sorted(by_trace.keys()))
    template = by_trace[template_key]

    base_time = datetime.fromtimestamp(1_700_000_000 + (seed % 86_400), tz=timezone.utc)
    personalized: list[dict] = []

    for index, span in enumerate(template):
        row = copy.deepcopy(span)
        row["trace_id"] = primary_trace_id
        row["span_id"] = f"{seed % 0xFFFFFFFF:08x}{index:04x}"[:12]
        lo, hi = (3500, 7200) if row.get("error") else (15, 280)
        base_latency = int(row.get("latency_ms") or lo)
        jitter = rng.randint(-450, 450)
        row["latency_ms"] = max(lo, min(hi, base_latency + jitter))
        row["timestamp"] = (base_time + timedelta(milliseconds=index * 17 + rng.randint(0, 40))).isoformat().replace(
            "+00:00", "Z",
        )

        # Occasionally soften a secondary cascade error so rows don't look cloned.
        if (
            row.get("service") == "api-gateway"
            and row.get("error")
            and rng.random() < 0.22
        ):
            row["error"] = False
            row["status_code"] = 200
            row["latency_ms"] = rng.randint(180, 950)

        personalized.append(row)

    # Rarely inject an extra secondary error on a healthy sibling service.
    if rng.random() < 0.18:
        for row in personalized:
            if row.get("service") == "inventory-service" and not row.get("error"):
                row["error"] = True
                row["status_code"] = rng.choice([502, 503, 504])
                row["latency_ms"] = rng.randint(1200, 3800)
                break

    payment = next((s for s in personalized if s.get("service") == affected_service), None)
    if payment and payment.get("error"):
        payment["status_code"] = rng.choice([500, 502, 503, 504])

    return personalized


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
    real_traces = [
        t for t in trace_ids
        if not t.startswith("order_id:") and not t.startswith("payment_id:")
    ]
    if not real_traces:
        real_traces = [_primary_trace_id(trace_ids)]

    rng = random.Random(_fixture_seed(trace_ids))

    for trace_id in real_traces:
        for svc in _KNOWN_SERVICES:
            if svc == service:
                # Primary failure — always inject an error scenario
                scenario = rng.choice(_ERROR_SCENARIOS[:3])  # pick a real error
                status_code = scenario["status_code"]
                lo, hi = scenario["latency_ms_range"]
                latency_ms = rng.randint(lo, hi)
                error = True
            elif rng.random() < 0.20:
                # Secondary cascade effect on other services
                scenario = rng.choice(_ERROR_SCENARIOS)
                status_code = scenario["status_code"]
                lo, hi = scenario["latency_ms_range"]
                latency_ms = rng.randint(lo, hi)
                error = status_code >= 400
            else:
                # Healthy span
                status_code = 200
                latency_ms = rng.randint(10, 250)
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
