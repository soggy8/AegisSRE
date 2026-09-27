"""Tests for post-incident narrative generation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sre_swarm.dashboard.app import app
from sre_swarm.dashboard.narrative import (
    build_incident_context,
    clear_narrative_cache,
    narrative_from_template,
    produce_narrative,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_narrative_cache()


_RESOLVED_STATUS = {
    "status": "resolved",
    "affected_service": "payment-service",
    "alert_summary": "HTTP 500 on checkout",
    "error_spans": [
        {"service": "api-gateway", "status_code": 504, "latency_ms": 5000},
        {"service": "payment-service", "status_code": 500, "latency_ms": 4700},
    ],
    "root_cause": "Payment processor timeout",
    "confidence": 0.92,
    "auto_approved": False,
    "rollback_approved": True,
    "compensation_results": [
        {"method": "POST", "endpoint": "/cancelOrder", "ok": True},
        {"method": "POST", "endpoint": "/refundPayment", "ok": True},
    ],
    "verification": {"healthy": True},
    "resolution_notes": "Resolved. Root cause: Payment processor timeout.",
    "compensations": [],
    "extra_context": [],
}


def test_template_narrative_includes_trace_and_verification() -> None:
    ctx = build_incident_context("incident-INC-1", _RESOLVED_STATUS)
    sections = narrative_from_template(ctx)
    assert "api-gateway" in sections["what_happened"]
    assert "payment-service" in sections["what_happened"]
    assert "Compensations" in sections["how_solved"]
    assert "Temporal" in sections["real_world"]


def test_produce_narrative_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sre_swarm.dashboard.narrative.OPENAI_API_KEY", "")
    first = produce_narrative("incident-INC-1", _RESOLVED_STATUS)
    second = produce_narrative("incident-INC-1", _RESOLVED_STATUS)
    assert first is second
    assert first["source"] == "template"


def test_produce_narrative_rejects_in_progress() -> None:
    with pytest.raises(ValueError, match="resolved or failed"):
        produce_narrative("incident-INC-2", {"status": "remediating"})


def test_narrative_endpoint_uses_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sre_swarm.dashboard.narrative.OPENAI_API_KEY", "")
    client = TestClient(app)
    with patch("sre_swarm.dashboard.app._query_workflow_status", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = _RESOLVED_STATUS
        r = client.post("/narrative/incident-INC-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "template"
    assert "what_happened" in body["sections"]


def test_dashboard_html_has_explain_action() -> None:
    client = TestClient(app)
    html = client.get("/dashboard").text
    assert "openNarrativeModal" in html
    assert "Incident explainer" in html
