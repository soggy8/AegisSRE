"""
Tests for dashboard webhook intake and signal endpoints.
=========================================================
Uses FastAPI's TestClient so no Temporal server is required for parsing tests.

Run with:
    pytest tests/test_dashboard.py -v
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient

from sre_swarm.dashboard.app import _parse_pagerduty, _parse_alertmanager, app


# ---------------------------------------------------------------------------
# _parse_pagerduty
# ---------------------------------------------------------------------------

class TestParsePagerDuty:
    def test_v3_messages_format(self) -> None:
        payload = {
            "messages": [
                {
                    "event": "incident.trigger",
                    "incident": {
                        "title": "Payment service 500 spike",
                        "service": {"summary": "payment-service"},
                    },
                }
            ]
        }
        result = _parse_pagerduty(payload)
        assert result["affected_service"] == "payment-service"
        assert result["alert_summary"] == "Payment service 500 spike"
        assert result["source"] == "pagerduty"

    def test_single_top_level_event(self) -> None:
        payload = {
            "event": "incident.trigger",
            "incident": {
                "title": "DB connection pool exhausted",
                "service": {"name": "order-service"},
            },
        }
        result = _parse_pagerduty(payload)
        assert result["affected_service"] == "order-service"
        assert result["alert_summary"] == "DB connection pool exhausted"

    def test_non_trigger_event_returns_empty(self) -> None:
        payload = {
            "messages": [
                {"event": "incident.resolve", "incident": {"title": "resolved"}}
            ]
        }
        result = _parse_pagerduty(payload)
        assert result == {}

    def test_empty_messages_returns_empty(self) -> None:
        result = _parse_pagerduty({"messages": []})
        assert result == {}

    def test_triggered_event_variant(self) -> None:
        payload = {
            "messages": [
                {
                    "event": "incident.triggered",
                    "incident": {
                        "title": "Latency spike",
                        "service": {"summary": "api-gateway"},
                    },
                }
            ]
        }
        result = _parse_pagerduty(payload)
        assert result["affected_service"] == "api-gateway"

    def test_missing_service_falls_back_to_unknown(self) -> None:
        payload = {
            "messages": [
                {
                    "event": "incident.trigger",
                    "incident": {"title": "Something broke", "service": {}},
                }
            ]
        }
        result = _parse_pagerduty(payload)
        assert result["affected_service"] == "unknown-service"


# ---------------------------------------------------------------------------
# _parse_alertmanager
# ---------------------------------------------------------------------------

class TestParseAlertmanager:
    def test_basic_alert(self) -> None:
        payload = {
            "alerts": [
                {
                    "labels": {"service": "checkout-service", "alertname": "HighLatency"},
                    "annotations": {"summary": "p99 latency > 2s on /checkout"},
                }
            ]
        }
        result = _parse_alertmanager(payload)
        assert result["affected_service"] == "checkout-service"
        assert result["alert_summary"] == "p99 latency > 2s on /checkout"
        assert result["source"] == "alertmanager"

    def test_falls_back_to_job_label(self) -> None:
        payload = {
            "alerts": [
                {
                    "labels": {"job": "payment-worker", "alertname": "WorkerDown"},
                    "annotations": {"description": "worker has 0 healthy pods"},
                }
            ]
        }
        result = _parse_alertmanager(payload)
        assert result["affected_service"] == "payment-worker"
        assert result["alert_summary"] == "worker has 0 healthy pods"

    def test_falls_back_to_alertname_when_no_annotations(self) -> None:
        payload = {
            "alerts": [
                {
                    "labels": {"job": "auth-service", "alertname": "InstanceDown"},
                    "annotations": {},
                }
            ]
        }
        result = _parse_alertmanager(payload)
        assert result["alert_summary"] == "InstanceDown"

    def test_empty_alerts_returns_empty(self) -> None:
        result = _parse_alertmanager({"alerts": []})
        assert result == {}

    def test_unknown_service_when_no_labels(self) -> None:
        payload = {
            "alerts": [
                {
                    "labels": {"alertname": "Watchdog"},
                    "annotations": {"summary": "heartbeat"},
                }
            ]
        }
        result = _parse_alertmanager(payload)
        assert result["affected_service"] == "unknown-service"

    def test_uses_first_alert_only(self) -> None:
        payload = {
            "alerts": [
                {
                    "labels": {"service": "first-service"},
                    "annotations": {"summary": "first"},
                },
                {
                    "labels": {"service": "second-service"},
                    "annotations": {"summary": "second"},
                },
            ]
        }
        result = _parse_alertmanager(payload)
        assert result["affected_service"] == "first-service"
        assert result["alert_summary"] == "first"


def test_dashboard_page_surfaces_verification_fields() -> None:
    """The live page template renders confidence, step results, and verification."""
    client = TestClient(app)
    html = client.get("/dashboard").text
    assert html.startswith("<!DOCTYPE html>")
    assert "compensation_results" in html
    assert "auto_approved" in html
    assert "verification" in html
