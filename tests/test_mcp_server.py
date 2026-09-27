"""
Tests for the MCP Server (sre_swarm/mcp/server.py)
====================================================
Uses FastAPI's TestClient so no network is needed.

Run with:
    pytest tests/test_mcp_server.py -v
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sre_swarm.mcp.server import app, MCP_API_KEY, MCP_PROTOCOL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _valid_headers() -> dict[str, str]:
    """Headers that satisfy every server check."""
    return {
        "Mcp-Protocol-Version": MCP_PROTOCOL,
        "Mcp-Method":           "tools/call",
        "Authorization":        f"Bearer {MCP_API_KEY}",
        "Content-Type":         "application/json",
    }


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_returns_ok(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["protocol_version"] == MCP_PROTOCOL


# ---------------------------------------------------------------------------
# Auth & header validation
# ---------------------------------------------------------------------------

class TestAuthAndHeaders:
    def test_missing_authorization_returns_401(self, client: TestClient) -> None:
        headers = _valid_headers()
        headers.pop("Authorization")
        response = client.post("/mcp", headers=headers, json={"tool": "get_telemetry_context", "arguments": {}})
        assert response.status_code == 401

    def test_wrong_token_returns_401(self, client: TestClient) -> None:
        headers = _valid_headers()
        headers["Authorization"] = "Bearer wrong-token"
        response = client.post("/mcp", headers=headers, json={"tool": "get_telemetry_context", "arguments": {}})
        assert response.status_code == 401

    def test_wrong_protocol_version_returns_400(self, client: TestClient) -> None:
        headers = _valid_headers()
        headers["Mcp-Protocol-Version"] = "2024-01-01"
        response = client.post(
            "/mcp", headers=headers,
            json={"tool": "get_telemetry_context", "arguments": {"trace_ids": ["t1"], "service": "s1"}},
        )
        assert response.status_code == 400

    def test_wrong_mcp_method_returns_400(self, client: TestClient) -> None:
        headers = _valid_headers()
        headers["Mcp-Method"] = "resources/read"
        response = client.post(
            "/mcp", headers=headers,
            json={"tool": "get_telemetry_context", "arguments": {"trace_ids": ["t1"], "service": "s1"}},
        )
        assert response.status_code == 400

    def test_unknown_tool_returns_404(self, client: TestClient) -> None:
        response = client.post(
            "/mcp", headers=_valid_headers(),
            json={"tool": "nonexistent_tool", "arguments": {}},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# get_telemetry_context
# ---------------------------------------------------------------------------

class TestGetTelemetryContext:
    def test_returns_spans_and_cpu_overhead(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            headers=_valid_headers(),
            json={
                "tool": "get_telemetry_context",
                "arguments": {
                    "trace_ids": ["trace-timeout-001"],
                    "service":   "payment-service",
                },
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert "spans" in body
        assert "cpu_overhead_pct" in body
        assert isinstance(body["spans"], list)
        assert len(body["spans"]) > 0
        assert isinstance(body["cpu_overhead_pct"], float)
        assert body["cpu_overhead_pct"] == pytest.approx(2.4)

    def test_span_shape(self, client: TestClient) -> None:
        """Every span must have the required OTLP fields."""
        response = client.post(
            "/mcp",
            headers=_valid_headers(),
            json={
                "tool": "get_telemetry_context",
                "arguments": {
                    "trace_ids": ["trace-timeout-001"],
                    "service":   "payment-service",
                },
            },
        )
        assert response.status_code == 200
        for span in response.json()["spans"]:
            assert "trace_id"    in span
            assert "service"     in span
            assert "status_code" in span
            assert "latency_ms"  in span
            assert "error"       in span

    def test_dynamic_span_generation(self, client: TestClient) -> None:
        """Trace IDs without scenario keywords trigger dynamic generation."""
        response = client.post(
            "/mcp",
            headers=_valid_headers(),
            json={
                "tool": "get_telemetry_context",
                "arguments": {
                    "trace_ids": ["random-trace-xyz"],
                    "service":   "order-service",
                },
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["spans"]) > 0

    def test_missing_trace_ids_returns_400(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            headers=_valid_headers(),
            json={"tool": "get_telemetry_context", "arguments": {"service": "payment-service"}},
        )
        assert response.status_code == 400

    def test_missing_service_returns_400(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            headers=_valid_headers(),
            json={"tool": "get_telemetry_context", "arguments": {"trace_ids": ["t1"]}},
        )
        assert response.status_code == 400

    def test_empty_trace_ids_returns_400(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            headers=_valid_headers(),
            json={
                "tool": "get_telemetry_context",
                "arguments": {"trace_ids": [], "service": "payment-service"},
            },
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# analyze_root_cause  (heuristic fallback — no LLM credentials needed)
# ---------------------------------------------------------------------------

class TestAnalyzeRootCause:
    """
    These tests exercise the deterministic heuristic fallback that runs when
    no LLM credentials are present. They do not make external network calls.
    """

    @pytest.fixture(autouse=True)
    def _force_heuristic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep tests off the real LLM even when a local .env has a key."""
        monkeypatch.setattr("sre_swarm.mcp.server.OPENAI_API_KEY", "")
        monkeypatch.setattr("sre_swarm.mcp.server.WATSONX_API_KEY", "")
        monkeypatch.setattr("sre_swarm.mcp.server.WATSONX_PROJECT_ID", "")

    _SAMPLE_TELEMETRY = {
        "spans": [
            {
                "trace_id":    "trace-001",
                "span_id":     "span-a",
                "service":     "payment-service",
                "status_code": 500,
                "latency_ms":  4700,
                "error":       True,
                "timestamp":   "2025-01-01T12:00:00Z",
            },
            {
                "trace_id":    "trace-001",
                "span_id":     "span-b",
                "service":     "order-service",
                "status_code": 200,
                "latency_ms":  80,
                "error":       False,
                "timestamp":   "2025-01-01T12:00:00Z",
            },
        ],
        "cpu_overhead_pct": 2.4,
    }

    def _call(self, client: TestClient, alert: str, telemetry: dict) -> dict:
        response = client.post(
            "/mcp",
            headers=_valid_headers(),
            json={
                "tool": "analyze_root_cause",
                "arguments": {
                    "alert_summary":     alert,
                    "telemetry_context": telemetry,
                },
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_returns_required_keys(self, client: TestClient) -> None:
        body = self._call(client, "Payment 500 spike", self._SAMPLE_TELEMETRY)
        assert "root_cause"    in body
        assert "confidence"    in body
        assert "compensations" in body

    def test_confidence_is_float_in_range(self, client: TestClient) -> None:
        body = self._call(client, "Payment 500 spike", self._SAMPLE_TELEMETRY)
        assert isinstance(body["confidence"], float)
        assert 0.0 <= body["confidence"] <= 1.0

    def test_compensations_have_required_fields(self, client: TestClient) -> None:
        body = self._call(client, "Payment 500 spike", self._SAMPLE_TELEMETRY)
        for comp in body["compensations"]:
            assert "endpoint" in comp
            assert "method"   in comp
            assert "payload"  in comp

    def test_no_error_spans_still_returns_valid_response(self, client: TestClient) -> None:
        telemetry = {
            "spans": [
                {
                    "trace_id": "t1", "span_id": "s1", "service": "api-gateway",
                    "status_code": 200, "latency_ms": 50, "error": False,
                    "timestamp": "2025-01-01T12:00:00Z",
                }
            ],
            "cpu_overhead_pct": 2.4,
        }
        body = self._call(client, "Spurious alert", telemetry)
        assert "root_cause" in body

    def test_demo_fixture_confidence_below_auto_approve(self, client: TestClient) -> None:
        """Trigger-incident telemetry should not always read as a flat 90%."""
        from sre_swarm.telemetry.pipeline import get_spans

        spans = get_spans(
            ["trace-timeout-001", "order_id:ord-demo", "payment_id:pay-demo"],
            "payment-service",
        )
        body = self._call(
            client,
            "HTTP 500 spike on /checkout",
            {"spans": spans, "cpu_overhead_pct": 2.4},
        )
        assert body["confidence"] == pytest.approx(0.93)
        assert body["confidence"] < 0.95

    def test_heuristic_builds_compensations_from_span_ids(self, client: TestClient) -> None:
        telemetry = {
            "spans": [
                {
                    "trace_id": "t1",
                    "span_id": "s1",
                    "service": "payment-service",
                    "status_code": 504,
                    "latency_ms": 4700,
                    "error": True,
                    "order_id": "ord-heur-1",
                    "payment_id": "pay-heur-9",
                    "timestamp": "2025-01-01T12:00:00Z",
                },
            ],
            "cpu_overhead_pct": 2.4,
        }
        body = self._call(client, "Checkout timeout", telemetry)
        assert len(body["compensations"]) == 2
        endpoints = {c["endpoint"] for c in body["compensations"]}
        assert "/cancelOrder" in endpoints
        assert "/refundPayment" in endpoints

    def test_extra_context_is_included_in_root_cause(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            headers=_valid_headers(),
            json={
                "tool": "analyze_root_cause",
                "arguments": {
                    "alert_summary": "Payment 500 spike",
                    "telemetry_context": self._SAMPLE_TELEMETRY,
                    "extra_context": ["is the DB involved?"],
                },
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "is the DB involved?" in body["root_cause"]
        assert "Follow-up considered" in body["root_cause"]

    def test_missing_alert_summary_returns_400(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            headers=_valid_headers(),
            json={
                "tool": "analyze_root_cause",
                "arguments": {"telemetry_context": self._SAMPLE_TELEMETRY},
            },
        )
        assert response.status_code == 400
