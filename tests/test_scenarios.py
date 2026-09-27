"""Demo incident scenario catalog tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sre_swarm.dashboard.app import app
from sre_swarm.incidents.scenarios import build_incident_input, get_scenario
from sre_swarm.telemetry.pipeline import get_spans


def test_each_scenario_builds_distinct_alert_service() -> None:
    ids = set()
    for key in ("payment_timeout", "cascade_failure", "gateway_overload", "inventory_outage", "spurious_alert"):
        inc = build_incident_input(
            scenario_id=key,
            incident_id="INC-TEST",
            order_id="ord-x",
            payment_id="pay-y",
        )
        ids.add((inc.affected_service, inc.alert_summary[:40]))
    assert len(ids) == 5


def test_unknown_scenario_raises() -> None:
    with pytest.raises(ValueError, match="Unknown scenario"):
        get_scenario("not-a-real-scenario")


def test_inventory_fixture_has_error_span() -> None:
    inc = build_incident_input(
        scenario_id="inventory_outage",
        incident_id="INC-INV",
        order_id="ord-a",
        payment_id="pay-b",
    )
    spans = get_spans(inc.trace_ids, inc.affected_service)
    errors = [s for s in spans if s.get("error")]
    assert any(s.get("service") == "inventory-service" for s in errors)


def test_spurious_alert_has_no_error_spans() -> None:
    inc = build_incident_input(
        scenario_id="spurious_alert",
        incident_id="INC-HAPPY",
        order_id="ord-a",
        payment_id="pay-b",
    )
    spans = get_spans(inc.trace_ids, inc.affected_service)
    assert spans
    assert not any(s.get("error") for s in spans)


def test_scenarios_api_lists_all() -> None:
    client = TestClient(app)
    r = client.get("/scenarios")
    assert r.status_code == 200
    body = r.json()
    assert len(body) >= 5
    assert {s["id"] for s in body} >= {
        "payment_timeout",
        "cascade_failure",
        "gateway_overload",
        "inventory_outage",
        "spurious_alert",
    }
