"""Spend guard and demo rate limit tests."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from sre_swarm.dashboard.app import app
from sre_swarm.demo import spend_guard
from sre_swarm.demo.spend_guard import (
    clear_http_rate_limits_for_tests,
    reset_openai_counters_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_limits() -> None:
    clear_http_rate_limits_for_tests()
    reset_openai_counters_for_tests()


def test_demo_config_endpoint() -> None:
    client = TestClient(app)
    r = client.get("/demo/config")
    assert r.status_code == 200
    body = r.json()
    assert "passcode_required" in body
    assert "limits" in body


def test_http_rate_limit_blocks_third_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_TRIGGER_MAX_PER_IP_HOUR", "2")
    importlib.reload(spend_guard)

    spend_guard.check_http_rate_limit("k", max_calls=2, window_seconds=3600)
    spend_guard.check_http_rate_limit("k", max_calls=2, window_seconds=3600)
    with pytest.raises(ValueError, match="rate limit"):
        spend_guard.check_http_rate_limit("k", max_calls=2, window_seconds=3600)


def test_trigger_passcode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_TRIGGER_PASSCODE", "judge-secret")
    importlib.reload(spend_guard)

    spend_guard.verify_trigger_passcode("judge-secret")
    with pytest.raises(ValueError, match="passcode"):
        spend_guard.verify_trigger_passcode("wrong")


def test_rca_daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_DAILY_RCA_MAX", "2")
    importlib.reload(spend_guard)

    assert spend_guard.rca_openai_allowed()
    spend_guard.record_rca_openai_call()
    assert spend_guard.rca_openai_allowed()
    spend_guard.record_rca_openai_call()
    assert not spend_guard.rca_openai_allowed()
