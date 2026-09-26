"""
Tests for post-rollback service verification.
"""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from sre_swarm.activities.verify import ResourceCheck, VerificationRequest, verify_service_state


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, responder) -> None:
        self._responder = responder

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def get(self, url: str, timeout: float = 10.0) -> _Response:
        return self._responder(url)


@pytest.mark.asyncio
async def test_open_order_is_unhealthy(monkeypatch) -> None:
    def responder(url: str) -> _Response:
        assert url.endswith("/orders/ord-1")
        return _Response(200, {"status": "payment_failed", "order_id": "ord-1"})

    monkeypatch.setattr(
        "sre_swarm.activities.verify.httpx.AsyncClient",
        lambda: _Client(responder),
    )
    result = await ActivityEnvironment().run(
        verify_service_state,
        VerificationRequest(checks=[
            ResourceCheck("order", "ord-1", "cancelled"),
        ]),
    )
    assert result["healthy"] is False
    assert "payment_failed" in result["findings"][0]
    assert "expected cancelled" in result["findings"][0]


@pytest.mark.asyncio
async def test_cancelled_order_and_refunded_payment_are_healthy(monkeypatch) -> None:
    def responder(url: str) -> _Response:
        if "/orders/" in url:
            return _Response(200, {"status": "cancelled"})
        return _Response(200, {"status": "refunded"})

    monkeypatch.setattr(
        "sre_swarm.activities.verify.httpx.AsyncClient",
        lambda: _Client(responder),
    )
    result = await ActivityEnvironment().run(
        verify_service_state,
        VerificationRequest(checks=[
            ResourceCheck("order", "ord-9", "cancelled"),
            ResourceCheck("payment", "pay-9", "refunded"),
        ]),
    )
    assert result["healthy"] is True
    assert any("ord-9 is cancelled" in line for line in result["findings"])
    assert any("pay-9 is refunded" in line for line in result["findings"])


@pytest.mark.asyncio
async def test_missing_resource_is_treated_as_cleared(monkeypatch) -> None:
    monkeypatch.setattr(
        "sre_swarm.activities.verify.httpx.AsyncClient",
        lambda: _Client(lambda url: _Response(404, {})),
    )
    result = await ActivityEnvironment().run(
        verify_service_state,
        VerificationRequest(checks=[
            ResourceCheck("payment", "pay-gone", "refunded"),
        ]),
    )
    assert result["healthy"] is True
    assert "already cleared" in result["findings"][0]
