"""
Test suite for IncidentResponseWorkflow
========================================
Uses Temporal's built-in time-skipping test environment so tests run
in-process — no Docker, no real Temporal server required.

Run with:
    pytest tests/

Test matrix:
  test_happy_path        — analyze_root_cause returns zero compensations → RESOLVED immediately
  test_approval_path     — compensations present, approve_rollback signal fires → RESOLVED
  test_rejection_path    — compensations present, reject_rollback signal fires → FAILED
  test_timeout_path      — no signal within 30 min, time skipped → FAILED (no crash)
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from sre_swarm.activities.mcp_tools import MCPToolRequest, MCPToolResult
from sre_swarm.activities.saga import CompensationRequest
from sre_swarm.workflows.incident_response import (
    IncidentInput,
    IncidentResponseWorkflow,
    IncidentStatus,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

TASK_QUEUE = "test-sre-swarm"

# Collects analyze_root_cause call arguments for test_request_more_info_then_approve.
# Reset to [] at the start of that test.
_rca_calls: list[dict] = []

_BASE_INCIDENT = IncidentInput(
    incident_id="INC-TEST-001",
    affected_service="payment-service",
    alert_summary="HTTP 500 spike on /checkout",
    trace_ids=["trace-abc", "trace-def"],
)


# ---------------------------------------------------------------------------
# Activity mocks
# ---------------------------------------------------------------------------

@activity.defn(name="call_mcp_tool")
async def _mock_call_mcp_tool_no_compensations(request: MCPToolRequest) -> MCPToolResult:
    """Simulates get_telemetry_context + analyze_root_cause with zero compensations."""
    if request.tool_name == "get_telemetry_context":
        return MCPToolResult(
            tool_name="get_telemetry_context",
            output={"spans": [], "cpu_overhead_pct": 2.4},
        )
    # analyze_root_cause — empty compensation list → happy path
    return MCPToolResult(
        tool_name="analyze_root_cause",
        output={
            "root_cause": "transient blip, self-healed",
            "confidence": 0.99,
            "compensations": [],
        },
    )


@activity.defn(name="call_mcp_tool")
async def _mock_call_mcp_tool_two_compensations(request: MCPToolRequest) -> MCPToolResult:
    """Simulates analyze_root_cause returning 2 compensations."""
    if request.tool_name == "get_telemetry_context":
        return MCPToolResult(
            tool_name="get_telemetry_context",
            output={
                "spans": [
                    {
                        "service": "api-gateway",
                        "status_code": 504,
                        "latency_ms": 4823,
                        "error": True,
                    },
                    {
                        "service": "order-service",
                        "status_code": 200,
                        "latency_ms": 93,
                        "error": False,
                    },
                    {
                        "service": "payment-service",
                        "status_code": 500,
                        "latency_ms": 4710,
                        "error": True,
                    },
                ],
                "cpu_overhead_pct": 2.4,
            },
        )
    return MCPToolResult(
        tool_name="analyze_root_cause",
        output={
            "root_cause": "Payment service timeout caused order saga to partially commit",
            "confidence": 0.91,
            "compensations": [
                {"endpoint": "/cancelOrder",   "method": "POST", "payload": {"order_id": "ord-789"}},
                {"endpoint": "/refundPayment", "method": "POST", "payload": {"payment_id": "pay-456"}},
            ],
        },
    )


@activity.defn(name="verify_service_state")
async def _mock_verify_healthy(request) -> dict:
    """Reports every compensated resource as matching the plan."""
    return {
        "healthy": True,
        "findings": ["order ord-789 is cancelled", "payment pay-456 is refunded"],
    }


@activity.defn(name="verify_service_state")
async def _mock_verify_unhealthy(request) -> dict:
    """Order is still open after the compensation calls returned."""
    return {
        "healthy": False,
        "findings": ["order ord-789 is payment_failed, expected cancelled"],
    }


@activity.defn(name="execute_compensating_transaction")
async def _mock_comp_refund_fails(request: CompensationRequest) -> dict:
    """Cancel succeeds; refund fails so the workflow must record a partial result."""
    if "refund" in request.endpoint.lower():
        raise ApplicationError("refund gateway timeout", non_retryable=True)
    return {"status": "ok", "endpoint": request.endpoint}


@activity.defn(name="call_mcp_tool")
async def _mock_call_mcp_tool_high_confidence(request: MCPToolRequest) -> MCPToolResult:
    """Confidence above the auto-approve threshold, with a real compensation plan."""
    if request.tool_name == "get_telemetry_context":
        return MCPToolResult(
            tool_name="get_telemetry_context",
            output={"spans": [{"service": "payment-service", "error": False}], "cpu_overhead_pct": 2.4},
        )
    return MCPToolResult(
        tool_name="analyze_root_cause",
        output={
            "root_cause": "Payment service timeout caused order saga to partially commit",
            "confidence": 0.99,
            "compensations": [
                {"endpoint": "/cancelOrder", "method": "POST", "payload": {"order_id": "ord-789"}},
                {"endpoint": "/refundPayment", "method": "POST", "payload": {"payment_id": "pay-456"}},
            ],
        },
    )


@activity.defn(name="execute_compensating_transaction")
async def _mock_execute_compensating_transaction(request: CompensationRequest) -> dict:
    """Returns a success dict without making any HTTP calls."""
    return {"status": "ok", "endpoint": request.endpoint}


@activity.defn(name="call_mcp_tool")
async def _mock_call_mcp_tool_tracking(request: MCPToolRequest) -> MCPToolResult:
    """Records every analyze_root_cause invocation into _rca_calls for assertion."""
    if request.tool_name == "get_telemetry_context":
        return MCPToolResult(
            tool_name="get_telemetry_context",
            output={"spans": [], "cpu_overhead_pct": 2.4},
        )
    _rca_calls.append(dict(request.arguments))
    return MCPToolResult(
        tool_name="analyze_root_cause",
        output={
            "root_cause": "Payment service timeout caused order saga to partially commit",
            "confidence": 0.91,
            "compensations": [
                {"endpoint": "/cancelOrder", "method": "POST", "payload": {"order_id": "ord-789"}},
            ],
        },
    )


# ---------------------------------------------------------------------------
# Test 1 — Happy path (zero compensations → RESOLVED immediately)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path() -> None:
    """
    When analyze_root_cause returns no compensations the workflow must resolve
    immediately — no signal required, no compensating transactions executed.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IncidentResponseWorkflow],
            activities=[
                _mock_call_mcp_tool_no_compensations,
                _mock_execute_compensating_transaction,
            ],
        ):
            result: str = await env.client.execute_workflow(
                IncidentResponseWorkflow.run,
                _BASE_INCIDENT,
                id="test-happy-path",
                task_queue=TASK_QUEUE,
            )

    assert result == "No compensating transactions required."


# ---------------------------------------------------------------------------
# Test 2 — Approval path (compensations + approve signal → RESOLVED)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approval_path() -> None:
    """
    When compensations are present the workflow waits for operator approval.
    Sending approve_rollback should let it run both compensating transactions
    and reach RESOLVED.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IncidentResponseWorkflow],
            activities=[
                _mock_call_mcp_tool_two_compensations,
                _mock_execute_compensating_transaction,
                _mock_verify_healthy,
            ],
        ):
            handle = await env.client.start_workflow(
                IncidentResponseWorkflow.run,
                _BASE_INCIDENT,
                id="test-approval-path",
                task_queue=TASK_QUEUE,
            )

            # Poll until the workflow enters REMEDIATING (waiting for signal).
            # auto_time_skipping is suspended here so we don't accidentally
            # skip past the 30-minute timeout before sending the signal.
            with env.auto_time_skipping_disabled():
                for _ in range(20):
                    state: dict = await handle.query(IncidentResponseWorkflow.get_status)
                    if state["status"] == IncidentStatus.REMEDIATING:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail(
                        f"Workflow never reached REMEDIATING; last status: {state['status']}"
                    )

                services = {e["service"] for e in state["error_spans"]}
                assert services == {"api-gateway", "payment-service"}

            await handle.signal(IncidentResponseWorkflow.approve_rollback)
            result: str = await handle.result()

    assert "Resolved" in result
    assert "2 compensating transaction(s)" in result


# ---------------------------------------------------------------------------
# Test 3 — Rejection path (reject_rollback signal → FAILED with reason)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejection_path() -> None:
    """
    Sending reject_rollback with a reason string must cause the workflow to
    set status=FAILED and embed the reason in resolution_notes.
    """
    rejection_reason = "Rollback too risky during peak traffic"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IncidentResponseWorkflow],
            activities=[
                _mock_call_mcp_tool_two_compensations,
                _mock_execute_compensating_transaction,
            ],
        ):
            handle = await env.client.start_workflow(
                IncidentResponseWorkflow.run,
                _BASE_INCIDENT,
                id="test-rejection-path",
                task_queue=TASK_QUEUE,
            )

            # Wait for REMEDIATING before sending the rejection signal.
            with env.auto_time_skipping_disabled():
                for _ in range(20):
                    state: dict = await handle.query(IncidentResponseWorkflow.get_status)
                    if state["status"] == IncidentStatus.REMEDIATING:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail(
                        f"Workflow never reached REMEDIATING; last status: {state['status']}"
                    )

            await handle.signal(IncidentResponseWorkflow.reject_rollback, rejection_reason)
            result: str = await handle.result()

    assert rejection_reason in result
    assert "Rollback rejected by operator" in result


# ---------------------------------------------------------------------------
# Test 4 — Timeout path (no signal → FAILED, not an exception crash)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_path() -> None:
    """
    If neither approve_rollback nor reject_rollback is sent within 30 minutes
    the workflow must return FAILED with a timeout message — not raise an
    unhandled exception.

    WorkflowEnvironment.start_time_skipping() automatically advances virtual
    time when awaiting a workflow result, so the 30-minute wait_condition
    timeout fires without real wall-clock delay.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IncidentResponseWorkflow],
            activities=[
                _mock_call_mcp_tool_two_compensations,
                _mock_execute_compensating_transaction,
            ],
        ):
            # No signal sent — let time skip past the 30-minute approval window.
            result: str = await env.client.execute_workflow(
                IncidentResponseWorkflow.run,
                _BASE_INCIDENT,
                id="test-timeout-path",
                task_queue=TASK_QUEUE,
            )

    assert "Timed out" in result
    assert "30 min" in result


# ---------------------------------------------------------------------------
# Test 5 — request_more_info path (re-analysis with extra context → RESOLVED)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_more_info_then_approve() -> None:
    """
    Operator sends request_more_info("is the DB involved?") before approving.

    Assertions:
      1. analyze_root_cause is called a second time.
      2. The second call's arguments include the question in extra_context.
      3. Sending approve_rollback after that resolves the workflow as RESOLVED.
    """
    global _rca_calls
    _rca_calls = []  # reset before this test

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IncidentResponseWorkflow],
            activities=[
                _mock_call_mcp_tool_tracking,
                _mock_execute_compensating_transaction,
                _mock_verify_healthy,
            ],
        ):
            handle = await env.client.start_workflow(
                IncidentResponseWorkflow.run,
                _BASE_INCIDENT,
                id="test-more-info-path",
                task_queue=TASK_QUEUE,
            )

            # Wait for the first REMEDIATING before sending the question.
            with env.auto_time_skipping_disabled():
                for _ in range(20):
                    state: dict = await handle.query(IncidentResponseWorkflow.get_status)
                    if state["status"] == IncidentStatus.REMEDIATING:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail(
                        f"Workflow never reached REMEDIATING; last status: {state['status']}"
                    )

            # Signal the question — workflow sets status=ANALYZING, wait_condition unblocks,
            # the while loop fires, re-runs RCA, then sets status back to REMEDIATING.
            await handle.signal(IncidentResponseWorkflow.request_more_info, "is the DB involved?")

            # Wait for the workflow to re-enter REMEDIATING after the second RCA run.
            with env.auto_time_skipping_disabled():
                for _ in range(20):
                    state = await handle.query(IncidentResponseWorkflow.get_status)
                    if state["status"] == IncidentStatus.REMEDIATING:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail(
                        f"Workflow never returned to REMEDIATING after re-analysis; "
                        f"last status: {state['status']}"
                    )

            # Now approve — workflow proceeds to COMPENSATING → RESOLVED.
            await handle.signal(IncidentResponseWorkflow.approve_rollback)
            result: str = await handle.result()

    # analyze_root_cause must have been called exactly twice (initial + re-run).
    assert len(_rca_calls) == 2, f"Expected 2 RCA calls, got {len(_rca_calls)}"

    # Second call must carry the question in extra_context.
    second_call_args = _rca_calls[1]
    assert "extra_context" in second_call_args
    assert "is the DB involved?" in second_call_args["extra_context"]

    # Final outcome must be RESOLVED.
    assert "Resolved" in result


# ---------------------------------------------------------------------------
# Test 6 — high confidence skips the operator and still verifies
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_approve_high_confidence() -> None:
    """
    Confidence >= 0.95 with a compensation plan must run the rollback without
    a signal, then resolve only after verification reports the services healthy.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IncidentResponseWorkflow],
            activities=[
                _mock_call_mcp_tool_high_confidence,
                _mock_execute_compensating_transaction,
                _mock_verify_healthy,
            ],
        ):
            handle = await env.client.start_workflow(
                IncidentResponseWorkflow.run,
                _BASE_INCIDENT,
                id="test-auto-approve",
                task_queue=TASK_QUEUE,
            )
            result: str = await handle.result()
            state: dict = await handle.query(IncidentResponseWorkflow.get_status)

    assert "Auto-approved" in result
    assert "Resolved" in result
    assert state["auto_approved"] is True
    assert state["status"] == IncidentStatus.RESOLVED
    assert state["confidence"] == pytest.approx(0.99)
    assert state["verification"]["healthy"] is True
    assert all(step["ok"] for step in state["compensation_results"])


# ---------------------------------------------------------------------------
# Test 7 — one compensation step fails; the other result is kept
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_compensation_failure_stays_compensating() -> None:
    """
    A failed refund must not discard the successful cancel. The workflow stays
    in COMPENSATING and records which step failed.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IncidentResponseWorkflow],
            activities=[
                _mock_call_mcp_tool_two_compensations,
                _mock_comp_refund_fails,
                _mock_verify_healthy,
            ],
        ):
            handle = await env.client.start_workflow(
                IncidentResponseWorkflow.run,
                _BASE_INCIDENT,
                id="test-partial-failure",
                task_queue=TASK_QUEUE,
            )

            with env.auto_time_skipping_disabled():
                for _ in range(20):
                    state: dict = await handle.query(IncidentResponseWorkflow.get_status)
                    if state["status"] == IncidentStatus.REMEDIATING:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail(f"Workflow never reached REMEDIATING; last status: {state['status']}")

            await handle.signal(IncidentResponseWorkflow.approve_rollback)
            result: str = await handle.result()
            state = await handle.query(IncidentResponseWorkflow.get_status)

    assert "Rollback incomplete" in result
    assert state["status"] == IncidentStatus.COMPENSATING
    by_endpoint = {step["endpoint"]: step for step in state["compensation_results"]}
    assert by_endpoint["/cancelOrder"]["ok"] is True
    assert by_endpoint["/refundPayment"]["ok"] is False
    assert "refund gateway timeout" in by_endpoint["/refundPayment"]["detail"]


# ---------------------------------------------------------------------------
# Test 8 — compensations succeed but the order is still open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unhealthy_verification_stays_compensating() -> None:
    """
    HTTP success is not enough. If the order is still open, the incident stays
    in COMPENSATING and the finding is visible on the workflow status.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IncidentResponseWorkflow],
            activities=[
                _mock_call_mcp_tool_two_compensations,
                _mock_execute_compensating_transaction,
                _mock_verify_unhealthy,
            ],
        ):
            handle = await env.client.start_workflow(
                IncidentResponseWorkflow.run,
                _BASE_INCIDENT,
                id="test-unhealthy-verify",
                task_queue=TASK_QUEUE,
            )

            with env.auto_time_skipping_disabled():
                for _ in range(20):
                    state: dict = await handle.query(IncidentResponseWorkflow.get_status)
                    if state["status"] == IncidentStatus.REMEDIATING:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail(f"Workflow never reached REMEDIATING; last status: {state['status']}")

            await handle.signal(IncidentResponseWorkflow.approve_rollback)
            result: str = await handle.result()
            state = await handle.query(IncidentResponseWorkflow.get_status)

    assert "Rollback incomplete" in result
    assert "payment_failed" in result
    assert state["status"] == IncidentStatus.COMPENSATING
    assert state["verification"]["healthy"] is False
    assert state["verification"]["steps_ok"] is True


# ---------------------------------------------------------------------------
# Test 9 — MCP root-cause failure surfaces FAILED (not stuck analyzing)
# ---------------------------------------------------------------------------

@activity.defn(name="call_mcp_tool")
async def _mock_mcp_rca_fails(request: MCPToolRequest) -> MCPToolResult:
    if request.tool_name == "get_telemetry_context":
        return MCPToolResult(
            tool_name="get_telemetry_context",
            output={"spans": [], "cpu_overhead_pct": 2.4},
        )
    raise ApplicationError("MCP analyze_root_cause unavailable", non_retryable=True)


@pytest.mark.asyncio
async def test_root_cause_activity_failure_marks_failed() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IncidentResponseWorkflow],
            activities=[
                _mock_mcp_rca_fails,
                _mock_execute_compensating_transaction,
                _mock_verify_healthy,
            ],
        ):
            result: str = await env.client.execute_workflow(
                IncidentResponseWorkflow.run,
                _BASE_INCIDENT,
                id="test-rca-failure",
                task_queue=TASK_QUEUE,
            )
            handle = env.client.get_workflow_handle("test-rca-failure")
            state: dict = await handle.query(IncidentResponseWorkflow.get_status)

    assert "Root-cause analysis failed" in result
    assert state["status"] == IncidentStatus.FAILED
