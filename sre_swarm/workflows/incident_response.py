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
    from temporalio.exceptions import ActivityError

    from sre_swarm.activities.mcp_tools import call_mcp_tool, MCPToolRequest, MCPToolResult
    from sre_swarm.activities.saga import execute_compensating_transaction, CompensationRequest
    from sre_swarm.activities.verify import (
        verify_service_state,
        VerificationRequest,
        ResourceCheck,
    )


# Plans at or above this confidence run without waiting for an operator.
# Lower confidence stays in REMEDIATING until approve, reject, or timeout.
AUTO_APPROVE_CONFIDENCE = 0.95


def _summarize_error_spans(spans: list[dict]) -> list[dict]:
    """Compact error-span list for dashboard display (deterministic, no I/O)."""
    summary: list[dict] = []
    for span in spans:
        if not span.get("error"):
            continue
        summary.append({
            "service": str(span.get("service") or "unknown"),
            "status_code": span.get("status_code"),
            "latency_ms": span.get("latency_ms"),
        })
    return summary


def _error_detail(outcome: BaseException) -> str:
    """Prefer the activity's own message over Temporal's wrapper text."""
    messages: list[str] = []
    current: BaseException | None = outcome
    for _ in range(8):
        if current is None:
            break
        text = str(getattr(current, "message", "") or current).strip()
        if text and text not in messages and text != "Activity task failed":
            messages.append(text)
        nxt = current.__cause__
        current = nxt if isinstance(nxt, BaseException) else None
    return " — ".join(messages) if messages else str(outcome)


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
    confidence: Optional[float] = None
    # True when confidence was high enough to skip the operator wait.
    auto_approved: bool = False
    # Compensating transactions that still need to be applied
    pending_compensations: list[CompensationRequest] = field(default_factory=list)
    # Per-step outcome of the last compensation attempt.
    compensation_results: list[dict] = field(default_factory=list)
    # Post-rollback check of mock-service state plus a fresh telemetry read.
    verification: dict = field(default_factory=dict)
    # Human approval: set via Signal when operator approves the rollback plan
    rollback_approved: bool = False
    resolution_notes: str = ""
    extra_context: list[str] = field(default_factory=list)
    # Error spans from the initial telemetry pull (may span multiple services).
    error_spans: list[dict] = field(default_factory=list)


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
      4. Auto-apply the plan when confidence >= 0.95; otherwise wait for
         a human signal (approve, reject, or a follow-up question).
      5. Execute compensating transactions and keep each step's result.
      6. Re-read telemetry and the mock services. Resolve only when every
         step succeeded and order/payment state matches the plan. Otherwise
         stay in COMPENSATING so the dashboard can show what is still open.

    The workflow is fully resumable: if the worker crashes at any step,
    Temporal replays the event history and picks up exactly where it left off.
    """

    def __init__(self) -> None:
        self._state = IncidentState()
        self._input: Optional[IncidentInput] = None

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

    @workflow.signal
    def request_more_info(self, question: str)  -> None:
        """Operator asks a follow-up question; triggers re-analysis with extra context."""
        self._state.extra_context.append(question)
        self._state.rollback_approved = False
        self._state.auto_approved = False
        self._state.status = IncidentStatus.ANALYZING

    # ------------------------------------------------------------------
    # Queries — read-only inspection without side-effects
    # ------------------------------------------------------------------

    @workflow.query
    def get_status(self) -> dict:
        return {
            "status": self._state.status,
            "affected_service": self._input.affected_service if self._input else None,
            "alert_summary": self._input.alert_summary if self._input else None,
            "root_cause": self._state.root_cause,
            "confidence": self._state.confidence,
            "auto_approved": self._state.auto_approved,
            "rollback_approved": self._state.rollback_approved,
            "resolution_notes": self._state.resolution_notes,
            "compensation_count": len(self._state.pending_compensations),
            "compensations": [
                {"endpoint": c.endpoint, "method": c.method, "payload": c.payload}
                for c in self._state.pending_compensations
            ],
            "compensation_results": self._state.compensation_results,
            "verification": self._state.verification,
            "extra_context": list(self._state.extra_context),
            "error_spans": list(self._state.error_spans),
        }

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, incident: IncidentInput) -> str:
        self._input = incident
        workflow.logger.info(
            "Incident response started",
            extra={"incident_id": incident.incident_id, "service": incident.affected_service},
        )

        retry = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))

        # -----------------------------------------------------------------
        # Step 1 — Fetch telemetry context for affected traces
        # -----------------------------------------------------------------
        self._state.status = IncidentStatus.DETECTING
        try:
            telemetry_result = await self._fetch_telemetry(incident, retry)
        except ActivityError as exc:
            return self._fail_from_activity("Telemetry fetch", exc)

        self._state.error_spans = _summarize_error_spans(
            telemetry_result.output.get("spans", []),
        )

        # -----------------------------------------------------------------
        # Step 2 — Root-cause analysis via LLM (non-deterministic → Activity)
        # -----------------------------------------------------------------
        self._state.status = IncidentStatus.ANALYZING
        try:
            rca_result = await self._analyze(incident, telemetry_result.output, retry)
        except ActivityError as exc:
            return self._fail_from_activity("Root-cause analysis", exc)
        self._record_analysis(rca_result)

        while True:
            if not self._state.pending_compensations:
                self._state.status = IncidentStatus.RESOLVED
                self._state.resolution_notes = "No compensating transactions required."
                return self._state.resolution_notes

            if self._state.auto_approved:
                workflow.logger.info(
                    "Auto-approving rollback",
                    extra={"confidence": self._state.confidence},
                )
                break

            # -----------------------------------------------------------------
            # Step 3 — Wait for human approval (or timeout after 30 minutes)
            # -----------------------------------------------------------------
            terminal = await self._wait_for_operator()
            if terminal is not None:
                return terminal

            if self._state.status != IncidentStatus.ANALYZING:
                break

            # -----------------------------------------------------------------
            # Step 3b — Re-run root-cause analysis with the operator's question
            # -----------------------------------------------------------------
            workflow.logger.info(
                "Re-running root-cause analysis with extra context",
                extra={"extra_context": self._state.extra_context},
            )
            try:
                rca_result = await self._analyze(incident, telemetry_result.output, retry)
            except ActivityError as exc:
                return self._fail_from_activity("Root-cause re-analysis", exc)
            self._record_analysis(rca_result)

        # -----------------------------------------------------------------
        # Step 4 — Execute Saga compensating transactions
        # -----------------------------------------------------------------
        steps_ok = await self._run_compensations(retry)

        # -----------------------------------------------------------------
        # Step 5 — Confirm the system matches the plan
        # -----------------------------------------------------------------
        try:
            return await self._verify_and_finish(incident, steps_ok, retry)
        except ActivityError as exc:
            return self._fail_from_activity("Post-rollback verification", exc)

    def _fail_from_activity(self, step: str, exc: ActivityError) -> str:
        """Mark the incident failed when an activity exhausts Temporal retries."""
        detail = _error_detail(exc)
        self._state.status = IncidentStatus.FAILED
        self._state.resolution_notes = f"{step} failed after retries: {detail}"
        workflow.logger.error(
            "Activity failure",
            extra={"step": step, "detail": detail},
        )
        return self._state.resolution_notes

    def _record_analysis(self, rca_result: MCPToolResult) -> None:
        output = rca_result.output
        self._state.root_cause = output.get("root_cause", "unknown")
        try:
            self._state.confidence = float(output.get("confidence", 0.0))
        except (TypeError, ValueError):
            self._state.confidence = 0.0
        self._state.pending_compensations = [
            CompensationRequest(**c)
            for c in output.get("compensations", [])
        ]
        self._state.auto_approved = (
            bool(self._state.pending_compensations)
            and self._state.confidence >= AUTO_APPROVE_CONFIDENCE
        )
        if self._state.auto_approved:
            self._state.rollback_approved = True
        workflow.logger.info(
            "Root cause identified",
            extra={
                "root_cause": self._state.root_cause,
                "confidence": self._state.confidence,
                "auto_approved": self._state.auto_approved,
                "compensations": len(self._state.pending_compensations),
            },
        )

    async def _fetch_telemetry(self, incident: IncidentInput, retry: RetryPolicy) -> MCPToolResult:
        return await workflow.execute_activity(
            call_mcp_tool,
            MCPToolRequest(
                tool_name="get_telemetry_context",
                arguments={"trace_ids": incident.trace_ids, "service": incident.affected_service},
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )

    async def _analyze(
        self,
        incident: IncidentInput,
        telemetry_output: dict,
        retry: RetryPolicy,
    ) -> MCPToolResult:
        return await workflow.execute_activity(
            call_mcp_tool,
            MCPToolRequest(
                tool_name="analyze_root_cause",
                arguments={
                    "alert_summary": incident.alert_summary,
                    "telemetry_context": telemetry_output,
                    "extra_context": list(self._state.extra_context),
                },
            ),
            start_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=timedelta(seconds=15),
            retry_policy=retry,
        )

    async def _wait_for_operator(self) -> str | None:
        """
        Block until the operator approves, rejects, or asks a follow-up.

        Returns a terminal resolution note when the workflow should stop,
        or None when execution should continue (approved, or re-analysis).
        """
        self._state.status = IncidentStatus.REMEDIATING
        try:
            await workflow.wait_condition(
                lambda: (
                    self._state.rollback_approved
                    or self._state.status == IncidentStatus.FAILED
                    or self._state.status == IncidentStatus.ANALYZING
                ),
                timeout=timedelta(minutes=30),
            )
        except asyncio.TimeoutError:
            self._state.status = IncidentStatus.FAILED
            self._state.resolution_notes = "Timed out waiting for operator approval (30 min)."
            return self._state.resolution_notes

        if self._state.status == IncidentStatus.FAILED:
            return self._state.resolution_notes
        return None

    async def _run_compensations(self, retry: RetryPolicy) -> bool:
        """
        Fan out compensating transactions and keep each step's outcome.

        A failed step does not abort the others. Returns True only when
        every step completed without an exception.
        """
        self._state.status = IncidentStatus.COMPENSATING
        tasks = [
            workflow.execute_activity(
                execute_compensating_transaction,
                comp,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry,
            )
            for comp in self._state.pending_compensations
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[dict] = []
        all_ok = True
        for comp, outcome in zip(self._state.pending_compensations, outcomes):
            if isinstance(outcome, BaseException):
                all_ok = False
                detail = _error_detail(outcome)
                ok = False
            else:
                detail = str(outcome.get("status", "ok")) if isinstance(outcome, dict) else "ok"
                ok = True
            results.append({
                "endpoint": comp.endpoint,
                "method": comp.method,
                "payload": comp.payload,
                "ok": ok,
                "detail": detail,
            })
        self._state.compensation_results = results
        return all_ok

    async def _verify_and_finish(
        self,
        incident: IncidentInput,
        steps_ok: bool,
        retry: RetryPolicy,
    ) -> str:
        """
        Re-read telemetry and mock-service state. Resolve only when the
        compensation steps succeeded and orders/payments match the plan.
        """
        fresh_telemetry = await self._fetch_telemetry(incident, retry)
        spans = fresh_telemetry.output.get("spans", [])
        error_count = sum(1 for span in spans if span.get("error"))
        telemetry_finding = (
            f"Post-rollback telemetry: {error_count} error span(s) on the original traces."
        )

        service_result: dict = await workflow.execute_activity(
            verify_service_state,
            VerificationRequest(checks=self._service_checks()),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )
        findings = list(service_result.get("findings", []))
        findings.append(telemetry_finding)
        service_healthy = bool(service_result.get("healthy", False))
        healthy = steps_ok and service_healthy
        self._state.verification = {
            "healthy": healthy,
            "service_healthy": service_healthy,
            "steps_ok": steps_ok,
            "findings": findings,
            "error_span_count": error_count,
        }

        failed_steps = [r for r in self._state.compensation_results if not r["ok"]]
        finding_text = " ".join(findings)
        if healthy:
            prefix = ""
            if self._state.auto_approved:
                prefix = (
                    f"Auto-approved (confidence {self._state.confidence:.2f} "
                    f">= {AUTO_APPROVE_CONFIDENCE:.2f}). "
                )
            self._state.status = IncidentStatus.RESOLVED
            self._state.resolution_notes = (
                f"{prefix}Resolved. Root cause: {self._state.root_cause}. "
                f"Applied {len(self._state.pending_compensations)} compensating transaction(s). "
                f"{finding_text}"
            )
        else:
            self._state.status = IncidentStatus.COMPENSATING
            step_text = ""
            if failed_steps:
                described = ", ".join(
                    f"{step['method']} {step['endpoint']} ({step['detail']})"
                    for step in failed_steps
                )
                step_text = f"Failed steps: {described}. "
            self._state.resolution_notes = (
                f"Rollback incomplete. Root cause: {self._state.root_cause}. "
                f"{step_text}{finding_text}"
            )

        workflow.logger.info(
            "Verification finished",
            extra={"healthy": healthy, "notes": self._state.resolution_notes},
        )
        return self._state.resolution_notes

    def _service_checks(self) -> list[ResourceCheck]:
        checks: list[ResourceCheck] = []
        for comp in self._state.pending_compensations:
            endpoint = comp.endpoint.rstrip("/").split("/")[-1]
            if endpoint == "cancelOrder" and comp.payload.get("order_id"):
                checks.append(ResourceCheck(
                    resource_type="order",
                    resource_id=str(comp.payload["order_id"]),
                    expected_status="cancelled",
                ))
            elif endpoint == "refundPayment" and comp.payload.get("payment_id"):
                checks.append(ResourceCheck(
                    resource_type="payment",
                    resource_id=str(comp.payload["payment_id"]),
                    expected_status="refunded",
                ))
        return checks
