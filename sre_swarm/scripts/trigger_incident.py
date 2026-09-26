"""
Trigger a test incident workflow.

Seeds a real order and payment in the mock services first so the LLM has
real IDs to work with when building the compensation plan.

Usage:
    python -m sre_swarm.scripts.trigger_incident
"""

import asyncio
import os
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()

from temporalio.client import Client

from sre_swarm.workflows.incident_response import IncidentInput, IncidentResponseWorkflow

TEMPORAL_HOST     = os.getenv("TEMPORAL_HOST", "localhost:7233")
MOCK_SERVICES_URL = os.getenv("MOCK_SERVICES_URL", "http://localhost:9090")
TASK_QUEUE        = "sre-swarm"


async def seed_and_build_incident() -> IncidentInput:
    """
    Place a real order and charge a real payment in the mock services,
    then return an IncidentInput whose trace_ids carry those IDs so the
    telemetry pipeline can embed them into the spans the LLM will read.
    """
    order_id    = f"ord-{uuid.uuid4().hex[:8]}"
    payment_id  = f"pay-{uuid.uuid4().hex[:8]}"
    customer_id = "cust-demo-001"

    async with httpx.AsyncClient() as http:
        # Place the order — may return 500 due to PAYMENT_FAILURE_RATE, but the
        # record is still written to the store so compensations will succeed.
        try:
            await http.post(
                f"{MOCK_SERVICES_URL}/placeOrder",
                json={"order_id": order_id, "items": ["item-a", "item-b"],
                      "customer_id": customer_id},
                timeout=5.0,
            )
        except Exception:
            pass  # 500 is expected sometimes; record is still stored

        # Charge the payment
        await http.post(
            f"{MOCK_SERVICES_URL}/chargePayment",
            json={"payment_id": payment_id, "amount": 99.99,
                  "customer_id": customer_id},
            timeout=5.0,
        )

    print(f"Seeded  order_id={order_id}  payment_id={payment_id}")

    # Embed the IDs as structured trace_ids.
    # The telemetry pipeline reads "order_id:<id>" and "payment_id:<id>" prefixes
    # and annotates the error span so the LLM reads the real values.
    trace_ids = [
        "trace-timeout-001",         # triggers the payment_timeout fixture
        f"order_id:{order_id}",      # carries the real order ID
        f"payment_id:{payment_id}",  # carries the real payment ID
    ]

    return IncidentInput(
        incident_id=f"INC-{uuid.uuid4().hex[:8].upper()}",
        affected_service="payment-service",
        alert_summary=(
            f"HTTP 500 spike on /checkout — p99 latency > 4 s. "
            f"Affected order: {order_id}, payment: {payment_id}"
        ),
        trace_ids=trace_ids,
    )


async def main() -> None:
    incident = await seed_and_build_incident()

    client = await Client.connect(TEMPORAL_HOST)
    handle = await client.start_workflow(
        IncidentResponseWorkflow.run,
        incident,
        id=f"incident-{incident.incident_id}",
        task_queue=TASK_QUEUE,
    )

    print(f"Started workflow  id={handle.id}  run_id={handle.result_run_id}")


if __name__ == "__main__":
    asyncio.run(main())
