"""
Catalog of demo incident scenarios for triggers and the dashboard.

Each scenario maps to a telemetry fixture keyword (see telemetry/pipeline.py)
and produces a distinct alert + affected service while reusing the same
seeded order/payment IDs for saga compensations.
"""

from __future__ import annotations

from dataclasses import dataclass

from sre_swarm.workflows.incident_response import IncidentInput

DEFAULT_SCENARIO_ID = "payment_timeout"


@dataclass(frozen=True)
class DemoScenario:
    id: str
    title: str
    description: str
    affected_service: str
    trace_keyword: str
    alert_template: str


SCENARIOS: dict[str, DemoScenario] = {
    "payment_timeout": DemoScenario(
        id="payment_timeout",
        title="Payment timeout",
        description=(
            "Checkout p99 breach: gateway times out while payment-service returns 500. "
            "Classic partial saga commit."
        ),
        affected_service="payment-service",
        trace_keyword="timeout",
        alert_template=(
            "HTTP 500 spike on /checkout — p99 latency > 4 s. "
            "Affected order: {order_id}, payment: {payment_id}"
        ),
    ),
    "cascade_failure": DemoScenario(
        id="cascade_failure",
        title="Multi-service cascade",
        description=(
            "503/500 across gateway, order, and payment — upstream overload fans out "
            "before compensations run."
        ),
        affected_service="order-service",
        trace_keyword="cascade",
        alert_template=(
            "Cascade failure: order-service error rate > 40% after gateway 503. "
            "Orders {order_id} / payment {payment_id} may be inconsistent."
        ),
    ),
    "gateway_overload": DemoScenario(
        id="gateway_overload",
        title="Gateway overload",
        description=(
            "Edge 504 storm on api-gateway with downstream payment errors. "
            "Alert fires on the gateway owner."
        ),
        affected_service="api-gateway",
        trace_keyword="timeout",
        alert_template=(
            "api-gateway 504 rate > 15% — clients timing out at 5 s. "
            "Sample checkout order {order_id}, payment {payment_id}."
        ),
    ),
    "inventory_outage": DemoScenario(
        id="inventory_outage",
        title="Inventory outage",
        description=(
            "Inventory-service unavailable during checkout; order/payment may have "
            "committed before stock reservation failed."
        ),
        affected_service="inventory-service",
        trace_keyword="inventory",
        alert_template=(
            "inventory-service 503 — cannot reserve stock for order {order_id} "
            "(payment {payment_id} may already be captured)."
        ),
    ),
    "spurious_alert": DemoScenario(
        id="spurious_alert",
        title="False alarm (healthy trace)",
        description=(
            "Alert fired but telemetry shows a clean trace — workflow should resolve "
            "with no compensations."
        ),
        affected_service="payment-service",
        trace_keyword="happy",
        alert_template=(
            "Transient blip on payment-service (auto-recovered?). "
            "Verify order {order_id} / payment {payment_id} — trace looks healthy."
        ),
    ),
}


def list_scenarios() -> list[dict[str, str]]:
    return [
        {
            "id": s.id,
            "title": s.title,
            "description": s.description,
            "affected_service": s.affected_service,
        }
        for s in SCENARIOS.values()
    ]


def get_scenario(scenario_id: str) -> DemoScenario:
    key = scenario_id.strip().lower() if scenario_id else DEFAULT_SCENARIO_ID
    if key not in SCENARIOS:
        known = ", ".join(sorted(SCENARIOS))
        raise ValueError(f"Unknown scenario {scenario_id!r}. Choose one of: {known}")
    return SCENARIOS[key]


def build_incident_input(
    *,
    scenario_id: str,
    incident_id: str,
    order_id: str,
    payment_id: str,
) -> IncidentInput:
    scenario = get_scenario(scenario_id)
    trace_ids = [
        f"trace-{scenario.trace_keyword}-{incident_id.lower()}",
        f"order_id:{order_id}",
        f"payment_id:{payment_id}",
    ]
    alert = scenario.alert_template.format(order_id=order_id, payment_id=payment_id)
    return IncidentInput(
        incident_id=incident_id,
        affected_service=scenario.affected_service,
        alert_summary=alert,
        trace_ids=trace_ids,
    )
