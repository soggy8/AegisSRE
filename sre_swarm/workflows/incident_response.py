"""
Incident Response Workflow
==========================
This is the core Temporal Workflow that drives the autonomous SRE agent loop.

DETERMINISM RULES (enforced by Temporal's replay engine):
  - No I/O of any kind inside this file.
  - No random numbers, no datetime.now(), no network calls.
  - All side-effects go through `workflow.execute_activity()` calls.
  - Use `workflow.now()` instead of `datetime.now()` if you need the current time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

# Activity imports are wrapped in a sandbox-safe import block.
# Temporal's workflow sandbox intercepts these; they are never actually
# executed inside the workflow — only their signatures are used.
with workflow.unsafe.imports_passed_through():
    from sre_swarm.activities.mcp_tools import call_mcp_tool, MCPToolRequest, MCPToolResult
    from sre_swarm.activities.saga import execute_compensating_transaction, CompensationRequest


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class IncidentStatus(str, Enum):
    DETECTING    = "detecting"
    ANALYZING    = "analyzing"
    REMEDIATING  = "remediating"
    COMPENSATING = "compensating"
    RESOLVED     = "resolved"
    FAILED       = "failed"


@dataclass
class IncidentInput:
    """Passed when starting the workflow (e.g. from an alerting webhook)."""
    incident_id: str
    affected_service: str
    alert_summary: str
    # Span/trace IDs gathered by the telemetry pipeline before triggering
    trace_ids: list[str] = field(default_factory=list)


@dataclass
class IncidentState:
    """Mutable state tracked across the workflow execution."""
    status: IncidentStatus = IncidentStatus.DETECTING
    root_cause: Optional[str] = None
    # Compensating transactions that still need to be applied
    pending_compensations: list[CompensationRequest] = field(default_factory=list)
    # Human approval: set via Signal when operator approves the rollback plan
    rollback_approved: bool = False
    resolution_notes: str = ""


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

@workflow.defn
class IncidentResponseWorkflow:
    """
    Main SRE agent orchestration loop.

    Lifecycle:
      1. Fetch telemetry context for the incident via MCP tool call.
      2. Run root-cause analysis (LLM inference) via MCP tool call.
      3. Build a Saga compensation plan.
      4. Wait for human approval (Signal) if required.
      5. Execute compensating transactions to restore consistency.
      6. Mark incident resolved.

    The workflow is fully resumable: if the worker crashes at any step,
    Temporal replays the event history and picks up exactly where it left off.
    """

    def __init__(self) -> None:
        self._state = IncidentState()

    # ------------------------------------------------------------------
    # Signals — called externally (e.g. from an ops dashboard or Slack bot)
    # ------------------------------------------------------------------

    @workflow.signal
    def approve_rollback(self) -> None:
        """Operator signals that the proposed rollback plan is approved."""
        self._state.rollback_approved = True

    @workflow.signal
    def reject_rollback(self, reason: str = "") -> None:
        """Operator rejects the plan; the workflow will mark itself failed."""
        self._state.resolution_notes = f"Rollback rejected by operator: {reason}"
        self._state.status = IncidentStatus.FAILED

    # ------------------------------------------------------------------
    # Queries — read-only inspection without side-effects
    # ------------------------------------------------------------------

    @workflow.query
    def get_status(self) -> dict:
        return {
            "status": self._state.status,
            "root_cause": self._state.root_cause,
            "rollback_approved": self._state.rollback_approved,
            "resolution_notes": self._state.resolution_notes,
        }

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, incident: IncidentInput) -> str:
        workflow.logger.info(
            "Incident response started",
            extra={"incident_id": incident.incident_id, "service": incident.affected_service},
        )

        retry = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))

        # -----------------------------------------------------------------
        # Step 1 — Fetch telemetry context for affected traces
        # -----------------------------------------------------------------
        self._state.status = IncidentStatus.DETECTING
        telemetry_result: MCPToolResult = await workflow.execute_activity(
            call_mcp_tool,
            MCPToolRequest(
                tool_name="get_telemetry_context",
                arguments={"trace_ids": incident.trace_ids, "service": incident.affected_service},
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )

        # -----------------------------------------------------------------
        # Step 2 — Root-cause analysis via LLM (non-deterministic → Activity)
        # -----------------------------------------------------------------
        self._state.status = IncidentStatus.ANALYZING
        rca_result: MCPToolResult = await workflow.execute_activity(
            call_mcp_tool,
            MCPToolRequest(
                tool_name="analyze_root_cause",
                arguments={
                    "alert_summary": incident.alert_summary,
                    "telemetry_context": telemetry_result.output,
                },
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )
        self._state.root_cause = rca_result.output.get("root_cause", "unknown")
        self._state.pending_compensations = [
            CompensationRequest(**c)
            for c in rca_result.output.get("compensations", [])
        ]

        workflow.logger.info(
            "Root cause identified",
            extra={"root_cause": self._state.root_cause, "compensations": len(self._state.pending_compensations)},
        )

        if not self._state.pending_compensations:
            self._state.status = IncidentStatus.RESOLVED
            self._state.resolution_notes = "No compensating transactions required."
            return self._state.resolution_notes

        # -----------------------------------------------------------------
        # Step 3 — Wait for human approval (or timeout after 30 minutes)
        # -----------------------------------------------------------------
        self._state.status = IncidentStatus.REMEDIATING
        try:
            await workflow.wait_condition(
                lambda: self._state.rollback_approved or self._state.status == IncidentStatus.FAILED,
                timeout=timedelta(minutes=30),
            )
        except asyncio.TimeoutError:
            self._state.status = IncidentStatus.FAILED
            self._state.resolution_notes = "Timed out waiting for operator approval (30 min)."
            return self._state.resolution_notes

        if self._state.status == IncidentStatus.FAILED:
            return self._state.resolution_notes

        # -----------------------------------------------------------------
        # Step 4 — Execute Saga compensating transactions
        # -----------------------------------------------------------------
        self._state.status = IncidentStatus.COMPENSATING
        compensation_tasks = [
            workflow.execute_activity(
                execute_compensating_transaction,
                comp,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry,
            )
            for comp in self._state.pending_compensations
        ]
        await asyncio.gather(*compensation_tasks)

        self._state.status = IncidentStatus.RESOLVED
        self._state.resolution_notes = (
            f"Resolved. Root cause: {self._state.root_cause}. "
            f"Applied {len(self._state.pending_compensations)} compensating transaction(s)."
        )
        workflow.logger.info("Incident resolved", extra={"notes": self._state.resolution_notes})
        return self._state.resolution_notes
