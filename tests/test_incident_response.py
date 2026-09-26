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
from temporalio.client import WorkflowFailureError
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
            output={"spans": [], "cpu_overhead_pct": 2.4},
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
