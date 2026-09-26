"""
Post-rollback verification
==========================
Reads order and payment state from the mock services after Saga compensations
run. The workflow uses this to decide whether the incident is actually resolved.

This is an Activity because it performs HTTP I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx
from temporalio import activity


MOCK_SERVICES_BASE_URL = os.getenv("MOCK_SERVICES_URL", "http://localhost:9090")


@dataclass
class ResourceCheck:
    """One resource the compensation plan claimed to fix."""
    resource_type: str       # "order" or "payment"
    resource_id: str
    expected_status: str     # "cancelled" or "refunded"


@dataclass
class VerificationRequest:
    """Checks derived from the compensation plan."""
    checks: list[ResourceCheck] = field(default_factory=list)


@activity.defn
async def verify_service_state(request: VerificationRequest) -> dict:
    """
    Confirm each compensated order is cancelled and each payment is refunded.

    Returns:
        {
            "healthy": bool,
            "findings": list[str],
        }

    A missing resource (HTTP 404) counts as already cleared. Any other
    unexpected status, including an order that is still open or a payment
    that is still charged, marks the check unhealthy. Network errors are
    unhealthy and are retried by Temporal like any other activity failure
    only when this function raises; HTTP error statuses are reported in
    findings so the workflow can stay in COMPENSATING instead of crashing.
    """
    findings: list[str] = []
    healthy = True

    async with httpx.AsyncClient() as client:
        for check in request.checks:
            if check.resource_type == "order":
                path = f"/orders/{check.resource_id}"
            elif check.resource_type == "payment":
                path = f"/payments/{check.resource_id}"
            else:
                healthy = False
                findings.append(f"unknown resource type {check.resource_type!r}")
                continue

            url = f"{MOCK_SERVICES_BASE_URL}{path}"
            try:
                response = await client.get(url, timeout=10.0)
            except httpx.HTTPError as exc:
                healthy = False
                findings.append(
                    f"{check.resource_type} {check.resource_id} could not be read: {exc}"
                )
                continue

            if response.status_code == 404:
                findings.append(
                    f"{check.resource_type} {check.resource_id} not found; treated as already cleared"
                )
                continue

            if response.status_code >= 400:
                healthy = False
                findings.append(
                    f"{check.resource_type} {check.resource_id} returned HTTP {response.status_code}"
                )
                continue

            actual = response.json().get("status")
            if actual != check.expected_status:
                healthy = False
                findings.append(
                    f"{check.resource_type} {check.resource_id} is {actual}, "
                    f"expected {check.expected_status}"
                )
            else:
                findings.append(
                    f"{check.resource_type} {check.resource_id} is {actual}"
                )

    activity.logger.info(
        "Verification healthy=%s findings=%d", healthy, len(findings)
    )
    return {"healthy": healthy, "findings": findings}
