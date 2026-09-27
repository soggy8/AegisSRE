"""Telemetry pipeline personalization tests."""

from __future__ import annotations

from sre_swarm.telemetry.pipeline import get_spans


def test_fixture_spans_differ_per_trace_id() -> None:
    a = get_spans(
        ["trace-timeout-inc-a", "order_id:ord-a", "payment_id:pay-a"],
        "payment-service",
    )
    b = get_spans(
        ["trace-timeout-inc-b", "order_id:ord-b", "payment_id:pay-b"],
        "payment-service",
    )
    assert a and b
    lat_a = {(s["service"], s["latency_ms"]) for s in a}
    lat_b = {(s["service"], s["latency_ms"]) for s in b}
    assert lat_a != lat_b


def test_fixture_spans_are_stable_for_same_trace_id() -> None:
    trace = ["trace-timeout-stable", "order_id:ord-x", "payment_id:pay-x"]
    first = get_spans(trace, "payment-service")
    second = get_spans(trace, "payment-service")
    assert first == second
