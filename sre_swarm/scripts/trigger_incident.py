"""
Trigger a test incident workflow.

Seeds a real order and payment in the mock services first, then starts
the IncidentResponseWorkflow with structured trace IDs so the telemetry
pipeline can surface the real IDs to the LLM.

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


async def seed_incident(http: httpx.AsyncClient) -> tuple[str, str]:
    """
    Create a real order and payment in the mock services.

    Returns (order_id, payment_id).  The /placeOrder call may return 500
    (~30 % of the time due to PAYMENT_FAILURE_RATE) but the record is still
    written, so we swallow that exception.
    """
    order_id    = f"ord-{uuid.uuid4().hex[:8]}"
    payment_id  = f"pay-{uuid.uuid4().hex[:8]}"
    customer_id = "cust-demo-001"

    try:
        await http.post(
            f"{MOCK_SERVICES_URL}/placeOrder",
            json={"order_id": order_id, "items": ["item-a", "item-b"],
                  "customer_id": customer_id},
            timeout=10.0,
        )
    except Exception:
        # 500 is expected sometimes; the record is still written by the service.
        pass

    await http.post(
        f"{MOCK_SERVICES_URL}/chargePayment",
        json={"payment_id": payment_id, "amount": 99.99,
              "customer_id": customer_id},
        timeout=10.0,
    )

    return order_id, payment_id


async def main() -> None:
    async with httpx.AsyncClient() as http:
        order_id, payment_id = await seed_incident(http)

    print(f"Seeded  order_id={order_id}  payment_id={payment_id}")

    # Embed the real IDs into trace_ids so the telemetry pipeline can annotate
    # the error span with them.  The "timeout" substring triggers the
    # payment_timeout.json fixture in pipeline._try_load_fixture().
    trace_ids = [
        f"trace-timeout-{order_id}",   # fixture key ("timeout" substring)
        f"order_id:{order_id}",        # parsed by pipeline._extract_ids()
        f"payment_id:{payment_id}",    # parsed by pipeline._extract_ids()
    ]

    client = await Client.connect(TEMPORAL_HOST)
    inc_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    handle = await client.start_workflow(
        IncidentResponseWorkflow.run,
        IncidentInput(
            incident_id=inc_id,
            affected_service="payment-service",
            alert_summary=(
                f"HTTP 500 spike on /checkout — p99 latency > 4 s. "
                f"Affected order: {order_id}, payment: {payment_id}"
            ),
            trace_ids=trace_ids,
        ),
        id=f"incident-{inc_id}",
        task_queue=TASK_QUEUE,
    )

    print(f"Started workflow  id={handle.id}  run_id={handle.result_run_id}")
    print("Send approval signal with:")
    print(f"  temporal workflow signal --workflow-id {handle.id} --name approve_rollback")


if __name__ == "__main__":
    asyncio.run(main())
