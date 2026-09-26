"""
Saga Compensating Transaction Activity
=======================================
Executes a single compensating transaction as part of the Saga rollback plan.

Each compensation is an independent HTTP call to a mock service endpoint
(e.g. /cancelOrder, /refundPayment). If one fails, Temporal retries it
independently without re-running the others.

This is an Activity (not a Workflow) because it performs external I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError


MOCK_SERVICES_BASE_URL = os.getenv("MOCK_SERVICES_URL", "http://localhost:9090")


# ---------------------------------------------------------------------------
# Data model (shared with the Workflow)
# ---------------------------------------------------------------------------

@dataclass
class CompensationRequest:
    """A single compensating transaction derived from the RCA output."""
    endpoint: str           # e.g. "/cancelOrder"
    method: str = "POST"    # HTTP method
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Activity definition
# ---------------------------------------------------------------------------

@activity.defn
async def execute_compensating_transaction(request: CompensationRequest) -> dict:
    """
    Call a mock service endpoint to perform a Saga compensation step.

    The Workflow fans out one of these per pending_compensation entry.
    Temporal retries this Activity on transient failures, making each
    compensation step independently reliable.

    Retries on 5xx responses (non_retryable=False). Returns immediately on 409
    (already compensated) so duplicate fan-out calls are idempotent.
    """
    activity.logger.info(
        "Executing compensating transaction: %s %s", request.method, request.endpoint
    )

    url = f"{MOCK_SERVICES_BASE_URL}{request.endpoint}"
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method=request.method,
            url=url,
            json=request.payload,
            timeout=20.0,
        )
        if response.status_code == 409:
            # Already compensated — idempotent, treat as success.
            return {"status": "already_compensated", "endpoint": request.endpoint}
        if response.status_code >= 500:
            raise ApplicationError(f"Compensation failed: {response.text}", non_retryable=False)
        response.raise_for_status()
        return response.json()
