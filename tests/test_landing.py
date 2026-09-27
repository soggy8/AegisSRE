"""Smoke tests for the public landing page routes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from sre_swarm.dashboard.app import app


client = TestClient(app)


def test_landing_page_at_root() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "AegisSRE" in response.text
    assert "already drafted" in response.text
    assert 'href="/dashboard"' in response.text
    assert "Try the demo" in response.text


def test_dashboard_at_dashboard_path() -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "AegisSRE Dashboard" in response.text
    assert "Trigger incident" in response.text
